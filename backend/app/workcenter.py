"""
Centro de trabajo: una sola lista con todo lo que hay que hacer, en vez de varias bandejas.

El Director mete aquí lo que antes estaba repartido (expedientes, facturas por revisar,
banco, salida, cierre, reglas aprendidas…) y cada cosa cae en uno de cuatro grupos:

    accion     🔴 Requiere tu decisión: nadie más puede hacerlo
    falta      🟠 Falta información: CapaFiscal no puede seguir sin un dato o documento
    haciendo   🟢 CapaFiscal lo está haciendo: no hay que tocar nada (y se dice qué y cuándo)
    resuelto   ✓  Lo que se ha resuelto solo en los últimos días

Cada elemento dice qué ha comprobado CapaFiscal («checked») y lleva una acción concreta.

SOLO LEE: es lo primero que se abre por la mañana, a la vez en varias pestañas y clientes;
en SQLite dos lecturas que escriben a la vez acaban en «database is locked».
"""
from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.perseguidor import CHASE
from app.models import AuditEvent
from app.models import Case
from app.models import DocumentRequest
from app.models import IngestedEvent
from app.models import Invoice
from app.models import LearningRule
from app.models import OutboxMessage

GROUPS = (
    ("accion", "Requiere tu decisión"),
    ("falta", "Falta información"),
    ("haciendo", "CapaFiscal lo está haciendo"),
    ("resuelto", "Resuelto"),
)
MIN_UNJUSTIFIED = Decimal("50")
STALE_BANK_DAYS = 7  # un extracto con más de una semana sin movimientos se pide


def eur(value: Any) -> str:
    text = f"{Decimal(str(value or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def day(value: date | str | None) -> str:
    if value is None:
        return ""
    value = date.fromisoformat(value[:10]) if isinstance(value, str) else value
    return f"{value:%d/%m}"


def item(group: str, kind: str, key: Any, title: str, why: str, *, action: dict[str, Any] | None = None,
         checked: list[dict[str, Any]] | None = None, amount: float | None = None, score: int = 0, when: str | None = None) -> dict[str, Any]:
    return {"id": f"{kind}:{key}", "group": group, "kind": kind, "title": title, "why": why, "checked": checked or [],
            "action": action, "amount": amount, "score": score, "when": when}


def case_checks(case: Any) -> list[dict[str, Any]]:
    """Lo que los agentes ya han hecho con el expediente, en una línea cada uno."""
    rows = []
    for finding in ((case.facts or {}).get("findings") or [])[:4]:
        text = finding.get("resultado") or finding.get("tipo")
        if text:
            rows.append({"label": f"{finding.get('agente', 'agente').capitalize()}: {str(text)[:110]}", "ok": True})
    return rows


def invoice_checks(invoice: Invoice) -> list[dict[str, Any]]:
    rows = [{"label": "Datos leídos del documento" + (f" ({invoice.confidence} % de confianza)" if invoice.confidence else ""), "ok": (invoice.confidence or 0) >= 80}]
    rows.append({"label": "NIF del proveedor" if invoice.supplier_tax_id else "Sin NIF del proveedor", "ok": bool(invoice.supplier_tax_id)})
    rows.append({"label": "Base + IVA = total" if invoice.validation_status == "VALID" else "Los importes no cuadran", "ok": invoice.validation_status == "VALID"})
    if invoice.duplicate_status not in (None, "NONE"):
        rows.append({"label": "Posible duplicado de otra factura", "ok": False})
    return rows


