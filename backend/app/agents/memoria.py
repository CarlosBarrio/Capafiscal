"""Memoria: busca antecedentes del mismo organismo, trámite, periodo o tema."""
from __future__ import annotations

import statistics
from datetime import date
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import Finding
from app.agents.base import eur
from app.agents.base import evidence
from app.agents.base import jsonable
from app.agents.memory import build_corpus
from app.agents.memory import search
from app.agents.memory import tokenize
from app.company_service import company_name
from app.company_service import company_tax_ids

# Palabras que aparecen en casi todas las notificaciones y no distinguen nada.
GENERIC = set(tokenize(
    "agencia estatal administracion tributaria delegacion especial dependencia regional seguridad social tesoreria "
    "general notificacion referencia obligado tributario destinatario nif plazo dias habiles documentacion requiere "
    "aporte aportar presente deberá debera correspondiente relacion procedimiento"
))
from app.models import Case  # noqa: E402
from app.models import FiscalNotification  # noqa: E402
from app.models import Invoice  # noqa: E402
from app.models import TaxFiling  # noqa: E402

KIND_LABELS = {"document": "Documento", "notification": "Notificación", "case": "Expediente", "filing": "Presentación"}
STATUS_WORDS = {"DISMISSED": "descartado por una persona", "RESOLVED": "resuelto", "WAITING_HUMAN": "pendiente de revisar"}


def supplier_history(database, invoice: Invoice, today: date) -> dict[str, Any]:
    """Qué sabemos de este proveedor: facturas, importes, pagos y lo que ya se decidió."""
    from app.agents.detector import supplier_invoices

    items = [item for item in supplier_invoices(database, invoice) if item.id != invoice.id]
    totals = [float(item.total) for item in items if item.total is not None]
    year_ago = today - timedelta(days=365)
    history: dict[str, Any] = {
        "supplier": invoice.supplier_name,
        "tax_id": invoice.supplier_tax_id,
        "invoices": len(items),
        "first_date": items[0].invoice_date if items else None,
        "last_date": items[-1].invoice_date if items else None,
        "median_total": round(statistics.median(totals), 2) if totals else None,
        "max_total": max(totals) if totals else None,
        "total_12m": round(sum(float(item.total) for item in items if item.invoice_date and item.invoice_date >= year_ago), 2),
        "paid": sum(1 for item in items if item.paid_at),
        "unpaid": sum(1 for item in items if not item.paid_at),
        "last_invoices": [
            {"id": item.id, "number": item.invoice_number, "date": item.invoice_date, "total": float(item.total), "document_id": item.document_id, "paid": bool(item.paid_at)}
            for item in items[-4:]
        ],
        "cases": [],
    }
    if invoice.supplier_tax_id or invoice.supplier_name:
        condition = Case.subject_tax_id == invoice.supplier_tax_id if invoice.supplier_tax_id else Case.subject_name == invoice.supplier_name
        previous_cases = database.scalars(select(Case).where(Case.kind == "ANOMALY", condition).order_by(Case.id.desc()).limit(5)).all()
        key = invoice.supplier_tax_id or (invoice.supplier_name or "").strip().upper()
        previous_cases += [
            case for case in database.scalars(select(Case).where(Case.kind == "ANOMALY").order_by(Case.id.desc()).limit(200)).all()
            if (case.facts or {}).get("supplier_key") == key and case not in previous_cases
        ]
        for case in previous_cases:
            if case.fingerprint == f"factura:{invoice.id}":
                continue
            history["cases"].append(
                {"id": case.id, "code": case.code, "title": case.title, "status": case.status, "resolution": case.resolution, "date": case.created_at.date() if case.created_at else None}
            )
    return history


