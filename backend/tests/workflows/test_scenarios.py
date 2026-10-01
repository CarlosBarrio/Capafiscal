"""
Batería de evaluación de agentes: casos reales simulados.

Cada escenario (``scenarios/*.json``) describe:
    entrada → agentes ejecutados → resultado esperado → evidencias esperadas → acción esperada

Para añadir un caso basta con un JSON nuevo; este test lo recorre igual
que haría el sistema en producción (subida del documento, evento de
factura o plazo) y comprueba cada una de las cinco partes.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
TEXTS_DIR = SCENARIOS_DIR / "textos"
SCENARIOS = sorted(SCENARIOS_DIR.glob("*.json"))
TODAY = date.today()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def text(name: str) -> str:
    return (TEXTS_DIR / f"{name}.txt").read_text(encoding="utf-8").replace("{year}", str(TODAY.year))


def setup_company(client) -> None:
    response = client.put(
        "/api/company",
        json={"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es", "city": "Burgos", "address": "C/ Vitoria 42"},
    )
    assert response.status_code == 200, response.text


def add_invoice(database, *, supplier: str, tax_id: str, number: str, total: float, when: date, vat_rate: float = 21, duplicate_of: int | None = None):
    from app.models import Document
    from app.models import Invoice

    document = Document(
        original_filename=f"{number}.pdf", stored_filename=f"{uuid.uuid4().hex}.pdf", sha256=f"{abs(hash((supplier, number, str(when), total))):064d}"[:64],
        extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE",
    )
    database.add(document)
    database.flush()
    gross = Decimal(str(total))
    subtotal = (gross / (1 + Decimal(str(vat_rate)) / 100)).quantize(Decimal("0.01"))
    invoice = Invoice(
        document_id=document.id, supplier_name=supplier, supplier_tax_id=tax_id, direction="RECEIVED",
        invoice_number=number, invoice_date=when, total=gross, subtotal=subtotal, tax_total=gross - subtotal,
        currency="EUR", confidence=95, field_confidences={}, validation_status="VALID", validation_messages=[],
        review_status="APPROVED", duplicate_status="STRONG" if duplicate_of else "NONE", duplicate_of_invoice_id=duplicate_of,
    )
    database.add(invoice)
    database.flush()
    return invoice


def run_scenario(client, scenario: dict) -> tuple[dict | None, dict]:
    """Ejecuta la entrada del escenario y devuelve (expediente, recorrido)."""
    from app.database import SessionLocal

    entry = scenario["entrada"]
    setup_company(client)

    if entry["tipo"] == "notification":
        with SessionLocal() as database:
            for item in entry.get("facturas", []):
                add_invoice(database, supplier=item["supplier"], tax_id=item["tax_id"], number=item["number"], total=item["total"], when=TODAY - timedelta(days=item["days_ago"]))
            database.commit()
        for name in entry.get("previas", []):
            client.post("/api/upload", files={"uploaded_file": (f"{name}.txt", text(name).encode(), "text/plain")})
        response = client.post("/api/upload", files={"uploaded_file": (f"{entry['texto']}.txt", text(entry["texto"]).encode(), "text/plain")})
        assert response.status_code == 201, response.text
        run = client.get("/api/agents/runs", params={"limit": 1}).json()[0]
        case = client.get(f"/api/cases/{run['case_id']}").json() if run["case_id"] else None
        return case, run

    if entry["tipo"] == "invoice":
        history = entry["historial"]
        invoice = entry["factura"]
        with SessionLocal() as database:
            ids = {}
            start = TODAY - timedelta(days=30 * len(history["totals"]))
            for index, total in enumerate(history["totals"]):
                item = add_invoice(database, supplier=history["supplier"], tax_id=history["tax_id"], number=f"E-{index}", total=total, when=start + timedelta(days=30 * index))
                ids[item.invoice_number] = item.id
            new = add_invoice(
                database, supplier=history["supplier"], tax_id=history["tax_id"], number=invoice["number"], total=invoice["total"],
                when=TODAY - timedelta(days=invoice["days_ago"]), vat_rate=invoice.get("vat_rate", 21), duplicate_of=ids.get(invoice.get("duplicate_of")),
            )
            database.commit()
            invoice_id = new.id
        response = client.post("/api/events", json={"kind": "invoice", "source": "test", "invoice_id": invoice_id})
        assert response.status_code == 201, response.text
        body = response.json()
        return body["case"], body["run"]

    if entry["tipo"] == "deadline":
        quarter = (TODAY.month - 1) // 3 + 1
        response = client.post(
            "/api/events",
            json={"kind": "deadline", "source": "test", "model": entry["model"], "year": TODAY.year, "quarter": quarter, "due": (TODAY + timedelta(days=entry["days_to_due"])).isoformat()},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return body["case"], body["run"]

    raise AssertionError(f"Tipo de entrada desconocido: {entry['tipo']}")


@pytest.mark.parametrize("path", SCENARIOS, ids=[path.stem for path in SCENARIOS])
def test_scenario(client, path: Path):
    scenario = load(path)
    case, run = run_scenario(client, scenario)
    expected = scenario["resultado_esperado"]

    # 1) Agentes ejecutados, en orden
    assert [step["agent"] for step in run["steps"]] == scenario["agentes_esperados"]
    assert all(step["status"] == "OK" for step in run["steps"]), [step["summary"] for step in run["steps"] if step["status"] != "OK"]

    # 2) Resultado
    if not expected["expediente"]:
        assert case is None
        assert run["case_id"] is None
        director = run["steps"][-1]
        assert director["agent"] == "director" and director["output"]["review"] is False
        return

    assert case is not None, "Se esperaba un expediente"
    route = case["route"]
    assert route["code"] == scenario["ruta_esperada"]
    if scenario.get("ruta_inicial"):
        assert route["escalated"]["from"] == scenario["ruta_inicial"]
        assert route["chained"], "Se esperaban agentes encadenados por resultados"
    for key in ("kind", "procedure", "status"):
        if key in expected:
            assert case[key] == expected[key], f"{key}: {case[key]} != {expected[key]}"
    if "subject_type" in expected:
        assert case["subject"]["type"] == expected["subject_type"]
    if "level_in" in expected:
        assert case["level"] in expected["level_in"]
    if "antecedente" in expected:
        assert any(expected["antecedente"] in item["title"] for item in case["antecedents"])
    if "insight" in expected:
        assert any(expected["insight"] in item for item in case["facts"]["insights"])

    findings = case["findings"]
    types = {item["tipo"] for item in findings}
    for kind in expected.get("hallazgos", []):
        assert kind in types, f"Falta el hallazgo {kind}: {types}"
    if "riesgo" in expected:
        detector = [item for item in findings if item["agente"] == "detector"]
        assert max(detector, key=lambda item: {"low": 0, "medium": 1, "high": 2}[item["riesgo"]])["riesgo"] == expected["riesgo"]
    # Todo hallazgo sigue la estructura común
    for item in findings:
        assert {"resultado", "por_que", "riesgo", "confianza", "evidencia", "documento_origen", "fecha", "agente", "siguiente"} <= set(item)
        assert 0 <= item["confianza"] <= 1

    # 3) Evidencias: en los pasos de los agentes o en los hallazgos
    labels = [evidence["label"] for step in run["steps"] for evidence in (step["evidence"] or [])]
    labels += [evidence["label"] for item in findings for evidence in item["evidencia"]]
    labels += [item["label"] for item in case.get("documents", [])]
    for wanted in scenario["evidencias_esperadas"]:
        assert any(wanted in label for label in labels), f"Falta evidencia «{wanted}» en {labels}"

    # 4) Acción esperada (lo que se propone al humano)
    if scenario["accion_esperada"]:
        actions = [item["label"] for item in case["actions"]]
        assert any(scenario["accion_esperada"] in label for label in actions), f"Falta la acción «{scenario['accion_esperada']}» en {actions}"

    # 5) Humano en el bucle: nada se ha enviado ni presentado sin aprobación
    assert case["status"] in {"WAITING_HUMAN", "WAITING_DOCS"}
    outbox = client.get("/api/outbox").json()["messages"]
    assert all(message["status"] == "DRAFT" for message in outbox)
