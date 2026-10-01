"""
Expedientes: lo que ve y hace la persona sobre el trabajo de los agentes.

Revisar → aprobar el escrito → presentar en sede → resolver. Adjuntos con
verificación automática, portal de subida para quien aporta documentos,
escrito en PDF y paquete listo para presentar.
"""
from __future__ import annotations

import csv
import io
import uuid
import zipfile
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.knowledge import DOCUMENTS
from app.agents.knowledge import PROCEDURES
from app.agents.registry import AGENTS_BY_CODE
from app.config import settings
from app.extractor import normalize_search_text
from app.invoice_service import add_audit_event
from app.models import AgentRun
from app.models import BankTransaction
from app.models import Case
from app.models import CaseAttachment
from app.models import CaseEvent
from app.models import CompanyProfile
from app.models import DocumentRequest
from app.models import EmployeeDocument
from app.models import FiscalNotification
from app.models import Invoice
from app.models import PayrollRun
from app.notification_service import ISSUERS

STATUS_LABELS = {
    "OPEN": "Abierto",
    "WAITING_HUMAN": "Espera tu revisión",
    "WAITING_DOCS": "Esperando documentación",
    "READY_TO_FILE": "Listo para presentar",
    "FILED": "Presentado",
    "RESOLVED": "Resuelto",
    "DISMISSED": "Descartado",
}
OPEN_STATUSES = {"OPEN", "WAITING_HUMAN", "WAITING_DOCS", "READY_TO_FILE"}
DOC_STATUS_LABELS = {
    "ready": "Preparado por el agente",
    "partial": "Preparado en parte",
    "missing": "Falta",
    "requested": "Pedido",
    "received": "Recibido",
    "provided": "Aportado",
    "not_applicable": "No aplica",
}
def _anomaly_labels() -> dict[str, str]:
    from app.agents.detector import ANOMALY_TYPES

    return {code: item["label"] for code, item in ANOMALY_TYPES.items()} | {"FACTURA_SOSPECHOSA": "Factura sospechosa"}


ANOMALY_LABELS = _anomaly_labels()
MAX_ATTACHMENT = 15 * 1024 * 1024
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".txt", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".zip"}


class CaseError(ValueError):
    pass


def event(database: Session, case: Case, title: str, *, kind: str = "human", actor: str = "user", detail: str | None = None, data: dict | None = None) -> None:
    database.add(CaseEvent(case_id=case.id, kind=kind, actor=actor, title=title[:255], detail=detail, data=data or {}))


# ---------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------


def procedure_label(case: Case) -> str:
    if case.kind == "ANOMALY":
        return ANOMALY_LABELS.get(case.procedure or "", "Anomalía")
    facts = case.facts or {}
    return facts.get("procedure_label") or PROCEDURES.get(case.procedure or "", PROCEDURES["OTRO"])["label"]


