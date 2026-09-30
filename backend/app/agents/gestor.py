"""
Gestor de incidencias: trabaja el expediente. Entiende qué se pide, reúne lo
que ya está en el sistema, detecta lo que falta, propone qué hacer y prepara
el borrador de respuesta para que una persona solo tenga que revisarlo.
"""
from __future__ import annotations

import re
from datetime import date
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents import llm
from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import eur
from app.agents.base import evidence
from app.agents.knowledge import DOCUMENTS
from app.agents.knowledge import PROCEDURES
from app.agents.knowledge import SOURCE_LABELS
from app.agents.knowledge import match_documents
from app.agents.letters import build_letter
from app.calendar_es import is_business_day
from app.extractor import normalize_search_text
from app.models import BankTransaction
from app.models import CompanyProfile
from app.models import Employee
from app.models import EmployeeDocument
from app.models import Invoice
from app.models import PayrollRun
from app.reports_service import quarter_range

ITEM_PATTERN = re.compile(r"^\s*(?:[a-z]\)|\d{1,2}[.)º-]|[-•·])\s+(.{6,})$", re.IGNORECASE)
REQUEST_CUES = ("aport", "document", "requier", "solicit", "deber", "present", "remit", "facilit")


def subtract_business_days(value: date, days: int) -> date:
    current = value
    while days > 0:
        current -= timedelta(days=1)
        if is_business_day(current):
            days -= 1
    return current


def requested_items(text: str) -> list[dict[str, Any]]:
    """Qué documentos pide el texto: primero las enumeraciones, si no el texto entero."""
    lines = [line for line in text.splitlines() if line.strip()]
    normalized_lines = [normalize_search_text(line) for line in lines]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    in_request = False
    for original, normalized in zip(lines, normalized_lines):
        if any(cue in normalized for cue in REQUEST_CUES):
            in_request = True
        match = ITEM_PATTERN.match(original)
        if not (match and in_request):
            continue
        detail = match.group(1).strip().rstrip(".;")
        codes = match_documents(normalize_search_text(detail)) or ["OTRO"]
        for code in codes[:1]:
            key = code if code != "OTRO" else f"OTRO:{detail[:40]}"
            if key in seen:
                continue
            seen.add(key)
            items.append({"code": code, "detail": detail})

    if not items:
        for code in match_documents(normalize_search_text(text)):
            items.append({"code": code, "detail": None})

    return items


def main_period(facts: dict[str, Any]) -> dict[str, Any] | None:
    for reference in facts.get("tax_references", []):
        if reference.get("year"):
            return {"year": reference["year"], "quarter": reference.get("quarter")}
    return None


def period_label(period: dict[str, Any] | None) -> str | None:
    if not period:
        return None
    return f"{period['quarter']}T {period['year']}" if period.get("quarter") else f"ejercicio {period['year']}"


def resolve(database, code: str, period: dict[str, Any] | None, facts: dict[str, Any]) -> dict[str, Any]:
    """¿Puede el sistema preparar este documento? Estado y cómo."""
    catalog = DOCUMENTS.get(code, {"label": "Documentación solicitada", "source": "internal"})
    result: dict[str, Any] = {"status": "missing", "artifact": None, "note": None}

    if catalog["source"] != "system":
        result["note"] = SOURCE_LABELS[catalog["source"]]
        return result

    if code in {"LIBRO_EMITIDAS", "LIBRO_RECIBIDAS", "FACTURAS", "EXTRACTOS", "MODELOS", "NOMINAS"} and not period:
        result["note"] = "Indica el periodo en el expediente para que el agente lo prepare."
        return result

    date_from, date_to = quarter_range(period["year"], period.get("quarter")) if period else (None, None)

    if code in {"LIBRO_EMITIDAS", "LIBRO_RECIBIDAS"}:
        direction = "ISSUED" if code == "LIBRO_EMITIDAS" else "RECEIVED"
        clause = Invoice.direction == "ISSUED" if direction == "ISSUED" else (Invoice.direction.is_(None) | (Invoice.direction != "ISSUED"))
        count = len(database.scalars(select(Invoice.id).where(clause, Invoice.invoice_date.between(date_from, date_to), Invoice.review_status == "APPROVED")).all())
        result.update(status="ready", artifact={"type": "ledger", "direction": direction, **period}, note=f"{count} factura(s) en el libro")
    elif code == "FACTURAS":
        count = len(database.scalars(select(Invoice.id).where(Invoice.invoice_date.between(date_from, date_to), Invoice.review_status == "APPROVED", Invoice.document_id.is_not(None))).all())
        if count:
            result.update(status="ready", artifact={"type": "invoices", **period}, note=f"{count} factura(s) en PDF")
        else:
            result["note"] = "No hay facturas aprobadas en ese periodo."
    elif code == "EXTRACTOS":
        count = len(database.scalars(select(BankTransaction.id).where(BankTransaction.booking_date.between(date_from, date_to))).all())
        if count:
            result.update(status="ready", artifact={"type": "bank", **period}, note=f"{count} movimiento(s)")
        else:
            result["note"] = "Importa el extracto del banco de ese periodo (Negocio → Banco)."
    elif code == "MODELOS":
        result.update(status="ready", artifact={"type": "models", **period}, note="Borrador calculado; añade el justificante de presentación si lo tienes")
    elif code == "NOMINAS":
        months = range(((period.get("quarter") or 1) - 1) * 3 + 1, (period.get("quarter") or 4) * 3 + 1)
        runs = database.scalars(select(PayrollRun).where(PayrollRun.year == period["year"], PayrollRun.month.in_(list(months)), PayrollRun.status != "DRAFT")).all()
        if runs:
            result.update(status="ready", artifact={"type": "payroll", "run_ids": [run.id for run in runs]}, note=f"{len(runs)} nómina(s) aprobada(s)")
        else:
            result["note"] = "No hay nóminas aprobadas de ese periodo en CapaFiscal."
    elif code == "CONTRATOS_TRABAJO":
        docs = database.scalars(select(EmployeeDocument).where(EmployeeDocument.kind == "CONTRATO")).all()
        active = database.scalars(select(Employee).where(Employee.termination_date.is_(None))).all()
        with_contract = {item.employee_id for item in docs}
        missing = [" ".join(filter(None, [item.first_name, item.last_name])) for item in active if item.id not in with_contract]
        if docs and not missing:
            result.update(status="ready", artifact={"type": "employee_docs", "ids": [item.id for item in docs]}, note=f"{len(docs)} contrato(s)")
        elif docs:
            result.update(status="partial", artifact={"type": "employee_docs", "ids": [item.id for item in docs]}, note=f"Faltan los de: {', '.join(missing[:4])}")
        else:
            result["note"] = "Sube los contratos a la ficha de cada persona (Equipo)."
    elif code == "RELACION_CREDITOS":
        pending = facts.get("embargo_pending") or []
        result.update(status="ready", artifact={"type": "credits"}, note=f"{len(pending)} factura(s) pendientes con el embargado")

    return result


