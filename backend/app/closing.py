"""
Cierre administrativo del mes: «déjame la administración al día».

No es otro agente: orquesta lo que ya existe y lo resume en una sola respuesta.

    run_close()      CapaFiscal trabaja: concilia lo seguro, pasa el Detector, recalcula perfiles
    evaluate()       SOLO LEE: diez comprobaciones del mes, cada una ok / aviso / bloquea,
                     con su detalle y adónde ir para resolverla
    close()          lo cierra una persona (con nota obligatoria si quedan bloqueos: «con salvedades»)

% cerrado = elementos resueltos / elementos del mes, a la vista en la propia respuesta:
    facturas del mes revisadas + movimientos del banco conciliados o justificados
    + documentos esperados que han llegado
No es una puntuación inventada: se puede contar a mano.
"""
from __future__ import annotations

from app import clock
import calendar
from collections import Counter
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction
from app.models import Case
from app.models import Invoice
from app.models import PeriodClose
from app.models import SalesInvoice

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
MIN_UNJUSTIFIED = Decimal("50")  # un pago menor sin factura (comisiones…) avisa pero no bloquea


def parse_period(period: str) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in period.split("-"))
        start = date(year, month, 1)
    except (ValueError, TypeError) as error:
        raise ValueError("El periodo debe ser AAAA-MM.") from error
    return start, date(year, month, calendar.monthrange(year, month)[1])


def period_label(period: str) -> str:
    start, _ = parse_period(period)
    return f"{MONTHS[start.month - 1]} {start.year}"


def eur(value: Any) -> str:
    text = f"{Decimal(str(value or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def check(key: str, label: str, status: str, detail: str, *, count: int | None = None, action: dict[str, Any] | None = None,
          items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail, "count": count, "action": action, "items": items or []}


