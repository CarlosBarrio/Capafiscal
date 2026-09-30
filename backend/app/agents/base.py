"""Piezas comunes: contexto compartido, resultado de un paso y registro."""
from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AgentRun
from app.models import AgentStep
from app.models import Case
from app.models import CaseEvent


@dataclass
class AgentContext:
    """Lo que los agentes se pasan de uno a otro durante un recorrido."""

    database: Session
    today: date
    now: datetime
    case: Case | None = None
    text: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    trigger: str = "system"


@dataclass
class StepResult:
    summary: str
    output: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    engine: str = "reglas"
    status: str = "OK"


class Agent:
    """Un empleado digital con un rol concreto."""

    code = "agent"
    name = "Agente"
    role = ""
    icon = "sparkles"

    def run(self, ctx: AgentContext) -> StepResult:  # pragma: no cover - interfaz
        raise NotImplementedError


def evidence(kind: str, label: str, **data: Any) -> dict[str, Any]:
    return {"kind": kind, "label": label, **{key: value for key, value in data.items() if value is not None}}


def run_step(ctx: AgentContext, run: AgentRun, agent: Agent, position: int) -> StepResult:
    """Ejecuta un agente, mide su tiempo y deja la traza (aunque falle)."""
    started = time.perf_counter()
    try:
        result = agent.run(ctx)
    except Exception as error:  # un agente que falla no tumba el recorrido
        result = StepResult(summary=f"No se pudo completar: {error}", status="ERROR")

    duration = int((time.perf_counter() - started) * 1000)
    step = AgentStep(
        run_id=run.id,
        position=position,
        agent=agent.code,
        status=result.status,
        summary=result.summary,
        output=jsonable(result.output),
        evidence=jsonable(result.evidence),
        engine=result.engine,
        duration_ms=duration,
    )
    ctx.database.add(step)

    if ctx.case is not None and result.status != "SKIPPED":
        ctx.database.add(
            CaseEvent(
                case_id=ctx.case.id,
                kind="agent",
                actor=agent.code,
                title=f"{agent.name} · {result.summary}"[:255],
                detail=None,
                data={"run_id": run.id, "status": result.status, "engine": result.engine, "duration_ms": duration},
            )
        )
    ctx.database.flush()
    return result


def eur(value: Any) -> str:
    """Importe en formato español: 1.234,56 €."""
    text = f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def jsonable(value: Any) -> Any:
    """Convierte fechas y decimales para guardarlos en columnas JSON."""
    from decimal import Decimal

    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
