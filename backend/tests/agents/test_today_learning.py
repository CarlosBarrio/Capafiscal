"""Director por impacto y aprendizaje de las decisiones humanas."""
from __future__ import annotations

from datetime import date
from datetime import timedelta

from tests.workflows.test_scenarios import TODAY
from tests.workflows.test_scenarios import add_invoice

COMPANY = {"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es"}


def notify(client, external_id: str, **notification):
    response = client.post("/api/events", json={"kind": "notification", "source": "api", "external_id": external_id, "notification": notification})
    response.raise_for_status()
    return response.json()


def test_today_ranks_by_impact_and_says_why(client):
    client.put("/api/company", json=COMPANY)
    notify(client, "req-1", issuer="AEAT", notification_type="REQUERIMIENTO", title="Requerimiento de información", reference="RQ-1",
           summary="Aporte el libro registro de facturas recibidas.", deadline=(TODAY + timedelta(days=2)).isoformat())
    notify(client, "emb-1", issuer="AEAT", notification_type="EMBARGO", title="Diligencia de embargo de créditos", reference="EMB-1",
           summary="Se declaran embargados los créditos. Importe pendiente 2.850,00 €", amount="2850.00", deadline=(TODAY + timedelta(days=20)).isoformat())

    today = client.get("/api/today").json()
    assert today["top"], today
    scores = [item["impact"]["score"] for item in today["top"]]
    assert scores == sorted(scores, reverse=True)
    reasons = {item["title"].split(" · ")[0]: item["impact"]["why"] for item in today["top"]}
    assert any("vence en 2 día(s)" in why and "requerimiento" in why for why in reasons.values())
    assert any("2.850,00 €" in why for why in reasons.values())
    assert "fiscal" in today and "bank" in today


def test_corrections_propose_a_rule_that_only_acts_after_admin_approval(client):
    from app.database import SessionLocal
    from app.learning import correction_hints

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        ids = [add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{index}", total=400, when=TODAY - timedelta(days=40 - index)).id for index in range(3)]
        for invoice_id in ids:
            from app.models import Invoice

            database.get(Invoice, invoice_id).review_status = "PENDING"
        database.commit()

    for index, invoice_id in enumerate(ids[:2]):
        response = client.patch(f"/api/invoices/{invoice_id}", json={"category": "Limpieza y mantenimiento", "invoice_number": f"LS-2026-{index}"})
        assert response.status_code == 200, response.text
    client.post(f"/api/invoices/{ids[2]}/approve")

    stats = client.get("/api/learning").json()
    assert stats["invoice_fields"]["invoice_number"]["corrected"] == 2
    assert stats["corrections_by_error"]["numero"] == 2 and stats["invoices_reviewed"] == 1

    # 1. El patrón se convierte en PROPUESTA, con evidencia y simulación sobre el histórico…
    listing = client.get("/api/learning/rules").json()
    rule = next(item for item in listing["rules"] if item["field"] == "invoice_number")
    assert rule["status"] == "PROPUESTA" and rule["version"] is None and listing["version"] == 0
    assert rule["evidence"]["corrections"] == 2 and len(rule["evidence"]["examples"]) == 2
    assert rule["simulation"]["invoices_affected"] == 3 and rule["simulation"]["would_have_caught"] == 2
    # …pero no cambia nada mientras no se apruebe.
    with SessionLocal() as database:
        assert correction_hints(database, "B11111110") == []

    # 2. Aprobada: entra en vigor con versión.
    approved = client.post(f"/api/learning/rules/{rule['id']}/aprobar", json={"note": "visto"}).json()
    assert approved["status"] == "APROBADA" and approved["version"] == 1
    with SessionLocal() as database:
        hints = correction_hints(database, "B11111110")
    assert any(hint.startswith("invoice_number corregido 2 veces") and "v1" in hint for hint in hints)
    assert client.post(f"/api/learning/rules/{rule['id']}/aprobar", json={}).status_code == 409

    # 3. Retirada: deja de actuar; queda el rastro.
    assert client.post(f"/api/learning/rules/{rule['id']}/retirar", json={}).json()["status"] == "RETIRADA"
    with SessionLocal() as database:
        assert correction_hints(database, "B11111110") == []
    actions = {event["action"] for event in client.get("/api/activity", params={"limit": 50}).json()}
    assert {"learning.rule_aprobada", "learning.rule_retirada"} <= actions


def test_only_admins_decide_learning_rules():
    from app.auth import permitted

    assert permitted("ADMIN", "POST", "/api/learning/rules/3/aprobar")
    for role in ("GESTOR", "REVISOR", "CLIENTE", "LECTURA"):
        assert not permitted(role, "POST", "/api/learning/rules/3/aprobar")
        assert permitted(role, "GET", "/api/learning/rules")


def test_dismissed_alarm_counts_as_false_positive(client):
    from app.agents.detector import run_anomaly_scan
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month in range(1, 7):
            add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number=f"LS-{month}", total=400, when=TODAY - timedelta(days=30 * (7 - month)))
        add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-X", total=2800, when=TODAY - timedelta(days=5))
        database.commit()
        run_anomaly_scan(database, today=TODAY)
        database.commit()
    case = next(item for item in client.get("/api/cases", params={"view": "anomalies"}).json() if item["procedure"] == "IMPORTE_ATIPICO")
    client.post(f"/api/cases/{case['id']}/resolve", json={"dismiss": True, "resolution": "Era la limpieza extraordinaria anual"})
    precision = client.get("/api/learning").json()["anomaly_precision"]
    assert precision["IMPORTE_ATIPICO"]["descartado"] == 1 and precision["IMPORTE_ATIPICO"]["precision"] == 0.0