def serialize_case(database: Session, case: Case, *, full: bool = False, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    documents = case.required_documents or []
    days_left = (case.deadline - today).days if case.deadline else None
    data: dict[str, Any] = {
        "id": case.id,
        "code": case.code,
        "kind": case.kind,
        "procedure": case.procedure,
        "procedure_label": procedure_label(case),
        "title": case.title,
        "headline": case.headline,
        "status": case.status,
        "status_label": STATUS_LABELS.get(case.status, case.status),
        "priority": case.priority,
        "level": case.level,
        "summary": case.summary,
        "subject": {"type": case.subject_type, "name": case.subject_name, "tax_id": case.subject_tax_id},
        "organism": case.organism,
        "organism_label": ISSUERS.get(case.organism or "", None),
        "reference": case.reference,
        "deadline": case.deadline.isoformat() if case.deadline else None,
        "internal_deadline": case.internal_deadline.isoformat() if case.internal_deadline else None,
        "days_left": days_left,
        "amount": float(case.amount) if case.amount is not None else None,
        "documents_total": len(documents),
        "documents_ready": sum(1 for item in documents if item.get("status") in {"ready", "received", "provided", "not_applicable"}),
        "requests_pending": sum(1 for item in case.requests if item.status == "PENDING"),
        "has_draft": bool(case.draft_response),
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }
    if not full:
        return data

    facts = case.facts or {}
    last_run = database.scalar(select(AgentRun).where(AgentRun.case_id == case.id).order_by(AgentRun.id.desc()).limit(1))
    from app.agents.orchestrator import serialize_run

    data.update(
        {
            "facts": {
                key: facts.get(key)
                for key in ("insights", "fiscal_notes", "tax_references", "affected", "intake_warnings", "embargo_pending", "source", "document_name", "deadline_rule", "severity", "evidence", "median", "history", "supplier_history", "invoice", "invoice_tax", "period", "recommendation", "human_decision", "processing")
                if facts.get(key) not in (None, [], {})
            },
            "documents": [{**item, "status_label": DOC_STATUS_LABELS.get(item.get("status"), item.get("status"))} for item in documents],
            "actions": case.proposed_actions or [],
            "antecedents": case.antecedents or [],
            "draft_response": case.draft_response,
            "draft_edited": case.draft_edited,
            "attachments": [
                {"id": item.id, "filename": item.filename, "item_code": item.item_code, "source": item.source, "verification": item.verification, "created_at": item.created_at.isoformat() if item.created_at else None}
                for item in case.attachments
            ],
            "requests": [
                {"id": item.id, "label": item.label, "status": item.status, "reminders": item.reminders_sent, "to_email": item.to_email, "created_at": item.created_at.isoformat() if item.created_at else None}
                for item in case.requests
            ],
            "events": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "actor": item.actor,
                    "actor_name": AGENTS_BY_CODE.get(item.actor, {}).get("name") or ("Orquestador" if item.actor == "orquestador" else item.actor),
                    "icon": AGENTS_BY_CODE.get(item.actor, {}).get("icon"),
                    "title": item.title,
                    "detail": item.detail,
                    "data": item.data,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in case.events
            ],
            "run": serialize_run(last_run) if last_run else None,
            "route": facts.get("route"),
            "findings": facts.get("findings") or [],
            "notification_id": case.notification_id,
            "document_id": case.document_id,
            "filing_reference": case.filing_reference,
            "filed_at": case.filed_at.isoformat() if case.filed_at else None,
            "resolution": case.resolution,
            "letter_kind": PROCEDURES.get((case.facts or {}).get("procedure") or case.procedure or "", {}).get("letter"),
        }
    )
    return data


def list_cases(database: Session, *, view: str = "open", today: date | None = None) -> list[dict[str, Any]]:
    statement = select(Case)
    if view == "open":
        statement = statement.where(Case.status.in_(OPEN_STATUSES), Case.kind.in_(["NOTIFICATION", "DEADLINE"]))
    elif view == "anomalies":
        statement = statement.where(Case.status.in_(OPEN_STATUSES), Case.kind == "ANOMALY")
    elif view == "closed":
        statement = statement.where(Case.status.notin_(OPEN_STATUSES))
    cases = database.scalars(statement).all()
    items = [serialize_case(database, case, today=today) for case in cases]
    if view == "closed":
        items.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    else:
        items.sort(key=lambda item: (-item["priority"], item["deadline"] or "9999"))
    return items


# ---------------------------------------------------------------------
# Acciones humanas
# ---------------------------------------------------------------------


def update_case(database: Session, case: Case, data: dict[str, Any], actor: str) -> Case:
    if "draft_response" in data and data["draft_response"] != case.draft_response:
        case.draft_response = data["draft_response"]
        case.draft_edited = True
        event(database, case, "Borrador de respuesta editado", actor=actor)

    if "action_index" in data:
        actions = [dict(item) for item in (case.proposed_actions or [])]
        index = int(data["action_index"])
        if 0 <= index < len(actions):
            actions[index]["done"] = bool(data.get("done", True))
            case.proposed_actions = actions

    if "document_code" in data:
        status = data.get("document_status")
        if status not in {"provided", "not_applicable", "missing"}:
            raise CaseError("Estado de documento no válido.")
        documents = [dict(item) for item in (case.required_documents or [])]
        for item in documents:
            if item["code"] == data["document_code"] and (item.get("detail") or "") == (data.get("document_detail") or item.get("detail") or ""):
                item["status"] = status
                event(database, case, f"«{item['label']}» marcado como {DOC_STATUS_LABELS[status].lower()}", actor=actor)
                break
        case.required_documents = documents

    if data.get("add_action"):
        label = str(data["add_action"]).strip()[:200]
        if label:
            case.proposed_actions = [*(case.proposed_actions or []), {"label": label, "done": False, "by": "human"}]
            record_decision(case, "modified", actor, label)
            event(database, case, f"Acción añadida: {label}", actor=actor)

    if "deadline" in data and data["deadline"]:
        case.deadline = data["deadline"]
        event(database, case, f"Plazo ajustado al {case.deadline:%d/%m/%Y}", actor=actor)

    from app.agents.director import prioritize

    prioritize(database, case, date.today())
    return case


