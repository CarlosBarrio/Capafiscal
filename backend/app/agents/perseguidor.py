"""
Perseguidor (follow-up): pide la documentación que falta, recuerda con
cortesía creciente y deja de insistir cuando llega y es correcta.

Nada sale sin visto bueno: las peticiones y recordatorios se preparan en la
bandeja de salida. El destinatario sube el documento con un enlace personal.
"""
from __future__ import annotations

import secrets
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import evidence
from app.calendar_es import add_business_days
from app.config import settings
from app.models import Case
from app.models import CaseEvent
from app.models import CompanyProfile
from app.models import DocumentRequest
from app.models import OutboxMessage

REMINDER_BUSINESS_DAYS = 3
MAX_REMINDERS = 3


def portal_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/portal/{token}"


def recipient(database: Session) -> tuple[str | None, str | None]:
    """A quién se piden los documentos. En una pyme, a la propia empresa;
    en una gestoría sería el contacto del cliente de la cartera."""
    company = database.scalar(select(CompanyProfile).limit(1))
    return (company.email if company else None), (company.name if company else None)


def request_message_body(case: Case, requests: list[DocumentRequest], reminder: int = 0) -> tuple[str, str]:
    deadline = case.internal_deadline or case.deadline
    when = f" antes del {deadline:%d/%m/%Y}" if deadline else ""
    items = "\n".join(f"  • {item.label}\n    Súbelo aquí: {portal_url(item.token)}" for item in requests)

    if reminder == 0:
        subject = f"Necesitamos documentación · {case.title}"
        intro = (
            f"Hola,\n\nPara contestar {case.title.lower()}"
            + (f" (ref. {case.reference})" if case.reference else "")
            + f" necesitamos la siguiente documentación{when}:\n\n"
        )
    elif reminder < MAX_REMINDERS:
        subject = f"Recordatorio: documentación pendiente · {case.title}"
        intro = f"Hola,\n\nTe recordamos que seguimos esperando esta documentación{when}:\n\n"
    else:
        subject = f"URGENTE: documentación pendiente · {case.title}"
        intro = f"Hola,\n\nEl plazo se acaba y todavía falta esta documentación. Por favor, envíala{when}:\n\n"

    body = intro + items + "\n\nCada enlace es personal: no hace falta usuario ni contraseña.\n\nGracias,\nCapaFiscal"
    return subject, body


def request_label(item: dict[str, Any]) -> str:
    from app.extractor import normalize_search_text

    detail = item.get("detail")
    if not detail or item["code"] == "OTRO":
        return item["label"]
    # Si el texto ya empieza por el nombre del documento, basta con el texto.
    first_word = normalize_search_text(item["label"]).split()[0]
    if normalize_search_text(detail).startswith(first_word):
        return detail[:255]
    return f"{item['label']} — {detail}"[:255]


def pending_requests(case: Case) -> list[DocumentRequest]:
    return [item for item in case.requests if item.status == "PENDING"]


def prepare_requests(database: Session, case: Case, *, created_by: str = "agent") -> tuple[list[DocumentRequest], OutboxMessage | None]:
    """Crea las peticiones de lo que falta y el mensaje (borrador) para pedirlas."""
    from app.outbox_service import create_message

    missing = [
        item for item in case.required_documents
        if item["status"] in {"missing", "partial"} and item["source"] in {"internal", "third"}
    ]
    existing = {item.item_code for item in case.requests if item.status in {"PENDING", "RECEIVED"}}
    to_email, to_name = recipient(database)
    created: list[DocumentRequest] = []

    for item in missing:
        key = item["code"] if item["code"] != "OTRO" else f"OTRO:{(item.get('detail') or item['label'])[:30]}"
        if key in existing:
            continue
        request = DocumentRequest(
            case_id=case.id,
            item_code=key,
            label=request_label(item),
            token=secrets.token_urlsafe(24),
            to_email=to_email,
            to_name=to_name,
            status="PENDING",
        )
        database.add(request)
        case.requests.append(request)
        created.append(request)
        existing.add(key)

    database.flush()
    pending = pending_requests(case)
    if not pending:
        return created, None

    subject, body = request_message_body(case, pending)
    message = database.scalar(
        select(OutboxMessage).where(
            OutboxMessage.entity_type == "case_request",
            OutboxMessage.entity_id == case.id,
            OutboxMessage.status == "DRAFT",
        )
    )
    if message:
        message.subject, message.body = subject, body
    elif created:
        message = create_message(
            database,
            kind="REQUEST",
            subject=subject,
            body=body,
            to_email=to_email,
            to_name=to_name,
            entity_type="case_request",
            entity_id=case.id,
            level=0,
            created_by=created_by,
        )
    return created, message


