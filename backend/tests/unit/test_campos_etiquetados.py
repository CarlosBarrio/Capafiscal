"""Campos de facturas con maquetas reales frecuentes: partes con etiqueta, nombres que no son nombres, número de
factura con serie o partido en líneas, fechas con el mes abreviado y fechas de albarán. Datos sintéticos
(SIMULACIÓN — NO OFICIAL): NIF inventados con prefijo B00 e IBAN ficticios."""
from __future__ import annotations

from pathlib import Path

from app import extraction_rules as rules
from app.extractor import extract_invoice

OURS = ("B00010009", "ESTUDIO EJEMPLO S.L.P.")
AMOUNTS = "\nBase imponible: 1.000,00 €\nI.V.A. 21%: 210,00 €\nTotal: 1.210,00 €\nSIMULACIÓN — NO OFICIAL\n"


def read(tmp_path: Path, text: str) -> dict:
    path = tmp_path / "factura.txt"
    path.write_text(text, encoding="utf-8")
    result = extract_invoice(path, company_tax_id=[OURS[0]], company_name=OURS[1])
    return {name: (field or {}).get("value") for name, field in result["fields"].items()}


def test_the_labelled_recipient_wins_over_the_address_next_to_its_tax_id(tmp_path):
    fields = read(tmp_path, (
        "Emisor: ESTUDIO EJEMPLO S.L.P.\nCalle Inventada 4, Bajo\n09000 Ciudad\nCIF: B 00010009\n"
        "Destinatario: CLIENTE DEMO ENERGÍA SLU\nCalle Falsa 133, 5ºA\nEdificio Ejemplo\n28000 Madrid\nCIF: B 00010017\n"
        "Fecha: 30 de julio de 2026\nFáctura: 31-26\nObjeto: Número de Pedido: 4700000001 / 0001\n" + AMOUNTS
    ))

    assert fields["customer_name"] == "CLIENTE DEMO ENERGÍA SLU"
    assert fields["invoice_number"] == "31-26"  # «Fáctura» con tilde sigue siendo la etiqueta; el pedido no
    assert fields["invoice_date"] == "2026-07-30"


def test_issuer_label_and_number_split_in_short_lines(tmp_path):
    fields = read(tmp_path, (
        "Emisor:\nLUCÍA EJEMPLO DEMO\nCalle Inventada 11, 2 B 09000 CIUDAD\nN.I.F.\n00100001C\n"
        "Destinatario:\nESTUDIO EJEMPLO S.L.P.\nC.I.F.\nB-00010009\nFactura:\nA\n-01\n-26\nFecha:\n12 de\nenero de 2026\n" + AMOUNTS
    ))

    assert fields["supplier_name"] == "LUCÍA EJEMPLO DEMO"
    assert fields["invoice_number"] == "A-01-26"


def test_a_professional_on_the_first_line_and_an_abbreviated_month(tmp_path):
    fields = read(tmp_path, (
        "Nombre Apellido Ejemplo Nº Factura: 0012345\nNIF: 00.100.002-K\nC/ Inventada Nº 7 3º D\n09000 Ciudad\nFACTURA\n"
        "Cliente\nNombre ESTUDIO EJEMPLO S.L.P. Fecha 05-mar-26\nCIF: B-00010009\n" + AMOUNTS
    ))

    assert fields["supplier_name"] == "Nombre Apellido Ejemplo"  # no «C/ Inventada Nº 7»
    assert fields["invoice_number"] == "0012345"
    assert fields["invoice_date"] == "2026-03-05"


def test_office_signs_registry_data_and_detail_rows_are_not_names(tmp_path):
    fields = read(tmp_path, (
        "FACTURA\nC.C.: 0005555\nSUMINISTROS DEMO S.L.U.\nESTUDIO EJEMPLO SLP\nB00010009\n"
        "NÚMERO FECHA TELÉFONO HOJA\n07-1234567 29-09-2026 900000000 1/ 1\n20 SERVICIO MENSUAL 1 4,50 0 4,50\n"
        "B00010025\nC.I.F.\nH/BU-0000\nF.11\nL.22\nT.33\nR.M.\nDELEGACIÓN: DELEGACIÓN:\n" + AMOUNTS
    ))

    assert fields["supplier_tax_id"] == "B00010025"
    assert fields["supplier_name"] == "SUMINISTROS DEMO S.L.U."
    assert fields["invoice_number"] == "07-1234567"  # no el código de cliente «C.C.»


def test_two_companies_on_one_line_series_column_and_delivery_note_date(tmp_path):
    fields = read(tmp_path, (
        "CLIENTE Nº. 9876\nPAPELERÍA DEMO, S.L. ESTUDIO EJEMPLO, S.L.P\nB00010033 ESTUDIO EJEMPLO, S.L.P.\n"
        "POLÍGONO DEMO, NAVE 1 B00010009\nFECHA SERIE NÚMERO\nFACTURA DE VENTA 30/09/2026 B2S 204060\n"
        "NºALBARÁN: Z99 / 1234 FECHA: 02/09/2026\n" + AMOUNTS
        + "Inscrita en el Registro Mercantil de Ciudad. Tomo 1. Libro 2. Folio 3. Hoja X/1.\nInscripción 1.ª B-00010033\n"
    ))

    assert fields["supplier_name"] == "PAPELERÍA DEMO, S.L."
    assert fields["invoice_number"] == "B2S 204060"
    assert fields["invoice_date"] == "2026-09-30"  # la del albarán no


def test_data_protection_name_with_its_tax_id_and_split_words():
    text = ("Datos de contacto: rgpd@gonzalez-demo.example\n"
            "el responsable del tratamiento de sus datos personales es GONZÁL EZ & DEMO CONSULTORES S.L., con NIF B00010041, "
            "y domicilio en Plaza Inventada 3")
    identities = [item for item in rules.legal_identities(text) if item.tax_id == "B00010041"]

    assert identities and identities[0].name == "GONZÁLEZ & DEMO CONSULTORES S.L."


def test_label_helpers_do_not_take_codes_or_our_own_company():
    assert rules.labelled_party("CLIENTE Nº. 9876\nOTRA COSA S.L.", "customer") is None
    assert rules.labelled_party("Cliente\nNombre CLIENTE DEMO S.A. Fecha 01/01/2026", "customer") == "CLIENTE DEMO S.A."
    assert rules.without_own_company("PAPELERÍA DEMO, S.L. ESTUDIO EJEMPLO, S.L.P", OURS[1]) == "PAPELERÍA DEMO, S.L."
    assert rules.not_a_name("Inscripción 1.ª") and rules.not_a_name("DELEGACIÓN: DELEGACIÓN")
