"""Sistema de agentes: el recorrido completo de una notificación y el detector."""
from __future__ import annotations

from app import clock
import io
import zipfile
from datetime import timedelta
from decimal import Decimal

from app.agents.fiscal import detect_references
from app.agents.gestor import requested_items
from app.agents.memory import tokenize

TODAY = clock.today()

REQUERIMIENTO = f"""AGENCIA TRIBUTARIA
Delegación Especial de Castilla y León
REQUERIMIENTO
Procedimiento de comprobación limitada
Referencia: 2026CL0099887
Obligado tributario: TALLER BARRIO S.L.   NIF: B12345674
Concepto: Impuesto sobre el Valor Añadido, modelo 303, ejercicio {TODAY.year}, periodo 2T
En relación con la autoliquidación del modelo 303 correspondiente al segundo trimestre, se le requiere
para que en el plazo de 10 días hábiles aporte la siguiente documentación:
a) Libro registro de facturas expedidas del periodo.
b) Libro registro de facturas recibidas del periodo.
c) Justificante de pago de la factura 2026/0145.
d) Contratos de arrendamiento vigentes.
La falta de atención de este requerimiento podrá constituir infracción tributaria (art. 203 LGT).
"""

EMBARGO = """AGENCIA TRIBUTARIA
Dependencia Regional de Recaudación
DILIGENCIA DE EMBARGO DE CRÉDITOS
Referencia: EMB-2026-5566
Destinatario: TALLER BARRIO S.L. NIF: B12345674
Deudor: SUMINISTROS LOPEZ S.L. NIF: B23456783
Se declaran embargados los créditos que el deudor tenga a su favor frente a ustedes.
Deberá retener los importes pendientes de pago al deudor e ingresarlos en el Tesoro.
"""


def setup_company(client):
    response = client.put(
        "/api/company",
        json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es", "city": "Burgos", "address": "C/ Vitoria 42"},
    )
    assert response.status_code == 200, response.text


