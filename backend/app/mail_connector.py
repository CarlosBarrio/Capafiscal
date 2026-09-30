import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from app.storage import (
    DATA_DIR,
    add_activity,
    add_audit_event,
    load_documents,
    next_document_id,
    save_documents,
)


CONNECTORS_FILE = DATA_DIR / "connectors.json"


DEMO_INVOICES = [
    {
        "external_id": "outlook-demo-repsol-2026-07",
        "filename": "factura_repsol_julio_2026.pdf",
        "supplier": "Repsol",
        "cif": "A28047223",
        "invoice_number": "FRA-2026-00912",
        "date": "15/07/2026",
        "classification": "Combustible",
        "concept": "Carburante",
        "base": 396.28,
        "tax": 83.22,
        "amount": 479.50,
        "confidence": 96,
        "risk": "Bajo",
        "priority": "Baja",
        "status": "Clasificada automáticamente",
    },
    {
        "external_id": "outlook-demo-endesa-2026-07",
        "filename": "factura_endesa_julio_2026.pdf",
        "supplier": "Endesa",
        "cif": "A81948077",
        "invoice_number": "E26-4471183",
        "date": "10/07/2026",
        "classification": "Suministros",
        "concept": "Suministro eléctrico",
        "base": 258.60,
        "tax": 54.31,
        "amount": 312.91,
        "confidence": 94,
        "risk": "Bajo",
        "priority": "Baja",
        "status": "Clasificada automáticamente",
    },
    {
        "external_id": "outlook-demo-iberdrola-review-2026-07",
        "filename": "factura_iberdrola_revisar_2026.pdf",
        "supplier": "Iberdrola",
        "cif": "A95758389",
        "invoice_number": "IB-2026-99001",
        "date": "20/07/2026",
        "classification": "Suministros",
        "concept": "Suministro eléctrico",
        "base": 100.00,
        "tax": 21.00,
        "amount": 200.00,
        "confidence": 91,
        "risk": "Medio",
        "priority": "Media",
        "status": "Requiere revisión",
    },
]


