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
    assert kinds(by_key["falta"]) == ["bank_none"]  # lo único que falta: el banco
