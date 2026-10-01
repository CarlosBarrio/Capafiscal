"""Banco de evaluación: métricas por motor, conjuntos A/B/C y preparación de etiquetas."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from evaluation.core import draft_labels
from evaluation.core import load_dataset
from evaluation.core import merge_reviewed
from evaluation.core import run
from evaluation.core import to_markdown

SYNTHETIC = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "sinteticas"


def copy_dataset(tmp_path: Path) -> Path:
    folder = tmp_path / "ds"
    shutil.copytree(SYNTHETIC, folder)
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    for index, case in enumerate(labels["casos"]):
        case["set"] = "AB"[index % 2]  # mitad desarrollo, mitad evaluación
    (folder / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    return folder


def test_report_without_key_says_claude_was_not_run(tmp_path):
    report = run(load_dataset(copy_dataset(tmp_path)), ["reglas", "claude", "hibrido"])
    assert report["engines"]["claude"]["available"] is False
    markdown = to_markdown(report)
    assert "Claude no se ha ejecutado" in markdown and "no ejecutado" in markdown
    assert "| Proveedor |" in markdown and "| Clasificación |" not in markdown  # sin etiqueta de clasificación no hay fila


def test_sets_filter_cases(tmp_path):
    dataset = load_dataset(copy_dataset(tmp_path))
    report = run(dataset, ["reglas"], sets={"B"})
    assert report["cases"] == 2 and report["sets"] == {"B": 2}
    assert "conjunto A se usó para ajustar" not in to_markdown(report)


def test_claude_and_hybrid_metrics_with_a_simulated_model(tmp_path, monkeypatch):
    from app.agents import llm

    dataset = load_dataset(copy_dataset(tmp_path))
    truth = {case.path.name: case.expected for case in dataset.cases}

    def fake_extract(*, pdf_bytes=None, text=None, company=None, model=None):
        # Un modelo simulado que acierta todo y cobra 2.000 tokens de entrada y 200 de salida.
        name = next(name for name in truth if (tmp_path / "ds" / name).read_bytes() == pdf_bytes)
        expected = truth[name]
        data = {key: expected.get(key, "") for key in llm.INVOICE_FIELDS}
        data["customer_tax_id"] = expected["customer_tax_id"]
        meta = {"model": "claude-opus-5-5", "input_tokens": 2000, "output_tokens": 200, "cost_usd": llm.estimate_cost("claude-opus-5-5", 2000, 200)}
        return data, meta

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", fake_extract)
    report = run(dataset, ["reglas", "claude", "hibrido"])
    claude = report["engines"]["claude"]
    assert claude["available"] and claude["field_accuracy"] == 1.0
    assert claude["claude_share"] == 1.0 and claude["cost_per_doc_usd"] == 0.012
    assert claude["cost_per_1000_docs_usd"] == 12.0
    hybrid = report["engines"]["hibrido"]
    # Las reglas ya leen bien las réplicas: el híbrido no necesita llamar a Claude
    assert hybrid["claude_share"] == 0 and hybrid["field_accuracy"] == 1.0
    markdown = to_markdown(report)
    assert "Coste por 1.000 documentos" in markdown and "12.00 $" in markdown


def test_draft_and_merge_labels(tmp_path):
    folder = tmp_path / "nuevos"
    folder.mkdir()
    shutil.copy(SYNTHETIC / "tienda_ticket.pdf", folder / "nueva.pdf")
    draft = draft_labels(folder, company={"name": "Estudio Ejemplo", "tax_id": "B49123458"})
    data = json.loads(draft.read_text(encoding="utf-8"))
    case = data["casos"][0]
    assert case["set"] == "B" and case["revisado"] is False and case["expected"]["total"] == ""  # vacío: sin sesgo

    prefilled = json.loads(draft_labels(folder, prefill=True).read_text(encoding="utf-8"))["casos"][0]
    assert prefilled["expected"]["total"] == "80.00"

    prefilled["revisado"] = True
    prefilled["expected"]["category"] = "Software e informática"
    draft.write_text(json.dumps({"empresa": data["empresa"], "casos": [prefilled]}), encoding="utf-8")
    assert merge_reviewed(folder) == (1, 0)
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    assert labels["casos"][0]["expected"]["category"] == "Software e informática" and "revisado" not in labels["casos"][0]
    assert load_dataset(folder).cases[0].set == "B"


def test_outcomes_error_matrix_and_known_errors(tmp_path):
    folder = copy_dataset(tmp_path)
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    tienda = next(case for case in labels["casos"] if case["id"] == "tienda_ticket")
    tienda["expected"]["category"] = "Software e informática"  # las reglas dicen «Servicios profesionales»
    tienda["errores_conocidos"] = [{"campo": "category", "nota": "error conocido de A"}]
    (folder / "labels.json").write_text(json.dumps(labels), encoding="utf-8")

    report = run(load_dataset(folder), ["reglas"])
    rules = report["engines"]["reglas"]
    assert rules["outcomes"] == {"solo_reglas": 4, "con_ia": 0, "humano": 0, "error_silencioso": 1}
    assert rules["error_matrix"] == {"error_clasificacion": 1}
    assert rules["confusion"] == {"Software e informática → Servicios profesionales": 1}
    assert set(rules["by_set"]) == {"A", "B"}
    markdown = to_markdown(report)
    assert "Errores silenciosos" in markdown and "sigue fallando" in markdown
    assert "## Por conjunto (reglas)" in markdown and "## Matriz de errores" in markdown


def test_routing_value_when_rules_are_not_sure(tmp_path, monkeypatch):
    """Si las reglas dudan, el híbrido llama a Claude; el informe dice cuánto aportó y cuánto costó."""
    from app import interpretation
    from app.agents import llm

    dataset = load_dataset(copy_dataset(tmp_path))
    truth = {case.path.name: case.expected for case in dataset.cases}
    real_needs_help = interpretation.needs_help

    def doubtful(result, company_ids):
        # Antes de pasar por Claude, las reglas «dudan» de todo; después, se evalúa de verdad.
        return ["prueba: reglas no concluyentes"] if "interpretation" not in result else real_needs_help(result, company_ids)

    def fake_extract(*, pdf_bytes=None, text=None, company=None, model=None):
        name = next(name for name in truth if (tmp_path / "ds" / name).read_bytes() == pdf_bytes)
        data = {key: truth[name].get(key, "") for key in llm.INVOICE_FIELDS}
        return data, {"model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 100, "cost_usd": llm.estimate_cost("claude-opus-5-5", 1000, 100)}

    monkeypatch.setattr(interpretation, "needs_help", doubtful)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", fake_extract)
    report = run(dataset, ["reglas", "hibrido"])
    hybrid = report["engines"]["hibrido"]
    assert hybrid["outcomes"]["con_ia"] == 5 and hybrid["claude_share"] == 1.0
    from evaluation.core import routing_value

    routing = routing_value(report)
    assert routing["claude_called"] == 5 and routing["fields_broken"] == 0 and routing["cost_usd"] == 0.03
    assert "¿Cuándo merece la pena llamar a Claude?" in to_markdown(report)
