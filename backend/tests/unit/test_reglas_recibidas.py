"""Reglas sacadas de los fallos en facturas recibidas reales, probadas con réplicas inventadas.

SIMULACIÓN — NO OFICIAL: nombres, NIF (prefijo B00) y DNI son ficticios; solo se copia la maqueta.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app import extraction_rules as rules
from app import interpretation
from app.extractor import TAX_ID_PATTERN
from app.extractor import company_name_near_tax_id
from app.extractor import extract_invoice
from app.extractor import find_invoice_number
from app.extractor import normalize_tax_id
from evaluation.core import name_tokens

OWN = "B00900100"
OWN_NAME = "Ibernova Servicios, S.L."


def test_footer_name_next_to_its_cif_beats_the_privacy_paragraph():
    """El párrafo de protección de datos trae una errata; el pie del registro, el nombre bueno junto al CIF."""
    text = (
        "FACTURA Nº 45/26\nIBERNOVA SERVICIOS, S.L\nB-00.900.100\n"
        "responsable del fichero: ENERGIAS DEMOO S.L en C/ Inventada 1\n"
        "ENERGIAS DEMO | C.I.F.: B-00901009 - Inscrita en el registro Mercantil de Burgos Tomo 1"
    )
    issuer = rules.issuer_from_legal_footer(text, {OWN}, OWN_NAME)
    assert issuer.tax_id == "B00901009" and issuer.name == "ENERGIAS DEMO"


def test_fecha_factura_label_is_not_the_invoice_number():
    text = "FACTURA: 77/26\nFECHA FACTURA: 01/02/2026 B-00.900.100\nCONCEPTO: proyecto"
    assert find_invoice_number(text).value == "77/26"


def test_two_column_header_keeps_the_issuer_half():
    text = (
        "FACTURA\nDATOS DE CLIENTE\n"
        "SURTIDORES DEMO S.A. IBERNOVA SERVICIOS S.L.P\n"
        "Avda. Inventada 36 CALLE FICTICIA 4\n"
        "28001 MADRID 09001 BURGOS\n"
        "MADRID BURGOS\n"
        "NIF: B00901108 NIF: B00900100\n"
        "CRTA. INVENTADA KM 2\n"
    )
    assert company_name_near_tax_id(text, "B00901108", "supplier").value == "SURTIDORES DEMO S.A."


def test_professional_with_dotted_dni_and_job_title():
    text = (
        "PEDRO EJEMPLO FICTICIO\nARQUITECTO TECNICO\nN.I.F. 12.345.678-Z\nAvda. Inventada 29\n"
        "IBERNOVA SERVICIOS S.L.\nCIF.- B00900100\nFactura nº 31/26"
    )
    found = [normalize_tax_id(match.group(0)) for match in TAX_ID_PATTERN.finditer(text)]
    assert "12345678Z" in found and not any(value.startswith("F") for value in found)
    assert company_name_near_tax_id(text, "12345678Z", "supplier").value == "PEDRO EJEMPLO FICTICIO"
    assert rules.profession_only("ARQUITECTO TÉCNICO") and not rules.profession_only("PEDRO EJEMPLO")


def test_dot_as_thousands_and_decimal_separator():
    text = "Importe\n3.100.00 €\nI.V.A 21%\nMedio de pago Impuestos 651.00 €\nSuma Parcial\nTRANSFERENCIA 3.751.00 €\nTOTAL 3.751.00 €"
    solved = rules.solve_amounts(text)
    assert (solved["subtotal"], solved["tax_total"], solved["total"]) == (Decimal("3100.00"), Decimal("651.00"), Decimal("3751.00"))
    assert rules.declared_vat_without_tax(text, "0.00")
    assert not rules.declared_vat_without_tax("Operación exenta de IVA 21%", "0.00")


def test_direct_debit_receipt_with_only_our_tax_id_is_not_a_sale(tmp_path: Path):
    """El emisor solo está en imágenes y no hay OCR: mejor sin emisor (y a revisión) que dárnosla como venta."""
    document = tmp_path / "recibo.txt"
    document.write_text(
        "IBERNOVA SERVICIOS, S.L.P\nB00900100\nFACTURA X00042/26\nFECHA 15/03/2026\n"
        "CUOTA SERVICIO DEMO 50,00 €\nSujeto a impuestos 50,00 €\nIVA 21% 10,50 €\n"
        "Forma de pago: RECIBO DOMICILIADO A LA VISTA\nTOTAL 60,50 €\n",
        encoding="utf-8",
    )
    result = extract_invoice(document, company_tax_id=[OWN], company_name=OWN_NAME)
    assert result["direction"] == "RECEIVED"
    assert result["fields"]["supplier_tax_id"]["value"] is None
    assert result["fields"]["customer_tax_id"]["value"] == OWN
    assert "falta supplier_tax_id" in interpretation.needs_help(result, {OWN})


def test_issued_invoice_with_a_customer_is_not_flagged():
    issued = {"direction": "ISSUED", "fields": {
        "supplier_name": {"value": OWN_NAME}, "supplier_tax_id": {"value": OWN}, "customer_tax_id": {"value": "B00901207"},
        "invoice_number": {"value": "33-90"}, "invoice_date": {"value": "2026-05-04"},
        "subtotal": {"value": "100.00"}, "tax_total": {"value": "21.00"}, "total": {"value": "121.00"}}}
    assert interpretation.needs_help(issued, {OWN}) == []
    alone = {**issued, "fields": {**issued["fields"], "customer_tax_id": {"value": None}}}
    assert "el emisor detectado es la propia empresa" in interpretation.needs_help(alone, {OWN})


def test_suspicious_values_raise_a_review():
    base = {"direction": "RECEIVED", "raw_text": "IVA 21% 0,00", "fields": {
        "supplier_name": {"value": "MADRID BURGOS"}, "supplier_tax_id": {"value": "B00901001"}, "customer_tax_id": {"value": OWN},
        "invoice_number": {"value": "7/26"}, "invoice_date": {"value": "2026-05-04"},
        "subtotal": {"value": "100.00"}, "tax_total": {"value": "0.00"}, "total": {"value": "100.00"},
        "category": {"value": "Suministros"}}}
    reasons = interpretation.needs_help(base, {OWN})
    assert "nombre del proveedor sospechoso" in reasons
    assert "NIF del proveedor no válido" in reasons
    assert "el documento declara IVA pero la cuota leída es 0" in reasons


def test_footer_name_after_another_legal_form_and_markers():
    text = "IBERNOVA SERVICIOS, S.L.P\n[imagen] PREVENCION DEMO, S.L.\n[imagen] Inscrita en el Registro Mercantil de Burgos, Tomo 1 - C.I.F.: B00901306"
    issuer = rules.issuer_from_legal_footer(text, {OWN}, OWN_NAME)
    assert issuer.tax_id == "B00901306" and issuer.name == "PREVENCION DEMO, S.L."


def test_evaluator_ignores_dotted_legal_forms():
    assert name_tokens("ENERGIAS DEMO, S.L.") == name_tokens("ENERGIAS DEMO") == {"energias", "demo"}
    assert name_tokens("Ibernova Servicios S.L.P.") == {"ibernova", "servicios"}
