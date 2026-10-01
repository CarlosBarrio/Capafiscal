from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from app.calendar_es import add_business_days
from app.calendar_es import national_holidays
from app.calendar_es import next_business_day
from app.notification_service import compute_deadline
from tests.conftest import FIXTURES_DIR
from tests.conftest import upload


COMPANY = {
    "name": "TALLERES GARCIA S.L.",
    "tax_id": "B20123457",
    "legal_form": "AUTONOMO",
}


def setup_company(client) -> None:
    response = client.put("/api/company", json=COMPANY)
    assert response.status_code == 200
    assert response.json()["configured"] is True


def approve(client, payload) -> int:
    invoice_id = payload["document"]["invoice"]["id"]
    response = client.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert response.status_code == 200, response.text
    return invoice_id


def build_quarter(client, sample_pdfs) -> dict[str, int]:
    """Emitida 1.452 € + asesoría con retención + Repsol, todo aprobado (3T 2026)."""
    setup_company(client)
    issued = approve(client, upload(client, FIXTURES_DIR / "emitida.txt"))
    advisor = approve(client, upload(client, FIXTURES_DIR / "bloques.txt"))
    fuel = approve(client, upload(client, sample_pdfs["factura_repsol_2026"]))
    return {"issued": issued, "advisor": advisor, "fuel": fuel}


# -------------------------------------------------------------------
# Calendario y plazos
# -------------------------------------------------------------------

def test_spanish_calendar_rules():
    assert date(2026, 4, 3) in national_holidays(2026)  # Viernes Santo
    # 30/01/2027 es sábado: pasa al lunes.
    assert next_business_day(date(2027, 1, 30)) == date(2027, 2, 1)
    # 10 hábiles desde el Jueves Santo saltan el Viernes Santo.
    assert add_business_days(date(2026, 4, 2), 10) == date(2026, 4, 17)


@pytest.mark.parametrize(
    ("kind", "notified", "expected"),
    [
        ("REQUERIMIENTO", date(2026, 9, 25), date(2026, 10, 9)),
        # Liquidación notificada el 10: día 20 del mes siguiente.
        ("LIQUIDACION", date(2026, 9, 10), date(2026, 10, 20)),
        # Liquidación notificada el 20: día 5 del segundo mes (5/11 es jueves).
        ("LIQUIDACION", date(2026, 9, 20), date(2026, 11, 5)),
        # Apremio notificado el 10: día 20 del mismo mes (domingo -> lunes 21).
        ("APREMIO", date(2026, 9, 10), date(2026, 9, 21)),
        ("APREMIO", date(2026, 9, 16), date(2026, 10, 5)),
    ],
)
def test_notification_deadline_rules(kind, notified, expected):
    deadline, rule = compute_deadline(kind, notified_at=notified, available_at=None)

    assert deadline == expected
    assert rule


def test_unaccessed_notification_counts_ten_calendar_days():
    deadline, rule = compute_deadline(
        "REQUERIMIENTO",
        notified_at=None,
        available_at=date(2026, 9, 1),
    )

    # Rechazada el 11/09 + 10 días hábiles.
    assert deadline == add_business_days(date(2026, 9, 11), 10)
    assert "43.2" in rule


# -------------------------------------------------------------------
# Emitidas, memoria y terceros
# -------------------------------------------------------------------

def test_issued_invoice_detected_with_company_tax_id(client):
    setup_company(client)
    payload = upload(client, FIXTURES_DIR / "emitida.txt")
    invoice = payload["document"]["invoice"]

    assert invoice["direction"] == "ISSUED"
    assert invoice["customer_name"] == "CONSTRUCCIONES ARANA S.A."
    assert invoice["category"] == "Prestación de servicios"

    assert len(client.get("/api/documents", params={"direction": "ISSUED"}).json()) == 1
    assert client.get("/api/documents", params={"direction": "RECEIVED"}).json() == []


def test_without_company_everything_is_received(client):
    payload = upload(client, FIXTURES_DIR / "emitida.txt")

    assert payload["document"]["invoice"]["direction"] == "RECEIVED"


