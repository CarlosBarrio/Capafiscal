"""API de tesorería predictiva y simulaciones: /api/treasury, /api/simulate."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field

from app.deps import DatabaseDependency

router = APIRouter(prefix="/api", tags=["Tesorería"])


class Scenario(BaseModel):
    type: str = Field(pattern="^(approve_invoice|pay_now|postpone_payment|collect_now|dismiss_anomaly|new_expense)$")
    invoice_id: int | None = None
    case_id: int | None = None
    days: int | None = Field(default=None, ge=1, le=365)
    amount: float | None = Field(default=None, gt=0, le=10_000_000)
    vat_rate: float | None = Field(default=None, ge=0, le=21)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    label: str | None = Field(default=None, max_length=120)


@router.get("/treasury")
def treasury(database: DatabaseDependency, horizon: int = Query(default=60, ge=7, le=180)) -> dict[str, Any]:
    """Caja prevista con el comportamiento real (retrasos, cargos habituales) y el riesgo de liquidez explicado. Solo lee."""
    from app.treasury import predict

    return predict(database, horizon_days=horizon)


@router.post("/simulate")
def simulate(payload: Scenario, database: DatabaseDependency) -> dict[str, Any]:
    """¿Qué cambia si…? Calcula el efecto en IVA, caja y cierre sin guardar nada."""
    from app.simulation import SimulationError
    from app.simulation import simulate as run

    try:
        return run(database, payload.model_dump(exclude_none=True))
    except SimulationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
