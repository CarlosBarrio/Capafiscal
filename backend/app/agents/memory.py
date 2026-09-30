"""
Memoria documental: índice de búsqueda sobre todo lo que ha pasado por la
empresa (documentos leídos, notificaciones y expedientes) para encontrar
antecedentes y responder preguntas con evidencia.

Es un índice BM25 local, sin dependencias ni servicios externos; con volumen
de gestoría se sustituiría por búsqueda vectorial (pgvector) sin cambiar la
interfaz.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extractor import normalize_search_text
from app.models import Case
from app.models import Document
from app.models import ExtractionRun
from app.models import FiscalNotification

STOPWORDS = set(
    """
    a al algo algun alguna algunas alguno algunos ante antes aqui asi aun con contra cual cuales cuando de del
    desde donde dos el ella ellas ellos en entre era es esa esas ese eso esos esta estas este esto estos fue ha
    han hasta hay la las le les lo los mas me mi mis mucho muy no nos o os otra otras otro otros para pero poco
    por porque que quien se ser si sin sobre son su sus tambien te tiene tu tus un una unas uno unos usted ya
    dicho dicha dichos dichas cuyo cuya segun mediante asimismo ademas presente presentes articulo ley fecha
    """.split()
)
TOKEN = re.compile(r"[a-z0-9]{3,}")
WINDOW = 420


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN.findall(normalize_search_text(text or "")) if token not in STOPWORDS]


@dataclass
class Entry:
    kind: str
    ref_id: int
    title: str
    text: str
    date: str | None
    tokens: list[str]


def latest_texts(database: Session) -> dict[int, str]:
    rows = database.execute(
        select(ExtractionRun.document_id, ExtractionRun.raw_text)
        .where(ExtractionRun.raw_text.is_not(None))
        .order_by(ExtractionRun.id.asc())
    ).all()
    return {document_id: text for document_id, text in rows if text}


def build_corpus(database: Session) -> list[Entry]:
    entries: list[Entry] = []
    texts = latest_texts(database)

    for document in database.scalars(select(Document)).all():
        text = texts.get(document.id)
        if not text:
            continue
        invoice = document.invoice
        title = document.original_filename
        if invoice is not None:
            party = invoice.customer_name if invoice.direction == "ISSUED" else invoice.supplier_name
            title = " · ".join(filter(None, [party, invoice.invoice_number, document.original_filename]))
        entries.append(
            Entry("document", document.id, title, text, document.created_at.date().isoformat() if document.created_at else None, tokenize(text))
        )

    for notification in database.scalars(select(FiscalNotification)).all():
        text = " ".join(filter(None, [notification.title, notification.reference, notification.summary, notification.notes]))
        entries.append(
            Entry(
                "notification",
                notification.id,
                f"{notification.title}{f' · {notification.reference}' if notification.reference else ''}",
                text,
                (notification.notified_at or notification.available_at or notification.created_at.date()).isoformat(),
                tokenize(text),
            )
        )

    for case in database.scalars(select(Case)).all():
        text = " ".join(filter(None, [case.title, case.summary, case.subject_name, case.reference, case.resolution]))
        entries.append(
            Entry("case", case.id, f"{case.code or 'Expediente'} · {case.title}", text, case.created_at.date().isoformat() if case.created_at else None, tokenize(text))
        )

    return entries


def best_snippet(text: str, terms: set[str]) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= WINDOW:
        return compact
    normalized = normalize_search_text(compact)
    best_start, best_hits = 0, -1
    for start in range(0, max(1, len(compact) - WINDOW), 80):
        window = normalized[start:start + WINDOW]
        hits = sum(window.count(term) for term in terms)
        if hits > best_hits:
            best_start, best_hits = start, hits
    snippet = compact[best_start:best_start + WINDOW]
    return ("…" if best_start else "") + snippet + ("…" if best_start + WINDOW < len(compact) else "")


def search(
    database: Session,
    query: str,
    *,
    limit: int = 5,
    exclude: set[tuple[str, int]] | None = None,
    kinds: set[str] | None = None,
    corpus: list[Entry] | None = None,
) -> list[dict[str, Any]]:
    corpus = corpus if corpus is not None else build_corpus(database)
    terms = tokenize(query)
    if not terms or not corpus:
        return []

    candidates = [entry for entry in corpus if (not kinds or entry.kind in kinds) and (entry.kind, entry.ref_id) not in (exclude or set())]
    if not candidates:
        return []

    document_frequency: Counter[str] = Counter()
    for entry in corpus:
        document_frequency.update(set(entry.tokens))

    total = len(corpus)
    average_length = sum(len(entry.tokens) for entry in corpus) / total or 1
    k1, b = 1.4, 0.75
    unique_terms = set(terms)
    results = []

    for entry in candidates:
        counts = Counter(entry.tokens)
        length = len(entry.tokens) or 1
        score = 0.0
        matched = []
        for term in unique_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            matched.append(term)
            idf = math.log(1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += idf * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length / average_length))
        if score <= 0:
            continue
        # Premia cubrir varios términos de la consulta, no repetir uno.
        score *= 0.5 + 0.5 * len(matched) / len(unique_terms)
        results.append(
            {
                "kind": entry.kind,
                "ref_id": entry.ref_id,
                "title": entry.title,
                "date": entry.date,
                "score": round(score, 3),
                "matched": sorted(matched),
                "snippet": best_snippet(entry.text, set(matched)),
            }
        )

    results.sort(key=lambda item: -item["score"])
    return results[:limit]


def answer(database: Session, question: str) -> dict[str, Any]:
    """Respuesta con evidencia: IA si está disponible; si no, extractiva."""
    from app.agents import llm

    sources = search(database, question, limit=5)
    text = llm.answer_with_sources(question, sources)
    engine = llm.engine_label() if text else "memoria (búsqueda)"

    if not text:
        if not sources:
            text = "No encuentro nada en la documentación que responda a esa pregunta."
        else:
            lines = [f"He encontrado {len(sources)} fuente(s) relacionadas. La más relevante:"]
            lines.append(f"[1] {sources[0]['title']}: {sources[0]['snippet']}")
            text = "\n".join(lines)

    return {"question": question, "answer": text, "sources": sources, "engine": engine}
