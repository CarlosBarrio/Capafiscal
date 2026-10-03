"""Proactividad: conciliación con sus excepciones, posición fiscal continua y obligaciones incompletas."""
from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal

from tests.workflows.test_scenarios import add_invoice

COMPANY = {"name": "Taller Barrio S.L.", "tax_id": "B12345674", "legal_form": "SOCIEDAD", "email": "admin@taller.es"}
# Trimestre cerrado cuyo plazo vence el 20 del mes siguiente.
Q_START, Q_END = date(2026, 7, 1), date(2026, 9, 30)


def import_csv(client, rows: list[tuple[date, str, str]]):
    csv = "Fecha;Concepto;Importe\n" + "".join(f"{when:%d/%m/%Y};{concept};{amount}\n" for when, concept, amount in rows)
    response = client.post("/api/bank/import", files={"uploaded_file": ("extracto.csv", csv.encode(), "text/csv")})
    response.raise_for_status()
    return response.json()


def setup_bank_case(client):
    from app.database import SessionLocal

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        ids = {
            "limpieza": add_invoice(database, supplier="LIMPIEZAS SOL S.L.", tax_id="B11111110", number="LS-0815", total=484.00, when=date(2026, 8, 15)).id,
            "portes": add_invoice(database, supplier="TRANSPORTES RIO S.L.", tax_id="B22222220", number="TR-091", total=968.00, when=date(2026, 8, 12)).id,
        }
        database.commit()
    report = import_csv(client, [
        (date(2026, 9, 1), "TRANSFERENCIA A TRANSPORTES RIO SL TR-091", "-986,00"),
        (date(2026, 9, 2), "RECIBO LIMPIEZAS SOL LS-0815", "-484,00"),
        (date(2026, 9, 3), "RECIBO LIMPIEZAS SOL LS-0815", "-484,00"),
        (date(2026, 9, 8), "TRANSFERENCIA A DESCONOCIDO SL", "-2350,00"),
    ])
    return ids, report


def test_reconciliation_classifies_every_movement(client):
    ids, imported = setup_bank_case(client)
    assert imported["auto_matched"] == 1  # el recibo con nombre, número e importe exacto

    report = client.get("/api/bank/reconciliation").json()
    states = {row["description"] + row["date"]: row for row in report["movements"]}
    assert states["RECIBO LIMPIEZAS SOL LS-08152026-09-02"]["state"] == "CONCILIADO"
    assert states["RECIBO LIMPIEZAS SOL LS-08152026-09-03"]["state"] == "DUPLICADO"
    portes = states["TRANSFERENCIA A TRANSPORTES RIO SL TR-0912026-09-01"]
    assert portes["state"] == "IMPORTE_DISTINTO" and portes["invoice_id"] == ids["portes"] and portes["difference"] == 18.0
    assert states["TRANSFERENCIA A DESCONOCIDO SL2026-09-08"]["state"] == "SIN_FACTURA"


def test_detector_turns_reconciliation_exceptions_into_findings(client):
    from app.agents.detector import run_anomaly_scan
    from app.database import SessionLocal

    setup_bank_case(client)
    with SessionLocal() as database:
        run_anomaly_scan(database, today=date(2026, 10, 1))
        database.commit()
    procedures = {item["procedure"] for item in client.get("/api/cases", params={"view": "anomalies"}).json()}
    assert {"IMPORTE_DIFERENTE", "PAGO_DUPLICADO", "PAGO_SIN_FACTURA"} <= procedures


def test_fiscal_position_says_how_the_quarter_is_going(client):
    from app.database import SessionLocal
    from app.fiscal_position import position

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        for month in (4, 5, 6, 7, 8):  # proveedor mensual… que en septiembre no ha facturado
            add_invoice(database, supplier="ALQUILERES NORTE S.L.", tax_id="B33333330", number=f"AN-{month}", total=605.00, when=date(2026, month, 5))
        pending = add_invoice(database, supplier="SUMINISTROS ESTE S.L.", tax_id="B44444440", number="SE-1", total=1210.00, when=date(2026, 9, 10))
        pending.review_status = "PENDING"
        database.commit()
    import_csv(client, [(date(2026, 9, 20), "PAGO TARJETA FERRETERIA", "-150,00")])

    with SessionLocal() as database:
        item = position(database, "303", 2026, 3, today=date(2026, 10, 5))
    types = sorted(gap["type"] for gap in item["gaps"])
    assert types == ["factura_falta", "movimiento_sin_factura", "pendiente_revision"]
    assert 0 < item["information_available"] < 1
    assert item["status"] == "INCOMPLETE" and item["result_with_pending"] is not None
    assert "% de información disponible" in item["summary"] and item["headline"].startswith("303 estimado:")


def test_incomplete_obligation_is_flagged_and_closes_itself(client):
    from app.agents.detector import run_anomaly_scan
    from app.database import SessionLocal
    from app.models import Case
    from app.models import Invoice

    client.put("/api/company", json=COMPANY)
    with SessionLocal() as database:
        invoice = add_invoice(database, supplier="SUMINISTROS ESTE S.L.", tax_id="B44444440", number="SE-2", total=1210.00, when=date(2026, 9, 10))
        invoice.review_status = "PENDING"
        database.commit()
        invoice_id = invoice.id

    today = date(2026, 10, 5)
    with SessionLocal() as database:
        run_anomaly_scan(database, today=today)
        database.commit()
        case = database.query(Case).filter(Case.procedure == "OBLIGACION_INCOMPLETA").one()
        assert case.status == "WAITING_HUMAN" and "303" in case.title
        assert "sin revisar" in case.summary

        # Se revisa la factura: la obligación queda completa y el aviso se cierra solo.
        database.get(Invoice, invoice_id).review_status = "APPROVED"
        database.commit()
        run_anomaly_scan(database, today=today + timedelta(days=1))
        database.commit()
        assert database.query(Case).filter(Case.procedure == "OBLIGACION_INCOMPLETA").one().status == "RESOLVED"