class AgenteMemoria(Agent):
    code = "memoria"
    name = "Memoria"
    role = "Recuerda todo lo que ha pasado y encuentra antecedentes con evidencia documental."
    icon = "brain"
    handles = ("notification", "invoice", "deadline")
    needs_case = False
    consumes = ("expediente o evento", "organismo, trámite, proveedor o modelo")
    produces = ("antecedentes con evidencia", "histórico del proveedor", "qué se decidió la última vez")

    def run(self, ctx: AgentContext) -> StepResult:
        kind = ctx.event.kind if ctx.event else "notification"
        if kind == "invoice":
            return self.run_invoice(ctx)
        if kind == "deadline":
            return self.run_deadline(ctx)
        return self.run_notification(ctx)

    def run_invoice(self, ctx: AgentContext) -> StepResult:
        invoice = ctx.facts["invoice_obj"]
        history = supplier_history(ctx.database, invoice, ctx.today)
        ctx.facts["supplier_history"] = history
        antecedents = [
            {
                "kind": "case",
                "ref_id": item["id"],
                "case_id": item["id"],
                "title": f"{item['code']} · {item['title']}",
                "date": item["date"],
                "why": "Mismo proveedor",
                "outcome": item["resolution"] or STATUS_WORDS.get(item["status"], item["status"].lower()),
            }
            for item in history["cases"]
        ]
        antecedents += [
            {"kind": "document", "ref_id": item["document_id"], "title": f"Factura {item['number'] or 's/n'} · {eur(item['total'])}", "date": item["date"], "why": "Factura anterior del mismo proveedor"}
            for item in reversed(history["last_invoices"])
        ]
        ctx.facts["antecedents"] = antecedents[:5]
        if ctx.case is not None:
            ctx.case.antecedents = jsonable(antecedents[:5])

        findings = []
        dismissed = [item for item in history["cases"] if item["status"] == "DISMISSED"]
        if dismissed:
            last = dismissed[0]
            findings.append(
                Finding(
                    agente=self.code,
                    tipo="ANTECEDENTE_REVISADO",
                    resultado=f"Ya revisaste un aviso de este proveedor ({last['code']})",
                    por_que="Una persona lo dio por correcto" + (f": «{last['resolution']}»" if last.get("resolution") else "") + ".",
                    riesgo="low",
                    confianza=0.9,
                    datos={"case_id": last["id"]},
                    evidencia=[evidence("case", f"{last['code']} · {last['title']}", ref_id=last["id"])],
                    fecha=last["date"].isoformat() if last.get("date") else None,
                    siguiente="Ten en cuenta aquella decisión antes de volver a reclamar.",
                )
            )

        if not history["invoices"]:
            summary = f"Primera factura de {history['supplier'] or 'este proveedor'}: no hay historial."
        else:
            summary = (
                f"{history['invoices']} factura(s) anteriores de {history['supplier']} desde {history['first_date']:%m/%Y}; "
                f"mediana {eur(history['median_total'])}, {history['unpaid']} sin pagar."
            )
            if history["cases"]:
                summary += f" {len(history['cases'])} aviso(s) previos" + (f", el último: {history['cases'][0]['resolution']}" if history["cases"][0].get("resolution") else "") + "."
        return StepResult(
            summary=summary,
            output={"supplier_history": history, "antecedents": antecedents[:5]},
            evidence=[evidence("invoice", f"Factura {item['number'] or 's/n'} · {eur(item['total'])}", document_id=item["document_id"]) for item in history["last_invoices"]],
            engine="memoria (histórico)",
            findings=findings,
        )

    def run_deadline(self, ctx: AgentContext) -> StepResult:
        period = ctx.facts["period"]
        filings = ctx.database.scalars(
            select(TaxFiling).where(TaxFiling.model == period["model"]).order_by(TaxFiling.year.desc(), TaxFiling.period.desc()).limit(4)
        ).all()
        previous = [
            {"year": item.year, "quarter": item.period, "filed_at": item.filed_at, "amount": float(item.amount) if item.amount is not None else None, "reference": item.reference}
            for item in filings
        ]
        ctx.facts["previous_filings"] = previous
        antecedents = [
            {"kind": "filing", "ref_id": item.id, "title": f"Modelo {item.model} {item.period}T {item.year}" + (f" · {eur(item.amount)}" if item.amount is not None else ""), "date": item.filed_at, "why": "Presentación anterior del mismo modelo"}
            for item in filings
        ]
        ctx.facts["antecedents"] = antecedents
        if ctx.case is not None:
            ctx.case.antecedents = jsonable(antecedents)
        summary = (
            f"Última presentación del {period['model']}: {previous[0]['quarter']}T {previous[0]['year']}"
            + (f" por {eur(previous[0]['amount'])}." if previous[0]["amount"] is not None else ".")
            if previous
            else f"No consta ninguna presentación anterior del {period['model']} en CapaFiscal."
        )
        return StepResult(summary=summary, output={"previous_filings": previous}, engine="memoria (histórico)")

    def run_notification(self, ctx: AgentContext) -> StepResult:
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
        own = set(tokenize(" ".join([company_name(database) or "", *company_tax_ids(database)])))
        query_tokens = [
            token for token in tokenize(" ".join(terms + [ctx.text[:800]]))
            if token not in GENERIC and token not in own and not (token.isdigit() and len(token) == 4)
        ]
        query = " ".join(query_tokens)
        corpus = build_corpus(database)
        for hit in search(database, query, limit=4, exclude=seen, corpus=corpus):
            if hit["score"] < 2.5 or len(hit["matched"]) < 2:
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
