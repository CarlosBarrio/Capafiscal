"""Reglas de lectura que deciden si algo es factura, quién la emite y cómo se reparan las etiquetas de OCR.

Cada caso es una línea o cabecera mínima con la forma que se ve en documentos reales (sin datos reales).
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------
# ¿Es factura? Título frente a campo, columna o referencia
# ---------------------------------------------------------------------

NOT_INVOICES = {
    "albarán real": ("TRANSPORTES EJEMPLO S.L.\nNIF B00001016\nALBARÁN DE ENTREGA Nº AE-55120\nCantidad Descripción\nTotal 121,00", "albarán"),
    "presupuesto real": ("ASESORÍA EJEMPLO S.L.\nPRESUPUESTO Nº P-2026-114 (no es una factura)\nIVA 21 %\nTotal 121,00", "presupuesto"),
    "proforma real": ("EMPRESA EJEMPLO S.L.\nFACTURA PROFORMA Nº PF-12\nBase 100,00\nTotal 121,00", "proforma"),
    "pedido real": ("EMPRESA EJEMPLO S.L.\nNOTA DE PEDIDO Nº 45\nTotal 121,00", "pedido"),
    "albarán que avisa de su factura": ("ALBARÁN Nº 7\nFactura: pendiente\nTotal 121,00", "albarán"),
}

INVOICES = {
    "factura con «Albarán: 4471» encima del título": "TALLERES EJEMPLO S.L.\nAlbarán: 4471\nFACTURA Nº 12\nTotal 121,00",
    "factura con columna «Albarán Fecha Importe»": "EMPRESA EJEMPLO S.L.\nNº de factura: F-12\nAlbarán Fecha Importe\nA-1 01/09/2026 100,00\nTotal 121,00",
    "columna «Albarán» sin título de factura": "EMPRESA EJEMPLO S.L.\nAlbarán   Fecha   Importe\nA-1 01/09/2026 100,00",
    "título «F A C T U R A»": "F A C T U R A\nAlbarán 5\nTotal 10,00",
    "documento sin título claro": "EMPRESA EJEMPLO S.L.\nPresupuesto nº 88 aceptado\nTotal 121,00",
    "«Pedido nº» como referencia antes del título": "EMPRESA EJEMPLO S.L.\nPedido nº 4500123\nFACTURA 2026-001",
    "factura simplificada": "FACTURA SIMPLIFICADA Nº T-9\nTotal 10,00",
    "factura rectificativa": "Factura rectificativa R-1",
    "albarán-factura": "ALBARÁN-FACTURA Nº 33",
    "factura que cita albarán y presupuesto": "FACTURA Nº 12\nSegún albarán 33 y presupuesto aceptado",
}


@pytest.mark.parametrize("name", NOT_INVOICES)
def test_real_quotes_delivery_notes_proformas_and_orders_are_not_invoices(name):
    from app.extractor import non_invoice_title

    text, kind = NOT_INVOICES[name]
    assert non_invoice_title(text) == kind


@pytest.mark.parametrize("name", INVOICES)
def test_invoices_that_mention_those_words_stay_invoices(name):
    from app.extractor import non_invoice_title

    assert non_invoice_title(INVOICES[name]) is None


def test_the_word_alone_anywhere_is_not_enough():
    """No es «aparece FACTURA ⇒ factura»: un campo «Factura: pendiente» no salva a un albarán, y una frase que
    menciona «factura» dentro de un presupuesto tampoco."""
    from app.extractor import non_invoice_title

    assert non_invoice_title("PRESUPUESTO Nº 3\nEste presupuesto no es una factura\nTotal 50,00") == "presupuesto"
    assert non_invoice_title("ALBARÁN Nº 7\nFactura: pendiente") == "albarán"


def test_invoice_likelihood_follows_the_title_rule(tmp_path):
    """De punta a punta (extract_invoice): el campo «Albarán: 4471» no anula la factura; el título ALBARÁN sí."""
    from app.extractor import extract_invoice

    def read(text: str) -> dict:
        path = tmp_path / "doc.txt"
        path.write_text(text, encoding="utf-8")
        return extract_invoice(path)

    body = "\nCIF B00001016\nFecha 01/09/2026\nBase imponible 100,00\nIVA 21 % 21,00\nTotal 121,00\n"
    invoice = read("TALLERES EJEMPLO S.L.\nAlbarán: 4471\nFACTURA Nº 12" + body)
    assert invoice["is_invoice"] and not any(signal.startswith("not_invoice") for signal in invoice["signals"])
    note = read("TALLERES EJEMPLO S.L.\nALBARÁN Nº 4471" + body)
    assert not note["is_invoice"] and note["invoice_likelihood"] <= 30 and "not_invoice:albarán" in note["signals"]


# ---------------------------------------------------------------------
# Nombre del proveedor
# ---------------------------------------------------------------------

NOT_COMPANY_NAMES = (
    "https://empresa.es", "Fechas de entrega", "Datos de facturación", "Importes en euros", "Precios netos",
    "Conceptos varios", "Números de serie", "info@empresa-ejemplo.es", "www.ejemplo.com", "empresa-ejemplo.es",
    "Número: 12", "Fecha factura", "Calle Mayor 3", "Forma de pago: EMPRESA EJEMPLO S.L.",
    "Concepto: servicios EMPRESA EJEMPLO S.L.", "Registro Mercantil de Burgos, EMPRESA EJEMPLO S.L.",
)
COMPANY_NAMES = (
    "ASESORÍA NÚMEROS CLAROS S.L.", "PACÍFICO SUMINISTROS S.L.", "UNIFORMES NORTE S.L.", "CALLEJA HERMANOS S.A.",
    "FACTURAS Y SERVICIOS S.L.", "TRANSPORTES RAPIDOS NORTE S.L.", "PAPELERIA CENTRO", "Fruteria La Huerta",
    "Talleres Barrio", "Construcciones Ejemplo S.A.", "Gestoría Ejemplo y Asociados SLP",
)


@pytest.mark.parametrize("line", NOT_COMPANY_NAMES)
def test_urls_labels_and_fields_are_never_a_supplier(line):
    from app.extractor import looks_like_company_name

    assert not looks_like_company_name(line)


@pytest.mark.parametrize("name", COMPANY_NAMES)
def test_real_company_names_are_still_accepted(name):
    from app.extractor import looks_like_company_name

    assert looks_like_company_name(name)


def test_supplier_comes_from_the_tax_id_not_from_a_table_label():
    """Con NIF, manda el NIF y su contexto: la etiqueta de tabla de las primeras líneas no gana."""
    from app.extractor import company_name_near_tax_id

    text = "Importes en euros\nFechas de entrega\nTALLERES EJEMPLO S.L.\nCIF: B00001016\nFACTURA Nº 12\nTotal 121,00"
    field = company_name_near_tax_id(text, "B00001016", "supplier")
    assert field.value == "TALLERES EJEMPLO S.L." and field.confidence >= 85


# ---------------------------------------------------------------------
# Reparación de etiquetas de OCR
# ---------------------------------------------------------------------


def test_clean_text_is_left_exactly_as_it_was():
    from app.extraction_rules import repair_ocr_labels

    clean = "Base Imponible 100,00\nIVA 21 % 21,00\nTotal factura 121,00\nTOTAL 121,00\nIva incluido\nTOTALES\nCuota 21,00"
    assert repair_ocr_labels(clean) == clean


@pytest.mark.parametrize(("ocr", "fixed"), [
    ("T0TAL 121,00", "TOTAL 121,00"),          # 0 por O
    ("T0tal factura 121,00", "Total factura 121,00"),
    ("TOT4L 121,00", "TOTAL 121,00"),          # 4 por A
    ("T0T4L 121,00", "TOTAL 121,00"),
    ("Ba5e imponible 238,00", "Base imponible 238,00"),  # 5 por S
    ("BA5E IMPONIBLE", "BASE IMPONIBLE"),
    ("lVA 21 % 21,00", "IVA 21 % 21,00"),      # l por I
    ("1VA 21 %", "IVA 21 %"),                  # 1 por I
    ("|VA 21 %", "IVA 21 %"),
    ("lmponible", "imponible"),
    ("Cu0ta 21,00", "Cuota 21,00"),
])
def test_ocr_confusions_are_repaired(ocr, fixed):
    from app.extraction_rules import repair_ocr_labels

    assert repair_ocr_labels(ocr) == fixed


def test_repair_signal_only_when_something_was_repaired(tmp_path):
    from app.extractor import extract_invoice

    def signals(text: str) -> list[str]:
        path = tmp_path / "f.txt"
        path.write_text(text, encoding="utf-8")
        return extract_invoice(path)["signals"]

    body = "TALLERES EJEMPLO S.L.\nCIF B00001016\nFACTURA Nº 12\nFecha 01/09/2026\nBase imponible 100,00\nIVA 21 % 21,00\n"
    assert "ocr_labels_repaired" not in signals(body + "Total factura 121,00")
    assert "ocr_labels_repaired" in signals(body + "T0TAL FACTURA 121,00")
