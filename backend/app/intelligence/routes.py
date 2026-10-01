"""API de Inteligencia: /api/intelligence."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from pydantic import BaseModel
from pydantic import Field

from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor

router = APIRouter(prefix="/api/intelligence", tags=["Inteligencia"])


class StatusPayload(BaseModel):
    status: str = Field(pattern="^(nueva|revisada|descartada)$")


class RefreshPayload(BaseModel):
    day: date | None = None
    back_days: int = Field(default=3, ge=1, le=14)


@router.get("")
def intelligence_overview(database: DatabaseDependency, radar: str = "juridico", include_dismissed: bool = False) -> dict[str, Any]:
    """Lo relevante que CapaFiscal ha encontrado fuera de la empresa, para un radar."""
    from app.intelligence.service import ensure_sources
    from app.intelligence.service import overview

    ensure_sources(database)
    database.commit()
    try:
        return overview(database, radar, include_dismissed=include_dismissed)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/items/{match_id}")
def intelligence_item(match_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.intelligence.service import detail
    from app.models import IntelMatch

    entry = database.get(IntelMatch, match_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Novedad no encontrada.")
    return detail(database, entry)


@router.post("/items/{match_id}/status")
def intelligence_item_status(match_id: int, payload: StatusPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.intelligence.service import card
    from app.intelligence.service import set_status
    from app.models import ExternalItem
    from app.models import IntelMatch

    entry = database.get(IntelMatch, match_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Novedad no encontrada.")
    set_status(database, entry, payload.status, normalize_actor(actor_header))
    database.commit()
    return card(database.get(ExternalItem, entry.item_id), entry)


@router.get("/profile")
def intelligence_profile(database: DatabaseDependency) -> dict[str, Any]:
    from app.intelligence.profile import LEGAL_AREAS
    from app.intelligence.profile import REGIONS
    from app.intelligence.profile import get_profile

    return {"profile": get_profile(database), "options": {"legal_areas": LEGAL_AREAS, "regions": list(REGIONS)}}


@router.put("/profile")
def intelligence_profile_update(payload: dict[str, Any], database: DatabaseDependency) -> dict[str, Any]:
    """Guarda el perfil y recalcula la relevancia con lo ya descargado (sin esperar a mañana)."""
    from app.intelligence.profile import save_profile
    from app.intelligence.service import match

    profile = save_profile(database, payload)
    result = match(database, "juridico", use_ai=False)
    database.commit()
    return {"profile": profile, "matched": result}


@router.post("/refresh")
def intelligence_refresh(payload: RefreshPayload, database: DatabaseDependency) -> dict[str, Any]:
    """Consulta ahora las fuentes (lo mismo que hace la tarea diaria) y recalcula."""
    from app.intelligence.service import match
    from app.intelligence.service import refresh_boe

    fetched = refresh_boe(database, today=payload.day, back_days=payload.back_days, force=True)
    database.commit()
    matched = match(database, "juridico", today=payload.day)
    database.commit()
    return {"fetched": fetched, "matched": matched}


@router.post("/import")
async def intelligence_import(database: DatabaseDependency, day: date = Form(...), uploaded_file: UploadFile = File(...)) -> dict[str, Any]:
    """Importa un sumario del BOE descargado a mano (JSON de la API de datos abiertos), por si no hay red hacia boe.es."""
    from app.intelligence.service import fetch_boe_day
    from app.intelligence.service import match
    from app.intelligence.sources.base import SourceUnavailable

    content = await uploaded_file.read()
    try:
        result = fetch_boe_day(database, day, payload=content)
    except SourceUnavailable as error:
        database.commit()
        raise HTTPException(status_code=422, detail=str(error)) from error
    database.commit()
    matched = match(database, "juridico", today=day)
    database.commit()
    return {"fetched": result, "matched": matched}
