"""Branchement réel des nœuds ``file`` / ``file_save`` et des pièces jointes.

Fichiers produits : référence du propriétaire seulement, taille bornée,
nombre borné par run. Envoi SMTP simulé au niveau de ``smtplib`` ; OneDrive
au niveau de ``onedrive_write.tool_upload_file``.
"""

import asyncio
import email
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apowerb.core import workflow_attachments as wa
from apowerb.core import workflow_runtime as rt
from apowerb.core.workflow_graph import GraphError
from apowerb.helpers import email_sender

ALICE, BOB = "alice@example.test", "bob@example.test"
CSV = "text/csv"


@pytest.fixture(autouse=True)
def att_root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setenv("TH2_ATTACHMENT_ROOT", str(root))
    return root


def _run(coro):
    return asyncio.run(coro)


# --- stockage des fichiers produits --------------------------------------------


def test_generated_file_is_resolved_for_its_owner_only(att_root):
    entry = wa.store_generated(ALICE, "ventes.csv", CSV, b"a,b\n1,2\n")
    assert entry["ref"].startswith("att_") and entry["size"] == 8
    meta = wa.resolve(entry["ref"], ALICE)
    with open(meta["path"], "rb") as stored:
        data = stored.read()
    assert data == b"a,b\n1,2\n"
    assert "workflow_generated" in meta["path"]
    with pytest.raises(wa.AttachmentError):
        wa.resolve(entry["ref"], BOB)


def test_generated_file_is_bounded():
    with pytest.raises(wa.AttachmentError) as exc:
        wa.store_generated(
            ALICE, "x.txt", "text/plain", b"x" * (wa.MAX_GENERATED_BYTES + 1)
        )
    assert exc.value.code == "attachment_size"


def test_generated_file_type_is_one_of_the_produced_formats():
    with pytest.raises(wa.AttachmentError) as exc:
        wa.store_generated(ALICE, "x.html", "text/html", b"<script>")
    assert exc.value.code == "attachment_type"


def test_simulated_mail_upload_still_refuses_office_files():
    # Les types produits ne s'ouvrent pas pour autant aux envois de fichiers.
    with pytest.raises(wa.AttachmentError):
        wa.store(
            ALICE,
            "x.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"PK",
        )


def test_save_file_service_caps_files_per_run(monkeypatch):
    monkeypatch.setattr(rt, "MAX_RUN_FILES", 2)
    services = rt.services_for(ALICE)
    for i in range(2):
        _run(services.save_file(f"f{i}.csv", CSV, b"a\n"))
    with pytest.raises(GraphError) as exc:
        _run(services.save_file("f3.csv", CSV, b"a\n"))
    assert exc.value.code == "file_run_limit"
    # Un autre run repart de zéro.
    _run(rt.services_for(ALICE).save_file("g.csv", CSV, b"a\n"))


def test_a_resumed_run_counts_the_files_it_already_made(monkeypatch):
    monkeypatch.setattr(rt, "MAX_RUN_FILES", 2)
    state = {
        "artifacts": [
            {"file_ref": "att_" + "a" * 32, "file_name": "a.csv"},
            {"type": "chart"},
        ]
    }
    assert rt.files_produced(state) == 1
    assert rt.files_produced(None) == 0
    services = rt.services_for(ALICE, files_already=rt.files_produced(state))
    _run(services.save_file("b.csv", CSV, b"a\n"))
    with pytest.raises(GraphError) as exc:
        _run(services.save_file("c.csv", CSV, b"a\n"))
    assert exc.value.code == "file_run_limit"


# --- pièces jointes du mail -----------------------------------------------------


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.test")
    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(rt, "_send_history", {})
    return FakeSMTP


def test_email_carries_the_owner_files(smtp):
    ref = wa.store_generated(ALICE, "ventes.csv", CSV, b"a,b\n1,2\n")["ref"]
    notify = rt._notify_for(ALICE)
    sent = _run(
        notify(
            "n", "email", ["x@example.com"], "Rapport", "Ci-joint", attachments=[ref]
        )
    )
    assert sent == 1
    msg = email.message_from_bytes(bytes(smtp.sent[0]))
    parts = [p for p in msg.walk() if p.get_filename()]
    assert [p.get_filename() for p in parts] == ["ventes.csv"]
    assert parts[0].get_payload(decode=True) == b"a,b\n1,2\n"
    assert parts[0].get_content_type() == "text/csv"


def test_email_refuses_a_file_of_someone_else(smtp):
    ref = wa.store_generated(BOB, "secret.csv", CSV, b"x")["ref"]
    notify = rt._notify_for(ALICE)
    with pytest.raises(GraphError) as exc:
        _run(notify("n", "email", ["x@example.com"], "s", "b", attachments=[ref]))
    assert exc.value.code == "notification_attachment_not_found"
    assert smtp.sent == []


def test_email_attachments_are_bounded_in_total(smtp, monkeypatch):
    monkeypatch.setattr(rt, "MAX_EMAIL_ATTACHMENT_BYTES", 10)
    refs = [
        wa.store_generated(ALICE, f"f{i}.csv", CSV, b"123456")["ref"] for i in range(2)
    ]
    with pytest.raises(GraphError) as exc:
        _run(
            rt._notify_for(ALICE)(
                "n", "email", ["x@example.com"], "s", "b", attachments=refs
            )
        )
    assert exc.value.code == "notification_attachments_too_large"
    assert smtp.sent == []


