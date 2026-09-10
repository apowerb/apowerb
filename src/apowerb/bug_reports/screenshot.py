"""Décodage et validation de la capture d'écran jointe.

Une capture arrive du navigateur en data URL. Trois choses doivent être
vraies avant qu'elle touche le stockage, et aucune n'est acquise parce
que c'est « notre » front qui l'envoie — l'endpoint est ouvert à tout
utilisateur connecté, donc à quiconque sait écrire une requête :

1. **C'est bien une image**, vérifiée sur les octets et non sur le type
   déclaré dans la data URL, qui est du texte fourni par l'appelant ;
2. **Elle tient dans la limite**, mesurée après décodage — le base64
   pèse un tiers de moins que ce qu'il transporte ;
3. **Le type est de la liste blanche.** Un SVG est une image et exécute
   du script ; il n'y est pas.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import NamedTuple

# 3 Mo décodés : un PNG plein écran en Retina pèse ~1,5 Mo.
DEFAULT_MAX_BYTES = 3 * 1024 * 1024

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?;base64,(?P<payload>.+)$", re.S)

# Signature réelle des formats acceptés. Le SVG est absent volontairement.
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"RIFF", "image/webp", "webp"),  # affiné plus bas sur l'octet 8
)


class Screenshot(NamedTuple):
    content: bytes
    content_type: str
    extension: str


class ScreenshotRejected(ValueError):
    """Capture refusée : trop grosse, mal formée, ou pas une image acceptée."""


def decode_screenshot(
    data_url: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> Screenshot:
    """Décode une data URL en octets validés, ou lève ``ScreenshotRejected``."""
    if not data_url or not isinstance(data_url, str):
        raise ScreenshotRejected("Capture vide.")

    match = _DATA_URL.match(data_url.strip())
    if not match:
        raise ScreenshotRejected(
            "Capture attendue sous la forme « data:image/png;base64,… »."
        )

    payload = match.group("payload")
    # Garde-fou avant décodage : 4 caractères de base64 pour 3 octets.
    if len(payload) > (max_bytes // 3 + 1) * 4 + 16:
        raise ScreenshotRejected(
            f"Capture trop volumineuse (limite {max_bytes // (1024 * 1024)} Mo)."
        )

    try:
        content = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScreenshotRejected(f"Base64 invalide : {exc}") from exc

    if len(content) > max_bytes:
        raise ScreenshotRejected(
            f"Capture trop volumineuse ({len(content) // 1024} Ko, "
            f"limite {max_bytes // (1024 * 1024)} Mo)."
        )
    if not content:
        raise ScreenshotRejected("Capture vide après décodage.")

    for signature, mime, extension in _MAGIC:
        if content.startswith(signature):
            if signature == b"RIFF":
                # RIFF sert aussi au WAV et à l'AVI ; « WEBP » est en 8..12.
                if content[8:12] != b"WEBP":
                    continue
            return Screenshot(content=content, content_type=mime, extension=extension)

    raise ScreenshotRejected(
        "Format non accepté. Les octets reçus ne sont ni du PNG, ni du JPEG, "
        "ni du WebP (le type annoncé dans la data URL n'est pas pris pour "
        "argent comptant)."
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "Screenshot",
    "ScreenshotRejected",
    "decode_screenshot",
]
