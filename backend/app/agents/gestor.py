"""
Gestor de incidencias: trabaja el expediente. Entiende qué se pide, reúne lo
que ya está en el sistema, detecta lo que falta, propone qué hacer y prepara
el borrador de respuesta para que una persona solo tenga que revisarlo.
"""
from __future__ import annotations

from app import clock
import re
from datetime import date
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents import llm
from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import RISK_ORDER
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
    lines = []
    for line in text.splitlines():
        # Dos apartados pegados en una línea («… euros. c) Justificantes …»): se separan.
        lines += [part for part in re.split(r"\s+(?=(?:[a-h]\)|\d{1,2}[.)])\s+[A-ZÁÉÍÓÚ])", line) if part.strip()]
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
        detail = match.group(1).strip().rstrip(";")
        if detail.endswith(".") and not re.search(r"\b[A-Z]\.[A-Z]\.$", detail):
            detail = detail[:-1]
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


INVOICE_REFERENCE = re.compile(r"\b[A-Z0-9]{1,8}(?:[-/][A-Z0-9]{1,10}){1,3}\b")


def cited_invoice_numbers(detail: str | None) -> list[str]:
    """Números de factura concretos que cita la petición («SFD-2026-001, SFD-2026-002 y …»)."""
    numbers = []
    for value in INVOICE_REFERENCE.findall((detail or "").upper()):
        if not re.search(r"\d", value) or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{1,4}/\d{4}", value):
            continue  # fechas y referencias normativas («RD 1619/2012»)
        if value not in numbers:
            numbers.append(value)
    return numbers


def resolve(database, code: str, period: dict[str, Any] | None, facts: dict[str, Any], detail: str | None = None) -> dict[str, Any]:
    """¿Puede el sistema preparar este documento? Estado y cómo."""
    catalog = DOCUMENTS.get(code, {"label": "Documentación solicitada", "source": "internal"})
    result: dict[str, Any] = {"status": "missing", "artifact": None, "note": None}

    numbers = cited_invoice_numbers(detail) if code == "FACTURAS" else []
    if numbers:
        found = set(database.scalars(select(Invoice.invoice_number).where(Invoice.invoice_number.in_(numbers), Invoice.review_status != "REJECTED")).all())
        missing = [number for number in numbers if number not in found]
        result["missing_numbers"] = missing
        if not missing:
            result.update(status="ready", artifact={"type": "invoices", "numbers": numbers}, note=f"Las {len(numbers)} factura(s) citadas están en CapaFiscal")
        else:
            # Las que faltan las tiene el proveedor: se le piden.
            result.update(status="partial" if found else "missing", artifact={"type": "invoices", "numbers": sorted(found)} if found else None,
                          note=f"Falta(n): {', '.join(missing)}", source="third")
        return result

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


def supplier_query_draft(invoice: Any, findings: list[Any], company: Any) -> str:
    """Borrador de consulta al proveedor (no sale sin visto bueno)."""
    points = "\n".join(f"  • {item.por_que}" for item in findings if item.agente == "detector")
    return (
        f"Asunto: Consulta sobre su factura {invoice.invoice_number or ''}\n\n"
        f"Buenos días:\n\nAntes de tramitar su factura {invoice.invoice_number or ''} de {invoice.invoice_date:%d/%m/%Y} "
        f"por {eur(invoice.total)}, necesitamos aclarar lo siguiente:\n\n{points}\n\n"
        "¿Pueden confirmarnos si es correcta o, en su caso, enviarnos la factura rectificativa?\n\n"
        f"Un saludo,\n{(company.name if company else None) or '[Tu empresa]'}"
    )


