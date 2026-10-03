"""Multiempresa: el correo de un cliente nunca acaba en otro.

El buzón (IMAP y data/buzon) y la carpeta de la DEHú son comunes a la instalación. Solo se procesan para el
cliente asignado (EMAIL_CLIENT_ID, DEHU_CLIENT_ID); sin asignación, para nadie. Outlook (una conexión única)
no está disponible en multiempresa. Ver app/connectors/assignment.py. Datos sintéticos.
"""
from __future__ import annotations

import shutil
import threading

import pytest

from tests.integration.test_multiempresa import as_
from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)
from tests.workflows.test_email_inbox import SYNTHETIC
from tests.workflows.test_email_inbox import build_eml

MAILS = (("<a-1@test>", "ingenieria_pie_legal.pdf"), ("<a-2@test>", "limpieza_tabla_totales.pdf"))


def mailbox(mails=MAILS, *, clean=True):
    from app.config import settings

    folder = settings.data_dir / "buzon"
    if clean:
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    for index, (message_id, pdf) in enumerate(mails):
        (folder / f"correo-{message_id.strip('<>@test')}-{index}.eml").write_bytes(build_eml(message_id, attachment=SYNTHETIC / pdf))
    return folder


def email_documents(client_id):
    from app.models import Document
    from app.tenancy import tenant_session

    with tenant_session(client_id) as database:
        return database.query(Document).filter(Document.source == "email").count()


def run_inbox_for(client_id):
    from app.automation_service import run_automation
    from app.tenancy import tenant_session

    with tenant_session(client_id) as database:
        run = run_automation(database, "EMAIL_INBOX", trigger="SCHEDULE")
        database.commit()
        return run.status, run.items, run.summary


def test_mailbox_assigned_to_a_never_reaches_b_even_if_b_runs_first(client, multi, monkeypatch):
    from app.config import settings

    a, b = multi["a"], multi["b"]
    monkeypatch.setattr(settings, "email_client_id", a)
    folder = mailbox()

    status, items, summary = run_inbox_for(b)  # B hace la ronda primero
    assert (status, items) == ("NOTHING", 0) and "asignado a otro cliente" in summary
    assert email_documents(b) == 0 and len(list(folder.glob("*.eml"))) == 2  # B no ha tocado el buzón

    status, items, _summary = run_inbox_for(a)
    assert (status, items) == ("OK", 2)
    assert email_documents(a) == 2 and email_documents(b) == 0


def test_the_whole_scheduler_round_keeps_the_mail_in_its_client(client, multi, monkeypatch):
    from app.automation_service import run_due_all_clients
    from app.config import settings

    a, b = multi["a"], multi["b"]
    monkeypatch.setattr(settings, "email_client_id", b)  # asignado al SEGUNDO cliente de la ronda
    mailbox()
    run_due_all_clients()
    assert email_documents(b) == 2 and email_documents(a) == 0


def test_unassigned_mailbox_is_left_unread_for_every_client(client, multi, monkeypatch):
    """Sin cliente identificable (buzón sin asignar): nadie lo procesa; mejor sin asignar que en otro cliente."""
    from app.automation_service import run_due_all_clients
    from app.config import settings

    monkeypatch.setattr(settings, "email_client_id", None)
    folder = mailbox()
    run_due_all_clients()
    assert email_documents(multi["a"]) == 0 and email_documents(multi["b"]) == 0
    assert len(list(folder.glob("*.eml"))) == 2  # siguen ahí, sin leer
    status, items, summary = run_inbox_for(multi["a"])
    assert (status, items) == ("NOTHING", 0) and "EMAIL_CLIENT_ID" in summary


def test_duplicate_mail_in_the_assigned_client_is_imported_once(client, multi, monkeypatch):
    from app.config import settings

    a = multi["a"]
    monkeypatch.setattr(settings, "email_client_id", a)
    mailbox()
    run_inbox_for(a)
    mailbox(clean=False)  # el mismo correo vuelve a llegar
    run_inbox_for(a)
    assert email_documents(a) == 2 and email_documents(multi["b"]) == 0


def test_manual_poll_status_and_dehu_folder_respect_the_assignment(client, multi, monkeypatch, tmp_path):
    from app.config import settings

    admin, a, b = multi["admin"], multi["a"], multi["b"]
    monkeypatch.setattr(settings, "email_client_id", a)
    mailbox()
    response = client.post("/api/connectors/email/poll", headers=as_(admin, b))
    assert response.status_code == 409 and "otro cliente" in response.json()["detail"]
    assert client.get("/api/connectors/email", headers=as_(admin, b)).json()["assignment"]["available_here"] is False
    assert client.get("/api/connectors/email", headers=as_(admin, a)).json()["assignment"]["available_here"] is True
    assert email_documents(b) == 0
    assert client.post("/api/connectors/email/poll", headers=as_(admin, a)).json()["messages"] == 2

    monkeypatch.setattr(settings, "dehu_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "dehu_client_id", None)
    assert client.post("/api/connectors/dehu/poll", headers=as_(admin, a)).status_code == 409
    monkeypatch.setattr(settings, "dehu_client_id", a)
    assert client.post("/api/connectors/dehu/poll", headers=as_(admin, b)).status_code == 409
    assert client.post("/api/connectors/dehu/poll", headers=as_(admin, a)).status_code == 200


def test_a_user_can_still_import_an_eml_into_the_client_they_chose(client, multi):
    """La importación manual de un .eml es una decisión explícita de la persona para SU cliente."""
    admin, b = multi["admin"], multi["b"]
    raw = build_eml("<manual-b@test>")
    response = client.post("/api/connectors/email/import", files={"uploaded_file": ("c.eml", raw, "message/rfc822")}, headers=as_(admin, b))
    assert response.status_code == 201
    assert email_documents(b) == 1 and email_documents(multi["a"]) == 0


def test_outlook_is_unavailable_in_multi_company(client, multi):
    admin, a = multi["admin"], multi["a"]
    status = client.get("/api/connectors/outlook/status", headers=as_(admin, a)).json()
    assert status["available"] is False and status["connected"] is False
    for method, path in (("post", "/api/connectors/outlook/sync"), ("get", "/api/connectors/outlook/login"),
                         ("post", "/api/connectors/outlook/disconnect")):
        assert getattr(client, method)(path, headers=as_(admin, a)).status_code == 409, path


def test_single_company_mode_is_unchanged(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "email_client_id", None)
    mailbox()
    from app.automation_service import run_automation
    from app.database import SessionLocal

    with SessionLocal() as database:
        run = run_automation(database, "EMAIL_INBOX")
        database.commit()
    assert (run.status, run.items) == ("OK", 2)


def test_simultaneous_rounds_never_duplicate_or_leak(client, multi, monkeypatch):
    from app.automation_service import run_due_all_clients
    from app.config import settings
    from app.database import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("Procesos simultáneos: PostgreSQL (SQLite funciona con un solo proceso).")
    a, b = multi["a"], multi["b"]
    monkeypatch.setattr(settings, "email_client_id", a)
    mailbox()
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def round_():
        try:
            start.wait()
            run_due_all_clients()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=round_) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert email_documents(a) == 2 and email_documents(b) == 0
