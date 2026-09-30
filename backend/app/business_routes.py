"""
API de las áreas de negocio: empresa, impuestos, notificaciones, banco y
tesorería, cumplimiento, agenda y memoria del agente.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agenda_service import build_agenda
from app.bank_service import BankImportError
from app.bank_service import build_business_health
from app.bank_service import build_cashflow_forecast
from app.bank_service import confirm_all_suggestions
from app.bank_service import confirm_match
from app.bank_service import import_bank_file
from app.bank_service import list_transactions
from app.bank_service import serialize_transaction
from app.bank_service import suggest_matches
from app.bank_service import unmatch
from app.company_service import serialize_profile
from app.company_service import update_company_profile
from app.compliance_service import build_compliance_status
from app.compliance_service import store_certificate
from app.compliance_service import update_item
from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor
from app.invoice_service import add_audit_event
from app.invoice_service import get_document
from app.models import BankImport
from app.models import BankTransaction
from app.models import FiscalNotification
from app.models import SupplierRule
from app.notification_service import ISSUERS
from app.notification_service import NOTIFICATION_TYPES
from app.notification_service import create_notification
from app.notification_service import detect_notification
from app.notification_service import list_notifications
from app.notification_service import serialize_notification
from app.notification_service import update_notification
from app.tax_service import build_model_111
from app.tax_service import build_model_115
from app.tax_service import build_model_130
from app.tax_service import build_model_303
from app.tax_service import build_model_347
from app.tax_service import build_tax_calendar
from app.tax_service import delete_filing
from app.tax_service import record_filing


router = APIRouter(prefix="/api")

MAX_BANK_FILE = 5 * 1024 * 1024
MAX_CERT_FILE = 64 * 1024


def current_year(year: int | None) -> int:
    return year or date.today().year


def current_quarter(quarter: int | None) -> int:
    return quarter or ((date.today().month - 1) // 3 + 1)


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


# -------------------------------------------------------------------
# Mi empresa
# -------------------------------------------------------------------

class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=30)
    legal_form: str | None = Field(default=None, max_length=20)
    activity: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    hourly_cost: Decimal | None = Field(default=None, ge=0, le=1000)
    iban: str | None = Field(default=None, max_length=40)
    bic: str | None = Field(default=None, max_length=11)
    at_ep_rate: Decimal | None = Field(default=None, ge=0, le=10)


@router.get("/company", tags=["Mi empresa"])
def get_company(database: DatabaseDependency) -> dict[str, Any]:
    return serialize_profile(database)


@router.put("/company", tags=["Mi empresa"])
def put_company(
    payload: CompanyUpdate,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    try:
        profile = update_company_profile(
            database,
            payload.model_dump(exclude_unset=True),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    add_audit_event(
        database,
        action="company.updated",
        entity_type="company",
        entity_id=profile.id,
        actor=normalize_actor(actor_header),
        event_data=payload.model_dump(exclude_unset=True),
    )
    database.commit()

    return serialize_profile(database)


# -------------------------------------------------------------------
# Impuestos
# -------------------------------------------------------------------

MODEL_BUILDERS = {
    "303": build_model_303,
    "130": build_model_130,
    "111": build_model_111,
    "115": build_model_115,
}


class FilingRequest(BaseModel):
    model: str = Field(pattern=r"^\d{3}$")
    year: int = Field(ge=2000, le=2100)
    period: int = Field(ge=0, le=4)
    filed_at: date | None = None
    amount: Decimal | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/taxes/calendar", tags=["Impuestos"])
def tax_calendar(
    database: DatabaseDependency,
    year: int | None = Query(default=None, ge=2000, le=2100),
) -> dict[str, Any]:
    return build_tax_calendar(database, year=current_year(year))


@router.get("/taxes/models/{model}", tags=["Impuestos"])
def tax_model(
    model: str,
    database: DatabaseDependency,
    year: int | None = Query(default=None, ge=2000, le=2100),
    quarter: int | None = Query(default=None, ge=1, le=4),
) -> dict[str, Any]:
    if model == "347":
        return build_model_347(database, year=current_year(year))

    builder = MODEL_BUILDERS.get(model)

    if builder is None:
        raise not_found("Modelo no disponible como borrador.")

    return builder(database, year=current_year(year), quarter=current_quarter(quarter))


@router.post("/taxes/filings", tags=["Impuestos"])
def create_filing(
    payload: FilingRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    try:
        filing = record_filing(
            database,
            model=payload.model,
            year=payload.year,
            period=payload.period,
            filed_at=payload.filed_at or date.today(),
            amount=payload.amount,
            reference=payload.reference,
            notes=payload.notes,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    add_audit_event(
        database,
        action="tax.filed",
        entity_type="tax",
        entity_id=f"{payload.model}-{payload.year}-{payload.period}",
        actor=normalize_actor(actor_header),
        event_data=payload.model_dump(),
    )
    database.commit()

    return {
        "success": True,
        "message": f"Modelo {filing.model} marcado como presentado.",
    }


@router.delete("/taxes/filings", tags=["Impuestos"])
def remove_filing(
    database: DatabaseDependency,
    model: str = Query(pattern=r"^\d{3}$"),
    year: int = Query(ge=2000, le=2100),
    period: int = Query(ge=0, le=4),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    if not delete_filing(database, model=model, year=year, period=period):
        raise not_found("No hay presentación registrada para ese periodo.")

    add_audit_event(
        database,
        action="tax.filing_removed",
        entity_type="tax",
        entity_id=f"{model}-{year}-{period}",
        actor=normalize_actor(actor_header),
        event_data={"model": model, "year": year, "period": period},
    )
    database.commit()

    return {"success": True, "message": "Presentación desmarcada."}


# -------------------------------------------------------------------
# Notificaciones
# -------------------------------------------------------------------

class NotificationBase(BaseModel):
    issuer: str | None = Field(default=None, max_length=30)
    notification_type: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)
    amount: Decimal | None = None
    available_at: date | None = None
    notified_at: date | None = None
    deadline: date | None = None


class NotificationUpdate(NotificationBase):
    status: str | None = Field(default=None, max_length=20)


def validate_codes(payload: NotificationBase) -> None:
    if payload.issuer and payload.issuer not in ISSUERS:
        raise HTTPException(status_code=422, detail="Organismo no válido.")

    if payload.notification_type and payload.notification_type not in NOTIFICATION_TYPES:
        raise HTTPException(status_code=422, detail="Tipo de notificación no válido.")


@router.get("/notifications/catalog", tags=["Notificaciones"])
def notification_catalog() -> dict[str, Any]:
    return {
        "issuers": [{"code": code, "label": label} for code, label in ISSUERS.items()],
        "types": [{"code": code, "label": label} for code, label in NOTIFICATION_TYPES.items()],
    }


@router.get("/notifications", tags=["Notificaciones"])
def get_notifications(
    database: DatabaseDependency,
    only_open: bool = Query(default=False, alias="open"),
) -> list[dict[str, Any]]:
    return list_notifications(database, only_open=only_open)


@router.post("/notifications", tags=["Notificaciones"], status_code=201)
def post_notification(
    payload: NotificationBase,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    validate_codes(payload)
    notification = create_notification(
        database,
        document=None,
        data=payload.model_dump(),
        actor=normalize_actor(actor_header),
    )
    database.commit()
    database.refresh(notification)

    return serialize_notification(notification)


@router.post("/notifications/from-document/{document_id}", tags=["Notificaciones"])
def notification_from_document(
    document_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    document = get_document(database, document_id)

    if document is None:
        raise not_found("Documento no encontrado.")

    if document.invoice is not None and document.invoice.review_status == "APPROVED":
        raise HTTPException(
            status_code=409,
            detail="El documento es una factura aprobada: reábrela antes.",
        )

    existing = database.scalar(
        select(FiscalNotification).where(FiscalNotification.document_id == document.id)
    )

    if existing is not None:
        return serialize_notification(existing)

    text = ""

    if document.extraction_runs:
        text = document.extraction_runs[0].raw_text or ""

    data = detect_notification(text, document.original_filename) or {
        "issuer": "OTRO",
        "notification_type": "OTRO",
        "title": "Notificación",
    }
    data["document_date"] = document.created_at.date()

    if document.invoice is not None:
        # No era una factura: se descarta la extracción como factura.
        database.delete(document.invoice)
        document.invoice = None

    notification = create_notification(
        database,
        document=document,
        data=data,
        actor=normalize_actor(actor_header),
    )
    database.commit()
    database.refresh(notification)

    return serialize_notification(notification)


@router.patch("/notifications/{notification_id}", tags=["Notificaciones"])
def patch_notification(
    notification_id: int,
    payload: NotificationUpdate,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    validate_codes(payload)
    notification = database.scalar(
        select(FiscalNotification)
        .where(FiscalNotification.id == notification_id)
        .options(selectinload(FiscalNotification.document))
    )

    if notification is None:
        raise not_found("Notificación no encontrada.")

    try:
        update_notification(
            database,
            notification,
            payload.model_dump(exclude_unset=True),
            normalize_actor(actor_header),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    database.commit()
    database.refresh(notification)

    return serialize_notification(notification)


# -------------------------------------------------------------------
# Banco, tesorería y salud del negocio
# -------------------------------------------------------------------

class ConfirmMatchRequest(BaseModel):
    invoice_id: int | None = None


class UnmatchRequest(BaseModel):
    ignore: bool = False


class BulkConfirmRequest(BaseModel):
    min_score: int = Field(default=85, ge=60, le=100)


@router.post("/bank/import", tags=["Banco"], status_code=201)
async def bank_import(
    database: DatabaseDependency,
    uploaded_file: UploadFile = File(...),
    account_label: str | None = Form(default=None, max_length=100),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    content = await uploaded_file.read(MAX_BANK_FILE + 1)
    await uploaded_file.close()

    if len(content) > MAX_BANK_FILE:
        raise HTTPException(status_code=413, detail="El extracto supera 5 MB.")

    filename = uploaded_file.filename or "extracto.csv"

    if not filename.lower().endswith((".csv", ".txt", ".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="Formato no admitido. Usa CSV o Excel (.xlsx).",
        )

    try:
        result = import_bank_file(
            database,
            content=content,
            filename=filename,
            account_label=(account_label or "").strip() or None,
            actor=normalize_actor(actor_header),
        )
    except BankImportError as error:
        database.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error

    database.commit()

    return result


@router.get("/bank/imports", tags=["Banco"])
def bank_imports(database: DatabaseDependency) -> list[dict[str, Any]]:
    imports = database.scalars(
        select(BankImport).order_by(BankImport.created_at.desc()).limit(20)
    ).all()

    return [
        {
            "id": item.id,
            "filename": item.filename,
            "account_label": item.account_label,
            "rows_total": item.rows_total,
            "rows_imported": item.rows_imported,
            "rows_duplicated": item.rows_duplicated,
            "created_at": item.created_at.isoformat(),
        }
        for item in imports
    ]


@router.get("/bank/transactions", tags=["Banco"])
def bank_transactions(
    database: DatabaseDependency,
    match_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(UNMATCHED|SUGGESTED|MATCHED|IGNORED|unmatched|suggested|matched|ignored)$",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    return list_transactions(database, status=match_status, limit=limit)


def get_transaction(database, transaction_id: int) -> BankTransaction:
    transaction = database.scalar(
        select(BankTransaction)
        .where(BankTransaction.id == transaction_id)
        .options(selectinload(BankTransaction.matched_invoice))
    )

    if transaction is None:
        raise not_found("Movimiento no encontrado.")

    return transaction


@router.post("/bank/transactions/{transaction_id}/confirm", tags=["Banco"])
def bank_confirm(
    transaction_id: int,
    payload: ConfirmMatchRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    transaction = get_transaction(database, transaction_id)

    try:
        confirm_match(
            database,
            transaction=transaction,
            invoice_id=payload.invoice_id,
            actor=normalize_actor(actor_header),
        )
    except ValueError as error:
        database.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error

    database.commit()
    database.refresh(transaction)

    return serialize_transaction(database, transaction)


@router.post("/bank/transactions/{transaction_id}/unmatch", tags=["Banco"])
def bank_unmatch(
    transaction_id: int,
    payload: UnmatchRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    transaction = get_transaction(database, transaction_id)
    unmatch(
        database,
        transaction=transaction,
        ignore=payload.ignore,
        actor=normalize_actor(actor_header),
    )
    database.commit()
    database.refresh(transaction)

    return serialize_transaction(database, transaction, include_candidates=True)


@router.post("/bank/confirm-suggestions", tags=["Banco"])
def bank_confirm_suggestions(
    payload: BulkConfirmRequest,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    confirmed = confirm_all_suggestions(
        database,
        min_score=payload.min_score,
        actor=normalize_actor(actor_header),
    )
    database.commit()

    return {
        "success": True,
        "confirmed": confirmed,
        "message": f"{confirmed} conciliación(es) confirmada(s).",
    }


@router.post("/bank/suggest", tags=["Banco"])
def bank_suggest(database: DatabaseDependency) -> dict[str, Any]:
    suggested = suggest_matches(database)
    database.commit()

    return {
        "success": True,
        "suggested": suggested,
        "message": f"El agente propone {suggested} conciliación(es) nueva(s).",
    }


@router.get("/cashflow", tags=["Negocio"])
def cashflow(
    database: DatabaseDependency,
    horizon: int = Query(default=90, ge=7, le=365),
) -> dict[str, Any]:
    return build_cashflow_forecast(database, horizon_days=horizon)


@router.get("/business-health", tags=["Negocio"])
def business_health(
    database: DatabaseDependency,
    year: int | None = Query(default=None, ge=2000, le=2100),
    quarter: int | None = Query(default=None, ge=1, le=4),
) -> dict[str, Any]:
    return build_business_health(database, year=current_year(year), quarter=quarter)


# -------------------------------------------------------------------
# Cumplimiento
# -------------------------------------------------------------------

class ComplianceUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=20)
    expires_at: date | None = None
    clear_expiry: bool = False
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/compliance", tags=["Cumplimiento"])
def compliance_status(database: DatabaseDependency) -> dict[str, Any]:
    return build_compliance_status(database)


@router.patch("/compliance/{code}", tags=["Cumplimiento"])
def patch_compliance(
    code: str,
    payload: ComplianceUpdate,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    try:
        update_item(
            database,
            code=code,
            status=payload.status,
            expires_at=payload.expires_at,
            notes=payload.notes,
            clear_expiry=payload.clear_expiry,
            actor=normalize_actor(actor_header),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    database.commit()

    return build_compliance_status(database)


@router.post("/compliance/certificate", tags=["Cumplimiento"])
async def upload_certificate(
    database: DatabaseDependency,
    certificate: UploadFile = File(...),
    password: str | None = Form(default=None),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    content = await certificate.read(MAX_CERT_FILE + 1)
    await certificate.close()

    if len(content) > MAX_CERT_FILE:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande para ser un certificado.")

    try:
        item = store_certificate(
            database,
            content=content,
            password=password,
            actor=normalize_actor(actor_header),
        )
    except ValueError as error:
        database.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error

    database.commit()

    return {
        "success": True,
        "message": (
            "Certificado leído. Solo se han guardado sus datos de validez; "
            "el archivo y la contraseña no se almacenan."
        ),
        "certificate": item.details,
    }


# -------------------------------------------------------------------
# Agenda y memoria del agente
# -------------------------------------------------------------------

@router.get("/agenda", tags=["Centro operativo"])
def agenda(
    database: DatabaseDependency,
    horizon: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    return build_agenda(database, horizon_days=horizon)


@router.get("/supplier-rules", tags=["Memoria del agente"])
def supplier_rules(database: DatabaseDependency) -> list[dict[str, Any]]:
    rules = database.scalars(select(SupplierRule).order_by(SupplierRule.updated_at.desc())).all()

    return [
        {
            "id": rule.id,
            "tax_id": rule.tax_id,
            "category": rule.category,
            "times_applied": rule.times_applied,
            "updated_at": rule.updated_at.isoformat(),
        }
        for rule in rules
    ]


@router.delete("/supplier-rules/{rule_id}", tags=["Memoria del agente"])
def delete_supplier_rule(
    rule_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    rule = database.get(SupplierRule, rule_id)

    if rule is None:
        raise not_found("Regla no encontrada.")

    add_audit_event(
        database,
        action="supplier_rule.deleted",
        entity_type="supplier_rule",
        entity_id=rule.id,
        actor=normalize_actor(actor_header),
        event_data={"tax_id": rule.tax_id, "category": rule.category},
    )
    database.delete(rule)
    database.commit()

    return {"success": True, "message": "Regla olvidada."}
