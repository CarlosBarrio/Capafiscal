"""Memoria: busca antecedentes del mismo organismo, trámite, periodo o tema."""
from __future__ import annotations

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import evidence
from app.agents.memory import build_corpus
from app.agents.memory import search
from app.models import Case
from app.models import FiscalNotification

KIND_LABELS = {"document": "Documento", "notification": "Notificación", "case": "Expediente"}


class AgenteMemoria(Agent):
    code = "memoria"
    name = "Memoria"
    role = "Recuerda todo lo que ha pasado y encuentra antecedentes con evidencia documental."
    icon = "brain"

    def run(self, ctx: AgentContext) -> StepResult:
        database = ctx.database
        case = ctx.case
        notification: FiscalNotification = ctx.facts["notification"]
        antecedents = []
        seen: set[tuple[str, int]] = {("notification", notification.id)}
        if case:
            seen.add(("case", case.id))
        if notification.document_id:
            seen.add(("document", notification.document_id))

        # 1) Antecedentes estructurados: mismo organismo y tipo de trámite.
        previous = database.scalars(
            select(FiscalNotification)
            .where(
                FiscalNotification.id != notification.id,
                FiscalNotification.issuer == notification.issuer,
                FiscalNotification.notification_type == notification.notification_type,
            )
            .order_by(FiscalNotification.created_at.desc())
            .limit(3)
        ).all()
        for item in previous:
            previous_case = database.scalar(select(Case).where(Case.notification_id == item.id))
            antecedents.append(
                {
                    "kind": "notification",
                    "ref_id": item.id,
                    "case_id": previous_case.id if previous_case else None,
                    "title": f"{item.title}{f' · {item.reference}' if item.reference else ''}",
                    "date": (item.notified_at or item.created_at.date()).isoformat(),
                    "why": "Mismo organismo y mismo tipo de trámite",
                    "outcome": (previous_case.resolution if previous_case and previous_case.resolution else None)
                    or {"PENDING": "sin resolver", "IN_PROGRESS": "en curso", "ANSWERED": "contestada", "CLOSED": "cerrada"}.get(item.status),
                }
            )
            seen.add(("notification", item.id))
            if previous_case:
                seen.add(("case", previous_case.id))

        # 2) Búsqueda en toda la documentación (texto libre).
        terms = [ctx.facts.get("procedure_label") or "", ctx.facts.get("reference") or ""]
        for reference in ctx.facts.get("tax_references", []):
            terms.append(f"modelo {reference['model']} {reference.get('year') or ''}")
        affected = ctx.facts.get("affected")
        if affected and affected.get("name"):
            terms.append(affected["name"])
        query = " ".join(terms + [ctx.text[:600]])
        corpus = build_corpus(database)
        for hit in search(database, query, limit=4, exclude=seen, corpus=corpus):
            if hit["score"] < 1.5:
                continue
            antecedents.append(
                {
                    "kind": hit["kind"],
                    "ref_id": hit["ref_id"],
                    "title": hit["title"],
                    "date": hit["date"],
                    "why": f"Coincide en: {', '.join(hit['matched'][:5])}",
                    "snippet": hit["snippet"][:300],
                    "score": hit["score"],
                }
            )

        antecedents = antecedents[:5]
        ctx.facts["antecedents"] = antecedents
        if case:
            case.antecedents = antecedents

        if not antecedents:
            summary = "Sin antecedentes parecidos: es la primera vez que llega algo así."
        else:
            first = antecedents[0]
            summary = f"{len(antecedents)} antecedente(s). El más relevante: {first['title']} ({first['date']})" + (
                f", {first['outcome']}." if first.get("outcome") else "."
            )

        return StepResult(
            summary=summary,
            output={"antecedents": antecedents, "corpus_size": len(corpus)},
            evidence=[evidence(item["kind"], f"{KIND_LABELS.get(item['kind'], item['kind'])}: {item['title']}", ref_id=item["ref_id"]) for item in antecedents],
            engine="memoria (BM25)",
        )