def sync_notification(database: Session, case: Case, status: str) -> None:
    if case.notification_id:
        notification = database.get(FiscalNotification, case.notification_id)
        if notification:
            notification.status = status
            if status == "CLOSED":
                notification.closed_at = datetime.now(timezone.utc)


def record_decision(case: Case, decision: str, actor: str, note: str | None) -> None:
    """La decisión humana queda en el expediente y en el registro de decisiones: la Memoria, el Detector y la evaluación aprenden de ella."""
    from sqlalchemy.orm import object_session

    from app.learning import record_case_decision

    database = object_session(case)
    if database is not None:
        record_case_decision(database, case, decision, actor, note)
    case.facts = {
        **(case.facts or {}),
        "human_decision": {"decision": decision, "actor": actor, "note": note, "at": datetime.now(timezone.utc).isoformat()},
    }


def approve(database: Session, case: Case, actor: str) -> Case:
    if case.status not in {"WAITING_HUMAN", "WAITING_DOCS", "OPEN"}:
        raise CaseError("Este expediente no está pendiente de aprobación.")
    missing = [item["label"] for item in (case.required_documents or []) if item.get("status") in {"missing", "requested", "partial"}]
    if case.kind == "ANOMALY":
        # Una anomalía no se presenta: aprobar es aceptar la recomendación.
        recommendation = (case.facts or {}).get("recommendation") or next((item["label"] for item in (case.proposed_actions or [])), "Revisado")
        case.status = "RESOLVED"
        case.resolution = f"Recomendación aprobada: {recommendation}"
        case.resolved_at = datetime.now(timezone.utc)
        record_decision(case, "approved", actor, recommendation)
        event(database, case, case.resolution, actor=actor)
    else:
        case.status = "READY_TO_FILE"
        record_decision(case, "approved", actor, None)
        event(
            database,
            case,
            ("Aprobado: listo para presentar" if case.kind == "DEADLINE" else "Escrito aprobado: listo para presentar en la sede electrónica")
            + (f" (atención: faltan {len(missing)} documento(s))" if missing else ""),
            actor=actor,
        )
    add_audit_event(database, action="case.approved", entity_type="case", entity_id=case.id, actor=actor, event_data={"code": case.code, "missing": missing})
    return case


def file_case(database: Session, case: Case, *, reference: str | None, filed_at: date | None, actor: str) -> Case:
    case.status = "FILED"
    case.filed_at = filed_at or date.today()
    case.filing_reference = (reference or "").strip() or None
    sync_notification(database, case, "ANSWERED")
    event(database, case, f"Presentado el {case.filed_at:%d/%m/%Y}" + (f" · registro {case.filing_reference}" if case.filing_reference else ""), actor=actor)
    add_audit_event(database, action="case.filed", entity_type="case", entity_id=case.id, actor=actor, event_data={"code": case.code, "reference": case.filing_reference})
    return case


def resolve(database: Session, case: Case, *, resolution: str | None, dismiss: bool, actor: str) -> Case:
    case.status = "DISMISSED" if dismiss else "RESOLVED"
    case.resolution = (resolution or "").strip() or ("Descartado: no requiere acción." if dismiss else "Resuelto.")
    case.resolved_at = datetime.now(timezone.utc)
    record_decision(case, "rejected" if dismiss else "resolved", actor, case.resolution)
    for request in case.requests:
        if request.status == "PENDING":
            request.status = "CANCELLED"
    sync_notification(database, case, "CLOSED")
    event(database, case, ("Descartado" if dismiss else "Resuelto") + f": {case.resolution}", actor=actor)
    add_audit_event(database, action="case.dismissed" if dismiss else "case.resolved", entity_type="case", entity_id=case.id, actor=actor, event_data={"code": case.code, "resolution": case.resolution})
    return case


def reopen(database: Session, case: Case, actor: str) -> Case:
    case.status = "WAITING_HUMAN"
    case.resolved_at = None
    sync_notification(database, case, "IN_PROGRESS")
    event(database, case, "Expediente reabierto", actor=actor)
    return case


