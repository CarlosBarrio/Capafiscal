from __future__ import annotations

import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from typing import Any

from fastapi import Body
from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.inspection import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.database import create_database_tables
from app.database import get_db
from app.invoice_service import add_audit_event
from app.invoice_service import approve_invoice
from app.invoice_service import get_document
from app.invoice_service import get_invoice
from app.invoice_service import process_document
from app.invoice_service import reject_invoice
from app.invoice_service import update_invoice
from app.models import AuditEvent
from app.models import Document
from app.models import Invoice
from app.operations_service import assistant_answer
from app.operations_service import build_agent_catalog
from app.operations_service import build_monthly_impact
from app.operations_service import build_today_dashboard
from app.operations_service import connector_catalog
from app.operations_service import list_open_risks
from app.outlook_connector import router as outlook_router
from app.schemas import ActionResponse
from app.schemas import AgentResponse
from app.schemas import AssistantQueryRequest
from app.schemas import AssistantQueryResponse
from app.schemas import AuditEventResponse
from app.schemas import ConnectorResponse
from app.schemas import DashboardTodayResponse
from app.schemas import DocumentDetail
from app.schemas import DocumentListItem
from app.schemas import InvoiceRejectRequest
from app.schemas import InvoiceResponse
from app.schemas import InvoiceUpdate
from app.schemas import MonthlyImpactResponse
from app.schemas import RiskResponse
from app.schemas import UploadResponse
from app.task_schemas import TaskActionResponse
from app.task_schemas import TaskResolveRequest
from app.task_schemas import TaskResponse
from app.task_service import get_task
from app.task_service import list_review_tasks
from app.task_service import resolve_task
from app.task_service import start_task
from app.task_service import synchronize_all_review_tasks


APP_DIRECTORY = Path(__file__).resolve().parent
STATIC_DIRECTORY = APP_DIRECTORY / "static"

UPLOAD_CHUNK_SIZE = 1024 * 1024

SAFE_ACTOR_PATTERN = re.compile(
    r"[^a-zA-Z0-9@._\-\s]"
)


class InvoiceApprovalRequest(BaseModel):
    force: bool = False


DatabaseDependency = Annotated[
    Session,
    Depends(get_db),
]

ActorHeader = Annotated[
    str | None,
    Header(alias="X-Actor"),
]


def orm_to_dict(
    instance: Any,
) -> dict[str, Any] | None:
    if instance is None:
        return None

    mapper = sqlalchemy_inspect(instance).mapper

    return jsonable_encoder(
        {
            attribute.key: getattr(instance, attribute.key)
            for attribute in mapper.column_attrs
        }
    )


def normalize_actor(
    actor: str | None,
) -> str:
    if not actor:
        return "usuario-local"

    cleaned = SAFE_ACTOR_PATTERN.sub(
        "",
        actor,
    ).strip()

    return cleaned[:100] or "usuario-local"


def clean_original_filename(
    filename: str | None,
) -> str:
    if not filename:
        return "documento-sin-nombre"

    cleaned = Path(filename).name.strip()

    return cleaned[:255] or "documento-sin-nombre"


def get_document_file_path(
    document: Document,
) -> Path:
    upload_directory = settings.upload_dir.resolve()

    file_path = (
        upload_directory / document.stored_filename
    ).resolve()

    try:
        file_path.relative_to(upload_directory)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La ruta almacenada para el documento no es válida.",
        ) from error

    return file_path


def validate_file_signature(
    *,
    file_path: Path,
    extension: str,
) -> None:
    if extension == ".pdf":
        with file_path.open("rb") as file_handle:
            signature = file_handle.read(5)

        if signature != b"%PDF-":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "El archivo tiene extensión PDF, pero su contenido "
                    "no corresponde a un PDF válido."
                ),
            )

        return

    if extension == ".txt":
        with file_path.open("rb") as file_handle:
            sample = file_handle.read(8192)

        if b"\x00" in sample:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "El archivo TXT contiene datos binarios y no puede "
                    "procesarse como texto."
                ),
            )

        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"El tipo de archivo {extension} no está permitido.",
    )


