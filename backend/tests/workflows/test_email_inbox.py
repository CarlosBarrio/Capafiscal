"""
Hito: una factura entra sola por correo y el sistema hace lo mismo que con
cualquier otra entrada — la lee, la registra, decide la ruta, detecta la
anomalía, abre el expediente y espera la aprobación humana.
"""
from __future__ import annotations

from datetime import date
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

from tests.workflows.test_scenarios import add_invoice

SYNTHETIC = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "sinteticas"
INVOICE_PDF = SYNTHETIC / "ingenieria_pie_legal.pdf"  # INGENIERIA DEMO S.L. · 2.420,00 € · 30/09/2026


def build_eml(message_id: str = "<factura-260612@ingenieria-demo.test>", attachment: Path = INVOICE_PDF) -> bytes:
    message = EmailMessage()
    message["From"] = "Ingeniería Demo <facturacion@ingenieria-demo.test>"
    message["To"] = "admin@estudio-ejemplo.test"
    message["Subject"] = "Factura 260612"
    message["Message-ID"] = message_id
    message["Date"] = "Wed, 30 Sep 2026 10:15:00 +0200"
    message.set_content("Adjuntamos la factura del mes. Un saludo.")
    message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    message.add_attachment(b"GIF89a", maintype="image", subtype="gif", filename="logo.gif")
    return message.as_bytes()


def setup(client):
    from app.database import SessionLocal

    client.put("/api/company", json={"name": "Estudio Ejemplo Arquitectos S.L.P.", "tax_id": "B49123458", "legal_form": "SOCIEDAD"})
    # Historial: INGENIERIA DEMO factura unos 400 € al mes
    with SessionLocal() as database:
        for index, total in enumerate([400, 410, 395, 405, 400]):
            add_invoice(database, supplier="INGENIERIA DEMO S.L.", tax_id="B46444444", number=f"H-{index}", total=total, when=date(2026, 4, 1) + timedelta(days=30 * index))
        database.commit()


def import_eml(client, raw: bytes):
    return client.post("/api/connectors/email/import", files={"uploaded_file": ("factura.eml", raw, "message/rfc822")})


def test_invoice_arrives_by_email_and_becomes_a_suspicious_invoice_case(client):
    setup(client)
    response = import_eml(client, build_eml())
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["subject"] == "Factura 260612"
    assert result["skipped"] == ["logo.gif (formato no admitido)"]
    attachment = result["attachments"][0]
    assert attachment["new_document"] is True and attachment["event"]["status"] == "COMPLETED"
    assert attachment["event"]["source"] == "email"

    # La factura se ha leído bien (reglas aprendidas de facturas reales)
    document = client.get(f"/api/documents/{attachment['document_id']}").json()
    invoice = document["invoice"]
    assert invoice["supplier_tax_id"] == "B46444444" and invoice["direction"] == "RECEIVED"
    assert float(invoice["total"]) == 2420.00 and invoice["invoice_number"] == "260612"
    assert document["source"] == "email"

    # El orquestador eligió la ruta y abrió el expediente
    case = client.get(f"/api/cases/{attachment['case_id']}").json()
    assert case["procedure"] == "FACTURA_SOSPECHOSA" and case["status"] == "WAITING_HUMAN"
    assert case["route"]["code"] == "factura_sospechosa"
    assert any(item["tipo"] == "IMPORTE_ATIPICO" and item["riesgo"] == "high" for item in case["findings"])
    assert any("Retener el pago" in action["label"] for action in case["actions"])

    # El humano aprueba: queda registrado
    approved = client.post(f"/api/cases/{case['id']}/approve").json()
    assert approved["status"] == "RESOLVED"


def test_the_same_email_twice_is_not_processed_twice(client):
    setup(client)
    first = import_eml(client, build_eml()).json()
    second = import_eml(client, build_eml()).json()
    assert second["attachments"][0]["duplicate"] is True
    assert second["attachments"][0]["case_id"] == first["attachments"][0]["case_id"]
    assert len(client.get("/api/cases", params={"view": "anomalies"}).json()) == 1


def test_local_mailbox_folder_is_polled(client):
    from app.config import settings

    setup(client)
    folder = settings.data_dir / "buzon"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "correo.eml").write_bytes(build_eml("<carpeta-1@test>"))
    status = client.get("/api/connectors/email").json()
    assert status["local_folder"]["pending"] == 1 and status["imap"]["configured"] is False

    result = client.post("/api/connectors/email/poll").json()
    assert result["messages"] == 1 and result["cases"]
    assert not (folder / "correo.eml").exists() and (folder / "procesados" / "correo.eml").exists()
    assert client.get("/api/connectors/email").json()["local_folder"]["pending"] == 0


