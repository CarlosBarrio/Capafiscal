"""
Ejecución continua y Director operativo: los tres disparadores (externo,
temporal y analítico) acaban en el mismo orquestador, y el Director dice qué
requiere atención, qué está pendiente y qué se ha resuelto solo.
"""
from __future__ import annotations


from datetime import timedelta

from tests.workflows.test_scenarios import SCENARIOS_DIR
from tests.workflows.test_scenarios import TODAY
from tests.workflows.test_scenarios import add_invoice
from tests.workflows.test_scenarios import load
from tests.workflows.test_scenarios import run_scenario


def seed_supplier_history(database, *, supplier="ENDESA ENERGIA S.A.", tax_id="A81948077", totals=(110, 95, 120, 105, 100)):
    start = TODAY - timedelta(days=30 * (len(totals) + 1))
    for index, total in enumerate(totals):
        add_invoice(database, supplier=supplier, tax_id=tax_id, number=f"E-{index}", total=total, when=start + timedelta(days=30 * index))


def test_operational_board_says_what_needs_attention_what_waits_and_what_was_solved(client):
    # Un requerimiento (plazo), una factura sospechosa (4,9 veces) y una factura normal.
    run_scenario(client, load(SCENARIOS_DIR / "notification_001.json"))
    run_scenario(client, load(SCENARIOS_DIR / "invoice_anomaly_001.json"))
    normal = run_scenario(client, load(SCENARIOS_DIR / "invoice_normal_001.json"))
    assert normal[0] is None
    # La misma factura normal entra otra vez: repetida, se ignora
    invoice_id = client.get("/api/events").json()["events"][0]
    client.post("/api/events", json={"kind": "invoice", "source": "test", "external_id": invoice_id["external_id"], "invoice_id": int(invoice_id["external_id"].split(":")[1])})

    board = client.get("/api/briefing").json()["board"]
    reasons = [item["reason"] for item in board["attention"]["items"]]
    assert board["attention"]["count"] >= 2
    assert any("veces por encima de lo habitual" in reason for reason in reasons)
    assert any("vence" in reason or "objetivo interno" in reason or "días" in reason for reason in reasons)
    pending = {item["key"]: item["count"] for item in board["pending"]["items"]}
    assert pending.get("requested", 0) >= 2  # el Perseguidor pidió los documentos del requerimiento
    resolved = {item["key"]: item["count"] for item in board["resolved"]["items"]}
    assert resolved["no_case"] >= 1 and resolved["duplicates"] >= 1 and resolved["prepared"] >= 1
    assert 1 <= len(board["top"]) <= 3 and board["top"][0]["case_id"]


def test_blocked_processing_goes_to_the_top(client, monkeypatch):
    from app.agents.orchestrator import AGENTS

    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})
    monkeypatch.setattr(AGENTS["gestor"], "run", lambda ctx: (_ for _ in ()).throw(RuntimeError("plantilla no encontrada")))
    client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-B1", "notification": {"issuer": "AEAT", "notification_type": "COMUNICACION", "title": "Comunicación"}})
    board = client.get("/api/briefing").json()["board"]
    first = board["attention"]["items"][0]
    assert first["blocked"] is True and "Gestor de incidencias no pudo terminar" in first["reason"]
    assert any(item["key"] == "blocked" for item in board["pending"]["items"])


def test_analytic_trigger_sends_serious_findings_to_the_orchestrator(client):
    """Una factura que entró sin pasar por los agentes (p. ej. importada en bloque):
    el barrido la detecta y, por ser grave, el orquestador la investiga entera."""
    from app.automation_service import run_automation
    from app.database import SessionLocal

    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})
    with SessionLocal() as database:
        seed_supplier_history(database)
        add_invoice(database, supplier="ENDESA ENERGIA S.A.", tax_id="A81948077", number="FAC-9001", total=640, when=TODAY - timedelta(days=5))
        database.commit()
        run = run_automation(database, "ANOMALY_SCAN")
        database.commit()
        assert "investigada(s) a fondo por el orquestador" in run.summary

    anomalies = client.get("/api/cases", params={"view": "anomalies"}).json()
    suspicious = [item for item in anomalies if item["procedure"] == "FACTURA_SOSPECHOSA"]
    assert len(suspicious) == 1
    case = client.get(f"/api/cases/{suspicious[0]['id']}").json()
    assert case["route"]["code"] == "factura_sospechosa" and case["route"]["source"] == "detector"
    assert [step["agent"] for step in case["run"]["steps"]] == ["vigilante", "expedientes", "memoria", "detector", "fiscal", "gestor", "director"]
    # No hay además un aviso suelto de «importe atípico» para la misma factura
    assert not [item for item in anomalies if item["procedure"] == "IMPORTE_ATIPICO" and "FAC-9001" in (item["summary"] or "")]
    events = client.get("/api/events").json()["events"]
    assert any(item["source"] == "detector" and item["external_id"].startswith("analitico:") for item in events)

    # Volver a pasar el barrido no duplica nada
    with SessionLocal() as database:
        run_automation(database, "ANOMALY_SCAN")
        database.commit()
    assert len([item for item in client.get("/api/cases", params={"view": "anomalies"}).json() if item["procedure"] == "FACTURA_SOSPECHOSA"]) == 1


def test_pulse_runs_the_three_triggers_and_reports_activity(client, monkeypatch):
    from app.config import settings

    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})
    pulse = client.get("/api/agents/pulse").json()
    assert pulse["healthy"] is False and "desactivado" in pulse["label"]  # en los tests no hay planificador

    monkeypatch.setattr(settings, "enable_scheduler", True)
    assert "Arrancando" in client.get("/api/agents/pulse").json()["label"]

    result = client.post("/api/agents/pulse/run").json()
    assert [item["code"] for item in result["runs"]] == ["EMAIL_INBOX", "AGENT_PIPELINE", "DEADLINE_WATCH", "FOLLOW_UP", "ANOMALY_SCAN"]
    assert all(item["status"] in {"OK", "NOTHING"} for item in result["runs"])
    pulse = result["pulse"]
    assert pulse["healthy"] is True and "CapaFiscal está trabajando" in pulse["label"]
    assert {item["key"] for item in pulse["triggers"]} == {"externo", "temporal", "analitico"}
    briefing = client.get("/api/briefing").json()
    assert briefing["pulse"]["last_cycle_at"]




def test_director_measures_work_human_intervention_and_time_saved(client):
    run_scenario(client, load(SCENARIOS_DIR / "notification_001.json"))   # va a una persona (escrito que aprobar)
    run_scenario(client, load(SCENARIOS_DIR / "invoice_anomaly_001.json"))  # va a una persona (factura sospechosa)
    run_scenario(client, load(SCENARIOS_DIR / "invoice_normal_001.json"))   # resuelto solo
    board = client.get("/api/briefing").json()["board"]
    intervention = board["intervention"]
    assert intervention["total"] == 3 and intervention["solo"] == 1 and intervention["human"] == 2 and intervention["failed"] == 0
    assert intervention["human_rate"] == round(2 / 3, 3)
    assert board["work"]["cases"] == 2 and board["work"]["anomalies"] == 1 and board["work"]["requests"] >= 2
    saved = board["time_saved"]
    assert saved["minutes"] > 0 and "Estimación" in saved["note"]
    assert {item["key"] for item in saved["assumptions"]} >= {"case_notification", "case_anomaly", "request"}
