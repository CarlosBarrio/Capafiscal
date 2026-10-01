"""El cliente REAL del agregador (GoCardless Bank Account Data) contra una API simulada a nivel HTTP.

Cubre lo que pasa con bancos de verdad: varias cuentas, token caducado, límite de consultas, banco caído,
movimientos sin transactionId, importes cero y otra divisa, autorización rechazada y renovación del
consentimiento. Datos inventados (SIMULACIÓN — NO OFICIAL).
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.agents.test_proactive import COMPANY

BASE = "https://agregador.simulado/api/v2"


def tx(transaction_id, day, amount, text, currency="EUR"):
    item = {"bookingDate": day, "transactionAmount": {"amount": amount, "currency": currency}, "remittanceInformationUnstructured": text}
    if transaction_id:
        item["transactionId"] = transaction_id
    return item


class FakeAggregator:
    def __init__(self):
        self.calls: list[str] = []
        self.tokens = 0
        self.requisition_status = "LN"
        self.expire_token_once = False
        self.account_errors: dict[str, int] = {}
        self.transactions = {
            "acc-1": [tx("t1", "2026-09-10", "-120.00", "RECIBO LUZ"),
                      tx(None, "2026-09-12", "-4.50", "CAFE"), tx(None, "2026-09-12", "-4.50", "CAFE"),  # sin id: dos cargos iguales
                      tx("t3", "2026-09-13", "0.00", "AJUSTE"), tx("t4", "2026-09-14", "-50.00", "COMPRA USA", "USD")],
            "acc-2": [tx("u1", "2026-09-11", "900.00", "TRANSFERENCIA CLIENTE")],
        }

    def __call__(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = url.removeprefix(BASE)
        self.calls.append(f"{method} {path}")
        def reply(status, body):
            return httpx.Response(status, json=body, request=httpx.Request(method, url))
        if path == "/token/new/":
            self.tokens += 1
            return reply(200, {"access": f"token-{self.tokens}"})
        if self.expire_token_once and path.startswith("/accounts/") and path.endswith("/transactions/"):
            self.expire_token_once = False
            return reply(401, {"detail": "Token is invalid or expired"})
        if path == "/institutions/":
            return reply(200, [{"id": "SANDBOXFINANCE_SFIN0000", "name": "Banco de pruebas"}])
        if path == "/agreements/enduser/":
            return reply(201, {"id": "agr-1"})
        if path == "/requisitions/":
            return reply(201, {"id": "req-1", "link": "https://banco.simulado/autorizar"})
        if path == "/requisitions/req-1/" and method == "DELETE":
            return reply(200, {"summary": "Requisition deleted"})
        if path == "/requisitions/req-1/":
            return reply(200, {"status": self.requisition_status, "accounts": ["acc-1", "acc-2"]})
        if path.endswith("/details/"):
            account = path.split("/")[2]
            return reply(200, {"account": {"iban": f"ES00000000000000000000{account[-1]}", "name": f"Cuenta {account}"}})
        if path.endswith("/transactions/"):
            account = path.split("/")[2]
            if account in self.account_errors:
                return reply(self.account_errors[account], {"detail": "error"})
            return reply(200, {"transactions": {"booked": self.transactions[account], "pending": [tx("p1", "2026-09-30", "-1.00", "PENDIENTE")]}})
        return reply(404, {"detail": "no existe"})


@pytest.fixture()
def api(monkeypatch):
    from app.config import settings

    from app.bank_connect import GoCardlessProvider

    GoCardlessProvider._cache.clear()
    fake = FakeAggregator()
    monkeypatch.setattr(settings, "bank_data_folder", None)
    monkeypatch.setattr(settings, "bank_data_url", BASE)
    monkeypatch.setattr(settings, "bank_data_secret_id", "id-simulado")
    monkeypatch.setattr(settings, "bank_data_secret_key", "clave-simulada")
    monkeypatch.setattr(httpx, "request", fake)
    return fake


def connect(client):
    client.put("/api/company", json=COMPANY)
    created = client.post("/api/bank/connections", json={"institution_id": "SANDBOXFINANCE_SFIN0000", "institution_name": "Banco de pruebas"}).json()
    assert created["link"] == "https://banco.simulado/autorizar"
    return created


def test_real_client_full_flow_with_several_accounts_and_odd_movements(client, api):
    connection = connect(client)
    assert "POST /agreements/enduser/" in api.calls and "POST /requisitions/" in api.calls
    linked = client.post(f"/api/bank/connections/{connection['id']}/confirm").json()
    assert linked["status"] == "LINKED" and [account["iban"] for account in linked["accounts"]] == ["ES00 •••• 0001", "ES00 •••• 0002"]
    # Cuenta 1: luz + dos cafés iguales sin identificador (los dos entran); el ajuste a cero y el cargo en dólares no.
    assert linked["result"] == {"imported": 4, "duplicated": 0, "auto_matched": 0, "accounts": 2, "skipped": 2}
    assert "2 sin usar" in linked["last_result"]
    descriptions = [row["description"] for row in client.get("/api/bank/reconciliation").json()["movements"]]
    assert descriptions.count("CAFE") == 2 and "PENDIENTE" not in descriptions  # solo movimientos asentados
    again = client.post(f"/api/bank/connections/{connection['id']}/sync").json()
    assert again["result"]["imported"] == 0 and again["result"]["duplicated"] == 4


def test_expired_token_is_renewed_and_one_failing_account_does_not_stop_the_others(client, api):
    connection = connect(client)
    assert api.tokens == 1  # el token se reutiliza entre peticiones
    api.expire_token_once = True
    api.account_errors["acc-2"] = 429
    linked = client.post(f"/api/bank/connections/{connection['id']}/confirm").json()
    assert api.tokens == 2  # el token caducado se renovó una vez
    assert linked["status"] == "LINKED" and linked["result"]["accounts"] == 1 and linked["result"]["imported"] == 3
    assert "limita las consultas" in linked["last_error"]
    assert any(item["kind"] == "bank_sync_error" for item in client.get("/api/work").json()["groups"][1]["items"])
    api.account_errors["acc-2"] = 503
    down = client.post(f"/api/bank/connections/{connection['id']}/sync").json()
    assert "no está disponible ahora" in down["last_error"] and down["status"] == "LINKED"


def test_rejected_authorization_and_renewal_after_consent_expires(client, api):
    connection = connect(client)
    api.requisition_status = "RJ"
    refused = client.post(f"/api/bank/connections/{connection['id']}/confirm")
    assert refused.status_code == 409 and "rechazó" in refused.json()["detail"]

    api.requisition_status = "LN"
    renewed = connect(client)
    client.post(f"/api/bank/connections/{renewed['id']}/confirm")
    api.account_errors["acc-1"] = 403
    expired = client.post(f"/api/bank/connections/{renewed['id']}/sync").json()
    assert expired["status"] == "EXPIRED"
    assert any(item["kind"] == "bank_consent" for item in client.get("/api/work").json()["groups"][0]["items"])

    # Renovar = volver a conectar el mismo banco: la caducada (y la rechazada) desaparecen, los movimientos no se duplican.
    api.account_errors.clear()
    fresh = connect(client)
    result = client.post(f"/api/bank/connections/{fresh['id']}/confirm").json()
    assert result["result"]["imported"] == 0
    statuses = [item["status"] for item in client.get("/api/bank/connections").json()["connections"]]
    assert statuses == ["LINKED"]


def test_consent_about_to_expire_is_warned_before_movements_stop(client, api):
    from datetime import datetime
    from datetime import timedelta
    from datetime import timezone

    from app.database import SessionLocal
    from app.models import BankConnection

    connection = connect(client)
    client.post(f"/api/bank/connections/{connection['id']}/confirm")
    with SessionLocal() as database:
        database.get(BankConnection, connection["id"]).consent_expires_at = datetime.now(timezone.utc) + timedelta(days=5)
        database.commit()
    warning = next(item for item in client.get("/api/work").json()["groups"][0]["items"] if item["kind"] == "bank_consent_soon")
    assert "caduca en" in warning["title"] and warning["action"]["label"] == "Renovar"


def test_payload_never_contains_secrets_in_the_api(client, api):
    connection = connect(client)
    body = json.dumps(client.get("/api/bank/connections").json())
    assert "clave-simulada" not in body and "id-simulado" not in body and "token-" not in body and connection


def test_disconnect_revokes_access_at_the_aggregator(client, api):
    connection = connect(client)
    client.post(f"/api/bank/connections/{connection['id']}/confirm")
    removed = client.delete(f"/api/bank/connections/{connection['id']}").json()
    assert removed["status"] == "REMOVED" and removed["last_error"] is None and "DELETE /requisitions/req-1/" in api.calls
    assert client.get("/api/bank/reconciliation").json()["total"] == 4  # los movimientos ya importados se quedan
