"""
Banco conectado (PSD2): los movimientos llegan solos, sin descargar extractos.

El acceso a cuentas en Europa (PSD2) solo lo pueden dar entidades con licencia AISP. CapaFiscal
no lo es: trabaja a través de un agregador con licencia. El adaptador real habla la API de
GoCardless Bank Account Data (antes Nordigen), que cubre los bancos españoles:

    1. token          POST /token/new/                  (credenciales del agregador)
    2. bancos         GET  /institutions/?country=ES
    3. acuerdo        POST /agreements/enduser/         (90 días de historial y de acceso)
    4. autorización   POST /requisitions/               → enlace al banco para que el TITULAR autorice
    5. cuentas        GET  /requisitions/{id}/          → estado LN (enlazado) y cuentas
    6. movimientos    GET  /accounts/{id}/transactions/?date_from=…

Qué hace falta para que funcione de verdad: credenciales del agregador (BANK_DATA_SECRET_ID y
BANK_DATA_SECRET_KEY), salida a internet hacia su API y que el titular de la cuenta autorice el
acceso en la web de su banco. El consentimiento caduca a los 90 días y hay que renovarlo.

FolderProvider lee respuestas guardadas con el mismo formato (pruebas y demostraciones sin red).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


class BankProviderError(RuntimeError):
    pass


class ConsentExpired(BankProviderError):
    pass


def masked(iban: str | None) -> str | None:
    if not iban:
        return None
    clean = iban.replace(" ", "")
    return f"{clean[:4]} •••• {clean[-4:]}"


def to_row(item: dict[str, Any]) -> dict[str, Any] | None:
    """Un movimiento del agregador → la fila común del banco (la misma que un extracto)."""
    booked = item.get("bookingDate") or item.get("valueDate")
    amount = (item.get("transactionAmount") or {}).get("amount")
    if not booked or amount is None:
        return None
    text = item.get("remittanceInformationUnstructured") or " ".join(item.get("remittanceInformationUnstructuredArray") or [])
    party = item.get("creditorName") or item.get("debtorName")
    description = " · ".join(part for part in (text.strip() if text else "", party or "") if part) or item.get("additionalInformation") or "Movimiento"
    balance = ((item.get("balanceAfterTransaction") or {}).get("balanceAmount") or {}).get("amount")
    external = item.get("transactionId") or item.get("internalTransactionId") or f"{booked}|{amount}|{description}"
    return {"booking_date": date.fromisoformat(booked[:10]), "description": description[:500], "amount": Decimal(str(amount)),
            "balance": Decimal(str(balance)) if balance is not None else None, "external_id": str(external)}


class GoCardlessProvider:
    name = "gocardless"

    def __init__(self, base_url: str, secret_id: str, secret_key: str):
        self.base_url = base_url.rstrip("/")
        self.secret_id, self.secret_key = secret_id, secret_key
        self._token: str | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        headers = {"accept": "application/json"}
        if path != "/token/new/":
            headers["Authorization"] = f"Bearer {self.token()}"
        try:
            response = httpx.request(method, f"{self.base_url}{path}", headers=headers, timeout=30, **kwargs)
        except httpx.HTTPError as error:
            raise BankProviderError(f"No se pudo conectar con el agregador bancario: {type(error).__name__}") from error
        if response.status_code in (401, 403) and path.startswith("/accounts/"):
            raise ConsentExpired("El banco ya no da acceso: el consentimiento ha caducado o se retiró. Hay que renovarlo.")
        if response.status_code == 429:
            raise BankProviderError("El banco limita las consultas (unas 4 al día por cuenta): se reintentará más tarde.")
        if response.status_code >= 400:
            raise BankProviderError(f"El agregador respondió {response.status_code}: {response.text[:200]}")
        return response.json()

    def token(self) -> str:
        if self._token is None:
            data = self._request("POST", "/token/new/", json={"secret_id": self.secret_id, "secret_key": self.secret_key})
            self._token = data["access"]
        return self._token

    def institutions(self, country: str = "ES") -> list[dict[str, Any]]:
        return [{"id": item["id"], "name": item["name"], "logo": item.get("logo")} for item in self._request("GET", "/institutions/", params={"country": country})]

    def create_link(self, institution_id: str, reference: str, redirect: str) -> dict[str, Any]:
        agreement = self._request("POST", "/agreements/enduser/", json={
            "institution_id": institution_id, "max_historical_days": 90, "access_valid_for_days": 90,
            "access_scope": ["balances", "details", "transactions"]})
        requisition = self._request("POST", "/requisitions/", json={
            "redirect": redirect, "institution_id": institution_id, "reference": reference, "agreement": agreement["id"], "user_language": "ES"})
        return {"requisition_id": requisition["id"], "link": requisition["link"]}

    def requisition(self, requisition_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/requisitions/{requisition_id}/")
        return {"status": data.get("status"), "accounts": data.get("accounts") or []}

    def account(self, account_id: str) -> dict[str, Any]:
        details = self._request("GET", f"/accounts/{account_id}/details/").get("account") or {}
        return {"id": account_id, "iban": masked(details.get("iban")), "name": details.get("name") or details.get("product") or details.get("ownerName")}

    def transactions(self, account_id: str, date_from: date) -> list[dict[str, Any]]:
        data = self._request("GET", f"/accounts/{account_id}/transactions/", params={"date_from": date_from.isoformat()})
        return (data.get("transactions") or {}).get("booked") or []


class FolderProvider:
    """Respuestas guardadas del agregador: institutions.json, requisition.json, details_<cuenta>.json, transactions_<cuenta>.json."""

    name = "carpeta"

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _read(self, name: str) -> Any:
        path = self.folder / name
        if not path.exists():
            raise BankProviderError(f"Falta {name} en la carpeta del banco simulado.")
        return json.loads(path.read_text(encoding="utf-8"))

    def institutions(self, country: str = "ES") -> list[dict[str, Any]]:
        return self._read("institutions.json")

    def create_link(self, institution_id: str, reference: str, redirect: str) -> dict[str, Any]:
        return {"requisition_id": f"simulada-{reference[:8]}", "link": f"{redirect}&simulado=1"}

    def requisition(self, requisition_id: str) -> dict[str, Any]:
        return self._read("requisition.json")

    def account(self, account_id: str) -> dict[str, Any]:
        details = self._read(f"details_{account_id}.json").get("account") or {}
        return {"id": account_id, "iban": masked(details.get("iban")), "name": details.get("name")}

    def transactions(self, account_id: str, date_from: date) -> list[dict[str, Any]]:
        if (self.folder / "expired").exists():
            raise ConsentExpired("El banco ya no da acceso: el consentimiento ha caducado o se retiró. Hay que renovarlo.")
        return self._read(f"transactions_{account_id}.json")["transactions"]["booked"]  # la ventana de fechas la aplica el agregador


def provider() -> GoCardlessProvider | FolderProvider | None:
    from app.config import settings

    if settings.bank_data_folder:
        return FolderProvider(settings.bank_data_folder)
    if settings.bank_data_secret_id and settings.bank_data_secret_key:
        return GoCardlessProvider(settings.bank_data_url, settings.bank_data_secret_id, settings.bank_data_secret_key)
    return None


NOT_CONFIGURED = ("Conexión bancaria no configurada. Hace falta un agregador PSD2 con licencia: credenciales de GoCardless "
                  "Bank Account Data en BANK_DATA_SECRET_ID y BANK_DATA_SECRET_KEY, salida a internet hacia su API y que el "
                  "titular autorice el acceso desde su banco. Mientras, importa el extracto CSV o Excel.")
