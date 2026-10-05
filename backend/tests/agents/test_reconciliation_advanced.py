"""Conciliación profesional: varias facturas en un pago, pagos a cuenta, devoluciones, comisiones,
traspasos, nóminas e impuestos. Solo lo SEGURO se aplica sin persona."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from tests.agents.test_proactive import import_csv
from tests.agents.test_reconciliation_levels import add
from tests.agents.test_reconciliation_levels import by_description


def report(client):
    return by_description(client.get("/api/bank/reconciliation").json())


def invoice(client, invoice_id):
    from app.database import SessionLocal
    from app.models import Invoice

    with SessionLocal() as database:
        item = database.get(Invoice, invoice_id)
        return {"paid_at": item.paid_at}


def test_one_payment_for_several_invoices_is_split_automatically(client):
    ids = add(client, {
        "a": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0501", total=600.00, when=date(2026, 9, 1)),
        "b": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0502", total=645.00, when=date(2026, 9, 3)),
        "c": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0503", total=990.00, when=date(2026, 9, 5)),
    })
    imported = import_csv(client, [(date(2026, 9, 20), "TRANSFERENCIA ACME SUMINISTROS", "-1245,00")])
    assert imported["auto_matched"] == 1
    row = report(client)["TRANSFERENCIA ACME SUMINISTROS"]
    assert row["state"] == "CONCILIADO"
    assert sorted(item["invoice_number"] for item in row["allocations"]) == ["FAC-0501", "FAC-0502"]
    assert invoice(client, ids["a"])["paid_at"] and invoice(client, ids["b"])["paid_at"] and not invoice(client, ids["c"])["paid_at"]


def test_two_combinations_with_the_same_total_are_never_automatic(client):
    add(client, {
        "a": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0511", total=100.00, when=date(2026, 9, 1)),
        "b": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0512", total=200.00, when=date(2026, 9, 2)),
        "c": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0513", total=120.00, when=date(2026, 9, 3)),
        "d": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0514", total=180.00, when=date(2026, 9, 4)),
    })
    assert import_csv(client, [(date(2026, 9, 20), "TRANSFERENCIA ACME SUMINISTROS", "-300,00")])["auto_matched"] == 0
    row = report(client)["TRANSFERENCIA ACME SUMINISTROS"]
    assert row["proposal"]["kind"] == "MULTI" and row["level"] == "PROBABLE" and "elige tú" in row["decision"]


def test_installments_partial_then_last_payment(client):
    ids = add(client, {"x": dict(supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0521", total=1000.00, when=date(2026, 9, 1))})
    import_csv(client, [(date(2026, 9, 10), "TRANSFERENCIA ACME FAC-0521 1/2", "-400,00")])
    first = report(client)["TRANSFERENCIA ACME FAC-0521 1/2"]
    # El importe no cuadra con la factura: sigue siendo un conflicto, pero se propone el pago a cuenta.
    assert first["level"] == "CONFLICTO" and first["proposal"]["kind"] == "PARCIAL" and "600,00 €" in first["proposal"]["explanation"]
    accepted = client.post(f"/api/bank/transactions/{first['transaction_id']}/accept-proposal").json()
    assert accepted["match_status"] == "MATCHED" and not invoice(client, ids["x"])["paid_at"]

    # El segundo pago deja la factura pagada y, con el número en el concepto, se concilia solo.
    assert import_csv(client, [(date(2026, 9, 25), "TRANSFERENCIA ACME FAC-0521 2/2", "-600,00")])["auto_matched"] == 1
    assert invoice(client, ids["x"])["paid_at"] == date(2026, 9, 25)


def test_bank_fees_transfers_and_returns_are_justified_without_invoice(client):
    add(client, {})
    imported = import_csv(client, [
        (date(2026, 9, 5), "TRASPASO A CUENTA AHORRO", "-5000,00"),
        (date(2026, 9, 5), "TRASPASO DESDE CUENTA CORRIENTE", "5000,00"),
        (date(2026, 9, 8), "COMISION TRANSFERENCIA", "-2,50"),
        (date(2026, 9, 10), "RECIBO SEGURO HOGAR POLIZA 123", "-180,00"),
        (date(2026, 9, 14), "DEVOLUCION RECIBO SEGURO HOGAR", "180,00"),
    ])
    assert imported["auto_matched"] == 5
    rows = report(client)
    kinds = {description: row["allocations"][0]["kind"] for description, row in rows.items()}
    assert kinds == {"TRASPASO A CUENTA AHORRO": "TRASPASO", "TRASPASO DESDE CUENTA CORRIENTE": "TRASPASO", "COMISION TRANSFERENCIA": "COMISION",
                     "RECIBO SEGURO HOGAR POLIZA 123": "DEVOLUCION", "DEVOLUCION RECIBO SEGURO HOGAR": "DEVOLUCION"}
    assert all(row["state"] == "CONCILIADO" for row in rows.values())


def test_taxes_match_a_filed_model_and_payroll_without_payslips_waits_for_a_person(client):
    from app.database import SessionLocal
    from app.models import TaxFiling

    add(client, {})
    with SessionLocal() as database:
        database.add(TaxFiling(model="303", year=2026, period=2, filed_at=date(2026, 7, 15), amount=Decimal("845.46")))
        database.commit()
    imported = import_csv(client, [
        (date(2026, 7, 20), "ADEUDO AEAT MODELO 303", "-845,46"),
        (date(2026, 7, 31), "PAGO NOMINAS JULIO", "-7420,00"),
    ])
    assert imported["auto_matched"] == 1
    rows = report(client)
    assert rows["ADEUDO AEAT MODELO 303"]["allocations"][0]["kind"] == "IMPUESTO"
    payroll = rows["PAGO NOMINAS JULIO"]
    assert payroll["level"] == "PROBABLE" and payroll["proposal"]["kind"] == "NOMINA" and "no cuadra" in payroll["decision"]
    accepted = client.post(f"/api/bank/transactions/{payroll['transaction_id']}/accept-proposal").json()
    assert accepted["match_status"] == "MATCHED" and accepted["allocations"][0]["kind"] == "NOMINA"


def test_manual_split_validates_and_undo_clears_it(client):
    ids = add(client, {
        "a": dict(supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-0531", total=300.00, when=date(2026, 9, 1)),
        "b": dict(supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-0532", total=200.00, when=date(2026, 9, 2)),
    })
    import_csv(client, [(date(2026, 9, 20), "PAGO VARIOS", "-510,00")])
    row = report(client)["PAGO VARIOS"]
    url = f"/api/bank/transactions/{row['transaction_id']}/allocate"
    too_much = client.post(url, json={"parts": [{"invoice_id": ids["a"], "amount": 300}, {"invoice_id": ids["b"], "amount": 250}]})
    assert too_much.status_code == 409  # a la factura b solo le quedan 200 € (y 550 > 510)
    done = client.post(url, json={"parts": [{"invoice_id": ids["a"], "amount": 300}, {"invoice_id": ids["b"], "amount": 200},
                                            {"kind": "COMISION", "amount": 10}]}).json()
    assert done["match_status"] == "MATCHED" and len(done["allocations"]) == 3
    assert invoice(client, ids["a"])["paid_at"] and invoice(client, ids["b"])["paid_at"]

    undone = client.post(f"/api/bank/transactions/{row['transaction_id']}/unmatch", json={"ignore": False}).json()
    assert undone["match_status"] == "UNMATCHED"
    assert not invoice(client, ids["a"])["paid_at"] and not invoice(client, ids["b"])["paid_at"]


def test_reading_the_reconciliation_never_applies_anything(client):
    from app.database import SessionLocal
    from app.models import BankAllocation

    add(client, {})
    import_csv(client, [(date(2026, 9, 8), "COMISION TRANSFERENCIA", "-2,50")])  # al importar sí se aplica
    with SessionLocal() as database:
        before = database.query(BankAllocation).count()
    for _ in range(3):
        client.get("/api/bank/reconciliation")
        client.get("/api/work")
    with SessionLocal() as database:
        assert database.query(BankAllocation).count() == before == 1
