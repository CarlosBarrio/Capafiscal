"""Tesorería predictiva (retrasos reales, cargos habituales, riesgo explicado) y «¿qué cambia si…?» sin guardar nada."""
from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

from tests.agents.test_closing import setup_month
from tests.agents.test_proactive import COMPANY
from tests.workflows.test_scenarios import add_invoice

TODAY = date(2026, 9, 29)


def import_with_balance(client, rows):
    csv = "Fecha;Concepto;Importe;Saldo\n" + "".join(f"{when:%d/%m/%Y};{concept};{amount};{balance}\n" for when, concept, amount, balance in rows)
    client.post("/api/bank/import", files={"uploaded_file": ("extracto.csv", csv.encode(), "text/csv")}).raise_for_status()


def predict(**kwargs):
    from app.database import SessionLocal
    from app.tenancy import tenant_session  # noqa: F401  (el cliente por defecto)
    from app.treasury import predict as run

    with SessionLocal() as database:
        return run(database, today=TODAY, **kwargs)


def test_monthly_charges_without_invoice_are_projected(client):
    client.put("/api/company", json=COMPANY)
    import_with_balance(client, [(date(2026, month, 5), f"RECIBO ALQUILER NAVE {month:02d}/2026", "-1500,00", f"{20000 - month * 1500},00") for month in (6, 7, 8, 9)])
    forecast = predict(horizon_days=30)
    recurring = forecast["recurring"]
    assert [item["date"] for item in recurring] == ["2026-10-05"] and recurring[0]["amount"] == -1500.0
    assert "4 meses" in recurring[0]["why"] and forecast["projected_balance"] == 6500.0 - 1500.0


def test_collections_move_to_when_the_customer_really_pays(client):
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    import_with_balance(client, [(date(2026, 9, 20), "TRANSFERENCIA RECIBIDA", "100,00", "5000,00")])
    with SessionLocal() as database:
        for number, due, paid in (("V-1", date(2026, 6, 30), date(2026, 7, 20)), ("V-2", date(2026, 7, 31), date(2026, 8, 20))):
            invoice = add_invoice(database, supplier="X", tax_id="B00000017", number=number, total=1000.0, when=due - timedelta(days=30))
            invoice.direction, invoice.customer_name, invoice.customer_tax_id, invoice.due_date, invoice.paid_at = "ISSUED", "CLIENTE LENTO S.L.", "B00600063", due, paid
        pending = add_invoice(database, supplier="X", tax_id="B00000017", number="V-3", total=2000.0, when=date(2026, 9, 10))
        pending.direction, pending.customer_name, pending.customer_tax_id, pending.due_date = "ISSUED", "CLIENTE LENTO S.L.", "B00600063", date(2026, 10, 10)
        database.commit()
    collection = predict()["delayed_collections"][0]
    assert collection["date"] == "2026-10-30" and "20 días tarde" in collection["why"]


def test_liquidity_risk_says_when_why_and_what_to_do(client):
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    import_with_balance(client, [(date(2026, 9, 25), "INGRESO", "100,00", "1000,00")])
    with SessionLocal() as database:
        invoice = add_invoice(database, supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-0950", total=3000.0, when=date(2026, 9, 9))
        invoice.due_date = TODAY + timedelta(days=10)
        database.commit()
    risk = predict()["risk"]
    assert risk["level"] == "alto" and risk["days"] == 10 and risk["headline"] == "Riesgo de liquidez en 10 días"
    assert "T-0950" in risk["explanation"] and "-2.000,00 €" in risk["explanation"]
    assert any("aplazamiento" in action["label"] for action in risk["actions"])


def test_simulating_an_approval_shows_the_vat_effect_and_saves_nothing(client):
    from app.database import SessionLocal
    from app.models import Invoice
    from app.simulation import simulate

    ids = setup_month(client)
    with SessionLocal() as database:
        result = simulate(database, {"type": "approve_invoice", "invoice_id": ids["pending"]}, today=date(2026, 10, 1))
    assert Decimal(str(result["after"]["vat_result"])) - Decimal(str(result["before"]["vat_result"])) == Decimal("-42.00")
    assert any(line.startswith("Modelo 303 3T 2026") and "ahorras 42,00 €" in line for line in result["effects"])
    assert result["after"]["close_blockers"] < result["before"]["close_blockers"]
    with SessionLocal() as database:
        assert database.get(Invoice, ids["pending"]).review_status == "PENDING"  # nada guardado


def test_simulating_a_new_expense_and_validation(client):
    from app.database import SessionLocal
    from app.models import Invoice
    from app.simulation import simulate

    setup_month(client)
    with SessionLocal() as database:
        count = database.query(Invoice).count()
        result = simulate(database, {"type": "new_expense", "amount": 1210, "vat_rate": 21, "date": "2026-09-15"}, today=date(2026, 10, 1))
    assert Decimal(str(result["before"]["vat_result"])) - Decimal(str(result["after"]["vat_result"])) == Decimal("210.00")
    with SessionLocal() as database:
        assert database.query(Invoice).count() == count
    assert client.post("/api/simulate", json={"type": "approve_invoice", "invoice_id": 999999}).status_code == 422
    assert client.post("/api/simulate", json={"type": "borrar_todo"}).status_code == 422
    assert client.post("/api/simulate", json={"type": "new_expense", "amount": 100}).status_code == 200


def test_work_center_raises_the_liquidity_risk(client):
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    import_with_balance(client, [(date.today() - timedelta(days=1), "INGRESO", "100,00", "500,00")])
    with SessionLocal() as database:
        invoice = add_invoice(database, supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-0960", total=3000.0, when=date.today() - timedelta(days=5))
        invoice.due_date = date.today() + timedelta(days=7)
        database.commit()
    first = client.get("/api/work").json()["groups"][0]["items"][0]
    assert first["kind"] == "liquidity" and first["title"] == "Riesgo de liquidez en 7 días"
