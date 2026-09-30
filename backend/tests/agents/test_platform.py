"""Plataforma de agentes: contratos, motor del Detector, rutas, encadenado y humano en el bucle."""
from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

from app.agents.base import Finding
from tests.workflows.test_scenarios import SCENARIOS_DIR
from tests.workflows.test_scenarios import add_invoice
from tests.workflows.test_scenarios import load
from tests.workflows.test_scenarios import run_scenario

SCAN_DAY = date(2026, 9, 15)


# ---------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------


def test_every_agent_declares_its_contract(client):
    from app.agents.orchestrator import AGENTS
    from app.agents.orchestrator import INTAKE
    from app.agents.orchestrator import ROUTES

    for agent in AGENTS.values():
        contract = agent.contract()
        assert contract["input"] and contract["output"], agent.code
        assert contract["handles"], agent.code
    for route in ROUTES.values():
        for code in (*INTAKE, *route["steps"]):
            assert code in AGENTS
            assert route["event"] in AGENTS[code].handles, f"{code} no atiende eventos {route['event']}"

    data = client.get("/api/agents/routes").json()
    assert {item["code"] for item in data["routes"]} == {"requerimiento", "embargo", "documento_normal", "factura_sospechosa", "plazo"}
    vigilante = next(item for item in data["contracts"] if item["code"] == "vigilante")
    assert "urgencia" in " ".join(vigilante["output"]) or "plazo/urgencia" in vigilante["output"]
    overview = client.get("/api/agents").json()
    assert overview["routes"] and all(item["contract"] for item in overview["agents"])


def test_finding_has_common_structure():
    finding = Finding(agente="detector", tipo="IMPORTE_ATIPICO", resultado="Importe 386 % superior", por_que="Mediana 105 €", riesgo="high", confianza=0.9, fecha="2026-09-01")
    data = finding.to_dict()
    assert set(data) >= {"resultado", "confianza", "evidencia", "documento_origen", "fecha", "agente", "por_que", "riesgo", "siguiente", "datos"}
    assert data["riesgo_label"] == "alto"


# ---------------------------------------------------------------------
# Motor del Detector: cada tipo con su salida estándar
# ---------------------------------------------------------------------


def seed_engine_data():
    from app.database import SessionLocal
    from app.models import BankTransaction

    def monthly(database, supplier, tax_id, months, totals, *, rate=21, day=5, year=2026):
        for index, (month, total) in enumerate(zip(months, totals)):
            add_invoice(database, supplier=supplier, tax_id=tax_id, number=f"{tax_id[-3:]}-{index}", total=total, when=date(year, month, day), vat_rate=rate)

    with SessionLocal() as database:
        # Importe atípico: ~100 € al mes y en agosto 900 €
        monthly(database, "ATIPICO S.A.", "A11111111", [3, 4, 5, 6, 7, 8], [100, 110, 95, 105, 98, 900])
        # Duplicado: mismo importe dos veces en agosto
        add_invoice(database, supplier="DUP S.L.", tax_id="B22222222", number="D1", total=450, when=date(2026, 8, 1))
        add_invoice(database, supplier="DUP S.L.", tax_id="B22222222", number="D2", total=450, when=date(2026, 8, 20))
        # IVA atípico: siempre al 21 % y una al 10 %
        monthly(database, "IVA S.L.", "B33333333", [4, 5, 6, 7], [200, 210, 190, 205])
        add_invoice(database, supplier="IVA S.L.", tax_id="B33333333", number="IVA-X", total=220, when=date(2026, 8, 6), vat_rate=10)
        # Proveedor nuevo con importe alto
        add_invoice(database, supplier="NUEVO S.L.", tax_id="B44444444", number="N1", total=5000, when=date(2026, 9, 1))
        # Cambio de comportamiento: mensual y de pronto tres en diez días
        monthly(database, "RARO S.L.", "B55555555", [3, 4, 5, 6, 7], [300, 320, 310, 305, 315])
        for number, day, total in (("R-A", date(2026, 8, 25), 120), ("R-B", date(2026, 8, 28), 140), ("R-C", date(2026, 9, 2), 160)):
            add_invoice(database, supplier="RARO S.L.", tax_id="B55555555", number=number, total=total, when=day)
        # Factura que falta: mayo, junio y julio, pero no agosto
        monthly(database, "MENSUAL S.L.", "B66666666", [5, 6, 7], [80, 82, 81])
        # Patrón interrumpido: febrero a junio y luego nada
        monthly(database, "CORTADO S.L.", "B77777777", [2, 3, 4, 5, 6], [60, 61, 62, 63, 64])
        # Factura sin pago: vencida en julio, sin pago en el banco
        invoice = add_invoice(database, supplier="IMPAGO S.L.", tax_id="B88888888", number="I-1", total=800, when=date(2026, 6, 1))
        invoice.due_date = date(2026, 7, 1)
        # Banco: un pago y un cobro sin factura
        for index, (when, amount, text) in enumerate(((date(2026, 8, 20), -1500, "TRANSFERENCIA A PROVEEDOR X"), (date(2026, 8, 25), 2000, "INGRESO CLIENTE Y"), (date(2026, 9, 10), -5, "COMISION"))):
            database.add(BankTransaction(booking_date=when, description=text, amount=Decimal(amount), fingerprint=f"fp-{index}", match_status="UNMATCHED"))
        database.commit()