def test_imap_reads_unseen_and_marks_them_seen(client, monkeypatch):
    from app.config import settings
    from app.connectors.email import client as email_client

    setup(client)
    raw = build_eml("<imap-1@test>")
    seen: list[bytes] = []

    class FakeIMAP:
        def __init__(self, host, port):
            assert host == "imap.test"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            assert (user, password) == ("buzon@test", "secreto")

        def select(self, folder):
            assert folder == "INBOX"

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"7"]
            if command == "fetch":
                return "OK", [(b"7 (BODY[] {123}", raw), b")"]
            if command == "store":
                seen.append(args[0])
                return "OK", []
            raise AssertionError(command)

    monkeypatch.setattr(settings, "imap_host", "imap.test")
    monkeypatch.setattr(settings, "imap_user", "buzon@test")
    monkeypatch.setattr(settings, "imap_password", "secreto")
    monkeypatch.setattr(email_client.imaplib, "IMAP4_SSL", FakeIMAP)

    result = client.post("/api/connectors/email/poll").json()
    assert result["messages"] == 1 and result["documents"] == 1
    assert seen == [b"7"]
    events = client.get("/api/events").json()["events"]
    assert any(item["source"] == "email" and item["external_id"].startswith("imap-1@test#") for item in events)


# ---------------------------------------------------------------------
# El buzón como automatización (planificador): regresión del commit dentro de un savepoint.
# Antes, run_automation envolvía el buzón en begin_nested(); el primer commit del primer correo cerraba
# el savepoint y los demás correos fallaban: entraba uno por vuelta y la ejecución decía «Sin correos nuevos».
# ---------------------------------------------------------------------

THREE_MAILS = (("<lote-1@test>", "ingenieria_pie_legal.pdf"), ("<lote-2@test>", "limpieza_tabla_totales.pdf"),
               ("<lote-3@test>", "autonomo_dos_paginas.pdf"))


def mailbox(mails=THREE_MAILS, *, clean: bool = True):
    import shutil

    from app.config import settings

    folder = settings.data_dir / "buzon"
    if clean:  # la carpeta de datos es de toda la sesión de tests: cada prueba empieza con el buzón vacío
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    for index, (message_id, pdf) in enumerate(mails):
        (folder / f"correo-{index}.eml").write_bytes(build_eml(message_id, attachment=SYNTHETIC / pdf))
    return folder


def counts():
    from sqlalchemy import func
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Document
    from app.models import IngestedEvent

    with SessionLocal() as database:
        documents = database.scalar(select(func.count()).select_from(Document).where(Document.source == "email"))
        events = database.scalar(select(func.count()).select_from(IngestedEvent).where(IngestedEvent.source == "email"))
    return documents, events


def run_inbox():
    from app.automation_service import run_automation
    from app.database import SessionLocal

    with SessionLocal() as database:
        run = run_automation(database, "EMAIL_INBOX", trigger="SCHEDULE")
        database.commit()
        return run.status, run.items, run.summary


def test_one_scheduled_run_processes_every_waiting_email(client):
    setup(client)
    folder = mailbox()

    status, items, summary = run_inbox()
    assert status == "OK" and items == 3, summary
    assert summary.startswith("3 correo(s), 3 documento(s)")
    assert counts() == (3, 3)
    assert not list(folder.glob("*.eml")) and len(list((folder / "procesados").glob("*.eml"))) == 3

    # Segunda vuelta: nada nuevo, nada repetido
    status, items, summary = run_inbox()
    assert (status, items, summary) == ("NOTHING", 0, "Sin correos nuevos.")
    assert counts() == (3, 3)


def test_the_same_emails_delivered_again_are_not_imported_twice(client):
    setup(client)
    mailbox()
    run_inbox()
    mailbox(clean=False)  # el servidor los entrega otra vez (mismo Message-ID y mismos adjuntos)
    status, _items, _summary = run_inbox()
    assert status == "OK"
    assert counts() == (3, 3)  # ni documentos ni eventos nuevos: la idempotencia se mantiene


def test_a_failing_email_rolls_back_only_itself_and_is_retried(client, monkeypatch):
    """Un correo que falla a mitad (después de escribir algo) deshace solo lo suyo; los demás quedan
    guardados, el fallido sigue en el buzón, la ejecución lo dice y la vuelta siguiente lo procesa."""
    from app.connectors.email import client as email_client
    from app.models import Document

    setup(client)
    folder = mailbox()
    original = email_client.import_eml

    def flaky(database, raw, *, provider="importacion"):
        if b"<lote-2@test>" in raw:
            database.add(Document(original_filename="a-medias.pdf", stored_filename="a-medias.pdf", sha256="0" * 64, extension=".pdf",
                                  size_bytes=1, source="email", status="RECEIVED", extraction_status="PENDING"))
            database.flush()
            raise RuntimeError("fallo simulado a mitad del correo")
        return original(database, raw, provider=provider)

    monkeypatch.setattr(email_client, "import_eml", flaky)
    status, items, summary = run_inbox()
    assert status == "OK" and items == 2
    assert "1 correo(s) no se pudieron procesar" in summary
    assert counts() == (2, 2)  # lo escrito a medias por el correo fallido no quedó
    assert [path.name for path in folder.glob("*.eml")] == ["correo-1.eml"]

    monkeypatch.setattr(email_client, "import_eml", original)
    status, items, summary = run_inbox()
    assert (status, items) == ("OK", 1) and counts() == (3, 3)
    assert not list(folder.glob("*.eml"))
