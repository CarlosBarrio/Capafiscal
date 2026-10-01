"""Routing de Claude: la política sale de los datos y se respeta al leer documentos."""
from __future__ import annotations

from app.routing import allowed_reasons
from app.routing import derive_policy
from app.routing import reason_key


def report(details):
    return {"dataset": "b", "date": "2026-10-01", "analysis": {"versus": {"hibrido": {"routed_detail": details, "cost_per_fix": 0.01}}}}


def test_reason_keys_group_similar_doubts():
    assert reason_key("falta supplier_tax_id") == "falta_supplier_tax_id"
    assert reason_key("los importes no cuadran") == "importes"
    assert reason_key("invoice_number corregido 3 veces en este proveedor") == "aprendizaje"


def test_policy_turns_claude_off_where_it_never_helps():
    details = (
        [{"reasons": ["categoría sin determinar"], "verdict": "igual"}] * 4
        + [{"reasons": ["los importes no cuadran"], "verdict": "mejora"}] * 3 + [{"reasons": ["los importes no cuadran"], "verdict": "empeora"}]
        + [{"reasons": ["falta total"], "verdict": "empeora"}] * 2 + [{"reasons": ["falta total"], "verdict": "mejora"}] * 2
        + [{"reasons": ["número de factura sospechoso"], "verdict": "mejora"}]
    )
    policy = derive_policy(report(details))["reasons"]
    assert policy["categoria"]["use_claude"] is False  # nunca mejoró
    assert policy["importes"]["use_claude"] is True  # mejora 3 de 4
    assert policy["falta_total"]["use_claude"] is False  # empeora tanto como mejora
    assert policy["numero"]["use_claude"] is True  # pocos datos: se mantiene

    rules = {"reasons": policy}
    assert allowed_reasons(["categoría sin determinar"], rules) == []
    assert allowed_reasons(["categoría sin determinar", "los importes no cuadran"], rules) == ["los importes no cuadran"]


def test_human_corrections_to_claude_count_against_it():
    details = [{"reasons": ["los importes no cuadran"], "verdict": "mejora"}] * 3
    policy = derive_policy(report(details), corrections={"importes": 3})["reasons"]
    assert policy["importes"]["use_claude"] is False


def test_refine_skips_claude_when_the_policy_says_so(tmp_path, monkeypatch):
    from app import interpretation
    from app import routing

    calls = []
    monkeypatch.setattr(routing, "load_policy", lambda: {"reasons": {"categoria": {"use_claude": False}}})
    monkeypatch.setattr("app.agents.llm.available", lambda: True)
    monkeypatch.setattr("app.agents.llm.extract_invoice", lambda **kwargs: calls.append(kwargs) or (None, {"fallback": "no debería llamarse"}))
    monkeypatch.setattr(interpretation, "needs_help", lambda result, ids: ["categoría sin determinar"])
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = interpretation.refine(pdf, {"fields": {}, "raw_text": ""})
    assert not calls and result["interpretation"]["skipped_by_policy"] is True
