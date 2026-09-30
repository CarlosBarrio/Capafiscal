"""
Notificaciones administrativas (AEAT, Seguridad Social, DGT...).

Detecta notificaciones en los documentos subidos, calcula un plazo
orientativo según el tipo de acto y crea tareas en la bandeja. Los plazos
son revisables: dependen de la fecha real de notificación y de festivos
autonómicos o locales que CapaFiscal no conoce.
"""
from __future__ import annotations

import re
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.calendar_es import add_business_days
from app.calendar_es import add_months
from app.calendar_es import days_until
from app.calendar_es import next_business_day
from app.extractor import DATE_PATTERN
from app.extractor import normalize_amount
from app.extractor import normalize_search_text
from app.extractor import parse_date_value
from app.models import Document
from app.models import FiscalNotification
from app.models import Task


ISSUERS = {
    "AEAT": "Agencia Tributaria",
    "TGSS": "Seguridad Social",
    "DGT": "Tráfico (DGT)",
    "AYUNTAMIENTO": "Ayuntamiento",
    "CCAA": "Hacienda autonómica",
    "OTRO": "Otro organismo",
}

NOTIFICATION_TYPES = {
    "REQUERIMIENTO": "Requerimiento de información o documentación",
    "PROPUESTA_LIQUIDACION": "Propuesta de liquidación / trámite de alegaciones",
    "LIQUIDACION": "Liquidación (pago en periodo voluntario)",
    "APREMIO": "Providencia de apremio",
    "EMBARGO": "Diligencia de embargo",
    "SANCION": "Procedimiento sancionador",
    "COMUNICACION": "Comunicación informativa",
    "OTRO": "Otra notificación",
}

STATUSES = ("PENDING", "IN_PROGRESS", "ANSWERED", "CLOSED")
OPEN_STATUSES = {"PENDING", "IN_PROGRESS"}

# Días naturales tras la puesta a disposición en sede/DEHú sin acceder
# para entenderse rechazada (Ley 39/2015, art. 43.2).
DEEMED_REJECTED_DAYS = 10

ISSUER_KEYWORDS = (
    ("TGSS", ("tesoreria general de la seguridad social", "seguridad social", "tgss")),
    ("DGT", ("direccion general de trafico", "jefatura provincial de trafico")),
    ("CCAA", ("agencia tributaria de", "hacienda tributaria de", "agencia tributaria canaria", "consejeria de hacienda")),
    ("AEAT", ("agencia tributaria", "agencia estatal de administracion tributaria", "aeat")),
    ("AYUNTAMIENTO", ("ayuntamiento", "organismo autonomo de recaudacion")),
)

TYPE_KEYWORDS = (
    ("EMBARGO", ("diligencia de embargo", "embargo")),
    ("APREMIO", ("providencia de apremio", "apremio")),
    ("SANCION", ("procedimiento sancionador", "imposicion de sancion", "expediente sancionador", "sancion")),
    ("PROPUESTA_LIQUIDACION", ("propuesta de liquidacion", "tramite de audiencia", "tramite de alegaciones", "alegaciones")),
    ("LIQUIDACION", ("liquidacion provisional", "resolucion con liquidacion", "liquidacion")),
    ("REQUERIMIENTO", ("requerimiento",)),
    ("COMUNICACION", ("comunicacion", "notificacion", "carta informativa")),
)

REFERENCE_PATTERN = re.compile(
    r"(?:referencia|expediente|n\.?\s*[ºo°]\s*(?:de\s+)?(?:expediente|referencia)?|n[úu]mero de (?:expediente|referencia))"
    r"\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-.]{4,40})",
    re.IGNORECASE,
)

AMOUNT_PATTERN = re.compile(
    r"(?:importe|deuda|total a ingresar|a ingresar|principal|cuota)[^\n\d]{0,40}"
    r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+\.\d{2})\s*(?:€|eur)?",
    re.IGNORECASE,
)

