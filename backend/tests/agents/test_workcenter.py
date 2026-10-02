"""Centro de trabajo: una sola lista (decides tú / falta información / lo hace CapaFiscal / resuelto) que no escribe."""
from __future__ import annotations

from tests.agents.test_closing import setup_month


def groups(data):
    return {group["key"]: group for group in data["groups"]}


def kinds(group):
    return [item["kind"] for item in group["items"]]


def test_one_list_with_what_was_checked_and_an_action(client):
    ids = setup_month(client)
    data = client.get("/api/work").json()
    by_key = groups(data)
    assert [group["key"] for group in data["groups"]] == ["accion", "falta", "haciendo", "resuelto"]

    # La factura pendiente es una decisión tuya, con lo comprobado y la acción para abrirla.
    invoice = next(item for item in by_key["accion"]["items"] if item["kind"] == "invoice")
    assert "LS-0903" in invoice["title"] and invoice["action"]["document_id"]
    assert any(check["label"].startswith("Base + IVA") for check in invoice["checked"])

    # La transferencia de 2.350 € sin factura falta por justificar; la comisión de 6 € no molesta.
    unjustified = [item for item in by_key["falta"]["items"] if item["kind"] == "bank_unjustified"]
    assert len(unjustified) == 1 and "2.350,00 €" in unjustified[0]["why"] and unjustified[0]["action"]["label"] == "Investigar"

    assert all(item["action"] for group in data["groups"][:3] for item in group["items"])
    assert data["counts"]["accion"] == len(by_key["accion"]["items"]) and "tu decisión" in data["headline"]
    assert data["close"]["period"] and "cerrado" in data["close"]["headline"]
    assert "label" in data["pulse"] and "reconciled" in {line["key"] for line in data["overnight"]["items"]}  # lo conciliado al importar
    assert ids


def test_approving_moves_the_item_out_of_the_list(client):
    ids = setup_month(client)
    client.post(f"/api/invoices/{ids['pending']}/approve")
    data = client.get("/api/work").json()
    assert not any(item["kind"] == "invoice" for item in groups(data)["accion"]["items"])


def test_work_center_never_writes(client):
    """Lo primero que se abre por la mañana, en varias pestañas a la vez: no puede bloquear SQLite."""
    from concurrent.futures import ThreadPoolExecutor

    from app.database import SessionLocal
    from app.models import BankTransaction
    from app.models import Case

    setup_month(client)
    client.post("/api/agents/anomalies/scan", json={})
    with SessionLocal() as database:
        before = [(row.id, row.match_status, row.matched_invoice_id) for row in database.query(BankTransaction).order_by(BankTransaction.id)]
        priorities = [(row.id, row.priority, row.level, row.status, row.headline) for row in database.query(Case).order_by(Case.id)]
    urls = ["/api/work", "/api/today", "/api/briefing"] * 4
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda url: client.get(url).status_code, urls))
    assert codes == [200] * len(urls)
    with SessionLocal() as database:
        assert [(row.id, row.match_status, row.matched_invoice_id) for row in database.query(BankTransaction).order_by(BankTransaction.id)] == before
        assert [(row.id, row.priority, row.level, row.status, row.headline) for row in database.query(Case).order_by(Case.id)] == priorities


def test_empty_company_has_a_calm_list(client):
    data = client.get("/api/work").json()
    by_key = groups(data)
    assert by_key["accion"]["items"] == [] and data["headline"] == "Nada requiere tu decisión hoy"
    assert set(kinds(by_key["falta"])) <= {"bank_none", "compliance"}  # lo único que falta: el banco y datos de cumplimiento


def test_metrics_count_finished_work_not_processed_documents(client):
    ids = setup_month(client)
    before = client.get("/api/work/metrics").json()
    assert before["finished"]["invoices"] == 2 and before["finished"]["movements"] >= 3  # FAC-0901 y T-0902 pagadas y conciliadas; la comisión justificada
    client.post(f"/api/invoices/{ids['pending']}/approve")
    client.post("/api/close/2026-09/run")
    after = client.get("/api/work/metrics").json()
    assert after["finished"]["invoices"] == 3 and after["decisions"] >= 1 and after["without_human"] >= 1
    assert "Terminado =" in after["definition"] and after["false_positives"]["rate"] is None


def test_each_decision_explains_itself_and_nothing_appears_twice(client):
    """Hoy: qué pasa, qué ha comprobado CapaFiscal, qué propone y qué te toca; un expediente abierto no se repite suelto."""
    from datetime import timedelta

    from app import clock
    from tests.agents.test_today_learning import notify

    setup_month(client)
    notify(client, "req-1", issuer="AEAT", notification_type="REQUERIMIENTO", title="Requerimiento de información", reference="RQ-1",
           summary="Aporte el libro registro de facturas recibidas.", deadline=(clock.today() + timedelta(days=2)).isoformat())
    client.post("/api/agents/pulse/run")  # el Detector abre el expediente del pago de 2.350 € sin factura
    by_key = groups(client.get("/api/work").json())

    notice = next(item for item in by_key["accion"]["items"] if item["kind"] == "case" and "Requerimiento" in item["title"])
    assert notice["you"] == "Revisar y aprobar la contestación" and notice["proposal"].startswith("Contestar")
    assert notice["checked"][0]["label"].startswith("Ha leído la notificación") and "vence en 2" in notice["means"]
    assert any(check["label"] == "Ha preparado el escrito de contestación" for check in notice["checked"])

    invoice = next(item for item in by_key["accion"]["items"] if item["kind"] == "invoice")
    assert invoice["you"] and invoice["proposal"]

    payment_case = next(item for item in by_key["accion"]["items"] if item["kind"] == "case" and "2.350,00" in item["title"])
    assert payment_case["proposal"] and payment_case["you"] == "Aprobar, corregir o descartar"
    # El mismo pago ya no sale también como «falta información»: tiene su expediente.
    assert not [item for item in by_key["falta"]["items"] if item["kind"] == "bank_unjustified" and "2.350,00" in item["why"]]


def test_today_leaves_out_what_is_not_work(client):
    """El resumen diario sin destinatario es configuración (Automatizaciones) y Verifactu a meses vista vive en
    Cumplimiento: ninguno de los dos es trabajo de hoy."""
    from app.database import SessionLocal
    from app.models import OutboxMessage

    client.put("/api/company", json={"name": "Taller Simulado S.L.", "tax_id": "B00100016", "legal_form": "SOCIEDAD"})
    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        database.add(OutboxMessage(kind="DIGEST", subject="Tu resumen de hoy", body="…", status="DRAFT", entity_type="digest", entity_id=1))
        database.commit()
    by_key = groups(client.get("/api/work").json())
    everything = [item for group in by_key.values() for item in group["items"]]
    assert not [item for item in everything if item["kind"] == "outbox"]
    assert not [item for item in everything if item["kind"] == "compliance" and "Verifactu" in item["title"]]
    assert client.get("/api/outbox", params={"status": "DRAFT"}).json()["messages"]  # sigue en la Bandeja de salida
