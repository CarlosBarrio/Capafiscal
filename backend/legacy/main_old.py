from datetime import datetime
from pathlib import Path
import shutil
import json

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.storage import (
    DATA_DIR,
    UPLOAD_DIR,
    add_activity,
    add_audit_event,
    calculate_sha256,
    find_document_by_hash,
    load_activity,
    load_documents,
    next_document_id,
    reset_documents,
    safe_upload_name,
    save_documents,
)

from app.extractor import (
    extract,
    compute_confidence,
    eur,
    is_aeat,
    is_laboral,
    detect_laboral_subtype,
    detect_aeat_type,
)

from app.features import (
    find_duplicate,
    compute_deadline,
    build_tax_summary,
    build_narrative,
    build_actions,
    needs_action,
    task_label,
    build_excel,
    build_monthly_impact,
    fetch_mailbox_files,
    build_notifications,
    build_aeat_summary,
    build_payroll,
    detect_requested_docs,
    build_aeat_draft,
    answer_assistant,
)

from app.mail_connector import sync_demo_emails



app = FastAPI(title="CapaFiscal")

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)


PRIORITY_WEIGHT = {
    "Alta": 0,
    "Media": 1,
    "Baja": 2,
}

ALLOWED_EXTENSIONS = {".pdf", ".txt"}
MAX_UPLOAD_SIZE = 15 * 1024 * 1024


def now_hm():
    return datetime.now().strftime("%H:%M")


def parse_amount(a):
    if not a or a == "-":
        return 0.0

    c = (
        str(a)
        .replace("€", "")
        .replace(".", "")
        .replace(",", ".")
        .strip()
    )

    try:
        return float(c)
    except ValueError:
        return 0.0


def format_amount(v):
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def build_timeline(dt, ex):
    m = now_hm()

    steps = [
        {
            "time": m,
            "label": "Documento recibido",
        }
    ]

    if ex.get("needs_ocr"):
        steps += [
            {
                "time": m,
                "label": "PDF escaneado detectado",
            },
            {
                "time": m,
                "label": "Enviado a OCR (pendiente)",
            },
        ]
        return steps

    if dt == "Notificación AEAT":
        steps += [
            {
                "time": m,
                "label": "Notificación oficial detectada",
            },
            {
                "time": m,
                "label": "Tipo y plazo calculados",
            },
            {
                "time": m,
                "label": "Pendiente de revisión prioritaria",
            },
        ]
        return steps

    if dt == "Documento laboral":
        steps += [
            {
                "time": m,
                "label": "Documento laboral detectado",
            },
            {
                "time": m,
                "label": "Empleado identificado",
            },
            {
                "time": m,
                "label": "Archivado en ficha del empleado",
            },
        ]
        return steps

    steps.append(
        {
            "time": m,
            "label": "Texto extraído del documento",
        }
    )

    if ex.get("supplier"):
        steps.append(
            {
                "time": m,
                "label": f"Proveedor identificado: {ex['supplier']}",
            }
        )

    if ex.get("amount") is not None:
        steps.append(
            {
                "time": m,
                "label": "Base, IVA y total extraídos",
            }
        )

    return steps


