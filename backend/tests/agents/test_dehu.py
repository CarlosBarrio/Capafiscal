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


def test_a_stray_or_broken_file_in_the_inbox_does_not_block_the_rest(client, tmp_path):
    """Un .json que no es una notificación (o está dañado) se salta con aviso; los demás se procesan."""
    from app.connectors.dehu.client import FolderTransport
    from app.connectors.dehu.client import poll
    from app.database import SessionLocal

    setup(client)
    inbox = tmp_path / "dehu"
    inbox.mkdir()
    (inbox / "notas.json").write_text(json.dumps({"comentario": "no es una notificación"}))
    (inbox / "roto.json").write_text("{ esto no es json")
    (inbox / "N-9.json").write_text(json.dumps({"identifier": "N-9", "issuer": "AEAT", "subject": "Requerimiento", "accessed_at": "2026-09-28"}))
    with SessionLocal() as database:
        database.info["tenant_id"] = 0
        result = poll(database, FolderTransport(inbox))
    assert result["new"] == 1 and {item["file"] for item in result["skipped"]} == {"notas.json", "roto.json"}


def test_metadata_only_notifications_still_say_whether_they_need_action(client):
    setup(client)
    result = client.post("/api/connectors/dehu/import", json={
        "identifier": "N-2026-000200", "issuer": "Tesorería General de la Seguridad Social", "subject": "Providencia de apremio", "accessed_at": "2026-10-05",
    }).json()
    notification = next(row for row in client.get("/api/notifications", params={"open_only": "false"}).json() if row["reference"] == "N-2026-000200")
    assert notification["classification"]["action"] == "ACTION_REQUIRED" and result["case_id"]
    assert notification["deadline"] == "2026-10-20"  # LGT 62.5: notificada el 5 → hasta el día 20


def test_synthetic_dehu_dataset_end_to_end(client, monkeypatch):
    """El evaluador recorre carpeta → adaptador → expediente y compara con la verdad calculada con reglas legales."""
    from pathlib import Path

    from app.config import settings
    from evaluation import dehu

    monkeypatch.setattr(settings, "dehu_inbox_dir", settings.dehu_inbox_dir)  # el evaluador lo cambia; se restaura al acabar
    report = dehu.run(Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "dehu_sintetico")
    failures = [row["id"] for row in report["rows"] if not row["perfect"]]
    assert report["processed"] == report["notifications"] == 7 and not failures, dehu.to_markdown(report)
    assert report["new_on_second_poll"] == 0 and report["duplicates_on_second_poll"] == 7