def link_requests_to_documents(case: Case) -> None:
    by_code = {item.item_code: item for item in case.requests}
    documents = []
    for item in case.required_documents:
        key = item["code"] if item["code"] != "OTRO" else f"OTRO:{(item.get('detail') or item['label'])[:30]}"
        request = by_code.get(key)
        if request:
            item = {**item, "request": {"id": request.id, "status": request.status, "reminders": request.reminders_sent, "url": portal_url(request.token)}}
            if request.status == "PENDING" and item["status"] in {"missing", "partial"}:
                item["status"] = "requested"
        documents.append(item)
    case.required_documents = documents


class Perseguidor(Agent):
    code = "perseguidor"
    name = "Perseguidor"
    role = "Pide la documentación que falta, recuerda y deja de insistir cuando llega."
    icon = "send"
    handles = ("notification", "invoice", "deadline")
    consumes = ("expediente con documentos que faltan (fuente interna o tercero)",)
    produces = ("peticiones con enlace de subida", "borrador del mensaje (espera visto bueno)", "seguimiento y recordatorios")

    def run(self, ctx: AgentContext) -> StepResult:
        case = ctx.case
        created, message = prepare_requests(ctx.database, case)
        link_requests_to_documents(case)
        pending = pending_requests(case)

        if not pending:
            return StepResult(summary="No hace falta pedir nada: el agente tiene todo lo necesario.", output={"requests": 0})

        target = message.to_email if message else (pending[0].to_email or "sin email")
        summary = (
            f"Preparada la petición de {len(pending)} documento(s) a {target or 'la empresa (falta el email en Mi empresa)'}; "
            "espera tu visto bueno en la bandeja de salida."
            if created
            else f"{len(pending)} documento(s) ya solicitados; seguimiento activo."
        )
        return StepResult(
            summary=summary,
            output={"requests": [{"id": item.id, "label": item.label, "status": item.status} for item in pending], "message_id": message.id if message else None},
            evidence=[evidence("request", item.label, url=portal_url(item.token)) for item in pending],
        )


def follow_up(database: Session, today: date | None = None) -> dict[str, Any]:
    """Recordatorios de las peticiones enviadas que siguen sin respuesta."""
    from app.outbox_service import create_message

    today = today or date.today()
    reminders = 0
    escalated = 0
    cases = database.scalars(select(Case).where(Case.status.notin_(["RESOLVED", "DISMISSED", "FILED"]))).all()

    for case in cases:
        pending = pending_requests(case)
        if not pending:
            continue
        messages = database.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.entity_type == "case_request", OutboxMessage.entity_id == case.id)
            .order_by(OutboxMessage.id.desc())
        ).all()
        if not messages or any(item.status == "DRAFT" for item in messages):
            continue  # aún no se ha enviado, o ya hay un recordatorio esperando visto bueno
        last_sent = next((item for item in messages if item.status == "SENT"), None)
        if last_sent is None or last_sent.sent_at is None:
            continue

        due = add_business_days(last_sent.sent_at.date(), REMINDER_BUSINESS_DAYS)
        if today < due:
            for item in pending:
                item.next_reminder_at = due
            continue

        level = max(item.reminders_sent for item in pending) + 1
        subject, body = request_message_body(case, pending, reminder=level)
        create_message(
            database,
            kind="REQUEST",
            subject=subject,
            body=body,
            to_email=pending[0].to_email,
            to_name=pending[0].to_name,
            entity_type="case_request",
            entity_id=case.id,
            level=level,
            created_by="agent",
        )
        for item in pending:
            item.reminders_sent = level
            item.last_contact_at = datetime.now(timezone.utc)
            item.next_reminder_at = add_business_days(today, REMINDER_BUSINESS_DAYS)
        reminders += 1
        database.add(
            CaseEvent(case_id=case.id, kind="agent", actor="perseguidor", title=f"Perseguidor · Recordatorio {level} preparado ({len(pending)} documento(s) sin recibir)", data={})
        )
        if level >= MAX_REMINDERS:
            case.priority = min(100, case.priority + 15)
            escalated += 1
            database.add(
                CaseEvent(
                    case_id=case.id,
                    kind="agent",
                    actor="perseguidor",
                    title="Perseguidor · Sin respuesta tras varios recordatorios: conviene llamar por teléfono",
                    data={},
                )
            )

    return {"reminders": reminders, "escalated": escalated}