def response_media_type(
    document: Document,
) -> str:
    if document.extension == ".pdf":
        return "application/pdf"

    if document.extension == ".txt":
        return "text/plain; charset=utf-8"

    return "application/octet-stream"


def check_invoice_is_editable(
    invoice: Invoice,
) -> None:
    if invoice.review_status == "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "La factura ya está aprobada y no puede modificarse "
                "con esta operación."
            ),
        )


def get_missing_invoice_fields(
    invoice: Invoice,
) -> list[dict[str, str]]:
    fields = (
        ("supplier_name", "Nombre del proveedor"),
        ("supplier_tax_id", "NIF/CIF del proveedor"),
        ("invoice_number", "Número de factura"),
        ("invoice_date", "Fecha de factura"),
        ("subtotal", "Base imponible"),
        ("tax_total", "Importe de IVA"),
        ("total", "Importe total"),
        ("currency", "Moneda"),
    )

    missing_fields: list[dict[str, str]] = []

    for field_name, label in fields:
        value = getattr(invoice, field_name, None)

        is_missing = value is None

        if isinstance(value, str):
            is_missing = not value.strip()

        if is_missing:
            missing_fields.append(
                {
                    "field": field_name,
                    "label": label,
                }
            )

    return missing_fields


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings.data_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    settings.upload_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    create_database_tables()

    database = SessionLocal()

    try:
        synchronize_all_review_tasks(database)
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()

    yield


app = FastAPI(
    title=settings.app_name,
    description=(
        "Centro operativo para carga, extracción, revisión, "
        "riesgos, agentes y aprobación de facturas."
    ),
    version="1.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.include_router(outlook_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=False,
    allow_methods=[
        "GET",
        "POST",
        "PATCH",
        "OPTIONS",
    ],
    allow_headers=[
        "Accept",
        "Content-Type",
        "X-Actor",
    ],
)


# -------------------------------------------------------------------
# Sistema
# -------------------------------------------------------------------

@app.get(
    "/api/health",
    tags=["Sistema"],
)
def health_check() -> dict[str, Any]:
    return {
        "success": True,
        "application": settings.app_name,
        "environment": settings.app_environment,
        "database": (
            "sqlite"
            if settings.database_url.startswith("sqlite")
            else "external"
        ),
        "demo_connectors_enabled": settings.enable_demo_connectors,
    }


# -------------------------------------------------------------------
# Centro operativo: dashboard, agentes, riesgos y asistente
# -------------------------------------------------------------------

@app.get(
    "/api/dashboard/today",
    response_model=DashboardTodayResponse,
    tags=["Centro operativo"],
)
def dashboard_today(
    database: DatabaseDependency,
) -> dict[str, Any]:
    synchronize_all_review_tasks(database)
    database.commit()

    return build_today_dashboard(database)


@app.get(
    "/api/dashboard/monthly-impact",
    response_model=MonthlyImpactResponse,
    tags=["Centro operativo"],
)
def dashboard_monthly_impact(
    database: DatabaseDependency,
) -> dict[str, Any]:
    return build_monthly_impact(database)


@app.get(
    "/api/agents",
    response_model=list[AgentResponse],
    tags=["Centro operativo"],
)
def list_agents(
    database: DatabaseDependency,
) -> list[dict[str, Any]]:
    return build_agent_catalog(database)


@app.get(
    "/api/risks",
    response_model=list[RiskResponse],
    tags=["Centro operativo"],
)
def list_risks(
    database: DatabaseDependency,
    limit: int = Query(
        default=200,
        ge=1,
        le=500,
    ),
) -> list[dict[str, Any]]:
    synchronize_all_review_tasks(database)
    database.commit()

    return list_open_risks(
        database,
        limit=limit,
    )


@app.get(
    "/api/connectors/catalog",
    response_model=list[ConnectorResponse],
    tags=["Conectores"],
)
def list_connector_catalog(
    database: DatabaseDependency,
) -> list[dict[str, Any]]:
    return connector_catalog(database)


