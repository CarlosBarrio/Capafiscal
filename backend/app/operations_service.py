from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.models import AuditEvent
from app.models import Document
from app.models import Invoice
from app.models import Task
from app.task_service import PRIORITY_ORDER


UTC = timezone.utc

OPEN_TASK_STATUSES = {
    "OPEN",
    "IN_PROGRESS",
}

ACTIVE_DOCUMENT_STATUSES = {
    "RECEIVED",
    "PROCESSING",
    "NEEDS_REVIEW",
    "READY_FOR_APPROVAL",
    "FAILED",
}

RISK_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
}


@dataclass
class RiskItem:
    code: str
    severity: str
    title: str
    explanation: str
    recommended_action: str
    entity_type: str
    entity_id: int
    document_id: int | None = None
    invoice_id: int | None = None
    supplier_name: str | None = None
    amount: Decimal | None = None
    confidence: int | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "recommended_action": self.recommended_action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "document_id": self.document_id,
            "invoice_id": self.invoice_id,
            "supplier_name": self.supplier_name,
            "amount": decimal_to_number(self.amount),
            "confidence": self.confidence,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at is not None
                else None
            ),
        }


def utc_now() -> datetime:
    return datetime.now(UTC)


def decimal_to_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def safe_text(value: str | None) -> str:
    return (value or "").strip()


def load_documents_with_invoices(database: Session) -> list[Document]:
    statement = (
        select(Document)
        .options(
            selectinload(Document.invoice).selectinload(
                Invoice.tax_lines
            )
        )
        .order_by(
            Document.created_at.desc(),
            Document.id.desc(),
        )
    )
    return list(database.scalars(statement).all())


def severity_from_document(
    document: Document,
) -> str:
    invoice = document.invoice

    if document.status == "FAILED":
        return "CRITICAL"

    if document.requires_ocr:
        return "HIGH"

    if invoice is None:
        return "HIGH"

    if invoice.duplicate_status == "STRONG":
        return "HIGH"

    if invoice.validation_status == "MISMATCH":
        return "HIGH"

    if invoice.validation_status == "INCOMPLETE":
        return "MEDIUM"

    if invoice.duplicate_status == "PROBABLE":
        return "MEDIUM"

    if invoice.confidence < 60:
        return "HIGH"

    if invoice.confidence < 80:
        return "MEDIUM"

    return "LOW"


