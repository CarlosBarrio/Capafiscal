"""Cierre mensual: CapaFiscal hace su parte, comprueba el mes y dice qué bloquea el cierre."""
from __future__ import annotations

from datetime import date

from tests.agents.test_proactive import COMPANY
from tests.agents.test_proactive import import_csv
from tests.workflows.test_scenarios import add_invoice

PERIOD = "2026-09"


def setup_month(client, *, full_statement: bool = True):
    from app.database import SessionLocal
    from app.models import Invoice

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        paid = add_invoice(database, supplier="ACME SUMINISTROS S.L.", tax_id="B00000017", number="FAC-0901", total=1210.00, when=date(2026, 9, 2))
        add_invoice(database, supplier="TALLERES OTRO S.L.", tax_id="B00000025", number="T-0902", total=363.00, when=date(2026, 9, 5))
        pending = add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-0903", total=242.00, when=date(2026, 9, 9))
        database.get(Invoice, pending.id).review_status = "PENDING"
        database.commit()
        ids = {"paid": paid.id, "pending": pending.id}
    rows = [
        (date(2026, 9, 10), "TRANSFERENCIA ACME FAC-0901", "-1210,00"),
        (date(2026, 9, 18), "TRANSFERENCIA A DESCONOCIDO SL", "-2350,00"),
        (date(2026, 9, 22), "COMISION MANTENIMIENTO", "-6,00"),
        (date(2026, 9, 25), "RECIBO LIMPIEZAS SOL LS-0903", "-242,00"),  # su factura aún no está aprobada: no se concilia al importar
    ]
    if full_statement:
        rows.append((date(2026, 9, 30), "TRANSFERENCIA TALLERES OTRO T-0902", "-363,00"))
    import_csv(client, rows)
    return ids


def checks(state):
    return {item["key"]: item for item in state["checks"]}


def test_close_state_lists_what_blocks_the_month_and_does_not_write(client):
    from app.database import SessionLocal
    from app.models import PeriodClose

    setup_month(client)
    state = client.get("/api/close", params={"period": PERIOD}).json()
    by_key = checks(state)
    assert by_key["recibidas"]["status"] == "block" and by_key["recibidas"]["count"] == 1
    assert by_key["extracto"]["status"] == "ok"
    assert by_key["conciliacion"]["status"] == "block" and "1 sin justificar" in by_key["conciliacion"]["detail"]  # la comisión de 6 € no bloquea
    assert by_key["duplicados"]["status"] == "ok" and by_key["iva"]["status"] == "ok"
    assert state["blockers"] >= 2 and not state["ready"] and "bloquean el cierre" in state["headline"]
    # % cerrado contable a mano: 2 de 3 facturas + movimientos conciliados/justificados de 4
    assert state["units"]["total"] == 3 + 5 and 0 < state["percent"] < 100
    with SessionLocal() as database:
        assert database.query(PeriodClose).count() == 0  # mirar el cierre no escribe


def test_run_reconciles_what_is_safe_and_records_the_work(client):
    ids = setup_month(client)
    client.post(f"/api/invoices/{ids['pending']}/approve")
    before = client.get("/api/close", params={"period": PERIOD}).json()["percent"]
    result = client.post(f"/api/close/{PERIOD}/run").json()
    assert result["work"]["auto_matched"] >= 1
    assert result["percent"] > before and result["last_run_at"]
    actions = {event["action"] for event in client.get("/api/activity", params={"limit": 50}).json()}
    assert "close.run" in actions


def test_missing_statement_blocks(client):
    setup_month(client, full_statement=False)
    by_key = checks(client.get("/api/close", params={"period": PERIOD}).json())
    assert by_key["extracto"]["status"] == "block" and "25/09/2026" in by_key["extracto"]["detail"]


def test_close_needs_a_note_while_blockers_remain_and_can_reopen(client):
    ids = setup_month(client)
    assert client.post(f"/api/close/{PERIOD}/close", json={}).status_code == 409
    closed = client.post(f"/api/close/{PERIOD}/close", json={"note": "La transferencia de 2.350 € es un préstamo: se justifica en octubre."}).json()
    assert closed["status"] == "CLOSED" and closed["closed"]["blockers"] >= 1 and "préstamo" in closed["closed"]["note"]
    history = client.get("/api/close", params={"period": PERIOD}).json()["history"]
    assert history[0]["period"] == PERIOD and history[0]["status"] == "CLOSED"
    assert client.post(f"/api/close/{PERIOD}/reopen").json()["status"] == "OPEN"
    assert client.post(f"/api/close/{PERIOD}/reopen").status_code == 409

    # Resuelto lo que bloqueaba, se cierra sin nota.
    client.post(f"/api/invoices/{ids['pending']}/approve")
    from app.database import SessionLocal
    from app.models import BankTransaction

    with SessionLocal() as database:
        unknown = database.query(BankTransaction).filter(BankTransaction.description.like("%DESCONOCIDO%")).one()
        unknown_id = unknown.id
    client.post(f"/api/bank/transactions/{unknown_id}/unmatch", json={"ignore": True})
    client.post(f"/api/close/{PERIOD}/run")
    state = client.get("/api/close", params={"period": PERIOD}).json()
    assert state["ready"], [item for item in state["checks"] if item["status"] == "block"]
    assert client.post(f"/api/close/{PERIOD}/close", json={}).json()["status"] == "CLOSED"


def test_bad_period_is_rejected(client):
    assert client.get("/api/close", params={"period": "septiembre"}).status_code == 422
