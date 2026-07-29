"""Image generation tools — multi-provider support.

Providers (in priority order):
1. Gemini Imagen (google) — uses GEMINI_API_KEY
2. OpenAI DALL-E — uses OPENAI_API_KEY
3. Stability AI — uses STABILITY_API_KEY

The tool saves generated images to the agent's uploads folder and returns
metadata + base64 so the vision callback can inject the image into the
LLM context.
"""

import base64
import io
import os
import re
import time
from logging import getLogger
from pathlib import Path

from th2agent.configs.paths import agent_upload_dir

logger = getLogger(__name__)

# API keys used by this module — declared at module level so the ToolsStore
# parameter scanner (regex on os.getenv) can discover them for the UI.
_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
_STABILITY_KEY = os.getenv("STABILITY_API_KEY", "")


def _sanitize_filename(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a safe filename slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower().strip())
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:max_len] or "generated"


# ---------------------------------------------------------------------------
# Provider: Gemini Imagen
# ---------------------------------------------------------------------------

def _generate_gemini(prompt: str, aspect_ratio: str, style: str | None) -> tuple[bytes, str]:
    """Generate image via Gemini Imagen (imagen-3.0-generate-002).

    Returns (image_bytes, mime_type).
    """
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    full_prompt = f"{style + ': ' if style else ''}{prompt}"

    response = client.models.generate_images(
        model="imagen-3.0-generate-002",
        prompt=full_prompt,
        config=genai.types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
        ),
    )

    if not response.generated_images:
        raise RuntimeError("Gemini Imagen returned no images")

    img = response.generated_images[0]
    image_bytes = img.image.image_bytes
    mime_type = getattr(img.image, "mime_type", "image/png") or "image/png"
    return image_bytes, mime_type


# ---------------------------------------------------------------------------
# Provider: OpenAI DALL-E
# ---------------------------------------------------------------------------

_DALLE_SIZE_MAP = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1024x1024",
    "3:4": "1024x1024",
}


def _generate_openai(prompt: str, aspect_ratio: str, style: str | None) -> tuple[bytes, str]:
    """Generate image via OpenAI DALL-E 3.

    Returns (image_bytes, mime_type).
    """
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set")

    size = _DALLE_SIZE_MAP.get(aspect_ratio, "1024x1024")
    full_prompt = f"{style + ': ' if style else ''}{prompt}"

    resp = httpx.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "dall-e-3",
            "prompt": full_prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64), "image/png"


# ---------------------------------------------------------------------------
# Provider: Stability AI
# ---------------------------------------------------------------------------

_STABILITY_AR_MAP = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 896),
    "3:4": (896, 1152),
}


def _generate_stability(prompt: str, aspect_ratio: str, style: str | None) -> tuple[bytes, str]:
    """Generate image via Stability AI (SDXL 1.0).

    Returns (image_bytes, mime_type).
    """
    import httpx

    api_key = os.environ.get("STABILITY_API_KEY")
    if not api_key:
        raise EnvironmentError("STABILITY_API_KEY not set")

    width, height = _STABILITY_AR_MAP.get(aspect_ratio, (1024, 1024))
    full_prompt = f"{style + ': ' if style else ''}{prompt}"

    resp = httpx.post(
        "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "text_prompts": [{"text": full_prompt, "weight": 1}],
            "cfg_scale": 7,
            "width": width,
            "height": height,
            "steps": 30,
            "samples": 1,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    b64 = data["artifacts"][0]["base64"]
    return base64.b64decode(b64), "image/png"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "gemini": ("Gemini Imagen", _generate_gemini, "GEMINI_API_KEY"),
    "openai": ("OpenAI DALL-E 3", _generate_openai, "OPENAI_API_KEY"),
    "stability": ("Stability AI SDXL", _generate_stability, "STABILITY_API_KEY"),
}

# Priority order — Gemini first as requested
_PROVIDER_ORDER = ["gemini", "openai", "stability"]


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