# ---------------------------------------------------------------------
# Adjuntos y verificación
# ---------------------------------------------------------------------


def verify_file(path: Path, item_code: str | None, case: Case) -> dict[str, Any]:
    """Comprueba que el archivo es lo que se pidió (sin garantías: orienta a la persona)."""
    checks: list[dict[str, Any]] = []
    text = ""
    if path.suffix.lower() in {".pdf", ".txt"}:
        try:
            from app.extractor import read_document

            text, _pages, _ocr, _page_texts = read_document(path)
        except Exception:
            text = ""
    if not text.strip():
        return {"status": "unverified", "checks": [{"label": "No se puede leer el contenido automáticamente; revísalo tú.", "ok": None}]}

    normalized = normalize_search_text(text)
    checks.append({"label": "El documento se puede leer", "ok": True})
    code = (item_code or "").split(":")[0]
    catalog = DOCUMENTS.get(code)
    if catalog:
        found = any(keyword in normalized for keyword in catalog["keywords"])
        checks.append({"label": f"Parece un «{catalog['label'].lower()}»", "ok": found})
    if case.reference:
        checks.append({"label": f"Menciona la referencia {case.reference}", "ok": normalize_search_text(case.reference) in normalized})
    if code == "JUSTIFICANTE_PAGO" and case.amount:
        amount = f"{float(case.amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        checks.append({"label": f"Incluye el importe {amount} €", "ok": amount in text or amount.replace(".", "") in text})
    if case.subject_tax_id:
        checks.append({"label": f"Menciona el NIF {case.subject_tax_id}", "ok": case.subject_tax_id.lower() in normalized.replace(" ", "")})

    decisive = [item["ok"] for item in checks if item["ok"] is not None]
    status = "ok" if all(decisive) else "doubtful"
    return {"status": status, "checks": checks}


def store_attachment(database: Session, case: Case, *, filename: str, content: bytes, content_type: str | None, item_code: str | None, source: str) -> CaseAttachment:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED:
        raise CaseError("Formato no admitido (PDF, imagen, Excel, Word, CSV, TXT o ZIP).")
    if len(content) > MAX_ATTACHMENT:
        raise CaseError("El archivo supera los 15 MB.")
    folder = settings.upload_dir / "cases"
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{extension}"
    path = folder / stored
    path.write_bytes(content)

    attachment = CaseAttachment(
        case_id=case.id,
        item_code=item_code,
        filename=Path(filename).name[:255],
        stored_filename=f"cases/{stored}",
        mime_type=(content_type or "")[:100] or None,
        size_bytes=len(content),
        source=source,
        verification=verify_file(path, item_code, case),
    )
    database.add(attachment)
    case.attachments.append(attachment)
    database.flush()

    if item_code:
        documents = [dict(item) for item in (case.required_documents or [])]
        for item in documents:
            key = item["code"] if item["code"] != "OTRO" else f"OTRO:{(item.get('detail') or item['label'])[:30]}"
            if key == item_code or item["code"] == item_code:
                item["status"] = "received" if source == "client" else "provided"
                item["attachment_ids"] = [*item.get("attachment_ids", []), attachment.id]
                item["verification"] = attachment.verification["status"]
                break
        case.required_documents = documents
    return attachment


def attachment_path(attachment: CaseAttachment) -> Path:
    return settings.upload_dir / attachment.stored_filename


# ---------------------------------------------------------------------
# Portal de subida (sin usuario: enlace personal)
# ---------------------------------------------------------------------


def request_by_token(database: Session, token: str) -> DocumentRequest | None:
    """El portal es público: el enlace se busca en todos los clientes y la sesión pasa a trabajar SOLO para el suyo."""
    request = database.scalar(select(DocumentRequest).where(DocumentRequest.token == token).execution_options(all_tenants=True))
    if request is not None:
        database.info["tenant_id"] = request.tenant_id
    return request


def portal_info(database: Session, request: DocumentRequest) -> dict[str, Any]:
    case = request.case
    company = database.scalar(select(CompanyProfile).limit(1))
    return {
        "label": request.label,
        "status": request.status,
        "case_title": case.title,
        "reference": case.reference,
        "deadline": (case.internal_deadline or case.deadline).isoformat() if (case.internal_deadline or case.deadline) else None,
        "company": company.name if company else None,
    }