def test_attachments_only_go_by_email(smtp):
    ref = wa.store_generated(ALICE, "f.csv", CSV, b"a")["ref"]
    with pytest.raises(GraphError) as exc:
        _run(rt._notify_for(ALICE)("n", "teams", [], "s", "b", attachments=[ref]))
    assert exc.value.code == "notification_attachments_email_only"


def test_pretend_send_never_logs_file_bytes(monkeypatch, caplog):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    secret = b"CONTENU-CONFIDENTIEL-123"
    with caplog.at_level(logging.DEBUG):
        _run(
            email_sender.send_email(
                to="x@example.com",
                subject="s",
                body="b",
                attachments=[("f.csv", CSV, secret)],
            )
        )
    assert "CONTENU-CONFIDENTIEL" not in caplog.text


# --- dépôt OneDrive -------------------------------------------------------------


def test_save_to_onedrive_uploads_the_resolved_file_as_the_owner(monkeypatch):
    from apowerb.core import invocation_context
    from apowerb.tools_store.portfolio import onedrive_write

    entry = wa.store_generated(ALICE, "ventes.csv", CSV, b"a,b\n")
    seen = {}

    def fake_upload(
        local_file_path, destination_path=None, conflict_behavior="rename", **k
    ):
        seen.update(
            path=local_file_path,
            dest=destination_path,
            conflict=conflict_behavior,
            invoker=invocation_context.get_current_invoker(),
        )
        return {
            "status": "success",
            "name": "ventes.csv",
            "webUrl": "https://od.example/v",
            "parentPath": "/drive/root:/Rapports",
        }

    monkeypatch.setattr(onedrive_write, "tool_upload_file", fake_upload)
    out = _run(
        rt.services_for(ALICE).save_to_drive(
            "onedrive", entry["ref"], "Rapports", "replace"
        )
    )
    assert seen["path"] == wa.resolve(entry["ref"], ALICE)["path"]
    assert seen["dest"] == "Rapports/ventes.csv"
    assert seen["conflict"] == "replace"
    assert seen["invoker"] == ALICE
    assert out == {
        "name": "ventes.csv",
        "path": "Rapports/ventes.csv",
        "web_url": "https://od.example/v",
    }


def test_save_to_onedrive_root_when_no_folder(monkeypatch):
    from apowerb.tools_store.portfolio import onedrive_write

    entry = wa.store_generated(ALICE, "ventes.csv", CSV, b"a")
    dests = []
    monkeypatch.setattr(
        onedrive_write,
        "tool_upload_file",
        lambda local_file_path, destination_path=None, conflict_behavior="rename", **k: (
            dests.append(destination_path)
            or {"status": "success", "name": "ventes.csv", "webUrl": "u"}
        ),
    )
    _run(rt.services_for(ALICE).save_to_drive("onedrive", entry["ref"], "", "rename"))
    assert dests == ["ventes.csv"]


def test_save_to_onedrive_refuses_a_file_of_someone_else(monkeypatch):
    from apowerb.tools_store.portfolio import onedrive_write

    entry = wa.store_generated(BOB, "b.csv", CSV, b"a")
    monkeypatch.setattr(
        onedrive_write, "tool_upload_file", lambda *a, **k: pytest.fail("rien ne part")
    )
    with pytest.raises(GraphError) as exc:
        _run(
            rt.services_for(ALICE).save_to_drive("onedrive", entry["ref"], "", "rename")
        )
    assert exc.value.code == "file_save_not_found"


def test_save_to_onedrive_reports_the_tool_error_without_detail(monkeypatch):
    from apowerb.tools_store.portfolio import onedrive_write

    entry = wa.store_generated(ALICE, "a.csv", CSV, b"a")
    monkeypatch.setattr(
        onedrive_write,
        "tool_upload_file",
        lambda *a, **k: {
            "status": "error",
            "message": "Microsoft integration not connected",
        },
    )
    with pytest.raises(GraphError) as exc:
        _run(
            rt.services_for(ALICE).save_to_drive("onedrive", entry["ref"], "", "rename")
        )
    assert exc.value.code == "file_save_failed"
    # Le message de l'outil (chemins serveur possibles) reste dans les journaux.
    assert "Microsoft integration not connected" not in str(exc.value)
    assert "Microsoft integration not connected" not in str(exc.value.params)


# --- téléchargement -------------------------------------------------------------


def test_owner_downloads_a_produced_file():
    from apowerb.routers import workflows

    entry = wa.store_generated(ALICE, "ventes.csv", CSV, b"a,b\n")
    resp = _run(
        workflows.download_workflow_file(
            entry["ref"], current_user=SimpleNamespace(email=ALICE)
        )
    )
    assert resp.media_type == CSV
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "ventes.csv" in resp.headers["content-disposition"]


def test_someone_else_gets_an_opaque_404():
    from apowerb.routers import workflows

    entry = wa.store_generated(ALICE, "ventes.csv", CSV, b"a,b\n")
    for user, ref in (
        (BOB, entry["ref"]),
        (ALICE, "att_" + "0" * 32),
        (ALICE, "../../etc/passwd"),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(
                workflows.download_workflow_file(
                    ref, current_user=SimpleNamespace(email=user)
                )
            )
        assert exc.value.status_code == 404