def decisions(database: Session, today: date, cases: list[Any]) -> list[dict[str, Any]]:
    from app.agents.director import attention_reason
    from app.agents.director import impact

    rows = []
    for case in cases:
        if case.status == "WAITING_HUMAN":
            score = impact(case, today)
            blocked = bool((case.facts or {}).get("processing"))
            rows.append(item("accion", "case", case.id, case.title, score["why"] or attention_reason(case, today),
                             action={"label": "Revisar expediente", "case_id": case.id}, checked=case_checks(case),
                             amount=score["amount"], score=score["score"] + (30 if blocked else 0),
                             when=case.deadline.isoformat() if case.deadline else None) | {"code": case.code, "level": case.level}
                        | ({"simulate": {"type": "dismiss_anomaly", "case_id": case.id}, "simulate_label": "¿Qué cambia si la descarto?"} if case.kind == "ANOMALY" else {}))
        elif case.status == "READY_TO_FILE":
            score = impact(case, today)
            rows.append(item("accion", "file", case.id, f"Presentar: {case.title}", "Está preparado: falta que lo revises y lo presentes.",
                             action={"label": "Revisar y presentar", "case_id": case.id}, checked=case_checks(case),
                             amount=score["amount"], score=score["score"], when=case.deadline.isoformat() if case.deadline else None) | {"code": case.code})

    for event in database.scalars(select(IngestedEvent).where(IngestedEvent.status == "FAILED").order_by(IngestedEvent.id.desc()).limit(5)).all():
        rows.append(item("accion", "event", event.id, f"No se pudo procesar una entrada ({event.source})", (event.error or "Error al procesar")[:140],
                         action={"label": "Ver entrada", "tab": "expedientes", "view": "agents"}, score=95))

    from app.business_memory import unusual

    pending = database.scalars(select(Invoice).where(Invoice.review_status == "PENDING").order_by(Invoice.id.desc()).limit(50)).all()
    pool = list(database.scalars(select(Invoice).where(Invoice.invoice_date.is_not(None), Invoice.total.is_not(None))).all()) if pending else []
    for invoice in pending:
        memory = unusual(database, invoice, today=today, pool=pool)["signals"]
        problems = [row for row in invoice_checks(invoice) if not row["ok"]] + [
            {"label": signal["text"], "ok": False if signal["severity"] in ("high", "medium") else None} for signal in memory]
        party = invoice.customer_name if invoice.direction == "ISSUED" else invoice.supplier_name
        rare = next((signal["text"] for signal in memory if signal["severity"] == "high"), None)
        why = rare or ("; ".join(row["label"].lower() for row in problems[:3]) if problems else "Todo cuadra: falta tu visto bueno")
        rows.append(item("accion", "invoice", invoice.id, f"Factura {invoice.invoice_number or 's/n'} · {party or 'sin identificar'}", why,
                         action={"label": "Revisar factura", "document_id": invoice.document_id},
                         checked=invoice_checks(invoice) + [{"label": signal["text"], "ok": False if signal["severity"] != "info" else None} for signal in memory],
                         amount=float(invoice.total) if invoice.total is not None else None, score=40 + 10 * len(problems) + (25 if rare else 0),
                         when=invoice.invoice_date.isoformat() if invoice.invoice_date else None)
                    | {"simulate": {"type": "approve_invoice", "invoice_id": invoice.id}, "simulate_label": "¿Qué cambia si la apruebo?"})

    drafts = database.scalars(select(OutboxMessage).where(OutboxMessage.status == "DRAFT").order_by(OutboxMessage.id)).all()
    asked = {}
    for request in database.scalars(select(DocumentRequest).where(DocumentRequest.status == "PENDING")).all():
        asked.setdefault(request.case_id, []).append(request.label)
    for message in drafts:
        wanted = asked.get(message.entity_id, []) if message.entity_type == "case_request" else []
        pedido = f"Pide: {', '.join(wanted[:3])}{'…' if len(wanted) > 3 else ''}. " if wanted else ""
        rows.append(item("accion", "outbox", message.id, f"Enviar: {message.subject}", f"{pedido}Redactado por CapaFiscal para {message.to_name or message.to_email or 'sin destinatario'}: espera tu visto bueno.",
                         action={"label": "Revisar y enviar", "tab": "salida"},
                         checked=[{"label": "Destinatario" if message.to_email else "Falta el email del destinatario", "ok": bool(message.to_email)}], score=45))

    for rule in database.scalars(select(LearningRule).where(LearningRule.status == "PROPUESTA")).all():
        evidence = rule.evidence or {}
        rows.append(item("accion", "rule", rule.id, f"Regla propuesta: {rule.subject_name or rule.subject_key} · {rule.field}",
                         f"Aprendida de {evidence.get('corrections', 'varias')} corrección(es) tuyas: se aplica solo si la apruebas.",
                         action={"label": "Decidir", "tab": "expedientes", "view": "agents", "anchor": "learningRules"}, score=20))
    return rows