class GestorIncidencias(Agent):
    code = "gestor"
    name = "Gestor de incidencias"
    role = "Trabaja el expediente: qué piden, qué falta, qué hacer y el borrador de respuesta."
    icon = "briefcase"
    handles = ("notification", "invoice", "deadline")
    consumes = ("expediente", "hallazgos de Fiscal, Memoria y Detector", "catálogo de trámites y documentos")
    produces = ("documentos necesarios y quién los aporta", "acciones propuestas", "recomendación", "borrador de respuesta", "plazo interno", "señal missing_documents")

    def run(self, ctx: AgentContext) -> StepResult:
        kind = ctx.event.kind if ctx.event else "notification"
        if kind == "invoice":
            return self.run_invoice(ctx)
        if kind == "deadline":
            return self.run_deadline(ctx)
        return self.run_notification(ctx)

    def run_invoice(self, ctx: AgentContext) -> StepResult:
        case = ctx.case
        invoice = ctx.facts["invoice_obj"]
        detected = [item for item in ctx.findings if item.agente == "detector"]
        others = [item for item in ctx.findings if item.agente != "detector"]
        actions: list[dict[str, Any]] = []

        def add(label: str | None) -> None:
            if label and all(label != item["label"] for item in actions):
                actions.append({"label": label, "done": False})

        high = any(item.riesgo == "high" for item in detected)
        if not invoice.paid_at and (high or any(item.tipo == "POSIBLE_DUPLICADO" for item in detected)):
            add("Retener el pago de esta factura hasta aclararlo")
        for item in sorted(detected, key=lambda finding: -RISK_ORDER[finding.riesgo]):
            add(item.siguiente)
        for item in others:
            add(item.siguiente)
        add("Si todo es correcto, descártalo explicando por qué: la próxima vez se tendrá en cuenta")

        done = {action["label"] for action in (case.proposed_actions or []) if action.get("done")}
        extra = [action for action in (case.proposed_actions or []) if action.get("by") == "human"]
        for action in actions:
            action["done"] = action["label"] in done
        case.proposed_actions = actions + [action for action in extra if action["label"] not in {item["label"] for item in actions}]

        recommendation = actions[0]["label"]
        ctx.facts["recommendation"] = recommendation
        said = recommendation[:1].lower() + recommendation[1:].rstrip(".")
        ctx.facts["insights"] = [item.por_que for item in [*detected, *others]]
        headline = detected[0].resultado if detected else "Factura a revisar"
        case.summary = f"{headline.rstrip('.')}. {detected[0].por_que if detected else ''}".strip() + f" Recomendación: {said}."
        company = ctx.database.scalar(select(CompanyProfile).limit(1))
        if detected and not case.draft_edited:
            case.draft_response = supplier_query_draft(invoice, detected, company)
        case.required_documents = case.required_documents or []
        return StepResult(
            summary=f"Recomendación: {said}. {len(actions)} acción(es) propuestas" + ("; borrador de consulta al proveedor listo." if detected else "."),
            output={"recommendation": recommendation, "actions": actions, "has_draft": bool(case.draft_response)},
            evidence=[evidence("finding", f"{item.resultado} ({item.agente})") for item in ctx.findings[:6]],
        )

    def run_deadline(self, ctx: AgentContext) -> StepResult:
        case = ctx.case
        period = ctx.facts["period"]
        if ctx.facts.get("not_applicable"):
            # El Fiscal ha visto que este modelo no corresponde: se descarta solo, con el motivo.

            note = (ctx.facts.get("fiscal_notes") or ["Este modelo no aplica a tu empresa."])[0]
            case.status, case.resolution, case.resolved_at = "DISMISSED", note, clock.now()
            case.summary = note
            case.proposed_actions = []
            ctx.facts["insights"] = [note]
            ctx.facts["recommendation"] = "Nada que hacer: descartado automáticamente."
            return StepResult(summary=f"Descartado: {note}", output={"dismissed": True})
        reference = (ctx.facts.get("tax_references") or [{}])[0]
        anomalies = ctx.facts.get("anomalies") or []
        actions: list[dict[str, Any]] = []
        if reference.get("invoices_pending"):
            actions.append({"label": f"Revisar las {reference['invoices_pending']} factura(s) pendientes del {period['quarter']}T", "done": False})
        relevant = [item for item in anomalies if item["severity"] in {"medium", "high"}]
        if relevant:
            actions.append({"label": f"Resolver {len(relevant)} anomalía(s) antes de presentar (Expedientes · Anomalías)", "done": False})
        actions.append({"label": f"Revisar el borrador del {period['model']} y su resultado", "done": False})
        actions.append({"label": f"Presentar el {period['model']} en la sede de la AEAT y registrar el justificante", "done": False})
        done = {action["label"] for action in (case.proposed_actions or []) if action.get("done")}
        for action in actions:
            action["done"] = action["label"] in done
        case.proposed_actions = actions

        deadline = ctx.facts.get("deadline")
        case.internal_deadline = max(ctx.today, subtract_business_days(deadline, 3)) if deadline else None
        result = reference.get("draft_result")
        case.summary = (
            f"El {period['model']} del {period['quarter']}T {period['year']} vence el {deadline:%d/%m/%Y}."
            + (f" Borrador: {eur(result)}." if result is not None else "")
            + (f" Antes: {actions[0]['label'].lower()}." if len(actions) > 2 else " Todo listo para revisar y presentar.")
        )
        ctx.facts["recommendation"] = actions[0]["label"]
        ctx.facts["insights"] = ctx.facts.get("fiscal_notes", [])
        return StepResult(
            summary=f"{len(actions)} paso(s) hasta presentar; objetivo: tenerlo el {case.internal_deadline:%d/%m}." if case.internal_deadline else f"{len(actions)} paso(s) hasta presentar.",
            output={"actions": actions, "internal_deadline": case.internal_deadline},
        )

    def run_notification(self, ctx: AgentContext) -> StepResult:
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
            resolution = resolve(database, item["code"], period, facts, item.get("detail"))
            key = item["code"] + (item.get("detail") or "")
            earlier = previous.get(key, {})
            status = earlier.get("status") if earlier.get("status") in {"received", "provided", "not_applicable", "requested"} else resolution["status"]
            documents.append(
                {
                    "code": item["code"],
                    "label": catalog["label"] if item["code"] != "OTRO" else (item.get("detail") or "Documentación solicitada")[:120],
                    "detail": item.get("detail"),
                    "source": resolution.get("source") or catalog["source"],
                    "source_label": SOURCE_LABELS[resolution.get("source") or catalog["source"]],
                    "status": status,
                    "artifact": resolution["artifact"],
                    "note": resolution["note"],
                    "period_label": period_label(period) if catalog["source"] == "system" else None,
                    "attachment_ids": earlier.get("attachment_ids", []),
                    "missing_numbers": resolution.get("missing_numbers") or [],
                }
            )

        # 2) Qué hacer
        actions = [{"label": label, "done": False} for label in rules["actions"]]
        insights: list[str] = []
        missing_invoices = [number for item in documents for number in item.get("missing_numbers") or []]
        if missing_invoices:
            insights.append(f"Piden facturas que no están en CapaFiscal: {', '.join(missing_invoices)}. Hay que pedírselas al proveedor antes de contestar.")
        if procedure == "EMBARGO" and affected and facts.get("subtype") != "EMBARGO_SALARIOS":
            insights += embargo_insights(ctx, affected)
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
        llm_calls: list[dict[str, Any]] = []
        if draft and llm.available():
            improved, llm_meta = llm.draft_letter(
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
            llm_calls.append(llm_meta)
            if improved:
                draft = improved
                engine = llm.engine_label()
        if not case.draft_edited:
            case.draft_response = draft

        case.required_documents = documents
        case.proposed_actions = actions
        case.summary = summary
        facts["insights"] = insights
        facts["recommendation"] = actions[0]["label"] if actions else None

        result_summary = f"{len(ready)} de {len(documents)} documento(s) preparados por el agente" if documents else "Sin documentación que aportar"
        if missing:
            result_summary += f"; faltan {len(missing)}"
        if draft:
            result_summary += "; borrador de respuesta listo"
        if internal:
            result_summary += f"; objetivo: tenerlo el {internal:%d/%m}"

        return StepResult(
            summary=result_summary + ".",
            output={"documents": documents, "actions": actions, "insights": insights, "internal_deadline": internal, "has_draft": bool(draft),
                    **({"llm": llm_calls} if llm_calls else {})},  # modelo, tokens, coste y resultado (queda en AgentStep.output)
            evidence=[evidence("document_check", f"{item['label']}: {item['status']}", note=item["note"]) for item in documents],
            engine=engine,
            signals={"missing_documents": sum(1 for item in missing if item["source"] in {"internal", "third"})},
        )


def embargo_insights(ctx: AgentContext, affected: dict) -> list[str]:
    """Cuánto retener: lo calcula app.debt (determinista), aquí solo se explica."""
    from decimal import Decimal
    from statistics import median

    from app.debt import amount_to_retain
    from app.debt import is_successive

    facts = ctx.facts
    who = affected.get("name") or affected.get("tax_id")
    pending = facts.get("embargo_pending") or []
    credit = sum((Decimal(str(item["total"])) for item in pending), Decimal("0"))
    notification = facts.get("notification")
    debt = Decimal(str(notification.amount)) if notification is not None and notification.amount is not None else None
    successive = is_successive(ctx.text)
    periodic = None
    if successive and affected.get("tax_id"):
        totals = ctx.database.scalars(
            select(Invoice.total).where(Invoice.supplier_tax_id == affected["tax_id"], Invoice.total.is_not(None)).order_by(Invoice.invoice_date.desc()).limit(6)
        ).all()
        periodic = Decimal(str(median(totals))) if totals else None
    retention = amount_to_retain(debt, credit, successive=successive, periodic_payment=periodic)
    facts["embargo"] = {**retention.as_dict(), "debt": float(debt) if debt is not None else None, "credit": float(credit)}

    lines: list[str] = []
    breakdown = (notification.debt or {}) if notification is not None else {}
    if breakdown:
        from app.debt import Debt

        values = {key: (Decimal(str(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else value) for key, value in breakdown.items()}
        lines.append("Deuda embargada: " + Debt(**values).explanation() + ".")
    if credit > 0 and debt is None:
        lines.append(f"Le debes {eur(credit)} a {who} ({len(pending)} factura(s)): NO se lo pagues; retenlo hasta confirmar el importe de la deuda, que no se ha podido leer.")
    elif credit > 0:
        text = f"Retén {eur(retention.retain_now)} de lo que le debes a {who} (le debes {eur(credit)}; la deuda embargada es {eur(debt)})"
        text += f" e ingrésalo en el Tesoro. El resto, {eur(retention.release)}, puedes pagárselo." if retention.release > 0 else " e ingrésalo en el Tesoro."
        lines.append(text)
    else:
        lines.append(f"No hay pagos pendientes a {who}: contesta indicando que no existen créditos a su favor.")
    if successive and retention.pending_after > 0:
        text = f"Embargo de pagos sucesivos: retén también cada pago futuro a {who} hasta completar {eur(debt)}; faltan {eur(retention.pending_after)}"
        text += f" (unos {retention.payments_needed} pago(s) de {eur(periodic)})." if retention.payments_needed else "."
        lines.append(text)
    return lines