def format_eur(value: float) -> str:
    return (
        f"{value:,.2f} €"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def load_connectors() -> list[dict]:
    if not CONNECTORS_FILE.exists():
        return []

    try:
        with CONNECTORS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_connectors(connectors: list[dict]) -> None:
    """
    Escritura atómica para no dejar connectors.json incompleto.
    """
    CONNECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix="connectors_",
        suffix=".tmp",
        dir=str(CONNECTORS_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            json.dump(
                connectors,
                temporary_file,
                ensure_ascii=False,
                indent=2,
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_name, CONNECTORS_FILE)

    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass

        raise


def get_source_key(invoice: dict) -> str:
    return f"outlook-demo:{invoice['external_id']}"


def get_source_hash(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()


def find_existing_document(
    documents: list[dict],
    source_key: str,
    filename: str,
) -> dict | None:
    """
    Primero busca mediante la clave estable del conector.

    Para datos antiguos que todavía no tengan source_key, utiliza
    el nombre de archivo como mecanismo de compatibilidad.
    """
    for document in documents:
        if document.get("source_key") == source_key:
            return document

    for document in documents:
        if (
            document.get("source") == "correo"
            and document.get("filename") == filename
        ):
            return document

    return None


def build_demo_document(
    invoice: dict,
    document_id: int,
) -> dict:
    source_key = get_source_key(invoice)
    source_hash = get_source_hash(source_key)
    current_time = datetime.now().strftime("%H:%M")
    created_at = datetime.now().isoformat()

    requires_review = invoice["risk"] != "Bajo"

    if requires_review:
        recommended_action = (
            "Revisar los importes antes de contabilizar. "
            "La base más el IVA no coincide con el total."
        )
        reason = (
            "Factura importada desde el correo con un descuadre "
            "entre base, IVA y total."
        )
    else:
        recommended_action = (
            "Factura importada y clasificada automáticamente. "
            "Puede aprobarse para contabilización."
        )
        reason = (
            "Proveedor reconocido e importes correctamente cuadrados."
        )

    return {
        "id": document_id,
        "filename": invoice["filename"],
        "stored_filename": None,
        "source": "correo",
        "source_provider": "Outlook",
        "source_key": source_key,
        "source_hash": source_hash,
        "external_id": invoice["external_id"],
        "document_type": "Factura proveedor",
        "supplier": invoice["supplier"],
        "cif": invoice["cif"],
        "invoice_number": invoice["invoice_number"],
        "date": invoice["date"],
        "classification": invoice["classification"],
        "concept": invoice["concept"],
        "base": format_eur(invoice["base"]),
        "tax": format_eur(invoice["tax"]),
        "amount": format_eur(invoice["amount"]),
        "confidence": invoice["confidence"],
        "risk": invoice["risk"],
        "priority": invoice["priority"],
        "status": invoice["status"],
        "recommended_action": recommended_action,
        "reason": reason,
        "is_duplicate": False,
        "duplicate_of": None,
        "created_at": created_at,
        "timeline": [
            {
                "time": current_time,
                "label": "Correo recibido desde Outlook simulado",
            },
            {
                "time": current_time,
                "label": "Adjunto identificado como posible factura",
            },
            {
                "time": current_time,
                "label": (
                    "Factura enviada a revisión"
                    if requires_review
                    else "Factura clasificada automáticamente"
                ),
            },
        ],
    }


def update_outlook_connector(
    documents: list[dict],
    imported_count: int,
) -> None:
    connectors = load_connectors()

    outlook = next(
        (
            connector
            for connector in connectors
            if connector.get("provider") == "Outlook"
        ),
        None,
    )

    if outlook is None:
        outlook = {
            "provider": "Outlook",
            "status": "Simulado",
            "last_sync": None,
            "documents_found": 0,
        }
        connectors.append(outlook)

    email_documents = [
        document
        for document in documents
        if document.get("source") == "correo"
        and document.get("source_provider", "Outlook") == "Outlook"
    ]

    outlook["status"] = "Simulado"
    outlook["last_sync"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    outlook["documents_found"] = len(email_documents)
    outlook["last_imported"] = imported_count
    outlook["is_demo"] = True

    if not any(
        connector.get("provider") == "Gmail"
        for connector in connectors
    ):
        connectors.append(
            {
                "provider": "Gmail",
                "status": "No disponible",
                "last_sync": None,
                "documents_found": 0,
                "is_demo": False,
            }
        )

    manual = next(
        (
            connector
            for connector in connectors
            if connector.get("provider") == "Carga manual"
        ),
        None,
    )

    if manual is None:
        manual = {
            "provider": "Carga manual",
            "status": "Activo",
            "last_sync": None,
            "documents_found": 0,
            "is_demo": False,
        }
        connectors.append(manual)

    manual["documents_found"] = len(
        [
            document
            for document in documents
            if document.get("source") == "subida manual"
        ]
    )

    save_connectors(connectors)


def sync_demo_emails() -> dict:
    """
    Sincronización idempotente.

    Ejecutarla varias veces no vuelve a insertar las mismas facturas.
    """
    documents = load_documents()

    imported_count = 0
    skipped_count = 0
    metadata_updated = False

    for invoice in DEMO_INVOICES:
        source_key = get_source_key(invoice)

        existing = find_existing_document(
            documents,
            source_key,
            invoice["filename"],
        )

        if existing:
            skipped_count += 1

            # Compatibilidad con documentos antiguos.
            if not existing.get("source_key"):
                existing["source_key"] = source_key
                existing["source_hash"] = get_source_hash(source_key)
                existing["source_provider"] = "Outlook"
                existing["external_id"] = invoice["external_id"]
                metadata_updated = True

            continue

        document_id = next_document_id(documents)

        document = build_demo_document(
            invoice,
            document_id,
        )

        documents.append(document)
        imported_count += 1

        add_audit_event(
            action="document.created",
            entity_type="document",
            entity_id=document_id,
            actor="connector.outlook.demo",
            metadata={
                "filename": document["filename"],
                "source_key": document["source_key"],
                "supplier": document["supplier"],
            },
        )

        add_audit_event(
            action="document.classified",
            entity_type="document",
            entity_id=document_id,
            actor="agent",
            metadata={
                "confidence": document["confidence"],
                "risk": document["risk"],
                "status": document["status"],
            },
        )

    if imported_count > 0 or metadata_updated:
        save_documents(documents)

    update_outlook_connector(
        documents,
        imported_count,
    )

    current_time = datetime.now().strftime("%H:%M")

    if imported_count > 0:
        add_activity(
            current_time,
            (
                f"Outlook simulado · {imported_count} factura(s) "
                "nueva(s) importada(s)"
            ),
        )
    else:
        add_activity(
            current_time,
            "Outlook simulado · sincronización completada sin novedades",
        )

    add_audit_event(
        action="connector.sync.completed",
        entity_type="connector",
        entity_id="outlook-demo",
        actor="user",
        metadata={
            "imported": imported_count,
            "skipped": skipped_count,
            "mode": "demo",
        },
    )

    return {
        "ok": True,
        "count": imported_count,
        "skipped": skipped_count,
        "mode": "demo",
    }