def test_detector_engine_covers_every_anomaly_type(client):
    from app.agents.detector import ANOMALY_TYPES
    from app.agents.detector import scan
    from app.database import SessionLocal

    seed_engine_data()
    with SessionLocal() as database:
        findings = scan(database, SCAN_DAY)

    types = {item["procedure"] for item in findings}
    expected = {
        "IMPORTE_ATIPICO", "POSIBLE_DUPLICADO", "IVA_INUSUAL", "PAGO_SIN_FACTURA", "COBRO_SIN_FACTURA",
        "FACTURA_SIN_PAGO", "PROVEEDOR_NUEVO", "CAMBIO_COMPORTAMIENTO", "FACTURA_FALTA", "PATRON_INTERRUMPIDO",
    }
    assert expected <= types, expected - types

    for item in findings:
        finding = item["finding"]
        # qué detectó, por qué, con qué datos, nivel de riesgo y qué debería pasar después
        assert finding["resultado"] and finding["por_que"] and isinstance(finding["datos"], dict)
        assert finding["riesgo"] in {"low", "medium", "high"}
        assert finding["siguiente"] == ANOMALY_TYPES[item["procedure"]]["next"] or item["procedure"] == "PATRON_INTERRUMPIDO"
        assert finding["agente"] == "detector"

    atypical = next(item for item in findings if item["procedure"] == "IMPORTE_ATIPICO")
    assert atypical["facts"]["percent"] > 700 and atypical["severity"] == "high"
    assert "A11111111" not in {item["facts"].get("supplier_key") for item in findings if item["procedure"] == "FACTURA_FALTA"}
    falta = next(item for item in findings if item["procedure"] == "FACTURA_FALTA")
    assert "MENSUAL" in falta["title"] and "agosto" in falta["title"]
    cortado = next(item for item in findings if item["procedure"] == "PATRON_INTERRUMPIDO")
    assert cortado["facts"]["months_missing"] == 2
    # Una comisión de 5 € no es una anomalía
    assert not any("COMISION" in item["detail"] for item in findings)


def test_fixed_monthly_fee_is_not_a_duplicate(client):
    from app.agents.detector import scan
    from app.database import SessionLocal

    with SessionLocal() as database:
        for month in (4, 5, 6, 7, 8):
            add_invoice(database, supplier="ALQUILER S.L.", tax_id="B99999999", number=f"AL-{month}", total=900, when=date(2026, month, 1))
        database.commit()
        findings = scan(database, SCAN_DAY)
    assert not [item for item in findings if item["procedure"] == "POSIBLE_DUPLICADO"]


# ---------------------------------------------------------------------
# Humano en el bucle: aprobar, rechazar, modificar… y aprender
# ---------------------------------------------------------------------


def test_human_decisions_close_the_loop(client):
    case, _run = run_scenario(client, load(SCENARIOS_DIR / "invoice_anomaly_001.json"))

    # Modificar: la persona añade su propia acción
    changed = client.patch(f"/api/cases/{case['id']}", json={"add_action": "Llamar a Endesa para revisar la lectura"}).json()
    assert changed["actions"][-1]["label"] == "Llamar a Endesa para revisar la lectura"
    assert changed["facts"]["human_decision"]["decision"] == "modified"

    # Reprocesar no pisa lo que ha añadido la persona
    again = client.post(f"/api/cases/{case['id']}/rerun").json()
    assert any(item["label"] == "Llamar a Endesa para revisar la lectura" for item in again["actions"])
    assert len(client.get("/api/cases", params={"view": "anomalies"}).json()) == 1

    # Rechazar: era correcto
    client.post(f"/api/cases/{case['id']}/resolve", json={"dismiss": True, "resolution": "Regularización anual de la lectura: es correcto."})

    # Una factura parecida del mismo proveedor: el sistema recuerda la decisión
    from app.database import SessionLocal

    with SessionLocal() as database:
        invoice = add_invoice(database, supplier="ENDESA ENERGIA S.A.", tax_id="A81948077", number="FAC-2001", total=540, when=date.today() - timedelta(days=1))
        database.commit()
        invoice_id = invoice.id
    body = client.post("/api/events", json={"kind": "invoice", "invoice_id": invoice_id}).json()
    second = body["case"]
    assert second is not None
    memory = [item for item in second["findings"] if item["agente"] == "memoria"]
    assert memory and "Regularización anual" in memory[0]["por_que"]
    detector = [item for item in second["findings"] if item["agente"] == "detector" and item["tipo"] == "IMPORTE_ATIPICO"]
    assert detector and detector[0]["riesgo"] == "medium"  # rebajado por la decisión anterior
    assert "lo diste por correcto" in detector[0]["por_que"]


