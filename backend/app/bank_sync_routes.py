"""API del banco conectado (PSD2): /api/bank/connections."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic import Field

from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor

router = APIRouter(prefix="/api/bank", tags=["Banco conectado"])


class ConnectPayload(BaseModel):
    institution_id: str = Field(min_length=2, max_length=100)
    institution_name: str | None = Field(default=None, max_length=255)


def connection_or_404(database, connection_id: int):
    from app.models import BankConnection

    connection = database.get(BankConnection, connection_id)
    if connection is None or connection.status == "REMOVED":
        raise HTTPException(status_code=404, detail="Conexión no encontrada.")
    return connection


@router.get("/connections")
def connections(database: DatabaseDependency) -> dict[str, Any]:
    """Bancos conectados y si la conexión está configurada. Solo lee."""
    from app.bank_sync import overview

    return overview(database)


@router.get("/institutions")
def institutions(country: str = Query(default="ES", pattern="^[A-Z]{2}$")) -> list[dict[str, Any]]:
    """Bancos disponibles en el agregador."""
    from app.bank_connect import BankProviderError
    from app.bank_sync import BankSyncError
    from app.bank_sync import require_provider

    try:
        return require_provider().institutions(country)
    except (BankSyncError, BankProviderError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/connections", status_code=201)
def connect(payload: ConnectPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """Empieza la conexión: devuelve el enlace a la web del banco donde el titular autoriza el acceso."""
    from app.bank_sync import BankSyncError
    from app.bank_sync import serialize
    from app.bank_sync import start

    try:
        connection = start(database, institution_id=payload.institution_id, institution_name=payload.institution_name, actor=normalize_actor(actor_header))
    except BankSyncError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return serialize(connection)


@router.get("/connections/callback", include_in_schema=False)
def callback(ref: str = Query(min_length=8, max_length=64, pattern="^[a-f0-9]+$")) -> RedirectResponse:
    """El banco devuelve aquí al titular. No escribe: la pantalla confirma la conexión con un POST."""
    return RedirectResponse(url=f"/?banco={ref}#negocio", status_code=303)


@router.post("/connections/by-reference/{reference}/confirm")
def confirm_by_reference(reference: str, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import BankConnection

    connection = database.scalar(select(BankConnection).where(BankConnection.reference == reference))
    if connection is None:
        raise HTTPException(status_code=404, detail="Conexión no encontrada.")
    return confirm_connection(connection.id, database, actor_header)


@router.post("/connections/{connection_id}/confirm")
def confirm_connection(connection_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """Comprueba que el titular autorizó, guarda las cuentas y trae los movimientos."""
    from app.bank_sync import BankSyncError
    from app.bank_sync import confirm

    connection = connection_or_404(database, connection_id)
    try:
        result = confirm(database, connection, actor=normalize_actor(actor_header))
    except BankSyncError as error:
        database.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return result


@router.post("/connections/{connection_id}/sync")
def sync_connection(connection_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.bank_sync import BankSyncError
    from app.bank_sync import sync

    connection = connection_or_404(database, connection_id)
    try:
        result = sync(database, connection, actor=normalize_actor(actor_header))
    except BankSyncError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return result


@router.delete("/connections/{connection_id}")
def disconnect(connection_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.bank_sync import remove

    result = remove(database, connection_or_404(database, connection_id), actor=normalize_actor(actor_header))
    database.commit()
    return result