def evaluate(database: Session, period: str, *, today: date | None = None) -> dict[str, Any]:
    """Las comprobaciones del mes. No escribe nada."""
    from app.fiscal_position import missing_recurring
    from app.fiscal_position import position
    from app.reconciliation import reconcile

    today = today or clock.today()
    start, end = parse_period(period)
    checks: list[dict[str, Any]] = []
    units_total = units_done = 0

    invoices = database.scalars(select(Invoice).where(Invoice.invoice_date.between(start, end), Invoice.review_status != "REJECTED")).all()
    received = [item for item in invoices if item.direction != "ISSUED"]
    issued = [item for item in invoices if item.direction == "ISSUED"]

    # 1 y 2. Facturas recibidas y emitidas revisadas
    for key, label, rows in (("recibidas", "Facturas recibidas", received), ("emitidas", "Facturas emitidas", issued)):
        pending = [item for item in rows if item.review_status != "APPROVED"]
        units_total += len(rows)
        units_done += len(rows) - len(pending)
        if pending:
            checks.append(check(key, label, "block", f"{len(pending)} de {len(rows)} sin revisar", count=len(pending),
                                action={"tab": "facturas", "label": "Revisar facturas"},
                                items=[{"label": f"{item.invoice_number or 's/n'} · {item.supplier_name if key == 'recibidas' else item.customer_name or ''}",
                                        "amount": float(item.total or 0), "invoice_id": item.id, "document_id": item.document_id} for item in pending[:10]]))
        else:
            checks.append(check(key, label, "ok", f"{len(rows)} revisada(s)" if rows else "Ninguna en el mes", count=len(rows)))
    drafts = database.scalars(select(SalesInvoice).where(SalesInvoice.status == "DRAFT", SalesInvoice.issue_date.between(start, end))).all()
    if drafts:
        checks[-1]["status"] = "warn" if checks[-1]["status"] == "ok" else checks[-1]["status"]
        checks[-1]["detail"] += f" · {len(drafts)} borrador(es) de venta sin emitir"

    # 3. El extracto cubre el mes
    last_booking = database.scalar(select(BankTransaction.booking_date).order_by(BankTransaction.booking_date.desc()).limit(1))
    if last_booking is None:
        checks.append(check("extracto", "Extracto del banco", "block", "No hay movimientos del banco: importa el extracto o conecta el banco",
                            action={"tab": "negocio", "anchor": "bankCard", "label": "Ir al banco"}))
    elif last_booking < end:
        checks.append(check("extracto", "Extracto del banco", "block", f"Llega hasta el {last_booking:%d/%m/%Y}: falta hasta el {end:%d/%m/%Y}",
                            action={"tab": "negocio", "anchor": "bankCard", "label": "Importar extracto"}))
    else:
        checks.append(check("extracto", "Extracto del banco", "ok", f"Cubre todo el mes (último movimiento {last_booking:%d/%m/%Y})"))

    # 4. Conciliación de los movimientos del mes
    report = reconcile(database, today=today, auto=False, persist=False)
    month_rows = [row for row in report["movements"] if start.isoformat() <= row["date"] <= end.isoformat() and row["state"] != "IGNORADO"]
    states = Counter(row["state"] for row in month_rows)
    levels = Counter(row.get("level") for row in month_rows)
    unjustified = [row for row in month_rows if row["state"] == "SIN_FACTURA" and abs(Decimal(str(row["amount"]))) >= MIN_UNJUSTIFIED]
    small = [row for row in month_rows if row["state"] == "SIN_FACTURA" and abs(Decimal(str(row["amount"]))) < MIN_UNJUSTIFIED]
    conflicts = [row for row in month_rows if row.get("level") == "CONFLICTO"]
    done_movements = states.get("CONCILIADO", 0) + len(small)
    units_total += len(month_rows)
    units_done += done_movements
    if conflicts or unjustified:
        parts = ([f"{len(conflicts)} en conflicto"] if conflicts else []) + ([f"{len(unjustified)} sin justificar"] if unjustified else [])
        checks.append(check("conciliacion", "Conciliación", "block", f"{done_movements} de {len(month_rows)} conciliados · " + " · ".join(parts),
                            count=len(conflicts) + len(unjustified), action={"tab": "negocio", "anchor": "bankCard", "label": "Resolver en el banco"},
                            items=[{"label": f"{row['date'][8:10]}/{row['date'][5:7]} · {row['description'][:60]}", "amount": row["amount"],
                                    "transaction_id": row["transaction_id"], "why": row.get("decision")} for row in (conflicts + unjustified)[:10]]))
    elif levels.get("PROBABLE") or levels.get("SEGURO", 0) > states.get("CONCILIADO", 0):
        checks.append(check("conciliacion", "Conciliación", "warn", f"{done_movements} de {len(month_rows)} conciliados · el resto con propuesta pendiente de confirmar",
                            action={"tab": "negocio", "anchor": "bankCard", "label": "Confirmar propuestas"}))
    else:
        checks.append(check("conciliacion", "Conciliación", "ok", f"{done_movements} de {len(month_rows)} movimientos conciliados o justificados"))

    # 5. Pagos y cobros vencidos sin movimiento
    unpaid = [row for row in report["unpaid_invoices"] if row["due"] <= end.isoformat()]
    if unpaid:
        checks.append(check("pagos", "Pagos y cobros vencidos", "warn", f"{len(unpaid)} factura(s) vencidas sin pago en el banco ({eur(sum(row['amount'] for row in unpaid))})",
                            count=len(unpaid), action={"tab": "negocio", "anchor": "bankCard", "label": "Ver"},
                            items=[{"label": row["invoice_label"], "amount": row["amount"], "invoice_id": row["invoice_id"]} for row in unpaid[:10]]))
    else:
        checks.append(check("pagos", "Pagos y cobros vencidos", "ok", "Nada vencido sin movimiento"))

    # 6. Documentos que faltan: facturas habituales que no han llegado y documentación pedida
    quarter = (start.month - 1) // 3 + 1
    missing = [item for item in missing_recurring(database, start.year, quarter, max(today, end)) if item["month"] == period]
    from app.models import DocumentRequest

    requested = database.scalars(select(DocumentRequest).where(DocumentRequest.status == "PENDING")).all()
    units_total += len(missing)
    if missing:
        checks.append(check("documentos", "Documentos que faltan", "block", f"No ha llegado la factura habitual de {len(missing)} {'proveedor' if len(missing) == 1 else 'proveedores'}",
                            count=len(missing), action={"tab": "facturas", "label": "Subir o pedir"},
                            items=[{"label": item["label"], "amount": item["usual"]} for item in missing]))
    elif requested:
        checks.append(check("documentos", "Documentos que faltan", "warn", f"{len(requested)} {'documento pedido' if len(requested) == 1 else 'documentos pedidos'} sin respuesta (el Perseguidor insiste)",
                            count=len(requested), action={"tab": "expedientes", "label": "Ver expedientes"}))
    else:
        checks.append(check("documentos", "Documentos que faltan", "ok", "Han llegado las facturas habituales del mes"))

    # 7. Duplicados
    duplicates = [item for item in invoices if item.duplicate_status not in (None, "NONE") and item.review_status != "APPROVED"]
    if duplicates:
        checks.append(check("duplicados", "Duplicados", "block", f"{len(duplicates)} posible(s) factura(s) duplicada(s) sin decidir", count=len(duplicates),
                            action={"tab": "facturas", "label": "Revisar"}, items=[{"label": f"{item.invoice_number} · {item.supplier_name}", "invoice_id": item.id, "document_id": item.document_id} for item in duplicates]))
    else:
        checks.append(check("duplicados", "Duplicados", "ok", "0 duplicados"))

    # 8. IVA del mes
    approved = [item for item in invoices if item.review_status == "APPROVED"]
    invalid = [item for item in approved if item.validation_status not in (None, "VALID")]
    output_vat = sum((Decimal(str(item.tax_total or 0)) for item in approved if item.direction == "ISSUED"), Decimal("0"))
    input_vat = sum((Decimal(str(item.tax_total or 0)) for item in approved if item.direction != "ISSUED"), Decimal("0"))
    vat_detail = f"repercutido {eur(output_vat)} · soportado {eur(input_vat)} · diferencia {eur(output_vat - input_vat)}"
    if invalid:
        checks.append(check("iva", "IVA", "block", f"{len(invalid)} factura(s) aprobadas con importes que no cuadran · {vat_detail}", count=len(invalid),
                            action={"tab": "facturas", "label": "Revisar"}))
    else:
        checks.append(check("iva", "IVA", "ok", f"Cuadrado · {vat_detail}"))

    # 9. Anomalías abiertas
    anomalies = database.scalars(select(Case).where(Case.kind == "ANOMALY", Case.status.in_(("OPEN", "WAITING_HUMAN", "WAITING_DOCS")))).all()
    serious = [case for case in anomalies if case.level in ("critical", "high")]
    if serious:
        checks.append(check("anomalias", "Anomalías", "block", f"{len(serious)} grave(s) sin decidir · {len(anomalies)} abiertas en total", count=len(serious),
                            action={"tab": "expedientes", "view": "anomalies", "label": "Ver anomalías"},
                            items=[{"label": case.title, "case_id": case.id} for case in serious[:10]]))
    elif anomalies:
        checks.append(check("anomalias", "Anomalías", "warn", f"{len(anomalies)} aviso(s) abiertos, ninguno grave", count=len(anomalies),
                            action={"tab": "expedientes", "view": "anomalies", "label": "Ver anomalías"}))
    else:
        checks.append(check("anomalias", "Anomalías", "ok", "Ninguna abierta"))

    # 10. Impuestos (solo al cerrar un trimestre) e incidencias con plazo
    if start.month % 3 == 0:
        state = position(database, "303", start.year, quarter, today=today)
        detail = f"303 {state['period_label']}: {eur(abs(state['result']))} {state['outcome'].lower()} · {round(state['information_available'] * 100)} % de información · vence {date.fromisoformat(state['due_date']):%d/%m/%Y}"
        checks.append(check("impuestos", "Impuestos del trimestre", "ok" if state["status"] in ("FILED", "COMPLETE") else "warn", detail,
                            action={"tab": "impuestos", "label": "Ver posición"}))
    else:
        checks.append(check("impuestos", "Impuestos del trimestre", "ok", "Este mes no cierra trimestre"))
    overdue = database.scalars(select(Case).where(Case.kind == "NOTIFICATION", Case.status.in_(("OPEN", "WAITING_HUMAN", "WAITING_DOCS")), Case.deadline < today)).all()
    open_notifications = database.scalars(select(Case).where(Case.kind == "NOTIFICATION", Case.status.in_(("OPEN", "WAITING_HUMAN", "WAITING_DOCS")))).all()
    if overdue:
        checks.append(check("incidencias", "Requerimientos y notificaciones", "block", f"{len(overdue)} con el plazo vencido", count=len(overdue),
                            action={"tab": "expedientes", "label": "Ver expedientes"}, items=[{"label": case.title, "case_id": case.id} for case in overdue]))
    elif open_notifications:
        checks.append(check("incidencias", "Requerimientos y notificaciones", "warn", f"{len(open_notifications)} en curso, dentro de plazo", count=len(open_notifications),
                            action={"tab": "expedientes", "label": "Ver expedientes"}))
    else:
        checks.append(check("incidencias", "Requerimientos y notificaciones", "ok", "Ninguna abierta"))

    blockers = [item for item in checks if item["status"] == "block"]
    to_resolve = blocking_items(blockers)
    percent = round(units_done / units_total * 100) if units_total else 100
    reviewed = len(received) + len(issued) - sum(1 for item in received + issued if item.review_status != "APPROVED")
    summary = [
        {"text": f"{reviewed} de {len(received) + len(issued)} facturas revisadas", "ok": reviewed == len(received) + len(issued)},
        {"text": f"{done_movements} de {len(month_rows)} movimientos conciliados o justificados", "ok": done_movements == len(month_rows)},
        {"text": "IVA cuadrado" if not invalid else f"IVA: {len(invalid)} factura(s) no cuadran", "ok": not invalid},
        {"text": f"{len(duplicates)} duplicado(s)", "ok": not duplicates},
    ]
    return {
        "period": period, "label": period_label(period), "from": start.isoformat(), "to": end.isoformat(),
        "percent": percent, "units": {"done": units_done, "total": units_total,
                                      "formula": "facturas del mes revisadas + movimientos conciliados o justificados + documentos esperados recibidos"},
        "checks": checks, "blockers": len(blockers), "warnings": sum(item["status"] == "warn" for item in checks),
        "ready": not blockers, "summary": summary, "to_resolve": to_resolve,
        "headline": (f"{period_label(period).capitalize()} — {percent} % cerrado · "
                     + (f"{len(to_resolve)} {'cosa bloquea' if len(to_resolve) == 1 else 'cosas bloquean'} el cierre" if to_resolve else "listo para cerrar")),
    }


