from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    original_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    stored_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )

    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    mime_type: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    extension: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="manual_upload",
        index=True,
    )
    source_provider: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    external_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    is_demo: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="RECEIVED",
        index=True,
    )
    extraction_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING",
        index=True,
    )

    requires_ocr: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    page_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # INVOICE (por defecto) o NOTIFICATION. Nulo en bases antiguas.
    kind: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    invoice: Mapped[Invoice | None] = relationship(
        "Invoice",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )

    extraction_runs: Mapped[list[ExtractionRun]] = relationship(
        "ExtractionRun",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="ExtractionRun.id.desc()",
    )

    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent",
        primaryjoin=(
            "and_("
            "AuditEvent.entity_type == 'document', "
            "foreign(AuditEvent.entity_id) == cast(Document.id, String)"
            ")"
        ),
        viewonly=True,
    )

    __table_args__ = (
        Index(
            "ix_documents_source_external_id",
            "source",
            "external_id",
        ),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey(
            "documents.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        unique=True,
        index=True,
    )

    supplier_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    supplier_tax_id: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        index=True,
    )

    customer_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    customer_tax_id: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    # RECEIVED (gasto) o ISSUED (ingreso). Nulo equivale a RECEIVED.
    direction: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        index=True,
    )

    invoice_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )
    invoice_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )
    due_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    subtotal: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    tax_total: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    withholding_total: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    surcharge_total: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    total: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="EUR",
    )

    concept: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    category: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    confidence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    field_confidences: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    validation_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="INCOMPLETE",
        index=True,
    )
    validation_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    review_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING",
        index=True,
    )

    duplicate_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="NONE",
        index=True,
    )
    duplicate_of_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "invoices.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    paid_at: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )
    payment_method: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    document: Mapped[Document] = relationship(
        "Document",
        back_populates="invoice",
    )

    tax_lines: Mapped[list[InvoiceTaxLine]] = relationship(
        "InvoiceTaxLine",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceTaxLine.id.asc()",
    )

    duplicate_of: Mapped[Invoice | None] = relationship(
        "Invoice",
        remote_side=[id],
        foreign_keys=[duplicate_of_invoice_id],
    )

    __table_args__ = (
        Index(
            "ix_invoices_supplier_number",
            "supplier_tax_id",
            "invoice_number",
        ),
        Index(
            "ix_invoices_supplier_date_total",
            "supplier_tax_id",
            "invoice_date",
            "total",
        ),
    )


class InvoiceTaxLine(Base):
    __tablename__ = "invoice_tax_lines"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    invoice_id: Mapped[int] = mapped_column(
        ForeignKey(
            "invoices.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    tax_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="IVA",
    )
    tax_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 3),
        nullable=True,
    )
    tax_base: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    tax_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    confidence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    invoice: Mapped[Invoice] = relationship(
        "Invoice",
        back_populates="tax_lines",
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey(
            "documents.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    extractor_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    extractor_version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="RUNNING",
    )

    raw_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    document: Mapped[Document] = relationship(
        "Document",
        back_populates="extraction_runs",
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    actor: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="system",
    )
    action: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
    )

    entity_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    entity_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        Index(
            "ix_audit_entity",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="REVIEW_INVOICE",
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="OPEN",
        index=True,
    )

    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="NORMAL",
        index=True,
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey(
            "documents.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "invoices.id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    assigned_to: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    resolution: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    resolution_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    document: Mapped[Document] = relationship(
        "Document",
    )

    invoice: Mapped[Invoice | None] = relationship(
        "Invoice",
    )

    events: Mapped[list[TaskEvent]] = relationship(
        "TaskEvent",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskEvent.id.asc()",
    )

    __table_args__ = (
        UniqueConstraint(
            "task_type",
            "document_id",
            name="uq_tasks_type_document",
        ),
        Index(
            "ix_tasks_inbox",
            "status",
            "priority",
            "created_at",
        ),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey(
            "tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    actor: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="system",
    )

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    task: Mapped[Task] = relationship(
        "Task",
        back_populates="events",
    )


# -------------------------------------------------------------------
# Empresa
# -------------------------------------------------------------------

class CompanyProfile(Base):
    """Datos de la empresa usuaria (una sola fila)."""

    __tablename__ = "company_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # AUTONOMO o SOCIEDAD
    legal_form: Mapped[str | None] = mapped_column(String(20), nullable=True)
    activity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hourly_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 2),
        nullable=True,
    )
    # Cuenta de cargo para pagar nóminas por SEPA.
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bic: Mapped[str | None] = mapped_column(String(11), nullable=True)
    # Tipo de AT/EP según CNAE (tarifa de primas), por defecto 1,50 %.
    at_ep_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    # Datos que aparecen en las facturas emitidas y en las cartas.
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    province: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    invoice_footer: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_payment_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Email de la gestoría o asesor externo (cierre trimestral).
    advisor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Interés de demora comercial (Ley 3/2004) vigente, en %.
    late_interest_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class SupplierRule(Base):
    """Memoria del agente: categoría aprendida por NIF de contraparte."""

    __tablename__ = "supplier_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tax_id: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        unique=True,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(150), nullable=False)
    learned_from_invoice_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    times_applied: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