def classify_document(path, filename):
    data = extract(path)

    conf = compute_confidence(data)

    base = data.get("base")
    tax = data.get("tax")
    amount = data.get("amount")

    base_str = eur(base) if base is not None else "-"
    tax_str = eur(tax) if tax is not None else "-"
    amount_str = eur(amount) if amount is not None else "-"

    text = data.get("raw_text", "")

    if data.get("needs_ocr"):
        return {
            "document_type": "Documento escaneado",
            "supplier": "Pendiente de OCR",
            "concept": "Requiere reconocimiento óptico",
            "classification": "Sin procesar",
            "cif": None,
            "date": None,
            "invoice_number": None,
            "employee": None,
            "base": "-",
            "tax": "-",
            "amount": "-",
            "status": "Pendiente OCR",
            "confidence": conf,
            "priority": "Media",
            "risk": "Medio",
            "recommended_action": "Enviar a Azure Document Intelligence.",
            "task": "Procesar documento escaneado",
            "reason": "El PDF no contiene texto seleccionable.",
        }

    if is_aeat(text, filename):
        aeat_type, days, urgency_base = detect_aeat_type(text, filename)

        return {
            "document_type": "Notificación AEAT",
            "supplier": "Agencia Tributaria",
            "concept": aeat_type,
            "aeat_type": aeat_type,
            "classification": "Notificación oficial",
            "cif": data.get("cif"),
            "date": data.get("date"),
            "invoice_number": data.get("invoice_number"),
            "employee": None,
            "base": "-",
            "tax": "-",
            "amount": "-",
            "status": "Urgente",
            "confidence": max(conf, 85),
            "priority": "Alta",
            "risk": "Alto",
            "recommended_action": "Responder antes del fin del plazo legal.",
            "task": f"Atender {aeat_type.lower()}",
            "reason": f"{aeat_type} con plazo de respuesta.",
            "summary": build_aeat_summary(text, aeat_type),
            "requested_docs": detect_requested_docs(text),
            "_aeat_days": days,
            "_aeat_urg": urgency_base,
        }

    if is_laboral(text, filename):
        employee = data.get("employee")
        subtype = detect_laboral_subtype(text, filename)
        is_payroll = subtype == "Nómina del mes"

        return {
            "document_type": "Documento laboral",
            "laboral_subtype": subtype,
            "supplier": f"{subtype} · {employee}" if employee else subtype,
            "concept": subtype,
            "classification": "Documento laboral",
            "cif": data.get("cif"),
            "date": data.get("date"),
            "invoice_number": None,
            "employee": employee,
            "base": base_str,
            "tax": tax_str,
            "amount": amount_str if is_payroll else "-",
            "status": "Pendiente de firma" if is_payroll else "Pendiente de archivo",
            "confidence": max(conf, 70),
            "priority": "Media" if is_payroll else "Baja",
            "risk": "Medio" if is_payroll else "Bajo",
            "recommended_action": "Validar importes antes de la firma." if is_payroll else f"Archivar {subtype.lower()} en la ficha.",
            "task": f"Validar nómina de {employee}" if is_payroll else f"Archivar {subtype.lower()}",
            "reason": f"{subtype} detectado.",
        }

    if data.get("supplier"):
        risk = "Bajo"
        status = "Clasificada automáticamente"
        action = "Ninguna. Clasificada automáticamente."
        priority = "Baja"
        amounts_ok = True

        if base is not None and tax is not None and amount is not None:
            amounts_ok = abs((base + tax) - amount) < 0.02

        if conf < 80 or not amounts_ok:
            risk = "Medio"
            status = "Requiere revisión"
            priority = "Media"

            if not amounts_ok:
                action = "Validar importes: base + IVA no cuadra con el total."
            else:
                action = "Validar cuenta contable propuesta."

        reason = f"Factura de {data['supplier']} reconocida. "
        reason += "Cuadre correcto." if amounts_ok else "Los importes no cuadran."

        return {
            "document_type": "Factura proveedor",
            "supplier": data["supplier"],
            "concept": data.get("concept") or "Factura",
            "classification": data.get("classification") or "Sin clasificar",
            "cif": data.get("cif"),
            "date": data.get("date"),
            "invoice_number": data.get("invoice_number"),
            "employee": None,
            "base": base_str,
            "tax": tax_str,
            "amount": amount_str,
            "status": status,
            "confidence": conf,
            "priority": priority,
            "risk": risk,
            "recommended_action": action,
            "task": f"Validar factura {data['supplier']}",
            "reason": reason,
        }

    return {
        "document_type": "Documento no clasificado",
        "supplier": "Desconocido",
        "concept": "Pendiente de análisis",
        "classification": "Sin clasificar",
        "cif": data.get("cif"),
        "date": data.get("date"),
        "invoice_number": data.get("invoice_number"),
        "employee": None,
        "base": base_str,
        "tax": tax_str,
        "amount": amount_str,
        "status": "Pendiente revisión",
        "confidence": conf,
        "priority": "Media",
        "risk": "Medio",
        "recommended_action": "Revisión manual.",
        "task": "Clasificar documento manualmente",
        "reason": "Proveedor no reconocido.",
    }


def is_pending(d):
    status = d.get("status", "").lower()

    if any(
        k in status
        for k in (
            "aprobada",
            "aprobado",
            "incidencia",
            "solicitada",
            "duplicado confirmado",
            "respuesta",
            "respondida",
            "firmada",
            "archivado",
        )
    ):
        return False

    return any(
        k in status
        for k in (
            "revisión",
            "revision",
            "urgente",
            "pendiente",
            "firma",
            "ocr",
            "duplicado",
            "archivo",
        )
    )


def is_approved(d):
    status = d.get("status", "").lower()
    return "aprobada" in status or "aprobado" in status


