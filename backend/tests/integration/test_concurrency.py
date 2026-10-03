"""Lo que pasa cuando dos cosas ocurren a la vez, y lo que no puede pasar nunca.

    planificador   varios procesos (uvicorn --workers, dos instancias): lo que toca se ejecuta una vez
    conciliación   un movimiento no queda asignado a dos facturas, ni una factura pagada por dos movimientos
    duplicados     dos peticiones que crean lo mismo: 409 explicable, no 500

Las pruebas de procesos simultáneos son de PostgreSQL (SQLite funciona con un solo proceso). Datos sintéticos.
"""
from __future__ import annotations

import threading
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

PROCESSES = 4


def postgres_only():
    from app.database import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("El turno entre procesos es de PostgreSQL; SQLite funciona con un solo proceso.")


def test_only_one_process_gets_the_turn_until_it_releases_it(client):
    """El turno es un cerrojo de sesión en una conexión propia: los commit del trabajo no lo sueltan
    (el buzón confirma correo a correo); solo se suelta al salir."""
    postgres_only()
    from app.automation_service import scheduler_turn
    from app.database import SessionLocal

    with scheduler_turn(7) as first:
        assert first
        with SessionLocal() as work:  # el trabajo confirma por su cuenta: el turno sigue siendo de quien lo tiene
            work.commit()
        with scheduler_turn(7) as second:
            assert not second  # el mismo cliente: espera a la siguiente vuelta
        with scheduler_turn(8) as other:
            assert other  # otro cliente: en paralelo
    with scheduler_turn(7) as again:
        assert again  # soltado al salir


def test_simultaneous_ticks_run_each_due_automation_once(client):
    postgres_only()
    from app.automation_service import local_now
    from app.automation_service import run_due
    from app.database import SessionLocal
    from app.models import AutomationRun

    now = local_now().replace(hour=23, minute=0)  # a esta hora todo lo diario ya toca
    start = threading.Barrier(PROCESSES)
    errors: list[BaseException] = []

    def tick():
        database = SessionLocal()
        database.info["tenant_id"] = 0
        try:
            start.wait()
            run_due(database, now=now)
            database.commit()
        except BaseException as error:  # noqa: BLE001 — se comprueba abajo
            database.rollback()
            errors.append(error)
        finally:
            database.close()

    threads = [threading.Thread(target=tick) for _ in range(PROCESSES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        runs = Counter(database.query(AutomationRun.code).filter(AutomationRun.trigger == "SCHEDULE").all())
    assert runs and max(runs.values()) == 1, runs


def movement_and_invoices():
    from app.database import SessionLocal
    from app.models import BankTransaction
    from tests.workflows.test_scenarios import add_invoice

    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        first = add_invoice(database, supplier="PAPELERÍA SIMULADA S.L.", tax_id="B00000017", number="C-1", total=121, when=date(2026, 9, 3))
        second = add_invoice(database, supplier="PAPELERÍA SIMULADA S.L.", tax_id="B00000017", number="C-2", total=121, when=date(2026, 9, 4))
        movement = BankTransaction(booking_date=date(2026, 9, 10), description="TRANSFERENCIA PAPELERIA SIMULADA", amount=Decimal("-121.00"),
                                   fingerprint="fp-concurrencia-1", match_status="UNMATCHED")
        other = BankTransaction(booking_date=date(2026, 9, 11), description="TRANSFERENCIA PAPELERIA SIMULADA", amount=Decimal("-121.00"),
                                fingerprint="fp-concurrencia-2", match_status="UNMATCHED")
        database.add_all([movement, other])
        database.commit()
        return movement.id, other.id, first.id, second.id


def test_a_reconciled_movement_is_not_silently_moved_to_another_invoice(client):
    movement, other, first, second = movement_and_invoices()
    assert client.post(f"/api/bank/transactions/{movement}/confirm", json={"invoice_id": first}).status_code == 200
    moved = client.post(f"/api/bank/transactions/{movement}/confirm", json={"invoice_id": second})
    assert moved.status_code in (400, 409) and "deshaz" in moved.json()["detail"]
    twice = client.post(f"/api/bank/transactions/{other}/confirm", json={"invoice_id": first})
    assert twice.status_code in (400, 409) and "otro movimiento" in twice.json()["detail"]
    assert client.post(f"/api/bank/transactions/{movement}/confirm", json={"invoice_id": first}).status_code == 200  # repetir es inocuo


def test_two_people_reconciling_the_same_movement_at_once(client):
    postgres_only()
    from app.database import SessionLocal
    from app.models import BankTransaction
    from app.models import Invoice

    movement, _other, first, second = movement_and_invoices()
    start = threading.Barrier(2)
    answers: list[int] = []

    def reconcile(invoice_id):
        start.wait()
        answers.append(client.post(f"/api/bank/transactions/{movement}/confirm", json={"invoice_id": invoice_id}).status_code)

    threads = [threading.Thread(target=reconcile, args=(invoice_id,)) for invoice_id in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(answers) == 2 and sorted(answers) in ([200, 400], [200, 409]), answers
    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        paid = database.query(Invoice).filter(Invoice.id.in_((first, second)), Invoice.paid_at.is_not(None)).count()
        assert paid == 1  # una sola factura pagada: la del movimiento
        assert database.get(BankTransaction, movement).matched_invoice_id in (first, second)


def test_simultaneous_requests_creating_the_same_close_never_end_in_500(client):
    postgres_only()
    from app.database import SessionLocal
    from app.models import PeriodClose

    start = threading.Barrier(PROCESSES)
    answers: list[int] = []

    def review():
        start.wait()
        answers.append(client.post("/api/close/2026-08/run").status_code)

    threads = [threading.Thread(target=review) for _ in range(PROCESSES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(answers) == PROCESSES and set(answers) <= {200, 409} and 200 in answers, answers
    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        assert database.query(PeriodClose).filter(PeriodClose.period == "2026-08").count() == 1
