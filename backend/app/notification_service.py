"""
Notificaciones administrativas (AEAT, Seguridad Social, DGT...).

Detecta notificaciones en los documentos subidos, calcula un plazo
orientativo según el tipo de acto y crea tareas en la bandeja. Los plazos
son revisables: dependen de la fecha real de notificación y de festivos
autonómicos o locales que CapaFiscal no conoce.
"""
from __future__ import annotations

from app import clock
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
from app.calendar_es import last_day_of_month
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
    ("LIQUIDACION", ("liquidacion provisional", "resolucion con liquidacion", "reclamacion de deuda", "reclamacion de cuotas", "liquidacion")),
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
    r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+\.\d{2})(?!\d)\s*(?:€|eur)?",
    re.IGNORECASE,
)

ACTION_WORDS = (
    "aportar", "presentar", "ingresar", "plazo", "alegaciones",
    "debera", "requiere", "documentacion", "recurso", "comparecer",
)


def utc_now() -> datetime:
    return clock.now()


# -------------------------------------------------------------------
# Detección
# -------------------------------------------------------------------

def detect_issuer(normalized: str) -> str | None:
    for issuer, keywords in ISSUER_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return issuer

    return None


def _type_in(normalized: str) -> str | None:
    for notification_type, keywords in TYPE_KEYWORDS:
        for keyword in keywords:
            # Palabra completa: «autoliquidación» no es una liquidación.
            if re.search(rf"(?<![a-z]){re.escape(keyword)}", normalized):
                return notification_type
    return None