def receive_upload(database: Session, request: DocumentRequest, *, filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
    from app.agents.director import prioritize

    if request.status == "CANCELLED":
        raise CaseError("Esta petición ya no está activa.")
    case = request.case
    attachment = store_attachment(database, case, filename=filename, content=content, content_type=content_type, item_code=request.item_code, source="client")
    request.status = "RECEIVED"
    request.received_at = datetime.now(timezone.utc)
    request.attachment_id = attachment.id

    verdict = attachment.verification.get("status")
    event(
        database,
        case,
        f"Perseguidor · Recibido «{request.label}»"
        + (" y verificado" if verdict == "ok" else " — revisa el contenido" if verdict == "doubtful" else ""),
        kind="agent",
        actor="perseguidor",
        data={"attachment_id": attachment.id, "verification": attachment.verification},
    )

    if not any(item.status == "PENDING" for item in case.requests) and case.status == "WAITING_DOCS":
        case.status = "WAITING_HUMAN"
        event(database, case, "Perseguidor · Documentación completa: el expediente vuelve a ti para revisar y aprobar", kind="agent", actor="perseguidor")
    prioritize(database, case, date.today())
    add_audit_event(database, action="case.document_received", entity_type="case", entity_id=case.id, actor="portal", event_data={"request_id": request.id, "filename": attachment.filename})
    return {"received": True, "verification": attachment.verification}


# ---------------------------------------------------------------------
# Escrito en PDF y paquete para presentar
# ---------------------------------------------------------------------


def build_letter_pdf(database: Session, case: Case) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    if not case.draft_response:
        raise CaseError("Este expediente no tiene escrito de respuesta.")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 25 * mm, width - 22 * mm
    ink, muted = HexColor("#1c1a17"), HexColor("#6b675f")
    pdf.setTitle(f"Escrito {case.code}")
    y = height - 28 * mm

    def new_page() -> None:
        nonlocal y
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(muted)
        pdf.drawString(left, 12 * mm, f"{case.code} · borrador revisado en CapaFiscal")
        pdf.showPage()
        y = height - 25 * mm

    for raw in case.draft_response.splitlines():
        line = raw.rstrip()
        bold = line.strip() in {"EXPONE", "SOLICITA"} or line.isupper() and len(line) > 6
        font = "Helvetica-Bold" if bold else "Helvetica"
        size = 11 if line.strip() in {"EXPONE", "SOLICITA"} else 10
        if not line.strip():
            y -= 4 * mm
            continue
        indent = left + (6 * mm if line.startswith("   ") else 0)
        words = line.strip().split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if stringWidth(candidate, font, size) > right - indent:
                if y < 25 * mm:
                    new_page()
                pdf.setFont(font, size)
                pdf.setFillColor(ink)
                pdf.drawString(indent, y, current)
                y -= 5.2 * mm
                current = word
            else:
                current = candidate
        if current:
            if y < 25 * mm:
                new_page()
            pdf.setFont(font, size)
            pdf.setFillColor(ink)
            if line.strip() in {"EXPONE", "SOLICITA"}:
                pdf.drawCentredString(width / 2, y, current)
            else:
                pdf.drawString(indent, y, current)
            y -= 5.2 * mm

    pdf.setFont("Helvetica", 7)
    pdf.setFillColor(muted)
    pdf.drawString(left, 12 * mm, f"{case.code} · borrador revisado en CapaFiscal")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def artifact_files(database: Session, item: dict[str, Any], case: Case) -> list[tuple[str, bytes]]:
    """Genera los documentos que el agente marcó como preparados."""
    from app.reports_service import build_ledger_rows
    from app.reports_service import ledger_to_xlsx
    from app.reports_service import quarter_range

    artifact = item.get("artifact") or {}
    kind = artifact.get("type")
    files: list[tuple[str, bytes]] = []
    year, quarter = artifact.get("year"), artifact.get("quarter")
    suffix = f"{year}_{quarter}T" if quarter else f"{year}"

    if kind == "ledger":
        date_from, date_to = quarter_range(year, quarter)
        rows = build_ledger_rows(database, date_from=date_from, date_to=date_to, direction=artifact["direction"])
        name = "expedidas" if artifact["direction"] == "ISSUED" else "recibidas"
        files.append((f"libro_facturas_{name}_{suffix}.xlsx", ledger_to_xlsx(rows, title=f"Libro registro de facturas {name}", direction=artifact["direction"])))
    elif kind == "invoices":
        date_from, date_to = quarter_range(year, quarter)
        for invoice in database.scalars(select(Invoice).where(Invoice.invoice_date.between(date_from, date_to), Invoice.review_status == "APPROVED")).all():
            document = invoice.document
            if document and (settings.upload_dir / document.stored_filename).exists():
                party = (invoice.customer_name if invoice.direction == "ISSUED" else invoice.supplier_name) or "factura"
                safe = "".join(ch if ch.isalnum() else "_" for ch in f"{invoice.invoice_date:%Y%m%d}_{party}_{invoice.invoice_number or invoice.id}")[:90]
                files.append((f"facturas/{safe}{document.extension or '.pdf'}", (settings.upload_dir / document.stored_filename).read_bytes()))
    elif kind == "bank":
        date_from, date_to = quarter_range(year, quarter)
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Fecha", "Concepto", "Importe", "Saldo"])
        for row in database.scalars(select(BankTransaction).where(BankTransaction.booking_date.between(date_from, date_to)).order_by(BankTransaction.booking_date)).all():
            writer.writerow([row.booking_date.strftime("%d/%m/%Y"), row.description, f"{row.amount:.2f}".replace(".", ","), f"{row.balance:.2f}".replace(".", ",") if row.balance is not None else ""])
        files.append((f"movimientos_bancarios_{suffix}.csv", ("﻿" + output.getvalue()).encode("utf-8")))
    elif kind == "models":
        from app.advisor_service import models_for
        from app.advisor_service import models_xlsx

        company = database.scalar(select(CompanyProfile).limit(1))
        files.append((f"borradores_modelos_{suffix}.xlsx", models_xlsx(models_for(company, database, year, quarter or 4))))
    elif kind == "payroll":
        from app.payroll_service import build_payslips_pdf

        company = database.scalar(select(CompanyProfile).limit(1))
        for run in database.scalars(select(PayrollRun).where(PayrollRun.id.in_(artifact.get("run_ids", [])))).all():
            files.append((f"nominas_{run.year}_{run.month:02d}.pdf", build_payslips_pdf(run, company)))
    elif kind == "employee_docs":
        for document in database.scalars(select(EmployeeDocument).where(EmployeeDocument.id.in_(artifact.get("ids", [])))).all():
            path = settings.upload_dir / document.stored_filename
            if path.exists():
                files.append((f"contratos/{document.original_filename}", path.read_bytes()))
    elif kind == "credits":
        pending = (case.facts or {}).get("embargo_pending") or []
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Factura", "Fecha", "Importe pendiente"])
        for row in pending:
            writer.writerow([row["number"], row["date"], f"{row['total']:.2f}".replace(".", ",")])
        files.append(("relacion_creditos_pendientes.csv", ("﻿" + output.getvalue()).encode("utf-8")))
    return files


def build_package(database: Session, case: Case) -> bytes:
    buffer = io.BytesIO()
    lines = [f"EXPEDIENTE {case.code} · {case.title}", f"Referencia: {case.reference or '—'}", ""]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if case.draft_response:
            archive.writestr("01_escrito.pdf", build_letter_pdf(database, case))
            archive.writestr("01_escrito.txt", case.draft_response)
        lines.append("DOCUMENTACIÓN")
        for index, item in enumerate(case.required_documents or [], start=1):
            status = DOC_STATUS_LABELS.get(item.get("status"), item.get("status"))
            lines.append(f"  {index}. {item['label']} — {status}")
            if item.get("status") in {"ready", "partial"}:
                for name, content in artifact_files(database, item, case):
                    archive.writestr(f"02_documentacion/{index:02d}_{name}", content)
        for attachment in case.attachments:
            path = attachment_path(attachment)
            if path.exists():
                archive.write(path, f"03_aportados/{attachment.id}_{attachment.filename}")
        missing = [item["label"] for item in (case.required_documents or []) if item.get("status") in {"missing", "requested", "partial"}]
        lines += ["", "PENDIENTE", *([f"  - {label}" for label in missing] or ["  Nada."])]
        lines += ["", "Presenta el escrito y la documentación en la sede electrónica y registra el justificante en el expediente."]
        archive.writestr("LEEME.txt", "\r\n".join(lines))
    return buffer.getvalue()