@app.post(
    "/api/assistant/query",
    response_model=AssistantQueryResponse,
    tags=["Asistente"],
)
def query_assistant(
    payload: AssistantQueryRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    actor = normalize_actor(actor_header)

    result = assistant_answer(
        database,
        payload.question,
    )

    add_audit_event(
        database,
        action="assistant.query",
        entity_type="assistant",
        entity_id="internal-data",
        actor=actor,
        event_data={
            "question": payload.question[:500],
            "mode": result.get("mode"),
            "sources_count": len(
                result.get("sources", [])
            ),
        },
    )

    database.commit()

    return result


# -------------------------------------------------------------------
# Carga manual
# -------------------------------------------------------------------

@app.post(
    "/api/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Documentos"],
)
async def upload_document(
    database: DatabaseDependency,
    uploaded_file: UploadFile = File(...),
    actor_header: ActorHeader = None,
) -> UploadResponse:
    actor = normalize_actor(actor_header)

    settings.upload_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_filename = clean_original_filename(
        uploaded_file.filename
    )

    extension = Path(original_filename).suffix.lower()

    if not extension:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo no tiene extensión.",
        )

    if extension not in settings.allowed_extension_set:
        allowed = ", ".join(
            sorted(settings.allowed_extension_set)
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Extensión no permitida: {extension}. "
                f"Extensiones permitidas: {allowed}."
            ),
        )

    content_type = (
        uploaded_file.content_type[:150]
        if uploaded_file.content_type
        else None
    )

    temporary_path = (
        settings.upload_dir
        / f".upload-{uuid.uuid4().hex}.tmp"
    )

    sha256 = hashlib.sha256()
    total_size = 0

    try:
        with temporary_path.open("wb") as destination:
            while True:
                chunk = await uploaded_file.read(
                    UPLOAD_CHUNK_SIZE
                )

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > settings.max_upload_size:
                    raise HTTPException(
                        status_code=(
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                        ),
                        detail=(
                            "El archivo supera el tamaño máximo permitido "
                            f"de {settings.max_upload_size} bytes."
                        ),
                    )

                sha256.update(chunk)
                destination.write(chunk)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    finally:
        await uploaded_file.close()

    if total_size == 0:
        temporary_path.unlink(missing_ok=True)

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo está vacío.",
        )

    try:
        validate_file_signature(
            file_path=temporary_path,
            extension=extension,
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    file_hash = sha256.hexdigest()

    duplicate_document_id = database.scalar(
        select(Document.id)
        .where(Document.sha256 == file_hash)
        .limit(1)
    )

    if duplicate_document_id is not None:
        temporary_path.unlink(missing_ok=True)

        duplicate_document = get_document(
            database,
            duplicate_document_id,
        )

        if duplicate_document is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Se detectó un documento duplicado, pero no "
                    "pudo recuperarse."
                ),
            )

        add_audit_event(
            database,
            action="document.duplicate_upload_attempt",
            entity_type="document",
            entity_id=duplicate_document.id,
            actor=actor,
            event_data={
                "attempted_filename": original_filename,
                "sha256": file_hash,
                "size_bytes": total_size,
            },
        )

        database.commit()

        can_reprocess = (
            duplicate_document.invoice is None
            or duplicate_document.invoice.review_status != "APPROVED"
        )

        if can_reprocess:
            duplicate_file_path = get_document_file_path(
                duplicate_document
            )

            if duplicate_file_path.is_file():
                try:
                    process_document(
                        database,
                        document=duplicate_document,
                        file_path=duplicate_file_path,
                        actor=actor,
                    )
                except Exception as error:
                    database.rollback()

                    recovered_document = get_document(
                        database,
                        duplicate_document.id,
                    )

                    return UploadResponse(
                        success=True,
                        duplicate=True,
                        message=(
                            "El archivo ya existía y se intentó "
                            "reprocesar, pero la extracción falló. "
                            f"Error: {error}"
                        ),
                        document=recovered_document,
                    )

        refreshed_document = get_document(
            database,
            duplicate_document.id,
        )

        return UploadResponse(
            success=True,
            duplicate=True,
            message=(
                "El archivo ya existía y se ha vuelto "
                "a procesar correctamente."
                if can_reprocess
                else (
                    "El archivo ya existía. No se ha reprocesado "
                    "porque la factura está aprobada."
                )
            ),
            document=refreshed_document,
        )

    stored_filename = f"{uuid.uuid4().hex}{extension}"
    final_file_path = settings.upload_dir / stored_filename

    document: Document | None = None

    try:
        os.replace(
            temporary_path,
            final_file_path,
        )

        document = Document(
            original_filename=original_filename,
            stored_filename=stored_filename,
            sha256=file_hash,
            mime_type=content_type,
            extension=extension,
            size_bytes=total_size,
            source="manual_upload",
            source_provider=None,
            external_id=None,
            is_demo=False,
            status="RECEIVED",
            extraction_status="PENDING",
            requires_ocr=False,
            failure_reason=None,
        )

        database.add(document)
        database.flush()

        add_audit_event(
            database,
            action="document.uploaded",
            entity_type="document",
            entity_id=document.id,
            actor=actor,
            event_data={
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "sha256": file_hash,
                "size_bytes": total_size,
                "mime_type": content_type,
                "extension": extension,
                "source": "manual_upload",
            },
        )

        database.commit()

    except Exception as error:
        database.rollback()
        temporary_path.unlink(missing_ok=True)
        final_file_path.unlink(missing_ok=True)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "No se pudo guardar el documento. "
                f"Error: {error}"
            ),
        ) from error

    if document is None or document.id is None:
        final_file_path.unlink(missing_ok=True)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "El archivo se guardó, pero no se pudo crear "
                "el registro del documento."
            ),
        )

    document_id = document.id

    try:
        process_document(
            database,
            document=document,
            file_path=final_file_path,
            actor=actor,
        )
    except Exception as error:
        database.rollback()

        failed_document = get_document(
            database,
            document_id,
        )

        if failed_document is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "El archivo fue guardado, pero no pudo recuperarse "
                    "después del error de extracción."
                ),
            ) from error

        return UploadResponse(
            success=True,
            duplicate=False,
            message=(
                "El archivo se ha guardado correctamente, pero la "
                "extracción automática ha fallado. "
                f"Error: {error}"
            ),
            document=failed_document,
        )

    processed_document = get_document(
        database,
        document_id,
    )

    if processed_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "El documento fue procesado, pero no pudo "
                "recuperarse desde la base de datos."
            ),
        )

    if processed_document.requires_ocr:
        message = (
            "El archivo se ha subido correctamente, pero necesita "
            "OCR o revisión manual."
        )
    elif processed_document.invoice is None:
        message = (
            "El archivo se ha subido correctamente, pero no se ha "
            "podido identificar como factura."
        )
    else:
        message = "Factura subida y procesada correctamente."

    return UploadResponse(
        success=True,
        duplicate=False,
        message=message,
        document=processed_document,
    )


