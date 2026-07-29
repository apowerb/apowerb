"""Le gate fournisseurs-exclus doit ignorer les transitaires internes @scei88.fr
(sinon le token court 'SCEI' matche le domaine et bloque un AR transféré)."""


def test_excluded_gate_skips_db_for_scei88(monkeypatch):
    from th2customers.scei import gates as m

    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return ("t", None)

    monkeypatch.setattr(m, "load_tool_config_params", _spy)
    cb = m.build_excluded_supplier_gate_callback("intake_out", "tool_config17", "com@scei88.fr")
    ctx = type("C", (), {"state": {"email_sender": "magasin@scei88.fr"}})()

    assert cb(ctx) is None
    assert calls == []  # la garde scei88 court-circuite AVANT toute requête DB


def test_excluded_gate_scei88_with_display_name_and_space(monkeypatch):
    """Sender réel = 'Nom <magasin@scei88.fr> ' (display name + espace terminal)."""
    from th2customers.scei import gates as m

    calls = []
    monkeypatch.setattr(m, "load_tool_config_params", lambda *a, **k: (calls.append(1), ("t", None))[1])
    cb = m.build_excluded_supplier_gate_callback("intake_out", "tool_config17", "com@scei88.fr")
    ctx = type("C", (), {"state": {"email_sender": "Magasin SCEI <magasin@scei88.fr> "}})()

    assert cb(ctx) is None
    assert calls == []
