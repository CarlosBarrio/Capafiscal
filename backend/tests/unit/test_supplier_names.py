"""Auditoría de proveedores: un domicilio nunca es el nombre del emisor."""
from __future__ import annotations

import pytest

from app.extractor import company_name_near_tax_id
from app.extractor import looks_like_address
from app.extractor import looks_like_company_name

HEADER = """{name}
{name}
{address} · NIF {nif}
FACTURA
Nº factura X-1
Cliente
Razón social Talleres Ejemplo del Arlanza S.L.
NIF B00100016
Domicilio Pol. Ind. Ficticio, parcela 7, 09001 Burgos
"""


@pytest.mark.parametrize("name, address, nif", [
    ("Suministros Ficticios Duero S.L.", "C/ Inventada 12, 47001 Valladolid", "B00200022"),
    ("Transportes Simulados Esla S.L.", "Av. Imaginaria 4, 24001 León", "B00300038"),
    ("Laura Inventada Martín", "C/ Ejemplo 3, 2º, 09002 Burgos", "00012345V"),
    ("Programas Ficticios Vena S.L.", "C/ Simulada 21, 28001 Madrid", "B00500058"),
])
def test_name_not_address_when_nif_shares_line_with_address(name, address, nif):
    found = company_name_near_tax_id(HEADER.format(name=name, address=address, nif=nif), nif, "supplier")
    assert found.value == name


@pytest.mark.parametrize("text", ["C/ Inventada 12, 47001 Valladolid ·", "Avda. de la Paz 3", "Pol. Ind. Ficticio, parcela 7, 09001 Burgos", "Paseo Fluvial 2, 3º"])
def test_addresses_are_not_company_names(text):
    assert looks_like_address(text) and not looks_like_company_name(text)


@pytest.mark.parametrize("text", ["Plaza Mayor S.L.", "Transportes 2000 S.A.", "Laura Inventada Martín", "Vía Rápida Logística S.L."])
def test_company_names_are_kept(text):
    assert looks_like_company_name(text)