def upload_text(client, name: str, text: str) -> dict:
    response = client.post("/api/upload", files={"uploaded_file": (name, text.encode("utf-8"), "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def open_case(client) -> dict:
    items = client.get("/api/cases").json()
    assert items, "No se creó ningún expediente"
    return client.get(f"/api/cases/{items[0]['id']}").json()


# ---------------------------------------------------------------------
# Piezas
# ---------------------------------------------------------------------


def test_detect_tax_references():
    refs = detect_references(REQUERIMIENTO)
    assert refs[0]["model"] == "303"
    assert refs[0]["year"] == TODAY.year
    assert refs[0]["quarter"] == 2


def test_requested_items_from_enumeration():
    codes = [item["code"] for item in requested_items(REQUERIMIENTO)]
    assert codes == ["LIBRO_EMITIDAS", "LIBRO_RECIBIDAS", "JUSTIFICANTE_PAGO", "CONTRATOS"]


def test_tokenize_removes_accents_and_stopwords():
    assert tokenize("La Agencia Tributaria requiere el modelo 303") == ["agencia", "tributaria", "requiere", "modelo", "303"]


# ---------------------------------------------------------------------
# Recorrido completo de una notificación
# ---------------------------------------------------------------------


def test_requerimiento_end_to_end(client):
    setup_company(client)
    document = upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    assert document["document"]["kind"] == "NOTIFICATION"

    case = open_case(client)
    assert case["code"].startswith(f"EXP-{TODAY.year}-")
    assert case["procedure"] == "COMPROBACION_LIMITADA"
    assert case["subject"]["type"] == "company"
    assert case["status"] == "WAITING_HUMAN"
    assert case["headline"].startswith("Taller Barrio S.L. — ")

    # La traza: los 7 agentes en orden
    agents = [step["agent"] for step in case["run"]["steps"]]
    assert agents == ["vigilante", "expedientes", "fiscal", "memoria", "gestor", "perseguidor", "director"]
    assert all(step["status"] == "OK" for step in case["run"]["steps"])

    documents = {item["code"]: item for item in case["documents"]}
    assert documents["LIBRO_EMITIDAS"]["status"] == "ready"
    assert documents["LIBRO_RECIBIDAS"]["status"] == "ready"
    assert documents["JUSTIFICANTE_PAGO"]["status"] == "requested"
    assert documents["CONTRATOS"]["status"] == "requested"

    assert "EXPONE" in case["draft_response"] and "SOLICITA" in case["draft_response"]
    assert "B12345674" in case["draft_response"]
    assert case["facts"]["tax_references"][0]["model"] == "303"

    # El perseguidor preparó la petición (pendiente de visto bueno)
    outbox = client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"]
    assert len(outbox) == 1 and outbox[0]["to_email"] == "admin@taller.es"
    assert "/portal/" in outbox[0]["body"]

    # Escrito en PDF y paquete para presentar con los libros generados
    letter = client.get(f"/api/cases/{case['id']}/letter.pdf")
    assert letter.status_code == 200 and letter.content.startswith(b"%PDF")
    package = zipfile.ZipFile(io.BytesIO(client.get(f"/api/cases/{case['id']}/package.zip").content))
    names = package.namelist()
    assert "01_escrito.pdf" in names and "LEEME.txt" in names
    assert any("libro_facturas_expedidas" in name for name in names)

    # Reprocesar no duplica el expediente ni pisa un borrador editado
    client.patch(f"/api/cases/{case['id']}", json={"draft_response": "Mi versión revisada del escrito."})
    again = client.post(f"/api/cases/{case['id']}/rerun").json()
    assert len(client.get("/api/cases").json()) == 1
    assert again["draft_response"] == "Mi versión revisada del escrito."
    assert len(client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"]) == 1


def test_portal_upload_verifies_and_completes(client):
    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    case = open_case(client)
    message = client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"][0]
    client.post(f"/api/outbox/{message['id']}/mark-sent")

    tokens = [line.split("/portal/")[1].strip() for line in message["body"].splitlines() if "/portal/" in line]
    assert len(tokens) == 2

    info = client.get(f"/api/portal/{tokens[0]}").json()
    assert info["company"] == "Taller Barrio S.L."

    page = client.get(f"/portal/{tokens[0]}")
    assert page.status_code == 200 and "<html" in page.text.lower()

    result = client.post(
        f"/api/portal/{tokens[0]}",
        files={"uploaded_file": ("justificante.txt", b"Justificante de pago de la factura 2026/0145 - Ref 2026CL0099887", "text/plain")},
    ).json()
    assert result["verification"]["status"] in {"ok", "doubtful"}

    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["status"] == "WAITING_DOCS"
    client.post(f"/api/portal/{tokens[1]}", files={"uploaded_file": ("contrato.txt", b"Contrato de arrendamiento del local", "text/plain")})
    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["status"] == "WAITING_HUMAN"
    assert any("Documentación completa" in item["title"] for item in detail["events"])
    assert {item["status"] for item in detail["documents"] if item["code"] in {"JUSTIFICANTE_PAGO", "CONTRATOS"}} == {"received"}

    # Aprobar → presentar → resolver, y la notificación se cierra
    assert client.post(f"/api/cases/{case['id']}/approve").json()["status"] == "READY_TO_FILE"
    filed = client.post(f"/api/cases/{case['id']}/file", json={"reference": "RGE-123456"}).json()
    assert filed["status"] == "FILED"
    resolved = client.post(f"/api/cases/{case['id']}/resolve", json={"resolution": "Hacienda archivó sin regularizar."}).json()
    assert resolved["status"] == "RESOLVED"
    notification = client.get("/api/notifications").json()
    items = notification if isinstance(notification, list) else notification.get("items", notification.get("notifications", []))
    assert items[0]["status"] == "CLOSED"


def test_follow_up_prepares_reminders(client):
    from app.agents.perseguidor import follow_up
    from app.database import SessionLocal

    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    message = client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"][0]
    client.post(f"/api/outbox/{message['id']}/mark-sent")

    with SessionLocal() as database:
        assert follow_up(database, today=TODAY)["reminders"] == 0  # aún no toca
        result = follow_up(database, today=TODAY + timedelta(days=7))
        database.commit()
    assert result["reminders"] == 1
    reminders = [item for item in client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"] if item["level"] == 1]
    assert reminders and reminders[0]["subject"].startswith("Recordatorio")


def test_embargo_identifies_supplier_and_pending_payments(client):
    from app.database import SessionLocal
    from app.models import Document
    from app.models import Invoice

    setup_company(client)
    with SessionLocal() as database:
        document = Document(original_filename="factura_lopez.pdf", stored_filename="x.pdf", sha256="a" * 64, extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
        database.add(document)
        database.flush()
        database.add(Invoice(
            document_id=document.id, supplier_name="SUMINISTROS LOPEZ S.L.", supplier_tax_id="B23456783", direction="RECEIVED",
            invoice_number="SL-77", invoice_date=TODAY - timedelta(days=10), total=Decimal("1234.56"), subtotal=Decimal("1020.30"),
            tax_total=Decimal("214.26"), currency="EUR", confidence=95, field_confidences={}, validation_status="VALID",
            validation_messages=[], review_status="APPROVED", duplicate_status="NONE",
        ))
        database.commit()

    upload_text(client, "embargo.txt", EMBARGO)
    case = open_case(client)
    assert case["procedure"] == "EMBARGO_CREDITOS"
    assert case["facts"]["affected"]["name"] == "SUMINISTROS LOPEZ S.L."
    assert case["facts"]["embargo_pending"][0]["total"] == 1234.56
    assert any("NO se lo pagues" in text and "no se ha podido leer" in text for text in case["facts"]["insights"])  # sin importe de deuda: se retiene todo
    assert "1.234,56 €" in case["draft_response"]
    assert case["level"] in {"critical", "high"}


# ---------------------------------------------------------------------
# Detector de anomalías, director y memoria
# ---------------------------------------------------------------------


def seed_invoices(totals, supplier="ENDESA ENERGIA S.A.", tax_id="A81948077", start=None):
    from app.database import SessionLocal
    from app.models import Document
    from app.models import Invoice

    start = start or TODAY - timedelta(days=30 * len(totals))
    with SessionLocal() as database:
        for index, total in enumerate(totals):
            document = Document(original_filename=f"f{index}.pdf", stored_filename=f"f{index}.pdf", sha256=f"{index:064d}", extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
            database.add(document)
            database.flush()
            subtotal = Decimal(str(total)) / Decimal("1.21")
            database.add(Invoice(
                document_id=document.id, supplier_name=supplier, supplier_tax_id=tax_id, direction="RECEIVED",
                invoice_number=f"E-{index}", invoice_date=start + timedelta(days=30 * index), total=Decimal(str(total)),
                subtotal=subtotal.quantize(Decimal("0.01")), tax_total=(Decimal(str(total)) - subtotal).quantize(Decimal("0.01")),
                currency="EUR", confidence=95, field_confidences={}, validation_status="VALID", validation_messages=[],
                review_status="APPROVED", duplicate_status="NONE",
            ))
        database.commit()


def test_detector_flags_atypical_amount_and_closes_itself(client):
    seed_invoices([110, 95, 120, 105, 100, 980])
    result = client.post("/api/agents/anomalies/scan").json()
    assert result["created"] >= 1
    anomalies = client.get("/api/cases", params={"view": "anomalies"}).json()
    atypical = [item for item in anomalies if item["procedure"] == "IMPORTE_ATIPICO"]
    assert atypical and "ENDESA" in atypical[0]["title"]

    # Escanear de nuevo no duplica
    assert client.post("/api/agents/anomalies/scan").json()["created"] == 0

    # Si una persona lo descarta, no vuelve a salir
    client.post(f"/api/cases/{atypical[0]['id']}/resolve", json={"dismiss": True, "resolution": "Regularización anual, es correcto."})
    assert client.post("/api/agents/anomalies/scan").json()["created"] == 0


def test_briefing_and_agents_and_memory(client):
    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    briefing = client.get("/api/briefing").json()
    assert briefing["items"] and briefing["items"][0]["source"] == "case"
    assert briefing["headline"].startswith("Hoy deberías revisar")

    agents = client.get("/api/agents").json()
    codes = {item["code"]: item for item in agents["agents"]}
    assert len(codes) == 8 and codes["gestor"]["steps"] >= 1
    assert agents["ai_enabled"] is False and agents["engine"] == "reglas"

    hits = client.get("/api/memory/search", params={"q": "arrendamiento comprobación limitada"}).json()
    assert hits and hits[0]["kind"] in {"document", "notification", "case"}
    answer = client.post("/api/memory/ask", json={"question": "¿Qué nos pidió Hacienda sobre el modelo 303?"}).json()
    assert answer["sources"] and answer["engine"] == "memoria (búsqueda)"


def test_second_similar_notification_finds_antecedent(client):
    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    second = REQUERIMIENTO.replace("2026CL0099887", "2026CL0011223").replace("periodo 2T", "periodo 1T")
    upload_text(client, "requerimiento_303_1t.txt", second)
    cases = client.get("/api/cases").json()
    assert len(cases) == 2
    newest = max(cases, key=lambda item: item["id"])
    detail = client.get(f"/api/cases/{newest['id']}").json()
    assert detail["antecedents"] and "2026CL0099887" in detail["antecedents"][0]["title"]


def test_pending_notifications_are_processed_by_automation(client):
    from app.database import SessionLocal
    from app.models import FiscalNotification

    setup_company(client)
    with SessionLocal() as database:
        database.add(FiscalNotification(issuer="TGSS", notification_type="REQUERIMIENTO", title="Requerimiento de documentación", status="PENDING", deadline=TODAY + timedelta(days=5)))
        database.commit()
    run = client.post("/api/automations/AGENT_PIPELINE/run").json()
    assert run["status"] == "OK" and run["items"] == 1
    assert client.get("/api/cases").json()[0]["organism"] == "TGSS"


def test_ai_layer_is_used_when_configured(client, monkeypatch):
    """Con clave, el Fiscal y el Gestor usan Claude; la traza lo refleja."""
    import json
    from types import SimpleNamespace

    from app.agents import llm
    from app.config import settings

    calls = []

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs["output_config"].get("format"):
                text = json.dumps({
                    "summary": "Hacienda revisa el IVA del segundo trimestre y pide los libros y un contrato.",
                    "requested_documents": [{"label": "Extractos bancarios", "detail": "del segundo trimestre"}],
                    "tax_references": [{"model": "303", "year": str(TODAY.year), "period": "2T"}],
                    "response_days": "10 días hábiles",
                    "recommended_actions": ["Revisar las facturas de proveedores del trimestre"],
                })
            else:
                text = "ESCRITO MEJORADO POR LA IA\nEXPONE\n...\nSOLICITA\n..."
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))

    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    case = open_case(client)

    engines = {step["agent"]: step["engine"] for step in case["run"]["steps"]}
    assert engines["fiscal"] == "claude-opus-5-5"
    assert engines["gestor"] == "claude-opus-5-5"
    assert case["summary"].startswith("Hacienda revisa el IVA")
    assert case["draft_response"].startswith("ESCRITO MEJORADO POR LA IA")
    assert any(item["code"] == "EXTRACTOS" for item in case["documents"])
    assert any(action["label"] == "Revisar las facturas de proveedores del trimestre" for action in case["actions"])

    first = calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"]["format"]["type"] == "json_schema"


def test_ai_refusal_falls_back_to_rules(client, monkeypatch):
    from types import SimpleNamespace

    from app.agents import llm
    from app.config import settings

    class Refusing:
        def create(self, **kwargs):
            return SimpleNamespace(stop_reason="refusal", content=[])

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Refusing())))
    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    case = open_case(client)
    assert {step["engine"] for step in case["run"]["steps"] if step["agent"] in {"fiscal", "gestor"}} == {"reglas"}
    assert "EXPONE" in case["draft_response"]


EMBARGO_WITH_DEBT = EMBARGO + """Principal 1.000,00 €
Recargo de apremio 200,00 €
Importe pendiente 1.200,00 €
Deberá comunicar en el plazo de 5 días hábiles la existencia de créditos.
"""


def test_embargo_retains_only_the_debt_when_we_owe_more(client):
    """Debemos 1.234,56 € al embargado y su deuda es 1.200,00 €: se retienen 1.200,00 €."""
    from app.database import SessionLocal
    from app.models import Document
    from app.models import Invoice

    setup_company(client)
    with SessionLocal() as database:
        document = Document(original_filename="factura_lopez.pdf", stored_filename="x.pdf", sha256="b" * 64, extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
        database.add(document)
        database.flush()
        database.add(Invoice(
            document_id=document.id, supplier_name="SUMINISTROS LOPEZ S.L.", supplier_tax_id="B23456783", direction="RECEIVED",
            invoice_number="SL-78", invoice_date=TODAY - timedelta(days=10), total=Decimal("1234.56"), subtotal=Decimal("1020.30"),
            tax_total=Decimal("214.26"), currency="EUR", confidence=95, field_confidences={}, validation_status="VALID",
            validation_messages=[], review_status="APPROVED", duplicate_status="NONE",
        ))
        database.commit()

    upload_text(client, "embargo.txt", EMBARGO_WITH_DEBT)
    case = open_case(client)
    insights = " ".join(case["facts"]["insights"])
    assert case["amount"] == 1200.0
    assert "Retén 1.200,00 €" in insights and "34,56 €" in insights  # el resto se le puede pagar
    assert "Total pendiente: 1.200,00 €" in insights