def is_resolved(d):

    s = d.get("status", "").lower()

    return any(
        k in s
        for k in (
            "aprobada",
            "aprobado",
            "solicitada",
            "duplicado confirmado",
            "respuesta",
            "respondida",
            "firmada",
            "archivado"
        )
    )


def is_risk(d):
    if is_resolved(d):
        return False

    return d.get("risk") in ("Alto", "Medio")


def enrich(d):
    d["narrative"] = build_narrative(d)
    d["actions"] = build_actions(d)
    return d


def process_document(dest, filename, source="subida manual"):
    documents = load_documents()

    base = classify_document(dest, filename)

    doc = {
        "id": next_document_id(documents),
        "filename": filename,
        "source": source,
    }

    doc.update(base)

    doc["timeline"] = build_timeline(
        base["document_type"],
        extract(dest),
    )

    if doc["document_type"] == "Notificación AEAT":
        deadline = compute_deadline(
            "Notificación AEAT",
            doc.pop("_aeat_days", 10),
            doc.pop("_aeat_urg", None),
        )

        if deadline:
            doc["deadline"] = deadline
            doc["timeline"].append(
                {
                    "time": now_hm(),
                    "label": f"Plazo estimado: {deadline['deadline_date']} ({deadline['days_left']} días)",
                }
            )

    duplicate = find_duplicate(doc, documents)

    if duplicate:
        doc.update(
            {
                "is_duplicate": True,
                "duplicate_of": duplicate["id"],
                "status": "Posible duplicado",
                "priority": "Alta",
                "risk": "Alto",
                "recommended_action": f"Revisar: parece duplicado de #{duplicate['id']}. No pagar dos veces.",
                "reason": f"Posible duplicado de #{duplicate['id']}.",
                "task": f"Revisar posible duplicado de {doc['supplier']}",
            }
        )

        doc["timeline"].append(
            {
                "time": now_hm(),
                "label": f"Posible duplicado de #{duplicate['id']}",
            }
        )
    else:
        doc["is_duplicate"] = False

    documents.append(doc)
    save_documents(documents)

    time = now_hm()
    via = " (por correo)" if source == "correo" else ""

    add_activity(
        time,
        f"{doc['supplier']} · documento recibido{via}",
    )

    if doc.get("is_duplicate"):
        add_activity(
            time,
            f"{doc['supplier']} · POSIBLE DUPLICADO detectado",
        )
    else:
        add_activity(
            time,
            f"{doc['supplier']} · clasificado ({doc['classification']}) · {doc['confidence']}%",
        )

    return doc

def load_connectors_file():
    connectors_file = DATA_DIR / "connectors.json"

    if connectors_file.exists():
        try:
            with connectors_file.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass

    documents = load_documents()

    return [
        {
            "provider": "Outlook",
            "status": "Simulado",
            "last_sync": None,
            "documents_found": len(
                [
                    document
                    for document in documents
                    if document.get("source") == "correo"
                ]
            ),
            "is_demo": True,
        },
        {
            "provider": "Gmail",
            "status": "No disponible",
            "last_sync": None,
            "documents_found": 0,
            "is_demo": False,
        },
        {
            "provider": "Carga manual",
            "status": "Activo",
            "last_sync": None,
            "documents_found": len(
                [
                    document
                    for document in documents
                    if document.get("source") == "subida manual"
                ]
            ),
            "is_demo": False,
        },
    ]


@app.get("/")
def home():
    return FileResponse("app/static/index.html")


@app.get("/api/documents")
def get_documents():
    documents = load_documents()

    documents.sort(
        key=lambda x: PRIORITY_WEIGHT.get(
            x.get("priority"),
            1,
        )
    )

    return [enrich(x) for x in documents]


@app.get("/api/documents/{document_id}")
def get_document(document_id: int):
    for doc in load_documents():
        if doc["id"] == document_id:
            return enrich(doc)

    return {
        "error": "Documento no encontrado",
    }


