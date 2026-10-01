"""
Aprendizaje de las decisiones humanas.

Cada vez que una persona corrige, aprueba, rechaza o descarta algo, queda un
registro estructurado (DecisionRecord): qué propuso el sistema, qué decidió
la persona, de qué tipo fue el error, qué motor lo leyó (reglas o Claude) y
en qué contexto (proveedor, trámite, hallazgos).

Para qué sirve:
    - medir la precisión real por campo, proveedor, motor y tipo de aviso;
    - el routing: un campo que las personas corrigen a menudo en un
      proveedor deja de darse por bueno con reglas (pasa a Claude y a revisión);
    - evaluar: las correcciones son etiquetas reales (`python -m evaluation decisiones`);
    - personalizar por empresa (la categoría ya se aprende por proveedor).
"""
from __future__ import annotations

from collections import Counter
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.models import Case
from app.models import DecisionRecord
from app.models import Invoice

INVOICE_FIELDS = ("supplier_name", "supplier_tax_id", "customer_name", "customer_tax_id", "invoice_number", "invoice_date", "due_date",
                  "subtotal", "tax_total", "withholding_total", "total", "category", "direction")
HINT_MIN_CORRECTIONS = 2


def text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def error_type(field: str, predicted: str | None, human: str | None) -> str:
    if predicted is None:
        return "no_leido"
    if human is None:
        return "sobraba"
    if field in {"subtotal", "tax_total", "total", "withholding_total"}:
        try:
            if abs(float(predicted)) == abs(float(human)):
                return "signo"
        except ValueError:
            pass
        return "importe"
    if field in {"supplier_tax_id", "customer_tax_id"}:
        return "nif"
    if field in {"supplier_name", "customer_name"}:
        return "contraparte"
    return {"category": "clasificacion", "direction": "sentido", "invoice_date": "fecha", "due_date": "fecha", "invoice_number": "numero"}.get(field, "valor")


def invoice_engine(database: Session, invoice: Invoice, field: str) -> str:
    """¿Lo leyeron las reglas o lo cambió Claude?"""
    audit = database.scalar(
        select(AuditEvent).where(AuditEvent.action == "document.interpretation", AuditEvent.entity_id == str(invoice.document_id)).order_by(AuditEvent.id.desc()).limit(1)
    ) if invoice.document_id else None
    changed = ((audit.event_data or {}).get("changed") or []) if audit else []
    return "claude" if field in changed else "reglas"


def subject(invoice: Invoice) -> str | None:
    if invoice.direction == "ISSUED":
        return invoice.customer_tax_id or (invoice.customer_name or "").upper() or None
    return invoice.supplier_tax_id or (invoice.supplier_name or "").upper() or None


def record_invoice_corrections(database: Session, invoice: Invoice, before: dict[str, Any], after: dict[str, Any], actor: str | None) -> int:
    count = 0
    for field in INVOICE_FIELDS:
        old, new = text(before.get(field)), text(after.get(field))
        if old == new:
            continue
        database.add(DecisionRecord(
            actor=actor, entity_type="invoice", entity_id=invoice.id, document_id=invoice.document_id, field=field, predicted=old, human=new,
            outcome="corregido", error_type=error_type(field, old, new), engine=invoice_engine(database, invoice, field), subject_key=subject(invoice),
            context={"confidence": invoice.confidence, "validation": invoice.validation_status},
        ))
        count += 1
    return count


def record_invoice_review(database: Session, invoice: Invoice, outcome: str, actor: str | None, note: str | None = None) -> None:
    database.add(DecisionRecord(
        actor=actor, entity_type="invoice", entity_id=invoice.id, document_id=invoice.document_id, field="*", predicted=invoice.review_status,
        human=outcome, outcome=outcome, engine=None, subject_key=subject(invoice), context={"note": note, "confidence": invoice.confidence},
    ))


def record_case_decision(database: Session, case: Case, decision: str, actor: str | None, note: str | None) -> None:
    facts = case.facts or {}
    outcome = {"approved": "aprobado", "resolved": "resuelto", "rejected": "descartado"}.get(decision, decision)
    database.add(DecisionRecord(
        actor=actor, entity_type="case", entity_id=case.id, document_id=case.document_id, field="*",
        predicted=facts.get("recommendation") or case.headline, human=note, outcome=outcome,
        error_type="falso_positivo" if case.kind == "ANOMALY" and outcome == "descartado" else None,
        engine=None, subject_key=facts.get("supplier_key") or case.subject_tax_id,
        context={"kind": case.kind, "procedure": case.procedure, "level": case.level, "finding_types": facts.get("finding_types") or [case.procedure]},
    ))


def correction_hints(database: Session, subject_key: str | None) -> list[str]:
    """Campos que las personas corrigen a menudo para este proveedor: no darlos por buenos con reglas."""
    if not subject_key:
        return []
    rows = database.execute(
        select(DecisionRecord.field, DecisionRecord.error_type).where(
            DecisionRecord.entity_type == "invoice", DecisionRecord.outcome == "corregido", DecisionRecord.subject_key == subject_key
        )
    ).all()
    counts = Counter(field for field, _kind in rows)
    return [f"{field} corregido {count} veces en este proveedor" for field, count in counts.most_common() if count >= HINT_MIN_CORRECTIONS]


def stats(database: Session) -> dict[str, Any]:
    records = database.scalars(select(DecisionRecord).order_by(DecisionRecord.id)).all()
    reviewed = {record.entity_id for record in records if record.entity_type == "invoice" and record.field == "*" and record.outcome == "aprobado"}
    corrections = [record for record in records if record.entity_type == "invoice" and record.outcome == "corregido"]
    corrected_invoices = defaultdict(set)
    for record in corrections:
        corrected_invoices[record.field].add(record.entity_id)
    fields = {
        field: {"corrected": len(ids), "reviewed": len(reviewed | ids), "accuracy": round(1 - len(ids) / len(reviewed | ids), 3) if reviewed | ids else None}
        for field, ids in corrected_invoices.items()
    }
    by_engine = Counter(record.engine or "reglas" for record in corrections)
    by_error = Counter(record.error_type for record in corrections)
    by_subject = Counter(record.subject_key for record in corrections if record.subject_key)
    cases = [record for record in records if record.entity_type == "case"]
    alarms: dict[str, Counter] = defaultdict(Counter)
    for record in cases:
        if (record.context or {}).get("kind") != "ANOMALY":
            continue
        for kind in (record.context or {}).get("finding_types") or ["?"]:
            alarms[kind][record.outcome] += 1
    precision = {
        kind: {**dict(counter), "precision": round(1 - counter.get("descartado", 0) / sum(counter.values()), 3)}
        for kind, counter in alarms.items()
    }
    return {
        "decisions": len(records),
        "invoices_reviewed": len(reviewed),
        "invoice_fields": fields,
        "corrections_by_engine": dict(by_engine),
        "corrections_by_error": dict(by_error),
        "most_corrected_subjects": by_subject.most_common(5),
        "anomaly_precision": precision,
        "case_outcomes": dict(Counter(record.outcome for record in cases)),
        "recent": [
            {"at": record.created_at.isoformat() if record.created_at else None, "entity": f"{record.entity_type}:{record.entity_id}", "field": record.field,
             "predicted": record.predicted, "human": record.human, "outcome": record.outcome, "error_type": record.error_type, "engine": record.engine}
            for record in records[-10:]
        ],
    }