def build_document_risks(
    document: Document,
) -> list[RiskItem]:
    risks: list[RiskItem] = []
    invoice = document.invoice

    supplier_name = (
        invoice.supplier_name
        if invoice is not None
        else None
    )

    invoice_id = invoice.id if invoice is not None else None
    amount = invoice.total if invoice is not None else None
    confidence = invoice.confidence if invoice is not None else None

    common = {
        "entity_type": "document",
        "entity_id": document.id,
        "document_id": document.id,
        "invoice_id": invoice_id,
        "supplier_name": supplier_name,
        "amount": amount,
        "confidence": confidence,
        "created_at": document.updated_at or document.created_at,
    }

    if document.status == "FAILED":
        risks.append(
            RiskItem(
                code="DOCUMENT_PROCESSING_FAILED",
                severity="CRITICAL",
                title="Error al procesar documento",
                explanation=(
                    document.failure_reason
                    or "La extracción automática no pudo completarse."
                ),
                recommended_action=(
                    "Abre el documento, revisa el archivo original "
                    "y ejecuta un reprocesamiento."
                ),
                **common,
            )
        )
        return risks

    if document.requires_ocr:
        risks.append(
            RiskItem(
                code="OCR_REQUIRED",
                severity="HIGH",
                title="Documento pendiente de OCR",
                explanation=(
                    "El documento no contiene texto seleccionable "
                    "suficiente para extraer sus datos de forma fiable."
                ),
                recommended_action=(
                    "Revisar el archivo, ejecutar OCR o completar "
                    "los campos manualmente."
                ),
                **common,
            )
        )

    if invoice is None:
        risks.append(
            RiskItem(
                code="DOCUMENT_NOT_IDENTIFIED",
                severity="HIGH",
                title="Documento no identificado como factura",
                explanation=(
                    "El sistema ha procesado el documento, pero no "
                    "ha encontrado señales suficientes para clasificarlo "
                    "como factura."
                ),
                recommended_action=(
                    "Clasifica el documento manualmente o revisa si "
                    "requiere OCR."
                ),
                **common,
            )
        )
        return risks

    if invoice.duplicate_status == "STRONG":
        risks.append(
            RiskItem(
                code="STRONG_DUPLICATE",
                severity="HIGH",
                title="Posible factura duplicada",
                explanation=(
                    "Existe otra factura con el mismo NIF/CIF de proveedor "
                    "y el mismo número de factura."
                ),
                recommended_action=(
                    "Compara ambos documentos antes de aprobar o "
                    "contabilizar la factura."
                ),
                **common,
            )
        )
    elif invoice.duplicate_status == "PROBABLE":
        risks.append(
            RiskItem(
                code="PROBABLE_DUPLICATE",
                severity="MEDIUM",
                title="Posible duplicado por importe y fecha",
                explanation=(
                    "Existe otra factura del mismo proveedor con la "
                    "misma fecha e importe."
                ),
                recommended_action=(
                    "Comprueba si corresponde a una factura distinta "
                    "o a un duplicado."
                ),
                **common,
            )
        )

    if invoice.validation_status == "MISMATCH":
        risks.append(
            RiskItem(
                code="AMOUNT_MISMATCH",
                severity="HIGH",
                title="Los importes de la factura no cuadran",
                explanation=(
                    "La base imponible, el IVA, las retenciones o el "
                    "recargo no coinciden con el total extraído."
                ),
                recommended_action=(
                    "Revisar base, IVA y total antes de aprobar."
                ),
                **common,
            )
        )

    if invoice.validation_status == "INCOMPLETE":
        missing_fields = [
            item.get("field")
            for item in invoice.validation_messages
            if item.get("code") == "required_field_missing"
        ]
        readable_fields = ", ".join(
            field for field in missing_fields if field
        )

        risks.append(
            RiskItem(
                code="INCOMPLETE_INVOICE",
                severity="MEDIUM",
                title="Factura con campos pendientes",
                explanation=(
                    "Faltan datos obligatorios para validar la factura."
                    + (
                        f" Campos detectados: {readable_fields}."
                        if readable_fields
                        else ""
                    )
                ),
                recommended_action=(
                    "Completa los campos pendientes y guarda la revisión."
                ),
                **common,
            )
        )

    if invoice.confidence < 60:
        risks.append(
            RiskItem(
                code="VERY_LOW_CONFIDENCE",
                severity="HIGH",
                title="Extracción con baja confianza",
                explanation=(
                    f"La confianza global de extracción es del "
                    f"{invoice.confidence}%."
                ),
                recommended_action=(
                    "Verifica todos los datos clave contra el documento "
                    "original."
                ),
                **common,
            )
        )
    elif invoice.confidence < 80:
        risks.append(
            RiskItem(
                code="LOW_CONFIDENCE",
                severity="MEDIUM",
                title="Extracción requiere validación",
                explanation=(
                    f"La confianza global de extracción es del "
                    f"{invoice.confidence}%."
                ),
                recommended_action=(
                    "Revisa los campos con menor confianza antes de "
                    "aprobar la factura."
                ),
                **common,
            )
        )

    if invoice.invoice_date is not None:
        today = datetime.now().date()

        if invoice.invoice_date > today:
            risks.append(
                RiskItem(
                    code="FUTURE_INVOICE_DATE",
                    severity="LOW",
                    title="Fecha de factura futura",
                    explanation=(
                        "La fecha extraída es posterior a la fecha actual."
                    ),
                    recommended_action=(
                        "Comprueba si la fecha se ha extraído correctamente."
                    ),
                    **common,
                )
            )

    return risks


def build_extra_risks(database: Session) -> list[RiskItem]:
    """Riesgos de notificaciones, plazos fiscales y cumplimiento."""
    from app.agenda_service import build_agenda

    severity_by_level = {
        "overdue": "CRITICAL",
        "critical": "CRITICAL",
        "high": "HIGH",
    }
    risks: list[RiskItem] = []

    for item in build_agenda(database, horizon_days=10)["items"]:
        severity = severity_by_level.get(item["level"])

        if severity is None:
            continue

        risks.append(
            RiskItem(
                code=item["code"],
                severity=severity,
                title=item["title"],
                explanation=item["detail"],
                recommended_action=item["action"],
                entity_type=item["entity_type"],
                entity_id=item["entity_id"],
                document_id=item.get("document_id"),
                amount=(
                    Decimal(str(item["amount"]))
                    if item.get("amount") is not None
                    else None
                ),
            )
        )

    return risks