@app.post("/api/upload")
async def upload_document(file: UploadFile = File(...)):
    filename = safe_upload_name(file.filename or "documento")
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        return {
            "success": False,
            "message": "Formato no permitido. Solo se admiten PDF y TXT.",
        }

    content = await file.read()

    if not content:
        return {
            "success": False,
            "message": "El archivo está vacío.",
        }

    if len(content) > MAX_UPLOAD_SIZE:
        return {
            "success": False,
            "message": "El archivo supera el límite de 15 MB.",
        }

    file_hash = calculate_sha256(content)
    existing = find_document_by_hash(file_hash)

    if existing:
        add_audit_event(
            action="document.duplicate_upload_blocked",
            entity_type="document",
            entity_id=existing["id"],
            actor="user",
            metadata={
                "filename": filename,
                "file_hash": file_hash,
            },
        )

        return {
            "success": False,
            "duplicate": True,
            "message": "Este archivo ya fue subido anteriormente.",
            "document": enrich(existing),
        }

    stored_name = f"{file_hash[:16]}_{filename}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(content)

    try:
        document = process_document(
            destination,
            filename,
            "subida manual",
        )

        documents = load_documents()

        for current in documents:
            if current.get("id") == document["id"]:
                current["file_hash"] = file_hash
                current["stored_filename"] = stored_name
                current["file_size"] = len(content)
                current["content_type"] = file.content_type
                current["created_at"] = datetime.now().isoformat()
                document = current
                break

        save_documents(documents)

        add_audit_event(
            action="document.created",
            entity_type="document",
            entity_id=document["id"],
            actor="user",
            metadata={
                "filename": filename,
                "stored_filename": stored_name,
                "file_hash": file_hash,
                "file_size": len(content),
            },
        )

        add_audit_event(
            action="document.classified",
            entity_type="document",
            entity_id=document["id"],
            actor="agent",
            metadata={
                "document_type": document.get("document_type"),
                "confidence": document.get("confidence"),
                "risk": document.get("risk"),
            },
        )

        return {
            "success": True,
            "document": enrich(document),
        }

    except Exception:
        destination.unlink(missing_ok=True)
        raise


@app.post("/api/sync/email")
def sync_email():
    result = sync_demo_emails()

    if result["count"] > 0:
        message = (
            f"Sincronización simulada completada: "
            f"{result['count']} factura(s) nueva(s) importada(s)."
        )
    else:
        message = (
            "Sincronización simulada completada. "
            "No se encontraron facturas nuevas."
        )

    return {
        "success": True,
        "mode": result["mode"],
        "imported": result["count"],
        "skipped": result["skipped"],
        "message": message,
    }


@app.get("/api/documents/{document_id}/draft")
def download_draft(document_id: int):
    for doc in load_documents():
        if doc["id"] == document_id and doc.get("document_type") == "Notificación AEAT":
            return PlainTextResponse(
                build_aeat_draft(doc),
                headers={
                    "Content-Disposition": f'attachment; filename="borrador_respuesta_{document_id}.txt"'
                },
            )

    return {
        "error": "No es una notificación AEAT",
    }


@app.post("/api/assistant/query")
def assistant_query(payload: dict):
    question = (payload or {}).get("question", "")

    return answer_assistant(
        question,
        load_documents(),
        load_activity(),
    )



ACTION_RESULT = {
    "approve": {
        "status": "Aprobada por usuario",
        "priority": "Baja",
        "risk": "Bajo",
        "recommended_action": "Ninguna. Documento aprobado.",
        "reason": "Documento aprobado manualmente.",
        "timeline": "Aprobado por usuario",
        "activity": "aprobado por usuario",
    },
    "request_invoice": {
        "status": "Nueva factura solicitada",
        "priority": "Media",
        "risk": "Medio",
        "recommended_action": "A la espera de factura corregida.",
        "reason": "Se ha solicitado nueva factura por el descuadre.",
        "timeline": "Nueva factura solicitada al proveedor",
        "activity": "nueva factura solicitada al proveedor",
    },
    "flag_issue":{
        "status":"Incidencia abierta",
        "priority":"Alta",
        "risk":"Alto",
        "recommended_action":"Resolver la incidencia antes de contabilizar.",
        "reason":"Factura bloqueada hasta revisión manual.",
        "timeline":"Incidencia abierta por el usuario",
        "activity":"incidencia abierta"
    },
    "mark_duplicate": {
        "status": "Duplicado confirmado",
        "priority": "Baja",
        "risk": "Bajo",
        "recommended_action": "Archivado como duplicado.",
        "reason": "El usuario ha confirmado el duplicado.",
        "timeline": "Duplicado confirmado",
        "activity": "duplicado confirmado",
    },
    "prepare_response": {
        "status": "Respuesta en preparación",
        "priority": "Alta",
        "risk": "Alto",
        "recommended_action": "Borrador generado. Descárgalo y valídalo.",
        "reason": "Se ha generado un borrador de respuesta.",
        "timeline": "Borrador de respuesta generado",
        "activity": "borrador de respuesta generado",
    },
    "mark_responded": {
        "status": "Respondida",
        "priority": "Baja",
        "risk": "Bajo",
        "recommended_action": "Trámite cerrado.",
        "reason": "Marcada como respondida.",
        "timeline": "Notificación respondida",
        "activity": "notificación respondida",
    },
    "sign_payroll": {
        "status": "Nómina firmada",
        "priority": "Baja",
        "risk": "Bajo",
        "recommended_action": "Lista para enviar al empleado.",
        "reason": "Nómina firmada.",
        "timeline": "Nómina firmada",
        "activity": "nómina firmada",
    },
    "archive_doc": {
        "status": "Archivado en ficha",
        "priority": "Baja",
        "risk": "Bajo",
        "recommended_action": "Archivado en la ficha del empleado.",
        "reason": "Documento archivado.",
        "timeline": "Archivado en ficha del empleado",
        "activity": "documento archivado en ficha",
    },
}