def bank_items(database: Session, today: date) -> list[dict[str, Any]]:
    from app.models import BankTransaction
    from app.reconciliation import reconcile

    from app.bank_sync import expiring
    from app.models import BankConnection

    last = database.scalar(select(BankTransaction.booking_date).order_by(BankTransaction.booking_date.desc()).limit(1))
    rows = []
    connections = database.scalars(select(BankConnection).where(BankConnection.status != "REMOVED")).all()
    linked = [item for item in connections if item.status == "LINKED"]
    for connection in connections:
        name = connection.institution_name or "el banco"
        if connection.status == "EXPIRED":
            rows.append(item("accion", "bank_consent", connection.id, f"Renueva el acceso a {name}",
                             "El consentimiento PSD2 caduca cada 90 días: sin renovarlo, los movimientos dejan de llegar solos.",
                             action={"label": "Renovar", "tab": "negocio", "anchor": "bankConnections"}, score=60))
        elif connection.status == "PENDING":
            rows.append(item("falta", "bank_authorize", connection.id, f"Falta autorizar {name}",
                             "El titular de la cuenta tiene que dar permiso en la web del banco.",
                             action={"label": "Autorizar", "tab": "negocio", "anchor": "bankConnections"}, score=30))
        elif connection.status == "LINKED" and expiring(connection, today) is not None:
            left = expiring(connection, today)
            rows.append(item("accion", "bank_consent_soon", connection.id, f"El acceso a {name} caduca en {max(left, 0)} días",
                             "Renuévalo antes y los movimientos seguirán llegando solos (el banco pide confirmarlo cada 90 días).",
                             action={"label": "Renovar", "tab": "negocio", "anchor": "bankConnections"}, score=40))
        if connection.status == "LINKED" and connection.last_error:
            rows.append(item("falta", "bank_sync_error", connection.id, f"No se pudo leer {name}", connection.last_error[:200],
                             action={"label": "Ver banco", "tab": "negocio", "anchor": "bankConnections"}, score=30))
    if linked:
        synced = max((item.last_sync_at for item in linked if item.last_sync_at), default=None)
        rows.append(item("haciendo", "bank_sync", "linked", f"Banco conectado ({len(linked)})",
                         "Los movimientos llegan solos cada 6 horas y se concilian al entrar."
                         + (f" Última lectura: {synced:%d/%m %H:%M}." if synced else ""),
                         action={"label": "Ver banco", "tab": "negocio", "anchor": "bankConnections"}))
    if last is None and linked:
        return rows
    if last is None:
        return [item("falta", "bank_none", "-", "Sin movimientos del banco", "Sin extracto no se pueden conciliar pagos y cobros ni cerrar el mes.",
                     action={"label": "Importar extracto", "tab": "negocio", "anchor": "bankCard"}, score=30)]
    if (today - last).days > STALE_BANK_DAYS and not linked:
        rows.append(item("falta", "bank_gap", last.isoformat(), f"El extracto llega hasta el {last:%d/%m/%Y}",
                         f"Faltan {(today - last).days} días de movimientos: los pagos y cobros de esos días no se pueden conciliar.",
                         action={"label": "Importar extracto", "tab": "negocio", "anchor": "bankCard"}, score=35))
    report = reconcile(database, today=today, auto=False, persist=False)
    for row in report["movements"]:
        checked = [{"label": check.get("label", ""), "ok": check.get("ok")} for check in row.get("checks") or []][:5]
        label = f"{day(row['date'])} · {row['description'][:60]}"
        if row.get("level") == "CONFLICTO":
            rows.append(item("accion", "bank_conflict", row["transaction_id"], label, row.get("decision") or "La evidencia se contradice.",
                             action={"label": "Decidir", "tab": "negocio", "anchor": "bankCard"}, checked=checked, amount=row["amount"], score=50, when=row["date"]))
        elif row["state"] == "SIN_FACTURA" and abs(Decimal(str(row["amount"]))) >= MIN_UNJUSTIFIED:
            rows.append(item("falta", "bank_unjustified", row["transaction_id"], label,
                             f"{'Pago' if row['amount'] < 0 else 'Cobro'} de {eur(abs(row['amount']))} sin factura que lo justifique: falta el documento o decir qué es.",
                             action={"label": "Investigar", "tab": "negocio", "anchor": "bankCard"}, checked=checked, amount=row["amount"], score=40, when=row["date"]))
        elif row.get("level") == "PROBABLE" and row["state"] == "POSIBLE":
            plan = row.get("proposal")
            why = (f"{plan['label']}: {plan['explanation']} Falta tu confirmación." if plan else
                   f"Probablemente es {row.get('invoice_label') or 'una factura'}, pero la evidencia no basta para conciliarlo solo.")
            rows.append(item("falta", "bank_probable", row["transaction_id"], label, why,
                             action={"label": "Confirmar", "tab": "negocio", "anchor": "bankCard"}, checked=checked, amount=row["amount"], score=25, when=row["date"]))
    for row in report["unpaid_invoices"]:
        rows.append(item("accion", "unpaid", row["invoice_id"], row["invoice_label"],
                         f"Venció el {day(row['due'])} y no hay {row['direction']} en el banco: ¿se {'cobró' if row['direction'] == 'cobro' else 'pagó'} por otra vía?",
                         action={"label": "Ver en el banco", "tab": "negocio", "anchor": "bankCard"}, amount=row["amount"], score=35, when=row["due"]))
    return rows


