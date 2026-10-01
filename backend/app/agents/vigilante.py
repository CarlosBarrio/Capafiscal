"""Vigilante: detecta lo nuevo en buzones y sede y prepara el material."""
from __future__ import annotations

import re
from datetime import date
from datetime import timedelta

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import eur
from app.agents.base import evidence
from app.calendar_es import add_business_days
from app.calendar_es import add_months
from app.extractor import normalize_search_text
from app.models import Document
from app.models import ExtractionRun
from app.models import FiscalNotification
from app.models import Invoice
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
    "email": "correo",
    "api": "API",
    "upload": "subida manual",
    "calendario": "calendario fiscal",
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


def invoice_triage(database, invoice: Invoice) -> list[str]:
    """Señales baratas al llegar una factura (la decisión fina es del Detector)."""
    hints = []
    if invoice.duplicate_status in {"STRONG", "PROBABLE"}:
        hints.append("puede estar duplicada")
    if invoice.validation_status not in {None, "VALID"}:
        hints.append("la validación de importes o IVA tiene avisos")
    key = invoice.supplier_tax_id
    if key:
        previous = database.scalars(
            select(Invoice.total).where(Invoice.supplier_tax_id == key, Invoice.id != invoice.id, Invoice.total.is_not(None))
        ).all()
        if previous and invoice.total is not None:
            ordered = sorted(float(value) for value in previous)
            median = ordered[len(ordered) // 2]
            if median > 0 and float(invoice.total) >= 2.5 * median:
                hints.append(f"el importe es {float(invoice.total) / median:.1f} veces lo habitual".replace(".", ","))
    return hints


class Vigilante(Agent):
    code = "vigilante"
    name = "Vigilante"
    role = "Revisa buzones, sede electrónica y correo, detecta lo nuevo y lo descarga."
    icon = "eye"
    handles = ("notification", "invoice", "deadline")
    needs_case = False
    consumes = ("evento: documento, notificación, factura o plazo",)
    produces = ("tipo", "fecha", "organismo o proveedor", "plazo/urgencia", "texto", "evidencia (documento de origen)", "señales de triaje")

    def run(self, ctx: AgentContext) -> StepResult:
        kind = ctx.event.kind if ctx.event else "notification"
        runner = {"invoice": self.run_invoice, "deadline": self.run_deadline}.get(kind, self.run_notification)
        result = runner(ctx)
        # Salida común, venga de donde venga el evento.
        facts = ctx.facts
        deadline = facts.get("deadline")
        days = (deadline - ctx.today).days if deadline else None
        urgency = "desconocida" if days is None else "vencido" if days < 0 else "alta" if days <= 5 else "media" if days <= 15 else "baja"
        if kind == "invoice":
            urgency = "revisar" if facts.get("triage") else "normal"
        result.output.update(
            {
                "tipo": facts.get("type_label"),
                "fecha": facts.get("notified_at") or facts.get("available_at") or (facts.get("invoice") or {}).get("date") or ctx.today,
                "organismo": facts.get("issuer_label") or (facts.get("invoice") or {}).get("supplier") or facts.get("source"),
                "urgencia": urgency,
                "plazo": deadline,
            }
        )
        return result

    def run_invoice(self, ctx: AgentContext) -> StepResult:
        invoice: Invoice = ctx.facts["invoice_obj"]
        document = ctx.database.get(Document, invoice.document_id) if invoice.document_id else None
        ctx.text = document_text(ctx.database, invoice.document_id)
        source = SOURCE_LABELS.get(document.source if document else "", "registro manual")
        hints = invoice_triage(ctx.database, invoice)
        ctx.facts.update(
            {
                "type": "FACTURA",
                "type_label": "Factura recibida",
                "invoice": {
                    "id": invoice.id,
                    "number": invoice.invoice_number,
                    "date": invoice.invoice_date,
                    "supplier": invoice.supplier_name,
                    "supplier_tax_id": invoice.supplier_tax_id,
                    "subtotal": invoice.subtotal,
                    "tax_total": invoice.tax_total,
                    "withholding_total": invoice.withholding_total,
                    "total": invoice.total,
                    "due_date": invoice.due_date,
                    "paid_at": invoice.paid_at,
                    "review_status": invoice.review_status,
                },
                "amount": invoice.total,
                "reference": invoice.invoice_number,
                "source": source,
                "document_id": invoice.document_id,
                "document_name": document.original_filename if document else None,
                "triage": hints,
            }
        )
        total = eur(invoice.total) if invoice.total is not None else "importe sin leer"
        return StepResult(
            summary=f"Nueva factura de {invoice.supplier_name or 'proveedor sin identificar'} ({source}): {invoice.invoice_number or 's/n'}, {total}"
            + (f". Ojo: {'; '.join(hints)}." if hints else "."),
            output={"source": source, "triage": hints, "invoice_id": invoice.id},
            evidence=[evidence("document", document.original_filename, document_id=document.id)] if document else [],
            signals={"suspicious": bool(hints)},
        )

    def run_deadline(self, ctx: AgentContext) -> StepResult:
        from app.tax_service import MODEL_NAMES

        payload = ctx.event.payload
        due = date.fromisoformat(payload["due"])
        days = (due - ctx.today).days
        name = MODEL_NAMES.get(payload["model"], f"Modelo {payload['model']}")
        ctx.facts.update(
            {
                "type": "PLAZO",
                "type_label": f"Plazo del modelo {payload['model']}",
                "period": {**payload},
                "model_name": name,
                "deadline": due,
                "deadline_rule": f"Plazo legal de presentación del {payload['model']} del {payload['quarter']}T {payload['year']}",
                "source": "calendario fiscal",
                "reference": f"{payload['model']} {payload['quarter']}T {payload['year']}",
            }
        )
        ctx.text = f"modelo {payload['model']} ejercicio {payload['year']} periodo {payload['quarter']}T"
        return StepResult(
            summary=f"Faltan {days} días para presentar el {payload['model']} del {payload['quarter']}T {payload['year']} (vence el {due:%d/%m/%Y}).",
            output={"days_left": days, **payload},
            evidence=[evidence("calendar", f"Calendario fiscal · {name}", due=payload["due"])],
        )

    def run_notification(self, ctx: AgentContext) -> StepResult:
        notification: FiscalNotification = ctx.facts["notification"]
        document = ctx.database.get(Document, notification.document_id) if notification.document_id else None
        ctx.text = document_text(ctx.database, notification.document_id) or " ".join(
            filter(None, [notification.title, notification.summary, notification.notes])
        )

        event_source = ctx.event.source if ctx.event else None
        if document is not None:
            source = SOURCE_LABELS.get(document.source, "registro manual")
        else:
            source = SOURCE_LABELS.get(event_source or "", event_source if event_source not in {None, "manual", "system", "pendientes"} else "registro manual")
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