def test_agent_learns_category_from_correction(client):
    setup_company(client)
    first = upload(client, FIXTURES_DIR / "bloques.txt")
    invoice_id = first["document"]["invoice"]["id"]

    response = client.patch(
        f"/api/invoices/{invoice_id}",
        json={"category": "Software e informática"},
    )
    assert response.status_code == 200

    rules = client.get("/api/supplier-rules").json()
    assert rules[0]["tax_id"] == "B95123456"
    assert rules[0]["category"] == "Software e informática"

    second = upload(client, FIXTURES_DIR / "asesores_octubre.txt")
    invoice = second["document"]["invoice"]

    assert invoice["category"] == "Software e informática"
    assert invoice["field_confidences"]["category"]["source"] == "supplier_rule"

    assert client.delete(f"/api/supplier-rules/{rules[0]['id']}").status_code == 200
    assert client.get("/api/supplier-rules").json() == []


def test_clients_and_receivables(client, sample_pdfs):
    build_quarter(client, sample_pdfs)

    clients = client.get("/api/suppliers", params={"direction": "ISSUED"}).json()
    assert clients[0]["name"] == "CONSTRUCCIONES ARANA S.A."
    assert clients[0]["total_approved"] == 1452.0

    suppliers = client.get("/api/suppliers").json()
    assert {item["name"] for item in suppliers} == {
        "ASESORES REUNIDOS DEL NORTE S.L.",
        "REPSOL COMERCIAL S.A.",
    }

    receivables = client.get("/api/payments", params={"direction": "ISSUED"}).json()
    assert receivables["unpaid_count"] == 1
    assert receivables["unpaid_total"] == 1452.0


# -------------------------------------------------------------------
# Impuestos
# -------------------------------------------------------------------

def boxes(model: dict) -> dict[str, float]:
    return {box["box"]: box["value"] for box in model["boxes"]}


def test_tax_drafts(client, sample_pdfs):
    build_quarter(client, sample_pdfs)

    model_303 = client.get("/api/taxes/models/303", params={"year": 2026, "quarter": 3}).json()
    values = boxes(model_303)
    assert values["07"] == 1200.0
    assert values["09"] == 252.0
    assert values["29"] == 146.22  # 63,00 + 83,22
    assert model_303["result"] == 105.78
    assert model_303["outcome"] == "A ingresar"

    model_130 = client.get("/api/taxes/models/130", params={"year": 2026, "quarter": 3}).json()
    values = boxes(model_130)
    assert values["01"] == 1200.0
    assert values["02"] == 696.28
    assert values["04"] == 100.74
    assert model_130["result"] == 100.74

    model_111 = client.get("/api/taxes/models/111", params={"year": 2026, "quarter": 3}).json()
    assert model_111["result"] == 45.0
    assert boxes(model_111)["07"] == 1

    model_115 = client.get("/api/taxes/models/115", params={"year": 2026, "quarter": 3}).json()
    assert model_115["result"] == 0.0

    assert client.get("/api/taxes/models/999").status_code == 404


def test_tax_calendar_and_filings(client, sample_pdfs):
    build_quarter(client, sample_pdfs)

    entries = client.get("/api/taxes/calendar", params={"year": 2026}).json()["entries"]
    by_key = {(entry["model"], entry["period_label"]): entry for entry in entries}

    third = by_key[("303", "3T 2026")]
    assert third["due_date"] == "2026-10-20"
    assert third["estimate"] == 105.78
    assert ("130", "3T 2026") in by_key  # autónomo
    assert ("202", "1T 2026") not in by_key  # solo sociedades

    # Trimestres pasados sin facturas no se marcan como vencidos.
    assert by_key[("303", "1T 2026")]["status"] in {"NO_DATA", "FILED"}

    response = client.post(
        "/api/taxes/filings",
        json={"model": "303", "year": 2026, "period": 3, "reference": "CSV123", "amount": "105.78"},
    )
    assert response.status_code == 200

    entries = client.get("/api/taxes/calendar", params={"year": 2026}).json()["entries"]
    filed = next(e for e in entries if e["model"] == "303" and e["period_label"] == "3T 2026")
    assert filed["status"] == "FILED"
    assert filed["reference"] == "CSV123"

    response = client.delete(
        "/api/taxes/filings",
        params={"model": "303", "year": 2026, "period": 3},
    )
    assert response.status_code == 200


