import os
from io import BytesIO
from logging import getLogger
from typing import Optional

logger = getLogger(__name__)


def _get_s3_client():
    """Build a boto3 S3 client from environment variables."""
    try:
        import boto3
    except ImportError:
        raise ImportError("boto3 is required: pip install boto3")

    access_key = os.getenv("S3_ACCESS_KEY")
    secret_key = os.getenv("S3_SECRET_KEY")
    region = os.getenv("S3_REGION", "fr-par")
    endpoint_url = os.getenv("S3_ENDPOINT_URL", f"https://s3.{region}.scw.cloud")

    if not access_key or not secret_key:
        raise ValueError(
            "S3_ACCESS_KEY and S3_SECRET_KEY environment variables must be set."
        )

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        endpoint_url=endpoint_url,
    )


def _parse_s3_url(url: str) -> tuple[str, str]:
    """
    Parse an S3 URL and return (bucket, key).

    Handles both styles:
      - Path-style:          https://s3.fr-par.scw.cloud/my-bucket/path/to/file.pdf
      - Virtual-hosted-style: https://my-bucket.s3.fr-par.scw.cloud/path/to/file.pdf
      - AWS path-style:      https://s3.amazonaws.com/my-bucket/path/to/file.pdf
      - AWS virtual-hosted:  https://my-bucket.s3.amazonaws.com/path/to/file.pdf
    """
    import re

    url = url.strip()

    # Remove schema
    no_schema = re.sub(r"^https?://", "", url)
    host_path = no_schema.split("/", 1)
    host = host_path[0]
    path = host_path[1] if len(host_path) > 1 else ""

    # Virtual-hosted style: bucket is a subdomain of s3.*
    # e.g. my-bucket.s3.fr-par.scw.cloud  or  my-bucket.s3.amazonaws.com
    vhost_match = re.match(r"^([^.]+)\.s3[.\-]", host)
    if vhost_match:
        bucket = vhost_match.group(1)
        key = path
        return bucket, key

    # Path-style: first path segment is the bucket
    parts = path.split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""

    return bucket, key


def _extract_pdf_text(pdf_bytes: bytes, truncate_chars: int = 15_000) -> str:
    """Extract text from a PDF byte string via pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError("pypdf is required: pip install pypdf")

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"--- Page {i} ---\n{text.strip()}")
    full_text = "\n\n".join(pages)

    if len(full_text) > truncate_chars:
        full_text = full_text[:truncate_chars] + "\n\n[... Content truncated ...]"

    return full_text


def _list_all_objects(s3_client, bucket: str, file_type: str = "all") -> list[dict]:
    """
    List ALL objects in a bucket using pagination.
    Returns a list of {'Key': ..., 'Size': ...} dicts.
    Filters by extension when file_type is 'pdf' or 'txt'.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket)

    objects = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if file_type == "pdf" and not key.lower().endswith(".pdf"):
                continue
            if file_type == "txt" and not key.lower().endswith(".txt"):
                continue
            objects.append({"Key": key, "Size": obj.get("Size", 0)})

    return objects


def _search_in_text(text: str, query: str, context_chars: int = 200) -> list[dict]:
    """Find up to 3 keyword matches in text, returning surrounding context."""
    matches = []
    query_lower = query.lower()
    text_lower = text.lower()
    start = 0

    while len(matches) < 3:
        pos = text_lower.find(query_lower, start)
        if pos == -1:
            break
        ctx_start = max(0, pos - context_chars)
        ctx_end = min(len(text), pos + len(query) + context_chars)
        ctx = text[ctx_start:ctx_end]
        if ctx_start > 0:
            ctx = "..." + ctx
        if ctx_end < len(text):
            ctx = ctx + "..."
        matches.append({"position": pos, "context": ctx.strip()})
        start = pos + 1

    return matches


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------

