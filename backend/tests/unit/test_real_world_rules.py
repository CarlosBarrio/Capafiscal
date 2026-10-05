"""Reglas aprendidas de facturas reales y la interpretación híbrida (reglas + Claude)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app import extraction_rules as rules
from app import interpretation

SYNTHETIC = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "sinteticas"


def test_synthetic_replicas_are_read_perfectly():
    """Réplicas anonimizadas de maquetas reales: la puerta de regresión del extractor."""
    from evaluation.core import load_dataset
    from evaluation.core import run

    report = run(load_dataset(SYNTHETIC), ["reglas"])
    rules_report = report["engines"]["reglas"]
    failures = {row["id"]: {name: (row["expected"][name], row["got"][name]) for name, ok in row["checks"].items() if not ok} for row in rules_report["rows"] if not row["perfect"]}
    assert rules_report["perfect"] == report["cases"], failures


def test_vat_solver_handles_table_totals_and_ambiguous_spaces():
    tabla_totales = "Base imponible IVA 21 % Retención 0 % Total factura (Eur)\n212,40 44,60 0,00 257,00Inscrito\nVencimientos: 15/09/2026 257,00"
    assert rules.solve_amounts(tabla_totales) == {"subtotal": Decimal("212.40"), "tax_total": Decimal("44.60"), "withholding_total": None, "total": Decimal("257.00"), "tax_rate": Decimal("21"), "score": rules.solve_amounts(tabla_totales)["score"]}
    # «21 420,00» es el tipo (21) y la cuota (420,00), no 21.420,00
    pie_legal = "Neto % I.V.A.\nForma de pago: Transferencia 2.000,00 21 420,00\nTOTAL 2.420,00 €"
    solved = rules.solve_amounts(pie_legal)
    assert (solved["subtotal"], solved["tax_total"], solved["total"]) == (Decimal("2000.00"), Decimal("420.00"), Decimal("2420.00"))
    # dígitos separados por espacios
    spaced = "Subtotal ....... 2.150,00\nI.V.A. (21%) ........... 4 5 1,50\nTOTAL: 2.601,50 €"
    assert rules.solve_amounts(spaced)["tax_total"] == Decimal("451.50")
    # con retención de IRPF
    professional = "Base 1.000,00\nIVA 21% 210,00\nRetención 15% 150,00\nTotal 1.060,00"
    solved = rules.solve_amounts(professional)
    assert (solved["withholding_total"], solved["total"]) == (Decimal("150.00"), Decimal("1060.00"))


def test_implausible_amounts_are_detected():
    assert not rules.plausible_amounts("431.00", "431.00", "431.00", "431.00")  # todo igual: lectura errónea
    assert not rules.plausible_amounts("49.98", "49.98", "49.98")
    assert rules.plausible_amounts("356.45", "74.85", "431.30")


def test_rotated_text_both_extraction_styles():
    letters = "\n".join(["9", "0", "0", "1", "0", "9", "0", "0", "-B", ":.F", ".I.C", "-", "a", "tircs", "nI"])
    assert "C.I.F.:B-00901009" in rules.restore_rotated_text("cabecera\n" + letters + "\npie normal de la factura")
    words = "\n".join(["C.I.F.: C.I.F.:", "Inscripción Inscripción", "Hoja Hoja", "Folio Folio", "Mercantil Mercantil", "Registro Registro", "el el", "en en", "Inscrita Inscrita"])
    restored = rules.restore_rotated_text("cabecera\n" + words + "\nfin del documento con texto normal")
    assert "Inscrita en el Registro Mercantil Folio Hoja Inscripción C.I.F.:" in restored


def test_legal_footer_identifies_the_issuer():
    text = "ESTUDIO EJEMPLO SLP N.I.F.: B49123458\nFactura 260612\nINGENIERIA DEMO S.L. Inscrita en el Registro Mercantil de Burgos. CIF B-46444444"
    issuer = rules.issuer_from_legal_footer(text, {"B49123458"}, "Estudio Ejemplo S.L.P.")
    assert issuer.tax_id == "B46444444" and issuer.name == "INGENIERIA DEMO S.L."
    gdpr = "Sus datos serán tratados por ESTUDIOS DEL SUELO DEMO, S.L., como Responsable del Tratamiento. CIF B-47222229. Cliente B49123458"
    issuer = rules.issuer_from_legal_footer(gdpr, {"B49123458"}, "Estudio Ejemplo S.L.P.")
    assert issuer.name == "ESTUDIOS DEL SUELO DEMO, S.L." and issuer.tax_id == "B47222229"
    # La propia empresa en su pie legal no es «otro emisor»
    own = "ESTUDIO EJEMPLO S.L.P. Inscrita en el Registro Mercantil de Burgos. CIF B49123458"
    assert rules.issuer_from_legal_footer(own, {"B49123458"}, "Estudio Ejemplo S.L.P.") is None


def test_small_fixes():
    assert rules.repair_tax_id("A28333334C") == "A28333334"
    assert rules.number_from_header_row("Número de Factura Pág. Fecha\nN.I.F.: B49123458\n260612 1 30/09/2026") == "260612"
    assert rules.number_from_header_row("DOCUMENTO NÚMERO PÁGINA FECHA\nFactura 1 260777 1 de 2 01/09/2026") == "260777"
    assert rules.suspicious_number("1 260190 1 DE 2 01/08/2026") and rules.suspicious_number("2026") and not rules.suspicious_number("FAC-2026-001")
    assert [item.isoformat() for item in rules.textual_dates("Fecha: 24 de Septiembre de 2026")] == ["2026-09-24"]
    assert rules.is_continuation_page("Factura 1 260777 2 de 2 01/09/2026", 2)
    assert not rules.is_continuation_page("Factura 1 260778 1 de 1", 2)


# ---------------------------------------------------------------------
# Híbrido: Claude propone, las reglas validan
# ---------------------------------------------------------------------

DOC = """INGENIERIA DEMO S.L. Inscrita en el Registro Mercantil de Burgos. CIF B-46444444
Número de Factura Pág. Fecha
N.I.F.: B49123458
260612 1 30/09/2026
Neto % I.V.A.
Transferencia 2.000,00 21 420,00
TOTAL 2.420,00 €"""


def rules_result(**values) -> dict:
    return {"raw_text": DOC, "direction": "ISSUED", "fields": {name: {"value": value, "confidence": 60} for name, value in values.items()}}


def test_hybrid_accepts_verified_values_and_rejects_invented_ones():
    result = rules_result(supplier_tax_id="B49123458", supplier_name="Estudio Ejemplo", invoice_number="260612131/08/2026", invoice_date="2026-09-30", subtotal=None, tax_total="21420.00", total="2420.00")
    reasons = interpretation.needs_help(result, {"B49123458"})
    assert "el emisor detectado es la propia empresa" in reasons and "número de factura sospechoso" in reasons
    claude = {
        "supplier_name": "INGENIERIA DEMO S.L.", "supplier_tax_id": "B46444444", "customer_name": "Estudio Ejemplo", "customer_tax_id": "B49123458",
        "invoice_number": "260612", "invoice_date": "2026-09-30", "due_date": "2026-10-30",  # vencimiento inventado: no está en el texto
        "subtotal": "2000.00", "tax_total": "420.00", "withholding_total": "", "total": "2420.00",
    }
    merged, decisions = interpretation.merge(result, claude, DOC, {"B49123458"}, reasons)
    assert merged["supplier_tax_id"] == "B46444444" and merged["customer_tax_id"] == "B49123458"
    assert merged["direction"] == "RECEIVED"  # lo deciden los NIF de la empresa, no la IA
    assert merged["invoice_number"] == "260612"
    assert (merged["subtotal"], merged["tax_total"], merged["total"]) == ("2000.00", "420.00", "2420.00")
    assert merged["due_date"] is None  # Claude lo inventó: descartado
    assert any(item["field"] == "due_date" and "descartado" in item["why"] for item in decisions)


def test_hybrid_rejects_amounts_that_do_not_reconcile():
    result = rules_result(subtotal="2000.00", tax_total="420.00", total="2420.00")
    claude = {"subtotal": "2100.00", "tax_total": "441.00", "total": "2541.00"}
    merged, _decisions = interpretation.merge(result, claude, DOC, {"B49123458"}, [])
    assert merged["total"] == "2420.00"


def test_refine_without_key_falls_back_to_rules(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text(DOC, encoding="utf-8")
    result = rules_result(subtotal=None, total=None)
    refined = interpretation.refine(path, result, company_tax_ids=["B49123458"])
    assert refined["interpretation"]["fallback"] == "sin ANTHROPIC_API_KEY"
    assert refined["fields"] == result["fields"]


def test_refine_uses_claude_when_configured(tmp_path, monkeypatch):
    from app.agents import llm

    path = tmp_path / "f.txt"
    path.write_text(DOC, encoding="utf-8")
    calls = []

    def fake_extract(**kwargs):
        calls.append(kwargs)
        return (
            {"supplier_name": "INGENIERIA DEMO S.L.", "supplier_tax_id": "B46444444", "customer_name": "", "customer_tax_id": "B49123458",
             "invoice_number": "260612", "invoice_date": "2026-09-30", "due_date": "", "subtotal": "2000.00", "tax_total": "420.00",
             "withholding_total": "", "total": "2420.00", "tax_rate": "21", "concept": "Proyecto"},
            {"model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 150, "cost_usd": llm.estimate_cost("claude-opus-5-5", 1200, 150)},
        )

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "extract_invoice", fake_extract)
    result = rules_result(supplier_tax_id="B49123458", subtotal=None, tax_total=None, total="2420.00")
    refined = interpretation.refine(path, result, company_tax_ids=["B49123458"], company_name="Estudio Ejemplo")
    assert calls and calls[0]["text"] and calls[0]["pdf_bytes"] is None
    assert refined["direction"] == "RECEIVED"
    assert refined["fields"]["supplier_tax_id"]["value"] == "B46444444"
    assert refined["fields"]["subtotal"]["source"] == "claude+reglas"
    assert refined["interpretation"]["meta"]["cost_usd"] == 0.0078