def test_model_347_threshold(client):
    setup_company(client)

    for index in range(3):
        path = FIXTURES_DIR.parent / f"_emitida_{index}.txt"
        path.write_text(
            (FIXTURES_DIR / "emitida.txt")
            .read_text(encoding="utf-8")
            .replace("TG-2026-0031", f"TG-2026-01{index}"),
            encoding="utf-8",
        )
        try:
            approve(client, upload(client, path))
        finally:
            path.unlink()

    model = client.get("/api/taxes/models/347", params={"year": 2026}).json()

    assert model["applies"] is True
    assert model["counterparties"][0]["key"] == "B"
    assert model["counterparties"][0]["total"] == 4356.0


# -------------------------------------------------------------------
# Notificaciones
# -------------------------------------------------------------------

def test_notification_detected_on_upload(client, sample_pdfs):
    payload = upload(client, sample_pdfs["requerimiento_aeat_2026"])

    assert payload["document"]["kind"] == "NOTIFICATION"

    notifications = client.get("/api/notifications").json()
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification["issuer"] == "AEAT"
    assert notification["notification_type"] == "REQUERIMIENTO"
    assert notification["reference"] == "REQ-2026-0088123"
    assert notification["deadline"] is not None

    inbox = client.get("/api/tasks/review-inbox").json()
    assert [task["task_type"] for task in inbox] == ["NOTIFICATION"]

    response = client.patch(
        f"/api/notifications/{notification['id']}",
        json={"notified_at": "2026-09-25"},
    )
    assert response.json()["deadline"] == "2026-10-09"

    response = client.patch(
        f"/api/notifications/{notification['id']}",
        json={"status": "ANSWERED"},
    )
    assert response.json()["urgency"] == "done"
    assert client.get("/api/tasks/review-inbox").json() == []
    assert client.get("/api/notifications", params={"open": "true"}).json() == []


def test_apremio_amount_and_manual_notification(client):
    upload(client, FIXTURES_DIR / "apremio.txt")
    notification = client.get("/api/notifications").json()[0]

    assert notification["notification_type"] == "APREMIO"
    assert notification["amount"] == 1250.40
    assert notification["reference"] == "A2026-778899"

    response = client.post(
        "/api/notifications",
        json={
            "issuer": "TGSS",
            "notification_type": "LIQUIDACION",
            "notified_at": "2026-09-10",
        },
    )
    assert response.status_code == 201
    assert response.json()["deadline"] == "2026-10-20"

    bad = client.post("/api/notifications", json={"issuer": "INVENTADO"})
    assert bad.status_code == 422


def test_invoice_document_converted_to_notification(client, sample_pdfs):
    payload = upload(client, sample_pdfs["factura_vodafone_2026"])
    document_id = payload["document"]["id"]

    response = client.post(f"/api/notifications/from-document/{document_id}")
    assert response.status_code == 200

    document = client.get(f"/api/documents/{document_id}").json()
    assert document["kind"] == "NOTIFICATION"
    assert document["invoice"] is None


# -------------------------------------------------------------------
# Banco, tesorería y negocio
# -------------------------------------------------------------------

BANK_CSV = (
    "Cuenta: ES00 0000;;;\n"
    "Fecha operación;Concepto;Importe;Saldo\n"
    "15/07/2026;RECIBO REPSOL COMERCIAL FRA-2026-00912;-479,50;10.520,50\n"
    "20/09/2026;TRANSFERENCIA CONSTRUCCIONES ARANA TG-2026-0031;1.452,00;11.972,50\n"
    "21/09/2026;COMISION MANTENIMIENTO;-6,00;11.966,50\n"
).encode("cp1252")


