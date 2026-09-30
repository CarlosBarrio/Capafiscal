from __future__ import annotations

from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
    )


class InvoiceTaxLineBase(BaseModel):
    tax_type: str = "IVA"
    tax_rate: Decimal | None = None
    tax_base: Decimal | None = None
    tax_amount: Decimal | None = None
    source: str | None = None
    confidence: int = Field(
        default=0,
        ge=0,
        le=100,
    )


class InvoiceTaxLineCreate(InvoiceTaxLineBase):
    pass


class InvoiceTaxLineResponse(
    InvoiceTaxLineBase,
    ORMModel,
):
    id: int


class InvoiceUpdate(BaseModel):
    supplier_name: str | None = None
    supplier_tax_id: str | None = None

    customer_name: str | None = None
    customer_tax_id: str | None = None

    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None

    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    withholding_total: Decimal | None = None
    surcharge_total: Decimal | None = None
    total: Decimal | None = None

    currency: str | None = None
    concept: str | None = None
    category: str | None = None

    tax_lines: list[InvoiceTaxLineCreate] | None = None

    @field_validator(
        "supplier_name",
        "supplier_tax_id",
        "customer_name",
        "customer_tax_id",
        "invoice_number",
        "currency",
        "concept",
        "category",
        mode="before",
    )
    @classmethod
    def clean_optional_text(
        cls,
        value: Any,
    ) -> Any:
        if not isinstance(value, str):
            return value

        cleaned = value.strip()

        return cleaned or None

    @field_validator("currency")
    @classmethod
    def normalize_currency(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        return value.upper()


class InvoiceRejectRequest(BaseModel):
    reason: str = Field(
        min_length=3,
        max_length=1000,
    )


class InvoiceResponse(ORMModel):
    id: int
    document_id: int

    supplier_name: str | None
    supplier_tax_id: str | None

    customer_name: str | None
    customer_tax_id: str | None

    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None

    subtotal: Decimal | None
    tax_total: Decimal | None
    withholding_total: Decimal | None
    surcharge_total: Decimal | None
    total: Decimal | None

    currency: str
    concept: str | None
    category: str | None

    confidence: int
    field_confidences: dict[str, Any]

    validation_status: str
    validation_messages: list[dict[str, Any]]
    review_status: str

    duplicate_status: str
    duplicate_of_invoice_id: int | None

    approved_at: datetime | None
    rejected_at: datetime | None
    rejection_reason: str | None

    created_at: datetime
    updated_at: datetime

    tax_lines: list[InvoiceTaxLineResponse] = Field(
        default_factory=list,
    )


class ExtractionRunResponse(ORMModel):
    id: int
    document_id: int

    extractor_name: str
    extractor_version: str
    status: str

    result_json: dict[str, Any] | None
    error_message: str | None

    started_at: datetime
    finished_at: datetime | None


class DocumentListItem(ORMModel):
    id: int

    original_filename: str
    stored_filename: str

    mime_type: str | None
    extension: str
    size_bytes: int

    source: str
    source_provider: str | None
    is_demo: bool

    status: str
    extraction_status: str

    requires_ocr: bool
    page_count: int | None

    created_at: datetime
    updated_at: datetime

    invoice: InvoiceResponse | None = None


class DocumentDetail(DocumentListItem):
    failure_reason: str | None

    extraction_runs: list[ExtractionRunResponse] = Field(
        default_factory=list,
    )


class AuditEventResponse(ORMModel):
    id: int

    actor: str
    action: str

    entity_type: str
    entity_id: str

    event_data: dict[str, Any]
    created_at: datetime


class UploadResponse(BaseModel):
    success: bool
    message: str

    duplicate: bool = False
    document: DocumentDetail | None = None


class ActionResponse(BaseModel):
    success: bool
    message: str

    document: DocumentDetail | None = None
    invoice: InvoiceResponse | None = None


# -------------------------------------------------------------------
# Dashboard operativo
# -------------------------------------------------------------------

class DashboardMetricsResponse(BaseModel):
    documents_today: int
    processed_today: int
    pending_documents: int
    open_risks: int
    open_tasks: int


class DashboardRecommendationResponse(BaseModel):
    title: str
    message: str
    action: str
    severity: str
    document_id: int | None = None


class RiskResponse(BaseModel):
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
    amount: float | None = None
    confidence: int | None = None

    created_at: datetime | None = None


class DashboardTaskSummaryResponse(BaseModel):
    id: int
    task_type: str
    status: str
    priority: str

    reason: str | None = None

    document_id: int
    invoice_id: int | None = None
    supplier_name: str | None = None


class AgentResponse(BaseModel):
    id: str
    name: str
    icon: str

    status: str
    status_label: str

    description: str

    metric_label: str
    metric_value: int | str | float

    detail: str


class DashboardActivityResponse(BaseModel):
    id: int

    action: str
    entity_type: str
    entity_id: str
    actor: str

    event_data: dict[str, Any]
    created_at: datetime


class DashboardTodayResponse(BaseModel):
    generated_at: datetime
    headline: str

    metrics: DashboardMetricsResponse
    recommendation: DashboardRecommendationResponse

    risks: list[RiskResponse] = Field(
        default_factory=list,
    )

    tasks: list[DashboardTaskSummaryResponse] = Field(
        default_factory=list,
    )

    agents: list[AgentResponse] = Field(
        default_factory=list,
    )

    recent_activity: list[DashboardActivityResponse] = Field(
        default_factory=list,
    )


class MonthlyImpactResponse(BaseModel):
    period: str

    documents_received: int
    documents_processed: int
    documents_approved: int

    risks_detected: int

    tasks_created: int
    tasks_resolved: int

    estimated_hours_saved: float
    estimated_cost_saved: float

    calculation_note: str


class ConnectorResponse(BaseModel):
    id: str
    name: str
    icon: str
    category: str

    status: str
    status_label: str

    description: str
    documents_found: int

    action: str
    action_label: str


class AssistantQueryRequest(BaseModel):
    question: str = Field(
        min_length=2,
        max_length=2000,
    )

    @field_validator("question")
    @classmethod
    def normalize_question(
        cls,
        value: str,
    ) -> str:
        cleaned = value.strip()

        if not cleaned:
            raise ValueError(
                "La consulta no puede estar vacía."
            )

        return cleaned


class AssistantSourceResponse(BaseModel):
    type: str
    label: str

    document_id: int | None = None
    invoice_id: int | None = None
    task_id: int | None = None


class AssistantQueryResponse(BaseModel):
    answer: str
    sources: list[AssistantSourceResponse] = Field(
        default_factory=list,
    )

    mode: str
    warning: str