def document_title(text: str) -> str | None:
    """El título del acto: la primera línea en mayúsculas de las primeras 30 que nombra un tipo de acto."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][:30]:
        letters = [character for character in line if character.isalpha()]
        if len(letters) >= 8 and sum(character.isupper() for character in letters) / len(letters) > 0.8:
            normalized = normalize_search_text(line)
            if _type_in(normalized) not in (None, "COMUNICACION") or re.search(r"comunicacion|certificado|justificante", normalized):
                return normalized
    return None


def classify_type(text: str, filename: str = "") -> tuple[str, float, str]:
    """(tipo, confianza, de dónde sale). El título manda; el nombre del archivo es el último recurso."""
    title = document_title(text)
    if title and _type_in(title):
        return _type_in(title), 0.95, "título"
    normalized = normalize_search_text(text)
    head = "\n".join([line for line in normalized.splitlines() if line.strip()][:8])
    if _type_in(head):
        return _type_in(head), 0.85, "cabecera"
    if _type_in(normalized):
        return _type_in(normalized), 0.6, "cuerpo"
    by_name = _type_in(re.sub(r"[_\-.]+", " ", normalize_search_text(filename)))
    if by_name:
        return by_name, 0.4, "nombre del archivo"
    return "OTRO", 0.2, "sin indicios"


def detect_type(normalized: str) -> str:
    """El título del acto (primeras líneas) manda sobre el resto del texto."""
    return classify_type(normalized)[0]


DATE_VALUE = r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
# Fecha de notificación: la que abre el plazo (acceso en sede/DEHú o recepción en papel).
NOTIFIED_PATTERNS = (
    r"fecha de notificaci[oó]n[^\d]{0,45}" + DATE_VALUE,
    r"notificad[oa] (?:el(?: d[ií]a)?|en fecha)[^\d]{0,15}" + DATE_VALUE,
    r"fecha de acceso[^\d]{0,40}" + DATE_VALUE,
    r"fecha de (?:recepci[oó]n|entrega)[^\d]{0,30}" + DATE_VALUE,
)
AVAILABLE_PATTERNS = (r"puesta a disposici[oó]n[^\d]{0,30}" + DATE_VALUE,)
DOCUMENT_DATE_PATTERNS = (
    r"(?:^|\n)\s*fecha(?: de emisi[oó]n| del documento| de la diligencia)?\s*:?\s*\n?\s*" + DATE_VALUE,
)
NUMBER_WORDS = {"cinco": 5, "diez": 10, "quince": 15, "veinte": 20, "tres": 3}
TERM_PATTERN = re.compile(r"plazo(?: m[aá]ximo)?:? (?:de )?(\d{1,2}|cinco|diez|quince|veinte|tres) d[ií]as h[aá]biles", re.IGNORECASE)
END_NEXT_MONTH = re.compile(r"[uú]ltimo d[ií]a (?:h[aá]bil )?del mes siguiente", re.IGNORECASE)
# Tipos con plazo propio por ley: el texto no lo cambia (los «días» que citen suelen ser de otra cosa).
LEGAL_TERM_TYPES = {"LIQUIDACION", "APREMIO", "COMUNICACION"}


def first_date(patterns: tuple[str, ...], text: str) -> date | None:
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            parsed = parse_date_value(match.group(1))
            if parsed:
                return parsed
    return None


def deadline_term(text: str, notification_type: str) -> dict[str, Any] | None:
    """El plazo que dice el propio documento, si lo dice."""
    if notification_type == "COMUNICACION":
        return None
    if END_NEXT_MONTH.search(text):
        return {"rule": "end_next_month", "label": "hasta el último día del mes siguiente al de la notificación"}
    if notification_type in LEGAL_TERM_TYPES:
        return None
    match = TERM_PATTERN.search(text)
    if match:
        raw = match.group(1).lower()
        days = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        return {"days": days, "unit": "business", "label": f"{days} días hábiles"}
    return None


# Qué hay que hacer con un documento de un organismo, además de qué es.
ACTION_REQUIRED = "ACTION_REQUIRED"  # acto con plazo o consecuencias: a una persona
INFORMATIONAL = "INFORMATIONAL"  # se lee y se archiva; no pide nada
NO_ACTION = "NO_ACTION"  # justificante, certificado, acuse: se archiva (y se aprovecha el dato)
UNKNOWN = "UNKNOWN"  # no se sabe: a una persona
ACTION_LABELS = {
    ACTION_REQUIRED: "Requiere actuación",
    INFORMATIONAL: "Informativo: no requiere actuación",
    NO_ACTION: "Sin acción: se archiva",
    UNKNOWN: "Sin clasificar: revísalo",
}
NO_ACTION_KINDS = (
    ("JUSTIFICANTE_PRESENTACION", r"justificante de presentacion|la presentacion se ha realizado correctamente"),
    ("CERTIFICADO", r"certificado de (?:estar|encontrarse) al corriente|certifica que[^.]{0,200}al corriente|certificado de (?:residencia|situacion censal|retenciones)"),
    ("ACUSE", r"acuse de recibo|recibo de presentacion"),
)
INFORMATIONAL_PATTERN = r"meramente informativ|no requiere ninguna actuacion|a titulo informativo|a efectos (?:meramente )?informativos"


def action_class(text: str, notification_type: str) -> tuple[str, str | None]:
    """(clase de acción, subtipo del documento si es un justificante o certificado)."""
    normalized = normalize_search_text(text)
    if notification_type in STRONG_TYPES:
        return ACTION_REQUIRED, None
    for kind, pattern in NO_ACTION_KINDS:
        if re.search(pattern, normalized):
            return NO_ACTION, kind
    if re.search(INFORMATIONAL_PATTERN, normalized):
        return INFORMATIONAL, None
    return UNKNOWN, None


def filing_receipt(text: str) -> dict[str, Any] | None:
    """Datos de un justificante de presentación: modelo, ejercicio, periodo, fecha e importe."""
    normalized = normalize_search_text(text)
    model = re.search(r"modelo\s*:?\s*(\d{3})", normalized)
    year = re.search(r"ejercicio\s*:?\s*(20\d{2})", normalized)
    period = re.search(r"periodo\s*:?\s*([1-4])\s*t\b", normalized)
    filed = re.search(r"fecha (?:y hora )?de presentacion\s*:?\s*" + DATE_VALUE, normalized)
    amount = re.search(r"resultado(?: de la autoliquidacion)?\s*:?\s*(-?[\d.]+,\d{2})", normalized)
    if not (model and year and filed):
        return None
    return {
        "model": model.group(1), "year": int(year.group(1)), "period": int(period.group(1)) if period else 0,
        "filed_at": parse_date_value(filed.group(1)), "amount": normalize_amount(amount.group(1)) if amount else None,
    }


def detect_notification(
    text: str,
    filename: str = "",
) -> dict[str, Any] | None:
    """
    Devuelve los datos de la notificación o None si el texto no parece
    una notificación administrativa.
    """
    normalized = normalize_search_text(f"{text}\n{filename}")
    issuer = detect_issuer(normalized)
    notification_type, type_confidence, type_source = classify_type(text, filename)

    if issuer is None and notification_type in {"OTRO", "COMUNICACION"}:
        return None

    if issuer is None:
        # Un acto típico sin organismo reconocible: probablemente sí lo es.
        issuer = "OTRO"

    action, document_kind = action_class(text, notification_type)

    reference_match = REFERENCE_PATTERN.search(text)
    reference = reference_match.group(1).strip(" .") if reference_match else None

    # La cifra que cuenta es el total pendiente, no el primer importe (el principal).
    from app.debt import parse_debt

    debt = parse_debt(text)
    amount = debt.total_outstanding if debt else None
    amount_match = AMOUNT_PATTERN.search(text) if amount is None else None

    if amount_match:
        amount = normalize_amount(amount_match.group(1))

    available_at = first_date(AVAILABLE_PATTERNS, text)
    notified_at = first_date(NOTIFIED_PATTERNS, text)
    document_date = first_date(DOCUMENT_DATE_PATTERNS, text)

    if document_date is None:
        for match in DATE_PATTERN.finditer(text):
            parsed = parse_date_value(match.group(1))

            if parsed and parsed not in {available_at, notified_at}:
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
        "notified_at": notified_at,
        "document_date": document_date,
        "deadline_term": deadline_term(text, notification_type),
        "debt": debt.as_dict() if debt else None,
        "classification": {"organism": issuer, "type": notification_type, "confidence": type_confidence, "source": type_source,
                           "action": action, "document_kind": document_kind},
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


def administrative_record(text: str) -> str | None:
    """Clase de acción si es un documento de un organismo que NO pide nada (y no es una factura)."""
    normalized = normalize_search_text(text)
    if detect_issuer(normalized) is None or ("base imponible" in normalized and "total factura" in normalized):
        return None
    action, _kind = action_class(text, classify_type(text)[0])
    return action if action in {INFORMATIONAL, NO_ACTION} else None


def looks_like_administrative_act(text: str) -> bool:
    """
    Acto administrativo claro: organismo reconocido + tipo de acto con
    plazo, y sin mencionar «factura».
    """
    normalized = normalize_search_text(text)
    is_act = detect_issuer(normalized) is not None and detect_type(normalized) in STRONG_TYPES

    if not is_act:
        return False

    if "factura" not in normalized:
        return True

    # Un requerimiento suele pedir facturas: si la cabecera es la de un
    # organismo con un acto claro y no tiene la estructura de una factura
    # (base imponible + total), es una notificación.
    head = normalize_search_text("\n".join(text.splitlines()[:12]))
    looks_like_invoice = "base imponible" in normalized and ("total factura" in normalized or "importe total" in normalized)
    return (
        not looks_like_invoice
        and detect_issuer(head) is not None
        and detect_type(head) in STRONG_TYPES
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


ESTIMATED = "ESTIMADO, falta la fecha de notificación"


def compute_deadline(
    notification_type: str,
    *,
    notified_at: date | None,
    available_at: date | None,
    document_date: date | None = None,
    term: dict[str, Any] | None = None,
) -> tuple[date | None, str]:
    """
    Devuelve (fecha límite, explicación de la regla aplicada).

    Sin fecha de notificación el plazo es una estimación y la explicación
    empieza por «ESTIMADO»: no se presenta como cierto.
    """
    base = notified_at
    base_text = "desde la fecha de notificación"

    if base is None and available_at is not None:
        base = available_at + timedelta(days=DEEMED_REJECTED_DAYS)
        base_text = (
            f"({ESTIMATED}): suponiendo que se entiende notificada 10 días naturales "
            "después de la puesta a disposición (art. 43.2 Ley 39/2015); confirma la fecha real"
        )

    if base is None and document_date is not None:
        base = document_date
        base_text = (
            f"({ESTIMATED}): desde la fecha del documento (la más prudente); indica la "
            "fecha real de notificación para afinarlo"
        )

    if base is None:
        return None, "Indica la fecha de notificación para calcular el plazo."

    if term and term.get("rule") == "end_next_month":
        following = add_months(base.replace(day=1), 1)
        return (
            last_day_of_month(following.year, following.month),
            f"Según el documento, {term['label']} {base_text}.",
        )

    if term and term.get("days") and notification_type not in LEGAL_TERM_TYPES:
        deadline = add_business_days(base, int(term["days"]))
        if notification_type == "EMBARGO":
            return (
                deadline,
                f"Diligencia de embargo: retén desde ya; {term['label']} para contestar {base_text}. "
                f"Para recurrir dispones de 1 mes ({add_months(base, 1).strftime('%d/%m/%Y')}).",
            )
        return deadline, f"{term['label']} según el documento, {base_text}."

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
        document_date=notification.document_date or document_date,
        term=notification.deadline_term,
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
        document_date=data.get("document_date"),
        deadline_term=data.get("deadline_term"),
        debt=data.get("debt"),
        classification=data.get("classification"),
        status="PENDING",
        notes=data.get("notes"),
    )

    action = (data.get("classification") or {}).get("action")
    if action in {INFORMATIONAL, NO_ACTION}:
        notification.status = "CLOSED"
        notification.closed_at = utc_now()
        notification.notes = ACTION_LABELS[action] + "."

    if data.get("deadline"):
        notification.deadline = data["deadline"]
        notification.deadline_manual = True
        notification.deadline_rule = "Plazo indicado manualmente."
    else:
        recompute_deadline(notification, data.get("document_date"))

    database.add(notification)

    if document is not None:
        document.kind = "NOTIFICATION"
        document.status = "APPROVED" if action in {INFORMATIONAL, NO_ACTION} else "NEEDS_REVIEW"

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

    notification = create_notification(
        database,
        document=document,
        data=data,
        actor="extractor",
    )

    # Un justificante de presentación responde a «¿está presentado?»: se registra.
    if (data.get("classification") or {}).get("document_kind") == "JUSTIFICANTE_PRESENTACION":
        receipt = filing_receipt(text)
        if receipt:
            from app.tax_service import record_filing

            try:
                record_filing(
                    database, model=receipt["model"], year=receipt["year"], period=receipt["period"], filed_at=receipt["filed_at"],
                    amount=receipt["amount"], reference=notification.reference, notes=f"Registrado desde el justificante «{document.original_filename}».",
                )
            except ValueError:
                pass  # modelo que CapaFiscal no lleva: el documento queda archivado igualmente

    return notification


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
        "document_date": notification.document_date.isoformat() if notification.document_date else None,
        "available_at": notification.available_at.isoformat() if notification.available_at else None,
        "notified_at": notification.notified_at.isoformat() if notification.notified_at else None,
        "deadline_term": notification.deadline_term,
        "debt": notification.debt,
        "classification": notification.classification,
        "deadline_estimated": bool(notification.deadline and not notification.deadline_manual and not notification.notified_at),
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
