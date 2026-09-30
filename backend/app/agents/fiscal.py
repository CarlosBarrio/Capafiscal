"""
Agente fiscal: a qué modelo y periodo se refiere la notificación, qué dicen
nuestros borradores y lo presentado, y dónde puede estar la diferencia.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.agents import llm
from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import eur
from app.agents.base import evidence
from app.extractor import normalize_search_text
from app.models import BankTransaction
from app.models import Invoice
from app.models import PayrollRun
from app.models import TaxFiling
from app.reports_service import quarter_range

MODEL_PATTERN = re.compile(r"modelo\s*(\d{3})")
YEAR_PATTERN = re.compile(r"(?:ejercicio|periodo|ano|año)\s*(?:fiscal\s*)?[:.]?\s*(20\d{2})")
ANY_YEAR = re.compile(r"\b(20[12]\d)\b")
QUARTER_PATTERNS = (
    (re.compile(r"\b([1-4])\s*t\b"), None),
    (re.compile(r"periodo\s*[:.]?\s*([1-4])\s*t"), None),
    (re.compile(r"\bprimer trimestre\b"), 1),
    (re.compile(r"\bsegundo trimestre\b"), 2),
    (re.compile(r"\btercer trimestre\b"), 3),
    (re.compile(r"\bcuarto trimestre\b"), 4),
)
TOPIC_MODELS = (
    ("impuesto sobre el valor anadido", "303"),
    ("iva", "303"),
    ("retenciones e ingresos a cuenta", "111"),
    ("rendimientos del trabajo", "111"),
    ("arrendamiento", "115"),
    ("pagos fraccionados", "130"),
    ("impuesto sobre sociedades", "200"),
    ("operaciones con terceras personas", "347"),
)
DRAFTABLE = {"303", "130", "111", "115"}


def detect_references(text: str) -> list[dict[str, Any]]:
    normalized = normalize_search_text(text)
    models = list(dict.fromkeys(MODEL_PATTERN.findall(normalized)))
    if not models:
        models = list(dict.fromkeys(model for topic, model in TOPIC_MODELS if topic in normalized))[:2]

    year_match = YEAR_PATTERN.search(normalized)
    years = [int(year_match.group(1))] if year_match else [int(item) for item in ANY_YEAR.findall(normalized)][:1]
    quarter = None
    for pattern, fixed in QUARTER_PATTERNS:
        match = pattern.search(normalized)
        if match:
            quarter = fixed or int(match.group(1))
            break

    return [{"model": model, "year": years[0] if years else None, "quarter": quarter} for model in models]


def merge_llm_references(references: list[dict[str, Any]], extracted: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not extracted:
        return references
    for item in extracted.get("tax_references", []):
        model = re.sub(r"\D", "", item.get("model", ""))[:3]
        if not model or any(ref["model"] == model for ref in references):
            continue
        year = re.search(r"20\d{2}", item.get("year", ""))
        quarter = re.search(r"[1-4]", item.get("period", ""))
        references.append({"model": model, "year": int(year.group(0)) if year else None, "quarter": int(quarter.group(0)) if quarter else None})
    return references


def build_model(database, model: str, year: int, quarter: int) -> dict[str, Any] | None:
    from app.tax_service import build_model_111
    from app.tax_service import build_model_115
    from app.tax_service import build_model_130
    from app.tax_service import build_model_303

    builder = {"303": build_model_303, "130": build_model_130, "111": build_model_111, "115": build_model_115}.get(model)
    if builder is None:
        return None
    try:
        return builder(database, year=year, quarter=quarter)
    except Exception:
        return None


class AgenteFiscal(Agent):
    code = "fiscal"
    name = "Fiscal"
    role = "Calcula los impuestos en continuo y comprueba si una notificación afecta a algún modelo."
    icon = "receipt"

    def run(self, ctx: AgentContext) -> StepResult:
        database = ctx.database
        extracted = llm.extract_notification(ctx.text) if llm.available() else None
        ctx.facts["llm_extraction"] = extracted
        references = merge_llm_references(detect_references(ctx.text), extracted)

        analyses = []
        notes: list[str] = []
        notified_amount = ctx.facts.get("amount")

        for reference in references:
            model, year, quarter = reference["model"], reference["year"], reference["quarter"]
            item: dict[str, Any] = {**reference}

            if year and quarter:
                date_from, date_to = quarter_range(year, quarter)
                invoices = database.scalars(
                    select(Invoice).where(Invoice.invoice_date >= date_from, Invoice.invoice_date <= date_to)
                ).all()
                item["invoices_approved"] = sum(1 for invoice in invoices if invoice.review_status == "APPROVED")
                item["invoices_pending"] = sum(1 for invoice in invoices if invoice.review_status == "PENDING")
                item["bank_movements"] = len(
                    database.scalars(
                        select(BankTransaction.id).where(BankTransaction.booking_date >= date_from, BankTransaction.booking_date <= date_to)
                    ).all()
                )
                filing = database.scalar(select(TaxFiling).where(TaxFiling.model == model, TaxFiling.year == year, TaxFiling.period == quarter))
                if filing:
                    item["filed"] = {"date": filing.filed_at, "amount": filing.amount, "reference": filing.reference}

                if model in DRAFTABLE:
                    draft = build_model(database, model, year, quarter)
                    if draft:
                        item["draft_result"] = draft.get("result")
                        item["draft_outcome"] = draft.get("outcome")

                if item.get("filed") and item.get("draft_result") is not None and item["filed"]["amount"] is not None:
                    difference = Decimal(str(item["draft_result"])) - Decimal(str(item["filed"]["amount"]))
                    item["difference_vs_filed"] = float(difference)
                    if abs(difference) >= 1:
                        notes.append(
                            f"El {model} del {quarter}T {year} presentado ({eur(item['filed']['amount'])}) no coincide con lo que dan hoy tus datos ({eur(item['draft_result'])}): diferencia de {eur(difference)}."
                        )
                if notified_amount and item.get("draft_result") is not None:
                    item["notified_vs_draft"] = float(Decimal(str(notified_amount)) - Decimal(str(item["draft_result"])))

                if item["invoices_pending"]:
                    notes.append(f"Hay {item['invoices_pending']} factura(s) de ese trimestre pendientes de revisar: revísalas antes de responder.")
                if not item.get("filed") and model in DRAFTABLE:
                    notes.append(f"No consta presentado el {model} del {quarter}T {year} en CapaFiscal: si lo presentaste, regístralo para tener el justificante a mano.")
            elif year:
                item["note"] = "Referencia anual: se revisan los datos del ejercicio completo."
            analyses.append(item)

        # Seguridad Social: nóminas del periodo
        if ctx.facts.get("issuer") == "TGSS":
            runs = database.scalars(select(PayrollRun).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(6)).all()
            ctx.facts["payroll_runs"] = [{"year": run.year, "month": run.month, "status": run.status} for run in runs]
            if runs:
                notes.append(f"Hay {len(runs)} nómina(s) recientes en CapaFiscal que sirven de base para revisar las cotizaciones.")

        ctx.facts["tax_references"] = analyses
        ctx.facts["fiscal_notes"] = notes

        if not analyses:
            summary = "No hace referencia a ningún modelo o periodo concreto."
        else:
            labels = ", ".join(
                f"{item['model']}" + (f" {item['quarter']}T" if item.get("quarter") else "") + (f" {item['year']}" if item.get("year") else "")
                for item in analyses
            )
            summary = f"Afecta a: {labels}." + (f" {notes[0]}" if notes else " Sin discrepancias con tus datos.")

        return StepResult(
            summary=summary,
            output={"references": analyses, "notes": notes},
            evidence=[
                evidence("tax_model", f"Modelo {item['model']} {item.get('quarter') or ''}T {item.get('year') or ''}".strip(), draft_result=item.get("draft_result"), filed=item.get("filed"))
                for item in analyses
            ],
            engine=llm.engine_label() if extracted else "reglas",
        )
