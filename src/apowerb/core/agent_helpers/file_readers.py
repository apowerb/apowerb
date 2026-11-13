"""File reading helpers for images, archives, Office documents and binaries."""
from __future__ import annotations

import os

from apowerb.core.agent_helpers.text_utils import _truncate_content


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".webm", ".flac", ".aac", ".wma", ".opus"}
_ARCHIVE_EXTENSIONS = {".zip"}
_MAX_IMAGE_DIMENSION = 1024  # longest side of an image sent to the model
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # larger files are not decoded at all
_MAX_PASSTHROUGH_BYTES = 2 * 1024 * 1024  # above this a small image is re-encoded


def _is_binary_by_magic(file_path: str) -> bool:
    """Check if a file is binary by looking at the first bytes for null characters."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


def _read_image(file_path: str, file_size: int, filename: str) -> dict:
    """Attach the image to the tool result (see ``apowerb.core.tool_images``).

    Images larger than ``_MAX_IMAGE_DIMENSION`` on their longest side are
    downscaled first: the providers bill images by their dimensions.
    """
    from apowerb.core.tool_images import attach_image

    content_type = "image/" + os.path.splitext(filename)[1].lstrip(".").lower().replace(
        "jpg", "jpeg"
    )
    if file_size > _MAX_IMAGE_BYTES:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "format": "image",
            "error_message": f"The image is larger than {_MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
        }
    with open(file_path, "rb") as f:
        data = f.read()
    try:
        data, content_type = _downscale_image(data, content_type)
    except Exception:  # noqa: BLE001 -- a provider would reject it as well
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "format": "image",
            "error_message": "The file could not be read as an image.",
        }
    return {
        "status": "success",
        "filename": filename,
        "size": file_size,
        "format": "image",
        "image_ref": attach_image(data, content_type),
        "message": "The image is attached to this result.",
    }


def _downscale_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if (
        max(img.width, img.height) <= _MAX_IMAGE_DIMENSION
        and content_type in ("image/png", "image/jpeg", "image/webp", "image/gif")
        and len(data) <= _MAX_PASSTHROUGH_BYTES
        and not getattr(img, "is_animated", False)  # providers reject animations
    ):
        return data, content_type
    img.thumbnail((_MAX_IMAGE_DIMENSION, _MAX_IMAGE_DIMENSION))
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _read_archive(file_path: str, file_size: int, filename: str) -> dict:
    """List contents of a ZIP archive."""
    import zipfile

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            entries = []
            for info in zf.infolist():
                entries.append(f"  {info.filename}  ({info.file_size} bytes)")
            listing = "\n".join(entries[:200])  # cap at 200 entries
            total = len(zf.infolist())
            if total > 200:
                listing += f"\n  ... and {total - 200} more files"
        return {
            "status": "success",
            "filename": filename,
            "size": file_size,
            "format": "zip_listing",
            "content": f"ZIP archive — {total} file(s):\n{listing}",
            "total_files": total,
        }
    except Exception as e:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": f"Could not read ZIP archive: {e}",
        }


def _read_excel(file_path: str, file_size: int, filename: str) -> dict:
    """Extract content from .xlsx / .xls files using openpyxl or xlrd."""
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            sheets_content = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    # Skip completely empty rows
                    if any(cell is not None for cell in row):
                        rows.append("\t".join("" if v is None else str(v) for v in row))
                if rows:
                    sheets_content.append(f"### Sheet: {sheet_name}\n" + "\n".join(rows))
            wb.close()
            content = "\n\n".join(sheets_content) or "(No data found in workbook)"
        else:
            # .xls — use xlrd
            import xlrd
            wb = xlrd.open_workbook(file_path)
            sheets_content = []
            for sheet in wb.sheets():
                rows = []
                for r in range(sheet.nrows):
                    row_vals = [str(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                    rows.append("\t".join(row_vals))
                if rows:
                    sheets_content.append(f"### Sheet: {sheet.name}\n" + "\n".join(rows))
            content = "\n\n".join(sheets_content) or "(No data found in workbook)"

        content, truncated = _truncate_content(content)
        return {
            "status": "success",
            "filename": filename,
            "size": file_size,
            "format": "excel",
            "content": content,
            "truncated": truncated,
        }
    except ImportError as e:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": f"Missing library to read Excel files: {e}. Install openpyxl (xlsx) or xlrd (xls).",
        }
    except Exception as e:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": f"Could not read Excel file: {e}",
        }


def _read_docx(file_path: str, file_size: int, filename: str) -> dict:
    """Extract text from a .docx Word document."""
    try:
        from docx import Document
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # Also extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = "\t".join(cell.text.strip() for cell in row.cells)
                if row_text.strip():
                    paragraphs.append(row_text)
        content = "\n\n".join(paragraphs) or "(No text content found)"
        content, truncated = _truncate_content(content)
        return {
            "status": "success",
            "filename": filename,
            "size": file_size,
            "format": "docx",
            "content": content,
            "truncated": truncated,
        }
    except ImportError:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": "Missing library to read Word files. Install python-docx.",
        }
    except Exception as e:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": f"Could not read Word file: {e}",
        }


def _read_pptx(file_path: str, file_size: int, filename: str) -> dict:
    """Extract text from a .pptx PowerPoint presentation."""
    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        slides_content = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    texts.append(shape.text.strip())
            if texts:
                slides_content.append(f"### Slide {i}\n" + "\n".join(texts))
        content = "\n\n".join(slides_content) or "(No text content found)"
        content, truncated = _truncate_content(content)
        return {
            "status": "success",
            "filename": filename,
            "size": file_size,
            "format": "pptx",
            "content": content,
            "truncated": truncated,
        }
    except ImportError:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": "Missing library to read PowerPoint files. Install python-pptx.",
        }
    except Exception as e:
        return {
            "status": "error",
            "filename": filename,
            "size": file_size,
            "message": f"Could not read PowerPoint file: {e}",
        }


def _read_binary_metadata(file_path: str, file_size: int, filename: str) -> dict:
    """Return basic metadata for unknown binary files."""
    ext = os.path.splitext(filename)[1].lower()
    return {
        "status": "success",
        "filename": filename,
        "size": file_size,
        "format": "binary_metadata",
        "message": (
            f"Binary file ({ext or 'unknown type'}, {file_size} bytes). "
            "Cannot extract text content. The file is stored and available on disk."
        ),
    }