# -------------------------------------------------------------------
# Impuestos
# -------------------------------------------------------------------

class TaxFiling(Base):
    """Registro de un modelo tributario presentado."""

    __tablename__ = "tax_filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(10), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-4 para trimestrales, 0 para anuales.
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    filed_at: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (
        UniqueConstraint(
            "model",
            "year",
            "period",
            name="uq_tax_filings_model_period",
        ),
    )


# -------------------------------------------------------------------
# Notificaciones administrativas
# -------------------------------------------------------------------

class FiscalNotification(Base):
    __tablename__ = "fiscal_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )

    # AEAT, TGSS, DGT, AYUNTAMIENTO, CCAA, OTRO
    issuer: Mapped[str] = mapped_column(String(30), nullable=False)
    # REQUERIMIENTO, PROPUESTA_LIQUIDACION, LIQUIDACION, APREMIO,
    # EMBARGO, SANCION, COMUNICACION, OTRO
    notification_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )

    # Puesta a disposición (DEHú / sede) y fecha de acceso (notificada).
    available_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    notified_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    deadline: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )
    deadline_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline_manual: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # PENDING, IN_PROGRESS, ANSWERED, CLOSED
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    document: Mapped[Document | None] = relationship("Document")


# -------------------------------------------------------------------
# Banco
# -------------------------------------------------------------------

class BankImport(Base):
    __tablename__ = "bank_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    account_label: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_imported: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    rows_duplicated: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    column_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_imports.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    account_label: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    booking_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Negativo = salida de dinero, positivo = entrada.
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    balance: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2),
        nullable=True,
    )
    fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    matched_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # UNMATCHED, SUGGESTED, MATCHED, IGNORED
    match_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="UNMATCHED",
        index=True,
    )
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    matched_invoice: Mapped[Invoice | None] = relationship("Invoice")


# -------------------------------------------------------------------
# Cumplimiento
# -------------------------------------------------------------------

class ComplianceItem(Base):
    __tablename__ = "compliance_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
    )
    # OK, PENDING, WARNING, NOT_APPLICABLE
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
    )
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


# -------------------------------------------------------------------
# Equipo
# -------------------------------------------------------------------

class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ss_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    job_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    department: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # INDEFINIDO, TEMPORAL, PRACTICAS, FORMACION, FIJO_DISCONTINUO
    contract_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="INDEFINIDO",
    )
    workday_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
    )
    annual_salary: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    payments_per_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=14,
    )
    irpf_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contribution_group: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    collective_agreement: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)

    # PENDIENTE_ALTA, ALTA, BAJA
    ss_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDIENTE_ALTA",
    )
    ss_registered_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    ss_deregistered_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    vacation_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=22,
    )
    skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Casillas manuales del checklist de incorporación: {código: fecha}
    checklist: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    documents: Mapped[list[EmployeeDocument]] = relationship(
        "EmployeeDocument",
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by="EmployeeDocument.id.desc()",
    )
    assignments: Mapped[list[ProjectAssignment]] = relationship(
        "ProjectAssignment",
        back_populates="employee",
        cascade="all, delete-orphan",
    )


