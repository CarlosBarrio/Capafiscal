"""Ventas, cobros, bandeja de salida, jornada, automatizaciones y cierre."""
from __future__ import annotations

from app import clock
import io
import zipfile
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

from app.dunning_service import late_interest
from app.dunning_service import target_level
from app.sales_service import compute_totals
from app.sales_service import fill_template
from app.sales_service import madrid_now
from app.sales_service import record_hash

TODAY = clock.today()


def setup_company(client, **extra):
    body = {
        "name": "Taller Barrio S.L.",
        "tax_id": "B12345674",
        "legal_form": "SOCIEDAD",
        "email": "admin@taller.es",
        "iban": "ES9121000418450200051332",
    }
    body.update(extra)
    response = client.put("/api/company", json=body)
    assert response.status_code == 200, response.text


def create_customer(client, **extra):
    body = {"name": "Construcciones Arana S.A.", "tax_id": "A58818501", "email": "pagos@arana.es", "payment_days": 30}
    body.update(extra)
    response = client.post("/api/sales/customers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def issue(client, customer_id, lines, **extra):
    draft = client.post("/api/sales/invoices", json={"customer_id": customer_id, "lines": lines, **extra})
    assert draft.status_code == 201, draft.text
    issued = client.post(f"/api/sales/invoices/{draft.json()['id']}/issue")
    assert issued.status_code == 200, issued.text
    return issued.json()


# ---------------------------------------------------------------------
# Cálculos puros
# ---------------------------------------------------------------------


def test_totals_with_mixed_vat_and_withholding():
    lines = [
        {"description": "Consultoría", "quantity": 10, "unit_price": 50, "discount": 0, "vat_rate": 21, "amount": 500},
        {"description": "Libro", "quantity": 1, "unit_price": 20, "discount": 0, "vat_rate": 4, "amount": 20},
    ]
    totals = compute_totals(lines, 15)
    assert totals["subtotal"] == Decimal("520.00")
    assert totals["tax_total"] == Decimal("105.80")
    assert totals["withholding_total"] == Decimal("78.00")
    assert totals["total"] == Decimal("547.80")


def test_verifactu_hash_is_deterministic_and_chained():
    first = record_hash(
        issuer_tax_id="B12345674", code="F2026-0001", issue_date=date(2026, 9, 1), invoice_type="F1",
        tax_total=Decimal("21"), total=Decimal("121"), previous_hash="", timestamp="2026-09-01T10:00:00+02:00",
    )
    assert len(first) == 64 and first == first.upper()
    second = record_hash(
        issuer_tax_id="B12345674", code="F2026-0002", issue_date=date(2026, 9, 1), invoice_type="F1",
        tax_total=Decimal("21"), total=Decimal("121"), previous_hash=first, timestamp="2026-09-01T10:05:00+02:00",
    )
    assert second != first


def test_madrid_offset_changes_with_daylight_saving():
    from datetime import timezone

    summer = madrid_now(datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    winter = madrid_now(datetime(2026, 12, 1, 12, tzinfo=timezone.utc))
    assert summer.utcoffset() == timedelta(hours=2)
    assert winter.utcoffset() == timedelta(hours=1)


def test_template_placeholders():
    assert fill_template("Cuota {mes}", date(2026, 10, 1)) == "Cuota octubre 2026"
    assert fill_template("Iguala {trimestre}", date(2026, 10, 1)) == "Iguala 4T 2026"


def test_late_interest_and_levels():
    result = late_interest(Decimal("1000"), date(2025, 1, 31), date(2025, 3, 2), Decimal("10"))
    assert result["days"] == 30
    assert result["amount"] == Decimal("8.22")  # 1000 × 10 % × 30 / 365
    assert target_level(0) == 0
    assert target_level(1) == 1
    assert target_level(20) == 2
    assert target_level(45) == 3


# ---------------------------------------------------------------------
# Emisión de facturas
# ---------------------------------------------------------------------


def test_issue_requires_company_and_customer(client):
    customer = create_customer(client)
    draft = client.post("/api/sales/invoices", json={"customer_id": customer["id"], "lines": [{"description": "Servicio", "unit_price": 100}]}).json()
    response = client.post(f"/api/sales/invoices/{draft['id']}/issue")
    assert response.status_code == 422
    assert "Mi empresa" in response.json()["detail"]


def test_issue_numbers_chain_and_feeds_ledger(client):
    setup_company(client)
    customer = create_customer(client)

    first = issue(client, customer["id"], [{"description": "Mantenimiento", "quantity": 2, "unit_price": 100, "vat_rate": 21}])
    second = issue(client, customer["id"], [{"description": "Reparación", "quantity": 1, "unit_price": 50, "vat_rate": 10}])

    year = TODAY.year
    assert first["code"] == f"F{year}-0001"
    assert second["code"] == f"F{year}-0002"
    assert first["total"] == 242.0
    assert first["invoice_type"] == "F1"
    assert second["record"]["previous_hash"] == first["record"]["hash"]
    assert "ValidarQR" in first["record"]["qr_url"]

    chain = client.get("/api/sales/chain").json()
    assert chain == {"valid": True, "records": 2, "broken_at": None}

    # En el libro de emitidas, con su IVA repercutido.
    documents = client.get("/api/documents").json()
    issued = [item for item in documents if item.get("invoice") and item["invoice"]["direction"] == "ISSUED"]
    assert {item["invoice"]["invoice_number"] for item in issued} == {first["code"], second["code"]}

    pdf = client.get(f"/api/sales/invoices/{first['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    qr = client.get(f"/api/sales/invoices/{first['id']}/qr.svg")
    assert qr.status_code == 200 and b"<svg" in qr.content

    # Emitida = inmutable
    assert client.patch(f"/api/sales/invoices/{first['id']}", json={"notes": "x"}).status_code == 422
    assert client.delete(f"/api/sales/invoices/{first['id']}").status_code == 409


def test_rectification_uses_r_series_and_negative_amounts(client):
    setup_company(client)
    customer = create_customer(client)
    original = issue(client, customer["id"], [{"description": "Servicio", "quantity": 1, "unit_price": 300, "vat_rate": 21}])

    draft = client.post(f"/api/sales/invoices/{original['id']}/rectify", json={"reason": "Importe mal facturado"})
    assert draft.status_code == 201, draft.text
    draft = draft.json()
    assert draft["series"] == "R" and draft["total"] == -363.0

    issued = client.post(f"/api/sales/invoices/{draft['id']}/issue").json()
    assert issued["code"].startswith(f"R{TODAY.year}-")
    assert issued["invoice_type"] == "R1"
    assert issued["rectifies_code"] == original["code"]


def test_negative_lines_rejected_in_ordinary_invoice(client):
    customer = create_customer(client)
    response = client.post("/api/sales/invoices", json={"customer_id": customer["id"], "lines": [{"description": "Abono", "quantity": -1, "unit_price": 10}]})
    assert response.status_code == 422


def test_recurring_invoice_generation_issues_and_prepares_email(client):
    setup_company(client)
    customer = create_customer(client)
    template = client.post(
        "/api/sales/recurring",
        json={
            "name": "Cuota de mantenimiento",
            "customer_id": customer["id"],
            "lines": [{"description": "Mantenimiento {mes}", "unit_price": 90, "vat_rate": 21}],
            "frequency": "MONTHLY",
            "next_date": TODAY.isoformat(),
            "auto_issue": True,
            "auto_send": True,
        },
    )
    assert template.status_code == 201, template.text

    run = client.post("/api/automations/RECURRING_INVOICES/run").json()
    assert run["status"] == "OK" and run["items"] == 1

    invoices = client.get("/api/sales/invoices").json()
    assert invoices[0]["status"] == "ISSUED"
    assert "Mantenimiento" in invoices[0]["concept"]

    outbox = client.get("/api/outbox").json()
    assert outbox["messages"][0]["kind"] == "INVOICE"
    assert outbox["messages"][0]["to_email"] == "pagos@arana.es"

    template = client.get("/api/sales/recurring").json()[0]
    assert template["generated_count"] == 1
    assert template["next_date"] > TODAY.isoformat()

    # Ejecutar otra vez no duplica
    again = client.post("/api/automations/RECURRING_INVOICES/run").json()
    assert again["items"] == 0


# ---------------------------------------------------------------------
# Cobros y bandeja de salida
# ---------------------------------------------------------------------


def test_dunning_escalates_and_outbox_flow(client):
    setup_company(client, late_interest_rate=10)
    customer = create_customer(client)
    old_date = (TODAY - timedelta(days=80)).isoformat()
    invoice = issue(
        client, customer["id"], [{"description": "Obra", "unit_price": 1000, "vat_rate": 21}],
        issue_date=old_date, due_date=(TODAY - timedelta(days=40)).isoformat(),
    )

    overview = client.get("/api/collections").json()
    row = overview["overdue"][0]
    assert row["days_overdue"] == 40
    assert row["level_due"] == 3
    assert row["compensation"] == 40.0
    assert row["interest"] > 0

    created = client.post("/api/collections/remind", json={}).json()
    assert created["created"] == 1

    message = client.get("/api/outbox", params={"kind": "DUNNING"}).json()["messages"][0]
    assert message["level"] == 3
    assert "Requerimiento" in message["subject"]
    assert {item["type"] for item in message["attachments"]} == {"sales_invoice", "dunning_letter"}

    # No se duplica
    assert client.post("/api/collections/remind", json={}).json()["created"] == 0

    letter = client.get(f"/api/outbox/{message['id']}/attachments/1")
    assert letter.status_code == 200 and letter.content.startswith(b"%PDF")

    eml = client.get(f"/api/outbox/{message['id']}/eml")
    assert eml.status_code == 200
    assert b"X-Unsent: 1" in eml.content
    assert b"pagos@arana.es" in eml.content

    # Sin SMTP no se puede enviar directamente, pero sí marcar como enviado.
    assert client.post(f"/api/outbox/{message['id']}/send").status_code == 422
    sent = client.post(f"/api/outbox/{message['id']}/mark-sent").json()
    assert sent["status"] == "SENT"

    # Cobrada: desaparece de la lista de vencidas.
    client.post(f"/api/invoices/{invoice['invoice_id']}/payment", json={"paid": True})
    assert client.get("/api/collections").json()["overdue"] == []


def test_outbox_edit_and_discard(client):
    setup_company(client)
    customer = create_customer(client, email=None)
    invoice = issue(client, customer["id"], [{"description": "Servicio", "unit_price": 100}])
    message = client.post(f"/api/sales/invoices/{invoice['id']}/send").json()
    assert message["needs_email"] is True

    updated = client.patch(f"/api/outbox/{message['id']}", json={"to_email": "otro@cliente.es", "subject": "Factura corregida"}).json()
    assert updated["to_email"] == "otro@cliente.es"

    discarded = client.post(f"/api/outbox/{message['id']}/discard").json()
    assert discarded["status"] == "DISCARDED"


def test_smtp_sending_uses_configured_server(client, monkeypatch):
    from app.config import settings

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def ehlo(self):
            pass

        def has_extn(self, name):
            return True

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            sent.append(message)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "facturas@taller.es")
    monkeypatch.setattr(settings, "smtp_allow_external", True)  # servidor real pedido expresamente
    monkeypatch.setattr("app.outbox_service.smtplib.SMTP", FakeSMTP)

    setup_company(client)
    customer = create_customer(client)
    invoice = issue(client, customer["id"], [{"description": "Servicio", "unit_price": 100}])
    message = client.post(f"/api/sales/invoices/{invoice['id']}/send").json()
    result = client.post(f"/api/outbox/{message['id']}/send")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "SENT"
    assert sent and sent[0]["To"].endswith("<pagos@arana.es>")
    assert any(part.get_filename() == f"{invoice['code']}.pdf" for part in sent[0].iter_attachments())

    detail = client.get(f"/api/sales/invoices/{invoice['id']}").json()
    assert detail["sent_at"] is not None


# ---------------------------------------------------------------------
# Registro de jornada
# ---------------------------------------------------------------------


def create_employee(client, **extra):
    body = {"first_name": "Ana", "last_name": "Gil", "hire_date": "2024-01-08", "contract_type": "INDEFINIDO", "annual_salary": 24000, "ss_status": "ALTA"}
    body.update(extra)
    response = client.post("/api/team/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_clock_in_and_out(client):
    person = create_employee(client)
    first = client.post("/api/timesheet/clock", json={"employee_id": person["id"]}).json()
    assert first["action"] == "in"

    board = client.get("/api/timesheet/today").json()
    assert board["people"][0]["state"] == "working"

    # Evita que entrada y salida caigan en el mismo minuto.
    from app.database import SessionLocal
    from app.models import TimeEntry

    with SessionLocal() as database:
        entry = database.get(TimeEntry, first["entry"]["id"])
        entry.clock_in -= timedelta(hours=2)
        database.commit()

    second = client.post("/api/timesheet/clock", json={"employee_id": person["id"]}).json()
    assert second["action"] == "out"
    assert second["entry"]["minutes"] >= 119


def test_manual_entries_need_reason_and_detect_incidents(client):
    person = create_employee(client)
    day = TODAY - timedelta(days=3)

    no_reason = client.post("/api/timesheet/entries", json={"employee_id": person["id"], "work_date": day.isoformat(), "start": "08:00", "end": "19:30"})
    assert no_reason.status_code == 422

    long_day = client.post(
        "/api/timesheet/entries",
        json={"employee_id": person["id"], "work_date": day.isoformat(), "start": "08:00", "end": "19:30", "reason": "Olvidó fichar"},
    )
    assert long_day.status_code == 201, long_day.text

    overlap = client.post(
        "/api/timesheet/entries",
        json={"employee_id": person["id"], "work_date": day.isoformat(), "start": "12:00", "end": "13:00", "reason": "Prueba"},
    )
    assert overlap.status_code == 422

    next_day = day + timedelta(days=1)
    client.post(
        "/api/timesheet/entries",
        json={"employee_id": person["id"], "work_date": next_day.isoformat(), "start": "06:00", "end": "10:00", "reason": "Turno de mañana"},
    )

    kinds = {alert["kind"] for alert in client.get("/api/timesheet/alerts").json()}
    assert "long_day" in kinds
    assert "short_rest" in kinds
    assert "missing" in kinds

    summary = client.get("/api/timesheet/month", params={"year": day.year, "month": day.month}).json()
    row = summary["rows"][0]
    assert row["manual_entries"] >= 1

    pdf = client.get("/api/timesheet/month.pdf", params={"year": day.year, "month": day.month})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xlsx = client.get("/api/timesheet/month.xlsx", params={"year": day.year, "month": day.month})
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")


# ---------------------------------------------------------------------
# Automatizaciones y cierre
# ---------------------------------------------------------------------


def test_automations_overview_and_toggle(client):
    data = client.get("/api/automations").json()
    codes = {item["code"] for item in data["automations"]}
    assert {"DUNNING", "RECURRING_INVOICES", "DAILY_DIGEST", "PAYROLL_DRAFT", "ADVISOR_PACK"} <= codes

    assert client.patch("/api/automations/DUNNING", json={"enabled": False}).status_code == 200
    item = next(entry for entry in client.get("/api/automations").json()["automations"] if entry["code"] == "DUNNING")
    assert item["enabled"] is False and item["next_run"] == "Desactivada"


def test_is_due_logic():
    from app.automation_service import BY_CODE
    from app.automation_service import is_due
    from app.models import AutomationSetting
    from datetime import timezone

    tz = timezone(timedelta(hours=2))
    daily = BY_CODE["DUNNING"]
    setting = AutomationSetting(code="DUNNING", enabled=True, last_run_at=None)
    assert not is_due(daily, setting, datetime(2026, 9, 30, 7, 0, tzinfo=tz))
    assert is_due(daily, setting, datetime(2026, 9, 30, 8, 5, tzinfo=tz))
    setting.last_run_at = datetime(2026, 9, 30, 6, 10, tzinfo=timezone.utc)
    assert not is_due(daily, setting, datetime(2026, 9, 30, 9, 0, tzinfo=tz))

    monthly = BY_CODE["PAYROLL_DRAFT"]
    setting = AutomationSetting(code="PAYROLL_DRAFT", enabled=True, last_run_at=None)
    assert not is_due(monthly, setting, datetime(2026, 9, 20, 9, 0, tzinfo=tz))
    assert is_due(monthly, setting, datetime(2026, 9, 25, 9, 0, tzinfo=tz))

    quarterly = BY_CODE["ADVISOR_PACK"]
    setting = AutomationSetting(code="ADVISOR_PACK", enabled=True, last_run_at=None)
    assert not is_due(quarterly, setting, datetime(2026, 9, 25, 10, 0, tzinfo=tz))
    assert is_due(quarterly, setting, datetime(2026, 10, 5, 10, 0, tzinfo=tz))


def test_payroll_draft_and_digest_automations(client):
    setup_company(client)
    create_employee(client)
    run = client.post("/api/automations/PAYROLL_DRAFT/run").json()
    assert run["status"] == "OK"
    assert client.get("/api/payroll/runs").json()["runs"][0]["status"] == "DRAFT"

    digest = client.post("/api/automations/DAILY_DIGEST/run").json()
    assert digest["status"] == "OK"
    message = client.get("/api/outbox", params={"kind": "DIGEST"}).json()["messages"][0]
    assert message["to_email"] == "admin@taller.es"
    assert "Buenos días" in message["body"]


def test_advisor_pack_contains_books_models_and_invoices(client):
    setup_company(client, advisor_email="gestoria@asesores.es")
    customer = create_customer(client)
    invoice = issue(client, customer["id"], [{"description": "Servicio", "unit_price": 100}])
    quarter = (TODAY.month - 1) // 3 + 1

    response = client.get("/api/advisor/pack.zip", params={"year": TODAY.year, "quarter": quarter})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert "LEEME.txt" in names
    assert any(name.startswith("01_libros/libro_facturas_emitidas") for name in names)
    assert any(name.startswith("02_modelos/") for name in names)
    assert any(name.startswith("03_facturas/emitidas/") and invoice["code"] in name for name in names)

    message = client.post("/api/advisor/send", params={"year": TODAY.year, "quarter": quarter}).json()
    assert message["to_email"] == "gestoria@asesores.es"
    assert message["attachments"][0]["type"] == "advisor_pack"
