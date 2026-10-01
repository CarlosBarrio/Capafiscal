"""
Entrada común: todo lo que llega al sistema pasa por aquí.

    FUENTE (subida, correo, DEHú, API, calendario…)
        → ingest(source, external_id, kind, payload)
            · idempotencia: (source, external_id) solo se procesa una vez
            · estado: RECEIVED → PROCESSING → COMPLETED | NEEDS_HUMAN | FAILED
        → orquestador (Vigilante → Expedientes → ruta → agentes → Director)

Si un agente falla, el evento queda en NEEDS_HUMAN con el agente y el
motivo; el expediente lo muestra y una persona puede reanudarlo. Si falla
todo el recorrido, queda en FAILED y se puede reintentar sin perder nada.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentStep
from app.models import Case
from app.models import CaseEvent
from app.models import IngestedEvent

logger = logging.getLogger(__name__)

KINDS = {"notification", "invoice", "deadline", "document"}
STATUS_LABELS = {
    "RECEIVED": "Recibido",
    "PROCESSING": "Procesando",
    "COMPLETED": "Completado",
    "NEEDS_HUMAN": "Necesita a una persona",
    "FAILED": "Falló",
}
SETTLED = {"COMPLETED", "NEEDS_HUMAN", "PROCESSING"}


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def dispatch(database: Session, event: IngestedEvent, holder: dict[str, Any], *, trigger: str, today: date | None) -> Case | None:
    """Convierte el evento registrado en un recorrido del orquestador."""
    from app.agents.base import Event
    from app.agents.orchestrator import process_deadline
    from app.agents.orchestrator import process_event
    from app.agents.orchestrator import process_invoice
    from app.agents.orchestrator import process_notification
    from app.models import FiscalNotification
    from app.models import Invoice

    payload = dict(event.payload or {})
    if event.kind == "notification":
        notification_id = payload.get("notification_id")
        if not notification_id:
            from app.notification_service import create_notification

            notification = create_notification(database, document=None, data=payload.get("notification") or {}, actor=event.source)
            database.flush()
            notification_id = notification.id
            event.payload = {**payload, "notification_id": notification_id}
        return process_event(database, Event("notification", source=event.source, ref_id=notification_id), trigger=trigger, today=today, holder=holder)

    if event.kind == "invoice":
        return process_invoice(database, int(payload["invoice_id"]), trigger=trigger, today=today, holder=holder, source=event.source)

    if event.kind == "deadline":
        return process_deadline(
            database, model=payload["model"], year=int(payload["year"]), quarter=int(payload["quarter"]),
            due=date.fromisoformat(str(payload["due"])), trigger=trigger, today=today, holder=holder,
        )

    if event.kind == "document":
        document_id = int(payload["document_id"])
        notification = database.scalar(select(FiscalNotification).where(FiscalNotification.document_id == document_id))
        if notification is not None:
            return process_notification(database, notification.id, trigger=trigger, today=today, holder=holder)
        invoice = database.scalar(select(Invoice).where(Invoice.document_id == document_id))
        if invoice is not None and invoice.direction != "ISSUED":
            return process_invoice(database, invoice.id, trigger=trigger, today=today, holder=holder, source=event.source)
        return None  # documento sin trabajo para los agentes (p. ej. factura emitida)

    raise ValueError(f"Tipo de evento desconocido: {event.kind}")


def ingest(
    database: Session,
    *,
    source: str,
    external_id: str,
    kind: str,
    payload: dict[str, Any],
    force: bool = False,
    trigger: str | None = None,
    today: date | None = None,
) -> tuple[IngestedEvent, bool, Case | None]:
    """Registra y procesa un evento. Devuelve (evento, ¿era un duplicado?, expediente)."""
    if kind not in KINDS:
        raise ValueError(f"Tipo de evento desconocido: {kind}")
    digest = payload_hash(payload)
    event = database.scalar(select(IngestedEvent).where(IngestedEvent.source == source, IngestedEvent.external_id == external_id))

    if event is not None:
        if event.status in SETTLED and event.payload_hash == digest and not force:
            event.duplicates += 1
            database.flush()
            return event, True, database.get(Case, event.case_id) if event.case_id else None
        # Contenido distinto (actualización) o intento anterior fallido: se vuelve a procesar.
        event.payload = {**(event.payload or {}), **payload}
        event.payload_hash = digest
    else:
        event = IngestedEvent(source=source, external_id=external_id[:255], kind=kind, status="RECEIVED", payload=payload, payload_hash=digest)
        database.add(event)
        database.flush()

    case = process(database, event, trigger=trigger or source, today=today)
    return event, False, case


def process(database: Session, event: IngestedEvent, *, trigger: str | None = None, today: date | None = None) -> Case | None:
    """Ejecuta (o reanuda) el evento y deja su estado final."""
    event.status = "PROCESSING"
    event.attempts += 1
    database.flush()

    # El recorrido va en una transacción anidada: si falla, se deshace solo
    # el recorrido y el registro del evento se conserva para reintentarlo.
    holder: dict[str, Any] = {}
    savepoint = database.begin_nested()
    try:
        case = dispatch(database, event, holder, trigger=trigger or event.source, today=today)
        database.flush()
        savepoint.commit()
    except Exception as error:
        savepoint.rollback()
        logger.exception("El evento %s (%s) no se pudo procesar", event.id, event.external_id)
        event.status = "FAILED"
        event.error = f"{type(error).__name__}: {error}"[:2000]
        event.failed_agent = None
        event.run_id = None
        database.flush()
        return None

    run = holder.get("run")
    event.run_id = run.id if run is not None else None
    event.case_id = case.id if case is not None else event.case_id
    failed = []
    if run is not None:
        failed = database.scalars(
            select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "ERROR").order_by(AgentStep.position)
        ).all()

    if failed:
        first = failed[0]
        event.status = "NEEDS_HUMAN"
        event.failed_agent = first.agent
        event.error = first.summary[:2000]
        if case is not None:
            mark_case(database, case, event)
    else:
        event.status = "COMPLETED"
        event.failed_agent = None
        event.error = None
        event.completed_at = datetime.now(timezone.utc)
        if case is not None and (case.facts or {}).get("processing"):
            facts = dict(case.facts)
            facts.pop("processing", None)
            case.facts = facts
            database.add(CaseEvent(case_id=case.id, kind="system", actor="orquestador", title="Recorrido reanudado y completado: todos los agentes terminaron bien", data={"event_id": event.id}))
    database.flush()
    return case


def mark_case(database: Session, case: Case, event: IngestedEvent) -> None:
    from app.agents.registry import AGENTS_BY_CODE

    name = AGENTS_BY_CODE.get(event.failed_agent or "", {}).get("name", event.failed_agent)
    case.facts = {
        **(case.facts or {}),
        "processing": {"status": "NEEDS_HUMAN", "event_id": event.id, "failed_agent": event.failed_agent, "agent_name": name, "error": event.error},
    }
    database.add(
        CaseEvent(
            case_id=case.id, kind="system", actor="orquestador",
            title=f"El agente {name} no pudo terminar: el expediente necesita a una persona"[:255],
            detail=event.error, data={"event_id": event.id, "failed_agent": event.failed_agent},
        )
    )


def retry(database: Session, event_id: int) -> tuple[IngestedEvent, Case | None]:
    event = database.get(IngestedEvent, event_id)
    if event is None:
        raise LookupError("Evento no encontrado.")
    case = process(database, event, trigger="manual")
    return database.get(IngestedEvent, event_id), case


def serialize_event(event: IngestedEvent) -> dict[str, Any]:
    from app.agents.registry import AGENTS_BY_CODE

    return {
        "id": event.id,
        "source": event.source,
        "external_id": event.external_id,
        "kind": event.kind,
        "status": event.status,
        "status_label": STATUS_LABELS.get(event.status, event.status),
        "case_id": event.case_id,
        "run_id": event.run_id,
        "failed_agent": event.failed_agent,
        "failed_agent_name": AGENTS_BY_CODE.get(event.failed_agent or "", {}).get("name"),
        "error": event.error,
        "attempts": event.attempts,
        "duplicates": event.duplicates,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "updated_at": event.updated_at.isoformat() if event.updated_at else None,
        "completed_at": event.completed_at.isoformat() if event.completed_at else None,
    }
