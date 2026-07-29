import os
from typing import Dict, Any, Optional
import requests
from pathlib import Path

# th2agent modules
from th2agent.configs.paths import agent_upload_dir
from th2agent.helpers.data_lake import StorageBoardFactory


def tool_get_bearer_token(auth_url: str, credentials: dict) -> str:
    """Envoie une requête POST avec X-Forwarded headers pour obtenir un token bearer"""
    headers = {"Content-Type": "application/json"}

    response = requests.post(auth_url, json=credentials, headers=headers)
    return response.json().get("access_token")


def tool_advanced(city: str) -> dict:
    """Retrieves the current weather report for a specified city.

    Args:
        city (str): The name of the city for which to retrieve the weather report.

    Returns:
        dict: status and result or error msg.
    """
    if city.lower() == "new york":
        return {
            "status": "success",
            "report": (
                "The weather in New York is sunny with a temperature of 25 degrees"
                " Celsius (77 degrees Fahrenheit)."
            ),
        }
    else:
        return {
            "status": "error",
            "error_message": f"Weather information for '{city}' is not available.",
        }


def tool_thaink2_forecast(
    api_url: str = "https://clever.thaink2.fr/app_direct/th2apis/private/",
    fcast_horizon=30,
    group_target=None,
    date_var="date",
    models_list=["xgboost"],
    actuals: Any = None,
    target_var: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call Thaink² Forecast API.

    Parameters
    ----------
    api_url : str
        Forecast API endpoint
    payload : dict
        Forecast request payload
    token : str
        Authentication token (Bearer)

    Returns
    -------
    Dict[str, Any]
        API response JSON

    Raises
    ------
    RuntimeError
        If the API call fails
    """
    token = os.getenv("THAINK2_API_FORECAST_TOKEN")
    endpoint = "thaink2/forecasting"
    url = f"{api_url}{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "actuals": actuals.to_json(orient="records", date_format="iso"),
        "fcast_horizon": fcast_horizon,
        "group_target": group_target,
        "target_var": target_var,
        "date_var": date_var,
        "models_list": models_list,
    }

    # Make the API call
    response = requests.post(url, json=payload, headers=headers)

    # Handle response
    if response.status_code == 200:
        return response.json()  # Return the parsed JSON response
    else:
        response.raise_for_status()  # Raise an exception for HTTP errors


def tool_read_file(file_path: str, max_size_mb: int = 10) -> dict:
    """
    Reads the content of a file and returns it with metadata.

    Args:
        file_path (str): Path to the file to read
        max_size_mb (int): Maximum file size in MB to read (default: 10MB)

    Returns:
        dict: Contains status, content (or error_message), file_name, file_size,
              encoding, and line_count on success
    """
    from pathlib import Path

    try:
        file_path_obj = Path(file_path)

        # Check if file exists
        if not file_path_obj.exists():
            return {"status": "error", "error_message": f"File not found: {file_path}"}

        # Check if it's a file (not a directory)
        if not file_path_obj.is_file():
            return {
                "status": "error",
                "error_message": f"Path is not a file: {file_path}",
            }

        # Get file size and check limits
        file_size = file_path_obj.stat().st_size
        max_size_bytes = max_size_mb * 1024 * 1024

        if file_size > max_size_bytes:
            return {
                "status": "error",
                "error_message": f"File too large: {file_size / 1024 / 1024:.2f}MB exceeds limit of {max_size_mb}MB",
            }

        # Try reading with different encodings
        encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
        content = None
        detected_encoding = None

        for encoding in encodings:
            try:
                with open(file_path, "r", encoding=encoding) as file:
                    content = file.read()
                    detected_encoding = encoding
                    break
            except (UnicodeDecodeError, LookupError):
                continue

        if content is None:
            return {
                "status": "error",
                "error_message": f"Could not decode file with supported encodings: {encodings}",
            }

        # Calculate metadata
        line_count = content.count("\n") + (
            1 if content and not content.endswith("\n") else 0
        )

        return {
            "status": "success",
            "content": content,
            "file_name": file_path_obj.name,
            "file_size_bytes": file_size,
            "file_size_kb": round(file_size / 1024, 2),
            "encoding": detected_encoding,
            "line_count": line_count,
        }

    except PermissionError:
        return {
            "status": "error",
            "error_message": f"Permission denied: Cannot read file {file_path}",
        }
    except Exception as e:
        return {"status": "error", "error_message": f"Failed to read file: {str(e)}"}


def tool_read_uploaded_file(filename: str) -> dict:
    """Read a file that was uploaded by the user for this agent.
    Use this tool when the user mentions a file they uploaded or asks about uploaded content.

    Args:
        filename: The name of the uploaded file to read.

    Returns:
        dict: Contains status, content (or error_message), filename, size, and encoding.
    """
    # This is a placeholder — at runtime, to_agent() replaces it with a
    # closure that knows the correct agent folder. If called directly,
    # it returns an error indicating proper setup is needed.
    return {
        "status": "error",
        "message": "Tool not properly initialised (missing agent context)",
    }


def tool_pdf_to_images(
    filename: str,
    max_pages: int = 2,
    dpi: int = 84,
    max_dimension: int = 768,
) -> dict:
    """Render each page of a PDF to a base64 PNG for vision-LLM analysis.

    Use this when the LLM needs to "see" a document — typical for
    scanned PDFs, supplier acknowledgements with complex tables/layouts,
    or any document where text extraction returned little or no content.
    Works on both text PDFs and scanned PDFs.

    Defaults are tuned for cost: ``max_pages=5`` covers the large
    majority of single-AR documents, ``dpi=120`` is enough for
    OCR-quality vision, ``max_dimension=1024`` keeps each page below
    ~150k input tokens on Gemini Flash. Override only when a specific
    document genuinely needs higher fidelity.

    Args:
        filename: Name of the PDF in the agent's uploads dir (no path).
        max_pages: Hard cap on rendered pages (default 5) — protects context.
        dpi: Render DPI (default 120).
        max_dimension: Resize so longest side ≤ this many pixels (default 1024).

    Returns:
        dict with status, page_count, total_pages_in_pdf, and pages: a list
        of {"page_number", "mime_type": "image/png", "data": <base64>}.
    """
    # Placeholder — at runtime, to_agent() replaces it with a closure that
    # knows the correct agent folder. If called directly, returns an error.
    return {
        "status": "error",
        "message": "Tool not properly initialised (missing agent context)",
    }


def tool_pdf_first_page(filename: str) -> dict:
    """Extract the TEXT of the first page of an uploaded PDF (no image
    conversion). Use for AR intake — the first page holds the order
    number, supplier and lines.

    Args:
        filename: Name of the PDF in the agent's uploads dir (no path).

    Returns:
        dict with status, total_pages_in_pdf, text, char_count,
        has_text_layer.
    """
    # Placeholder — at runtime, to_agent() replaces it with a closure bound
    # to the correct agent folder (see bind_pdf_first_page).
    return {
        "status": "error",
        "message": "Tool not properly initialised (missing agent context)",
    }


def tool_get_webhook_backlog_status() -> dict:
    """Return the webhook-backlog state scoped to this agent.

    Use this when you've been invoked by a webhook (Outlook, Gmail, …)
    so you can mention how many notifications are still queued behind
    the one you just handled. Helps the operator decide whether to wait
    for more output or start triaging manually.

    The result includes the row currently being processed (``current``),
    a count of pending and retrying rows, the next 10 in FIFO order,
    and the per-day success / failure counts.

    Returns:
        dict with ``status``, ``agent_id``, ``current``, ``pending_count``,
        ``retrying_count``, ``pending`` (list), ``completed_today``,
        ``failed_today``.
    """
    # Placeholder — to_agent() replaces it with a closure bound to the
    # current agent_id so the agent can never read another agent's queue.
    return {
        "status": "error",
        "message": "Tool not properly initialised (missing agent context)",
    }


def tool_convert_image_to_base64(image_path: str, max_dimension: int = 1600) -> dict:
    """
    Convert an image file to base64 encoding for LLM processing.
    Large images are automatically resized to fit within max_dimension pixels
    while preserving aspect ratio, ensuring efficient LLM token usage.

    Args:
        image_path (str): Path to the image file (supports PNG, JPG, JPEG, GIF, WEBP, BMP, TIFF)
        max_dimension (int): Maximum width or height in pixels (default: 1600). Images larger
                             than this are resized proportionally.

    Returns:
        dict: Contains status, base64_data (data URI), image_format, original_size, final_size
              or error_message if conversion fails.
    """
    import base64
    import io
    from pathlib import Path

    try:
        # Verify file exists
        file_path = Path(image_path)
        if not file_path.exists():
            return {"status": "error", "error_message": f"File not found: {image_path}"}

        # Validate image format
        valid_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
        file_ext = file_path.suffix.lower()

        if file_ext not in valid_formats:
            return {
                "status": "error",
                "error_message": f"Unsupported image format: {file_ext}. Supported: {', '.join(valid_formats)}",
            }

        original_size = file_path.stat().st_size
        resized = False

        try:
            from PIL import Image as PILImage

            img = PILImage.open(file_path)
            original_dimensions = f"{img.width}x{img.height}"

            # Resize if any dimension exceeds max_dimension
            if img.width > max_dimension or img.height > max_dimension:
                ratio = min(max_dimension / img.width, max_dimension / img.height)
                new_w = int(img.width * ratio)
                new_h = int(img.height * ratio)
                img = img.resize((new_w, new_h), PILImage.LANCZOS)
                resized = True

            # Convert to RGB if RGBA (for JPEG output)
            if img.mode == "RGBA" and file_ext in (".jpg", ".jpeg"):
                img = img.convert("RGB")

            # Save to buffer as JPEG for efficiency (unless PNG transparency needed)
            buffer = io.BytesIO()
            if img.mode == "RGBA":
                img.save(buffer, format="PNG", optimize=True)
                mime_type = "image/png"
                out_format = "PNG"
            else:
                img.save(buffer, format="JPEG", quality=85, optimize=True)
                mime_type = "image/jpeg"
                out_format = "JPEG"

            image_data = buffer.getvalue()
            final_dimensions = f"{img.width}x{img.height}"

        except ImportError:
            # Pillow not available — read raw file without resize
            mime_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
                ".tiff": "image/tiff",
            }
            mime_type = mime_types[file_ext]
            out_format = file_ext[1:].upper()
            with open(file_path, "rb") as f:
                image_data = f.read()
            original_dimensions = "unknown (Pillow not installed)"
            final_dimensions = original_dimensions

        base64_data = base64.b64encode(image_data).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{base64_data}"

        return {
            "status": "success",
            "base64_data": base64_data,
            "data_uri": data_uri,
            "image_format": out_format,
            "file_name": file_path.name,
            "original_size_kb": round(original_size / 1024, 1),
            "final_size_kb": round(len(image_data) / 1024, 1),
            "original_dimensions": original_dimensions,
            "final_dimensions": final_dimensions,
            "was_resized": resized,
        }

    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Image conversion failed: {str(e)}",
        }


def tool_available_data() -> dict:
    """
    List available data tables in the specified S3 bucket and prefix.

    Args:
        bucket_name (str): Name of the S3 bucket
        prefix (str): Prefix/path in the S3 bucket to look for tables
    Returns:
        dict: Success status with list of tables or error message
    """

    agent_owner: str = os.getenv("AGENT_OWNER")  # type: ignore
    organization_id = agent_owner.split("@")[1].split(".")[0]
    bucket_name: str = f"th2{organization_id}"
    prefix: str = "thaink2_data_pool"
    if organization_id == "thaink2":
        bucket_name = "thaink2"

    try:
        # Create a storage board factory instance
        board_factory = StorageBoardFactory(bucket_name=bucket_name, prefix=prefix)

        # Get the S3 board
        board = board_factory.get_board(storage_source="s3")

        # List all pins available in the board
        available_pins = board.pin_list()

        return {
            "success": True,
            "bucket": bucket_name,
            "prefix": prefix,
            "available_tables": available_pins,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Failed to list tables from specified bucket and prefix: {bucket_name}/{prefix}",
        }


def tool_data_loader(data_table: str) -> dict:
    """
    Tool to retrieve data from Belgatrans S3 storage.

    Args:
        limit (int): Number of items to retrieve (default: 100)
    Returns:
        dict: Success status with data or error message
    """

    agent_owner: str = os.getenv("AGENT_OWNER")

    organization_id = agent_owner.split("@")[1].split(".")[0]
    bucket_name: str = f"th2{organization_id}"
    if organization_id == "thaink2":
        bucket_name = "thaink2"
    prefix: str = "thaink2_data_pool"
    try:
        # Create a storage board factory instance
        board_factory = StorageBoardFactory(bucket_name=bucket_name, prefix=prefix)

        # Get the S3 board
        board = board_factory.get_board(storage_source="s3")
        if board.pin_exists(data_table):
            raw_data = board.pin_read(name=data_table)
        else:
            return {
                "success": False,
                "message": f"Data table '{data_table}' does not exist in S3 storage.",
            }

        agent_id = os.getenv("ROOT_AGENT_ID", "")
        agent_owner: str = os.getenv("AGENT_OWNER", "unknown_user")

        # create subfolder for the agent if it doesn't exist
        agent_folder = str(agent_upload_dir(agent_id))
        if not os.path.exists(agent_folder):
            os.makedirs(agent_folder)
        raw_data_file = f"{agent_folder}/{data_table}.parquet"
        raw_data.to_parquet(raw_data_file)
        # Convert to absolute path
        raw_data_file = str(Path(raw_data_file).resolve())
        # Filter pins related to Belgatrans and limit the results
        return {
            "success": True,
            "bucket": bucket_name,
            "raw_data_file": raw_data_file,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to retrieve data from Belgatrans S3 storage.",
        }
