"""
Evaluador de expedientes completos (bancos B y C).

Para cada caso: base de datos vacía → contexto (empresa, plantilla,
facturas previas, modelos presentados) → entradas en orden (subidas,
correos, eventos de plazo, extractos) por las mismas puertas que en
producción → se observa lo que hizo el sistema → se compara con caso.json.

    python -m evaluation casos --dataset b_sintetico

No cambia nada del sistema: solo lo usa y mira el resultado.

Comprobaciones (cada una es acierto o fallo; lo que el caso no dice no se mira):

    type, route, procedure, procedure_not, issuer, reference, affected,
    deadline, deadline_pending_confirmation, debt_amount, credit_amount,
    requires_human, expected_agents, expected_findings, findings_excluded,
    requested_documents, requested_documents_excluded, document_status,
    insight_contains, tax_reference, fiscal_applies, not_invoice, ocr,
    addressee_unknown, duplicate, invoice (por campo) · filed_period, memory

Hallazgos semánticos (no son tipos del Detector; se comprueban así):
    credit_exceeds_debt  el crédito observado supera la deuda y lo que se
                         recomienda menciona retener el importe de la DEUDA
    successive_payments  lo que se recomienda habla de pagos sucesivos /
                         cada pago / cada mes / futuros
    no_pending_payment   no hay crédito pendiente y se recomienda contestar
                         que no existen créditos
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

DATASETS = Path(__file__).resolve().parent / "datasets"
REPORTS = Path(__file__).resolve().parent / "informes"

CHECK_LABELS = {
    "type": "Tipo de documento",
    "route": "Ruta",
    "procedure": "Trámite",
    "procedure_not": "Trámite que NO es",
    "issuer": "Organismo",
    "reference": "Nº de expediente",
    "affected": "Afectado (embargado)",
    "deadline": "Plazo",
    "deadline_pending_confirmation": "Plazo sin fecha de notificación (no inventar)",
    "debt_amount": "Importe de la deuda",
    "credit_amount": "Crédito pendiente con el embargado",
    "requires_human": "¿Necesita a una persona?",
    "expected_agents": "Agentes que intervienen",
    "expected_findings": "Hallazgos",
    "findings_excluded": "Hallazgos que NO deben salir",
    "requested_documents": "Documentos pedidos",
    "requested_documents_excluded": "Documentos que NO se piden",
    "document_status": "Estado de los documentos pedidos",
    "insight_contains": "Lo que recomienda",
    "tax_reference": "Modelo y periodo afectados",
    "fiscal_applies": "Impacto fiscal",
    "not_invoice": "No tomarlo por factura",
    "ocr": "Escaneado: no darlo por bueno",
    "addressee_unknown": "Destinatario desconocido",
    "duplicate": "Duplicado",
    "invoice": "Campos de la factura",
    "filed_period": "Periodo presentado registrado",
    "memory": "La Memoria lo encuentra",
}

INVOICE_FIELDS = ("supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number", "invoice_date", "subtotal", "tax_total", "withholding_total", "total", "direction")
DONE_STATUSES = {"RESOLVED", "DISMISSED", "FILED"}


def norm(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", value)


def eur(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def money(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return f"{Decimal(str(value)):.2f}"


# ---------------------------------------------------------------------
# Entorno
# ---------------------------------------------------------------------


def prepare_environment(engine: str) -> Path | None:
    """Base de datos temporal y, para «reglas», sin clave de IA. Antes de importar la app."""
    if "app.config" in sys.modules:
        return None
    work = Path(tempfile.mkdtemp(prefix="capafiscal-casos-"))
    os.environ.update({
        "DATA_DIR": str(work / "data"), "UPLOAD_DIR": str(work / "uploads"), "DATABASE_URL": f"sqlite:///{(work / 'casos.db').as_posix()}",
        "ENABLE_SCHEDULER": "false",
    })
    if engine == "reglas":
        os.environ["ANTHROPIC_API_KEY"] = ""
    return work


def reset_database() -> None:
    from app.config import settings
    from app.database import create_database_tables
    from app.database import engine
    from app.migrate import reset_database

    reset_database(engine)
    create_database_tables()
    shutil.rmtree(settings.upload_dir, ignore_errors=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)


def load_context(client, case: dict[str, Any]) -> None:
    from app.database import SessionLocal
    from app.models import Document
    from app.models import Invoice

    context = case.get("contexto") or {}
    company = context.get("empresa") or {}
    client.put("/api/company", json={"legal_form": "SOCIEDAD", "city": "Burgos", **company}).raise_for_status()
    for employee in context.get("empleados", []):
        client.post("/api/team/employees", json=employee).raise_for_status()
    for filing in context.get("presentaciones", []):
        client.post("/api/taxes/filings", json=filing).raise_for_status()
    invoices = context.get("facturas", [])
    if invoices:
        with SessionLocal() as database:
            for index, item in enumerate(invoices):
                document = Document(
                    original_filename=f"previa_{index}.pdf", stored_filename=f"previa_{case['case_id']}_{index}.pdf", sha256=f"{case['case_id']}-{index}".ljust(64, "0")[:64],
                    extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE",
                )
                database.add(document)
                database.flush()
                database.add(Invoice(
                    document_id=document.id, supplier_name=item["supplier_name"], supplier_tax_id=item["supplier_tax_id"], direction="RECEIVED",
                    invoice_number=item["invoice_number"], invoice_date=date.fromisoformat(item["invoice_date"]), subtotal=Decimal(item["subtotal"]),
                    tax_total=Decimal(item["tax_total"]), total=Decimal(item["total"]), currency="EUR", confidence=95, field_confidences={},
                    validation_status="VALID", validation_messages=[], review_status="APPROVED", duplicate_status="NONE",
                    paid_at=date.fromisoformat(item["paid_at"]) if item.get("paid_at") else None,
                ))
            database.commit()


# ---------------------------------------------------------------------
# Observación: qué hizo el sistema con una entrada
# ---------------------------------------------------------------------


def observe(*, document_id: int | None = None, event_id: int | None = None, duplicate_signal: bool = False) -> dict[str, Any]:
    from sqlalchemy import select

    from app.agents.orchestrator import ROUTES
    from app.database import SessionLocal
    from app.interpretation import needs_help
    from app.models import AgentRun
    from app.models import AgentStep
    from app.models import Case
    from app.models import Document
    from app.models import ExtractionRun
    from app.models import FiscalNotification
    from app.models import IngestedEvent
    from app.models import Invoice

    with SessionLocal() as database:
        document = database.get(Document, document_id) if document_id else None
        invoice = database.scalar(select(Invoice).where(Invoice.document_id == document_id)) if document_id else None
        notification = database.scalar(select(FiscalNotification).where(FiscalNotification.document_id == document_id)) if document_id else None
        events = []
        if event_id:
            events = [database.get(IngestedEvent, event_id)]
        elif document_id:
            events = [item for item in database.scalars(select(IngestedEvent).order_by(IngestedEvent.id)).all() if (item.payload or {}).get("document_id") == document_id]
        event = events[-1] if events else None

        case = database.get(Case, event.case_id) if event is not None and event.case_id else None
        if case is None and notification is not None:
            case = database.scalar(select(Case).where(Case.notification_id == notification.id))
        if case is None and document_id:
            case = database.scalar(select(Case).where(Case.document_id == document_id).order_by(Case.id.desc()).limit(1))
        facts = (case.facts or {}) if case else {}

        run_ids = [item.run_id for item in events if item and item.run_id]
        steps = database.scalars(select(AgentStep).where(AgentStep.run_id.in_(run_ids)).order_by(AgentStep.run_id, AgentStep.position)).all() if run_ids else []
        runs = [database.get(AgentRun, run_id) for run_id in run_ids]
        findings = {item.get("tipo") for step in steps for item in (step.output or {}).get("findings", [])}
        findings |= {item.get("tipo") for item in facts.get("findings") or []}
        findings |= set(facts.get("finding_types") or [])

        route = (facts.get("route") or {}).get("code")
        if route is None and runs:
            summary = runs[-1].summary or ""
            route = next((code for code, item in ROUTES.items() if summary.startswith(item["label"])), None)

        # ¿Entró Claude en este documento? (lo deja anotado interpretation.refine)
        ai = None
        if document is not None:
            from app.models import AuditEvent

            audit = database.scalar(
                select(AuditEvent).where(AuditEvent.action == "document.interpretation", AuditEvent.entity_id == str(document.id)).order_by(AuditEvent.id.desc()).limit(1)
            )
            if audit is not None:
                data = audit.event_data or {}
                ai = {key: data.get(key) for key in ("reasons", "changed", "fallback", "model", "input_tokens", "output_tokens", "cost_usd")}

        flags: list[str] = []  # dudas de la lectura de FACTURA: solo cuentan si se tomó por factura
        if document is not None and invoice is not None:
            extraction = database.scalar(select(ExtractionRun).where(ExtractionRun.document_id == document.id).order_by(ExtractionRun.id.desc()).limit(1))
            if extraction is not None and extraction.result_json:
                from app.company_service import company_tax_ids

                flags = needs_help(extraction.result_json, company_tax_ids(database))
            if invoice is not None and invoice.validation_status not in (None, "VALID"):
                flags.append(f"validación: {invoice.validation_status}")

        documents = case.required_documents if case else []
        texts = list(facts.get("insights") or []) + [facts.get("recommendation") or ""] + [item.get("label", "") for item in (case.proposed_actions or [])] if case else []
        texts += [f"{item.get('code')} {item.get('detail') or ''}" for item in documents or []]
        texts += [item.label for item in case.requests] if case else []
        pending = facts.get("embargo_pending")

        if document is not None and notification is not None:
            kind = "NOTIFICATION"
        elif invoice is not None:
            kind = "INVOICE"
        elif event is not None and event.kind == "deadline":
            kind = "DEADLINE"
        else:
            kind = (document.kind if document is not None and document.kind else "OTHER") if document is not None else None

        case_open = case is not None and case.status not in DONE_STATUSES
        requires_human = bool(
            (event is not None and (event.status in {"NEEDS_HUMAN", "FAILED"} or event.case_id is not None and case_open))
            or (case_open and event is None)
            or (document is not None and document.requires_ocr)
        )

        return {
            "type": kind,
            "route": route,
            "procedure": (case.procedure if case and case.procedure else None) or (notification.notification_type if notification else None),
            "issuer": (notification.issuer if notification else None) or (case.organism if case else None),
            "reference": (case.reference if case else None) or (notification.reference if notification else None),
            "affected": facts.get("affected"),
            "subject": {"type": case.subject_type, "tax_id": case.subject_tax_id} if case else None,
            "intake_warnings": facts.get("intake_warnings") or [],
            "deadline": ((case.deadline if case else None) or (notification.deadline if notification else None)),
            "deadline_rule": facts.get("deadline_rule") or (notification.deadline_rule if notification else None),
            "debt_amount": float(case.amount) if case and case.amount is not None else (float(notification.amount) if notification and notification.amount is not None else None),
            "credit_amount": round(sum(item.get("total", 0) for item in pending), 2) if pending is not None else None,
            "requires_human": requires_human,
            "agents": [step.agent for step in steps],
            "findings": sorted(item for item in findings if item),
            "documents": [{"code": item.get("code"), "status": item.get("status"), "detail": item.get("detail")} for item in documents or []],
            "insights": " · ".join(item for item in texts if item),
            "tax_references": facts.get("tax_references") or [],
            "event_status": event.status if event else None,
            "case_code": case.code if case else None,
            "case_status": case.status if case else None,
            "requires_ocr": bool(document.requires_ocr) if document is not None else False,
            "document_status": document.status if document is not None else None,
            "duplicate": duplicate_signal or bool(invoice is not None and invoice.duplicate_status not in (None, "NONE")) or "POSIBLE_DUPLICADO" in findings,
            "invoice": {
                "supplier_name": invoice.supplier_name, "supplier_tax_id": invoice.supplier_tax_id, "customer_tax_id": invoice.customer_tax_id, "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None, "subtotal": money(invoice.subtotal), "tax_total": money(invoice.tax_total),
                "withholding_total": money(invoice.withholding_total), "total": money(invoice.total), "direction": invoice.direction,
            } if invoice is not None else None,
            "flags": flags,
            "ai": ai,
        }


# ---------------------------------------------------------------------
# Comparación
# ---------------------------------------------------------------------


def semantic_finding(name: str, observed: dict[str, Any], expected: dict[str, Any]) -> bool:
    text = norm(observed["insights"])
    if name == "credit_exceeds_debt":
        debt = observed.get("debt_amount")
        credit = observed.get("credit_amount")
        return bool(debt and credit and credit > debt and eur(debt) in text and re.search(r"reten|retien|ingres", text))
    if name == "successive_payments":
        return bool(re.search(r"suces|cada pago|cada mes|mensual|futur|a medida que", text))
    if name == "no_pending_payment":
        return not observed.get("credit_amount") and bool(re.search(r"no (existen|hay) (creditos|pagos pendientes)", text))
    return name in observed["findings"]


def compare_invoice(expected: dict[str, Any], observed: dict[str, Any] | None, flags: list[str]) -> dict[str, Any]:
    fields = {}
    for name in INVOICE_FIELDS:
        if name not in expected:
            continue
        want = expected[name]
        got = (observed or {}).get(name)
        if name in {"subtotal", "tax_total", "withholding_total", "total"}:
            want, got = money(want), money(got)
        elif name == "supplier_name":  # v0.5: sin mayúsculas, tildes ni puntuación final
            want, got = (norm(want).strip(" .·,") or None) if want else None, (norm(got).strip(" .·,") or None) if got else None
        elif name.endswith("tax_id"):
            want, got = (want or "").upper() or None, (got or "").upper() or None
        fields[name] = {"expected": want, "observed": got, "ok": want == got}
    errors = [name for name, item in fields.items() if not item["ok"]]
    outcome = "correcto" if not errors else ("detectado" if flags else "error_silencioso")
    return {"fields": fields, "errors": errors, "outcome": outcome}


def check(name: str, expected: Any, observed: dict[str, Any], spec: dict[str, Any]) -> tuple[bool, str]:
    """(acierto, explicación breve de lo observado)."""
    if name == "type":
        return observed["type"] == expected, str(observed["type"])
    if name == "route":
        return observed["route"] == expected, str(observed["route"])
    if name == "procedure":
        allowed = expected if isinstance(expected, list) else [expected]
        return observed["procedure"] in allowed, str(observed["procedure"])
    if name == "procedure_not":
        return observed["procedure"] not in expected, str(observed["procedure"])
    if name == "issuer":
        return observed["issuer"] == expected, str(observed["issuer"])
    if name == "reference":
        return norm(observed["reference"]) == norm(expected), str(observed["reference"])
    if name == "affected":
        got = observed["affected"] or {}
        return got.get("type") == expected.get("type") and (got.get("tax_id") or "").upper() == expected.get("tax_id", "").upper(), f"{got.get('type')} {got.get('tax_id')}"
    if name == "deadline":
        got = observed["deadline"].isoformat() if observed["deadline"] else None
        return got == expected, str(got)
    if name == "deadline_pending_confirmation":
        rule = norm(observed["deadline_rule"])
        marked = observed["deadline"] is None or any(word in rule for word in ("indica la fecha", "fecha real", "suponiendo", "estimad", "confirm"))
        return marked, f"{observed['deadline']} · {observed['deadline_rule']}"
    if name == "debt_amount":
        got = observed["debt_amount"]
        return got is not None and abs(got - expected) < 0.01, str(got)
    if name == "credit_amount":
        got = observed["credit_amount"]
        return got is not None and abs(got - expected) < 0.01 if expected else not got, str(got)
    if name == "requires_human":
        return observed["requires_human"] == expected, f"{observed['requires_human']} (evento {observed['event_status']}, expediente {observed['case_status']})"
    if name == "expected_agents":
        missing = [item for item in expected if item not in observed["agents"]]
        return not missing, ("faltan " + ", ".join(missing)) if missing else "todos"
    if name == "expected_findings":
        missing = [item for item in expected if not semantic_finding(item, observed, spec)]
        return not missing, ("faltan " + ", ".join(missing)) if missing else ", ".join(observed["findings"]) or "—"
    if name == "findings_excluded":
        present = [item for item in expected if item in observed["findings"]]
        return not present, ", ".join(present) or "ninguno"
    if name == "requested_documents":
        codes = [item["code"] for item in observed["documents"]]
        missing = [item for item in expected if item not in codes]
        return not missing, ("faltan " + ", ".join(missing)) if missing else ", ".join(codes)
    if name == "requested_documents_excluded":
        present = [item["code"] for item in observed["documents"] if item["code"] in expected]
        return not present, ", ".join(present) or "ninguno"
    if name == "document_status":
        problems = []
        for code, allowed in expected.items():
            statuses = [item["status"] for item in observed["documents"] if item["code"] == code]
            if not statuses or not any(status in allowed for status in statuses):
                problems.append(f"{code}={statuses or 'no pedido'}")
        return not problems, "; ".join(problems) or "ok"
    if name == "insight_contains":
        text = norm(observed["insights"])
        missing = [item for item in expected if norm(item) not in text]
        return not missing, ("no dice " + ", ".join(missing)) if missing else "ok"
    if name == "tax_reference":
        found = [item for item in observed["tax_references"] if str(item.get("model")) == expected["model"] and item.get("year") == expected["year"] and item.get("quarter") == expected["quarter"]]
        listed = ", ".join(f"{item.get('model')} {item.get('quarter')}T {item.get('year')}" for item in observed["tax_references"]) or "ninguno"
        return bool(found), listed
    if name == "fiscal_applies":
        has = bool(observed["tax_references"])
        return has == expected, "con modelos" if has else "sin modelos"
    if name == "not_invoice":
        return observed["type"] != "INVOICE", str(observed["type"])
    if name == "ocr":
        ok = observed["requires_ocr"] and observed["type"] != "INVOICE" and observed["requires_human"]
        return ok, f"ocr={observed['requires_ocr']} tipo={observed['type']} humano={observed['requires_human']}"
    if name == "addressee_unknown":
        subject = observed["subject"] or {}
        warned = any(expected in str(item) or "destinat" in norm(item) for item in observed["intake_warnings"])
        ok = observed["requires_human"] and (subject.get("type") not in (None, "company") or warned or observed["case_code"] is None)
        return ok, f"titular={subject.get('type')} avisos={len(observed['intake_warnings'])}"
    if name == "duplicate":
        return observed["duplicate"] == expected, str(observed["duplicate"])
    raise KeyError(name)


def evaluate_expected(expected: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    checks = []
    invoice = None
    for name, value in expected.items():
        if name == "invoice":
            invoice = compare_invoice(value, observed["invoice"], observed["flags"])
            checks.append({"check": "invoice", "ok": invoice["outcome"] == "correcto", "observed": invoice["outcome"] + (f" ({', '.join(invoice['errors'])})" if invoice["errors"] else ""), "expected": "todos los campos"})
            continue
        ok, detail = check(name, value, observed, expected)
        checks.append({"check": name, "ok": ok, "observed": detail, "expected": value})
    return {"checks": checks, "invoice": invoice}


def outcome_of(result: dict[str, Any], observed: dict[str, Any]) -> str:
    """Igual que en la evaluación de documentos: bien solo, a persona, o error sin avisar."""
    if all(item["ok"] for item in result["checks"]):
        return "correcto"
    if observed["requires_human"] or observed["flags"] or observed["event_status"] in {"NEEDS_HUMAN", "FAILED"}:
        return "detectado"
    return "error_silencioso"


# ---------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------


def run_case(client, folder: Path) -> dict[str, Any]:
    case = json.loads((folder / "caso.json").read_text(encoding="utf-8"))
    reset_database()
    load_context(client, case)
    started = time.perf_counter()
    entries = []
    for index, entry in enumerate(case["entradas"]):
        try:
            expectations = run_entry(client, folder, entry)
        except Exception as error:  # un fallo del sistema es un resultado, no un fallo del evaluador
            message = f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"
            wanted = entry.get("expected") or {key: value for item in entry.get("expected_adjuntos", []) for key, value in item.items()}
            entries.append({
                "entrada": entry.get("archivo") or entry["tipo"], "outcome": "error_sistema", "observed": {"error": message}, "invoice": None,
                "checks": [{"check": name, "ok": False, "observed": f"error del sistema ({message})", "expected": value} for name, value in wanted.items()],
            })
            continue
        for label, expected, observed in expectations:
            if observed is None:
                result = {"checks": [{"check": name, "ok": False, "observed": "no llegó", "expected": value} for name, value in expected.items()], "invoice": None}
                entries.append({"entrada": label, **result, "outcome": "error_silencioso", "observed": None})
                continue
            result = evaluate_expected(expected, observed)
            entries.append({"entrada": label, **result, "outcome": outcome_of(result, observed), "observed": summarize(observed)})

    global_checks = []
    expected = case.get("expected") or {}
    if "filed_period" in expected:
        global_checks.append(check_filed(expected["filed_period"]))
    if "memory" in expected:
        global_checks.append(check_memory(client, expected["memory"]))
    if global_checks:
        entries.append({"entrada": "expediente", "checks": global_checks, "invoice": None, "outcome": "correcto" if all(item["ok"] for item in global_checks) else "detectado", "observed": None})

    return {
        "case_id": case["case_id"], "bloque": case["bloque"], "titulo": case["titulo"], "etiquetas": case["etiquetas"],
        "entries": entries, "seconds": round(time.perf_counter() - started, 2),
    }


def run_entry(client, folder: Path, entry: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """Mete una entrada por su puerta de producción y devuelve (etiqueta, esperado, observado)."""
    kind = entry["tipo"]
    expectations: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    if kind == "subida":
        path = folder / entry["archivo"]
        response = client.post("/api/upload", files={"uploaded_file": (path.name, path.read_bytes(), "application/pdf")})
        response.raise_for_status()
        body = response.json()
        observed = observe(document_id=body["document"]["id"], duplicate_signal=bool(body.get("duplicate")))
        expectations.append((entry["archivo"], entry["expected"], observed))
    elif kind == "correo":
        path = folder / entry["archivo"]
        response = client.post("/api/connectors/email/import", files={"uploaded_file": (path.name, path.read_bytes(), "message/rfc822")})
        response.raise_for_status()
        attachments = response.json()["attachments"]
        for position, expected in enumerate(entry["expected_adjuntos"]):
            item = attachments[position] if position < len(attachments) else None
            observed = observe(document_id=item["document_id"], duplicate_signal=bool(not item["new_document"] or item["duplicate"])) if item else None
            expectations.append((f"{entry['archivo']}#{position + 1}", expected, observed))
    elif kind == "plazo":
        event = json.loads((folder / entry["archivo"]).read_text(encoding="utf-8"))
        response = client.post("/api/events", json=event)
        response.raise_for_status()
        body = response.json()
        observed = observe(event_id=body["event"]["id"])
        expectations.append((entry["archivo"], entry["expected"], observed))
    elif kind == "banco":
        path = folder / entry["archivo"]
        client.post("/api/bank/import", files={"uploaded_file": (path.name, path.read_bytes(), "text/csv")}).raise_for_status()
    elif kind == "analisis":
        client.post("/api/agents/anomalies/scan").raise_for_status()
        expectations.append(("análisis", entry["expected"], observe_scan()))
    return expectations


def observe_scan() -> dict[str, Any]:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Case

    with SessionLocal() as database:
        cases = database.scalars(select(Case).where(Case.kind == "ANOMALY")).all()
        findings = sorted({case.procedure for case in cases if case.procedure})
    return {
        "type": None, "route": None, "procedure": None, "issuer": None, "reference": None, "affected": None, "subject": None, "intake_warnings": [], "deadline": None,
        "deadline_rule": None, "debt_amount": None, "credit_amount": None, "requires_human": bool(cases), "agents": ["detector"], "findings": findings, "documents": [],
        "insights": "", "tax_references": [], "event_status": None, "case_code": None, "case_status": None, "requires_ocr": False, "document_status": None,
        "duplicate": False, "invoice": None, "flags": [],
    }


def check_filed(expected: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import TaxFiling

    with SessionLocal() as database:
        found = database.scalar(select(TaxFiling).where(TaxFiling.model == expected["model"], TaxFiling.year == expected["year"], TaxFiling.period == expected["quarter"]))
    return {"check": "filed_period", "ok": found is not None, "observed": "registrado" if found else "no consta", "expected": expected}


def check_memory(client, expected: dict[str, Any]) -> dict[str, Any]:
    body = client.post("/api/memory/ask", json={"question": expected["question"]}).json()
    sources = body.get("sources") or []
    hit = any(norm(expected["source_contains"]) in norm(json.dumps(source, ensure_ascii=False, default=str)) for source in sources)
    return {"check": "memory", "ok": hit, "observed": f"{len(sources)} fuente(s)" + (" con el dato" if hit else " sin el dato"), "expected": expected["source_contains"]}


def summarize(observed: dict[str, Any]) -> dict[str, Any]:
    keep = ("type", "route", "procedure", "issuer", "reference", "affected", "subject", "intake_warnings", "deadline", "deadline_rule", "debt_amount", "credit_amount", "requires_human",
            "agents", "findings", "event_status", "case_code", "case_status", "requires_ocr", "duplicate", "invoice", "flags", "insights", "ai")
    data = {key: observed.get(key) for key in keep}
    data["deadline"] = data["deadline"].isoformat() if data["deadline"] else None
    data["documents"] = [f"{item['code']}:{item['status']}" for item in observed.get("documents") or []]
    return data


def run(dataset: str, *, engine: str = "reglas", blind: bool = False) -> dict[str, Any]:
    folder = DATASETS / dataset
    if not folder.is_dir():
        raise SystemExit(f"No existe el banco {folder}. Genéralo primero (python -m evaluation generar-b).")
    index = json.loads((folder / "indice.json").read_text(encoding="utf-8"))
    if index.get("conjunto") == "C" and not blind:
        raise SystemExit("El banco C es ciego: solo se ejecuta con --ciego, una vez y sin cambiar reglas después.")
    work = prepare_environment(engine)
    from fastapi.testclient import TestClient

    from app.main import app

    results = []
    try:
        with TestClient(app) as client:
            for item in index["casos"]:
                results.append(run_case(client, folder / item["case_id"]))
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return {"dataset": dataset, "engine": engine, "date": date.today().isoformat(), "cases": results, "summary": summarize_run(results)}


def summarize_run(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_check: dict[str, Counter] = defaultdict(Counter)
    by_block: dict[str, Counter] = defaultdict(Counter)
    by_tag: dict[str, Counter] = defaultdict(Counter)
    outcomes: Counter = Counter()
    invoice_fields: dict[str, Counter] = defaultdict(Counter)
    invoice_outcomes: Counter = Counter()
    for case in results:
        case_ok = all(check["ok"] for entry in case["entries"] for check in entry["checks"])
        by_block[case["bloque"]]["casos"] += 1
        by_block[case["bloque"]]["casos_ok"] += case_ok
        for tag in case["etiquetas"]:
            by_tag[tag]["casos"] += 1
            by_tag[tag]["casos_ok"] += case_ok
        for entry in case["entries"]:
            outcomes[entry["outcome"]] += 1
            for item in entry["checks"]:
                by_check[item["check"]]["total"] += 1
                by_check[item["check"]]["ok"] += item["ok"]
                by_block[case["bloque"]]["checks"] += 1
                by_block[case["bloque"]]["checks_ok"] += item["ok"]
            if entry.get("invoice"):
                invoice_outcomes[entry["invoice"]["outcome"]] += 1
                for name, field in entry["invoice"]["fields"].items():
                    invoice_fields[name]["total"] += 1
                    invoice_fields[name]["ok"] += field["ok"]
    total_checks = sum(item["total"] for item in by_check.values())
    return {
        "cases": len(results),
        "cases_ok": sum(1 for case in results if all(check["ok"] for entry in case["entries"] for check in entry["checks"])),
        "checks": total_checks,
        "checks_ok": sum(item["ok"] for item in by_check.values()),
        "outcomes": dict(outcomes),
        "by_check": {key: dict(value) for key, value in by_check.items()},
        "by_block": {key: dict(value) for key, value in by_block.items()},
        "by_tag": {key: dict(value) for key, value in by_tag.items()},
        "invoice_fields": {key: dict(value) for key, value in invoice_fields.items()},
        "invoice_outcomes": dict(invoice_outcomes),
    }


def pct(ok: int, total: int) -> str:
    return f"{100 * ok / total:.0f} %" if total else "—"


def to_markdown(report: dict[str, Any]) -> str:
    from evaluation.banco_casos import BLOCKS

    summary = report["summary"]
    lines = [
        f"# Evaluación por expedientes · banco {report['dataset']} · motor {report['engine']} · {report['date']}",
        "",
        "Cada caso es un expediente (uno o varios documentos relacionados) con su verdad de referencia en `caso.json`. "
        "Las reglas NO se han tocado al ver estos resultados.",
        "",
        f"- **Expedientes completamente correctos:** {summary['cases_ok']}/{summary['cases']} ({pct(summary['cases_ok'], summary['cases'])})",
        f"- **Comprobaciones superadas:** {summary['checks_ok']}/{summary['checks']} ({pct(summary['checks_ok'], summary['checks'])})",
        f"- **Resultado por entrada:** " + " · ".join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(summary["outcomes"].items())),
        "",
        "Un **error silencioso** es una entrada mal resuelta en la que el sistema no avisa ni la manda a una persona: es lo más grave.",
        "",
        "## Por bloque",
        "",
        "| Bloque | Expedientes bien | Comprobaciones bien |",
        "|---|---|---|",
    ]
    for block, values in summary["by_block"].items():
        lines.append(f"| {BLOCKS.get(block, block)} | {values.get('casos_ok', 0)}/{values['casos']} | {values.get('checks_ok', 0)}/{values.get('checks', 0)} ({pct(values.get('checks_ok', 0), values.get('checks', 0))}) |")
    lines += ["", "## Por tipo de comprobación", "", "| Comprobación | Bien | % |", "|---|---|---|"]
    for name, values in sorted(summary["by_check"].items(), key=lambda item: item[1]["ok"] / item[1]["total"]):
        lines.append(f"| {CHECK_LABELS.get(name, name)} | {values['ok']}/{values['total']} | {pct(values['ok'], values['total'])} |")
    if summary["invoice_fields"]:
        lines += ["", "## Facturas: por campo", "", "| Campo | Bien |", "|---|---|"]
        for name, values in summary["invoice_fields"].items():
            lines.append(f"| {name} | {values['ok']}/{values['total']} ({pct(values['ok'], values['total'])}) |")
        lines.append("")
        lines.append("Facturas: " + " · ".join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(summary["invoice_outcomes"].items())))
    lines += ["", "## Por etiqueta", "", "| Etiqueta | Expedientes bien |", "|---|---|"]
    for tag, values in sorted(summary["by_tag"].items()):
        lines.append(f"| {tag} | {values.get('casos_ok', 0)}/{values['casos']} |")
    lines += ["", "## Fallos, caso a caso", ""]
    for case in report["cases"]:
        failures = [(entry["entrada"], item) for entry in case["entries"] for item in entry["checks"] if not item["ok"]]
        silent = [entry["entrada"] for entry in case["entries"] if entry["outcome"] == "error_silencioso"]
        mark = "✅" if not failures else ("⛔" if silent else "⚠️")
        lines.append(f"### {mark} {case['case_id']} · {case['titulo']}")
        if not failures:
            lines.append("Todo correcto.")
        for entrada, item in failures:
            expected = item["expected"] if not isinstance(item["expected"], (dict, list)) else json.dumps(item["expected"], ensure_ascii=False)
            lines.append(f"- `{entrada}` · **{CHECK_LABELS.get(item['check'], item['check'])}**: esperado {expected} · observado {item['observed']}")
        if silent:
            lines.append(f"- ⛔ Error silencioso en: {', '.join(silent)}")
        lines.append("")
    return "\n".join(lines)


def save(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = f"casos_{report['dataset']}_{report['engine']}_{report['date']}"
    json_path = REPORTS / f"{stamp}.json"
    md_path = REPORTS / f"{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path
