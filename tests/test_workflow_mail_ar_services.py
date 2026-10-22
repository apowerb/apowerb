"""Branchement réel des nœuds document / classifier Jev / db_write.

Références de pièces jointes : propriétaire contrôlé, chemin jamais lu hors de
la racine. OCR et Jev simulés au niveau de leurs outils du portfolio ; la
connexion base au niveau de ``fetch_tool_configs``. L'écriture réelle en
PostgreSQL est couverte par ``test_workflow_mail_ar_e2e.py``.
"""

import asyncio
import os

import pytest

from apowerb.core import workflow_attachments as wa
from apowerb.core import workflow_runtime as rt
from apowerb.core.workflow_graph import GraphError

ALICE, BOB = "alice@example.test", "bob@example.test"
PDF = b"%PDF-1.4 fictif"


@pytest.fixture(autouse=True)
def att_root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setenv("TH2_ATTACHMENT_ROOT", str(root))
    return root


# --- références -----------------------------------------------------------------


def test_store_then_resolve_for_the_owner():
    entry = wa.store(ALICE, "AR CMD-1.pdf", "application/pdf", PDF)
    assert entry["ref"].startswith("att_") and entry["size"] == len(PDF)
    meta = wa.resolve(entry["ref"], ALICE)
    with open(meta["path"], "rb") as f:
        assert f.read() == PDF


def test_other_owner_gets_the_same_answer_as_an_unknown_ref():
    entry = wa.store(ALICE, "a.pdf", "application/pdf", PDF)
    with pytest.raises(wa.AttachmentError) as other:
        wa.resolve(entry["ref"], BOB)
    with pytest.raises(wa.AttachmentError) as unknown:
        wa.resolve("att_" + "0" * 32, BOB)
    assert other.value.code == unknown.value.code == "attachment_not_found"


@pytest.mark.parametrize("ref", ["/etc/passwd", "../x", "att_xyz", "", None])
def test_malformed_refs_are_not_found(ref):
    with pytest.raises(wa.AttachmentError, match="introuvable"):
        wa.resolve(ref, ALICE)


