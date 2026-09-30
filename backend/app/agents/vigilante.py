"""Vigilante: detecta lo nuevo en buzones y sede y prepara el material."""
from __future__ import annotations

import re
from datetime import date
from datetime import timedelta

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import evidence
from app.calendar_es import add_business_days
from app.calendar_es import add_months
from app.extractor import normalize_search_text
from app.models import Document
from app.models import ExtractionRun
from app.models import FiscalNotification
from app.notification_service import DEEMED_REJECTED_DAYS
from app.notification_service import ISSUERS
from app.notification_service import NOTIFICATION_TYPES

SOURCE_LABELS = {
    "manual_upload": "subida manual",
    "outlook": "correo (Outlook)",
    "outlook_demo": "correo (Outlook)",
    "dehu": "buzón DEHú",
    "aeat": "sede de la AEAT",
    "capafiscal_emision": "CapaFiscal",
}


def document_text(database, document_id: int | None) -> str:
    if not document_id:
        return ""
    run = database.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.document_id == document_id, ExtractionRun.raw_text.is_not(None))
        .order_by(ExtractionRun.id.desc())
        .limit(1)
    )
    return run.raw_text if run else ""


TERM_PATTERN = re.compile(r"plazo\s+(?:de\s+|maximo\s+de\s+)?(\d{1,2}|diez|quince|cinco|veinte|un)\s+(dias\s+habiles|dias\s+naturales|dias|mes|meses)")
WORD_NUMBERS = {"cinco": 5, "diez": 10, "quince": 15, "veinte": 20, "un": 1}


def estimate_deadline(text: str, received: date) -> tuple[date, str] | None:
    """Plazo estimado desde la recepción cuando no consta la fecha de notificación."""
    match = TERM_PATTERN.search(normalize_search_text(text))
    if not match:
        return None
    raw, unit = match.group(1), match.group(2)
    number = int(raw) if raw.isdigit() else WORD_NUMBERS[raw]
    if unit.startswith("mes"):
        return add_months(received, number), f"{number} mes(es) desde la recepción ({received:%d/%m/%Y}), estimado por el agente"
    if "naturales" in unit:
        return received + timedelta(days=number), f"{number} días naturales desde la recepción ({received:%d/%m/%Y}), estimado por el agente"
    return add_business_days(received, number), f"{number} días hábiles desde la recepción ({received:%d/%m/%Y}), estimado por el agente"


class Vigilante(Agent):
    code = "vigilante"
    name = "Vigilante"
    role = "Revisa buzones, sede electrónica y correo, detecta lo nuevo y lo descarga."
    icon = "eye"

    def run(self, ctx: AgentContext) -> StepResult:
        notification: FiscalNotification = ctx.facts["notification"]
        document = ctx.database.get(Document, notification.document_id) if notification.document_id else None
        ctx.text = document_text(ctx.database, notification.document_id) or " ".join(
            filter(None, [notification.title, notification.summary, notification.notes])
        )

        source = SOURCE_LABELS.get(document.source if document else "", "registro manual")
        issuer_label = ISSUERS.get(notification.issuer, notification.issuer)
        type_label = NOTIFICATION_TYPES.get(notification.notification_type, notification.notification_type)

        ctx.facts.update(
            {
                "issuer": notification.issuer,
                "issuer_label": issuer_label,
                "type": notification.notification_type,
                "type_label": type_label,
                "reference": notification.reference,
                "amount": notification.amount,
                "available_at": notification.available_at,
                "notified_at": notification.notified_at,
                "deadline": notification.deadline,
                "deadline_rule": notification.deadline_rule,
                "source": source,
                "document_id": notification.document_id,
                "document_name": document.original_filename if document else None,
            }
        )

        warnings = []
        if notification.deadline is None and not notification.deadline_manual:
            received = notification.notified_at or notification.available_at or (notification.created_at.date() if notification.created_at else ctx.today)
            estimate = estimate_deadline(ctx.text, received)
            if estimate:
                notification.deadline, notification.deadline_rule = estimate
                ctx.facts["deadline"] = notification.deadline
                ctx.facts["deadline_rule"] = notification.deadline_rule
                ctx.facts["deadline_estimated"] = True
                warnings.append(f"Plazo estimado ({notification.deadline_rule}): confírmalo con la fecha real de notificación.")
            else:
                warnings.append("No consta el plazo: trátalo como urgente hasta confirmarlo.")
        if notification.available_at and not notification.notified_at:
            limit = notification.available_at + timedelta(days=DEEMED_REJECTED_DAYS)
            if ctx.today >= limit:
                warnings.append(
                    f"Lleva más de {DEEMED_REJECTED_DAYS} días en el buzón sin abrir: se entiende notificada desde el {limit:%d/%m/%Y}."
                )
            else:
                warnings.append(f"Si no se abre antes del {limit:%d/%m/%Y}, se entenderá notificada igualmente.")
        ctx.facts["intake_warnings"] = warnings

        return StepResult(
            summary=f"Nueva notificación de {issuer_label} ({source}): {type_label.lower()}"
            + (f", ref. {notification.reference}" if notification.reference else "")
            + ".",
            output={"source": source, "text_length": len(ctx.text), "warnings": warnings},
            evidence=[evidence("document", document.original_filename, document_id=document.id)] if document else [],
        )
