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
