"""
Clasificador de expedientes: a quién afecta la notificación, qué trámite es
exactamente y abre (o reutiliza) el expediente.
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import func
from sqlalchemy import select

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import evidence
from app.agents.knowledge import PROCEDURES
from app.company_service import company_name
from app.company_service import company_tax_ids
from app.extractor import TAX_ID_PATTERN
from app.extractor import is_valid_spanish_tax_id
from app.extractor import normalize_search_text
from app.extractor import normalize_tax_id
from app.models import Case
from app.models import CaseEvent
from app.models import Customer
from app.models import Employee
from app.models import Invoice

SUBJECT_LABELS = {
    "company": "tu empresa",
    "employee": "una persona de la plantilla",
    "customer": "un cliente",
    "supplier": "un proveedor",
    "unknown": "un tercero no identificado",
}

# Subtipo de trámite por palabras clave (el texto ya normalizado).
SUBTYPES = (
    ("EMBARGO_SALARIOS", "EMBARGO", ("sueldos, salarios", "sueldos salarios", "embargo de salario", "retribuciones", "sueldos y salarios")),
    ("EMBARGO_CREDITOS", "EMBARGO", ("embargo de creditos", "creditos que", "creditos a favor", "creditos pendientes", "debera retener")),
    ("EMBARGO_CUENTAS", "EMBARGO", ("cuentas bancarias", "depositos", "saldos en cuenta")),
    ("COMPROBACION_LIMITADA", "REQUERIMIENTO", ("comprobacion limitada",)),
    ("VERIFICACION_DATOS", "REQUERIMIENTO", ("verificacion de datos",)),
    ("REQUERIMIENTO_TERCEROS", "REQUERIMIENTO", ("articulo 93", "art. 93", "informacion con trascendencia tributaria")),
)

SUBTYPE_LABELS = {
    "EMBARGO_SALARIOS": "Embargo de salario de una persona de la plantilla",
    "EMBARGO_CREDITOS": "Embargo de créditos de un tercero",
    "EMBARGO_CUENTAS": "Embargo de cuentas",
    "COMPROBACION_LIMITADA": "Requerimiento en procedimiento de comprobación limitada",
    "VERIFICACION_DATOS": "Requerimiento en procedimiento de verificación de datos",
    "REQUERIMIENTO_TERCEROS": "Requerimiento de información sobre terceros",
}

OWNER_HINTS = ("obligado tributario", "contribuyente", "destinatario", "deudor", "titular", "razon social", "sujeto pasivo", "empresa")


def next_case_code(database, year: int) -> str:
    count = database.scalar(select(func.count()).select_from(Case).where(Case.code.like(f"EXP-{year}-%"))) or 0
    return f"EXP-{year}-{count + 1:04d}"


def find_tax_ids(text: str, known: dict[str, dict] | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        for match in TAX_ID_PATTERN.finditer(line.upper()):
            tax_id = normalize_tax_id(match.group(0))
            if not tax_id or tax_id in found:
                continue
            # Un NIF con el dígito de control mal se acepta si ya lo conocemos.
            if not is_valid_spanish_tax_id(tax_id) and tax_id not in (known or {}):
                continue
            context = normalize_search_text(" ".join(lines[max(0, index - 1): index + 1]))
            found[tax_id] = {"tax_id": tax_id, "owner_hint": any(hint in context for hint in OWNER_HINTS), "line": line.strip()[:160]}
    return list(found.values())


def directory(database) -> dict[str, dict]:
    """NIF → quién es para la empresa."""
    people: dict[str, dict] = {}
    for tax_id in company_tax_ids(database):
        people[tax_id] = {"type": "company", "name": company_name(database) or "Tu empresa", "ref_id": None}
    for employee in database.scalars(select(Employee).where(Employee.tax_id.is_not(None))).all():
        people.setdefault(employee.tax_id.upper(), {"type": "employee", "name": " ".join(filter(None, [employee.first_name, employee.last_name])), "ref_id": employee.id})
    for customer in database.scalars(select(Customer).where(Customer.tax_id.is_not(None))).all():
        people.setdefault(customer.tax_id.upper(), {"type": "customer", "name": customer.name, "ref_id": customer.id})
    rows = database.execute(
        select(Invoice.supplier_tax_id, Invoice.supplier_name, Invoice.direction, Invoice.customer_tax_id, Invoice.customer_name)
    ).all()
    for supplier_tax, supplier_name, direction, customer_tax, customer_name in rows:
        if direction != "ISSUED" and supplier_tax:
            people.setdefault(supplier_tax.upper(), {"type": "supplier", "name": supplier_name, "ref_id": None})
        if direction == "ISSUED" and customer_tax:
            people.setdefault(customer_tax.upper(), {"type": "customer", "name": customer_name, "ref_id": None})
    return people


def open_case_for_event(ctx: AgentContext, *, announce: bool = True) -> Case:
    """Abre el expediente de una factura o un plazo (las notificaciones lo abren en ``run``)."""
    database = ctx.database
    event = ctx.event
    year = ctx.today.year
    if event.kind == "invoice":
        invoice = ctx.facts["invoice_obj"]
        from app.agents.detector import SEVERITY

        severity = ctx.signals.get("severity") or "medium"
        case = Case(
            code=next_case_code(database, year),
            kind="ANOMALY",
            procedure="FACTURA_SOSPECHOSA",
            title=f"Factura sospechosa de {invoice.supplier_name or 'proveedor'} · {invoice.invoice_number or 's/n'}"[:255],
            status="OPEN",
            facts={"origin": "invoice", "severity": severity, "severity_score": SEVERITY[severity]},
            required_documents=[],
            proposed_actions=[],
            antecedents=[],
            fingerprint=f"factura:{invoice.id}",
            subject_type="supplier",
            subject_name=invoice.supplier_name,
            subject_tax_id=invoice.supplier_tax_id,
            reference=invoice.invoice_number,
            amount=invoice.total,
            document_id=invoice.document_id,
            deadline=invoice.due_date if not invoice.paid_at and invoice.due_date and invoice.due_date >= ctx.today else None,
        )
    else:
        period = ctx.facts["period"]
        case = Case(
            code=next_case_code(database, year),
            kind="DEADLINE",
            procedure=f"MODELO_{period['model']}",
            title=f"Modelo {period['model']} · {period['quarter']}T {period['year']}",
            status="OPEN",
            facts={"origin": "deadline"},
            required_documents=[],
            proposed_actions=[],
            antecedents=[],
            fingerprint=f"plazo:{period['model']}:{period['year']}-{period['quarter']}",
            subject_type="company",
            subject_name=company_name(database) or "Tu empresa",
            organism="AEAT",
            reference=ctx.facts.get("reference"),
            deadline=ctx.facts.get("deadline"),
        )
    database.add(case)
    database.flush()
    ctx.case = case
    if announce:
        database.add(
            CaseEvent(case_id=case.id, kind="agent", actor="expedientes", title=f"Expedientes · Abierto {case.code}: {case.title}"[:255], data={"event": event.kind})
        )
    return case


class ClasificadorExpedientes(Agent):
    code = "expedientes"
    name = "Expedientes"
    role = "Identifica a quién afecta cada documento, qué trámite es y abre el expediente."
    icon = "archive"
    handles = ("notification", "invoice", "deadline")
    needs_case = False
    consumes = ("evento del Vigilante", "texto del documento", "directorio de NIF (empresa, plantilla, clientes, proveedores)")
    produces = ("expediente (abierto o reutilizado)", "a quién afecta", "trámite y subtipo", "tercero afectado")

    def run(self, ctx: AgentContext) -> StepResult:
        kind = ctx.event.kind if ctx.event else "notification"
        if kind == "invoice":
            return self.run_invoice(ctx)
        if kind == "deadline":
            return self.run_deadline(ctx)
        return self.run_notification(ctx)

    def run_invoice(self, ctx: AgentContext) -> StepResult:
        invoice = ctx.facts["invoice_obj"]
        known = directory(ctx.database)
        tax_id = (invoice.supplier_tax_id or "").upper() or None
        match = known.get(tax_id) if tax_id else None
        previous = 0
        if tax_id:
            previous = ctx.database.scalar(
                select(func.count()).select_from(Invoice).where(Invoice.supplier_tax_id == tax_id, Invoice.id != invoice.id)
            ) or 0
        role = (match or {}).get("type", "supplier")
        subject = {"type": "supplier", "name": invoice.supplier_name or (match or {}).get("name"), "tax_id": tax_id, "ref_id": None}
        ctx.facts.update({"procedure": "FACTURA", "procedure_label": "Factura recibida", "subject": subject, "supplier_known": previous > 0, "supplier_previous": previous})
        notes = []
        if role in {"employee", "customer"}:
            notes.append(f"el NIF es también de {SUBJECT_LABELS[role]} ({match['name']})")
        who = f"{subject['name'] or 'proveedor sin nombre'}" + (f" ({tax_id})" if tax_id else " (sin NIF)")
        if ctx.case is not None:
            summary = f"Actualizado {ctx.case.code}: proveedor {who}, {previous} factura(s) anteriores."
        elif previous:
            summary = f"Proveedor conocido: {who}, {previous} factura(s) anteriores. Expediente solo si el Detector encuentra algo."
        else:
            summary = f"Proveedor nuevo: {who}. Expediente solo si el Detector encuentra algo."
        if notes:
            summary += " Ojo: " + "; ".join(notes) + "."
        return StepResult(
            summary=summary,
            output={"subject": subject, "previous_invoices": previous, "case_code": ctx.case.code if ctx.case else None},
            evidence=[evidence("tax_id", f"{tax_id} → {subject['name']}")] if tax_id else [],
        )

    def run_deadline(self, ctx: AgentContext) -> StepResult:
        created = ctx.case is None
        case = ctx.case or open_case_for_event(ctx, announce=False)
        case.deadline = ctx.facts.get("deadline")
        ctx.facts.update({"procedure": case.procedure, "procedure_label": f"Presentación del modelo {ctx.facts['period']['model']}", "subject": {"type": "company", "name": case.subject_name}})
        return StepResult(
            summary=f"{'Abierto' if created else 'Actualizado'} {case.code}: {case.title}, a presentar por {(case.subject_name or 'tu empresa').rstrip('.')}.",
            output={"case_code": case.code, "procedure": case.procedure},
        )

    def run_notification(self, ctx: AgentContext) -> StepResult:
        database = ctx.database
        notification = ctx.facts["notification"]
        normalized = normalize_search_text(ctx.text)
        known = directory(database)
        tax_ids = find_tax_ids(ctx.text, known)

        for item in tax_ids:
            item["match"] = known.get(item["tax_id"])

        # Subtipo del trámite
        procedure = notification.notification_type
        subtype = None
        for code, parent, keywords in SUBTYPES:
            if parent == procedure and any(keyword in normalized for keyword in keywords):
                subtype = code
                break

        # Titular: la empresa salvo que el texto diga otra cosa.
        matched = [item for item in tax_ids if item["match"]]
        own = next((item for item in matched if item["match"]["type"] == "company"), None)
        third = next((item for item in matched if item["match"]["type"] != "company"), None)
        unknown_third = next((item for item in tax_ids if not item["match"]), None)

        subject = {"type": "company", "name": company_name(database) or "Tu empresa", "tax_id": own["tax_id"] if own else None, "ref_id": None}
        affected = None

        if procedure == "EMBARGO":
            target = third or unknown_third
            if target:
                affected = {
                    "type": target["match"]["type"] if target["match"] else "unknown",
                    "name": target["match"]["name"] if target["match"] else None,
                    "tax_id": target["tax_id"],
                    "ref_id": target["match"]["ref_id"] if target["match"] else None,
                }
                if subtype is None:
                    subtype = "EMBARGO_SALARIOS" if affected["type"] == "employee" else "EMBARGO_CREDITOS"
        elif not own and third and third["owner_hint"]:
            # Notificación dirigida a otra persona de la que es responsable la empresa.
            subject = {"type": third["match"]["type"], "name": third["match"]["name"], "tax_id": third["tax_id"], "ref_id": third["match"]["ref_id"]}
        elif not own and not third and unknown_third and company_tax_ids(database):
            # No aparece nuestro NIF y sí uno que no conocemos: no se inventa el titular.
            subject = {"type": "unknown", "name": f"NIF {unknown_third['tax_id']}", "tax_id": unknown_third["tax_id"], "ref_id": None}
            ctx.facts.setdefault("intake_warnings", []).append(
                f"Va dirigida al NIF {unknown_third['tax_id']}, que no es el de tu empresa ni el de nadie conocido: comprueba a quién corresponde antes de actuar."
            )

        rules = PROCEDURES.get(procedure, PROCEDURES["OTRO"])
        label = SUBTYPE_LABELS.get(subtype or "", rules["label"])
        title = f"{label} · {ctx.facts['issuer_label']}"

        case = database.scalar(select(Case).where(Case.notification_id == notification.id))
        created = case is None
        if created:
            year = (ctx.today or date.today()).year
            case = Case(
                code=next_case_code(database, year),
                kind="NOTIFICATION",
                status="OPEN",
                facts={},
                required_documents=[],
                proposed_actions=[],
                antecedents=[],
                notification_id=notification.id,
            )
            database.add(case)

        case.procedure = subtype or procedure
        case.title = title[:255]
        case.subject_type = subject["type"]
        case.subject_name = subject["name"]
        case.subject_tax_id = subject["tax_id"]
        case.subject_ref_id = subject["ref_id"]
        case.organism = notification.issuer
        case.reference = notification.reference
        case.deadline = notification.deadline
        case.amount = notification.amount
        case.document_id = notification.document_id
        database.flush()
        ctx.case = case

        ctx.facts.update({"procedure": procedure, "subtype": subtype, "procedure_label": label, "subject": subject, "affected": affected, "tax_ids": tax_ids})

        who = (subject["name"] or "").rstrip(".") if subject["type"] == "company" else f"{subject['name']} ({SUBJECT_LABELS[subject['type']]})"
        summary = f"{'Abierto' if created else 'Actualizado'} {case.code}: {label.lower()} que afecta a {who}"
        if affected:
            summary += f"; embargado: {(affected['name'] or affected['tax_id']).rstrip('.')} ({SUBJECT_LABELS[affected['type']]})"

        return StepResult(
            summary=summary + ".",
            output={"case_code": case.code, "procedure": case.procedure, "subject": subject, "affected": affected, "tax_ids": tax_ids},
            evidence=[evidence("tax_id", f"{item['tax_id']} → {item['match']['name'] if item['match'] else 'sin identificar'}", line=item["line"]) for item in tax_ids[:6]],
        )
