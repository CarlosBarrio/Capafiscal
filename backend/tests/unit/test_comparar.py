"""Comparación reglas / Claude / híbrido con un Claude simulado (sin red ni clave real)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from evaluation import casos
from evaluation import comparar

CASES = ("B18", "B19", "B05")  # rectificativa, retención, embargo (notificación)


def small_bank(tmp_path: Path) -> Path:
    folder = tmp_path / "banco"
    folder.mkdir()
    for case_id in CASES:
        shutil.copytree(casos.DATASETS / "b_sintetico" / case_id, folder / case_id)
    (folder / "indice.json").write_text(json.dumps({"conjunto": "B", "casos": [{"case_id": case_id} for case_id in CASES]}), encoding="utf-8")
    return folder


def fake_claude(*, system, content, effort, max_tokens, output_format=None, model=None):
    """Lee siempre la misma factura, con un NIF que no está en el documento (las reglas deben descartarlo)."""
    from app.agents import llm

    meta = {"model": "simulado", "served_by": "simulado", "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.01, "fallback": None}
    if system != llm.SYSTEM_INVOICE:
        return None, {**meta, "fallback": "simulado"}
    fields = {name: "" for name in llm.INVOICE_FIELDS}
    fields.update(supplier_tax_id="B99999997", category="Otros gastos")
    return json.dumps(fields), meta


def test_three_engines_with_a_simulated_claude(tmp_path, monkeypatch):
    from app.agents import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "simulada")
    monkeypatch.setattr(llm, "_complete", fake_claude)
    report = comparar.run(str(small_bank(tmp_path)), engines=["reglas", "claude", "hibrido"])

    metrics = report["analysis"]["metrics"]
    documents = 4  # B18 (2) + B19 (1) + el PDF del embargo: forzado, Claude lee todo documento
    assert metrics["claude"]["calls_by_purpose"]["factura"] == documents
    assert metrics["hibrido"]["calls_by_purpose"].get("factura", 0) < documents  # solo donde las reglas dudan
    assert metrics["reglas"]["calls"] == 0 and metrics["reglas"]["cost_usd"] == 0
    assert metrics["claude"]["cost_usd"] >= 0.03

    # Un NIF inventado por Claude no entra: las reglas lo validan contra el documento.
    versus = report["analysis"]["versus"]["claude"]
    assert not [item for item in versus["broken"] if item["check"] == "invoice.supplier_tax_id"]
    assert versus["documents_with_ai"] == documents

    markdown = comparar.to_markdown(report)
    assert "Qué rompe" in markdown and "CLAUDE (siempre)" in markdown and "Precisión de la escalada" in markdown


def test_without_key_only_rules_run(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = comparar.run(str(small_bank(tmp_path)), engines=["reglas", "hibrido"])
    assert report["engines"]["hibrido"]["available"] is False
    assert "no ejecutado" in comparar.to_markdown(report)
