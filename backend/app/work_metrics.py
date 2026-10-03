"""
Medir trabajos terminados, no documentos procesados.

Un trabajo cuenta como terminado solo al final de su recorrido:

    factura         aprobada → contabilizable → pagada o cobrada (conciliada con el banco o marcada)
    movimiento      conciliado o justificado (comisión, traspaso, nómina, impuesto…)
    expediente      resuelto o presentado
    mes             cerrado

Y junto a eso, lo que importa para decidir qué construir:

    sin humano      lo que CapaFiscal terminó solo (conciliaciones automáticas, avisos cerrados solos, entradas sin nada que objetar)
    decisiones      lo que tuvo que decidir una persona
    errores         lo que CapaFiscal detectó (anomalías, duplicados, importes que no cuadran)
    falsos positivos avisos que una persona descartó; reglas propuestas que rechazó; conciliaciones automáticas deshechas
    horas           estimación con supuestos a la vista (los del Director)

Solo lee.
"""
from __future__ import annotations

from app import clock
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.models import BankTransaction
from app.models import Case
from app.models import Invoice
from app.models import LearningRule
from app.models import PeriodClose

AUTOMATIC_ACTORS = ("conciliacion-automatica", "cierre-mensual", "banco-conectado", "agent", "perseguidor", "sistema")
HUMAN_ACTIONS = ("invoice.approved", "invoice.rejected", "invoice.updated", "bank.reconciled", "bank.allocated", "bank.unreconciled",
                 "case.approved", "case.resolved", "case.dismissed", "case.filed", "close.closed", "outbox.sent", "outbox.marked_sent")


def measure(database: Session, *, days: int = 30, today: date | None = None) -> dict[str, Any]:
    from app.agents.director import operational_board

    today = today or clock.today()
    since_day = today - timedelta(days=days)
    since = datetime.combine(since_day, time.min, tzinfo=timezone.utc)

    def count(statement) -> int:
        return int(database.scalar(statement) or 0)

    invoices_done = count(select(func.count()).select_from(Invoice).where(
        Invoice.review_status == "APPROVED", Invoice.paid_at.is_not(None), Invoice.paid_at >= since_day))
    invoices_open = count(select(func.count()).select_from(Invoice).where(Invoice.review_status == "APPROVED", Invoice.paid_at.is_(None)))
    movements_done = count(select(func.count()).select_from(BankTransaction).where(
        BankTransaction.match_status.in_(("MATCHED", "IGNORED")), BankTransaction.booking_date >= since_day))
    movements_open = count(select(func.count()).select_from(BankTransaction).where(BankTransaction.match_status.in_(("UNMATCHED", "SUGGESTED"))))
    cases_done = count(select(func.count()).select_from(Case).where(Case.status.in_(("RESOLVED", "FILED")), Case.resolved_at >= since))
    months_done = count(select(func.count()).select_from(PeriodClose).where(PeriodClose.status == "CLOSED", PeriodClose.closed_at >= since))

    events = database.execute(select(AuditEvent.action, AuditEvent.actor).where(AuditEvent.created_at >= since)).all()
    automatic = sum(1 for action, actor in events if action in ("bank.reconciled", "bank.allocated") and actor in AUTOMATIC_ACTORS)
    decisions = sum(1 for action, actor in events if action in HUMAN_ACTIONS and actor not in AUTOMATIC_ACTORS)
    undone = sum(1 for action, actor in events if action == "bank.unreconciled")

    anomalies = database.scalars(select(Case).where(Case.kind == "ANOMALY", Case.created_at >= since)).all()
    dismissed = sum(1 for case in anomalies if case.status == "DISMISSED")
    decided = sum(1 for case in anomalies if case.status in ("DISMISSED", "RESOLVED"))
    duplicates = count(select(func.count()).select_from(Invoice).where(Invoice.duplicate_status.not_in(("NONE",)), Invoice.created_at >= since))
    invalid = count(select(func.count()).select_from(Invoice).where(Invoice.validation_status != "VALID", Invoice.created_at >= since))
    rules = database.scalars(select(LearningRule.status)).all()

    board = operational_board(database, today, days=days)
    finished = invoices_done + movements_done + cases_done + months_done
    return {
        "days": days, "since": since_day.isoformat(),
        "finished": {"total": finished, "invoices": invoices_done, "movements": movements_done, "cases": cases_done, "months": months_done},
        "open": {"invoices": invoices_open, "movements": movements_open},
        "without_human": automatic + board["resolved"]["count"],
        "decisions": decisions,
        "errors_found": {"anomalies": len(anomalies), "duplicates": duplicates, "invalid": invalid},
        "false_positives": {
            "anomalies_dismissed": dismissed, "anomalies_decided": decided,
            "rate": round(dismissed / decided, 2) if decided else None,
            "rules_rejected": sum(1 for status in rules if status == "RECHAZADA"),
            "automatic_undone": undone,
        },
        "hours_saved": board["time_saved"]["hours"],
        "hours_note": board["time_saved"]["note"],
        "definition": "Terminado = factura aprobada y pagada o cobrada · movimiento conciliado o justificado · expediente resuelto o presentado · mes cerrado.",
    }