# -------------------------------------------------------------------
# Documentos
# -------------------------------------------------------------------

@app.get(
    "/api/documents",
    response_model=list[DocumentListItem],
    tags=["Documentos"],
)
def list_documents(
    database: DatabaseDependency,
    document_status: str | None = Query(
        default=None,
        alias="status",
    ),
    source: str | None = Query(default=None),
    is_demo: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Document]:
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
        .offset(offset)
        .limit(limit)
    )

    if document_status:
        statement = statement.where(
            Document.status == document_status.strip().upper()
        )

    if source:
        statement = statement.where(
            Document.source == source.strip()
        )

    if is_demo is not None:
        statement = statement.where(
            Document.is_demo.is_(is_demo)
        )

    return list(database.scalars(statement).all())


@app.get(
    "/api/documents/{document_id}",
    response_model=DocumentDetail,
    tags=["Documentos"],
)
def document_detail(
    document_id: int,
    database: DatabaseDependency,
) -> Document:
    document = get_document(
        database,
        document_id,
    )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Documento no encontrado.",
        )

    return document


@app.get(
    "/api/documents/{document_id}/file",
    response_class=FileResponse,
    tags=["Documentos"],
)
def document_file(
    document_id: int,
    database: DatabaseDependency,
) -> FileResponse:
    document = get_document(
        database,
        document_id,
    )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Documento no encontrado.",
        )

    file_path = get_document_file_path(document)

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "El registro existe, pero el archivo original "
                "no se encuentra en el disco."
            ),
        )

    return FileResponse(
        path=file_path,
        media_type=response_media_type(document),
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@app.post(
    "/api/documents/{document_id}/reprocess",
    response_model=ActionResponse,
    tags=["Documentos"],
)
def reprocess_document(
    document_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> ActionResponse:
    actor = normalize_actor(actor_header)

    document = get_document(
        database,
        document_id,
    )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Documento no encontrado.",
        )

    if document.invoice is not None:
        check_invoice_is_editable(document.invoice)

    file_path = get_document_file_path(document)

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No se encuentra el archivo original necesario "
                "para reprocesar."
            ),
        )

    add_audit_event(
        database,
        action="document.reprocess.requested",
        entity_type="document",
        entity_id=document.id,
        actor=actor,
        event_data={
            "previous_status": document.status,
            "previous_extraction_status": (
                document.extraction_status
            ),
        },
    )

    database.commit()

    try:
        process_document(
            database,
            document=document,
            file_path=file_path,
            actor=actor,
        )
    except Exception as error:
        database.rollback()

        failed_document = get_document(
            database,
            document.id,
        )

        return ActionResponse(
            success=False,
            message=f"El reprocesamiento falló. Error: {error}",
            document=failed_document,
        )

    processed_document = get_document(
        database,
        document.id,
    )

    if processed_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "El documento se reprocesó, pero no pudo recuperarse."
            ),
        )

    return ActionResponse(
        success=True,
        message="Documento reprocesado correctamente.",
        document=processed_document,
        invoice=processed_document.invoice,
    )


