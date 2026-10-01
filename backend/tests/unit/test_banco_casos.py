"""Banco de expedientes sintéticos: marca de simulación, NIF inventados, plazos y evaluador."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pymupdf

from evaluation import casos
from evaluation.banco_casos import B_DIR
from evaluation.banco_casos import build_c
from evaluation.banco_casos import world_b
from evaluation.documentos import MARK
from evaluation.plantillas import business_days_after


def test_every_case_has_ground_truth_and_documents():
    index = json.loads((B_DIR / "indice.json").read_text(encoding="utf-8"))
    assert index["total_casos"] >= 25 and index["total_documentos"] >= 40
    for item in index["casos"]:
        case = json.loads((B_DIR / item["case_id"] / "caso.json").read_text(encoding="utf-8"))
        assert case["entradas"], item["case_id"]
        for document in case["documents"]:
            assert (B_DIR / item["case_id"] / document["archivo"]).is_file()
        for entry in case["entradas"]:
            for name in [*(entry.get("expected") or {})] + [key for expected in entry.get("expected_adjuntos", []) for key in expected]:
                assert name in casos.CHECK_LABELS, f"{item['case_id']}: comprobación desconocida {name}"


def test_documents_are_marked_and_use_invented_tax_ids():
    from app.extractor import is_valid_spanish_tax_id

    world = world_b()
    for party in (world.company, world.supplier, world.carrier, world.cleaner, world.software):
        assert party.tax_id.startswith("B00") and is_valid_spanish_tax_id(party.tax_id)  # provincia 00: no existe
    for path in B_DIR.rglob("*.pdf"):
        document = pymupdf.open(path)
        text = "".join(page.get_text() for page in document)
        assert MARK in text or not text.strip(), path  # los escaneados no tienen texto


def test_business_days_skip_weekends_and_national_holidays():
    assert business_days_after(date(2026, 9, 28), 10) == date(2026, 10, 13)  # el 12 de octubre es festivo


def test_blind_set_is_sealed_and_reproducible(tmp_path):
    first = build_c(7, tmp_path / "c1")
    second = build_c(7, tmp_path / "c2")
    assert first["total_casos"] == 12 and first["semilla"] == 7
    assert [item["case_id"] for item in first["casos"]] == [item["case_id"] for item in second["casos"]]
    assert first["sello"]


def observed(**values):
    base = {
        "type": "NOTIFICATION", "route": None, "procedure": None, "issuer": None, "reference": None, "affected": None, "subject": None, "intake_warnings": [],
        "deadline": None, "deadline_rule": None, "debt_amount": None, "credit_amount": None, "requires_human": True, "agents": [], "findings": [], "documents": [],
        "insights": "", "tax_references": [], "event_status": "COMPLETED", "case_code": "EXP-1", "case_status": "OPEN", "requires_ocr": False,
        "document_status": None, "duplicate": False, "invoice": None, "flags": [],
    }
    return {**base, **values}


def test_credit_exceeds_debt_needs_the_debt_amount_in_the_advice():
    retains_everything = observed(debt_amount=2850.0, credit_amount=4100.0, insights="Tienes 2 facturas por 4.100,00 €: no se las pagues; quedan retenidas.")
    retains_the_debt = observed(debt_amount=2850.0, credit_amount=4100.0, insights="Retén 2.850,00 € (la deuda) y paga el resto al proveedor.")
    assert not casos.check("expected_findings", ["credit_exceeds_debt"], retains_everything, {})[0]
    assert casos.check("expected_findings", ["credit_exceeds_debt"], retains_the_debt, {})[0]


def test_deadline_without_notification_date_must_not_be_invented():
    invented = observed(deadline=date(2026, 10, 7), deadline_rule="10 días hábiles desde la fecha de notificación.")
    marked = observed(deadline=date(2026, 10, 7), deadline_rule="10 días hábiles desde la fecha del documento; indica la fecha real de notificación para afinarlo.")
    assert not casos.check("deadline_pending_confirmation", True, invented, {})[0]
    assert casos.check("deadline_pending_confirmation", True, marked, {})[0]
    assert casos.check("deadline_pending_confirmation", True, observed(), {})[0]


def test_wrong_invoice_without_warning_is_a_silent_error():
    truth = {"invoice_number": "SFD-2026-144", "total": "550.55"}
    wrong = {"invoice_number": "550", "total": "550.55"}
    assert casos.compare_invoice(truth, wrong, [])["outcome"] == "error_silencioso"
    assert casos.compare_invoice(truth, wrong, ["falta invoice_number"])["outcome"] == "detectado"
    assert casos.compare_invoice(truth, truth, [])["outcome"] == "correcto"