class GestorIncidencias(Agent):
    code = "gestor"
    name = "Gestor de incidencias"
    role = "Trabaja el expediente: qué piden, qué falta, qué hacer y el borrador de respuesta."
    icon = "briefcase"

    def run(self, ctx: AgentContext) -> StepResult:
        database = ctx.database
        case = ctx.case
        facts = ctx.facts
        procedure = facts.get("procedure") or "OTRO"
        rules = PROCEDURES.get(procedure, PROCEDURES["OTRO"])
        extracted = facts.get("llm_extraction")
        engine = "reglas"

        # Embargo de créditos: qué le debemos al embargado.
        affected = facts.get("affected")
        if procedure == "EMBARGO" and affected and affected.get("tax_id"):
            pending = database.scalars(
                select(Invoice).where(
                    Invoice.supplier_tax_id == affected["tax_id"],
                    (Invoice.direction.is_(None)) | (Invoice.direction != "ISSUED"),
                    Invoice.paid_at.is_(None),
                    Invoice.review_status != "REJECTED",
                )
            ).all()
            facts["embargo_pending"] = [
                {"invoice_id": item.id, "number": item.invoice_number, "date": item.invoice_date.strftime("%d/%m/%Y") if item.invoice_date else "", "total": float(item.total or 0)}
                for item in pending
            ]

        # 1) Qué documentos se piden
        items = requested_items(ctx.text)
        if extracted:
            engine = llm.engine_label()
            for requested in extracted.get("requested_documents", []):
                codes = match_documents(normalize_search_text(f"{requested['label']} {requested['detail']}")) or ["OTRO"]
                if codes[0] == "OTRO" or not any(item["code"] == codes[0] for item in items):
                    items.append({"code": codes[0], "detail": requested["label"] + (f" ({requested['detail']})" if requested.get("detail") else "")})
        for code in rules["default_documents"]:
            if not any(item["code"] == code for item in items):
                items.append({"code": code, "detail": None})
        if procedure == "EMBARGO" and facts.get("subtype") != "EMBARGO_SALARIOS":
            items.insert(0, {"code": "RELACION_CREDITOS", "detail": None})

        period = main_period(facts)
        previous = {item["code"] + (item.get("detail") or ""): item for item in (case.required_documents or [])}
        documents = []
        for item in items:
            catalog = DOCUMENTS.get(item["code"], {"label": item.get("detail") or "Documentación solicitada", "source": "internal"})
            resolution = resolve(database, item["code"], period, facts)
            key = item["code"] + (item.get("detail") or "")
            earlier = previous.get(key, {})
            status = earlier.get("status") if earlier.get("status") in {"received", "provided", "not_applicable", "requested"} else resolution["status"]
            documents.append(
                {
                    "code": item["code"],
                    "label": catalog["label"] if item["code"] != "OTRO" else (item.get("detail") or "Documentación solicitada")[:120],
                    "detail": item.get("detail"),
                    "source": catalog["source"],
                    "source_label": SOURCE_LABELS[catalog["source"]],
                    "status": status,
                    "artifact": resolution["artifact"],
                    "note": resolution["note"],
                    "period_label": period_label(period) if catalog["source"] == "system" else None,
                    "attachment_ids": earlier.get("attachment_ids", []),
                }
            )

        # 2) Qué hacer
        actions = [{"label": label, "done": False} for label in rules["actions"]]
        insights: list[str] = []
        if facts.get("embargo_pending"):
            total = sum(item["total"] for item in facts["embargo_pending"])
            insights.append(
                f"Tienes {len(facts['embargo_pending'])} factura(s) pendientes de pagar a {affected.get('name') or affected['tax_id']} por {eur(total)}: "
                "NO se las pagues; quedan retenidas para la Administración."
            )
        elif procedure == "EMBARGO" and affected:
            insights.append(f"No hay pagos pendientes a {affected.get('name') or affected.get('tax_id')}: contesta indicando que no existen créditos.")
        if facts.get("subtype") == "EMBARGO_SALARIOS" and affected:
            insights.append(f"Retén cada mes en la nómina de {affected.get('name')} la parte embargable según el art. 607 LEC y avisa a quien prepare las nóminas.")
        for reference in facts.get("tax_references", []):
            if reference.get("notified_vs_draft") is not None and abs(reference["notified_vs_draft"]) >= 1:
                insights.append(
                    f"La Administración reclama {eur(reference['notified_vs_draft'])} más de lo que dan tus datos del {reference['model']}: revisa si falta alguna factura o si hay un error."
                    if reference["notified_vs_draft"] > 0
                    else f"El importe notificado es {eur(-reference['notified_vs_draft'])} inferior a lo que dan tus datos del {reference['model']}."
                )
        insights += facts.get("fiscal_notes", [])
        if extracted:
            for label in extracted.get("recommended_actions", []):
                if label and all(label.lower() != action["label"].lower() for action in actions):
                    actions.append({"label": label, "done": False})
        if rules.get("note"):
            insights.append(rules["note"])
        # Conserva lo que la persona ya marcó como hecho.
        done = {action["label"] for action in (case.proposed_actions or []) if action.get("done")}
        for action in actions:
            action["done"] = action["label"] in done

        # 3) Plazo interno de seguridad
        deadline = facts.get("deadline")
        internal = None
        if deadline:
            internal = max(ctx.today, subtract_business_days(deadline, 2))
        case.internal_deadline = internal

        # 4) Resumen para humanos
        missing = [item for item in documents if item["status"] in {"missing", "partial"}]
        ready = [item for item in documents if item["status"] == "ready"]
        if extracted and extracted.get("summary"):
            summary = extracted["summary"]
        else:
            parts = [f"{facts.get('procedure_label', rules['label'])} de {facts.get('issuer_label', '')}".strip()]
            if facts.get("amount"):
                parts.append(f"por {eur(facts['amount'])}")
            summary = " ".join(parts) + "."
            if documents:
                summary += " Pide: " + "; ".join(item["label"].lower() for item in documents[:5]) + "."
        if insights:
            summary += " " + insights[0]

        # 5) Borrador de respuesta
        company = database.scalar(select(CompanyProfile).limit(1))
        company_data = {
            "name": company.name if company else None,
            "tax_id": company.tax_id if company else None,
            "address": company.address if company else None,
            "city": company.city if company else None,
        }
        draft = build_letter(rules.get("letter"), facts, company_data, documents, ctx.today)
        if draft and llm.available():
            improved = llm.draft_letter(
                {
                    "empresa": company_data,
                    "tramite": facts.get("procedure_label"),
                    "organismo": facts.get("issuer_label"),
                    "referencia": facts.get("reference"),
                    "importe": facts.get("amount"),
                    "documentos": [item["label"] for item in documents],
                    "resumen": summary,
                    "observaciones": insights,
                },
                draft,
            )
            if improved:
                draft = improved
                engine = llm.engine_label()
        if not case.draft_edited:
            case.draft_response = draft

        case.required_documents = documents
        case.proposed_actions = actions
        case.summary = summary
        facts["insights"] = insights

        result_summary = f"{len(ready)} de {len(documents)} documento(s) preparados por el agente" if documents else "Sin documentación que aportar"
        if missing:
            result_summary += f"; faltan {len(missing)}"
        if draft:
            result_summary += "; borrador de respuesta listo"
        if internal:
            result_summary += f"; objetivo: tenerlo el {internal:%d/%m}"

        return StepResult(
            summary=result_summary + ".",
            output={"documents": documents, "actions": actions, "insights": insights, "internal_deadline": internal, "has_draft": bool(draft)},
            evidence=[evidence("document_check", f"{item['label']}: {item['status']}", note=item["note"]) for item in documents],
            engine=engine,
        )