def missing_documents(database: Session, today: date) -> list[dict[str, Any]]:
    from app.fiscal_position import missing_recurring

    month_start = today.replace(day=1)
    previous = (month_start - timedelta(days=1)).replace(day=1)
    quarter = (previous.month - 1) // 3 + 1
    rows = []
    for missing in missing_recurring(database, previous.year, quarter, today):
        rows.append(item("falta", "recurring", f"{missing['supplier_key']}:{missing['month']}", f"Falta la factura de {missing['label']}",
                         f"Llega todos los meses (habitual: {eur(missing['usual'])}) y la de ese mes no ha llegado.",
                         action={"label": "Subir o pedir", "tab": "facturas"},
                         checked=[{"label": "Proveedor mensual en los 3 meses anteriores", "ok": True}, {"label": "Factura del mes", "ok": False}], score=30))
    return rows


def working(database: Session, today: date, cases: list[Any]) -> list[dict[str, Any]]:
    rows = []
    requests = database.scalars(select(DocumentRequest).where(DocumentRequest.status == "PENDING")).all()
    sent_cases = set(database.scalars(select(OutboxMessage.entity_id).where(OutboxMessage.entity_type == "case_request", OutboxMessage.status == "SENT")).all())
    by_case: dict[int, list[DocumentRequest]] = {}
    for request in requests:
        by_case.setdefault(request.case_id, []).append(request)
    titles = {case.id: case for case in cases}
    for case_id, items in by_case.items():
        case = titles.get(case_id)
        if case is None:
            continue
        labels = ", ".join(request.label for request in items[:3]) + ("…" if len(items) > 3 else "")
        if case_id in sent_cases:
            next_at = min((request.next_reminder_at for request in items if request.next_reminder_at), default=None)
            reminders = max(request.reminders_sent for request in items)
            rows.append(item("haciendo", "chase", case_id, f"Esperando documentación · {case.title}",
                             f"Pedido: {labels}. " + (f"{min(reminders, 2)} recordatorio(s) enviados. " if reminders else "")
                             + (f"Siguiente paso: {CHASE[reminders][1]} el {next_at:%d/%m}." if next_at and reminders < len(CHASE)
                                else "Ya se avisó al gestor: conviene llamar." if reminders >= len(CHASE) else "El Perseguidor vuelve a insistir si no llega."),
                             action={"label": "Ver expediente", "case_id": case_id}, when=next_at.isoformat() if next_at else None) | {"code": case.code})
        # Sin enviar: es la petición redactada que espera visto bueno («Enviar: …» en Requiere tu decisión).
    for case in cases:
        if case.status == "WAITING_DOCS" and case.id not in by_case:
            rows.append(item("haciendo", "case_docs", case.id, case.title, "Espera documentación de un tercero; CapaFiscal sigue el plazo.",
                             action={"label": "Ver expediente", "case_id": case.id}) | {"code": case.code})
    return rows