class EmployeeDocument(Base):
    __tablename__ = "employee_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # CV, CONTRATO, ALTA_SS, BAJA_SS, DNI, MODELO_145, NOMINA,
    # CERTIFICADO, OTRO
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    employee: Mapped[Employee] = relationship(
        "Employee",
        back_populates="documents",
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ACTIVE, PAUSED, DONE
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="ACTIVE",
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    assignments: Mapped[list[ProjectAssignment]] = relationship(
        "ProjectAssignment",
        back_populates="project",
        cascade="all, delete-orphan",
    )


class ProjectAssignment(Base):
    __tablename__ = "project_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    allocation_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
    )

    project: Mapped[Project] = relationship("Project", back_populates="assignments")
    employee: Mapped[Employee] = relationship("Employee", back_populates="assignments")

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "employee_id",
            name="uq_assignment_project_employee",
        ),
    )


class Absence(Base):
    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # VACACIONES, BAJA_IT, PERMISO, ASUNTOS_PROPIOS, OTRO
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    employee: Mapped[Employee] = relationship("Employee")


# -------------------------------------------------------------------
# Nóminas
# -------------------------------------------------------------------

class PayrollRun(Base):
    __tablename__ = "payroll_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    # DRAFT, APPROVED, PAID
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DRAFT",
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    payslips: Mapped[list[Payslip]] = relationship(
        "Payslip",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="Payslip.id.asc()",
    )

    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_payroll_run_period"),
    )


class Payslip(Base):
    __tablename__ = "payslips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    employee_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Variables del mes introducidas por la empresa.
    overtime: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    bonus: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    advance: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)

    gross: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    contribution_base: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=0,
    )
    ss_employee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    irpf_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    irpf: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    net: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    ss_employer: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    company_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Detalle de conceptos (devengos, deducciones y cuotas).
    lines: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[PayrollRun] = relationship("PayrollRun", back_populates="payslips")
    employee: Mapped[Employee | None] = relationship("Employee")



# ---------------------------------------------------------------------
# VENTAS: clientes, facturas emitidas desde la aplicación y recurrentes
# ---------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tax_id: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    payment_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Retención de IRPF que aplica este cliente (profesionales), en %.
    withholding_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SalesInvoice(Base):
    """Factura emitida desde CapaFiscal (borrador hasta que se emite)."""

    __tablename__ = "sales_invoices"
    __table_args__ = (
        UniqueConstraint("series", "year", "number", name="uq_sales_invoice_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # F: ordinaria · R: rectificativa
    series: Mapped[str] = mapped_column(String(5), nullable=False, default="F")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    # DRAFT, ISSUED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT", index=True)
    # F1 completa, F2 simplificada, R1/R4 rectificativa (tipos Verifactu)
    invoice_type: Mapped[str | None] = mapped_column(String(5), nullable=True)

    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    operation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    withholding_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    tax_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    withholding_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(255), nullable=True)

    rectifies_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_invoices.id", ondelete="SET NULL"), nullable=True
    )
    rectification_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recurring_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Enlace con el libro de facturas (impuestos, cobros, tesorería).
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    # Registro de facturación (RD 1007/2023): huella encadenada.
    record_timestamp: Mapped[str | None] = mapped_column(String(40), nullable=True)
    record_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    customer: Mapped[Customer | None] = relationship()
    rectifies: Mapped[SalesInvoice | None] = relationship(remote_side="SalesInvoice.id")
    invoice: Mapped[Invoice | None] = relationship()


class RecurringInvoice(Base):
    """Plantilla que el agente convierte en factura cada periodo."""

    __tablename__ = "recurring_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    withholding_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # MONTHLY, QUARTERLY, YEARLY
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="MONTHLY")
    next_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Emitir directamente (si no, deja borrador) y preparar el envío.
    auto_issue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    customer: Mapped[Customer] = relationship()


