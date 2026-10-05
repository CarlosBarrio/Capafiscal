"""API de expedientes, agentes, memoria y portal de subida de documentos."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from app.config import settings
from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor
from app.models import AgentRun
from app.models import Case
from app.models import CaseAttachment

router = APIRouter(prefix="/api")
portal_router = APIRouter()



def case_or_404(database, case_id: int) -> Case:
    case = database.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Expediente no encontrado.")
    return case


def unprocessable(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def file_response(content: bytes, filename: str, media_type: str, inline: bool = False) -> Response:
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{filename}"'})


# -------------------------------------------------------------------
# Expedientes
# -------------------------------------------------------------------


@router.get("/cases", tags=["Expedientes"])
def cases(database: DatabaseDependency, view: str = Query(default="open")) -> list[dict[str, Any]]:
    from app.case_service import list_cases

    return list_cases(database, view=view)


@router.get("/cases/{case_id}", tags=["Expedientes"])
def case_detail(case_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.case_service import serialize_case

    return serialize_case(database, case_or_404(database, case_id), full=True)


class CaseUpdate(BaseModel):
    draft_response: str | None = Field(default=None, max_length=50000)
    action_index: int | None = None
    add_action: str | None = Field(default=None, max_length=200)
    done: bool | None = None
    document_code: str | None = Field(default=None, max_length=60)
    document_detail: str | None = Field(default=None, max_length=500)
    document_status: str | None = Field(default=None, max_length=20)
    deadline: date | None = None


@router.patch("/cases/{case_id}", tags=["Expedientes"])
def case_update(case_id: int, payload: CaseUpdate, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.case_service import CaseError
    from app.case_service import serialize_case
    from app.case_service import update_case

    case = case_or_404(database, case_id)
    try:
        update_case(database, case, payload.model_dump(exclude_unset=True), normalize_actor(actor_header))
    except CaseError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_case(database, case, full=True)


@router.post("/cases/{case_id}/approve", tags=["Expedientes"])
def case_approve(case_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.case_service import CaseError
    from app.case_service import approve
    from app.case_service import serialize_case

    case = case_or_404(database, case_id)
    try:
        approve(database, case, normalize_actor(actor_header))
    except CaseError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_case(database, case, full=True)


class FilePayload(BaseModel):
    reference: str | None = Field(default=None, max_length=120)
    filed_at: date | None = None


@router.post("/cases/{case_id}/file", tags=["Expedientes"])
def case_file(case_id: int, payload: FilePayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.case_service import file_case
    from app.case_service import serialize_case

    case = case_or_404(database, case_id)
    file_case(database, case, reference=payload.reference, filed_at=payload.filed_at, actor=normalize_actor(actor_header))
    database.commit()
    return serialize_case(database, case, full=True)


class ResolvePayload(BaseModel):
    resolution: str | None = Field(default=None, max_length=2000)
    dismiss: bool = False


@router.post("/cases/{case_id}/resolve", tags=["Expedientes"])
def case_resolve(case_id: int, payload: ResolvePayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.case_service import resolve
    from app.case_service import serialize_case

    case = case_or_404(database, case_id)
    resolve(database, case, resolution=payload.resolution, dismiss=payload.dismiss, actor=normalize_actor(actor_header))
    database.commit()
    return serialize_case(database, case, full=True)


@router.post("/cases/{case_id}/reopen", tags=["Expedientes"])
def case_reopen(case_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.case_service import reopen
    from app.case_service import serialize_case

    case = case_or_404(database, case_id)
    reopen(database, case, normalize_actor(actor_header))
    database.commit()
    return serialize_case(database, case, full=True)


@router.post("/cases/{case_id}/rerun", tags=["Expedientes"])
def case_rerun(case_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.orchestrator import rerun_case
    from app.case_service import serialize_case

    case = case_or_404(database, case_id)
    if rerun_case(database, case) is None and not case.notification_id:
        raise HTTPException(status_code=409, detail="Este expediente no se puede volver a trabajar (lo abrió el barrido del Detector).")
    database.commit()
    database.refresh(case)
    return serialize_case(database, case, full=True)


@router.post("/cases/{case_id}/request-documents", tags=["Expedientes"])
def case_request_documents(case_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.perseguidor import link_requests_to_documents
    from app.agents.perseguidor import prepare_requests
    from app.outbox_service import serialize_message

    case = case_or_404(database, case_id)
    created, message = prepare_requests(database, case, created_by="user")
    link_requests_to_documents(case)
    database.commit()
    return {"created": len(created), "message": serialize_message(message) if message else None}


@router.post("/cases/{case_id}/attachments", tags=["Expedientes"], status_code=201)
async def case_attach(
    case_id: int,
    database: DatabaseDependency,
    uploaded_file: UploadFile = File(...),
    item_code: str | None = Form(default=None),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    from app.case_service import CaseError
    from app.case_service import event
    from app.case_service import serialize_case
    from app.case_service import store_attachment

    case = case_or_404(database, case_id)
    content = await uploaded_file.read()
    try:
        attachment = store_attachment(
            database, case, filename=uploaded_file.filename or "documento", content=content,
            content_type=uploaded_file.content_type, item_code=item_code or None, source="user",
        )
    except CaseError as error:
        raise unprocessable(error) from error
    event(database, case, f"Documento aportado: {attachment.filename}", actor=normalize_actor(actor_header), data={"attachment_id": attachment.id})
    database.commit()
    return serialize_case(database, case, full=True)


@router.get("/case-attachments/{attachment_id}", tags=["Expedientes"])
def case_attachment(attachment_id: int, database: DatabaseDependency) -> FileResponse:
    from app.case_service import attachment_path

    attachment = database.get(CaseAttachment, attachment_id)
    if attachment is None or not attachment_path(attachment).exists():
        raise HTTPException(status_code=404, detail="Adjunto no encontrado.")
    return FileResponse(attachment_path(attachment), filename=attachment.filename, media_type=attachment.mime_type or "application/octet-stream")


@router.get("/cases/{case_id}/letter.pdf", tags=["Expedientes"])
def case_letter(case_id: int, database: DatabaseDependency) -> Response:
    from app.case_service import CaseError
    from app.case_service import build_letter_pdf

    case = case_or_404(database, case_id)
    try:
        content = build_letter_pdf(database, case)
    except CaseError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return file_response(content, f"escrito_{case.code}.pdf", "application/pdf", inline=True)


@router.get("/cases/{case_id}/package.zip", tags=["Expedientes"])
def case_package(case_id: int, database: DatabaseDependency) -> Response:
    from app.case_service import build_package

    case = case_or_404(database, case_id)
    return file_response(build_package(database, case), f"{case.code}_para_presentar.zip", "application/zip")


# -------------------------------------------------------------------
# Agentes, director y memoria
# -------------------------------------------------------------------


@router.get("/briefing", tags=["Agentes"])
def briefing(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.director import daily_briefing
    from app.agents.director import operational_board

    from app.agents.pulse import pulse_status

    data = daily_briefing(database)
    data["board"] = operational_board(database)
    data["pulse"] = pulse_status(database)
    return data


@router.get("/today", tags=["Agentes"])
def today_view(database: DatabaseDependency) -> dict[str, Any]:
    """¿Qué tengo que hacer hoy? Rojo/naranja/verde, lo de más impacto con el porqué, el trimestre y el banco."""
    from app.agents.director import operational_board

    board = operational_board(database)
    return {key: board[key] for key in ("date", "attention", "pending", "resolved", "top", "fiscal", "bank", "intervention", "time_saved")}


@router.get("/learning", tags=["Agentes"])
def learning_stats(database: DatabaseDependency) -> dict[str, Any]:
    """Qué corrigen y deciden las personas: precisión por campo, por motor y por tipo de aviso."""
    from app.learning import stats

    return stats(database)


class RuleDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@router.get("/learning/rules", tags=["Agentes"])
def learning_rules(database: DatabaseDependency) -> dict[str, Any]:
    """Reglas aprendidas de las correcciones: propuestas (con evidencia y simulación), aprobadas, rechazadas y retiradas."""
    from app.learning import ruleset_version
    from app.learning import serialize_rule
    from app.models import LearningRule

    # Solo lee: las propuestas se crean al registrar la corrección (invoice_service.update_invoice).
    rules = database.scalars(select(LearningRule).order_by(LearningRule.id.desc())).all()
    return {
        "version": ruleset_version(database),
        "rules": [serialize_rule(rule) for rule in rules],
        "policy": "Ninguna regla aprendida actúa hasta que la aprueba un administrador.",
    }


@router.post("/learning/rules/{rule_id}/{decision}", tags=["Agentes"])
def learning_rule_decide(rule_id: int, decision: str, payload: RuleDecision, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """Aprobar, rechazar o retirar una regla (solo administradores cuando hay usuarios)."""
    from app.learning import decide_rule
    from app.learning import serialize_rule
    from app.models import LearningRule

    rule = database.get(LearningRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Regla no encontrada.")
    try:
        decide_rule(database, rule, decision, normalize_actor(actor_header), payload.note)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    database.commit()
    return serialize_rule(rule)


@router.get("/agents/routing", tags=["Agentes"])
def agents_routing() -> dict[str, Any]:
    """Cuándo se llama a Claude: la política vigente por tipo de duda y de dónde sale."""
    from app.routing import load_policy

    return load_policy()


@router.get("/agents/pulse", tags=["Agentes"])
def agents_pulse(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.pulse import pulse_status

    return pulse_status(database)


@router.post("/agents/pulse/run", tags=["Agentes"])
def agents_pulse_run(database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """«Trabajar ahora»: buzón, pendientes, plazos, seguimiento y detector, en orden."""
    from app.agents.pulse import pulse_status
    from app.agents.pulse import run_cycle

    result = run_cycle(database, actor=normalize_actor(actor_header))
    database.commit()
    return {**result, "pulse": pulse_status(database)}


@router.get("/agents", tags=["Agentes"])
def agents(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.registry import agents_overview

    return agents_overview(database)


@router.get("/agents/runs", tags=["Agentes"])
def agent_runs(database: DatabaseDependency, limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    from app.agents.orchestrator import serialize_run

    runs = database.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)).all()
    return [serialize_run(run) for run in runs]


@router.post("/agents/process-pending", tags=["Agentes"])
def agents_process_pending(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.orchestrator import process_pending

    cases = process_pending(database, trigger="manual")
    database.commit()
    return {"processed": len(cases)}


@router.post("/agents/anomalies/scan", tags=["Agentes"])
def agents_scan(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.detector import run_anomaly_scan

    result = run_anomaly_scan(database, trigger="manual")
    database.commit()
    return result


@router.get("/agents/routes", tags=["Agentes"])
def agents_routes() -> dict[str, Any]:
    """Qué ruta sigue cada tipo de caso y el contrato de entrada/salida de cada agente."""
    from app.agents.orchestrator import AGENTS
    from app.agents.orchestrator import routes_overview

    return {"routes": routes_overview(), "contracts": [agent.contract() | {"name": agent.name} for agent in AGENTS.values()]}


@router.post("/agents/deadlines/watch", tags=["Agentes"])
def agents_deadlines(database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.orchestrator import watch_deadlines

    cases = watch_deadlines(database, trigger="manual")
    database.commit()
    return {"created": len(cases), "cases": [{"id": case.id, "code": case.code, "title": case.title} for case in cases]}


class EventNotification(BaseModel):
    issuer: str | None = Field(default=None, max_length=30)
    notification_type: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)
    amount: float | None = None
    available_at: date | None = None
    notified_at: date | None = None
    deadline: date | None = None


class EventIn(BaseModel):
    kind: str = Field(pattern="^(notification|invoice|deadline)$")
    source: str = Field(default="api", max_length=40)
    external_id: str | None = Field(default=None, max_length=255)
    notification: EventNotification | None = None
    invoice_id: int | None = None
    model: str | None = Field(default=None, pattern="^(303|130|111|115)$")
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    due: date | None = None


@router.post("/events", tags=["Agentes"], status_code=201)
def post_event(payload: EventIn, database: DatabaseDependency, response: Response) -> dict[str, Any]:
    """Entrada común para cualquier fuente (DEHú, correo, banco, plazos…):
    evento → Vigilante → Expediente → Orquestador → agentes → Director → humano.

    Idempotente: la misma pareja (source, external_id) no se procesa dos veces.
    """
    from app.agents.intake import ingest
    from app.agents.intake import payload_hash
    from app.agents.intake import serialize_event
    from app.agents.orchestrator import serialize_run
    from app.case_service import serialize_case
    from app.notification_service import ISSUERS
    from app.notification_service import NOTIFICATION_TYPES

    if payload.kind == "notification":
        if payload.notification is None:
            raise HTTPException(status_code=422, detail="Falta la notificación.")
        data = payload.notification.model_dump(mode="json")
        if data.get("issuer") and data["issuer"] not in ISSUERS:
            raise HTTPException(status_code=422, detail="Organismo no válido.")
        if data.get("notification_type") and data["notification_type"] not in NOTIFICATION_TYPES:
            raise HTTPException(status_code=422, detail="Tipo de notificación no válido.")
        body = {"notification": data}
        external_id = payload.external_id or f"sha256:{payload_hash(body)}"
    elif payload.kind == "invoice":
        if payload.invoice_id is None:
            raise HTTPException(status_code=422, detail="Falta invoice_id.")
        body = {"invoice_id": payload.invoice_id}
        external_id = payload.external_id or f"invoice:{payload.invoice_id}"
    else:
        if not (payload.model and payload.year and payload.quarter):
            raise HTTPException(status_code=422, detail="Faltan modelo, año y trimestre.")
        from app.tax_service import quarterly_due_date

        due = payload.due or quarterly_due_date(payload.year, payload.quarter)
        body = {"model": payload.model, "year": payload.year, "quarter": payload.quarter, "due": due.isoformat()}
        external_id = payload.external_id or f"{payload.model}:{payload.year}-{payload.quarter}"

    from app.agents.intake import with_retry

    def work():
        result = ingest(database, source=payload.source, external_id=external_id, kind=payload.kind, payload=body)
        database.commit()
        return result

    event, duplicate, case = with_retry(database, work)
    if duplicate:
        response.status_code = 200
    run = database.get(AgentRun, event.run_id) if event.run_id else None
    return {
        "event": serialize_event(event),
        "duplicate": duplicate,
        "case": serialize_case(database, case, full=True) if case else None,
        "run": serialize_run(run) if run else None,
    }


@router.get("/events", tags=["Agentes"])
def list_events(database: DatabaseDependency, status: str | None = Query(default=None, max_length=20), limit: int = Query(default=30, ge=1, le=200)) -> dict[str, Any]:
    from app.agents.intake import STATUS_LABELS
    from app.agents.intake import serialize_event
    from app.models import IngestedEvent
    from sqlalchemy import func

    statement = select(IngestedEvent).order_by(IngestedEvent.id.desc()).limit(limit)
    if status:
        statement = statement.where(IngestedEvent.status == status)
    counts = dict(database.execute(select(IngestedEvent.status, func.count()).group_by(IngestedEvent.status)).all())
    return {
        "events": [serialize_event(item) for item in database.scalars(statement).all()],
        "counts": {key: counts.get(key, 0) for key in STATUS_LABELS},
    }


@router.post("/events/{event_id}/retry", tags=["Agentes"])
def retry_event(event_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.intake import retry
    from app.agents.intake import serialize_event
    from app.case_service import serialize_case

    from app.agents.intake import with_retry

    def work():
        result = retry(database, event_id)
        database.commit()
        return result

    try:
        event, case = with_retry(database, work)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"event": serialize_event(event), "case": serialize_case(database, case, full=True) if case else None}


class DehuPayload(BaseModel):
    identifier: str = Field(min_length=1, max_length=200)
    issuer: str = Field(default="", max_length=255)
    subject: str = Field(default="", max_length=2000)
    holder_tax_id: str | None = Field(default=None, max_length=20)
    kind: str = Field(default="notificacion", pattern="^(notificacion|comunicacion)$")
    available_at: date | None = None
    accessed_at: date | None = None
    pdf_base64: str | None = None
    filename: str | None = Field(default=None, max_length=255)


@router.post("/connectors/dehu/import", tags=["Conectores"], status_code=201)
def dehu_import(payload: DehuPayload, database: DatabaseDependency) -> dict[str, Any]:
    """Una notificación de la DEHú (metadatos + PDF opcional) → expediente."""
    from app.connectors.dehu.adapter import DehuItem
    from app.connectors.dehu.adapter import ingest_dehu

    return ingest_dehu(database, DehuItem.from_payload(payload.model_dump()))


@router.post("/connectors/dehu/poll", tags=["Conectores"])
def dehu_poll(database: DatabaseDependency) -> dict[str, Any]:
    """Procesa la carpeta de notificaciones descargadas de la DEHú (DEHU_INBOX_DIR)."""
    from pathlib import Path

    from app.config import settings
    from app.connectors.dehu.client import FolderTransport
    from app.connectors.dehu.client import poll

    from app.connectors.dehu.client import DehuUnavailable

    from app.connectors.assignment import blocked_reason

    if not settings.dehu_inbox_dir:
        raise HTTPException(status_code=400, detail="Configura DEHU_INBOX_DIR con la carpeta de notificaciones descargadas de la DEHú.")
    reason = blocked_reason(database, "dehu")
    if reason:  # la carpeta es común a la instalación: solo para el cliente asignado
        raise HTTPException(status_code=409, detail=reason)
    try:
        return poll(database, FolderTransport(Path(settings.dehu_inbox_dir)))
    except DehuUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/connectors/email", tags=["Conectores"])
def email_connector_status(database: DatabaseDependency) -> dict[str, Any]:
    from app.connectors.email.client import status

    return status(database)


@router.post("/connectors/email/import", tags=["Conectores"], status_code=201)
async def email_connector_import(database: DatabaseDependency, uploaded_file: UploadFile = File(...)) -> dict[str, Any]:
    """Importa un correo (.eml): cada adjunto entra por la entrada común."""
    from app.connectors.email.client import import_eml

    if not (uploaded_file.filename or "").lower().endswith(".eml"):
        raise HTTPException(status_code=422, detail="Sube un correo en formato .eml.")
    raw = await uploaded_file.read()
    if len(raw) > settings.max_upload_size * 3:
        raise HTTPException(status_code=413, detail="El correo es demasiado grande.")
    result = import_eml(database, raw)
    database.commit()
    return result


@router.post("/connectors/email/poll", tags=["Conectores"])
def email_connector_poll(database: DatabaseDependency) -> dict[str, Any]:
    from app.connectors.assignment import SourceNotAssigned
    from app.connectors.email.client import poll

    try:
        result = poll(database)
    except SourceNotAssigned as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=502, detail=f"No se pudo leer el buzón: {error}") from error
    database.commit()
    return result


@router.get("/memory/profiles", tags=["Agentes"])
def memory_profiles(database: DatabaseDependency, party: str | None = Query(default=None, pattern="^(supplier|customer)$"), refresh: bool = False) -> list[dict[str, Any]]:
    """Memoria financiera: cómo se comporta normalmente cada proveedor y cliente."""
    from sqlalchemy import select

    from app.financial_memory import compute_profiles
    from app.financial_memory import serialize
    from app.models import CounterpartyProfile

    def ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda item: -(item.get("amount") or {}).get("total_12m", 0))

    if refresh or database.scalar(select(CounterpartyProfile.id).limit(1)) is None:
        # Calculado al vuelo, sin guardar: una lectura no escribe (los guarda el Detector en su barrido).
        return ranked([{"party": kind, "key": key, "name": name, **profile, "updated_at": None}
                       for kind, key, name, profile in compute_profiles(database) if not party or kind == party])
    statement = select(CounterpartyProfile)
    if party:
        statement = statement.where(CounterpartyProfile.party == party)
    return ranked([serialize(item) for item in database.scalars(statement).all()])


@router.get("/memory/profiles/{party}/{key}", tags=["Agentes"])
def memory_profile(party: str, key: str, database: DatabaseDependency) -> dict[str, Any]:
    from sqlalchemy import select

    from app.financial_memory import serialize
    from app.models import CounterpartyProfile

    record = database.scalar(select(CounterpartyProfile).where(CounterpartyProfile.party == party, CounterpartyProfile.key == key))
    if record is None:
        raise HTTPException(status_code=404, detail="Sin perfil para esa contraparte.")
    return serialize(record)


@router.get("/memory/search", tags=["Agentes"])
def memory_search(database: DatabaseDependency, q: str = Query(..., min_length=2, max_length=300)) -> list[dict[str, Any]]:
    from app.agents.memory import search

    return search(database, q, limit=8)


class AskPayload(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/memory/ask", tags=["Agentes"])
def memory_ask(payload: AskPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.agents.memory import answer

    return answer(database, payload.question)


# -------------------------------------------------------------------
# Portal de subida (enlace personal, sin usuario)
# -------------------------------------------------------------------


@router.get("/portal/{token}", tags=["Portal"])
def portal_get(token: str, database: DatabaseDependency) -> dict[str, Any]:
    from app.case_service import portal_info
    from app.case_service import request_by_token

    request = request_by_token(database, token)
    if request is None:
        raise HTTPException(status_code=404, detail="Enlace no válido o caducado.")
    return portal_info(database, request)


@router.post("/portal/{token}", tags=["Portal"])
async def portal_upload(token: str, database: DatabaseDependency, uploaded_file: UploadFile = File(...)) -> dict[str, Any]:
    from app.case_service import CaseError
    from app.case_service import receive_upload
    from app.case_service import request_by_token

    request = request_by_token(database, token)
    if request is None:
        raise HTTPException(status_code=404, detail="Enlace no válido o caducado.")
    content = await uploaded_file.read()
    try:
        result = receive_upload(database, request, filename=uploaded_file.filename or "documento", content=content, content_type=uploaded_file.content_type)
    except CaseError as error:
        raise unprocessable(error) from error
    database.commit()
    return result


@portal_router.get("/portal/{token}", include_in_schema=False)
def portal_page(token: str) -> HTMLResponse:
    from pathlib import Path

    page = Path(__file__).resolve().parent / "static" / "portal.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


@router.get("/invoices/{invoice_id}/memory", tags=["Agentes"])
def invoice_memory(invoice_id: int, database: DatabaseDependency) -> dict[str, Any]:
    """«Esto es raro para ti»: la factura comparada con lo que ese proveedor o cliente hace normalmente contigo."""
    from app.business_memory import unusual
    from app.models import Invoice

    invoice = database.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    return unusual(database, invoice)


@router.get("/search", tags=["Agentes"])
def universal_search(database: DatabaseDependency, q: str = Query(..., min_length=1, max_length=120)) -> dict[str, Any]:
    """Búsqueda universal: facturas, terceros, expedientes, movimientos, documentos, modelos fiscales y cierres. Solo lee."""
    from app.search import search

    return search(database, q)