def agenda_items(database: Session, today: date, taken: set[str]) -> list[dict[str, Any]]:
    """Pagos que vencen, cobros vencidos y cumplimiento: lo que antes había que ir a buscar a cada módulo."""
    from app.agenda_service import build_agenda

    try:
        agenda = build_agenda(database, horizon_days=14, today=today)["items"]
    except Exception:  # la agenda no debe tumbar la lista
        return []
    reminded = set(database.scalars(select(OutboxMessage.entity_id).where(OutboxMessage.entity_type.in_(("invoice", "sales_invoice", "dunning")),
                                                                        OutboxMessage.status == "SENT")).all())
    rows = []
    for entry in agenda:
        days = entry["days_left"]
        when = "vencido hace " + f"{-days} días" if days < 0 else "vence hoy" if days == 0 else f"vence en {days} días"
        if entry["kind"] == "payment" and f"unpaid:{entry['entity_id']}" not in taken and days <= 7:
            rows.append(item("accion", "pay", entry["entity_id"], entry["title"], f"{entry['detail']} ({when}).",
                             action={"label": "Registrar pago", "document_id": entry["document_id"]}, amount=-(entry["amount"] or 0),
                             score=50 if days < 0 else 35, when=entry["date"]))
        elif entry["kind"] == "collection" and days < 0:
            if entry["entity_id"] in reminded:
                rows.append(item("haciendo", "collect", entry["entity_id"], entry["title"], f"{entry['detail']} Recordatorio enviado: CapaFiscal sigue la reclamación.",
                                 action={"label": "Ver cobros", "tab": "ventas", "view": "cobros"}, amount=entry["amount"], when=entry["date"]))
            else:
                rows.append(item("accion", "collect", entry["entity_id"], entry["title"], f"{entry['detail']} ({when}): reclámalo.",
                                 action={"label": "Reclamar", "tab": "ventas", "view": "cobros"}, amount=entry["amount"], score=45, when=entry["date"]))
        elif entry["kind"] == "compliance":
            rows.append(item("accion" if entry["level"] in ("overdue", "critical") else "falta", "compliance", entry["entity_id"], entry["title"],
                             entry["detail"], action={"label": "Revisar", "tab": "cumplimiento"}, score=40 if entry["level"] == "overdue" else 25,
                             when=entry["date"]))
    return rows


def fiscal_items(database: Session, today: date) -> list[dict[str, Any]]:
    from app.fiscal_position import positions

    try:
        report = positions(database, today=today)
    except Exception:  # la foto fiscal no debe tumbar la lista
        return []
    rows = []
    for model in report["models"]:
        if model["status"] == "FILED" or model["days_left"] is None or model["days_left"] > 30:
            continue
        info = model["information_available"] or 0
        text = f"{model['headline']} · vence el {day(model['due_date'])} ({model['days_left']} días)"
        if info < 1:
            rows.append(item("falta", "tax", f"{model['model']}:{model['period_label']}", f"Modelo {model['model']} · {model['period_label']}",
                             f"{text} · {model['summary']}",
                             action={"label": "Ver qué falta", "tab": "impuestos"}, score=20 + (20 if model["days_left"] <= 7 else 0), when=model["due_date"]))
        else:
            rows.append(item("haciendo", "tax", f"{model['model']}:{model['period_label']}", f"Modelo {model['model']} · {model['period_label']}",
                             f"{text}. Con toda la información; el Vigilante de plazos prepara el expediente 15 días antes.",
                             action={"label": "Ver posición", "tab": "impuestos"}, when=model["due_date"]))
    return rows


def treasury_items(database: Session, today: date) -> list[dict[str, Any]]:
    """Control financiero continuo: el riesgo de liquidez entra en la lista con su porqué."""
    from app.treasury import predict

    try:
        forecast = predict(database, today=today, horizon_days=60)
        risk, basis = forecast["risk"], forecast["confidence"]
    except Exception:  # la previsión no debe tumbar la lista
        return []
    if risk["level"] not in ("alto", "medio"):
        return []
    first = risk["actions"][0] if risk["actions"] else {"label": "Ver la previsión de caja", "tab": "negocio"}
    return [item("accion", "liquidity", risk.get("date"), risk["headline"],
                 f"{risk['explanation']} Confianza {basis['level']}: {basis['reasons']}.",
                 action={"label": "Ver previsión", "tab": "negocio", "anchor": "cashflowCard"},
                 checked=[{"label": action["label"], "ok": None} for action in risk["actions"]] or [{"label": first["label"], "ok": None}],
                 score=90 if risk["level"] == "alto" else 45, when=risk.get("date"))]


