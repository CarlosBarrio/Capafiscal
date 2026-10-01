"""Perseguidor 48 h / 5 días / 8 días + memoria del negocio («raro para ti») + aprendizaje visible."""
from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from tests.agents.test_agents import REQUERIMIENTO
from tests.agents.test_agents import setup_company
from tests.agents.test_agents import upload_text
from tests.agents.test_proactive import COMPANY
from tests.workflows.test_scenarios import add_invoice


def send_pending_drafts(client):
    for message in client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"]:
        if message["status"] == "DRAFT":
            client.post(f"/api/outbox/{message['id']}/mark-sent")


def test_chase_reminds_at_48h_insists_at_5_days_and_tells_the_gestor_at_8(client):
    from app.agents.perseguidor import follow_up
    from app.database import SessionLocal
    from app.models import Case
    from app.models import CaseEvent

    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    send_pending_drafts(client)  # la petición inicial sale hoy
    sent = datetime.now(timezone.utc).date()

    def run(days):
        with SessionLocal() as database:
            result = follow_up(database, today=sent + timedelta(days=days))
            database.commit()
        send_pending_drafts(client)
        return result

    assert run(1)["reminders"] == 0
    assert run(2)["reminders"] == 1
    assert run(4)["reminders"] == 0
    assert run(5)["reminders"] == 1
    levels = sorted(item["level"] or 0 for item in client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"])
    assert levels == [0, 1, 2]
    urgent = [item for item in client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"] if item["level"] == 2]
    assert urgent[0]["subject"].startswith("URGENTE")

    escalation = run(8)
    assert escalation == {"reminders": 0, "escalated": 1}
    assert run(12) == {"reminders": 0, "escalated": 0}  # ya está delante de una persona: no se insiste más
    with SessionLocal() as database:
        case = database.query(Case).filter(Case.kind == "NOTIFICATION").one()
        assert case.status == "WAITING_HUMAN" and case.facts["chase_escalated"]["days"] == 8
        assert database.query(CaseEvent).filter(CaseEvent.title.like("%aviso al gestor%")).count() == 1


def test_unusual_for_this_company(client):
    from app.business_memory import unusual
    from app.database import SessionLocal
    from app.models import ExtractionRun

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        def with_iban(invoice, iban):
            database.add(ExtractionRun(document_id=invoice.document_id, extractor_name="prueba", extractor_version="1", status="COMPLETED",
                                       raw_text=f"Pago por transferencia a {iban}", started_at=datetime.now(timezone.utc)))
            return invoice

        for month in range(4, 10):
            with_iban(add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{month}", total=400 + month, when=date(2026, month, 5)),
                      "ES00 0000 0000 1111 2222 3333")
        odd = with_iban(add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-9b", total=1300, when=date(2026, 9, 10), vat_rate=10),
                        "ES99 0000 0000 4444 5555 6666")
        new = add_invoice(database, supplier="PROVEEDOR NUEVO S.L.", tax_id="B00700074", number="PN-1", total=100, when=date(2026, 9, 12))
        database.commit()
        signals = {signal["kind"]: signal for signal in unusual(database, odd)["signals"]}
        assert {"importe", "iva", "frecuencia", "iban"} <= set(signals)
        assert "entre" in signals["importe"]["text"] and "veces lo habitual" in signals["importe"]["text"]
        assert signals["iban"]["severity"] == "high" and "…6666" in signals["iban"]["text"] and "3333" not in signals["iban"]["text"]
        assert [signal["kind"] for signal in unusual(database, new)["signals"]] == ["nuevo"]
        normal = add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-10", total=405, when=date(2026, 10, 5))
        assert unusual(database, normal)["signals"] == []
        odd_id = odd.id
        database.rollback()
    assert {signal["kind"] for signal in client.get(f"/api/invoices/{odd_id}/memory").json()["signals"]} >= {"importe", "iban"}


def test_work_center_puts_the_rare_thing_first_and_shows_what_was_learned(client):
    from app.database import SessionLocal
    from app.models import Invoice

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month in range(4, 10):
            add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{month}", total=400, when=date(2026, month, 5))
        odd = add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-X", total=2000, when=date(2026, 9, 20))
        database.get(Invoice, odd.id).review_status = "PENDING"
        database.commit()
    data = client.get("/api/work").json()
    item = next(row for row in data["groups"][0]["items"] if row["kind"] == "invoice")
    assert "suele facturarte" in item["why"] and "5 veces lo habitual" in item["why"]
    assert data["learning"]["approved"] == 0 and "aprende de tus correcciones" in data["learning"]["text"]


def test_reading_profiles_never_writes(client):
    from app.database import SessionLocal
    from app.models import CounterpartyProfile

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-1", total=400, when=date(2026, 9, 5))
        database.commit()
    listed = client.get("/api/memory/profiles", params={"refresh": True}).json()
    assert listed[0]["key"] == "B11111110"
    with SessionLocal() as database:
        assert database.query(CounterpartyProfile).count() == 0
