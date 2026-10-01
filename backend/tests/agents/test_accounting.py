"""Contabilización: el libro diario sale de las facturas y del banco ya conciliado, cuadrado y sin teclear."""
from __future__ import annotations

from tests.agents.test_closing import setup_month

SEPTEMBER = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


def lines_of(entry):
    return {row["account"]: (row["debit"], row["credit"]) for row in entry["lines"]}


def test_journal_books_invoices_and_reconciled_movements(client):
    setup_month(client)
    report = client.get("/api/accounting/journal", params=SEPTEMBER).json()
    assert report["count"] and all(entry["balanced"] for entry in report["entries"])
    assert report["totals"]["debit"] == report["totals"]["credit"]

    invoice = next(entry for entry in report["entries"] if entry["document"] == "FAC-0901")
    booked = lines_of(invoice)
    assert booked["472"] == (210.0, 0.0)
    creditor = "400" if "400" in booked else "410"
    assert booked[creditor] == (0.0, 1210.0)
    assert sum(debit for debit, _ in booked.values()) == 1210.0

    payment = next(entry for entry in report["entries"] if "FAC-0901" in entry["concept"] and entry["source"]["type"] == "bank")
    assert lines_of(payment)[creditor] == (1210.0, 0.0) and lines_of(payment)["572"] == (0.0, 1210.0)
    fee = next(entry for entry in report["entries"] if "COMISION" in entry["concept"])
    assert lines_of(fee) == {"626": (6.0, 0.0), "572": (0.0, 6.0)}

    # Lo que no se puede contabilizar se dice: la factura sin aprobar y la transferencia sin justificar.
    pending = {item["description"]: item["why"] for item in report["pending"]}
    assert "sin aprobar" in pending["Factura LS-0903"] and any("DESCONOCIDO" in key for key in pending)
    assert next(check for check in report["checks"] if check["label"].startswith("Todo lo del periodo"))["ok"] is None


def test_issued_invoice_books_customer_income_and_output_vat(client):
    from app.database import SessionLocal
    from app.models import Invoice

    setup_month(client)
    with SessionLocal() as database:
        invoice = database.query(Invoice).filter_by(invoice_number="T-0902").one()
        invoice.direction, invoice.customer_name, invoice.customer_tax_id, invoice.category = "ISSUED", "CLIENTE EJEMPLO S.L.", "B00600063", "Prestación de servicios"
        database.commit()
    entry = next(item for item in client.get("/api/accounting/journal", params=SEPTEMBER).json()["entries"] if item["document"] == "T-0902")
    booked = lines_of(entry)
    assert booked["430"] == (363.0, 0.0) and booked["705"] == (0.0, 300.0) and booked["477"] == (0.0, 63.0)


def test_exports_and_validation(client):
    setup_month(client)
    csv = client.get("/api/accounting/journal", params={**SEPTEMBER, "format": "csv"})
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    text = csv.content.decode("utf-8-sig").splitlines()
    assert text[0] == "asiento;fecha;cuenta;nombre_cuenta;nif;tercero;concepto;debe;haber;documento" and len(text) > 5
    xlsx = client.get("/api/accounting/journal", params={**SEPTEMBER, "format": "xlsx"})
    assert xlsx.content[:2] == b"PK"
    assert client.get("/api/accounting/journal", params={"date_from": "2026-09-30", "date_to": "2026-09-01"}).status_code == 422


def test_credit_note_books_on_the_opposite_side(client):
    from datetime import date

    from app.database import SessionLocal
    from tests.workflows.test_scenarios import add_invoice

    setup_month(client)
    with SessionLocal() as database:
        add_invoice(database, supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="R-0901", total=-242.00, when=date(2026, 9, 12))
        database.commit()
    entry = next(item for item in client.get("/api/accounting/journal", params=SEPTEMBER).json()["entries"] if item["document"] == "R-0901")
    booked = lines_of(entry)
    assert entry["balanced"] and booked["472"] == (0.0, 42.0)
    assert (booked.get("400") or booked.get("410")) == (242.0, 0.0)
