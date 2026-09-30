"""
Orquestador: recorre un proceso completo encadenando agentes.

    Vigilante → Expedientes → Fiscal → Memoria → Gestor → Perseguidor → Director

Cada recorrido queda registrado (AgentRun + AgentStep) y el expediente
recibe la línea de tiempo. El humano recibe un único aviso con todo trabajado
y un «Revisar y aprobar».
"""
from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import AgentContext
from app.agents.base import run_step
from app.agents.director import Director
from app.agents.expedientes import ClasificadorExpedientes
from app.agents.fiscal import AgenteFiscal
from app.agents.gestor import GestorIncidencias
from app.agents.memoria import AgenteMemoria
from app.agents.perseguidor import Perseguidor
from app.agents.vigilante import Vigilante
from app.invoice_service import add_audit_event
from app.models import AgentRun
from app.models import Case
from app.models import CaseEvent
from app.models import FiscalNotification

NOTIFICATION_PIPELINE = (
    Vigilante(),
    ClasificadorExpedientes(),
    AgenteFiscal(),
    AgenteMemoria(),
    GestorIncidencias(),
    Perseguidor(),
    Director(),
)
NEEDS_CASE = {"fiscal", "memoria", "gestor", "perseguidor", "director"}


def process_notification(
    database: Session,
    notification_id: int,
    *,
    trigger: str = "system",
    today: date | None = None,
) -> Case | None:
    notification = database.get(FiscalNotification, notification_id)
    if notification is None:
        return None

    now = datetime.now(timezone.utc)
    ctx = AgentContext(database=database, today=today or date.today(), now=now, trigger=trigger)
    ctx.facts["notification"] = notification
    ctx.case = database.scalar(select(Case).where(Case.notification_id == notification.id))

    run = AgentRun(pipeline="notification", trigger=trigger, case_id=ctx.case.id if ctx.case else None, status="RUNNING")
    database.add(run)
    database.flush()

    errors = 0
    for position, agent in enumerate(NOTIFICATION_PIPELINE, start=1):
        if agent.code in NEEDS_CASE and ctx.case is None:
            continue
        result = run_step(ctx, run, agent, position)
        if result.status == "ERROR":
            errors += 1
        if run.case_id is None and ctx.case is not None:
            run.case_id = ctx.case.id

    case = ctx.case
    run.status = "OK" if not errors else "PARTIAL"
    run.finished_at = datetime.now(timezone.utc)
    run.summary = case.headline if case else "Sin expediente"

    if case is not None:
        facts = {key: value for key, value in ctx.facts.items() if key not in {"notification", "llm_extraction"}}
        from app.agents.base import jsonable

        case.facts = jsonable(facts)
        elapsed = int((run.finished_at - run.started_at.replace(tzinfo=run.started_at.tzinfo or timezone.utc)).total_seconds() * 1000)
        database.add(
            CaseEvent(
                case_id=case.id,
                kind="system",
                actor="orquestador",
                title=f"Recorrido completo: {len(run.steps)} agentes en {elapsed} ms" + (f" ({errors} con incidencias)" if errors else ""),
                data={"run_id": run.id},
            )
        )
        if notification.status == "PENDING":
            notification.status = "IN_PROGRESS"
        add_audit_event(
            database,
            action="agents.notification_processed",
            entity_type="case",
            entity_id=case.id,
            actor="agent",
            event_data={"run_id": run.id, "code": case.code, "priority": case.priority, "status": case.status},
        )

    database.flush()
    return case


def process_pending(database: Session, *, trigger: str = "schedule", today: date | None = None) -> list[Case]:
    """Notificaciones abiertas que aún no tienen expediente (backlog o importadas)."""
    handled = select(Case.notification_id).where(Case.notification_id.is_not(None))
    pending = database.scalars(
        select(FiscalNotification).where(
            FiscalNotification.status.in_(["PENDING", "IN_PROGRESS"]),
            FiscalNotification.id.notin_(handled),
        )
    ).all()
    cases = []
    for notification in pending:
        case = process_notification(database, notification.id, trigger=trigger, today=today)
        if case:
            cases.append(case)
    return cases


def serialize_run(run: AgentRun) -> dict[str, Any]:
    from app.agents.registry import AGENTS_BY_CODE

    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "trigger": run.trigger,
        "case_id": run.case_id,
        "status": run.status,
        "summary": run.summary,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "steps": [
            {
                "position": step.position,
                "agent": step.agent,
                "agent_name": AGENTS_BY_CODE.get(step.agent, {}).get("name", step.agent),
                "icon": AGENTS_BY_CODE.get(step.agent, {}).get("icon", "sparkles"),
                "status": step.status,
                "summary": step.summary,
                "engine": step.engine,
                "duration_ms": step.duration_ms,
                "evidence": step.evidence,
                "output": step.output,
                "created_at": step.created_at.isoformat() if step.created_at else None,
            }
            for step in run.steps
        ],
    }