ACTION_WORDS = (
    "aportar", "presentar", "ingresar", "plazo", "alegaciones",
    "debera", "requiere", "documentacion", "recurso", "comparecer",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -------------------------------------------------------------------
# Detección
# -------------------------------------------------------------------

def detect_issuer(normalized: str) -> str | None:
    for issuer, keywords in ISSUER_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return issuer

    return None


def detect_type(normalized: str) -> str:
    for notification_type, keywords in TYPE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return notification_type

    return "OTRO"


def detect_notification(
    text: str,
    filename: str = "",
) -> dict[str, Any] | None:
    """
    Devuelve los datos de la notificación o None si el texto no parece
    una notificación administrativa.
    """
    normalized = normalize_search_text(f"{filename}\n{text}")
    issuer = detect_issuer(normalized)
    notification_type = detect_type(normalized)

    if issuer is None and notification_type in {"OTRO", "COMUNICACION"}:
        return None

    if issuer is None:
        # Un acto típico sin organismo reconocible: probablemente sí lo es.
        issuer = "OTRO"

    reference_match = REFERENCE_PATTERN.search(text)
    reference = reference_match.group(1).strip(" .") if reference_match else None

    amount = None
    amount_match = AMOUNT_PATTERN.search(text)

    if amount_match:
        amount = normalize_amount(amount_match.group(1))

    available_at = None
    availability_match = re.search(
        r"puesta a disposici[oó]n[^\d]{0,30}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        text,
        re.IGNORECASE,
    )

    if availability_match:
        available_at = parse_date_value(availability_match.group(1))

    document_date = None

    for match in DATE_PATTERN.finditer(text):
        parsed = parse_date_value(match.group(1))

        if parsed:
            document_date = parsed
            break

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    key_lines = [
        line
        for line in lines
        if any(word in normalize_search_text(line) for word in ACTION_WORDS)
    ][:3]

    title = NOTIFICATION_TYPES[notification_type]

    if issuer != "OTRO":
        title = f"{title} · {ISSUERS[issuer]}"

    return {
        "issuer": issuer,
        "notification_type": notification_type,
        "title": title,
        "reference": reference,
        "amount": amount,
        "available_at": available_at,
        "document_date": document_date,
        "summary": " ".join(key_lines)[:1000] or None,
    }


STRONG_TYPES = {
    "REQUERIMIENTO",
    "PROPUESTA_LIQUIDACION",
    "LIQUIDACION",
    "APREMIO",
    "EMBARGO",
    "SANCION",
}


def looks_like_administrative_act(text: str) -> bool:
    """
    Acto administrativo claro: organismo reconocido + tipo de acto con
    plazo, y sin mencionar «factura».
    """
    normalized = normalize_search_text(text)

    if "factura" in normalized:
        return False

    return (
        detect_issuer(normalized) is not None
        and detect_type(normalized) in STRONG_TYPES
    )


# -------------------------------------------------------------------
# Plazos
# -------------------------------------------------------------------

def payment_deadline_62_2(notified: date) -> date:
    # LGT art. 62.2: liquidaciones en periodo voluntario.
    if notified.day <= 15:
        target = add_months(notified.replace(day=1), 1).replace(day=20)
    else:
        target = add_months(notified.replace(day=1), 2).replace(day=5)

    return next_business_day(target)


def payment_deadline_62_5(notified: date) -> date:
    # LGT art. 62.5: providencia de apremio.
    if notified.day <= 15:
        target = notified.replace(day=20)
    else:
        target = add_months(notified.replace(day=1), 1).replace(day=5)

    return next_business_day(target)


def compute_deadline(
    notification_type: str,
    *,
    notified_at: date | None,
    available_at: date | None,
    document_date: date | None = None,
) -> tuple[date | None, str]:
    """
    Devuelve (fecha límite, explicación de la regla aplicada).
    """
    base = notified_at
    base_text = "desde la fecha de notificación"

    if base is None and available_at is not None:
        base = available_at + timedelta(days=DEEMED_REJECTED_DAYS)
        base_text = (
            "suponiendo que se entiende notificada 10 días naturales "
            "después de la puesta a disposición (art. 43.2 Ley 39/2015)"
        )

    if base is None and document_date is not None:
        base = document_date
        base_text = (
            "desde la fecha del documento (la más prudente); indica la "
            "fecha real de notificación para afinarlo"
        )

    if base is None:
        return None, "Indica la fecha de notificación para calcular el plazo."

    if notification_type == "REQUERIMIENTO":
        return (
            add_business_days(base, 10),
            f"10 días hábiles {base_text}.",
        )

    if notification_type == "PROPUESTA_LIQUIDACION":
        return (
            add_business_days(base, 10),
            f"10 días hábiles para alegaciones {base_text} (puede ser 15 "
            "según el procedimiento: compruébalo en el documento).",
        )

    if notification_type == "SANCION":
        return (
            add_business_days(base, 15),
            f"15 días hábiles para alegaciones {base_text}.",
        )

    if notification_type == "LIQUIDACION":
        return (
            payment_deadline_62_2(base),
            "Pago en periodo voluntario (art. 62.2 LGT): notificada del "
            "1 al 15 → hasta el día 20 del mes siguiente; del 16 al final → "
            f"hasta el día 5 del segundo mes siguiente. Calculado {base_text}. "
            "Para recurrir: 1 mes.",
        )

    if notification_type == "APREMIO":
        return (
            payment_deadline_62_5(base),
            "Providencia de apremio (art. 62.5 LGT): notificada del 1 al "
            "15 → hasta el día 20 del mismo mes; del 16 al final → hasta el "
            f"día 5 del mes siguiente. Calculado {base_text}.",
        )

    if notification_type == "EMBARGO":
        return (
            base,
            "Diligencia de embargo: actúa de inmediato. Para recurrir "
            f"dispones de 1 mes ({add_months(base, 1).strftime('%d/%m/%Y')}).",
        )

    if notification_type == "COMUNICACION":
        return None, "Comunicación informativa: normalmente sin plazo de respuesta."

    return None, "Tipo sin regla de plazo: indícalo manualmente si procede."


def recompute_deadline(
    notification: FiscalNotification,
    document_date: date | None = None,
) -> None:
    if notification.deadline_manual:
        return

    deadline, rule = compute_deadline(
        notification.notification_type,
        notified_at=notification.notified_at,
        available_at=notification.available_at,
        document_date=document_date,
    )
    notification.deadline = deadline
    notification.deadline_rule = rule


# -------------------------------------------------------------------
# Creación y tareas
# -------------------------------------------------------------------

def notification_priority(notification: FiscalNotification) -> str:
    if notification.notification_type in {"EMBARGO", "APREMIO"}:
        return "HIGH"

    if notification.deadline is None:
        return "NORMAL"

    remaining = days_until(notification.deadline)

    if remaining <= 5:
        return "HIGH"

    if remaining <= 15:
        return "NORMAL"

    return "LOW"


def notification_reason(notification: FiscalNotification) -> str:
    text = notification.title

    if notification.deadline:
        remaining = days_until(notification.deadline)
        when = notification.deadline.strftime("%d/%m/%Y")

        if remaining < 0:
            text += f" · plazo vencido el {when}"
        elif remaining == 0:
            text += " · vence hoy"
        else:
            text += f" · vence el {when} ({remaining} días)"

    return text


def sync_notification_task(
    database: Session,
    notification: FiscalNotification,
) -> Task | None:
    from app.task_service import add_task_event

    if notification.document_id is None:
        return None

    task = database.scalar(
        select(Task).where(
            Task.task_type == "NOTIFICATION",
            Task.document_id == notification.document_id,
        )
    )

    if notification.status not in OPEN_STATUSES:
        if task is not None and task.status != "RESOLVED":
            task.status = "RESOLVED"
            task.resolution = notification.status
            task.resolved_at = utc_now()
            add_task_event(
                database,
                task=task,
                action="task.resolved_automatically",
                actor="system",
                event_data={"notification_status": notification.status},
            )

        return task

    priority = notification_priority(notification)
    reason = notification_reason(notification)

    if task is None:
        task = Task(
            task_type="NOTIFICATION",
            status="OPEN",
            priority=priority,
            document_id=notification.document_id,
            reason=reason,
        )
        database.add(task)
        database.flush()
        add_task_event(
            database,
            task=task,
            action="task.created",
            actor="system",
            event_data={"notification_id": notification.id, "priority": priority},
        )
    else:
        if task.status == "RESOLVED":
            task.status = "OPEN"
            task.resolved_at = None
        task.priority = priority
        task.reason = reason

    return task


def sync_all_notification_tasks(database: Session) -> None:
    notifications = database.scalars(select(FiscalNotification)).all()

    for notification in notifications:
        sync_notification_task(database, notification)


def create_notification(
    database: Session,
    *,
    document: Document | None,
    data: dict[str, Any],
    actor: str,
) -> FiscalNotification:
    from app.invoice_service import add_audit_event

    notification = FiscalNotification(
        document_id=document.id if document else None,
        issuer=data.get("issuer") or "OTRO",
        notification_type=data.get("notification_type") or "OTRO",
        title=(data.get("title") or NOTIFICATION_TYPES.get(
            data.get("notification_type") or "OTRO", "Notificación"
        ))[:255],
        reference=data.get("reference"),
        summary=data.get("summary"),
        amount=data.get("amount"),
        available_at=data.get("available_at"),
        notified_at=data.get("notified_at"),
        status="PENDING",
        notes=data.get("notes"),
    )

    if data.get("deadline"):
        notification.deadline = data["deadline"]
        notification.deadline_manual = True
        notification.deadline_rule = "Plazo indicado manualmente."
    else:
        recompute_deadline(notification, data.get("document_date"))

    database.add(notification)

    if document is not None:
        document.kind = "NOTIFICATION"
        document.status = "NEEDS_REVIEW"

    database.flush()

    add_audit_event(
        database,
        action="notification.detected" if actor == "extractor" else "notification.created",
        entity_type="notification",
        entity_id=notification.id,
        actor=actor,
        event_data={
            "document_id": notification.document_id,
            "issuer": notification.issuer,
            "type": notification.notification_type,
            "deadline": notification.deadline,
            "reference": notification.reference,
        },
    )

    sync_notification_task(database, notification)

    return notification


def detect_and_register(
    database: Session,
    *,
    document: Document,
    text: str,
) -> FiscalNotification | None:
    """Llamado tras extraer un documento que no es factura."""
    existing = database.scalar(
        select(FiscalNotification).where(
            FiscalNotification.document_id == document.id
        )
    )

    if existing is not None:
        return existing

    data = detect_notification(text, document.original_filename)

    if data is None:
        return None

    return create_notification(
        database,
        document=document,
        data=data,
        actor="extractor",
    )


def update_notification(
    database: Session,
    notification: FiscalNotification,
    changes: dict[str, Any],
    actor: str,
) -> FiscalNotification:
    from app.invoice_service import add_audit_event

    before = {
        "status": notification.status,
        "deadline": notification.deadline,
        "notified_at": notification.notified_at,
        "type": notification.notification_type,
    }

    recompute = False

    for field in ("issuer", "notification_type", "title", "reference",
                  "summary", "notes", "amount"):
        if field in changes:
            setattr(notification, field, changes[field])
            recompute = recompute or field == "notification_type"

    for field in ("notified_at", "available_at"):
        if field in changes:
            setattr(notification, field, changes[field])
            recompute = True

    if "deadline" in changes:
        if changes["deadline"]:
            notification.deadline = changes["deadline"]
            notification.deadline_manual = True
            notification.deadline_rule = "Plazo indicado manualmente."
        else:
            notification.deadline_manual = False
            recompute = True

    if recompute:
        document_date = None

        if notification.document is not None:
            document_date = notification.document.created_at.date()

        recompute_deadline(notification, document_date)

    if "status" in changes:
        status = changes["status"]

        if status not in STATUSES:
            raise ValueError("Estado de notificación no válido.")

        notification.status = status
        notification.closed_at = (
            utc_now() if status in {"ANSWERED", "CLOSED"} else None
        )

        if notification.document is not None and status in {"ANSWERED", "CLOSED"}:
            notification.document.status = "RESOLVED"
        elif notification.document is not None:
            notification.document.status = "NEEDS_REVIEW"

    database.flush()

    add_audit_event(
        database,
        action="notification.updated",
        entity_type="notification",
        entity_id=notification.id,
        actor=actor,
        event_data={
            "document_id": notification.document_id,
            "before": before,
            "after": {
                "status": notification.status,
                "deadline": notification.deadline,
                "notified_at": notification.notified_at,
                "type": notification.notification_type,
            },
        },
    )

    sync_notification_task(database, notification)

    return notification


def serialize_notification(notification: FiscalNotification) -> dict[str, Any]:
    remaining = (
        days_until(notification.deadline)
        if notification.deadline
        else None
    )

    if notification.status not in OPEN_STATUSES:
        urgency = "done"
    elif remaining is None:
        urgency = "none"
    elif remaining < 0:
        urgency = "overdue"
    elif remaining <= 3 or notification.notification_type == "EMBARGO":
        urgency = "critical"
    elif remaining <= 10:
        urgency = "high"
    else:
        urgency = "normal"

    return {
        "id": notification.id,
        "document_id": notification.document_id,
        "filename": (
            notification.document.original_filename
            if notification.document
            else None
        ),
        "issuer": notification.issuer,
        "issuer_label": ISSUERS.get(notification.issuer, notification.issuer),
        "notification_type": notification.notification_type,
        "type_label": NOTIFICATION_TYPES.get(
            notification.notification_type,
            notification.notification_type,
        ),
        "title": notification.title,
        "reference": notification.reference,
        "summary": notification.summary,
        "amount": float(notification.amount) if notification.amount is not None else None,
        "available_at": notification.available_at.isoformat() if notification.available_at else None,
        "notified_at": notification.notified_at.isoformat() if notification.notified_at else None,
        "deadline": notification.deadline.isoformat() if notification.deadline else None,
        "deadline_rule": notification.deadline_rule,
        "deadline_manual": notification.deadline_manual,
        "days_left": remaining,
        "urgency": urgency,
        "status": notification.status,
        "notes": notification.notes,
        "created_at": notification.created_at.isoformat(),
    }


def list_notifications(
    database: Session,
    *,
    only_open: bool = False,
) -> list[dict[str, Any]]:
    statement = (
        select(FiscalNotification)
        .options(selectinload(FiscalNotification.document))
    )

    if only_open:
        statement = statement.where(
            FiscalNotification.status.in_(OPEN_STATUSES)
        )

    notifications = [
        serialize_notification(item)
        for item in database.scalars(statement).all()
    ]

    urgency_order = {
        "overdue": 0, "critical": 1, "high": 2, "normal": 3,
        "none": 4, "done": 5,
    }
    notifications.sort(
        key=lambda item: (
            urgency_order.get(item["urgency"], 9),
            item["deadline"] or "9999-12-31",
        )
    )

    return notifications


def parse_optional_amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None

    return normalize_amount(value)
