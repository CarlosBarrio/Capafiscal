"""
Aprendizaje de las decisiones humanas.

Cada vez que una persona corrige, aprueba, rechaza o descarta algo, queda un
registro estructurado (DecisionRecord): qué propuso el sistema, qué decidió
la persona, de qué tipo fue el error, qué motor lo leyó (reglas o Claude) y
en qué contexto (proveedor, trámite, hallazgos).

Para qué sirve:
    - medir la precisión real por campo, proveedor, motor y tipo de aviso;
    - proponer reglas, NUNCA aplicarlas solo: si un campo se corrige a menudo
      en un proveedor, se propone «no darlo por bueno con reglas». La propuesta
      lleva su evidencia y una simulación sobre el histórico, y solo entra en
      vigor cuando un administrador la aprueba (con número de versión):

          correcciones → patrón → propuesta → simulación → aprobación → versión

      Así el sistema no aprende en silencio una mala decisión humana;
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
from app.models import LearningRule

INVOICE_FIELDS = ("supplier_name", "supplier_tax_id", "customer_name", "customer_tax_id", "invoice_number", "invoice_date", "due_date",
                  "subtotal", "tax_total", "withholding_total", "total", "category", "direction")
HINT_MIN_CORRECTIONS = 2  # correcciones del mismo campo y proveedor para proponer una regla


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
    """Los motivos que las reglas APROBADAS añaden para este proveedor (las propuestas no actúan)."""
    if not subject_key:
        return []
    rules = database.scalars(
        select(LearningRule).where(LearningRule.status == "APROBADA", LearningRule.kind == "revisar_campo", LearningRule.subject_key == subject_key)
    ).all()
    return [f"{rule.field} corregido {rule.evidence.get('corrections', '?')} veces en este proveedor (regla v{rule.version})" for rule in rules]


def detected_patterns(database: Session) -> dict[tuple[str, str], list[DecisionRecord]]:
    """Correcciones repetidas del mismo campo en el mismo proveedor."""
    rows = database.scalars(
        select(DecisionRecord).where(DecisionRecord.entity_type == "invoice", DecisionRecord.outcome == "corregido", DecisionRecord.subject_key.is_not(None))
    ).all()
    groups: dict[tuple[str, str], list[DecisionRecord]] = defaultdict(list)
    for record in rows:
        groups[(record.subject_key, record.field)].append(record)
    return {key: items for key, items in groups.items() if len({item.entity_id for item in items}) >= HINT_MIN_CORRECTIONS}


def simulate(database: Session, subject_key: str, field: str) -> dict[str, Any]:
    """Qué habría pasado con el histórico si la regla hubiera estado en vigor."""
    invoices = database.scalars(
        select(Invoice).where((Invoice.supplier_tax_id == subject_key) | (Invoice.customer_tax_id == subject_key) | (Invoice.supplier_name == subject_key))
    ).all()
    corrected = {
        record.entity_id for record in database.scalars(
            select(DecisionRecord).where(DecisionRecord.entity_type == "invoice", DecisionRecord.outcome == "corregido",
                                         DecisionRecord.subject_key == subject_key, DecisionRecord.field == field)
        ).all()
    }
    affected = len(invoices)
    caught = len(corrected & {invoice.id for invoice in invoices})
    return {
        "invoices_affected": affected,
        "would_have_caught": caught,
        "extra_reviews": max(0, affected - caught),
        "precision": round(caught / affected, 3) if affected else None,
        "summary": (f"De {affected} factura(s) de este proveedor, {affected} habrían pasado por revisión del campo «{field}»; "
                    f"en {caught} una persona lo corrigió de verdad ({max(0, affected - caught)} revisión(es) de más).") if affected else "Sin facturas en el histórico.",
    }


def propose_rules(database: Session) -> list[LearningRule]:
    """Convierte patrones de corrección en propuestas (idempotente). No activa nada."""
    existing = {(rule.subject_key, rule.field): rule for rule in database.scalars(select(LearningRule).where(LearningRule.kind == "revisar_campo")).all()}
    proposed = []
    for (subject_key, field), records in detected_patterns(database).items():
        invoices = sorted({record.entity_id for record in records})
        evidence = {
            "corrections": len(invoices), "invoice_ids": invoices[:20],
            "examples": [{"invoice_id": record.entity_id, "predicted": record.predicted, "human": record.human, "engine": record.engine,
                          "at": record.created_at.isoformat() if record.created_at else None} for record in records[-5:]],
        }
        rule = existing.get((subject_key, field))
        if rule is None:
            invoice = database.get(Invoice, invoices[-1])
            name = (invoice.customer_name if invoice and invoice.direction == "ISSUED" else invoice.supplier_name if invoice else None) or subject_key
            rule = LearningRule(kind="revisar_campo", subject_key=subject_key, subject_name=name, field=field, status="PROPUESTA",
                                evidence=evidence, simulation=simulate(database, subject_key, field))
            database.add(rule)
            proposed.append(rule)
        elif rule.status == "PROPUESTA":
            rule.evidence, rule.simulation = evidence, simulate(database, subject_key, field)
    database.flush()
    return proposed


def ruleset_version(database: Session) -> int:
    from sqlalchemy import func

    return database.scalar(select(func.max(LearningRule.version))) or 0


def decide_rule(database: Session, rule: LearningRule, decision: str, actor: str | None, note: str | None = None) -> LearningRule:
    """Aprobar, rechazar o retirar una regla. Aprobar crea una versión nueva del conjunto de reglas."""
    from datetime import datetime
    from datetime import timezone

    allowed = {"aprobar": ({"PROPUESTA"}, "APROBADA"), "rechazar": ({"PROPUESTA"}, "RECHAZADA"), "retirar": ({"APROBADA"}, "RETIRADA")}
    if decision not in allowed:
        raise ValueError("Decisión no válida: aprobar, rechazar o retirar.")
    sources, target = allowed[decision]
    if rule.status not in sources:
        raise ValueError(f"La regla está {rule.status.lower()}: no se puede {decision}.")
    if decision == "aprobar":
        rule.simulation = simulate(database, rule.subject_key, rule.field)  # se aprueba con la simulación al día
        rule.version = ruleset_version(database) + 1
    rule.status, rule.decided_by, rule.decided_at, rule.note = target, actor, datetime.now(timezone.utc), note
    from app.invoice_service import add_audit_event

    add_audit_event(database, action=f"learning.rule_{target.lower()}", entity_type="learning_rule", entity_id=rule.id, actor=actor or "persona",
                    event_data={"subject": rule.subject_key, "field": rule.field, "version": rule.version, "note": note})
    database.flush()
    return rule


def serialize_rule(rule: LearningRule) -> dict[str, Any]:
    return {
        "id": rule.id, "kind": rule.kind, "subject_key": rule.subject_key, "subject_name": rule.subject_name, "field": rule.field,
        "status": rule.status, "version": rule.version, "evidence": rule.evidence, "simulation": rule.simulation,
        "effect": f"Las facturas de {rule.subject_name or rule.subject_key} dejan de dar por bueno «{rule.field}» solo con reglas: pasa a interpretación y revisión.",
        "decided_by": rule.decided_by, "decided_at": rule.decided_at.isoformat() if rule.decided_at else None, "note": rule.note,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


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
