"""Memoria financiera y Detector de cambios de comportamiento."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from tests.workflows.test_scenarios import add_invoice

COMPANY = {"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es"}
TODAY = date(2026, 10, 5)


def issued(database, *, customer: str, tax_id: str, number: str, total: float, when: date, paid: date | None = None):
    invoice = add_invoice(database, supplier="Taller Barrio S.L.", tax_id="B12345674", number=number, total=total, when=when)
    invoice.direction, invoice.customer_name, invoice.customer_tax_id, invoice.paid_at = "ISSUED", customer, tax_id, paid
    return invoice


def test_profile_learns_how_a_supplier_usually_behaves(client):
    from app.database import SessionLocal
    from app.financial_memory import get_profile
    from app.financial_memory import refresh_profiles

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month, total in zip(range(3, 10), (400, 410, 395, 405, 430, 380, 400)):
            invoice = add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{month}", total=total, when=date(2026, month, 5))
            invoice.paid_at, invoice.payment_method = date(2026, month, 20), "TRANSFERENCIA"
        refresh_profiles(database, today=TODAY)
        database.commit()
        profile = get_profile(database, "B11111110")
    assert profile["frequency"]["label"] == "mensual" and profile["frequency"]["next_expected"].startswith("2026-10")
    assert 380 <= profile["amount"]["low"] <= profile["amount"]["median"] <= profile["amount"]["high"] <= 430
    assert profile["vat"]["usual_rate"] == 21 and profile["payment"]["method"] == "TRANSFERENCIA" and profile["payment"]["days_to_pay"] == 15
    assert "mensual" in profile["summary"] and "transferencia" in profile["summary"]
    listed = client.get("/api/memory/profiles", params={"party": "supplier"}).json()
    assert listed[0]["key"] == "B11111110"


def test_abnormal_spending_names_who_explains_it(client):
    from app.agents.detector import run_anomaly_scan
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month in range(3, 10):
            add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{month}", total=400, when=date(2026, month, 5))
            add_invoice(database, supplier="SUMINISTROS ESTE S.L.", tax_id="B44444440", number=f"SE-{month}", total=600, when=date(2026, month, 8))
        add_invoice(database, supplier="SUMINISTROS ESTE S.L.", tax_id="B44444440", number="SE-9b", total=2400, when=date(2026, 9, 22))
        database.commit()
        run_anomaly_scan(database, today=TODAY)
        database.commit()
    cases = [item for item in client.get("/api/cases", params={"view": "anomalies"}).json() if item["procedure"] == "GASTO_ANORMAL"]
    assert len(cases) == 1
    detail = client.get(f"/api/cases/{cases[0]['id']}").json()
    assert "SUMINISTROS ESTE" in detail["summary"] and "septiembre" in detail["title"]


def test_revenue_drop_and_customer_that_stops_paying(client):
    from app.agents.detector import run_anomaly_scan
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month in range(3, 9):  # paga siempre a los 20 días
            issued(database, customer="CLIENTE FIEL S.A.", tax_id="A55555550", number=f"V-{month}", total=3000, when=date(2026, month, 1), paid=date(2026, month, 21))
        issued(database, customer="CLIENTE FIEL S.A.", tax_id="A55555550", number="V-7b", total=900, when=date(2026, 7, 15))
        issued(database, customer="CLIENTE FIEL S.A.", tax_id="A55555550", number="V-8b", total=900, when=date(2026, 8, 1))
        issued(database, customer="CLIENTE FIEL S.A.", tax_id="A55555550", number="V-9", total=500, when=date(2026, 9, 1))
        database.commit()
        run_anomaly_scan(database, today=TODAY)
        database.commit()
    procedures = {item["procedure"]: item for item in client.get("/api/cases", params={"view": "anomalies"}).json()}
    assert "CAIDA_FACTURACION" in procedures
    assert "CLIENTE_DEJA_DE_PAGAR" in procedures
    detail = client.get(f"/api/cases/{procedures['CLIENTE_DEJA_DE_PAGAR']['id']}").json()
    assert "20 días" in detail["summary"] and "Lo habitual" in detail["summary"]