# Cómo se dice cada bloqueo, en concreto («Falta la factura de X», «Pago de 1.240 € sin justificar»).
BLOCK_TEXT = {
    "recibidas": lambda row: f"Factura {row['label']} sin revisar",
    "emitidas": lambda row: f"Factura emitida {row['label']} sin revisar",
    "conciliacion": lambda row: (f"{'Cobro' if row['amount'] > 0 else 'Pago'} de {eur(abs(row['amount']))} sin justificar · {row['label']}"
                                 if not (row.get("why") or "").startswith("No se concilia") else f"Movimiento en conflicto · {row['label']}"),
    "documentos": lambda row: f"Falta la factura de {row['label']}",
    "duplicados": lambda row: f"Posible factura duplicada: {row['label']}",
    "anomalias": lambda row: f"Anomalía sin decidir: {row['label']}",
    "incidencias": lambda row: f"Plazo vencido: {row['label']}",
}


def blocking_items(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los bloqueos, uno a uno, cada uno con su acción para resolverlo desde el cierre."""
    result = []
    for check_item in blockers:
        rows = check_item["items"] or [None]
        for row in rows:
            if row is None:
                text, action = check_item["detail"], dict(check_item["action"] or {})
            else:
                text = BLOCK_TEXT.get(check_item["key"], lambda value: value["label"])(row)
                action = ({"label": "Abrir", "document_id": row["document_id"]} if row.get("document_id") else
                          {"label": "Abrir", "case_id": row["case_id"]} if row.get("case_id") else dict(check_item["action"] or {}))
            result.append({"check": check_item["key"], "text": text, "amount": row.get("amount") if row else None, "action": action})
    return result


def record(database: Session, period: str) -> PeriodClose | None:
    return database.scalar(select(PeriodClose).where(PeriodClose.period == period))


def state(database: Session, period: str, *, today: date | None = None) -> dict[str, Any]:
    """Lo que ve la pantalla (solo lee)."""
    result = evaluate(database, period, today=today)
    stored = record(database, period)
    result["status"] = stored.status if stored else "OPEN"
    result["closed"] = ({"by": stored.closed_by, "at": stored.closed_at.isoformat() if stored.closed_at else None, "note": stored.note,
                         "percent": stored.percent, "blockers": (stored.snapshot or {}).get("blockers", 0)} if stored and stored.status == "CLOSED" else None)
    result["last_run_at"] = stored.last_run_at.isoformat() if stored and stored.last_run_at else None
    result["work"] = (stored.snapshot or {}).get("work") if stored else None
    return result


def upsert(database: Session, period: str) -> PeriodClose:
    stored = record(database, period)
    if stored is None:
        stored = PeriodClose(period=period, status="OPEN", snapshot={})
        database.add(stored)
    return stored


def run_close(database: Session, period: str, *, today: date | None = None, actor: str = "persona") -> dict[str, Any]:
    """CapaFiscal hace su parte del cierre y deja la foto del resultado."""
    from app.agents.detector import run_anomaly_scan
    from app.reconciliation import reconcile

    parse_period(period)
    today = today or clock.today()
    reconciled = reconcile(database, today=today, actor="cierre-mensual")
    scan = run_anomaly_scan(database, trigger="cierre", today=today)
    database.flush()
    result = evaluate(database, period, today=today)
    stored = upsert(database, period)
    work = {"auto_matched": reconciled["auto_matched"], "anomalies_created": scan.get("created", 0), "anomalies_closed": scan.get("closed", 0)}
    stored.percent, stored.last_run_at = result["percent"], clock.now()
    stored.snapshot = {"checks": [{key: item[key] for key in ("key", "status", "detail")} for item in result["checks"]], "blockers": result["blockers"], "work": work}
    from app.invoice_service import add_audit_event

    add_audit_event(database, action="close.run", entity_type="period_close", entity_id=period, actor=actor,
                    event_data={"percent": result["percent"], "blockers": result["blockers"], **work})
    database.flush()
    return {**state(database, period, today=today), "work": work}


def close(database: Session, period: str, *, actor: str, note: str | None = None, today: date | None = None) -> dict[str, Any]:
    result = evaluate(database, period, today=today)
    if result["blockers"] and not (note and note.strip()):
        raise ValueError(f"Quedan {result['blockers']} bloqueo(s): para cerrar con salvedades escribe el motivo.")
    stored = upsert(database, period)
    stored.status, stored.closed_by, stored.closed_at, stored.note, stored.percent = "CLOSED", actor, clock.now(), note, result["percent"]
    stored.snapshot = {**(stored.snapshot or {}), "checks": [{key: item[key] for key in ("key", "status", "detail")} for item in result["checks"]],
                       "blockers": result["blockers"]}
    from app.invoice_service import add_audit_event

    add_audit_event(database, action="close.closed", entity_type="period_close", entity_id=period, actor=actor,
                    event_data={"percent": result["percent"], "blockers": result["blockers"], "note": note})
    database.flush()
    return state(database, period, today=today)


def reopen(database: Session, period: str, *, actor: str) -> dict[str, Any]:
    stored = record(database, period)
    if stored is None or stored.status != "CLOSED":
        raise ValueError("Ese mes no está cerrado.")
    stored.status = "OPEN"
    from app.invoice_service import add_audit_event

    add_audit_event(database, action="close.reopened", entity_type="period_close", entity_id=period, actor=actor, event_data={})
    database.flush()
    return state(database, period)


def default_period(today: date | None = None) -> str:
    """El mes que toca cerrar: el anterior."""
    today = today or clock.today()
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year}-{month:02d}"


def history(database: Session) -> list[dict[str, Any]]:
    return [{"period": item.period, "label": period_label(item.period), "status": item.status, "percent": item.percent,
             "closed_at": item.closed_at.isoformat() if item.closed_at else None, "closed_by": item.closed_by, "note": item.note}
            for item in database.scalars(select(PeriodClose).order_by(PeriodClose.period.desc())).all()]


def report_pdf(database: Session, period: str, *, today: date | None = None) -> bytes:
    """Informe de cierre: lo comprobado, las cifras del mes, lo que hizo CapaFiscal y las salvedades. Solo lee."""
    import io

    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    from app.accounting import journal
    from app.models import CompanyProfile

    data = state(database, period, today=today)
    start, end = parse_period(period)
    company = database.scalar(select(CompanyProfile).limit(1))
    closed = data["status"] == "CLOSED"
    stored = record(database, period)
    snapshot = {item["key"]: item for item in ((stored.snapshot or {}).get("checks") or [])} if closed and stored else {}
    diary = journal(database, start, end)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin

    def line(text: str, size: float = 10, bold: bool = False, color: str = "#1c1914", gap: float = 4) -> None:
        nonlocal y
        font = "Helvetica-Bold" if bold else "Helvetica"
        words, current = text.split(), ""
        lines = []
        for word in words:
            trial = f"{current} {word}".strip()
            if stringWidth(trial, font, size) > width - 2 * margin and current:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        for chunk in lines:
            if y < margin + 20:
                pdf.showPage()
                y = height - margin
            pdf.setFont(font, size)
            pdf.setFillColor(HexColor(color))
            pdf.drawString(margin, y, chunk)
            y -= size + gap

    label = period_label(period).capitalize()
    line(f"Informe de cierre · {label}", 18, bold=True, gap=6)
    if company is not None:
        line(" · ".join(part for part in (company.name, f"NIF {company.tax_id}" if company.tax_id else None) if part), 10, color="#6b6459")
    if closed and data["closed"]:
        when = datetime.fromisoformat(data["closed"]["at"]).strftime("%d/%m/%Y %H:%M") if data["closed"]["at"] else ""
        line(f"Cerrado por {data['closed']['by'] or '—'} el {when} con un {data['closed']['percent']} % de elementos resueltos.", 10)
        if data["closed"]["note"]:
            line(f"Cerrado con salvedades ({data['closed']['blockers']}): {data['closed']['note']}", 10, color="#a4262c")
    else:
        line("PROVISIONAL: el mes sigue abierto.", 10, bold=True, color="#a4262c")
    y -= 6
    line(data["headline"], 12, bold=True)
    line(f"{data['units']['done']} de {data['units']['total']} elementos resueltos ({data['units']['formula']}).", 9, color="#6b6459")
    y -= 6
    line("Comprobaciones", 12, bold=True)
    words = {"ok": "Correcto", "warn": "Aviso", "block": "Bloquea"}
    for item in data["checks"]:
        shown = snapshot.get(item["key"], item)
        line(f"{words[shown['status']]} · {item['label']}: {shown['detail']}", 10, color="#a4262c" if shown["status"] == "block" else "#1c1914")
    y -= 6
    line("Cifras del mes", 12, bold=True)
    for summary_line in data["summary"]:
        line(f"· {summary_line['text']}", 10)
    line(f"· Borrador contable: {diary['count']} asiento(s), debe {eur(diary['totals']['debit'])} = haber {eur(diary['totals']['credit'])}"
         + (f"; {len(diary['pending'])} elemento(s) sin contabilizar" if diary["pending"] else ""), 10)
    if data["work"]:
        y -= 6
        line("Trabajo de CapaFiscal", 12, bold=True)
        line(f"Concilió {data['work']['auto_matched']} movimiento(s) con evidencia suficiente, abrió {data['work']['anomalies_created']} "
             f"anomalía(s) y cerró {data['work']['anomalies_closed']}.", 10)
    if data["to_resolve"] and not closed:
        y -= 6
        line("Pendiente para cerrar", 12, bold=True)
        for number, item in enumerate(data["to_resolve"], start=1):
            line(f"{number}. {item['text']}", 10)
    y -= 10
    line(f"Generado por CapaFiscal el {clock.today():%d/%m/%Y}. Los importes y asientos son un borrador a revisar por la gestoría; "
         "CapaFiscal no presenta nada ante la Administración.", 8, color="#6b6459")
    pdf.save()
    return buffer.getvalue()
