"""
Piezas comunes de la plataforma de agentes.

  Event       lo que ha ocurrido (notificación, factura, plazo…): la entrada.
  AgentContext lo que los agentes se pasan durante un recorrido.
  Agent       un empleado digital con un contrato explícito: qué consume
              (``consumes``), qué produce (``produces``) y para qué tipos de
              evento sirve (``handles``). Así se reutiliza en otros procesos.
  StepResult  lo que devuelve cada paso: resumen, datos, evidencias,
              hallazgos (Finding) y señales para encadenar agentes.
  Finding     el hallazgo con evidencia, común a todos los agentes.
"""
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


EVENT_KINDS = {
    "notification": "Notificación administrativa",
    "invoice": "Factura recibida",
    "deadline": "Plazo programado",
}


@dataclass
class Event:
    """Ha ocurrido algo. Toda entrada al sistema pasa por aquí."""

    kind: str  # notification | invoice | deadline
    source: str = "system"  # upload, dehu, email, schedule, manual…
    ref_id: int | None = None  # id de la notificación, factura…
    payload: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return EVENT_KINDS.get(self.kind, self.kind)


RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
RISK_LABELS = {"low": "bajo", "medium": "medio", "high": "alto"}


@dataclass
class Finding:
    """Un hallazgo con evidencia. Misma forma venga del agente que venga:
    qué se detectó, por qué, con qué datos, riesgo, confianza, de qué
    documento sale, cuándo y qué debería pasar después."""

    agente: str
    tipo: str
    resultado: str
    por_que: str
    riesgo: str = "low"
    confianza: float = 0.8
    datos: dict[str, Any] = field(default_factory=dict)
    evidencia: list[dict[str, Any]] = field(default_factory=list)
    documento_origen: int | None = None
    fecha: str | None = None
    siguiente: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(
            {
                "agente": self.agente,
                "tipo": self.tipo,
                "resultado": self.resultado,
                "por_que": self.por_que,
                "riesgo": self.riesgo,
                "riesgo_label": RISK_LABELS.get(self.riesgo, self.riesgo),
                "confianza": round(self.confianza, 2),
                "datos": self.datos,
                "evidencia": self.evidencia,
                "documento_origen": self.documento_origen,
                "fecha": self.fecha,
                "siguiente": self.siguiente,
            }
        )


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
    event: Event | None = None
    route: str | None = None
    findings: list[Finding] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    summary: str
    output: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    engine: str = "reglas"
    status: str = "OK"
    findings: list[Finding] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Un empleado digital con un rol concreto y un contrato de entrada/salida."""

    code = "agent"
    name = "Agente"
    role = ""
    icon = "sparkles"
    handles: tuple[str, ...] = ("notification",)
    needs_case = True
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()

    @classmethod
    def contract(cls) -> dict[str, Any]:
        return {
            "code": cls.code,
            "handles": list(cls.handles),
            "needs_case": cls.needs_case,
            "input": list(cls.consumes),
            "output": list(cls.produces),
        }

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
    output = dict(result.output)
    if result.findings:
        output["findings"] = [item.to_dict() for item in result.findings]
        ctx.findings.extend(result.findings)
    if result.signals:
        output["signals"] = result.signals
        ctx.signals.update(result.signals)
    step = AgentStep(
        run_id=run.id,
        position=position,
        agent=agent.code,
        status=result.status,
        summary=result.summary,
        output=jsonable(output),
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