# -------------------------------------------------------------------
# Facturas
# -------------------------------------------------------------------

@app.patch(
    "/api/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    tags=["Facturas"],
)
def patch_invoice(
    invoice_id: int,
    payload: InvoiceUpdate,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> Invoice:
    actor = normalize_actor(actor_header)

    invoice = get_invoice(
        database,
        invoice_id,
    )

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Factura no encontrada.",
        )

    check_invoice_is_editable(invoice)

    try:
        updated_invoice = update_invoice(
            database,
            invoice=invoice,
            payload=payload,
            actor=actor,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    complete_invoice = get_invoice(
        database,
        updated_invoice.id,
    )

    if complete_invoice is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "La factura se actualizó, pero no pudo recuperarse."
            ),
        )

    return complete_invoice


@app.post(
    "/api/invoices/{invoice_id}/approve",
    tags=["Facturas"],
)
def approve_invoice_endpoint(
    invoice_id: int,
    database: DatabaseDependency,
    payload: InvoiceApprovalRequest | None = Body(default=None),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    actor = normalize_actor(actor_header)
    force_approval = payload.force if payload is not None else False

    invoice = get_invoice(
        database,
        invoice_id,
    )

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "INVOICE_NOT_FOUND",
                "message": "Factura no encontrada.",
            },
        )

    document = invoice.document

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "DOCUMENT_NOT_FOUND",
                "message": (
                    "No se encontró el documento asociado a la factura."
                ),
            },
        )

    if invoice.review_status == "APPROVED":
        missing_fields = get_missing_invoice_fields(invoice)

        return {
            "success": True,
            "already_approved": True,
            "approved_with_warnings": bool(missing_fields),
            "message": "La factura ya estaba aprobada.",
            "missing_fields": missing_fields,
            "invoice": orm_to_dict(invoice),
            "document": orm_to_dict(document),
        }

    if invoice.review_status == "REJECTED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVOICE_REJECTED",
                "message": (
                    "La factura está rechazada. Debe reabrirse antes "
                    "de poder aprobarla."
                ),
                "can_force_approval": False,
            },
        )

    missing_fields = get_missing_invoice_fields(invoice)

    if missing_fields and not force_approval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "MISSING_FIELDS",
                "message": (
                    "La factura tiene campos sin completar. Puedes "
                    "añadirlos o aprobar con advertencias."
                ),
                "missing_fields": missing_fields,
                "can_force_approval": True,
            },
        )

    if missing_fields and force_approval:
        invoice.review_status = "APPROVED"
        invoice.approved_at = datetime.now(timezone.utc)
        invoice.rejected_at = None
        invoice.rejection_reason = None

        invoice.validation_status = "INCOMPLETE"
        invoice.validation_messages = [
            {
                "code": "MISSING_FIELD",
                "field": item["field"],
                "message": f"Falta el campo: {item['label']}.",
            }
            for item in missing_fields
        ]

        document.status = "APPROVED"

        add_audit_event(
            database,
            action="invoice.approved_with_warnings",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            event_data={
                "forced": True,
                "missing_fields": missing_fields,
            },
        )

        add_audit_event(
            database,
            action="document.approved_with_warnings",
            entity_type="document",
            entity_id=document.id,
            actor=actor,
            event_data={
                "invoice_id": invoice.id,
                "forced": True,
                "missing_fields": missing_fields,
            },
        )

        database.commit()
        database.refresh(invoice)
        database.refresh(document)

    else:
        try:
            approve_invoice(
                database,
                invoice=invoice,
                actor=actor,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "APPROVAL_CONFLICT",
                    "message": str(error),
                    "can_force_approval": False,
                },
            ) from error

    complete_document = get_document(
        database,
        document.id,
    )

    if complete_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo recuperar la factura aprobada.",
        )

    return {
        "success": True,
        "already_approved": False,
        "approved_with_warnings": bool(missing_fields),
        "message": (
            "Factura aprobada con campos pendientes."
            if missing_fields
            else "Factura aprobada correctamente."
        ),
        "missing_fields": missing_fields,
        "invoice": orm_to_dict(complete_document.invoice),
        "document": orm_to_dict(complete_document),
    }


