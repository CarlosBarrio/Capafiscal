"""Conectar el banco, confirmar la autorización del titular y sincronizar movimientos hacia la entrada común."""
from __future__ import annotations

import secrets
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bank_connect import NOT_CONFIGURED
from app.bank_connect import BankProviderError
from app.bank_connect import ConsentExpired
from app.bank_connect import provider
from app.bank_connect import to_row
from app.models import BankConnection

CONSENT_DAYS = 90
OVERLAP_DAYS = 3  # se vuelve a pedir un poco hacia atrás: el banco a veces asienta tarde (los repetidos no entran)


class BankSyncError(ValueError):
    pass


def serialize(connection: BankConnection) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {"id": connection.id, "provider": connection.provider, "institution_id": connection.institution_id,
            "institution_name": connection.institution_name, "status": connection.status, "accounts": connection.accounts or [],
            "link": connection.link if connection.status == "PENDING" else None, "consent_expires_at": iso(connection.consent_expires_at),
            "last_sync_at": iso(connection.last_sync_at), "last_result": connection.last_result, "last_error": connection.last_error,
            "created_at": iso(connection.created_at)}


def overview(database: Session) -> dict[str, Any]:
    """Estado de la conexión bancaria. Solo lee."""
    source = provider()
    connections = database.scalars(select(BankConnection).where(BankConnection.status != "REMOVED").order_by(BankConnection.id)).all()
    return {"configured": source is not None, "provider": source.name if source else None,
            "message": None if source else NOT_CONFIGURED, "connections": [serialize(item) for item in connections]}


def require_provider():
    source = provider()
    if source is None:
        raise BankSyncError(NOT_CONFIGURED)
    return source


def start(database: Session, *, institution_id: str, institution_name: str | None, actor: str) -> BankConnection:
    """Prepara la autorización: devuelve el enlace a la web del banco donde el titular da su permiso."""
    from app.config import settings

    source = require_provider()
    reference = secrets.token_hex(16)
    redirect = f"{settings.public_base_url.rstrip('/')}/api/bank/connections/callback?ref={reference}"
    try:
        created = source.create_link(institution_id, reference, redirect)
    except BankProviderError as error:
        raise BankSyncError(str(error)) from error
    connection = BankConnection(provider=source.name, institution_id=institution_id, institution_name=institution_name, reference=reference,
                                requisition_id=created["requisition_id"], link=created["link"], status="PENDING", accounts=[], created_by=actor)
    database.add(connection)
    database.flush()
    return connection


def confirm(database: Session, connection: BankConnection, *, actor: str) -> dict[str, Any]:
    """Tras autorizar en el banco: comprueba el permiso, guarda las cuentas y trae los movimientos."""
    from app.invoice_service import add_audit_event

    source = require_provider()
    try:
        state = source.requisition(connection.requisition_id)
        if state["status"] != "LN":
            raise BankSyncError("El banco aún no ha confirmado la autorización: termina el proceso en la web del banco y vuelve a intentarlo.")
        connection.accounts = [source.account(account_id) for account_id in state["accounts"]]
    except BankProviderError as error:
        raise BankSyncError(str(error)) from error
    connection.status, connection.last_error = "LINKED", None
    connection.consent_expires_at = datetime.now(timezone.utc) + timedelta(days=CONSENT_DAYS)
    add_audit_event(database, action="bank.connected", entity_type="bank_connection", entity_id=connection.id, actor=actor,
                    event_data={"institution": connection.institution_name or connection.institution_id, "accounts": len(connection.accounts)})
    database.flush()
    return sync(database, connection, actor=actor)


def sync(database: Session, connection: BankConnection, *, actor: str = "banco-conectado", today: date | None = None) -> dict[str, Any]:
    """Trae los movimientos nuevos de cada cuenta y los pasa por la entrada común (sin duplicar, conciliando)."""
    from app.bank_service import CONNECTED_SOURCE
    from app.bank_service import store_rows

    if connection.status != "LINKED":
        raise BankSyncError("Ese banco no está conectado.")
    source = require_provider()
    today = today or date.today()
    since = (connection.last_sync_at.date() - timedelta(days=OVERLAP_DAYS)) if connection.last_sync_at else today - timedelta(days=CONSENT_DAYS)
    totals = {"imported": 0, "duplicated": 0, "auto_matched": 0, "accounts": 0}
    try:
        for account in connection.accounts or []:
            rows = [row for row in (to_row(item) for item in source.transactions(account["id"], since)) if row]
            label = account.get("iban") or account.get("name") or connection.institution_name or "Banco conectado"
            result = store_rows(database, rows, source=f"{CONNECTED_SOURCE} · {connection.institution_name or connection.institution_id} · {label}",
                                account_label=label, actor=actor, external=True)
            totals["imported"] += result["imported"]
            totals["duplicated"] += result["duplicated"]
            totals["auto_matched"] += result["auto_matched"]
            totals["accounts"] += 1
    except ConsentExpired as error:
        connection.status, connection.last_error = "EXPIRED", str(error)
        database.flush()
        return {**serialize(connection), "result": totals}
    except BankProviderError as error:
        connection.last_error = str(error)
        database.flush()
        return {**serialize(connection), "result": totals}
    connection.last_sync_at, connection.last_error = datetime.now(timezone.utc), None
    connection.last_result = (f"{totals['imported']} movimiento(s) nuevos, {totals['duplicated']} ya estaban"
                              f" y {totals['auto_matched']} conciliado(s) solos")
    database.flush()
    return {**serialize(connection), "result": totals}


def sync_all(database: Session, *, today: date | None = None) -> dict[str, Any]:
    connections = database.scalars(select(BankConnection).where(BankConnection.status == "LINKED")).all()
    results = [sync(database, connection, today=today) for connection in connections]
    return {"connections": len(connections), "imported": sum(item["result"]["imported"] for item in results),
            "auto_matched": sum(item["result"]["auto_matched"] for item in results),
            "errors": [item["last_error"] for item in results if item["last_error"]]}


def remove(database: Session, connection: BankConnection, *, actor: str) -> dict[str, Any]:
    from app.invoice_service import add_audit_event

    connection.status, connection.link = "REMOVED", None
    add_audit_event(database, action="bank.disconnected", entity_type="bank_connection", entity_id=connection.id, actor=actor, event_data={})
    database.flush()
    return serialize(connection)
