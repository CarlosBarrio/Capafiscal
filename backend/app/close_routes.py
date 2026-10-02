"""API del cierre mensual: /api/close."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field

from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor

router = APIRouter(prefix="/api/close", tags=["Cierre"])


class ClosePayload(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.get("")
def close_state(database: DatabaseDependency, period: str | None = None) -> dict[str, Any]:
    """Cómo va el cierre de un mes (por defecto, el anterior). Solo lee."""
    from app import closing

    period = period or closing.default_period()
    try:
        return {**closing.state(database, period), "history": closing.history(database)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{period}/run")
def close_run(period: str, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """CapaFiscal hace su parte: concilia lo seguro, pasa el Detector y comprueba el mes."""
    from app import closing

    try:
        result = closing.run_close(database, period, actor=normalize_actor(actor_header) or "persona")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    database.commit()
    return result


@router.post("/{period}/close")
def close_period(period: str, payload: ClosePayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app import closing

    try:
        result = closing.close(database, period, actor=normalize_actor(actor_header) or "persona", note=payload.note)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return result


@router.post("/{period}/reopen")
def close_reopen(period: str, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app import closing

    try:
        result = closing.reopen(database, period, actor=normalize_actor(actor_header) or "persona")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return result


@router.get("/{period}/report")
def close_report(period: str, database: DatabaseDependency):
    """Informe de cierre en PDF (provisional si el mes sigue abierto). Solo lee."""
    from fastapi import Response

    from app import closing

    try:
        content = closing.report_pdf(database, period)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="informe_cierre_{period}.pdf"'})