@app.post(
    "/api/invoices/{invoice_id}/reject",
    response_model=ActionResponse,
    tags=["Facturas"],
)
def reject_invoice_endpoint(
    invoice_id: int,
    payload: InvoiceRejectRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> ActionResponse:
    actor = normalize_actor(actor_header)

    invoice = get_invoice(
        database,
        invoice_id,
    )

    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Factura no encontrada.",
        )

    if invoice.review_status == "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Una factura aprobada no puede rechazarse sin "
                "un flujo previo de reapertura."
            ),
        )

    reason = payload.reason.strip()

    try:
        rejected_invoice = reject_invoice(
            database,
            invoice=invoice,
            reason=reason,
            actor=actor,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    complete_document = get_document(
        database,
        rejected_invoice.document_id,
    )

    if complete_document is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo recuperar la factura rechazada.",
        )

    return ActionResponse(
        success=True,
        message="Factura rechazada correctamente.",
        document=complete_document,
        invoice=complete_document.invoice,
    )


# -------------------------------------------------------------------
# Auditoría
# -------------------------------------------------------------------

@app.get(
    "/api/documents/{document_id}/audit",
    response_model=list[AuditEventResponse],
    tags=["Auditoría"],
)
def document_audit(
    document_id: int,
    database: DatabaseDependency,
) -> list[AuditEventResponse]:
    document = get_document(
        database,
        document_id,
    )

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Documento no encontrado.",
        )

    conditions = [
        (
            (AuditEvent.entity_type == "document")
            & (AuditEvent.entity_id == str(document.id))
        )
    ]

    if document.invoice is not None:
        conditions.append(
            (
                (AuditEvent.entity_type == "invoice")
                & (
                    AuditEvent.entity_id
                    == str(document.invoice.id)
                )
            )
        )

    statement = (
        select(AuditEvent)
        .where(or_(*conditions))
        .order_by(
            AuditEvent.created_at.desc(),
            AuditEvent.id.desc(),
        )
    )

    events = database.scalars(statement).all()

    return [
        AuditEventResponse.model_validate(
            event,
            from_attributes=True,
        )
        for event in events
    ]


