"""Conciliación con niveles de confianza: SEGURO / PROBABLE / CONFLICTO / SIN MATCH, con evidencia."""
from __future__ import annotations

from datetime import date

from tests.agents.test_proactive import COMPANY
from tests.agents.test_proactive import import_csv
from tests.workflows.test_scenarios import add_invoice


def add(client, rows):
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    ids = {}
    with SessionLocal() as database:
        for key, kwargs in rows.items():
            ids[key] = add_invoice(database, **kwargs).id
        database.commit()
    return ids


def by_description(report):
    return {row["description"]: row for row in report["movements"]}


def checks(row):
    return {check["key"]: check["ok"] for check in row["checks"]}


def test_seguro_reconciles_with_evidence(client):
    ids = add(client, {"acme": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0381", total=1245.00, when=date(2026, 9, 1))})
    imported = import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ACME FAC-0381", "-1245,00")])
    assert imported["auto_matched"] == 1

    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA ACME FAC-0381"]
    assert row["state"] == "CONCILIADO" and row["level"] == "SEGURO" and row["invoice_id"] == ids["acme"]


def test_two_plausible_invoices_are_a_conflict_and_never_auto(client):
    add(client, {
        "a": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0381", total=1245.00, when=date(2026, 9, 1)),
        "b": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0387", total=1245.00, when=date(2026, 9, 3)),
    })
    imported = import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ACME SUMINISTROS", "-1245,00")])
    assert imported["auto_matched"] == 0

    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA ACME SUMINISTROS"]
    assert row["level"] == "CONFLICTO" and row["state"] == "POSIBLE"
    assert len(row["candidates"]) == 2 and "2 facturas igual de plausibles" in row["decision"]
    tx = client.get("/api/bank/transactions").json()[0]
    assert tx["match_status"] == "UNMATCHED"  # ni siquiera se deja como propuesta: lo decide una persona
    assert client.post("/api/bank/confirm-suggestions", json={"min_score": 60}).json()["confirmed"] == 0


def test_bulk_confirm_only_confirms_safe(client):
    add(client, {"x": dict(supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-78", total=311.00, when=date(2026, 9, 1))})
    import_csv(client, [(date(2026, 9, 12), "PAGO TARJETA 311", "-311,00")])  # solo el importe: PROBABLE
    assert client.post("/api/bank/confirm-suggestions", json={"min_score": 60}).json()["confirmed"] == 0


def test_amount_only_is_probable(client):
    ids = add(client, {"x": dict(supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-77", total=310.00, when=date(2026, 9, 1))})
    import_csv(client, [(date(2026, 9, 12), "PAGO TARJETA", "-310,00")])

    row = by_description(client.get("/api/bank/reconciliation").json())["PAGO TARJETA"]
    assert row["level"] == "PROBABLE" and row["state"] == "POSIBLE" and row["invoice_id"] == ids["x"]
    assert checks(row)["amount"] is True and checks(row)["number"] is False and checks(row)["name"] is False
    assert row["confidence"] < 85 and "solo coincide el importe" in row["decision"]


def test_small_difference_is_probable_not_safe(client):
    add(client, {"x": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0390", total=1245.00, when=date(2026, 9, 1))})
    import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ACME FAC-0390", "-1244,50")])

    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA ACME FAC-0390"]
    assert row["level"] == "PROBABLE" and row["state"] == "POSIBLE"
    assert checks(row)["amount"] is None  # dentro de la tolerancia: ni ✓ ni ✗


def test_identified_invoice_with_wrong_amount_is_conflict(client):
    add(client, {"x": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0391", total=1245.00, when=date(2026, 9, 1))})
    import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ACME FAC-0391", "-1195,00")])

    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA ACME FAC-0391"]
    assert row["level"] == "CONFLICTO" and row["state"] == "IMPORTE_DISTINTO" and row["difference"] == -50.0


def test_nothing_is_sin_match(client):
    add(client, {})
    import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA A NADIE", "-77,00")])
    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA A NADIE"]
    assert row["level"] == "SIN_MATCH" and row["state"] == "SIN_FACTURA" and row["candidates"] == []


def test_iban_in_bank_and_invoice_is_identity_evidence(client):
    from app.database import SessionLocal
    from app.models import ExtractionRun
    from app.models import Invoice

    ids = add(client, {
        "a": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0400", total=500.00, when=date(2026, 9, 1)),
        "b": dict(supplier="OTRA EMPRESA S.L.", tax_id="B00000033", number="OE-1", total=500.00, when=date(2026, 9, 1)),
    })
    with SessionLocal() as database:
        invoice = database.get(Invoice, ids["a"])
        database.add(ExtractionRun(document_id=invoice.document_id, extractor_name="test", extractor_version="1", status="COMPLETED", raw_text="Pago por transferencia a ES00 0000 0000 0000 0000 1234"))
        database.commit()
    import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ES0000000000000000001234", "-500,00")])

    row = by_description(client.get("/api/bank/reconciliation").json())["TRANSFERENCIA ES0000000000000000001234"]
    assert row["invoice_id"] == ids["a"] and row["level"] == "SEGURO" and row["state"] == "CONCILIADO"
    assert checks(row)["iban"] is True  # la evidencia sigue visible después de conciliar


def test_synthetic_bank_dataset_end_to_end(client):
    """Dos extractos de formatos distintos → importación → conciliación, contra la verdad de banco_sintetico.

    Lo crítico: ninguna conciliación automática incorrecta y reimportar no duplica nada."""
    from pathlib import Path

    from evaluation import banco

    report = banco.run(Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "banco_sintetico")
    assert not report["errors"] and report["wrong_auto"] == 0, banco.to_markdown(report)
    assert report["perfect"] == report["movements"] == 18, banco.to_markdown(report)
    assert report["unpaid_ok"], banco.to_markdown(report)
    assert report["reimport"] == {"imported": 0, "duplicated": 18}