def test_register_refuses_a_path_outside_the_root(tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(PDF)
    with pytest.raises(wa.AttachmentError) as exc:
        wa.register(ALICE, outside, "secret.pdf", "application/pdf", 3)
    assert exc.value.code == "attachment_outside_root"


def test_symlink_escaping_the_root_is_refused(tmp_path, att_root):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(PDF)
    link = att_root / "lien.pdf"
    os.symlink(outside, link)
    with pytest.raises(wa.AttachmentError):
        wa.register(ALICE, link, "lien.pdf", "application/pdf", 3)


def test_store_refuses_other_types_and_sizes():
    with pytest.raises(wa.AttachmentError) as t:
        wa.store(ALICE, "x.exe", "application/x-msdownload", b"MZ")
    assert t.value.code == "attachment_type"
    with pytest.raises(wa.AttachmentError) as s:
        wa.store(ALICE, "x.pdf", "application/pdf", b"")
    assert s.value.code == "attachment_size"


def test_store_strips_path_components_from_the_name(att_root):
    entry = wa.store(ALICE, "../../evil.pdf", "application/pdf", PDF)
    assert "/" not in entry["name"] and ".." not in entry["name"]
    assert wa.resolve(entry["ref"], ALICE)["path"].startswith(str(att_root.resolve()))


# --- service document -----------------------------------------------------------


def test_document_service_ocrs_the_resolved_path(monkeypatch):
    from apowerb.tools_store.portfolio import ocr

    entry = wa.store(ALICE, "a.pdf", "application/pdf", PDF)
    seen = {}

    def fake_ocr(source, provider="auto", include_images=False):
        seen.update(source=source, provider=provider)
        return {"status": "success", "markdown": "ACCUSÉ DE RÉCEPTION", "page_count": 1}

    monkeypatch.setattr(ocr, "tool_ocr_document", fake_ocr)
    out = asyncio.run(rt.services_for(ALICE).document(entry["ref"]))
    assert out == {"name": "a.pdf", "text": "ACCUSÉ DE RÉCEPTION", "page_count": 1}
    assert seen == {"source": wa.resolve(entry["ref"], ALICE)["path"], "provider": "azure"}


def test_document_service_never_ocrs_another_owners_file(monkeypatch):
    from apowerb.tools_store.portfolio import ocr

    entry = wa.store(ALICE, "a.pdf", "application/pdf", PDF)
    monkeypatch.setattr(ocr, "tool_ocr_document", lambda **k: pytest.fail("OCR appelé"))
    with pytest.raises(wa.AttachmentError):
        asyncio.run(rt.services_for(BOB).document(entry["ref"]))


def test_document_service_surfaces_ocr_errors(monkeypatch, caplog):
    from apowerb.tools_store.portfolio import ocr

    entry = wa.store(ALICE, "a.pdf", "application/pdf", PDF)
    monkeypatch.setattr(
        ocr,
        "tool_ocr_document",
        lambda **k: {"status": "error", "error_message": "HTTP 401"},
    )
    with pytest.raises(GraphError) as exc:
        asyncio.run(rt.services_for(ALICE).document(entry["ref"]))
    assert exc.value.code == "document_failed" and "a.pdf" in str(exc.value)
    assert "HTTP 401" in caplog.text


# --- service Jev ----------------------------------------------------------------


def test_jev_service_maps_the_single_result(monkeypatch):
    from apowerb.tools_store.portfolio import jev

    calls = {}

    def fake(labels, items, instructions, min_confidence):
        calls.update(labels=labels, items=items, min=min_confidence)
        return {
            "status": "success",
            "results": [
                {
                    "index": 0,
                    "label": "ar",
                    "confidence": 0.97,
                    "uncertain": False,
                    "best_guess": "ar",
                    "usage": {"cost": 0.00002},
                }
            ],
        }

    monkeypatch.setattr(jev, "tool_jev_classify", fake)
    out = asyncio.run(rt.services_for(ALICE).jev_classify({"ar": "x", "autre": "y"}, "texte", "", 0.8))
    assert out == {
        "label": "ar",
        "confidence": 0.97,
        "uncertain": False,
        "best_guess": "ar",
        "usage": {"cost": 0.00002},
    }
    assert calls == {"labels": {"ar": "x", "autre": "y"}, "items": ["texte"], "min": 0.8}


@pytest.mark.parametrize(
    "result",
    [
        {"status": "error", "message": "JEV_API_KEY manquante"},
        {"status": "success", "results": [{"index": 0, "error": "timeout"}]},
        {"status": "success", "results": []},
    ],
)
def test_jev_service_failures_raise(monkeypatch, result):
    from apowerb.tools_store.portfolio import jev

    monkeypatch.setattr(jev, "tool_jev_classify", lambda **k: result)
    with pytest.raises(GraphError) as exc:
        asyncio.run(rt.services_for(ALICE).jev_classify({"a": "x", "b": "y"}, "t", "", 0.7))
    assert exc.value.code == "jev_failed"


# --- connexion db_write ---------------------------------------------------------


def _config(owner, **params):
    return {
        "tool_config_id": "tool_config7",
        "owner_id": owner,
        "tool_category": "database",
        "tool_config_params": params,
    }


@pytest.fixture
def configs(monkeypatch):
    from apowerb.tools_store import tools_helpers

    table = {}

    def fetch(tool_config_id, owner_id, items_to_select="*"):
        cfg = table.get(tool_config_id)
        if cfg and cfg["owner_id"] in (owner_id, "system"):
            return cfg
        return {"status": 404, "message": "not found"}

    monkeypatch.setattr(tools_helpers, "fetch_tool_configs", fetch)
    return table


def test_db_params_need_an_owned_config_with_insert(configs):
    configs["tool_config7"] = _config(ALICE, host="h", database="d", user="u", DB_ALLOWED_OPS="SELECT,INSERT")
    params = rt._db_params("tool_config7", ALICE)
    assert params["DB_NAME"] == "d" and params["DB_HOST"] == "h"


@pytest.mark.parametrize(
    "owner, ops, code",
    [
        (BOB, "SELECT,INSERT", "db_connection_not_found"),
        ("system", "SELECT,INSERT", "db_connection_not_found"),
        (ALICE, "SELECT", "db_write_not_allowed"),
        (ALICE, "", "db_write_not_allowed"),
    ],
)
def test_db_params_refusals(configs, owner, ops, code):
    configs["tool_config7"] = _config(owner, DB_NAME="d", DB_USER="u", DB_ALLOWED_OPS=ops)
    with pytest.raises(GraphError) as exc:
        rt._db_params("tool_config7", ALICE)
    assert exc.value.code == code


def test_insert_row_refuses_non_postgres():
    with pytest.raises(GraphError) as exc:
        rt.insert_row({"DB_TYPE": "mysql", "DB_NAME": "d", "DB_USER": "u"}, "t", {"a": 1})
    assert exc.value.code == "db_type_unsupported"


# --- revue 19/10 : erreurs lisibles sans fuite, noms longs ----------------------


def test_very_long_name_is_truncated_not_a_500():
    entry = wa.store(ALICE, "x" * 300 + ".pdf", "application/pdf", PDF)
    assert len(entry["name"]) <= 120 and entry["name"].endswith(".pdf")
    assert wa.resolve(entry["ref"], ALICE)["name"] == entry["name"]


def test_ocr_error_detail_stays_in_the_logs(monkeypatch):
    from apowerb.tools_store.portfolio import ocr

    entry = wa.store(ALICE, "a.pdf", "application/pdf", PDF)
    monkeypatch.setattr(
        ocr,
        "tool_ocr_document",
        lambda **k: {
            "status": "error",
            "error_message": "Azure: HTTP 401 https://secret-endpoint.cognitiveservices.azure.com/x",
        },
    )
    with pytest.raises(GraphError) as exc:
        asyncio.run(rt.services_for(ALICE).document(entry["ref"]))
    assert "secret-endpoint" not in str(exc.value)
    assert exc.value.code == "document_failed"


def test_db_connection_failure_is_a_coded_error_without_host():
    params = {"DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "p"}
    with pytest.raises(GraphError) as exc:
        rt.insert_row(params, "t", {"a": 1})
    assert exc.value.code == "db_connection_failed"
    assert "127.0.0.1" not in str(exc.value)


def test_bad_port_is_a_coded_error():
    with pytest.raises(GraphError) as exc:
        rt.insert_row({"DB_PORT": "abc", "DB_NAME": "d", "DB_USER": "u"}, "t", {"a": 1})
    assert exc.value.code == "db_connection_failed"


def test_db_params_need_a_database_config(configs):
    configs["tool_config7"] = {
        **_config(ALICE, DB_NAME="d", DB_USER="u", DB_ALLOWED_OPS="INSERT"),
        "tool_category": "email",
    }
    with pytest.raises(GraphError) as exc:
        rt._db_params("tool_config7", ALICE)
    assert exc.value.code == "db_connection_not_found"
