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
from app.agents.base import Finding
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


def analyse_references(database, references: list[dict[str, Any]], notified_amount: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Por cada modelo y periodo: facturas, banco, lo presentado, el borrador y las diferencias."""
    analyses = []
    notes: list[str] = []

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

    return analyses, notes


class AgenteFiscal(Agent):
    code = "fiscal"
    name = "Fiscal"
    role = "Calcula los impuestos en continuo y comprueba si una notificación afecta a algún modelo."
    icon = "receipt"
    handles = ("notification", "invoice", "deadline")
    needs_case = False
    consumes = ("expediente o evento", "texto (modelos y periodos citados)", "facturas, banco y presentaciones")
    produces = ("modelo", "periodo", "borrador y presentado", "discrepancias", "documentación relevante", "evidencia")

    def run(self, ctx: AgentContext) -> StepResult:
        kind = ctx.event.kind if ctx.event else "notification"
        if kind == "invoice":
            return self.run_invoice(ctx)
        if kind == "deadline":
            return self.run_deadline(ctx)
        return self.run_notification(ctx)

    def run_invoice(self, ctx: AgentContext) -> StepResult:
        """Qué supone la factura para los impuestos si se da por buena."""
        database = ctx.database
        invoice = ctx.facts["invoice_obj"]
        if not invoice.invoice_date:
            return StepResult(summary="La factura no tiene fecha: no se puede asignar a un periodo.", output={})
        year, quarter = invoice.invoice_date.year, (invoice.invoice_date.month - 1) // 3 + 1
        analyses, notes = analyse_references(database, [{"model": "303", "year": year, "quarter": quarter}])
        vat = float(invoice.tax_total or 0)
        withholding = float(invoice.withholding_total or 0)
        filing = analyses[0].get("filed") if analyses else None
        impact = {"model": "303", "year": year, "quarter": quarter, "deductible_vat": vat, "withholding": withholding, "filed": bool(filing)}
        ctx.facts["tax_references"] = analyses
        ctx.facts["invoice_tax"] = impact

        findings: list[Finding] = []
        anomaly_types = {item.tipo for item in ctx.findings if item.agente == "detector"}
        lines = [f"Va al 303 del {quarter}T {year}: {eur(vat)} de IVA soportado deducible si se da por buena."]
        if withholding:
            lines.append(f"Lleva {eur(withholding)} de retención: hay que ingresarla en el 111 del {quarter}T.")
        if filing:
            findings.append(
                Finding(
                    agente=self.code, tipo="PERIODO_PRESENTADO",
                    resultado=f"El 303 del {quarter}T {year} ya está presentado",
                    por_que=f"Se presentó el {filing['date']:%d/%m/%Y}; incluir ahora esta factura obliga a una complementaria o a deducirla en un periodo posterior (dentro de 4 años).",
                    riesgo="medium", confianza=0.9, datos=impact, documento_origen=invoice.document_id,
                    fecha=invoice.invoice_date.isoformat(), siguiente="Decide si la deduces en el trimestre actual o preparas una complementaria.",
                )
            )
        if anomaly_types & {"IVA_INUSUAL", "POSIBLE_DUPLICADO", "IMPORTE_ATIPICO"} and vat:
            reason = {
                "IVA_INUSUAL": "el tipo de IVA no es el habitual: si es erróneo, Hacienda no admite la deducción de más",
                "POSIBLE_DUPLICADO": "si es un duplicado, deducirías dos veces el mismo IVA",
                "IMPORTE_ATIPICO": "el importe no es el habitual y el IVA deducible crece en proporción",
            }
            why = "; ".join(reason[item] for item in ("POSIBLE_DUPLICADO", "IVA_INUSUAL", "IMPORTE_ATIPICO") if item in anomaly_types)
            findings.append(
                Finding(
                    agente=self.code, tipo="IVA_EN_RIESGO",
                    resultado=f"{eur(vat)} de IVA deducible en riesgo",
                    por_que=why[:1].upper() + why[1:] + ".",
                    riesgo="high" if "POSIBLE_DUPLICADO" in anomaly_types else "medium",
                    confianza=0.8, datos=impact, documento_origen=invoice.document_id,
                    fecha=invoice.invoice_date.isoformat(), siguiente="No la incluyas en el 303 hasta aclararlo.",
                )
            )
        ctx.facts["fiscal_notes"] = [item.por_que for item in findings] + notes
        return StepResult(
            summary=" ".join(lines) + (f" {findings[0].resultado}." if findings else ""),
            output={"impact": impact, "references": analyses},
            evidence=[evidence("tax_model", f"Modelo 303 {quarter}T {year}", draft_result=analyses[0].get("draft_result") if analyses else None, filed=filing)],
            findings=findings,
            signals={"tax_risk": bool(findings)},
        )

    def run_deadline(self, ctx: AgentContext) -> StepResult:
        period = ctx.facts["period"]
        from app.company_service import legal_form

        if period["model"] == "130" and legal_form(ctx.database) == "SOCIEDAD":
            # El 130 es de autónomos en estimación directa: una sociedad no lo presenta.
            note = "El modelo 130 no aplica: tu empresa es una sociedad (tributa por el Impuesto sobre Sociedades, modelo 202)."
            ctx.facts["tax_references"] = []
            ctx.facts["fiscal_notes"] = [note]
            ctx.facts["not_applicable"] = True
            return StepResult(summary=note, output={"not_applicable": True})
        analyses, notes = analyse_references(ctx.database, [{"model": period["model"], "year": period["year"], "quarter": period["quarter"]}])
        item = analyses[0]
        ctx.facts["tax_references"] = analyses
        ctx.facts["fiscal_notes"] = notes
        result = item.get("draft_result")
        if item.get("filed"):
            summary = f"El {period['model']} del {period['quarter']}T {period['year']} ya consta presentado."
        elif result is None:
            summary = f"No hay borrador automático del {period['model']}: prepáralo con tu asesor."
        else:
            summary = f"Borrador del {period['model']} {period['quarter']}T {period['year']}: {eur(result)}" + (f" ({item.get('draft_outcome')})" if item.get("draft_outcome") else "") + "."
        if item.get("invoices_pending"):
            summary += f" {item['invoices_pending']} factura(s) del trimestre sin revisar."
        findings = []
        if item.get("invoices_pending"):
            findings.append(
                Finding(
                    agente=self.code, tipo="FACTURAS_SIN_REVISAR",
                    resultado=f"{item['invoices_pending']} factura(s) del {period['quarter']}T sin revisar",
                    por_que="Las facturas pendientes no entran en el borrador: el resultado puede cambiar.",
                    riesgo="medium", confianza=0.95, datos={"pending": item["invoices_pending"]},
                    siguiente="Revísalas antes de presentar.",
                )
            )
        return StepResult(
            summary=summary,
            output={"references": analyses, "notes": notes},
            evidence=[evidence("tax_model", f"Modelo {period['model']} {period['quarter']}T {period['year']}", draft_result=result, filed=item.get("filed"))],
            findings=findings,
        )

    def run_notification(self, ctx: AgentContext) -> StepResult:
        database = ctx.database
        extracted, llm_meta = llm.extract_notification(ctx.text) if llm.available() else (None, None)
        ctx.facts["llm_extraction"] = extracted
        references = merge_llm_references(detect_references(ctx.text), extracted)
        analyses, notes = analyse_references(database, references, ctx.facts.get("amount"))

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
            # llm: modelo, tokens, coste, duración y resultado de la llamada (queda en AgentStep.output)
            output={"references": analyses, "notes": notes, **({"llm": [llm_meta]} if llm_meta else {})},
            evidence=[
                evidence("tax_model", f"Modelo {item['model']} {item.get('quarter') or ''}T {item.get('year') or ''}".strip(), draft_result=item.get("draft_result"), filed=item.get("filed"))
                for item in analyses
            ],
            engine=llm.engine_label() if extracted else "reglas",
            findings=discrepancy_findings(analyses, ctx.facts.get("document_id")),
            signals={"discrepancy": any(abs(item.get("difference_vs_filed") or 0) >= 1 or abs(item.get("notified_vs_draft") or 0) >= 1 for item in analyses)},
        )


def discrepancy_findings(analyses: list[dict[str, Any]], document_id: int | None) -> list[Finding]:
    findings = []
    for item in analyses:
        label = f"{item['model']} {item.get('quarter') or ''}T {item.get('year') or ''}".replace(" T ", " ").strip()
        filed = item.get("filed")
        if filed:
            when = filed["date"].strftime("%d/%m/%Y") if hasattr(filed.get("date"), "strftime") else filed.get("date")
            findings.append(
                Finding(
                    agente="fiscal", tipo="PERIODO_PRESENTADO",
                    resultado=f"El {label} consta presentado",
                    por_que=f"Presentado el {when}" + (f" por {eur(filed['amount'])}" if filed.get("amount") is not None else "") + ": el justificante puede aportarse tal cual.",
                    riesgo="low", confianza=0.95,
                    datos={key: item.get(key) for key in ("model", "year", "quarter")},
                    documento_origen=document_id, siguiente="Adjunta el justificante de presentación a la respuesta.",
                )
            )
        if abs(item.get("difference_vs_filed") or 0) >= 1:
            findings.append(
                Finding(
                    agente="fiscal", tipo="DIFERENCIA_PRESENTADO",
                    resultado=f"El {label} presentado no cuadra con tus datos actuales",
                    por_que=f"Presentado: {eur(item['filed']['amount'])}; tus datos dan {eur(item['draft_result'])} (diferencia {eur(item['difference_vs_filed'])}).",
                    riesgo="high" if abs(item["difference_vs_filed"]) >= 1000 else "medium", confianza=0.85,
                    datos={key: item.get(key) for key in ("model", "year", "quarter", "draft_result", "difference_vs_filed")},
                    documento_origen=document_id, siguiente="Localiza la diferencia antes de contestar (facturas añadidas o modificadas después).",
                )
            )
        if abs(item.get("notified_vs_draft") or 0) >= 1:
            findings.append(
                Finding(
                    agente="fiscal", tipo="DIFERENCIA_NOTIFICADA",
                    resultado=f"El importe notificado no coincide con tu {label}",
                    por_que=f"Diferencia de {eur(item['notified_vs_draft'])} entre lo que dice la Administración y tu borrador.",
                    riesgo="medium", confianza=0.7,
                    datos={key: item.get(key) for key in ("model", "year", "quarter", "draft_result", "notified_vs_draft")},
                    documento_origen=document_id, siguiente="Compara factura a factura con el detalle de la propuesta.",
                )
            )
    return findings