@app.post("/api/documents/{document_id}/action")
def apply_action(document_id: int, payload: dict):
    action = (payload or {}).get("action")

    if action not in ACTION_RESULT:
        return {
            "success": False,
            "message": f"Acción no válida: {action}",
        }

    result = ACTION_RESULT[action]

    documents = load_documents()

    for doc in documents:
        if doc["id"] == document_id:
            time = now_hm()

            doc["status"] = result["status"]
            doc["priority"] = result["priority"]
            doc["risk"] = result["risk"]
            doc["recommended_action"] = result["recommended_action"]
            doc["reason"] = result["reason"]

            doc.setdefault("timeline", []).append(
                {
                    "time": time,
                    "label": result["timeline"],
                }
            )

            save_documents(documents)
            add_audit_event(
                action=f"document.action.{action}",
                entity_type="document",
                entity_id=document_id,
                actor="user",
                metadata={
                    "status": doc["status"],
                    "priority": doc["priority"],
                    "risk": doc["risk"],
                },
            )
            add_activity(
                time,
                f"{doc['supplier']} · {result['activity']}",
            )

            return {
                "success": True,
                "document": enrich(doc),
            }

    return {
        "success": False,
        "message": "Documento no encontrado",
    }


@app.post("/api/documents/{document_id}/approve")
def approve_document(document_id: int):
    return apply_action(
        document_id,
        {
            "action": "approve",
        },
    )


@app.post("/api/reset")
def reset():
    reset_documents()

    return {
        "success": True,
        "message": "Bandeja reiniciada",
    }


@app.get("/api/export/excel")
def export_excel():
    out = build_excel(load_documents())

    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="capafiscal_facturas_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx"'
        },
    )


@app.get("/api/dashboard")
def get_dashboard():
    documents = load_documents()

    tax = build_tax_summary(documents)

    hour = datetime.now().hour

    greeting = (
        "Buenos días"
        if hour < 12
        else "Buenas tardes"
        if hour < 20
        else "Buenas noches"
    )

    return {
        "greeting": greeting,
        "user": "Carlos",
        "risks_detected": len(
            [
                doc
                for doc in documents
                if is_risk(doc)
            ]
        ),
        "iva_soportado": tax["total_iva"],
        "gasto_validado": tax["total_amount"],
        "pending_review": len(
            [
                doc
                for doc in documents
                if is_pending(doc)
            ]
        ),
        "reliable_invoices": tax["invoice_count"],
        "pending_invoices": tax["pending_count"],
    }


@app.get("/api/monthly-impact")
def get_monthly_impact():
    return build_monthly_impact(
        load_documents(),
        load_activity(),
    )


@app.get("/api/notifications")
def get_notifications():
    return build_notifications(
        load_documents(),
    )


@app.get("/api/payroll")
def get_payroll():
    return build_payroll(
        load_documents(),
    )


@app.get("/api/tasks")
def get_tasks():
    documents = load_documents()

    tasks = []

    for doc in documents:
        if not needs_action(doc):
            continue

        tasks.append(
            {
                "id": doc["id"],
                "label": task_label(doc),
                "narrative": build_narrative(doc),
                "supplier": doc.get("supplier", "Desconocido"),
                "priority": doc.get("priority", "Media"),
            }
        )

    tasks.sort(
        key=lambda z: PRIORITY_WEIGHT.get(
            z.get("priority"),
            1,
        )
    )

    return tasks


@app.get("/api/activity")
def get_activity():
    return load_activity()


@app.get("/api/tax-summary")
def get_tax_summary():
    return build_tax_summary(
        load_documents(),
    )


@app.get("/api/connectors")
def get_connectors():
    return load_connectors_file()