def test_approving_an_anomaly_accepts_the_recommendation(client):
    case, _run = run_scenario(client, load(SCENARIOS_DIR / "invoice_duplicate_001.json"))
    approved = client.post(f"/api/cases/{case['id']}/approve").json()
    assert approved["status"] == "RESOLVED"
    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["facts"]["human_decision"]["decision"] == "approved"
    assert "Retener el pago" in detail["facts"]["human_decision"]["note"]


# ---------------------------------------------------------------------
# Entradas: subida de facturas, plazos programados y fuentes externas
# ---------------------------------------------------------------------


def test_uploaded_invoice_goes_through_the_detector(client, sample_pdfs):
    from tests.conftest import upload

    upload(client, next(iter(sample_pdfs.values())))
    runs = client.get("/api/agents/runs").json()
    invoice_runs = [run for run in runs if run["pipeline"] == "invoice"]
    assert invoice_runs, "Una factura subida debe pasar por el orquestador"
    agents = [step["agent"] for step in invoice_runs[0]["steps"]]
    assert agents[:2] == ["vigilante", "expedientes"] and "detector" in agents and agents[-1] == "director"


def test_scan_does_not_duplicate_an_invoice_case(client):
    case, _run = run_scenario(client, load(SCENARIOS_DIR / "invoice_anomaly_001.json"))
    client.post("/api/agents/anomalies/scan")
    anomalies = client.get("/api/cases", params={"view": "anomalies"}).json()
    assert [item["id"] for item in anomalies if item["procedure"] in {"IMPORTE_ATIPICO", "FACTURA_SOSPECHOSA"}] == [case["id"]]
    # Y el barrido no lo cierra por no haberlo abierto él
    assert client.get(f"/api/cases/{case['id']}").json()["status"] == "WAITING_HUMAN"


def test_deadline_watch_opens_a_case_once(client):
    from app.agents.orchestrator import watch_deadlines
    from app.database import SessionLocal
    from app.tax_service import quarterly_due_date

    due = quarterly_due_date(2026, 3)
    today = due - timedelta(days=10)
    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})
    with SessionLocal() as database:
        add_invoice(database, supplier="PROVEEDOR S.L.", tax_id="B12121212", number="P-1", total=121, when=date(2026, 8, 10))
        database.commit()
        cases = watch_deadlines(database, today=today)
        database.commit()
        assert any(case.title.startswith("Modelo 303 · 3T 2026") for case in cases)
        assert watch_deadlines(database, today=today) == []

    listed = client.get("/api/cases").json()
    deadline = next(item for item in listed if item["kind"] == "DEADLINE" and item["procedure"] == "MODELO_303")
    detail = client.get(f"/api/cases/{deadline['id']}").json()
    assert detail["route"]["code"] == "plazo"
    assert detail["deadline"] == due.isoformat()


def test_external_source_event_uses_the_same_chain(client):
    """Una fuente externa (p. ej. DEHú) entrega una notificación: misma cadena que una subida."""
    client.put("/api/company", json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD"})
    body = client.post(
        "/api/events",
        json={
            "kind": "notification",
            "source": "dehu",
            "notification": {"issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento de información", "reference": "DEHU-0001", "summary": "Aporte el libro registro de facturas recibidas del 2T."},
        },
    )
    assert body.status_code == 201, body.text
    case = body.json()["case"]
    assert case["route"]["code"] == "requerimiento" and case["route"]["source"] == "dehu"
    assert [step["agent"] for step in body.json()["run"]["steps"]][:2] == ["vigilante", "expedientes"]
    assert client.post("/api/events", json={"kind": "notification", "notification": {"issuer": "NOPE"}}).status_code == 422
