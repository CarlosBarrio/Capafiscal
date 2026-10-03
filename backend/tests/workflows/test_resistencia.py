"""
Prueba de resistencia: 100 eventos a la vez por la entrada única.

    20 facturas · 20 notificaciones · 15 embargos · 15 plazos
    10 duplicados · 10 documentos ambiguos · 10 con un agente que falla

Comprueba que no se duplican expedientes, no se pierden eventos, los errores
quedan aislados, se pueden reanudar, la traza es coherente y el Director
refleja el estado real.
"""
from __future__ import annotations

import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from tests.workflows.test_scenarios import TODAY
from tests.workflows.test_scenarios import add_invoice

WORKERS = 8


def build_events(invoice_ids: list[int]) -> list[dict]:
    events: list[dict] = []
    for index, invoice_id in enumerate(invoice_ids):
        events.append({"kind": "invoice", "source": "estres", "external_id": f"factura-{index}", "invoice_id": invoice_id})
    for index in range(20):
        events.append({"kind": "notification", "source": "estres", "external_id": f"req-{index}", "notification": {
            "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento de información",
            "reference": f"REQ-{index:03d}", "summary": f"Aporte el libro registro de facturas recibidas del {index % 4 + 1}T.",
        }})
    for index in range(15):
        events.append({"kind": "notification", "source": "estres", "external_id": f"emb-{index}", "notification": {
            "issuer": "AEAT", "notification_type": "EMBARGO", "title": "Diligencia de embargo de créditos",
            "reference": f"EMB-{index:03d}", "summary": "Se declaran embargados los créditos del deudor; deberá retener los importes pendientes.",
        }})
    periods = [(model, year, quarter) for year in (2025, 2026) for model in ("303", "130", "111", "115") for quarter in (1, 2, 3, 4)]
    for index, (model, year, quarter) in enumerate(periods[:15]):
        events.append({"kind": "deadline", "source": "estres", "external_id": f"plazo-{index}", "model": model, "year": year, "quarter": quarter,
                       "due": (TODAY + timedelta(days=5 + index)).isoformat()})
    for index in range(10):  # duplicados de facturas y notificaciones ya enviadas
        events.append(dict(events[index * 3]))
    for index in range(10):  # ambiguos: sin tipo ni texto útil
        events.append({"kind": "notification", "source": "estres", "external_id": f"ambiguo-{index}", "notification": {"title": f"Documento {index}"}})
    for index in range(10):  # el agente Fiscal fallará en estos
        events.append({"kind": "notification", "source": "estres", "external_id": f"fallo-{index}", "notification": {
            "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento de información", "reference": f"FALLO-{index:03d}",
            "summary": "Requerimiento sobre el modelo 303.",
        }})
    return events


def test_one_hundred_events_at_once(client, monkeypatch):
    from app.agents.orchestrator import AGENTS
    from app.database import SessionLocal

    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es"})
    with SessionLocal() as database:
        invoice_ids = []
        for index in range(20):
            supplier = f"PROVEEDOR {index % 5} S.L."
            invoice = add_invoice(database, supplier=supplier, tax_id=f"B0000000{index % 5}", number=f"ST-{index}", total=100 + index * (40 if index % 7 == 0 else 1), when=TODAY - timedelta(days=60 - index))
            invoice_ids.append(invoice.id)
        database.commit()

    real_fiscal = AGENTS["fiscal"].run

    def flaky_fiscal(ctx):
        notification = ctx.facts.get("notification")
        if notification is not None and (notification.reference or "").startswith("FALLO-"):
            raise RuntimeError("fallo inyectado en la prueba de resistencia")
        return real_fiscal(ctx)

    monkeypatch.setattr(AGENTS["fiscal"], "run", flaky_fiscal)

    events = build_events(invoice_ids)
    assert len(events) == 100
    random.Random(3).shuffle(events)

    def send(body: dict):
        response = client.post("/api/events", json=body)
        return body["external_id"], response.status_code, response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(send, events))

    statuses = Counter(code for _id, code, _body in results)
    assert set(statuses) <= {200, 201}, [(external, code, body) for external, code, body in results if code not in {200, 201}][:3]

    # 1) No se pierden eventos y no se duplican: 90 distintos, 10 repetidos
    listing = client.get("/api/events", params={"limit": 200}).json()
    registered = listing["events"]
    assert len(registered) == 90
    assert len({item["external_id"] for item in registered}) == 90
    expected_duplicates = {body["external_id"] for body in build_events(invoice_ids)[70:80]}
    counted = {item["external_id"]: item for item in registered if item["external_id"] in expected_duplicates}
    assert sum(item["duplicates"] for item in registered) == 10, [(key, item["duplicates"], item["attempts"], item["status"]) for key, item in counted.items() if item["duplicates"] != 1] + [(code, body) for key, code, body in results if key in expected_duplicates and code == 201 and isinstance(body, dict) and not body.get("duplicate")][:4]
    assert not [item for item in registered if item["status"] in {"RECEIVED", "PROCESSING"}]

    # 2) Los errores quedan aislados: solo los 10 inyectados
    needs_human = [item for item in registered if item["status"] == "NEEDS_HUMAN"]
    assert len(needs_human) == 10 and all(item["failed_agent"] == "fiscal" and item["external_id"].startswith("fallo-") for item in needs_human)
    assert not [item for item in registered if item["status"] == "FAILED"]

    # 3) Ningún expediente duplicado y códigos únicos
    from app.models import AgentRun
    from app.models import AgentStep
    from app.models import Case

    with SessionLocal() as database:
        cases = database.query(Case).all()
        codes = Counter(case.code for case in cases)
        assert not [code for code, count in codes.items() if count > 1], "códigos de expediente repetidos"
        notification_ids = Counter(case.notification_id for case in cases if case.notification_id)
        assert not [key for key, count in notification_ids.items() if count > 1]
        fingerprints = Counter(case.fingerprint for case in cases if case.fingerprint)
        assert not [key for key, count in fingerprints.items() if count > 1]
        assert sum(1 for case in cases if case.kind == "DEADLINE") == 15

        # 4) Traza coherente: cada recorrido tiene pasos 1..n y su expediente existe
        for run in database.query(AgentRun).all():
            positions = [step.position for step in database.query(AgentStep).filter(AgentStep.run_id == run.id).order_by(AgentStep.position)]
            assert positions == list(range(1, len(positions) + 1)), f"recorrido {run.id} con pasos {positions}"
            assert run.status in {"OK", "PARTIAL"}
            if run.case_id:
                assert database.get(Case, run.case_id) is not None

    # 5) El Director lo refleja
    board = client.get("/api/briefing").json()["board"]
    assert board["intervention"]["total"] == 90
    assert sum(1 for item in board["attention"]["items"] if item["blocked"]) == 10

    # 6) Se puede reanudar: sin el fallo, los 10 se completan
    monkeypatch.setattr(AGENTS["fiscal"], "run", real_fiscal)
    for item in needs_human:
        assert client.post(f"/api/events/{item['id']}/retry").json()["event"]["status"] == "COMPLETED"
    final = client.get("/api/events", params={"limit": 200}).json()
    assert final["counts"]["COMPLETED"] == 90 and final["counts"]["NEEDS_HUMAN"] == 0
    assert not [item for item in client.get("/api/briefing").json()["board"]["attention"]["items"] if item["blocked"]]