# ---------------------------------------------------------------------
# BANDEJA DE SALIDA: mensajes que prepara el agente y aprueba una persona
# ---------------------------------------------------------------------


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # DUNNING, INVOICE, PAYSLIP, DIGEST, ADVISOR, OTHER
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # DRAFT, SENT, DISCARDED, FAILED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT", index=True)
    to_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # [{"type": "sales_invoice", "id": 3, "filename": "F2026-0003.pdf"}]
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nivel de reclamación (1 recordatorio, 2 segundo aviso, 3 requerimiento).
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False, default="agent")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_outbox_entity", "entity_type", "entity_id"),)


# ---------------------------------------------------------------------
# REGISTRO DE JORNADA (art. 34.9 ET)
# ---------------------------------------------------------------------


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Hora local (sin zona) de entrada y salida.
    clock_in: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    clock_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # APP, MANUAL
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="APP")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Las correcciones quedan trazadas con su motivo.
    edit_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    edited_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    employee: Mapped[Employee] = relationship()


# ---------------------------------------------------------------------
# AUTOMATIZACIONES DEL AGENTE
# ---------------------------------------------------------------------


class AutomationSetting(Base):
    __tablename__ = "automation_settings"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # SCHEDULE, MANUAL
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="SCHEDULE")
    # OK, NOTHING, ERROR
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------
# AGENTES: expedientes, trazas de ejecución y peticiones de documentación
# ---------------------------------------------------------------------


class Case(Base):
    """Expediente: la unidad de trabajo que recorren los agentes."""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    # NOTIFICATION, ANOMALY
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Tipo de trámite: REQUERIMIENTO, EMBARGO_CREDITOS, ANOMALIA_IMPORTE…
    procedure: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # OPEN, WAITING_HUMAN, WAITING_DOCS, READY_TO_FILE, FILED, RESOLVED, DISMISSED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # critical, high, normal, low
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")
    headline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A quién afecta (en una gestoría, el cliente de la cartera).
    subject_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    subject_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_tax_id: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    subject_ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    organism: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    internal_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    required_documents: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    proposed_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    antecedents: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    draft_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("fiscal_notifications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Clave para no duplicar anomalías de la misma condición.
    fingerprint: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)

    filed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    filing_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    events: Mapped[list[CaseEvent]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseEvent.id"
    )
    attachments: Mapped[list[CaseAttachment]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseAttachment.id"
    )
    requests: Mapped[list[DocumentRequest]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="DocumentRequest.id"
    )


class CaseEvent(Base):
    """Línea de tiempo del expediente: pasos de agentes y acciones humanas."""

    __tablename__ = "case_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    # agent, human, message, document, system
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    case: Mapped[Case] = relationship(back_populates="events")


class CaseAttachment(Base):
    __tablename__ = "case_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    item_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # user, client, agent
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    case: Mapped[Case] = relationship(back_populates="attachments")


class DocumentRequest(Base):
    """Petición de documentación que persigue el agente hasta recibirla."""

    __tablename__ = "document_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    item_code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    to_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # PENDING, RECEIVED, CANCELLED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    reminders_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_reminder_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("case_attachments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    case: Mapped[Case] = relationship(back_populates="requests")


class AgentRun(Base):
    """Una ejecución del orquestador (un recorrido completo de agentes)."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    # RUNNING, OK, PARTIAL, ERROR
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.position"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    agent: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # OK, SKIPPED, ERROR
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # "reglas" o el modelo de IA que participó
    engine: Mapped[str] = mapped_column(String(60), nullable=False, default="reglas")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class IngestedEvent(Base):
    """Registro de todo lo que entra al sistema (idempotencia y estado).

    Una misma fuente no puede entregar dos veces el mismo evento: la pareja
    (source, external_id) es única. Si llega repetido, se ignora (o se
    reprocesa si el contenido ha cambiado). Si un agente falla, el evento
    queda en NEEDS_HUMAN con el agente y el motivo, y se puede reanudar.
    """

    __tablename__ = "ingested_events"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_ingested_event_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    # RECEIVED, PROCESSING, COMPLETED, NEEDS_HUMAN, FAILED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    failed_agent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
