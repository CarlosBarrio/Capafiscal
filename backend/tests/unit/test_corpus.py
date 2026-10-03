"""Corpus de evaluación documental (evaluation/datasets/corpus): estructura, verdad, metadatos y evaluador.

No mide el acierto de las reglas (el corpus es conjunto B: no se ajustan reglas con él). Comprueba que el corpus
está bien construido y que el evaluador lo usa igual para los tres motores.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evaluation.core import DOCUMENT_TYPES
from evaluation.core import FIELDS
from evaluation.core import load_dataset
from evaluation.core import run

CORPUS = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "corpus"
META_KEYS = {"synthetic", "document_type", "difficulty", "ambiguous", "multipage", "ocr", "language", "visual_template"}
INVOICE_FIELDS = {"direction", "supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number", "invoice_date", "subtotal", "tax_total", "total"}


@pytest.fixture(scope="module")
def labels():
    return json.loads((CORPUS / "labels.json").read_text(encoding="utf-8"))


def test_every_document_exists_has_metadata_and_ground_truth(labels):
    assert labels["synthetic"] is True and labels["empresa"]["tax_id"].startswith("B00")
    ids = [case["id"] for case in labels["casos"]]
    assert len(ids) == len(set(ids))
    for case in labels["casos"]:
        assert (CORPUS / case["file"]).exists(), case["file"]
        assert META_KEYS <= set(case["meta"]), case["id"]
        assert case["meta"]["synthetic"] is True and case["set"] == "B"
        expected = case["expected"]
        assert set(expected) <= set(FIELDS), case["id"]
        assert expected["document_type"] in DOCUMENT_TYPES and isinstance(expected["is_invoice"], bool)
        assert expected["is_invoice"] == (expected["document_type"] == "factura"), case["id"]
        if expected["is_invoice"]:  # una factura se evalúa entera; lo demás, solo el tipo
            assert INVOICE_FIELDS <= set(expected), case["id"]
        else:
            assert set(expected) == {"is_invoice", "document_type"}, case["id"]


def test_minimum_coverage_by_category_and_dimension(labels):
    cases = labels["casos"]
    folders = Counter(case["file"].split("/")[0] for case in cases)
    assert 6 <= folders["albaranes"] <= 8 and 6 <= folders["presupuestos"] <= 8
    assert 5 <= folders["proformas"] <= 6 and 5 <= folders["pedidos"] <= 6 and 5 <= folders["otros"] <= 8
    assert sum(case["meta"]["ambiguous"] for case in cases) >= 10
    assert sum(case["meta"]["ocr"] for case in cases) >= 8
    assert sum(case["meta"]["multipage"] for case in cases) >= 8
    assert {case["meta"]["visual_template"] for case in cases} >= set("ABCDEFG")
    assert 65 <= len(cases) <= 75


def test_normal_invoices_five_easy_five_medium_five_hard(labels):
    """Facturas normales, sin trampas: verdad completa, importes que cuadran, empresas propias y estilos variados."""
    from decimal import Decimal

    from evaluation.datasets.corpus import generar

    normal = [case for case in labels["casos"] if case["file"].startswith("facturas/")]
    assert Counter(case["meta"]["difficulty"] for case in normal) == {"easy": 5, "medium": 5, "hard": 5}
    assert not any(case["meta"]["ambiguous"] or case["meta"]["ocr"] for case in normal)
    assert len({case["meta"]["visual_template"] for case in normal}) >= 5
    own = {company.nif for company in [*generar.EMISORES_FACTURAS.values(), *generar.CLIENTES_FACTURAS.values()]}
    reused = {company.nif for company in [*generar.PROVEEDORES.values(), *generar.CLIENTES.values()]}
    assert not own & reused
    for case in normal:
        expected = case["expected"]
        assert expected["is_invoice"] is True and expected["document_type"] == "factura"
        parties = {expected["supplier_tax_id"], expected["customer_tax_id"]}
        assert generar.NOSOTROS.nif in parties and parties - {generar.NOSOTROS.nif} <= own, case["id"]
        assert expected["direction"] == ("RECEIVED" if expected["customer_tax_id"] == generar.NOSOTROS.nif else "ISSUED")
        amounts = {key: Decimal(expected.get(key, "0")) for key in ("subtotal", "tax_total", "withholding_total", "total")}
        assert amounts["subtotal"] + amounts["tax_total"] - amounts["withholding_total"] == amounts["total"], case["id"]
    assert sum("withholding_total" in case["expected"] for case in normal) >= 3  # IRPF en algunas
    assert {"RECEIVED", "ISSUED"} <= {case["expected"]["direction"] for case in normal}


def test_fictitious_companies_and_tax_ids(labels):
    from evaluation.datasets.corpus import generar

    assert len(generar.PROVEEDORES) >= 10 and len(generar.CLIENTES) >= 8
    assert len({company.sector for company in generar.PROVEEDORES.values()}) >= 6
    for company in [generar.NOSOTROS, *generar.PROVEEDORES.values(), *generar.CLIENTES.values()]:
        assert company.nif.startswith("B00") and company.email.endswith(".example"), company.name
    for case in labels["casos"]:
        for key in ("supplier_tax_id", "customer_tax_id"):
            if key in case["expected"]:
                assert case["expected"][key].startswith("B00")


def test_multipage_documents_really_have_several_pages(labels):
    import pymupdf

    for case in labels["casos"]:
        pages = len(pymupdf.open(CORPUS / case["file"]))
        assert case["meta"]["multipage"] == (pages > 1) and case["meta"]["pages"] == pages, case["id"]


def test_ocr_documents_have_no_text_layer(labels):
    """Escaneados de verdad: imagen sin capa de texto. Sin Tesseract, el extractor no puede leerlos (se dice, no se finge)."""
    import pymupdf

    for case in labels["casos"]:
        if case["meta"]["ocr"]:
            document = pymupdf.open(CORPUS / case["file"])
            assert all(not page.get_text().strip() for page in document), case["id"]
            assert all(page.get_images() for page in document), case["id"]


def test_titles_are_not_split_by_the_layout(labels):
    """Una maqueta que parte «ALBARÁN Nº» / «12345» en dos líneas falsearía la prueba de títulos."""
    import re

    import pymupdf

    for case in labels["casos"]:
        if case["meta"]["ocr"]:
            continue
        text = pymupdf.open(CORPUS / case["file"])[0].get_text("text", sort=True)
        for line in (line.strip() for line in text.splitlines()):
            assert not re.fullmatch(r"(?i)(albar[aá]n|presupuesto|proforma|pedido|factura)[^\d]* n[ºo°]", line), (case["id"], line)


def test_the_three_engines_get_exactly_the_same_documents(monkeypatch):
    """Mismo corpus para Rules, Claude e Hybrid; sin clave, Claude no se ejecuta y nadie recibe otro conjunto."""
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    dataset = load_dataset(CORPUS)
    dataset.cases = [case for case in dataset.cases if case.id in {"alb_basico_12345", "amb_A_albaran_luego_factura", "pre_88_aceptado"}]
    report = run(dataset, ["reglas", "claude", "hibrido"])
    ids = {engine: [row["id"] for row in data["rows"]] for engine, data in report["engines"].items()}
    assert ids["reglas"] == ids["claude"] == ids["hibrido"] and len(ids["reglas"]) == 3
    assert report["engines"]["claude"]["available"] is False
    rules = report["engines"]["reglas"]
    assert rules["not_evaluated"] == 0 and all(row["evaluated"] for row in rules["rows"])
    assert {"document_type", "ambiguous", "ocr", "multipage", "visual_template"} <= set(rules["by_meta"])
    assert rules["classification"]["tp"] + rules["classification"]["fn"] == 1  # una factura entre los tres
    claude = report["engines"]["claude"]["classification"]
    # Sin clave Claude no responde: «sin respuesta» no es «no es factura» y no le regala verdaderos negativos.
    assert claude["tn"] == 0 and claude["tp"] == 0 and claude["no_answer"] == 3
    assert report["engines"]["hibrido"]["escalation"]["to_claude"]["docs"] == 3


def test_classification_metrics_are_computed_from_truth_and_reading():
    from evaluation.core import classification

    def row(want, got, kind_want, kind_got):
        return {"checks": {"is_invoice": want == got, "document_type": kind_want == kind_got},
                "expected": {"is_invoice": want, "document_type": kind_want}, "got": {"is_invoice": got, "document_type": kind_got}}

    rows = [row(True, True, "factura", "factura"), row(True, False, "factura", "otro"), row(False, True, "presupuesto", "factura"),
            row(False, False, "albaran", "albaran"), row(False, False, "pedido", "otro")]
    result = classification(rows)
    assert (result["tp"], result["fn"], result["fp"], result["tn"]) == (1, 1, 1, 2)
    assert result["precision"] == 0.5 and result["recall"] == 0.5 and result["f1"] == 0.5
    assert result["document_type_confusion"]["presupuesto → factura"] == 1


def test_real_invoices_are_marked_as_not_synthetic_and_drafted_from_their_folder(tmp_path):
    """reales/emitidas y reales/recibidas: el borrador sale con synthetic=false y el sentido que dice la carpeta."""
    from evaluation.core import draft_labels

    folder = tmp_path / "reales"
    (folder / "emitidas").mkdir(parents=True)
    (folder / "recibidas").mkdir()
    (folder / "emitidas" / "f1.txt").write_text("FACTURA 1", encoding="utf-8")
    (folder / "recibidas" / "f2.txt").write_text("FACTURA 2", encoding="utf-8")
    draft = json.loads(draft_labels(folder, company={"name": "Mi empresa", "tax_id": "B00000000"}).read_text(encoding="utf-8"))
    by_file = {case["file"]: case for case in draft["casos"]}
    assert by_file["emitidas/f1.txt"]["expected"]["direction"] == "ISSUED"
    assert by_file["recibidas/f2.txt"]["expected"]["direction"] == "RECEIVED"
    assert all(case["meta"]["synthetic"] is False for case in draft["casos"])

    (folder / "labels.json").write_text(json.dumps({"empresa": {}, "casos": [{"id": "f1", "file": "emitidas/f1.txt", "expected": {"is_invoice": True}}]}),
                                        encoding="utf-8")
    assert load_dataset(folder).cases[0].meta["synthetic"] is False  # sin marca, lo que está en reales/ es real


def test_real_documents_stay_out_of_git():
    import subprocess

    root = Path(__file__).resolve().parents[3]
    for path in ("backend/evaluation/datasets/reales/emitidas/x.pdf", "backend/evaluation/datasets/reales/recibidas/x.pdf",
                 "backend/evaluation/datasets/reales/labels.json"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=root).returncode == 0, path


def test_document_type_is_compared_with_the_same_truth_for_the_three_engines(monkeypatch):
    """Con un Claude simulado (sin API): las tres columnas se miden contra la misma verdad del tipo de documento,
    y salen precisión/recall de ¿es factura?, de la escalada y las operaciones (llamadas, coste)."""
    from app.agents import llm
    from evaluation.core import to_markdown

    dataset = load_dataset(CORPUS)
    chosen = {"alb_basico_12345", "pre_88_aceptado", "fac_facil_limpieza_2026_0091", "otr_recibo_0057"}
    dataset.cases = [case for case in dataset.cases if case.id in chosen]
    by_bytes = {case.path.read_bytes(): case for case in dataset.cases}

    def fake_extract(*, pdf_bytes=None, text=None, company=None, model=None):
        case = by_bytes[pdf_bytes]
        answer = {name: None for name in llm.INVOICE_FIELDS} | {"is_invoice": case.expected["is_invoice"], "document_type": case.expected["document_type"]}
        if case.id == "otr_recibo_0057":  # un error a propósito: Claude lo lee como factura
            answer |= {"is_invoice": True, "document_type": "factura"}
        for name in ("supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number", "invoice_date", "due_date", "subtotal", "tax_total", "total"):
            answer[name] = case.expected.get(name, answer[name])
        assert llm.schema_errors({**answer, **{k: float(v) for k, v in answer.items() if k in llm.INVOICE_AMOUNTS and v}}) == []
        return llm.normalize_invoice(answer), {"model": "simulado", "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.01, "ms": 5}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", fake_extract)
    report = run(dataset, ["reglas", "claude", "hibrido"])
    claude = report["engines"]["claude"]
    assert claude["fields"]["document_type"] == {"ok": 3, "n": 4, "rate": 0.75}
    assert claude["classification"]["fp"] == 1 and claude["classification"]["tp"] == 1 and claude["classification"]["precision"] == 0.5
    assert claude["classification"]["document_type_accuracy"] == 0.75
    assert claude["claude_calls"] == 4 and claude["cost_usd"] == 0.04
    assert claude["escalation"] is None and claude["silent_errors"] is None  # Claude solo no avisa: no se inventa
    for engine in ("reglas", "hibrido"):
        data = report["engines"][engine]
        assert data["fields"]["document_type"]["n"] == 4 and data["escalation"]["to_person"]["docs"] == 4
    assert "to_claude" in report["engines"]["hibrido"]["escalation"]
    markdown = to_markdown(report)
    assert "Acierto del tipo de documento" in markdown and "Calidad de la escalada" in markdown and "Llamadas a Claude" in markdown
    versus = report["engines"]["hibrido"]["versus_rules"]
    assert versus["docs"] == 4 and versus["rescued"] + versus["still_wrong"] == versus["rules_wrong"]
    assert "## Resumen" in markdown and "Híbrido frente a reglas" in markdown and "Escalados a Claude" in markdown


def test_readme_matches_the_corpus_and_the_claude_schema(labels):
    """La documentación dice lo que hay: número de documentos, carpetas y campos que devuelve Claude."""
    from app.agents.llm import DOCUMENT_TYPES as CLAUDE_TYPES

    readme = (CORPUS.parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"{len(labels['casos'])} documentos sintéticos" in readme
    for folder in {case["file"].split("/")[0] for case in labels["casos"]}:
        assert f"`{folder}/`" in readme, folder
    assert "`is_invoice`" in readme and "`document_type`" in readme
    assert all(f"`{kind}`" in readme for kind in CLAUDE_TYPES)
    assert set(CLAUDE_TYPES) == set(DOCUMENT_TYPES)


def test_hybrid_is_compared_with_rules_document_by_document():
    """Rescatados, errores nuevos y errores detectables que pasan a silenciosos, con filas construidas a mano."""
    from evaluation.core import hybrid_vs_rules

    def row(rules_ok, hybrid_ok, rules_flagged, outcome, called=True):
        return {"id": f"{rules_ok}{hybrid_ok}{rules_flagged}{outcome}", "evaluated": True, "perfect": hybrid_ok, "outcome": outcome,
                "rules_perfect": rules_ok, "rules_flagged": rules_flagged, "meta": {"claude_called": called},
                "checks": {"total": hybrid_ok}, "rules_checks": {"total": rules_ok}}

    rows = [row(False, True, True, "con_ia"),               # Claude arregla lo que las reglas fallaban
            row(False, False, True, "error_silencioso"),    # detectable → silencioso: lo peor
            row(False, True, False, "con_ia"),              # silencioso de reglas arreglado
            row(True, False, False, "error_silencioso"),    # error nuevo
            row(True, True, False, "solo_reglas", called=False)]
    result = hybrid_vs_rules(rows)
    assert (result["rules_wrong"], result["rescued"], result["still_wrong"]) == (3, 2, 1)
    assert result["new_errors"] == 1 and result["detectable_to_silent"] == 1
    assert (result["silent_fixed"], result["rules_silent_errors"]) == (1, 1)
    assert (result["fields_fixed"], result["fields_broken"]) == (2, 1) and result["claude_called_on"] == 4
