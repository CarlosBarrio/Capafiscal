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
    event(database, case, f"Documento aportado: {attachment.filename}", data={"attachment_id": attachment.id})
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

    data = daily_briefing(database)
    database.commit()
    return data


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
    notification: EventNotification | None = None
    invoice_id: int | None = None
    model: str | None = Field(default=None, pattern="^(303|130|111|115)$")
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    due: date | None = None


@router.post("/events", tags=["Agentes"], status_code=201)
def post_event(payload: EventIn, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    """Entrada común para cualquier fuente (DEHú, correo, banco, plazos…):
    evento → Vigilante → Expediente → Orquestador → agentes → Director → humano."""
    from app.agents.base import Event
    from app.agents.orchestrator import process_deadline
    from app.agents.orchestrator import process_event
    from app.agents.orchestrator import process_invoice
    from app.case_service import serialize_case
    from app.notification_service import ISSUERS
    from app.notification_service import NOTIFICATION_TYPES
    from app.notification_service import create_notification

    case = None
    if payload.kind == "notification":
        if payload.notification is None:
            raise HTTPException(status_code=422, detail="Falta la notificación.")
        data = payload.notification.model_dump()
        if data.get("issuer") and data["issuer"] not in ISSUERS:
            raise HTTPException(status_code=422, detail="Organismo no válido.")
        if data.get("notification_type") and data["notification_type"] not in NOTIFICATION_TYPES:
            raise HTTPException(status_code=422, detail="Tipo de notificación no válido.")
        notification = create_notification(database, document=None, data=data, actor=normalize_actor(actor_header) or payload.source)
        database.flush()
        case = process_event(database, Event("notification", source=payload.source, ref_id=notification.id), trigger=payload.source)
    elif payload.kind == "invoice":
        if payload.invoice_id is None:
            raise HTTPException(status_code=422, detail="Falta invoice_id.")
        case = process_invoice(database, payload.invoice_id, trigger=payload.source)
    else:
        if not (payload.model and payload.year and payload.quarter):
            raise HTTPException(status_code=422, detail="Faltan modelo, año y trimestre.")
        from app.tax_service import quarterly_due_date

        due = payload.due or quarterly_due_date(payload.year, payload.quarter)
        case = process_deadline(database, model=payload.model, year=payload.year, quarter=payload.quarter, due=due, trigger=payload.source)
    database.commit()
    run = database.scalar(select(AgentRun).order_by(AgentRun.id.desc()).limit(1))
    from app.agents.orchestrator import serialize_run

    return {"case": serialize_case(database, case, full=True) if case else None, "run": serialize_run(run) if run else None}


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
