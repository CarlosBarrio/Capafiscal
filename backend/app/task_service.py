from __future__ import annotations

from app import clock
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import case
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.models import AuditEvent
from app.models import Document
from app.models import Invoice
from app.models import Task
from app.models import TaskEvent


OPEN_TASK_STATUSES = {
    "OPEN",
    "IN_PROGRESS",
}

# Orden explícito: ordenar el texto daría HIGH < LOW < NORMAL.
PRIORITY_ORDER = case(
    {
        "HIGH": 0,
        "NORMAL": 1,
        "LOW": 2,
    },
    value=Task.priority,
    else_=3,
)

STATUS_ORDER = case(
    {
        "IN_PROGRESS": 0,
        "OPEN": 1,
    },
    value=Task.status,
    else_=2,
)

REVIEW_DOCUMENT_STATUSES = {
    "NEEDS_REVIEW",
    "READY_FOR_APPROVAL",
    "FAILED",
}


def utc_now() -> datetime:
    return clock.now()


def add_task_event(
    database: Session,
    *,
    task: Task,
    action: str,
    actor: str,
    event_data: dict[str, Any] | None = None,
) -> TaskEvent:
    clean_event_data = event_data or {}

    event = TaskEvent(
        task=task,
        action=action,
        actor=actor,
        event_data=clean_event_data,
    )
    database.add(event)

    # También se registra en la auditoría global para que la tarea
    # aparezca en /api/activity.
    if task.id is not None:
        audit_event = AuditEvent(
            actor=actor,
            action=action,
            entity_type="task",
            entity_id=str(task.id),
            event_data={
                **clean_event_data,
                "document_id": task.document_id,
                "invoice_id": task.invoice_id,
            },
        )
        database.add(audit_event)

    return event


def calculate_task_priority(
    document: Document,
) -> str:
    invoice = document.invoice

    if document.status == "FAILED":
        return "HIGH"

    if document.requires_ocr:
        return "HIGH"

    if invoice is None:
        return "HIGH"

    if invoice.duplicate_status == "STRONG":
        return "HIGH"

    if invoice.validation_status in {
        "INCOMPLETE",
        "MISMATCH",
    }:
        return "HIGH"

    if invoice.confidence < 75:
        return "HIGH"

    if invoice.confidence < 90:
        return "NORMAL"

    return "LOW"


def calculate_task_reason(
    document: Document,
) -> str:
    invoice = document.invoice

    if document.status == "FAILED":
        return (
            document.failure_reason
            or "La extracción del documento ha fallado."
        )

    if document.requires_ocr:
        return (
            "El documento necesita OCR o una revisión "
            "manual de su contenido."
        )

    if invoice is None:
        return (
            "No se ha podido identificar una factura "
            "en el documento."
        )

    if invoice.duplicate_status == "STRONG":
        return (
            "La factura podría estar duplicada por "
            "proveedor y número de factura."
        )

    if invoice.duplicate_status == "PROBABLE":
        return (
            "La factura podría estar duplicada por "
            "proveedor, fecha e importe."
        )

    if invoice.validation_status == "MISMATCH":
        return (
            "Los importes extraídos no cuadran y "
            "necesitan revisión."
        )

    if invoice.validation_status == "INCOMPLETE":
        return (
            "La factura tiene campos obligatorios "
            "sin completar."
        )

    if document.status == "READY_FOR_APPROVAL":
        return "La factura está preparada para su aprobación."

    return "El documento necesita revisión."


def document_requires_review_task(
    document: Document,
) -> bool:
    if document.kind == "NOTIFICATION":
        return False

    if document.status in {
        "APPROVED",
        "REJECTED",
        "EXPORTED",
    }:
        return False

    if document.requires_ocr:
        return True

    return document.status in REVIEW_DOCUMENT_STATUSES


def get_task(
    database: Session,
    task_id: int,
) -> Task | None:
    statement = (
        select(Task)
        .where(Task.id == task_id)
        .options(
            selectinload(Task.document)
            .selectinload(Document.invoice)
            .selectinload(Invoice.tax_lines),
            selectinload(Task.events),
        )
    )

    return database.scalar(statement)


def find_document_task(
    database: Session,
    document_id: int,
) -> Task | None:
    statement = (
        select(Task)
        .where(
            Task.task_type == "REVIEW_INVOICE",
            Task.document_id == document_id,
        )
        .limit(1)
    )

    return database.scalar(statement)