def import_bank(client, content: bytes = BANK_CSV, name: str = "extracto.csv") -> dict:
    response = client.post(
        "/api/bank/import",
        files={"uploaded_file": (name, content)},
        data={"account_label": "Santander"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_bank_import_reconciliation(client, sample_pdfs):
    ids = build_quarter(client, sample_pdfs)

    result = import_bank(client)
    assert result["imported"] == 3
    assert result["suggestions"] == 2

    again = import_bank(client)
    assert again["imported"] == 0
    assert again["duplicated"] == 3

    confirmed = client.post("/api/bank/confirm-suggestions", json={"min_score": 85}).json()
    assert confirmed["confirmed"] == 2

    matched = client.get("/api/bank/transactions", params={"status": "MATCHED"}).json()
    assert len(matched) == 2

    fuel_document = next(
        item for item in client.get("/api/documents").json()
        if item["invoice"] and item["invoice"]["id"] == ids["fuel"]
    )
    assert fuel_document["invoice"]["paid_at"] == "2026-07-15"
    assert fuel_document["invoice"]["payment_method"] == "DOMICILIACION"

    transaction = next(t for t in matched if t["amount"] < 0)
    undone = client.post(
        f"/api/bank/transactions/{transaction['id']}/unmatch",
        json={"ignore": False},
    ).json()
    assert undone["match_status"] == "UNMATCHED"
    assert undone["candidates"][0]["invoice_id"] == ids["fuel"]


def test_bank_rejects_unreadable_file(client):
    response = client.post(
        "/api/bank/import",
        files={"uploaded_file": ("x.csv", b"hola;mundo\n1;2\n")},
    )
    assert response.status_code == 422


def test_bank_excel_with_debit_credit(client):
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Date", "Description", "Debit", "Credit", "Balance"])
    sheet.append([datetime(2026, 7, 10), "Endesa luz", 312.91, None, 5000.00])
    sheet.append([datetime(2026, 7, 11), "Cliente X", None, 100.00, 5100.00])
    buffer = BytesIO()
    workbook.save(buffer)

    result = import_bank(client, buffer.getvalue(), "extracto.xlsx")
    amounts = sorted(t["amount"] for t in client.get("/api/bank/transactions").json())

    assert result["imported"] == 2
    assert amounts == [-312.91, 100.0]


def test_business_health_and_cashflow(client, sample_pdfs):
    build_quarter(client, sample_pdfs)
    import_bank(client)

    health = client.get("/api/business-health", params={"year": 2026, "quarter": 3}).json()
    assert health["income"] == 1200.0
    assert health["expenses"] == 696.28
    assert health["result"] == 503.72
    assert health["vat_balance"] == 105.78
    assert health["bank_balance"] == 11966.5
    assert len(health["months"]) == 12

    cashflow = client.get("/api/cashflow").json()
    labels = [movement["label"] for movement in cashflow["movements"]]
    assert cashflow["current_balance"] == 11966.5

    if date.today() <= date(2026, 10, 20):
        assert any("Modelo 303" in label for label in labels)


# -------------------------------------------------------------------
# Cumplimiento y agenda
# -------------------------------------------------------------------

def make_certificate(days_valid: int, password: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "TALLERES GARCIA SL"),
        x509.NameAttribute(x509.ObjectIdentifier("2.5.4.97"), "VATES-B20123457"),
    ])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )

    return pkcs12.serialize_key_and_certificates(
        b"cert",
        key,
        certificate,
        None,
        serialization.BestAvailableEncryption(password),
    )


def test_certificate_expiry_is_monitored(client):
    content = make_certificate(10, b"clave")

    wrong = client.post(
        "/api/compliance/certificate",
        files={"certificate": ("cert.p12", content)},
        data={"password": "mala"},
    )
    assert wrong.status_code == 422

    response = client.post(
        "/api/compliance/certificate",
        files={"certificate": ("cert.p12", content)},
        data={"password": "clave"},
    )
    assert response.status_code == 200
    assert response.json()["certificate"]["tax_id"] == "B20123457"

    status = client.get("/api/compliance").json()
    certificate = next(item for item in status["items"] if item["code"] == "CERT_DIGITAL")
    assert certificate["status"] == "WARNING"

    agenda = client.get("/api/agenda").json()
    assert any(item["kind"] == "compliance" for item in agenda["items"])


def test_manual_compliance_item(client):
    response = client.patch(
        "/api/compliance/RGPD_REGISTRO",
        json={"status": "OK", "notes": "Firmado con la gestoría"},
    )
    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["code"] == "RGPD_REGISTRO")
    assert item["status"] == "OK"

    assert client.patch("/api/compliance/INVENTADO", json={"status": "OK"}).status_code == 422
    assert client.patch("/api/compliance/RGPD_REGISTRO", json={"status": "RARO"}).status_code == 422


def test_assistant_business_intents(client, sample_pdfs):
    build_quarter(client, sample_pdfs)
    upload(client, sample_pdfs["requerimiento_aeat_2026"])

    def ask(question: str) -> str:
        return client.post("/api/assistant/query", json={"question": question}).json()["answer"]

    assert "Requerimiento" in ask("¿Qué notificaciones tengo pendientes?")
    assert "105,78 €" in ask("IVA del 3T 2026")
    assert "ingresos 1.200,00 €" in ask("¿Cómo va el negocio en el 3T 2026?")
