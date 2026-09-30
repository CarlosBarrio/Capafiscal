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

