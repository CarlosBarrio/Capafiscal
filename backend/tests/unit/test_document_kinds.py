"""Qué es un documento que no es factura: albarán, presupuesto u oferta, pedido o nómina.

Cada caso es una cabecera mínima con la forma que se ve en documentos reales (sin datos reales). Las reglas
miran la FORMA (título, campo con su número, estructura legal de la nómina), no un texto concreto.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------
# El número propio del documento como campo («Albarán: 23-01», «Pedido nº 4500»)
# ---------------------------------------------------------------------

FIELD_CASES = [
    # Sin identidad de factura en la cabecera, el número propio dice qué es el documento
    ("EMPRESA EJEMPLO S.L.\nFecha: 3 de marzo de 2026\nAlbarán: 26-14\nNúm. pedido/fecha 77/3", "albarán"),
    ("TRANSPORTES EJEMPLO S.L.\nNº albarán: AE-551\nTotal 121,00", "albarán"),
    ("ESTUDIO EJEMPLO S.L. Pedido nº 4500012345 S.L.\nPlazo de entrega 01.04.2026", "pedido"),
    ("Presupuesto: 26000123\nCliente EMPRESA EJEMPLO S.L.", "presupuesto"),
    # El primero que aparece manda: «según oferta 23-45» más abajo no convierte un pedido en presupuesto
    ("EMPRESA EJEMPLO S.L. Pedido nº 4500012345\nTotal 121,00\nOferta 23-45", "pedido"),
]

FIELD_INVOICES = [
    # Con identidad de factura, el campo es una referencia: sigue siendo factura
    "TALLERES EJEMPLO S.L.\nAlbarán: 4471\nFACTURA Nº 12\nTotal 121,00",
    "EMPRESA EJEMPLO S.L.\nNº de factura: F-12\nPedido nº 4500123\nTotal 121,00",
    # Facturas reales cuyo número no tiene la forma estricta de título, con referencias a su pedido o albarán
    "EMPRESA EJEMPLO S.L.\nFactura: 24-07\nNúmero de pedido: 4500012345\nTotal 121,00",
    "EMPRESA EJEMPLO S.L.\nFactura de venta 01/09/2026 A1 123456\nNº albarán: A9\nTotal 121,00",
    # Sin número, o sin marca, no es un campo de identidad
    "EMPRESA EJEMPLO S.L.\nAlbarán   Fecha   Importe\nA-1 01/09/2026 100,00",
    "EMPRESA EJEMPLO S.L.\nRef. presupuesto 88 aceptado\nTotal 121,00",
]


@pytest.mark.parametrize(("text", "kind"), FIELD_CASES)
def test_the_documents_own_number_says_what_it_is(text, kind):
    from app.extractor import non_invoice_title

    assert non_invoice_title(text) == kind


@pytest.mark.parametrize("text", FIELD_INVOICES)
def test_a_reference_field_does_not_turn_an_invoice_into_something_else(text):
    from app.extractor import non_invoice_title

    assert non_invoice_title(text) is None


@pytest.mark.parametrize(("title", "kind"), [
    ("OFERTA DE HONORARIOS 12/24", "presupuesto"),
    ("OFERTA DE HONORARIOS TÉCNICOS Nº 12/24REV01", "presupuesto"),
    ("ORDEN DE COMPRA Nº 4500123", "pedido"),
])
def test_a_title_with_a_document_number_needs_no_number_mark(title, kind):
    from app.extractor import non_invoice_title

    assert non_invoice_title(f"ESTUDIO EJEMPLO S.L.\n{title}\nTotal 121,00") == kind


@pytest.mark.parametrize("sentence", [
    "Pedido realizado el 01/09/2026", "Albarán firmado el 01/09/2026", "Presupuesto válido 30 días",
])
def test_a_date_is_not_a_document_number(sentence):
    from app.extractor import non_invoice_title

    assert non_invoice_title(f"EMPRESA EJEMPLO S.L.\n{sentence}\nTotal 121,00") is None


# ---------------------------------------------------------------------
# Nómina: la estructura del recibo de salarios oficial, no la palabra «nómina»
# ---------------------------------------------------------------------

PAYSLIP = (
    "EMPRESA EJEMPLO S.L.  CIF B00001016\nTRABAJADOR/A  NIF 00000000T\nI. DEVENGOS\nSalario base 1.500,00\n"
    "A. TOTAL DEVENGADO 1.500,00\nII. DEDUCCIONES\nContingencias comunes 70,50\nB. TOTAL A DEDUCIR 300,00\n"
    "LÍQUIDO TOTAL A PERCIBIR (A-B) 1.200,00\n"
    "DETERMINACIÓN DE LAS BASES DE COTIZACIÓN A LA SEGURIDAD SOCIAL Y CONCEPTOS DE RECAUDACIÓN\n"
)


def test_a_payslip_is_recognised_by_its_legal_structure():
    from app.extractor import is_payslip
    from app.extractor import non_invoice_title

    assert is_payslip(PAYSLIP)
    assert non_invoice_title(PAYSLIP) == "nómina"
    # Una nómina mal escaneada conserva al menos el encabezado oficial de las bases de cotización
    assert is_payslip("ILEGIBLE\nDETERMINACION DE LAS BASES DE COTIZACION A LA SEGURIDAD SOCIAL\nCONTINGENCIAS COMUNES")


@pytest.mark.parametrize("text", [
    # La gestoría que factura la confección de nóminas emite una factura, no una nómina
    "FACTURA Nº 2026-31\nASESORÍA EJEMPLO S.L.\nConfección de nóminas y seguros sociales 120,00\nIVA 21 % 25,20\nTotal 145,20",
    # Una factura de profesional con retención dice «líquido a percibir» y no es una nómina
    "FACTURA Nº 7\nHonorarios 1.000,00\nIVA 21 % 210,00\nIRPF -15 % -150,00\nLíquido a percibir 1.060,00",
])
def test_invoices_that_talk_about_payroll_are_not_payslips(text):
    from app.extractor import is_payslip
    from app.extractor import non_invoice_title

    assert not is_payslip(text)
    assert non_invoice_title(text) is None


def test_a_payslip_is_not_an_invoice_end_to_end(tmp_path):
    from app.extractor import extract_invoice

    path = tmp_path / "nomina.txt"
    path.write_text(PAYSLIP, encoding="utf-8")
    result = extract_invoice(path, company_tax_id=["B00001016"])
    assert not result["is_invoice"]
    assert "not_invoice:nómina" in result["signals"]
    assert result["document_type"] == "nomina"


def test_document_type_names_the_kind_it_found(tmp_path):
    from app.extractor import extract_invoice

    path = tmp_path / "albaran.txt"
    path.write_text("TRANSPORTES EJEMPLO S.L.\nALBARÁN Nº AE-55120\nCIF B00001016\nTotal 121,00", encoding="utf-8")
    assert extract_invoice(path)["document_type"] == "albaran"


# ---------------------------------------------------------------------
# Sentido: si solo lo decide la posición del NIF, lo confirma una persona
# ---------------------------------------------------------------------

def test_issued_by_position_only_asks_a_person():
    from app.interpretation import needs_help

    result = {"is_invoice": True, "direction": "ISSUED", "direction_confidence": 80, "fields": {}, "signals": []}
    assert any("emitimos o la recibimos" in reason for reason in needs_help(result, {"B00001016"}))
    clear = {**result, "direction_confidence": 92}
    assert not any("emitimos o la recibimos" in reason for reason in needs_help(clear, {"B00001016"}))


# ---------------------------------------------------------------------
# Pedidos y ofertas sin título reconocible
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "EMPRESA EJEMPLO S.L.\nORDEN\nNúmero de orden 4500012345\nSe aplican nuestras condiciones generales de compra.\nTotal 121,00",
    "EMPRESA EJEMPLO S.L.  Num. pedido/fecha\nPor favor confirme la aceptación del pedido.\nTotal 121,00",
    "Cítese el nº de pedido en la fra. y en el albarán\nEMPRESA EJEMPLO S.L.\nSegún oferta 23-45\nTotal 121,00",
])
def test_what_only_a_purchase_order_says(text):
    from app.extractor import non_invoice_title

    assert non_invoice_title(text) == "pedido"


def test_an_offer_letter_without_title():
    from app.extractor import non_invoice_title

    letter = ("ESTUDIO EJEMPLO S.L.\nEstimado cliente:\nCon el fin de ofertarte los trabajos solicitados, te enviamos "
              "nuestra oferta. No es necesario contratar todo lo ofertado.\nHonorarios 1.000,00\nIVA 21 % 210,00")
    assert non_invoice_title(letter) == "presupuesto"


@pytest.mark.parametrize("text", [
    "FACTURA Nº 12\nSegún su pedido 4500 y nuestras condiciones generales de compra\nTotal 121,00",
    "FACTURA Nº 7\nSegún oferta 23-45 y presupuesto aceptado; ver oferta adjunta\nTotal 121,00",
])
def test_an_invoice_that_mentions_orders_or_offers_stays_an_invoice(text):
    from app.extractor import non_invoice_title

    assert non_invoice_title(text) is None


# ---------------------------------------------------------------------
# Word, RTF, HTML y XML: se leen para clasificarlos
# ---------------------------------------------------------------------

def test_other_formats_are_read(tmp_path):
    from app.office_text import office_text

    (tmp_path / "pedido.htm").write_text("<html><body><h1>PEDIDO Nº 4500123</h1><script>alert(1)</script><p>Total 121,00</p></body></html>", encoding="utf-8")
    (tmp_path / "pedido.xml").write_text('<?xml version="1.0" encoding="UTF-8"?><Order><Title>PEDIDO Nº 4500123</Title><Total>121,00</Total></Order>', encoding="utf-8")
    (tmp_path / "oferta.rtf").write_bytes(rb"{\rtf1\ansi{\fonttbl{\f0 Arial;}}{\*\generator Ejemplo;}\f0 OFERTA N\'ba 12/26\par Honorarios 1.000,00\par}")
    for name, expected in (("pedido.htm", "PEDIDO Nº 4500123"), ("pedido.xml", "PEDIDO Nº 4500123"), ("oferta.rtf", "OFERTA Nº 12/26")):
        text = office_text(tmp_path / name)
        assert expected in text.splitlines(), (name, text)
    assert "alert" not in office_text(tmp_path / "pedido.htm")
    # Un .doc de Word 97 guarda el texto en UTF-16: se recupera
    (tmp_path / "presupuesto.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64 + "PRESUPUESTO Nº 26-14 para la reforma\r".encode("utf-16-le") + b"\x00" * 32)
    assert "PRESUPUESTO Nº 26-14 para la reforma" in office_text(tmp_path / "presupuesto.doc")
