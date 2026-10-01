"""DEHú → adaptador → evento → expediente, con las fechas oficiales."""
from __future__ import annotations

import base64
import json
from datetime import date

from evaluation import plantillas as P
from evaluation.documentos import render_admin
from evaluation.plantillas import Party
from evaluation.plantillas import business_days_after

COMPANY = Party("Talleres Ejemplo del Arlanza S.L.", "B00100016", "Pol. Ind. Ficticio 7, Burgos")


def requerimiento_pdf(tmp_path) -> bytes:
    blocks = P.requerimiento_aeat(party=COMPANY, expediente="202609SIM0007777Q", documento="A23SIM7", emitido=date(2026, 9, 23),
                                  impuesto="Impuesto sobre el Valor Añadido", ejercicio=2026, periodo="2T", pide=["Libro registro de facturas recibidas."],
                                  puesta=None, acceso=None)  # el PDF no dice cuándo se notificó
    path = render_admin(tmp_path / "req.pdf", organism="AEAT", area="Gestión Tributaria", blocks=blocks, seed="dehu")
    return path.read_bytes()


def setup(client):
    client.put("/api/company", json={"name": COMPANY.name, "tax_id": COMPANY.tax_id, "legal_form": "SOCIEDAD", "email": "a@b.test"})


def test_dehu_dates_make_the_deadline_exact(client, tmp_path):
    setup(client)
    payload = {"identifier": "N-2026-000123", "issuer": "Agencia Estatal de Administración Tributaria", "subject": "Requerimiento de información",
               "available_at": "2026-09-25", "accessed_at": "2026-09-28", "pdf_base64": base64.b64encode(requerimiento_pdf(tmp_path)).decode()}
    first = client.post("/api/connectors/dehu/import", json=payload).json()
    assert first["case_code"] and not first["duplicate"]
    case = client.get(f"/api/cases/{first['case_id']}").json()
    assert case["deadline"] == business_days_after(date(2026, 9, 28), 10).isoformat()  # 13/10: el 12 es festivo
    assert "ESTIMADO" not in (case["facts"].get("deadline_rule") or "")

    again = client.post("/api/connectors/dehu/import", json=payload).json()
    assert again["duplicate"] is True and len(client.get("/api/cases").json()) == 1


def test_metadata_only_notification_is_not_lost(client):
    setup(client)
    result = client.post("/api/connectors/dehu/import", json={
        "identifier": "N-2026-000124", "issuer": "Tesorería General de la Seguridad Social", "subject": "Providencia de apremio",
        "available_at": "2026-09-29",
    }).json()
    case = client.get(f"/api/cases/{result['case_id']}").json()
    assert case["organism"] == "TGSS" and case["procedure"] == "APREMIO"


def test_folder_transport_processes_downloads_once(client, tmp_path):
    from app.connectors.dehu.client import FolderTransport
    from app.connectors.dehu.client import poll
    from app.database import SessionLocal

    setup(client)
    inbox = tmp_path / "dehu"
    inbox.mkdir()
    (inbox / "N-1.json").write_text(json.dumps({"identifier": "N-1", "issuer": "AEAT", "subject": "Requerimiento", "accessed_at": "2026-09-28"}))
    (inbox / "N-1.pdf").write_bytes(requerimiento_pdf(tmp_path))
    with SessionLocal() as database:
        first = poll(database, FolderTransport(inbox))
        second = poll(database, FolderTransport(inbox))
    assert first["new"] == 1 and first["cases"] and second["duplicates"] == 1