def synchronize_document_task(
    database: Session,
    *,
    document: Document,
) -> Task | None:
    task = find_document_task(
        database,
        document.id,
    )

    invoice = document.invoice
    invoice_id = invoice.id if invoice else None

    if document.status in {
        "APPROVED",
        "REJECTED",
        "EXPORTED",
        "RESOLVED",
    } or document.kind == "NOTIFICATION":
        if task is not None and task.status in OPEN_TASK_STATUSES:
            task.status = "RESOLVED"
            task.resolution = (
                "NOTIFICATION"
                if document.kind == "NOTIFICATION"
                else document.status
            )
            task.resolution_notes = (
                "Documento clasificado como notificación administrativa."
                if document.kind == "NOTIFICATION"
                else "Tarea resuelta automáticamente por "
                f"el estado {document.status}."
            )
            task.resolved_at = utc_now()

            add_task_event(
                database,
                task=task,
                action="task.resolved_automatically",
                actor="system",
                event_data={
                    "document_status": document.status,
                    "invoice_id": invoice_id,
                },
            )

        return task

    if not document_requires_review_task(document):
        return task

    priority = calculate_task_priority(document)
    reason = calculate_task_reason(document)

    if task is None:
        task = Task(
            task_type="REVIEW_INVOICE",
            status="OPEN",
            priority=priority,
            document_id=document.id,
            invoice_id=invoice_id,
            reason=reason,
        )
        database.add(task)
        database.flush()

        add_task_event(
            database,
            task=task,
            action="task.created",
            actor="system",
            event_data={
                "document_status": document.status,
                "priority": priority,
                "reason": reason,
            },
        )

        return task

    task.invoice_id = invoice_id
    task.priority = priority
    task.reason = reason

    return task


def synchronize_all_review_tasks(
    database: Session,
) -> int:
    statement = (
        select(Document)
        .options(
            selectinload(Document.invoice).selectinload(
                Invoice.tax_lines
            )
        )
        .order_by(Document.id.asc())
    )

    documents = database.scalars(statement).all()
    changed_count = 0

    for document in documents:
        previous_task = find_document_task(
            database,
            document.id,
        )

        previous_state = None
        if previous_task is not None:
            previous_state = (
                previous_task.status,
                previous_task.priority,
                previous_task.reason,
            )

        task = synchronize_document_task(
            database,
            document=document,
        )

        if task is None:
            continue

        current_state = (
            task.status,
            task.priority,
            task.reason,
        )

        if previous_task is None or previous_state != current_state:
            changed_count += 1

    from app.notification_service import sync_all_notification_tasks

    sync_all_notification_tasks(database)

    return changed_count


def list_review_tasks(
    database: Session,
    *,
    task_status: str | None = None,
    limit: int = 100,
) -> list[Task]:
    statement = (
        select(Task)
        .where(Task.task_type.in_({"REVIEW_INVOICE", "NOTIFICATION"}))
        .options(
            selectinload(Task.document)
            .selectinload(Document.invoice)
            .selectinload(Invoice.tax_lines),
            selectinload(Task.events),
        )
    )

    if task_status:
        statement = statement.where(
            Task.status == task_status.strip().upper()
        )
    else:
        statement = statement.where(
            Task.status.in_(OPEN_TASK_STATUSES)
        )

    statement = (
        statement
        .order_by(
            STATUS_ORDER,
            PRIORITY_ORDER,
            Task.created_at.asc(),
            Task.id.asc(),
        )
        .limit(limit)
    )

    return list(database.scalars(statement).all())


def start_task(
    database: Session,
    *,
    task: Task,
    actor: str,
) -> Task:
    if task.status == "RESOLVED":
        raise ValueError(
            "La tarea ya está resuelta y no puede iniciarse."
        )

    if task.status == "IN_PROGRESS":
        return task

    task.status = "IN_PROGRESS"
    task.assigned_to = actor
    task.started_at = utc_now()

    add_task_event(
        database,
        task=task,
        action="task.started",
        actor=actor,
        event_data={
            "assigned_to": actor,
        },
    )

    database.flush()
    return task


def resolve_task(
    database: Session,
    *,
    task: Task,
    actor: str,
    resolution: str,
    notes: str | None,
) -> Task:
    if task.status == "RESOLVED":
        return task

    clean_resolution = resolution.strip().upper()
    clean_notes = notes.strip() if notes else None

    if not clean_resolution:
        raise ValueError("La resolución es obligatoria.")

    task.status = "RESOLVED"
    task.resolution = clean_resolution
    task.resolution_notes = clean_notes
    task.resolved_at = utc_now()

    if task.assigned_to is None:
        task.assigned_to = actor

    add_task_event(
        database,
        task=task,
        action="task.resolved",
        actor=actor,
        event_data={
            "resolution": clean_resolution,
            "notes": clean_notes,
        },
    )

    database.flush()
    return task