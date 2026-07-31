"""TÂCHE 3 — TDD: l intake expose le texte PDF via {intake_pdf_text?}.

Le placeholder DOIT porter le ``?`` (sinon prompt_safety refuse le boot quand
la cle est absente). Le bloc doit dire au LLM d utiliser le texte fourni et de
ne PAS appeler tool_pdf_first_page / tool_download_attachment quand present.
On ne retire AUCUN outil. On ne touche pas au Step 1 FournisseursExclus.
"""
from __future__ import annotations


def test_intake_prompt_exposes_optional_placeholder():
    from th2customers.scei.templates.scei_v2 import _INTAKE_PROMPT
    assert "{intake_pdf_text?}" in _INTAKE_PROMPT
    # le ? est obligatoire : la forme sans ? ne doit pas apparaitre seule
    assert "{intake_pdf_text}" not in _INTAKE_PROMPT.replace("{intake_pdf_text?}", "")


def test_intake_prompt_instructs_to_use_provided_text():
    from th2customers.scei.templates.scei_v2 import _INTAKE_PROMPT
    low = _INTAKE_PROMPT.lower()
    assert "tool_pdf_first_page" in _INTAKE_PROMPT
    # le bloc doit mentionner d utiliser directement le texte fourni
    assert "fourni" in low


def test_intake_tools_unchanged():
    from th2customers.scei.templates.scei_v2 import SCEI_AR_INTAKE
    assert SCEI_AR_INTAKE["agent_tools"] == [
        "outlook_mail.tool_read_email",
        "outlook_mail.tool_download_attachment",
        "basic.tool_pdf_first_page",
    ]


def test_intake_placeholder_is_optional_not_a_boot_error():
    """find_unsafe_braces doit classer {intake_pdf_text?} en warning (optionnel),
    PAS en error (qui crasherait le boot quand la cle est absente)."""
    from th2customers.scei.templates.scei_v2 import _INTAKE_PROMPT
    from apowerb.core.validation.prompt_safety import find_unsafe_braces
    issues = find_unsafe_braces(_INTAKE_PROMPT)
    errors = [i for i in issues if i.level == "error" and "intake_pdf_text" in i.placeholder]
    assert errors == []
