from __future__ import annotations

import pytest

from app.extractor import category_account
from app.extractor import extract_invoice
from tests.conftest import FIXTURES_DIR


def values(result: dict) -> dict:
    return {
        name: field.get("value")
        for name, field in result["fields"].items()
    }


@pytest.mark.parametrize(
    ("stem", "supplier", "tax_id", "number", "invoice_date", "total", "category"),
    [
        ("factura_repsol_2026", "REPSOL COMERCIAL S.A.", "A28047223",
         "FRA-2026-00912", "2026-07-15", "479.50", "Combustible"),
        ("factura_endesa_2026", "ENDESA ENERGIA S.A.U.", "A81948077",
         "E26-4471183", "2026-07-10", "312.91", "Suministros"),
        ("factura_makro_2026", "MAKRO AUTOSERVICIO MAYORISTA S.A.", "A28647451",
         "M-2026-778120", "2026-07-12", "768.36", "Compras y aprovisionamientos"),
        ("factura_vodafone_2026", "VODAFONE ESPANA S.A.U.", "A80907397",
         "VF-2026-556231", "2026-07-08", "87.00", "Telecomunicaciones"),
        ("factura_iberdrola_MAL", "IBERDROLA CLIENTES S.A.U.", "A95758389",
         "IB-2026-99001", "2026-07-20", "200.00", "Suministros"),
    ],
)
def test_pdf_invoices_identify_issuer_not_customer(
    sample_pdfs, stem, supplier, tax_id, number, invoice_date, total, category,
):
    result = extract_invoice(sample_pdfs[stem])
    fields = values(result)

    assert result["is_invoice"] is True
    assert fields["supplier_name"] == supplier
    assert fields["supplier_tax_id"] == tax_id
    assert fields["customer_name"] == "PYME EJEMPLO S.L."
    assert fields["customer_tax_id"] == "B87654321"
    assert fields["invoice_number"] == number
    assert fields["invoice_date"] == invoice_date
    assert fields["total"] == total
    assert fields["category"] == category


def test_non_invoice_document_is_not_an_invoice(sample_pdfs):
    result = extract_invoice(sample_pdfs["requerimiento_aeat_2026"])

    assert result["is_invoice"] is False


def test_labelled_blocks_with_due_date_and_withholding():
    fields = values(extract_invoice(FIXTURES_DIR / "bloques.txt"))

    assert fields["supplier_name"] == "ASESORES REUNIDOS DEL NORTE S.L."
    assert fields["supplier_tax_id"] == "B95123456"
    assert fields["customer_name"] == "TALLERES GARCIA S.L."
    # No debe confundir la fecha de factura con un número ni con el vencimiento.
    assert fields["invoice_number"] == "2026/0145"
    assert fields["invoice_date"] == "2026-09-03"
    assert fields["due_date"] == "2026-10-03"
    assert fields["withholding_total"] == "45.00"
    assert fields["total"] == "318.00"
    assert fields["category"] == "Servicios profesionales"


def test_customer_block_before_issuer():
    fields = values(extract_invoice(FIXTURES_DIR / "cliente_primero.txt"))

    assert fields["supplier_name"] == "TOTALENERGIES CLIENTES S.A.U."
    assert fields["supplier_tax_id"] == "A12345674"
    assert fields["customer_name"] == "HOSTELERIA LOPEZ S.L."
    assert fields["invoice_number"] == "TE-99812"
    assert fields["concept"] == "Suministro gas natural julio 2026"


def test_category_accounts_follow_spanish_chart_of_accounts():
    assert category_account("Servicios profesionales") == "623"
    assert category_account("Arrendamientos") == "621"
    assert category_account("Categoría inventada") == "629"
    assert category_account(None) == "629"
