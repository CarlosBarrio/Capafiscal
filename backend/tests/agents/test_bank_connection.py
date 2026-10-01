"""Banco conectado (PSD2): autorizar, traer movimientos sin duplicar el extracto, conciliar, caducidad del consentimiento.

El agregador se simula con respuestas guardadas en su formato (tests/fixtures/bank, SIMULACIÓN — NO OFICIAL).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.agents.test_closing import setup_month

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bank"


@pytest.fixture()
def bank(monkeypatch, tmp_path):
    from app.config import settings

    folder = tmp_path / "banco"
    shutil.copytree(FIXTURES, folder)
    monkeypatch.setattr(settings, "bank_data_folder", folder)
    return folder


def connect(client):
    created = client.post("/api/bank/connections", json={"institution_id": "BANCO_SIMULADO_ES", "institution_name": "Banco Simulado"})
    assert created.status_code == 201, created.text
    return created.json()


def test_without_an_aggregator_it_says_what_is_missing(client):
    overview = client.get("/api/bank/connections").json()
    assert overview["configured"] is False and "BANK_DATA_SECRET_ID" in overview["message"] and "titular" in overview["message"]
    refused = client.post("/api/bank/connections", json={"institution_id": "X1"})
    assert refused.status_code == 409 and "extracto" in refused.json()["detail"]


def test_connect_authorize_and_bring_movements_without_duplicating_the_statement(client, bank):
    setup_month(client)  # el extracto CSV ya trae el pago de 1.210 € y el de 363 €
    assert [item["id"] for item in client.get("/api/bank/institutions").json()] == ["BANCO_SIMULADO_ES", "CAJA_FICTICIA_ES"]
    pending = connect(client)
    assert pending["status"] == "PENDING" and pending["link"]
    assert any(item["kind"] == "bank_authorize" for item in client.get("/api/work").json()["groups"][1]["items"])

    callback = client.get("/api/bank/connections/callback", params={"ref": "a" * 32}, follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"].startswith("/?banco=")

    linked = client.post(f"/api/bank/connections/{pending['id']}/confirm").json()
    assert linked["status"] == "LINKED" and linked["accounts"][0]["iban"] == "ES00 •••• 0001"
    assert linked["result"] == {"imported": 2, "duplicated": 2, "auto_matched": 1, "accounts": 1}  # la comisión se justifica sola
    descriptions = [row["description"] for row in client.get("/api/bank/reconciliation").json()["movements"]]
    assert sum("1210" in text or "FAC-0901" in text for text in descriptions) == 1
    assert "RECIBO SEGURO LOCAL POLIZA 0000 · ASEGURADORA FICTICIA S.A." in descriptions

    again = client.post(f"/api/bank/connections/{pending['id']}/sync").json()
    assert again["result"]["imported"] == 0 and "0 movimiento(s) nuevos" in again["last_result"]
    work = client.get("/api/work").json()
    assert any(item["kind"] == "bank_sync" for item in work["groups"][2]["items"])


def test_expired_consent_is_a_decision_for_a_person(client, bank):
    setup_month(client)
    connection = connect(client)
    client.post(f"/api/bank/connections/{connection['id']}/confirm")
    (bank / "expired").touch()
    expired = client.post(f"/api/bank/connections/{connection['id']}/sync").json()
    assert expired["status"] == "EXPIRED" and "renovarlo" in expired["last_error"]
    renew = next(item for item in client.get("/api/work").json()["groups"][0]["items"] if item["kind"] == "bank_consent")
    assert renew["title"] == "Renueva el acceso a Banco Simulado"


def test_scheduled_sync_and_disconnect(client, bank):
    from app.automation_service import run_automation
    from app.database import SessionLocal

    setup_month(client)
    connection = connect(client)
    client.post(f"/api/bank/connections/{connection['id']}/confirm")
    with SessionLocal() as database:
        run = run_automation(database, "BANK_SYNC", trigger="MANUAL")
        database.commit()
        assert run.summary == "0 movimiento(s) nuevos del banco, 0 conciliado(s) solos."  # ya estaba todo
    assert client.delete(f"/api/bank/connections/{connection['id']}").json()["status"] == "REMOVED"
    assert client.get("/api/bank/connections").json()["connections"] == []
    assert client.post(f"/api/bank/connections/{connection['id']}/sync").status_code == 404