def overnight(database: Session, since: datetime) -> dict[str, Any]:
    """«CapaFiscal ha trabajado durante la noche»: lo hecho desde ayer a esta hora, contado."""
    def count(statement) -> int:
        return int(database.scalar(statement) or 0)

    documents = count(select(func.count()).select_from(IngestedEvent).where(IngestedEvent.created_at >= since, IngestedEvent.kind.in_(("document", "invoice"))))
    reconciled = count(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "bank.reconciled", AuditEvent.created_at >= since))
    anomalies = count(select(func.count()).select_from(Case).where(Case.kind == "ANOMALY", Case.created_at >= since))
    requested = count(select(func.count()).select_from(DocumentRequest).where(DocumentRequest.created_at >= since))
    prepared = count(select(func.count()).select_from(Case).where(Case.kind != "ANOMALY", Case.created_at >= since))
    lines = [
        {"key": "documents", "count": documents, "label": "documento(s) leídos y registrados"},
        {"key": "reconciled", "count": reconciled, "label": "pago(s) y cobro(s) conciliados"},
        {"key": "anomalies", "count": anomalies, "label": "anomalía(s) detectadas"},
        {"key": "requested", "count": requested, "label": "documento(s) pedidos"},
        {"key": "prepared", "count": prepared, "label": "expediente(s) preparados"},
    ]
    return {"since": since.isoformat(), "total": sum(line["count"] for line in lines), "items": [line for line in lines if line["count"]]}


def learning(database: Session) -> dict[str, Any]:
    """Patrones que CapaFiscal ha visto en tus correcciones y cuántos has confirmado tú (nada se aplica sin confirmar)."""
    rules = database.scalars(select(LearningRule)).all()
    confirmed = [rule for rule in rules if rule.status == "APROBADA"]
    version = max((rule.version or 0 for rule in confirmed), default=0)
    proposals = sum(1 for rule in rules if rule.status == "PROPUESTA")
    if not rules:
        text = "Sin patrones todavía: cuando corrijas algo varias veces, CapaFiscal te propondrá una regla."
    else:
        text = (f"{len(rules)} patrón(es) detectados en tus correcciones · {len(confirmed)} confirmados por ti"
                + (f" · {proposals} esperan tu decisión" if proposals else ""))
    return {"patterns": len(rules), "approved": len(confirmed), "version": version, "proposals": proposals, "text": text}


def work_center(database: Session, *, today: date | None = None, now: datetime | None = None, user_name: str | None = None) -> dict[str, Any]:
    from app.agents.director import assessed_open_cases
    from app.agents.director import operational_board
    from app.agents.pulse import pulse_status
    from app.closing import default_period
    from app.closing import evaluate

    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    cases = assessed_open_cases(database, today)

    rows = treasury_items(database, today) + decisions(database, today, cases) + bank_items(database, today) + missing_documents(database, today) + working(database, today, cases) + fiscal_items(database, today)
    rows += agenda_items(database, today, {row["id"] for row in rows})
    rows.sort(key=lambda row: (-row["score"], row["when"] or "9999"))
    groups = {key: [row for row in rows if row["group"] == key] for key, _ in GROUPS}

    board = operational_board(database, today)
    groups["resuelto"] = [item("resuelto", line["key"], line["key"], f"{line['count']} {line['label']}", "Últimos 7 días, sin que nadie tuviera que intervenir.")
                          | {"count": line["count"]} for line in board["resolved"]["items"]]

    period = default_period(today)
    close = evaluate(database, period, today=today)
    pulse = pulse_status(database, now=now)
    hour = now.astimezone().hour
    greeting = "Buenos días" if hour < 14 else "Buenas tardes" if hour < 21 else "Buenas noches"
    first = (user_name or "").split(" ")[0]
    decide = len(groups["accion"])
    headline = ("Nada requiere tu decisión hoy" if not decide else
                f"{decide} {'cosa necesita' if decide == 1 else 'cosas necesitan'} tu decisión")
    return {
        "date": today.isoformat(),
        "greeting": f"{greeting}{', ' + first if first else ''}",
        "headline": headline,
        "overnight": overnight(database, now - timedelta(hours=24)),
        "pulse": pulse,
        "close": {"period": period, "label": close["label"], "percent": close["percent"], "blockers": close["blockers"], "headline": close["headline"], "ready": close["ready"]},
        "groups": [{"key": key, "label": label, "count": len(groups[key]) if key != "resuelto" else sum(row["count"] for row in groups[key]), "items": groups[key]}
                   for key, label in GROUPS],
        "counts": {key: len(groups[key]) for key, _ in GROUPS if key != "resuelto"},
        "time_saved": board["time_saved"],
        "learning": learning(database),
    }
