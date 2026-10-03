"""Búsqueda universal (Ctrl+K): factura, NIF, tercero, expediente, movimiento, «303 septiembre», «cierre septiembre»."""
from __future__ import annotations

from tests.agents.test_closing import setup_month


def find(client, query):
    return client.get("/api/search", params={"q": query}).json()["results"]


def test_finds_any_business_object_with_its_action(client):
    ids = setup_month(client)
    invoice = next(item for item in find(client, "LS-0903") if item["type"] == "Factura")
    assert invoice["action"]["document_id"] and "por revisar" in invoice["subtitle"]
    supplier = next(item for item in find(client, "B00000017") if item["type"] == "Proveedor")
    assert supplier["title"] == "ACME SUMINISTROS S.L." and supplier["action"]["tab"] == "proveedores"
    assert any(item["type"] == "Proveedor" for item in find(client, "acme"))
    for amount in ("1210", "1.210,00", "1210,00 €"):
        assert any(item["type"] == "Movimiento" and "FAC-0901" in item["title"] for item in find(client, amount)), amount
    fiscal = next(item for item in find(client, "303 septiembre 2026") if item["type"] == "Fiscal")
    assert fiscal["title"] == "Modelo 303 · 3T 2026" and fiscal["action"]["tab"] == "impuestos"
    close = next(item for item in find(client, "cierre septiembre 2026") if item["type"] == "Cierre")
    assert close["action"] == {"tab": "cierre", "period": "2026-09"}
    assert ids


def test_finds_cases_and_never_writes(client):
    from app.database import SessionLocal
    from app.models import Case

    setup_month(client)
    client.post("/api/agents/anomalies/scan", json={})
    with SessionLocal() as database:
        case = database.query(Case).first()
        code, count = case.code, database.query(Case).count()
    assert any(item["type"] == "Expediente" and item["action"]["case_id"] for item in find(client, code))
    assert find(client, "x") == []
    with SessionLocal() as database:
        assert database.query(Case).count() == count
