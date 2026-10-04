"""Documentos que no son factura ni notificación, de punta a punta por la API.

Un albarán, un presupuesto, un pedido o una nómina no se registran como gasto, no se convierten en notificación
aunque citen a un organismo, no piden revisión y quedan a la vista con su tipo. Si una persona dice que sí es una
factura, se lee como factura. Datos sintéticos (SIMULACIÓN — NO OFICIAL).
"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import upload

BODY = "\nCIF B00001016\nFecha 01/09/2026\nBase imponible 100,00\nIVA 21 % 21,00\nTotal 121,00\nSIMULACIÓN — NO OFICIAL\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_delivery_note_is_archived_with_its_type(client, tmp_path):
    payload = upload(client, write(tmp_path, "albaran.txt", "TRANSPORTES EJEMPLO S.L.\nALBARÁN Nº AE-55120" + BODY))
    document = payload["document"]

    assert document["kind"] == "ALBARAN"
    assert document["status"] == "CLASSIFIED"
    assert document["invoice"] is None
    # Ni en la lista de facturas, ni notificación, ni tarea de revisión
    assert client.get("/api/documents", params={"kind": "INVOICE"}).json() == []
    assert client.get("/api/notifications").json() == []
    assert client.get("/api/tasks/review-inbox").json() == []
    # Trazabilidad: queda dicho por qué
    audit = client.get(f"/api/documents/{document['id']}/audit").json()
    assert any(event["action"] == "document.classified_not_invoice" for event in audit)


def test_a_quote_that_mentions_an_authority_is_not_a_notification(client, tmp_path):
    """Una oferta que cita «requerimiento» y un ayuntamiento, o una nómina que cita la Seguridad Social, no son
    actos de un organismo: su propio título o estructura manda."""
    quote = ("ESTUDIO EJEMPLO S.L.\nOFERTA DE HONORARIOS TÉCNICOS Nº 12/26\nTramitación ante el Ayuntamiento de Ejemplo "
             "y atención a cualquier requerimiento de subsanación." + BODY)
    payslip = (
        "EMPRESA EJEMPLO S.L. CIF B00001016\nI. DEVENGOS\nA. TOTAL DEVENGADO 1.500,00\nII. DEDUCCIONES\n"
        "B. TOTAL A DEDUCIR 300,00\nLÍQUIDO TOTAL A PERCIBIR (A-B) 1.200,00\n"
        "DETERMINACIÓN DE LAS BASES DE COTIZACIÓN A LA SEGURIDAD SOCIAL Y CONCEPTOS DE RECAUDACIÓN\nSIMULACIÓN — NO OFICIAL\n"
    )
    kinds = [upload(client, write(tmp_path, name, text))["document"]["kind"]
             for name, text in (("oferta.txt", quote), ("nomina.txt", payslip))]

    assert kinds == ["PRESUPUESTO", "NOMINA"]
    assert client.get("/api/notifications").json() == []


def test_a_person_can_say_it_is_an_invoice(client, tmp_path):
    document = upload(client, write(tmp_path, "pedido.txt", "EMPRESA EJEMPLO S.L.\nPEDIDO Nº 4500123" + BODY))["document"]
    assert document["kind"] == "PEDIDO"

    response = client.post(f"/api/documents/{document['id']}/reprocess", params={"as_invoice": True})

    assert response.status_code == 200 and response.json()["success"]
    reread = client.get(f"/api/documents/{document['id']}").json()
    assert reread["kind"] == "INVOICE"
    assert reread["invoice"] is not None and float(reread["invoice"]["total"]) == 121.0
    assert reread["status"] in {"NEEDS_REVIEW", "READY_FOR_APPROVAL"}
    audit = client.get(f"/api/documents/{document['id']}/audit").json()
    assert any(event["action"] == "document.marked_as_invoice" for event in audit)
    # Volver a leerlo después no lo devuelve a «pedido»: la decisión de la persona se mantiene
    client.post(f"/api/documents/{document['id']}/reprocess")
    assert client.get(f"/api/documents/{document['id']}").json()["kind"] == "INVOICE"


def test_an_invoice_is_still_an_invoice(client, tmp_path):
    document = upload(client, write(tmp_path, "factura.txt", "TALLERES EJEMPLO S.L.\nAlbarán: 4471\nFACTURA Nº 12" + BODY))["document"]

    assert document["kind"] in (None, "INVOICE")
    assert document["invoice"] is not None
