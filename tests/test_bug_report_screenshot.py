"""La capture d'écran est une charge utile hostile jusqu'à preuve du contraire.

L'endpoint est ouvert à tout utilisateur connecté : ce n'est pas « notre
front » qui envoie, c'est quiconque sait écrire une requête. Le type
annoncé dans la data URL est du texte fourni par l'appelant, donc les
octets seuls font foi.
"""

import base64

import pytest

from apowerb.bug_reports.screenshot import (
    ScreenshotRejected,
    decode_screenshot,
)


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 64


@pytest.mark.parametrize(
    "payload,attendu",
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_les_formats_acceptes_passent(payload, attendu):
    assert decode_screenshot(_data_url(payload)).content_type == attendu


def test_un_svg_annonce_en_png_est_refuse():
    """Un SVG est une image ET exécute du script. Il n'est pas dans la liste.

    Le type annoncé dit `image/png` ; seuls les octets disent la vérité.
    """
    svg = b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'></svg>"
    with pytest.raises(ScreenshotRejected):
        decode_screenshot(_data_url(svg, "image/png"))


def test_un_riff_qui_nest_pas_du_webp_est_refuse():
    """RIFF sert aussi au WAV et à l'AVI : la signature seule ne suffit pas."""
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 64
    with pytest.raises(ScreenshotRejected):
        decode_screenshot(_data_url(wav))


def test_une_capture_trop_grosse_est_refusee():
    trop = PNG + b"\x00" * 5000
    with pytest.raises(ScreenshotRejected) as exc:
        decode_screenshot(_data_url(trop), max_bytes=1024)
    assert "volumineuse" in str(exc.value)


def test_le_garde_de_taille_agit_avant_le_decodage():
    """Décoder d'abord ferait allouer la charge qu'on voulait refuser."""
    enorme = "A" * 10_000_000
    with pytest.raises(ScreenshotRejected):
        decode_screenshot(f"data:image/png;base64,{enorme}", max_bytes=1024)


@pytest.mark.parametrize(
    "entree",
    ["", None, "http://ailleurs/image.png", "data:image/png,pas-de-base64", "data:image/png;base64,%%%"],
)
def test_les_entrees_malformees_sont_refusees(entree):
    with pytest.raises(ScreenshotRejected):
        decode_screenshot(entree)


def test_lextension_suit_le_type_reel():
    assert decode_screenshot(_data_url(JPEG, "image/png")).extension == "jpg"
