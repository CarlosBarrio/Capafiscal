"""Catálogo sintético de casos (presupuestos, albaranes, IRPF, rectificativas, OCR imperfecto, importes complejos…).

Si las reglas dejan de leer bien uno de estos casos, es una regresión. El único que depende del entorno es el
escaneado sin texto: necesita Tesseract (la imagen Docker lo incluye) y se omite donde no está instalado.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evaluation.core import load_dataset
from evaluation.core import run

CATALOG = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "catalogo"
NEEDS_OCR = "necesita Tesseract"


@pytest.fixture(scope="module")
def rules_report():
    return run(load_dataset(CATALOG), ["reglas"])["engines"]["reglas"]


def test_every_case_is_read_correctly_by_the_rules(rules_report):
    failures = {row["id"]: {name: (row["expected"][name], row["got"][name]) for name, ok in row["checks"].items() if not ok}
                for row in rules_report["rows"] if not row["perfect"]}
    if shutil.which("tesseract") is None:
        failures = {key: value for key, value in failures.items()
                    if NEEDS_OCR not in next(row["tags"] for row in rules_report["rows"] if row["id"] == key)}
    assert not failures, failures


def test_quotes_delivery_notes_and_official_letters_are_not_invoices(rules_report):
    rows = {row["id"]: row for row in rules_report["rows"]}
    for case in ("ambiguo_presupuesto", "ambiguo_albaran", "notificacion_requerimiento", "notificacion_providencia",
                 "administrativo_certificado", "administrativo_justificante"):
        assert rows[case]["got"]["is_invoice"] is False, case


def test_withholding_and_credit_notes_keep_their_sign(rules_report):
    rows = {row["id"]: row for row in rules_report["rows"]}
    assert rows["irpf_asesoria"]["got"]["withholding_total"] in ("52.50", 52.5) or float(rows["irpf_asesoria"]["got"]["withholding_total"]) == 52.5
    assert float(rows["rectificativa_precio"]["got"]["total"]) < 0 and float(rows["rectificativa_devolucion"]["got"]["total"]) < 0


def test_non_invoice_title_only_looks_at_the_first_title():
    from app.extractor import non_invoice_title

    assert non_invoice_title("PRESUPUESTO Nº P-1\nIVA 21 %\nTotal 10,00") == "presupuesto"
    assert non_invoice_title("ALBARÁN DE ENTREGA Nº 5") == "albarán"
    assert non_invoice_title("FACTURA PROFORMA 12") == "proforma"
    # Una factura que cita un albarán, un presupuesto o un pedido sigue siendo factura.
    assert non_invoice_title("FACTURA Nº 12\nSegún albarán 33 y presupuesto aceptado") is None
    assert non_invoice_title("Pedido: 4500123\nNº factura 77") is None
    assert non_invoice_title("Factura rectificativa R-1") is None


def test_ocr_label_repair_only_touches_amount_labels():
    from app.extraction_rules import repair_ocr_labels

    repaired = repair_ocr_labels("Ba5e imponible 238,00\nlVA 21 % 4 9,98\nT0TAL FACTURA 287,98\n1 2,50 lápiz\nlVAN Pérez · VIVA")
    assert repaired.splitlines() == ["Base imponible 238,00", "IVA 21 % 49,98", "TOTAL FACTURA 287,98", "1 2,50 lápiz", "lVAN Pérez · VIVA"]


def test_company_names_are_not_rejected_for_containing_a_label_inside_a_word():
    from app.extractor import looks_like_company_name

    for name in ("ASESORÍA NÚMEROS CLAROS S.L.", "PACÍFICO SUMINISTROS S.L.", "UNIFORMES NORTE S.L.", "CALLEJA HERMANOS S.A."):
        assert looks_like_company_name(name), name
    for label in ("Número: 12", "Fecha factura", "www.ejemplo.com", "Calle Mayor 3"):
        assert not looks_like_company_name(label), label


def test_claude_is_not_scored_on_fields_it_does_not_produce(tmp_path, monkeypatch):
    """Claude no decide si un documento es factura: ese campo no se le cuenta (ni a favor ni en contra)."""
    from app.agents import llm

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", lambda **kwargs: ({key: "" for key in llm.INVOICE_FIELDS}, {"model": "simulado"}))
    dataset = load_dataset(CATALOG)
    dataset.cases = [case for case in dataset.cases if case.id == "ambiguo_presupuesto"]
    claude = run(dataset, ["claude"])["engines"]["claude"]
    assert claude["rows"][0]["checks"] == {}