@app.get(
    "/api/activity",
    response_model=list[AuditEventResponse],
    tags=["Auditoría"],
)
def activity_feed(
    database: DatabaseDependency,
    entity_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventResponse]:
    statement = select(AuditEvent)

    if entity_type:
        statement = statement.where(
            AuditEvent.entity_type == entity_type.strip().lower()
        )

    if action:
        normalized_action = action.strip()

        if normalized_action.endswith("*"):
            statement = statement.where(
                AuditEvent.action.like(
                    f"{normalized_action[:-1]}%"
                )
            )
        else:
            statement = statement.where(
                AuditEvent.action == normalized_action
            )

    if actor:
        statement = statement.where(
            AuditEvent.actor.like(
                f"%{actor.strip()}%"
            )
        )

    statement = (
        statement
        .order_by(
            AuditEvent.created_at.desc(),
            AuditEvent.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )

    events = database.scalars(statement).all()

    return [
        AuditEventResponse.model_validate(
            event,
            from_attributes=True,
        )
        for event in events
    ]


# -------------------------------------------------------------------
# Tareas
# -------------------------------------------------------------------

@app.get(
    "/api/tasks/review-inbox",
    response_model=list[TaskResponse],
    tags=["Tareas"],
)
def review_task_inbox(
    database: DatabaseDependency,
    task_status: str | None = Query(
        default=None,
        alias="status",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=200,
    ),
) -> list[Any]:
    synchronize_all_review_tasks(database)
    database.commit()

    return list_review_tasks(
        database,
        task_status=task_status,
        limit=limit,
    )


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskResponse,
    tags=["Tareas"],
)
def task_detail(
    task_id: int,
    database: DatabaseDependency,
) -> Any:
    task = get_task(
        database,
        task_id,
    )

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tarea no encontrada.",
        )

    return task


@app.post(
    "/api/tasks/{task_id}/start",
    response_model=TaskActionResponse,
    tags=["Tareas"],
)
def start_review_task(
    task_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> TaskActionResponse:
    actor = normalize_actor(actor_header)

    task = get_task(
        database,
        task_id,
    )

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tarea no encontrada.",
        )

    try:
        start_task(
            database,
            task=task,
            actor=actor,
        )
        database.commit()

    except ValueError as error:
        database.rollback()

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    complete_task = get_task(
        database,
        task_id,
    )

    if complete_task is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="La tarea se inició, pero no pudo recuperarse.",
        )

    return TaskActionResponse(
        success=True,
        message="Tarea de revisión iniciada.",
        task=complete_task,
    )


@app.post(
    "/api/tasks/{task_id}/resolve",
    response_model=TaskActionResponse,
    tags=["Tareas"],
)
def resolve_review_task(
    task_id: int,
    payload: TaskResolveRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> TaskActionResponse:
    actor = normalize_actor(actor_header)

    task = get_task(
        database,
        task_id,
    )

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tarea no encontrada.",
        )

    try:
        resolve_task(
            database,
            task=task,
            actor=actor,
            resolution=payload.resolution,
            notes=payload.notes,
        )

        database.commit()

    except Exception:
        database.rollback()
        raise

    complete_task = get_task(
        database,
        task_id,
    )

    if complete_task is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="La tarea se resolvió, pero no pudo recuperarse.",
        )

    return TaskActionResponse(
        success=True,
        message="Tarea resuelta correctamente.",
        task=complete_task,
    )


# -------------------------------------------------------------------
# Frontend
# -------------------------------------------------------------------

@app.get(
    "/",
    include_in_schema=False,
)
def frontend_home() -> FileResponse:
    index_path = STATIC_DIRECTORY / "index.html"

    if not index_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encuentra app/static/index.html.",
        )

    return FileResponse(index_path)


app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIRECTORY)),
    name="static",
)