def tool_generate_image(
    prompt: str,
    provider: str = "auto",
    aspect_ratio: str = "1:1",
    style: str = "",
) -> dict:
    """Generate an image from a text prompt.

    Creates an image using AI image generation and saves it to the agent's
    uploads folder for download.

    Args:
        prompt (str): Description of the image to generate. Be detailed and specific
                      for best results.
        provider (str): Which provider to use. Options: "auto" (tries available
                        providers in order), "gemini", "openai", "stability".
                        Default: "auto".
        aspect_ratio (str): Aspect ratio of the generated image.
                            Options: "1:1", "16:9", "9:16", "4:3", "3:4".
                            Default: "1:1".
        style (str): Optional style prefix (e.g. "photorealistic", "watercolor",
                     "pixel art", "oil painting", "3D render"). Leave empty for
                     the provider's default style.

    Returns:
        dict: Contains status, file_name, file_path, base64_data, image_format,
              provider_used, or error_message if generation fails.
    """
    # Validate aspect ratio
    valid_ratios = {"1:1", "16:9", "9:16", "4:3", "3:4"}
    if aspect_ratio not in valid_ratios:
        return {
            "status": "error",
            "error_message": f"Invalid aspect_ratio '{aspect_ratio}'. Valid: {', '.join(sorted(valid_ratios))}",
        }

    # Determine provider order
    if provider == "auto":
        order = _PROVIDER_ORDER
    elif provider in _PROVIDERS:
        order = [provider]
    else:
        return {
            "status": "error",
            "error_message": f"Unknown provider '{provider}'. Valid: auto, {', '.join(_PROVIDERS.keys())}",
        }

    # Try each provider
    errors = []
    for prov_key in order:
        prov_name, gen_fn, env_key = _PROVIDERS[prov_key]

        if not os.environ.get(env_key):
            errors.append(f"{prov_name}: {env_key} not set")
            continue

        try:
            logger.info("[IMAGE_GEN] Generating with %s: %.80s…", prov_name, prompt)
            image_bytes, mime_type = gen_fn(prompt, aspect_ratio, style or None)

            # Determine file extension
            ext = ".png" if "png" in mime_type else ".jpeg"

            # Save to uploads folder
            agent_id = os.getenv("ROOT_AGENT_ID", "")
            folder = str(agent_upload_dir(agent_id))
            os.makedirs(folder, exist_ok=True)

            slug = _sanitize_filename(prompt)
            filename = f"gen_{slug}_{int(time.time())}{ext}"
            file_path = os.path.join(folder, filename)

            with open(file_path, "wb") as f:
                f.write(image_bytes)

            abs_path = str(Path(file_path).resolve())
            folder_name = f"agent{agent_id}"
            download_path = f"/api/files/{folder_name}/{filename}"
            b64 = base64.b64encode(image_bytes).decode("ascii")
            fmt = "PNG" if ext == ".png" else "JPEG"

            logger.info("[IMAGE_GEN] Saved %s (%d KB) via %s", abs_path, len(image_bytes) // 1024, prov_name)

            return {
                "status": "success",
                "file_name": filename,
                "file_path": abs_path,
                "download_path": download_path,
                "base64_data": b64,
                "image_format": fmt,
                "content_type": mime_type,
                "size_kb": round(len(image_bytes) / 1024, 1),
                "provider_used": prov_name,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "style": style or "default",
                "message": (
                    f"Image generated successfully with {prov_name}. "
                    f"Saved as {filename}. The user can download it."
                ),
            }

        except Exception as e:
            logger.warning("[IMAGE_GEN] %s failed: %s", prov_name, e)
            errors.append(f"{prov_name}: {e}")
            continue

    # All providers failed
    return {
        "status": "error",
        "error_message": (
            "Image generation failed with all providers.\n"
            + "\n".join(f"  - {err}" for err in errors)
            + "\n\nMake sure at least one API key is configured: "
            "GEMINI_API_KEY, OPENAI_API_KEY, or STABILITY_API_KEY."
        ),
    }
