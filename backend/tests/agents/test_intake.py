"""Entrada común: idempotencia, estados de procesamiento y reanudación."""
from __future__ import annotations

from tests.workflows.test_scenarios import SCENARIOS_DIR
from tests.workflows.test_scenarios import load
from tests.workflows.test_scenarios import run_scenario

DEHU_NOTIFICATION = {
    "issuer": "AEAT",
    "notification_type": "REQUERIMIENTO",
    "title": "Requerimiento de información",
    "reference": "DEHU-0042",
    "summary": "Aporte el libro registro de facturas recibidas del 2T.",
}


def setup_company(client):
    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})


def test_same_external_event_is_processed_once(client):
    setup_company(client)
    first = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-0042", "notification": DEHU_NOTIFICATION})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["duplicate"] is False and body["event"]["status"] == "COMPLETED"
    case_id = body["case"]["id"]

    again = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-0042", "notification": DEHU_NOTIFICATION})
    assert again.status_code == 200
    assert again.json()["duplicate"] is True and again.json()["case"]["id"] == case_id
    assert len(client.get("/api/cases").json()) == 1
    runs = [run for run in client.get("/api/agents/runs").json() if run["pipeline"] == "notification"]
    assert len(runs) == 1

    events = client.get("/api/events").json()
    assert events["events"][0]["duplicates"] == 1 and events["counts"]["COMPLETED"] == 1

    # La misma fuente con otro identificador sí es un evento nuevo
    other = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-0043", "notification": {**DEHU_NOTIFICATION, "reference": "DEHU-0043"}})
    assert other.status_code == 201 and other.json()["case"]["id"] != case_id


def test_changed_content_is_reprocessed_not_duplicated(client):
    setup_company(client)
    client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-7", "notification": DEHU_NOTIFICATION})
    changed = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-7", "notification": {**DEHU_NOTIFICATION, "summary": "Aporte también los extractos bancarios."}})
    assert changed.status_code == 201 and changed.json()["duplicate"] is False
    assert changed.json()["event"]["attempts"] == 2
    assert len(client.get("/api/cases").json()) == 1


def test_failed_agent_leaves_case_waiting_for_a_human_and_can_resume(client, monkeypatch):
    from app.agents.orchestrator import AGENTS

    setup_company(client)

    def broken(ctx):
        raise RuntimeError("timeout leyendo el borrador del 303")

    monkeypatch.setattr(AGENTS["fiscal"], "run", broken)
    body = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-9", "notification": DEHU_NOTIFICATION}).json()
    event = body["event"]
    assert event["status"] == "NEEDS_HUMAN" and event["failed_agent"] == "fiscal"
    assert "timeout" in event["error"]
    case = client.get(f"/api/cases/{body['case']['id']}").json()
    assert case["facts"]["processing"]["failed_agent"] == "fiscal"
    assert any("no pudo terminar" in item["title"] for item in case["events"])
    # El resto de agentes siguió trabajando: el expediente no se pierde
    assert [step["agent"] for step in case["run"]["steps"]][-1] == "director"

    monkeypatch.undo()
    resumed = client.post(f"/api/events/{event['id']}/retry").json()
    assert resumed["event"]["status"] == "COMPLETED" and resumed["event"]["attempts"] == 2
    assert "processing" not in resumed["case"]["facts"]
    assert len(client.get("/api/cases").json()) == 1


def test_total_failure_is_recorded_and_can_be_retried(client, monkeypatch):
    from app.agents import intake

    setup_company(client)
    real_dispatch = intake.dispatch

    def crash(*args, **kwargs):
        raise ConnectionError("la base de datos de la sede no responde")

    monkeypatch.setattr(intake, "dispatch", crash)
    body = client.post("/api/events", json={"kind": "notification", "source": "dehu", "external_id": "DEHU-10", "notification": DEHU_NOTIFICATION}).json()
    assert body["event"]["status"] == "FAILED" and "no responde" in body["event"]["error"] and body["case"] is None
    assert client.get("/api/cases").json() == []

    monkeypatch.setattr(intake, "dispatch", real_dispatch)
    retried = client.post(f"/api/events/{body['event']['id']}/retry").json()
    assert retried["event"]["status"] == "COMPLETED" and retried["case"]["route"]["code"] == "requerimiento"
    assert client.post("/api/events/9999/retry").status_code == 404


def test_uploads_are_registered_as_events(client):
    run_scenario(client, load(SCENARIOS_DIR / "notification_001.json"))
    events = client.get("/api/events").json()["events"]
    upload = next(item for item in events if item["source"] == "upload")
    assert upload["kind"] == "document" and upload["status"] == "COMPLETED" and upload["case_id"]
