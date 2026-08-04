import json
from google.genai import types


async def tool_save_code_artifact(
    tool_context,
    filename: str,
    code: str,
    language: str,
) -> dict:
    """Save a code artifact that can be executed later.

    Args:
        tool_context: ADK tool context (injected automatically).
        filename: The filename for the artifact (e.g., "main.py", "script.sh").
        code: The source code content.
        language: The programming language ("python", "javascript", "bash", etc.).

    Returns:
        dict with status and artifact metadata.
    """
    # Build artifact metadata as a JSON string, wrapped in a types.Part
    artifact_data = json.dumps({
        "filename": filename,
        "language": language,
        "code": code,
    })
    artifact_part = types.Part.from_text(text=artifact_data)

    # Save via ADK artifact service (async)
    version = await tool_context.save_artifact(filename=filename, artifact=artifact_part)

    return {
        "status": "success",
        "filename": filename,
        "language": language,
        "version": version,
        "message": f"Artifact '{filename}' saved successfully (version {version})."
    }