def tool_read_s3_pdf(s3_url: str) -> dict:
    """
    Reads and extracts text content from a PDF file stored in S3 / Scaleway
    object storage.

    Returns:
        dict with extracted text content or an error message.
    """
    try:
        s3 = _get_s3_client()
    except (ImportError, ValueError) as e:
        return {"success": False, "error": str(e)}

    try:
        bucket, key = _parse_s3_url(s3_url)
        if not bucket or not key:
            return {
                "success": False,
                "error": f"Could not parse bucket/key from URL: {s3_url}",
            }

        logger.info(f"[S3_PDF] Reading s3://{bucket}/{key}")
        response = s3.get_object(Bucket=bucket, Key=key)
        pdf_bytes = response["Body"].read()

    except Exception as e:
        return {"success": False, "error": f"S3 download failed: {e}"}

    try:
        text = _extract_pdf_text(pdf_bytes)
    except ImportError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"PDF extraction failed: {e}"}

    if not text.strip():
        return {
            "success": False,
            "error": "PDF contains no extractable text (may be scanned/image-based).",
        }

    return {
        "success": True,
        "bucket": bucket,
        "key": key,
        "char_count": len(text),
        "content": text,
    }


def tool_search_s3_files(
    query: str,
    bucket_name: str = "",
    file_type: str = "pdf",
    max_results: int = 5,
) -> dict:
    """
    Searches for specific content across PDF and text files stored in S3 /
    Scaleway object storage. Returns relevant excerpts and file locations.

    Returns:
        dict with matching files, match counts, and text excerpts.
    """
    bucket = bucket_name or os.getenv("S3_BUCKET_NAME", "")
    if not bucket:
        return {
            "success": False,
            "error": (
                "No bucket specified. Pass bucket_name or set the "
                "S3_BUCKET_NAME environment variable."
            ),
        }

    if not query.strip():
        return {"success": False, "error": "query cannot be empty"}

    try:
        s3 = _get_s3_client()
    except (ImportError, ValueError) as e:
        return {"success": False, "error": str(e)}

    # List ALL objects (paginated — no 1000 item cap)
    try:
        objects = _list_all_objects(s3, bucket, file_type)
    except Exception as e:
        return {"success": False, "error": f"Failed to list bucket contents: {e}"}

    if not objects:
        return {
            "success": True,
            "result_count": 0,
            "files_scanned": 0,
            "message": f"No {file_type} files found in bucket '{bucket}'.",
            "results": [],
        }

    region = os.getenv("S3_REGION", "fr-par")
    endpoint_base = os.getenv(
        "S3_ENDPOINT_URL", f"https://s3.{region}.scw.cloud"
    ).rstrip("/")

    results = []
    files_scanned = 0
    errors = 0

    for obj in objects:
        key = obj["Key"]
        try:
            file_obj = s3.get_object(Bucket=bucket, Key=key)
            content_bytes = file_obj["Body"].read()

            if key.lower().endswith(".pdf"):
                text = _extract_pdf_text(content_bytes, truncate_chars=100_000)
            else:
                text = content_bytes.decode("utf-8", errors="replace")

            files_scanned += 1
            matches = _search_in_text(text, query)

            if matches:
                results.append(
                    {
                        "file": key,
                        "url": f"{endpoint_base}/{bucket}/{key}",
                        "match_count": len(matches),
                        "excerpts": matches,
                    }
                )

            if len(results) >= max_results:
                logger.info(
                    f"[S3_SEARCH] Reached max_results={max_results}, stopping early."
                )
                break

        except Exception as e:
            logger.warning(f"[S3_SEARCH] Skipping {key}: {e}")
            errors += 1
            continue

    if not results:
        result_text = (
            f"Searched {files_scanned} file(s) in '{bucket}' but found no matches "
            f"for '{query}'."
        )
        if errors:
            result_text += f" ({errors} file(s) could not be read.)"
    else:
        lines = [
            f"Found '{query}' in {len(results)} file(s) "
            f"(scanned {files_scanned} of {len(objects)} total):\n"
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['file']} — {r['match_count']} match(es)")
            lines.append(f"   URL: {r['url']}")
            for j, exc in enumerate(r["excerpts"][:2], 1):
                lines.append(f"   Excerpt {j}: {exc['context']}")
            lines.append("")
        result_text = "\n".join(lines)

    logger.info(
        f"[S3_SEARCH] query={query!r} bucket={bucket!r} "
        f"→ {len(results)} results from {files_scanned} files scanned"
    )

    return {
        "success": True,
        "query": query,
        "bucket": bucket,
        "files_scanned": files_scanned,
        "result_count": len(results),
        "results": results,
        "summary": result_text,
    }