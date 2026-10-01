"""
Demo «cero intervención»: ¿hasta dónde llega CapaFiscal sin que nadie toque nada?

Parte únicamente de un evento externo y muestra, con la hora de cada paso, lo
que hace el sistema hasta dejar el trabajo listo para «Revisar y aprobar».

    python scripts/demo_cero_intervencion.py

Usa una base de datos temporal: no toca tus datos.
  1. DEHú → requerimiento → expediente → documentos pedidos → escrito → humano
  2. Correo → factura 6 veces por encima de lo habitual → investigación → humano
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="capafiscal-demo-"))
os.environ.update({
    "DATA_DIR": str(WORK / "data"),
    "UPLOAD_DIR": str(WORK / "uploads"),
    "DATABASE_URL": f"sqlite:///{(WORK / 'demo.db').as_posix()}",
    "ENABLE_SCHEDULER": "false",
})
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.database import create_database_tables  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentRun  # noqa: E402
from app.models import AgentStep  # noqa: E402
from app.models import Document  # noqa: E402
from app.models import Invoice  # noqa: E402

AGENT_NAMES = {
    "vigilante": "Vigilante", "expedientes": "Expedientes", "fiscal": "Fiscal", "memoria": "Memoria", "detector": "Detector",
    "gestor": "Gestor", "perseguidor": "Perseguidor", "director": "Director",
}


def timeline(client: TestClient, run_id: int, started: datetime, label: str) -> None:
    with SessionLocal() as database:
        run = database.get(AgentRun, run_id)
        steps = database.query(AgentStep).filter(AgentStep.run_id == run_id).order_by(AgentStep.position).all()
        print(f"\n{'─' * 78}\n{label}\n{'─' * 78}")
        print(f"{started:%H:%M:%S.%f}"[:-3] + "  entra el evento externo")
        for step in steps:
            moment = step.created_at.replace(tzinfo=None)
            print(f"{moment:%H:%M:%S.%f}"[:-3] + f"  {AGENT_NAMES.get(step.agent, step.agent):<12} {step.summary[:120]}")
        case = client.get(f"/api/cases/{run.case_id}").json() if run.case_id else None
        finished = (run.finished_at or steps[-1].created_at).replace(tzinfo=None)
        print(f"{finished:%H:%M:%S.%f}"[:-3] + f"  Ruta «{case['route']['label']}»" + (f" · {', '.join(item['reason'] for item in case['route']['chained'])}" if case and case["route"]["chained"] else "") if case else "  sin expediente")
        if case:
            print(f"{finished:%H:%M:%S.%f}"[:-3] + f"  {case['status_label'].upper()} · {case['code']} · {case['headline']}")
            print(f"\n  Total sin intervención: {(finished - started).total_seconds() * 1000:.0f} ms")
            print(f"  Lo que queda para la persona: «{case['facts'].get('recommendation') or case['actions'][0]['label']}»")
            done = [item for item in case["documents"] if item["status"] == "ready"]
            if case["documents"]:
                print(f"  Documentos: {len(done)} preparados por el sistema, {len(case['documents']) - len(done)} pedidos o por aportar")
            if case.get("has_draft"):
                print("  Borrador de respuesta: listo para revisar")
            outbox = [item for item in client.get("/api/outbox").json()["messages"] if item["status"] == "DRAFT"]
            if outbox:
                print(f"  Mensajes preparados (sin enviar hasta tu visto bueno): {len(outbox)}")


def main() -> None:
    create_database_tables()
    with TestClient(app) as client:
        client.put("/api/company", json={"name": "Estudio Ejemplo Arquitectos S.L.P.", "tax_id": "B49123458", "legal_form": "SOCIEDAD", "email": "admin@estudio-ejemplo.test", "city": "Burgos", "address": "C/ Ficticia 1"})

        # 1) DEHú → requerimiento
        started = datetime.utcnow()
        body = client.post("/api/events", json={
            "kind": "notification", "source": "dehu", "external_id": "DEHU-DEMO-0001",
            "notification": {
                "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento de información", "reference": "2026RQ000123",
                "summary": "Procedimiento de comprobación limitada del modelo 303, ejercicio 2026, periodo 3T. En el plazo de 10 días hábiles aporte: "
                           "a) Libro registro de facturas recibidas. b) Justificante de pago de la factura 2026/0145. c) Contratos de arrendamiento vigentes.",
                "notified_at": date.today().isoformat(),
            },
        }).json()
        timeline(client, body["run"]["id"], started, "1 · DEHú → requerimiento → expediente → acción")

        # 2) Correo → factura → anomalía
        with SessionLocal() as database:
            for index, total in enumerate([400, 410, 395, 405, 400]):
                document = Document(original_filename=f"h{index}.pdf", stored_filename=f"h{index}.pdf", sha256=f"{index:064d}", extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
                database.add(document)
                database.flush()
                gross = Decimal(total)
                database.add(Invoice(
                    document_id=document.id, supplier_name="INGENIERIA DEMO S.L.", supplier_tax_id="B46444444", direction="RECEIVED",
                    invoice_number=f"H-{index}", invoice_date=date(2026, 4, 1) + timedelta(days=30 * index), total=gross,
                    subtotal=(gross / Decimal("1.21")).quantize(Decimal("0.01")), tax_total=gross - (gross / Decimal("1.21")).quantize(Decimal("0.01")),
                    currency="EUR", confidence=95, field_confidences={}, validation_status="VALID", validation_messages=[], review_status="APPROVED", duplicate_status="NONE",
                ))
            database.commit()
        pdf = BACKEND / "evaluation" / "datasets" / "sinteticas" / "ingenieria_pie_legal.pdf"
        message = EmailMessage()
        message["From"], message["To"], message["Subject"], message["Message-ID"] = "facturacion@ingenieria-demo.test", "admin@estudio-ejemplo.test", "Factura 260612", "<demo-260612@test>"
        message.set_content("Adjuntamos la factura.")
        message.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
        started = datetime.utcnow()
        result = client.post("/api/connectors/email/import", files={"uploaded_file": ("factura.eml", message.as_bytes(), "message/rfc822")}).json()
        attachment = result["attachments"][0]
        print(f"\n{'─' * 78}\n2 · Correo → factura → anomalía → investigación → revisión\n{'─' * 78}")
        print(f"  Correo «{result['subject']}» de {result['from']}: {len(result['attachments'])} adjunto(s) leído(s) como factura")
        timeline(client, attachment["event"]["run_id"], started, "  Recorrido de la factura")

        board = client.get("/api/briefing").json()["board"]
        print(f"\n{'─' * 78}\nEl Director, después de los dos eventos\n{'─' * 78}")
        print(f"  🔴 {board['attention']['count']} requieren atención · 🟠 {board['pending']['count']} pendientes · 🟢 {board['resolved']['count']} resueltas sin intervención")
        for index, item in enumerate(board["top"], start=1):
            print(f"  {index}. {item['title']} — {item['reason']}")
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
