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


def test_word_html_and_xml_are_accepted_and_classified(client, tmp_path):
    """Un pedido en HTML o XML y una oferta en RTF se leen y van a su sitio; un HTML subido nunca se sirve como página."""
    html_order = write(tmp_path, "pedido.htm", "<html><body><h1>PEDIDO Nº 4500123</h1><script>alert(1)</script>"
                       "<p>Condiciones generales de compra</p><p>Total 121,00</p><p>SIMULACIÓN — NO OFICIAL</p></body></html>")
    xml_order = write(tmp_path, "pedido.xml", '<?xml version="1.0" encoding="UTF-8"?><Pedido><Titulo>PEDIDO Nº 4500124</Titulo>'
                      "<Total>121,00</Total></Pedido>")
    rtf_offer = tmp_path / "oferta.rtf"
    rtf_offer.write_bytes(rb"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 OFERTA N\'ba 12/26\par Honorarios 1.000,00\par IVA 21 % 210,00\par}")

    kinds = [upload(client, path)["document"]["kind"] for path in (html_order, xml_order, rtf_offer)]
    assert kinds == ["PEDIDO", "PEDIDO", "PRESUPUESTO"]

    document_id = client.get("/api/documents").json()[-1]["id"]
    original = client.get(f"/api/documents/{document_id}/file")
    assert original.headers["content-type"].startswith("application/octet-stream")
    preview = client.get(f"/api/documents/{document_id}/file", params={"as_text": True})
    assert preview.headers["content-type"].startswith("text/plain")
    assert "PEDIDO Nº 4500123" in preview.text and "alert" not in preview.text


def test_a_person_puts_a_document_in_its_place(client, tmp_path):
    """«Es un pedido» sobre algo leído como factura: deja de ser factura, va a Documentos y ahí se queda al releerlo."""
    document = upload(client, write(tmp_path, "factura.txt", "TALLERES EJEMPLO S.L.\nFACTURA Nº 12" + BODY))["document"]
    assert document["invoice"] is not None

    response = client.post(f"/api/documents/{document['id']}/classify", json={"kind": "PEDIDO"})

    assert response.status_code == 200
    reread = client.get(f"/api/documents/{document['id']}").json()
    assert (reread["kind"], reread["status"], reread["invoice"]) == ("PEDIDO", "CLASSIFIED", None)
    assert client.get("/api/tasks/review-inbox").json() == []
    client.post(f"/api/documents/{document['id']}/reprocess")
    assert client.get(f"/api/documents/{document['id']}").json()["kind"] == "PEDIDO"
    assert client.post(f"/api/documents/{document['id']}/classify", json={"kind": "CONTRATO"}).status_code == 422


def test_documents_read_with_older_rules_are_put_in_their_place(client, tmp_path):
    """Una base de antes: un albarán guardado como factura por revisar y una nómina convertida en notificación de la
    Seguridad Social. Al arrancar (o con «Reprocesar pendientes») cada uno va a su sitio con el texto ya leído; la
    notificación falsa se cierra. Lo que hizo una persona no se toca."""
    from app.database import SessionLocal
    from app.invoice_service import reclassify_stored_documents
    from app.models import Document
    from app.models import FiscalNotification
    from app.models import Invoice
    from app.notification_service import create_notification

    note = upload(client, write(tmp_path, "albaran.txt", "TRANSPORTES EJEMPLO S.L.\nALBARÁN Nº AE-55120" + BODY))["document"]
    payslip = upload(client, write(tmp_path, "nomina.txt",
                                   "EMPRESA EJEMPLO S.L.\nI. DEVENGOS\nA. TOTAL DEVENGADO 1.500,00\nB. TOTAL A DEDUCIR 300,00\n"
                                   "LÍQUIDO TOTAL A PERCIBIR 1.200,00\nDETERMINACIÓN DE LAS BASES DE COTIZACIÓN A LA "
                                   "SEGURIDAD SOCIAL\nSIMULACIÓN — NO OFICIAL\n"))["document"]
    person = upload(client, write(tmp_path, "oferta.txt", "ESTUDIO EJEMPLO S.L.\nOFERTA Nº 12/26" + BODY))["document"]
    client.post(f"/api/documents/{person['id']}/reprocess", params={"as_invoice": True})

    with SessionLocal() as database:  # como lo dejaban las reglas anteriores
        old_note = database.get(Document, note["id"])
        old_note.kind, old_note.status = None, "NEEDS_REVIEW"
        database.add(Invoice(document=old_note, supplier_name="ALBARÁN", total=121))
        old_payslip = database.get(Document, payslip["id"])
        old_payslip.kind, old_payslip.status = None, "NEEDS_REVIEW"
        create_notification(database, document=old_payslip, data={"issuer": "TGSS", "notification_type": "OTRO", "title": "Otro"}, actor="extractor")
        database.commit()

    with SessionLocal() as database:
        assert reclassify_stored_documents(database) == 2
        database.commit()

    documents = {item["id"]: item for item in client.get("/api/documents").json()}
    assert (documents[note["id"]]["kind"], documents[note["id"]]["invoice"]) == ("ALBARAN", None)
    assert documents[payslip["id"]]["kind"] == "NOMINA"
    assert documents[person["id"]]["kind"] == "INVOICE"  # la persona dijo que era factura
    with SessionLocal() as database:
        assert database.query(FiscalNotification).filter_by(document_id=payslip["id"]).one().status == "CLOSED"


def test_a_zip_puts_every_document_in_its_place(client, tmp_path):
    """Se sube un .zip y cada documento va a su sitio; lo que no se puede leer se dice, no se pierde en silencio."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facturas/factura-12.txt", "TALLERES EJEMPLO S.L.\nFACTURA Nº 12" + BODY)
        archive.writestr("albaranes/albaran.txt", "TRANSPORTES EJEMPLO S.L.\nALBARÁN Nº AE-55120" + BODY)
        archive.writestr("pedidos/pedido.htm", "<h1>PEDIDO Nº 4500123</h1><p>Condiciones generales de compra</p>")
        archive.writestr("otros/foto.jpg", b"\xff\xd8\xff")
        archive.writestr("__MACOSX/._factura-12.txt", b"x")
    response = client.post("/api/upload-zip", files={"uploaded_file": ("lote.zip", buffer.getvalue())})

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["counts"] == {"INVOICE": 1, "ALBARAN": 1, "PEDIDO": 1}
    assert [item["file"] for item in result["skipped"]] == ["foto.jpg"]
    assert client.post("/api/upload-zip", files={"uploaded_file": ("x.zip", b"no es un zip")}).status_code == 400