def list_open_risks(
    database: Session,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    documents = load_documents_with_invoices(database)

    risks: list[RiskItem] = []

    for document in documents:
        if document.status in {
            "APPROVED",
            "REJECTED",
            "EXPORTED",
            "RESOLVED",
        } or document.kind == "NOTIFICATION":
            continue

        risks.extend(build_document_risks(document))

    risks.extend(build_extra_risks(database))

    def sort_key(risk: RiskItem) -> tuple[int, int, datetime]:
        # SQLite devuelve fechas sin zona: se comparan todas sin zona.
        created = risk.created_at.replace(tzinfo=None) if risk.created_at else datetime.max

        return (
            RISK_SEVERITY_ORDER.get(risk.severity, 99),
            0 if risk.created_at is None else 1,
            created,
        )

    risks.sort(key=sort_key)

    return [
        risk.to_dict()
        for risk in risks[:limit]
    ]


def build_agent_catalog(
    database: Session,
) -> list[dict[str, Any]]:
    documents = load_documents_with_invoices(database)
    risks = list_open_risks(database, limit=500)

    open_tasks_statement = (
        select(func.count(Task.id))
        .where(Task.status.in_(OPEN_TASK_STATUSES))
    )
    open_tasks = int(
        database.scalar(open_tasks_statement) or 0
    )

    documents_today = sum(
        1
        for document in documents
        if document.created_at.date() == datetime.now().date()
    )

    processed_documents = sum(
        1
        for document in documents
        if document.extraction_status == "COMPLETED"
    )

    failed_documents = sum(
        1
        for document in documents
        if document.status == "FAILED"
    )

    high_risks = sum(
        1
        for risk in risks
        if risk["severity"] in {"CRITICAL", "HIGH"}
    )

    return [
        {
            "id": "documental",
            "name": "Agente documental",
            "icon": "📄",
            "status": (
                "attention"
                if failed_documents > 0
                else "active"
            ),
            "status_label": (
                "Requiere atención"
                if failed_documents > 0
                else "Activo"
            ),
            "description": (
                "Recibe documentos, extrae información, identifica "
                "facturas y prepara los datos para revisión."
            ),
            "metric_label": "Documentos procesados",
            "metric_value": processed_documents,
            "detail": (
                f"{documents_today} documento(s) incorporado(s) hoy."
            ),
        },
        {
            "id": "riesgos",
            "name": "Agente de riesgos",
            "icon": "⚠️",
            "status": (
                "attention"
                if high_risks > 0
                else "active"
            ),
            "status_label": (
                "Prioridades detectadas"
                if high_risks > 0
                else "Sin riesgos críticos"
            ),
            "description": (
                "Detecta descuadres, duplicados, baja confianza, "
                "campos pendientes y errores operativos."
            ),
            "metric_label": "Riesgos abiertos",
            "metric_value": len(risks),
            "detail": (
                f"{high_risks} riesgo(s) de prioridad alta o crítica."
            ),
        },
        {
            "id": "revision",
            "name": "Agente de revisión",
            "icon": "✅",
            "status": (
                "attention"
                if open_tasks > 0
                else "active"
            ),
            "status_label": (
                "Decisiones pendientes"
                if open_tasks > 0
                else "Bandeja al día"
            ),
            "description": (
                "Convierte excepciones del procesamiento documental "
                "en tareas humanas priorizadas."
            ),
            "metric_label": "Tareas abiertas",
            "metric_value": open_tasks,
            "detail": (
                "Las tareas se sincronizan automáticamente con "
                "el estado de cada documento."
            ),
        },
        {
            "id": "outlook",
            "name": "Agente de correo",
            "icon": "✉️",
            "status": "pending",
            "status_label": "Configuración disponible",
            "description": (
                "Importa adjuntos PDF desde Outlook mediante "
                "Microsoft Graph cuando la cuenta está autorizada."
            ),
            "metric_label": "Modo",
            "metric_value": "Graph / manual",
            "detail": (
                "La conexión depende de las credenciales OAuth "
                "configuradas en el entorno."
            ),
        },
        {
            "id": "assistant",
            "name": "Asistente fiscal",
            "icon": "💬",
            "status": "beta",
            "status_label": "Beta controlada",
            "description": (
                "Responde sobre los documentos, facturas, riesgos "
                "y tareas registradas en CapaFiscal."
            ),
            "metric_label": "Fuentes",
            "metric_value": "Datos internos",
            "detail": (
                "No presenta modelos ni sustituye la revisión "
                "de un asesor fiscal."
            ),
        },
    ]


def build_today_dashboard(
    database: Session,
) -> dict[str, Any]:
    documents = load_documents_with_invoices(database)
    risks = list_open_risks(database, limit=20)
    agents = build_agent_catalog(database)

    today = datetime.now().date()

    documents_today = [
        document
        for document in documents
        if document.created_at.date() == today
    ]

    processed_today = [
        document
        for document in documents_today
        if document.extraction_status == "COMPLETED"
    ]

    pending_documents = [
        document
        for document in documents
        if document.status in ACTIVE_DOCUMENT_STATUSES
    ]

    open_tasks_statement = (
        select(Task)
        .where(Task.status.in_(OPEN_TASK_STATUSES))
        .options(
            selectinload(Task.document).selectinload(
                Document.invoice
            )
        )
        .order_by(
            PRIORITY_ORDER,
            Task.created_at.asc(),
        )
        .limit(8)
    )
    tasks = list(database.scalars(open_tasks_statement).all())

    open_tasks_count = int(
        database.scalar(
            select(func.count(Task.id))
            .where(Task.status.in_(OPEN_TASK_STATUSES))
        )
        or 0
    )

    recommendation: dict[str, Any]

    if risks:
        first_risk = risks[0]
        recommendation = {
            "title": first_risk["title"],
            "message": first_risk["explanation"],
            "action": first_risk["recommended_action"],
            "severity": first_risk["severity"],
            "document_id": first_risk["document_id"],
        }
    elif tasks:
        first_task = tasks[0]
        recommendation = {
            "title": "Hay una decisión pendiente",
            "message": (
                first_task.reason
                or "Existe una tarea pendiente de revisión."
            ),
            "action": "Abre la bandeja de revisión y resuelve la tarea.",
            "severity": "MEDIUM",
            "document_id": first_task.document_id,
        }
    else:
        recommendation = {
            "title": "Situación bajo control",
            "message": (
                "No hay riesgos críticos ni tareas abiertas "
                "en este momento."
            ),
            "action": (
                "Puedes revisar las últimas facturas o conectar "
                "una nueva fuente documental."
            ),
            "severity": "LOW",
            "document_id": None,
        }

    activity_statement = (
        select(AuditEvent)
        .order_by(
            AuditEvent.created_at.desc(),
            AuditEvent.id.desc(),
        )
        .limit(8)
    )
    activity = list(database.scalars(activity_statement).all())

    return {
        "generated_at": utc_now().isoformat(),
        "headline": (
            "Tu administrativo digital ha revisado "
            "la actividad disponible."
        ),
        "metrics": {
            "documents_today": len(documents_today),
            "processed_today": len(processed_today),
            "pending_documents": len(pending_documents),
            "open_risks": len(list_open_risks(database, limit=500)),
            "open_tasks": open_tasks_count,
        },
        "recommendation": recommendation,
        "risks": risks[:5],
        "tasks": [
            {
                "id": task.id,
                "task_type": task.task_type,
                "status": task.status,
                "priority": task.priority,
                "reason": task.reason,
                "document_id": task.document_id,
                "invoice_id": task.invoice_id,
                "supplier_name": (
                    task.document.invoice.supplier_name
                    if task.document
                    and task.document.invoice
                    else None
                ),
            }
            for task in tasks
        ],
        "agents": agents,
        "recent_activity": [
            {
                "id": event.id,
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "actor": event.actor,
                "event_data": event.event_data,
                "created_at": event.created_at.isoformat(),
            }
            for event in activity
        ],
    }


def build_monthly_impact(
    database: Session,
) -> dict[str, Any]:
    now = utc_now()
    month_start = now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    document_statement = (
        select(Document)
        .where(Document.created_at >= month_start)
        .options(selectinload(Document.invoice))
    )
    documents = list(database.scalars(document_statement).all())

    processed = [
        document
        for document in documents
        if document.extraction_status == "COMPLETED"
    ]

    approved = [
        document
        for document in documents
        if document.status == "APPROVED"
    ]

    risk_documents = [
        document
        for document in documents
        if document.status in {
            "NEEDS_REVIEW",
            "FAILED",
        }
    ]

    tasks_statement = (
        select(Task)
        .where(Task.created_at >= month_start)
    )
    tasks = list(database.scalars(tasks_statement).all())

    resolved_tasks = [
        task
        for task in tasks
        if task.status == "RESOLVED"
    ]

    from app.company_service import hourly_cost as configured_hourly_cost

    minutes_per_processed_document = 7
    minutes_per_risk_prevented = 12
    hourly_cost = configured_hourly_cost(database)

    saved_minutes = (
        len(processed) * minutes_per_processed_document
        + len(risk_documents) * minutes_per_risk_prevented
    )

    saved_hours = Decimal(saved_minutes) / Decimal("60")
    estimated_cost = saved_hours * hourly_cost

    return {
        "period": now.strftime("%m/%Y"),
        "documents_received": len(documents),
        "documents_processed": len(processed),
        "documents_approved": len(approved),
        "risks_detected": len(risk_documents),
        "tasks_created": len(tasks),
        "tasks_resolved": len(resolved_tasks),
        "estimated_hours_saved": round(float(saved_hours), 1),
        "estimated_cost_saved": round(float(estimated_cost), 2),
        "calculation_note": (
            "Estimación operativa: 7 minutos por documento procesado "
            "y 12 minutos adicionales por incidencia detectada, "
            f"valorados a {float(hourly_cost):.0f} €/hora (configurable en "
            "Mi empresa). No representa ahorro garantizado."
        ),
    }


def connector_catalog(
    database: Session,
) -> list[dict[str, Any]]:
    documents = load_documents_with_invoices(database)

    manual_documents = sum(
        1
        for document in documents
        if document.source == "manual_upload"
    )

    outlook_documents = sum(
        1
        for document in documents
        if document.source in {
            "outlook",
            "outlook_graph",
            "email",
        }
        or (
            document.source_provider
            and "outlook" in document.source_provider.lower()
        )
    )

    return [
        {
            "id": "manual_upload",
            "name": "Carga manual",
            "icon": "⬆️",
            "category": "Documental",
            "status": "active",
            "status_label": "Activo",
            "description": (
                "Carga directa de documentos PDF y TXT con "
                "procesamiento y deduplicación por SHA-256."
            ),
            "documents_found": manual_documents,
            "action": "upload",
            "action_label": "Subir documento",
        },
        {
            "id": "outlook",
            "name": "Microsoft Outlook",
            "icon": "✉️",
            "category": "Correo",
            "status": "available",
            "status_label": "Disponible",
            "description": (
                "Conexión mediante Microsoft Graph para importar "
                "adjuntos PDF desde el buzón autorizado."
            ),
            "documents_found": outlook_documents,
            "action": "outlook",
            "action_label": "Gestionar Outlook",
        },
        {
            "id": "gmail",
            "name": "Gmail",
            "icon": "📨",
            "category": "Correo",
            "status": "planned",
            "status_label": "Próximamente",
            "description": (
                "Conector previsto mediante Gmail API con permisos "
                "mínimos y lectura de adjuntos documentales."
            ),
            "documents_found": 0,
            "action": "none",
            "action_label": "Planificado",
        },
        {
            "id": "bank_csv",
            "name": "CSV bancario",
            "icon": "🏦",
            "category": "Finanzas",
            "status": "planned",
            "status_label": "Próximamente",
            "description": (
                "Importación asistida de movimientos bancarios CSV "
                "para tesorería y conciliación futura."
            ),
            "documents_found": 0,
            "action": "none",
            "action_label": "Planificado",
        },
        {
            "id": "dehu",
            "name": "DEHú / AEAT",
            "icon": "🏛️",
            "category": "Notificaciones",
            "status": "validation",
            "status_label": "Validación legal y técnica",
            "description": (
                "La integración oficial requiere alta, certificados, "
                "apoderamiento y validación operativa previa."
            ),
            "documents_found": 0,
            "action": "none",
            "action_label": "Pendiente",
        },
    ]


QUARTER_WORDS = {
    "primer": 1,
    "1er": 1,
    "segundo": 2,
    "tercer": 3,
    "cuarto": 4,
}


def parse_question_period(
    normalized_question: str,
) -> tuple[int, int | None]:
    """
    Extrae año y trimestre de la pregunta. Por defecto, el trimestre
    en curso del año actual.
    """
    today = date.today()
    year_match = re.search(r"\b(20\d{2})\b", normalized_question)
    year = int(year_match.group(1)) if year_match else today.year

    quarter: int | None = None

    quarter_match = re.search(
        r"\b([1-4])\s*(?:t|º?\s*trimestre)\b",
        normalized_question,
    )

    if quarter_match:
        quarter = int(quarter_match.group(1))
    else:
        for word, number in QUARTER_WORDS.items():
            if f"{word} trimestre" in normalized_question:
                quarter = number
                break

    if quarter is None and "anual" not in normalized_question:
        if "trimestre anterior" in normalized_question:
            current_quarter = (today.month - 1) // 3 + 1
            quarter = current_quarter - 1 or 4
            if current_quarter == 1 and not year_match:
                year -= 1
        else:
            quarter = (today.month - 1) // 3 + 1

    return year, quarter


def format_eur(value: float | Decimal | None) -> str:
    amount = float(value or 0)
    text = f"{amount:,.2f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def format_day(value: str | None) -> str:
    if not value:
        return "—"

    year, month, day = value[:10].split("-")

    return f"{day}/{month}/{year}"


def assistant_answer(
    database: Session,
    question: str,
) -> dict[str, Any]:
    normalized_question = safe_text(question).lower()

    if not normalized_question:
        return {
            "answer": (
                "Puedo ayudarte con documentos, facturas, riesgos, "
                "tareas pendientes, IVA soportado y actividad reciente."
            ),
            "sources": [],
            "mode": "internal_data",
            "warning": (
                "Respuesta basada exclusivamente en datos internos "
                "de CapaFiscal."
            ),
        }

    from app.bank_service import build_business_health
    from app.notification_service import list_notifications
    from app.reports_service import build_payments_overview
    from app.reports_service import build_supplier_list
    from app.reports_service import build_vat_report
    from app.tax_service import build_tax_calendar

    if any(
        keyword in normalized_question
        for keyword in (
            "notificacion",
            "notificación",
            "requerimiento",
            "hacienda",
            "apremio",
            "embargo",
            "multa",
            "sancion",
            "sanción",
            "seguridad social",
            "dehu",
            "dehú",
        )
    ):
        open_notifications = list_notifications(database, only_open=True)

        if not open_notifications:
            return {
                "answer": "No tienes notificaciones administrativas abiertas.",
                "sources": [],
                "mode": "internal_data",
                "warning": "Solo se consideran notificaciones registradas en CapaFiscal.",
            }

        first = open_notifications[0]
        answer = (
            f"Tienes {len(open_notifications)} notificación(es) abierta(s). "
            f"La más urgente: {first['title']}"
        )

        if first["deadline"]:
            answer += f", con plazo orientativo hasta el {format_day(first['deadline'])}"

            if first["days_left"] is not None and first["days_left"] < 0:
                answer += " (ya vencido: actúa cuanto antes)"

        answer += "."

        return {
            "answer": answer,
            "sources": [
                {
                    "type": "notification",
                    "document_id": item["document_id"],
                    "label": (
                        f"{item['title']}"
                        + (f" · vence {format_day(item['deadline'])}" if item["deadline"] else "")
                    ),
                }
                for item in open_notifications[:5]
            ],
            "mode": "internal_data",
            "warning": (
                "Plazos orientativos calculados por reglas; confírmalos con "
                "tu asesor y la fecha real de notificación."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in ("calendario", "presentar", "proximo modelo", "próximo modelo", "plazos fiscales", "que modelos", "qué modelos")
    ):
        today = date.today()
        upcoming = [
            entry
            for entry in build_tax_calendar(database, year=today.year)["entries"]
            + build_tax_calendar(database, year=today.year + 1)["entries"]
            if entry["status"] in {"OVERDUE", "DUE_SOON", "UPCOMING"}
        ][:6]

        if not upcoming:
            return {
                "answer": "No hay modelos pendientes de presentar en el calendario.",
                "sources": [],
                "mode": "internal_data",
                "warning": "Calendario general de la AEAT; revisa obligaciones específicas.",
            }

        listing = "; ".join(
            f"{entry['model']} {entry['period_label']} hasta el {format_day(entry['due_date'])}"
            + (f" (≈ {format_eur(entry['estimate'])})" if entry.get("estimate") else "")
            for entry in upcoming[:4]
        )

        return {
            "answer": f"Próximas obligaciones: {listing}.",
            "sources": [],
            "mode": "internal_data",
            "warning": "Importes estimados con facturas aprobadas; no son autoliquidaciones.",
        }

    if any(
        keyword in normalized_question
        for keyword in ("beneficio", "negocio", "margen", "cobrar", "cobros", "me deben", "facturado", "ingresos", "tesoreria", "tesorería", "caja")
    ):
        today = date.today()
        health = build_business_health(
            database,
            year=today.year,
            quarter=(today.month - 1) // 3 + 1,
        )
        answer = (
            f"{health['period']}: ingresos {format_eur(health['income'])}, gastos "
            f"{format_eur(health['expenses'])} y resultado {format_eur(health['result'])}."
            f" Pendiente de cobro: {format_eur(health['receivables_total'])}"
            + (f" ({format_eur(health['receivables_overdue'])} vencido)" if health["receivables_overdue"] else "")
            + f"; pendiente de pago: {format_eur(health['payables_total'])}."
        )

        if health["bank_balance"] is not None:
            answer += f" Saldo en banco: {format_eur(health['bank_balance'])}."

        if health["insights"]:
            answer += " " + " ".join(health["insights"][:2])

        return {
            "answer": answer,
            "sources": [],
            "mode": "internal_data",
            "warning": health["note"],
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "pago",
            "pagar",
            "pagad",
            "vencid",
            "vence",
            "vencimiento",
            "deuda",
            "debo",
        )
    ):
        payments = build_payments_overview(database)

        if not payments["unpaid_count"]:
            return {
                "answer": (
                    "No hay facturas aprobadas pendientes de pago."
                ),
                "sources": [],
                "mode": "internal_data",
                "warning": (
                    "Solo se consideran facturas aprobadas sin fecha "
                    "de pago registrada."
                ),
            }

        answer = (
            f"Tienes {payments['unpaid_count']} factura(s) aprobada(s) "
            f"sin pagar por {format_eur(payments['unpaid_total'])}."
        )

        if payments["overdue_count"]:
            answer += (
                f" {payments['overdue_count']} están vencidas "
                f"({format_eur(payments['overdue_total'])})."
            )

        if payments["due_soon_count"]:
            answer += (
                f" {payments['due_soon_count']} vence(n) en los próximos "
                f"7 días ({format_eur(payments['due_soon_total'])})."
            )

        return {
            "answer": answer,
            "sources": [
                {
                    "type": "invoice",
                    "document_id": item["document_id"],
                    "invoice_id": item["invoice_id"],
                    "label": (
                        f"{item['supplier_name'] or 'Proveedor'} · "
                        f"{format_eur(item['total'])}"
                        + (
                            f" · vence {format_day(item['due_date'])}"
                            if item["due_date"]
                            else ""
                        )
                    ),
                }
                for item in payments["items"][:6]
            ],
            "mode": "internal_data",
            "warning": (
                "Los pagos se registran manualmente en CapaFiscal; "
                "no se consulta el banco."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "trimestre",
            "303",
            "modelo",
            "liquidacion",
            "liquidación",
        )
    ) or re.search(r"\b[1-4]t\b", normalized_question):
        year, quarter = parse_question_period(normalized_question)
        report = build_vat_report(
            database,
            year=year,
            quarter=quarter,
        )

        rates_text = "; ".join(
            f"{item['label']}: base {format_eur(item['base'])}, "
            f"cuota {format_eur(item['tax'])}"
            for item in report["by_rate"]
        )

        answer = (
            f"{report['period']}: {report['approved_invoices']} "
            f"factura(s) recibida(s) aprobada(s), base "
            f"{format_eur(report['total_base'])} e IVA soportado "
            f"{format_eur(report['total_tax'])}."
        )

        if rates_text:
            answer += f" Desglose por tipo: {rates_text}."

        if report["total_withholding"]:
            answer += (
                f" Retenciones practicadas: "
                f"{format_eur(report['total_withholding'])}."
            )

        if quarter:
            from app.tax_service import build_model_303

            model_303 = build_model_303(database, year=year, quarter=quarter)
            answer += (
                f" IVA repercutido en ventas: {format_eur(report['issued_tax'])}. "
                f"Resultado estimado del modelo 303: "
                f"{format_eur(model_303['result'])} ({model_303['outcome'].lower()})."
            )

        if report["warnings"]:
            answer += " " + " ".join(report["warnings"])

        return {
            "answer": answer,
            "sources": [],
            "mode": "internal_data",
            "warning": report["note"],
        }

    suppliers = build_supplier_list(database)

    for supplier in suppliers:
        name_words = [
            word
            for word in re.findall(
                r"[a-záéíóúñ0-9]+",
                (supplier["name"] or "").lower(),
            )
            if len(word) >= 4
            and word not in {"s.a.", "espana", "españa", "clientes",
                             "comercial", "servicios", "energia"}
        ]

        matches_name = bool(name_words) and name_words[0] in (
            normalized_question
        )
        matches_tax_id = bool(supplier["tax_id"]) and (
            supplier["tax_id"].lower() in normalized_question
        )

        if not (matches_name or matches_tax_id):
            continue

        answer = (
            f"{supplier['name'] or supplier['tax_id']}: "
            f"{supplier['approved_invoices']} factura(s) aprobada(s) por "
            f"{format_eur(supplier['total_approved'])} "
            f"(IVA {format_eur(supplier['tax_approved'])})."
        )

        if supplier["pending_invoices"]:
            answer += (
                f" Además hay {supplier['pending_invoices']} "
                "pendiente(s) de revisión."
            )

        if supplier["unpaid_amount"]:
            answer += (
                f" Pendiente de pago: "
                f"{format_eur(supplier['unpaid_amount'])}."
            )

        if supplier["last_invoice_date"]:
            answer += (
                f" Última factura: "
                f"{format_day(supplier['last_invoice_date'])}."
            )

        return {
            "answer": answer,
            "sources": [],
            "mode": "internal_data",
            "warning": (
                "Datos de facturas registradas en CapaFiscal."
            ),
        }

    documents = load_documents_with_invoices(database)
    risks = list_open_risks(database, limit=100)

    invoices = [
        document.invoice
        for document in documents
        if document.invoice is not None
    ]

    # Solo gastos: las emitidas se consultan en impuestos y negocio.
    approved_invoices = [
        invoice
        for invoice in invoices
        if invoice.review_status == "APPROVED"
        and invoice.direction != "ISSUED"
    ]

    open_tasks_statement = (
        select(Task)
        .where(Task.status.in_(OPEN_TASK_STATUSES))
        .options(
            selectinload(Task.document).selectinload(
                Document.invoice
            )
        )
        .order_by(Task.created_at.asc())
    )
    open_tasks = list(
        database.scalars(open_tasks_statement).all()
    )

    def source_for_invoice(invoice: Invoice) -> dict[str, Any]:
        return {
            "type": "invoice",
            "document_id": invoice.document_id,
            "invoice_id": invoice.id,
            "label": (
                f"{invoice.supplier_name or 'Proveedor sin identificar'} · "
                f"{decimal_to_number(invoice.total) or 0:.2f} "
                f"{invoice.currency or 'EUR'}"
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "riesgo",
            "riesgos",
            "problema",
            "problemas",
            "descuadre",
            "duplicad",
            "urgente",
        )
    ):
        if not risks:
            return {
                "answer": (
                    "No tengo riesgos abiertos en documentos que sigan "
                    "pendientes de revisión."
                ),
                "sources": [],
                "mode": "internal_data",
                "warning": (
                    "Respuesta basada en reglas internas; no es un "
                    "dictamen fiscal o legal."
                ),
            }

        top_risks = risks[:5]

        return {
            "answer": (
                f"He detectado {len(risks)} riesgo(s) abierto(s). "
                f"El más prioritario es: "
                f"{top_risks[0]['title']}."
            ),
            "sources": [
                {
                    "type": "risk",
                    "document_id": risk["document_id"],
                    "label": (
                        f"{risk['severity']} · {risk['title']}"
                    ),
                }
                for risk in top_risks
            ],
            "mode": "internal_data",
            "warning": (
                "Respuesta basada en reglas internas; no sustituye "
                "la revisión profesional."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "pendiente",
            "tarea",
            "tareas",
            "revisar",
            "bandeja",
        )
    ):
        if not open_tasks:
            return {
                "answer": (
                    "La bandeja está al día: no hay tareas abiertas "
                    "de revisión."
                ),
                "sources": [],
                "mode": "internal_data",
                "warning": (
                    "Respuesta basada en datos internos de CapaFiscal."
                ),
            }

        return {
            "answer": (
                f"Tienes {len(open_tasks)} tarea(s) abierta(s). "
                f"La primera prioridad es: "
                f"{open_tasks[0].reason or 'Revisar documento'}."
            ),
            "sources": [
                {
                    "type": "task",
                    "task_id": task.id,
                    "document_id": task.document_id,
                    "label": (
                        task.reason
                        or f"Tarea de revisión #{task.id}"
                    ),
                }
                for task in open_tasks[:5]
            ],
            "mode": "internal_data",
            "warning": (
                "Respuesta basada en datos internos de CapaFiscal."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "iva",
            "impuesto",
            "impuestos",
            "soportado",
        )
    ):
        tax_total = sum(
            (
                invoice.tax_total
                or Decimal("0.00")
            )
            for invoice in approved_invoices
        )

        subtotal = sum(
            (
                invoice.subtotal
                or Decimal("0.00")
            )
            for invoice in approved_invoices
        )

        return {
            "answer": (
                f"Según las facturas aprobadas, tienes "
                f"{float(tax_total):.2f} € de IVA soportado "
                f"sobre una base de {float(subtotal):.2f} €. "
                "Este cálculo no incluye facturas pendientes, "
                "emitidas ni ajustes fiscales externos."
            ),
            "sources": [
                source_for_invoice(invoice)
                for invoice in approved_invoices[:8]
            ],
            "mode": "internal_data",
            "warning": (
                "Es una estimación operativa y no un borrador oficial "
                "de modelo tributario."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "gasto",
            "gastos",
            "cuánto",
            "cuanto",
            "total",
            "proveedor",
            "factura",
            "facturas",
        )
    ):
        total_amount = sum(
            (
                invoice.total
                or Decimal("0.00")
            )
            for invoice in approved_invoices
        )

        supplier_counter = Counter(
            safe_text(invoice.supplier_name)
            for invoice in approved_invoices
            if safe_text(invoice.supplier_name)
        )

        most_common = supplier_counter.most_common(3)

        supplier_text = (
            ", ".join(
                f"{name} ({count})"
                for name, count in most_common
            )
            if most_common
            else "sin proveedores identificados"
        )

        return {
            "answer": (
                f"El gasto aprobado registrado es de "
                f"{float(total_amount):.2f} €. "
                f"Proveedores más frecuentes: {supplier_text}."
            ),
            "sources": [
                source_for_invoice(invoice)
                for invoice in approved_invoices[:8]
            ],
            "mode": "internal_data",
            "warning": (
                "Solo se incluyen facturas aprobadas registradas "
                "en CapaFiscal."
            ),
        }

    if any(
        keyword in normalized_question
        for keyword in (
            "resumen",
            "situación",
            "situacion",
            "hoy",
            "estado",
            "ayuda",
        )
    ):
        return {
            "answer": (
                f"Ahora mismo hay {len(documents)} documento(s) "
                f"registrado(s), {len(risks)} riesgo(s) abierto(s), "
                f"{len(open_tasks)} tarea(s) pendiente(s) y "
                f"{len(approved_invoices)} factura(s) aprobada(s)."
            ),
            "sources": [],
            "mode": "internal_data",
            "warning": (
                "Resumen construido a partir de datos internos "
                "de CapaFiscal."
            ),
        }

    return {
        "answer": (
            "Todavía no tengo una respuesta específica para esa "
            "consulta. Puedo ayudarte con: riesgos, tareas, facturas, "
            "gasto aprobado, IVA soportado de un trimestre, pagos "
            "pendientes, un proveedor concreto o un resumen de situación."
        ),
        "sources": [],
        "mode": "internal_data",
        "warning": (
            "El asistente está en beta y utiliza exclusivamente "
            "datos internos disponibles."
        ),
    }