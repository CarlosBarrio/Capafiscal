"""
Inteligencia: descargar, guardar sin duplicados, decidir la relevancia, enriquecer y explicar.

    refresh_boe()      sumarios de los últimos días que falten (idempotente; el domingo no hay BOE)
    match()            reglas contra el perfil del cliente → IntelMatch (conserva lo que decidió la persona)
                       solo los candidatos (alta/media) se enriquecen con su ficha oficial y,
                       si hay IA, con un resumen cuyas citas se comprueban en el texto
    overview()         la pantalla: estado de la fuente, recuentos y novedades del radar
    detail()           las siete preguntas de cada novedad, con su evidencia
"""
from __future__ import annotations

from app import clock
import logging
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intelligence import relevance
from app.intelligence.profile import LEGAL_AREAS
from app.intelligence.profile import fingerprint
from app.intelligence.profile import get_profile
from app.intelligence.sources import boe
from app.intelligence.sources.base import FolderTransport
from app.intelligence.sources.base import HttpTransport
from app.intelligence.sources.base import NotPublished
from app.intelligence.sources.base import SourceUnavailable
from app.models import ExternalItem
from app.models import IntelMatch
from app.models import IntelSource

logger = logging.getLogger(__name__)

SOURCES = {
    "boe": {"name": "Boletín Oficial del Estado", "type": "regulation", "url": "https://www.boe.es/datosabiertos/", "status": "active"},
    "bdns": {"name": "Base de Datos Nacional de Subvenciones", "type": "grant", "url": "https://www.infosubvenciones.es/", "status": "pending"},
    "placsp": {"name": "Plataforma de Contratación del Sector Público", "type": "tender", "url": "https://contrataciondelsectorpublico.gob.es/", "status": "pending"},
}
RADARS = {
    "arquitectura": {"label": "Arquitectura", "source": "placsp", "goal": "Encontrar negocio: licitaciones, concursos y obra pública.",
                     "needs": "especialidades, radio en km e importe mínimo"},
    "juridico": {"label": "Jurídico", "source": "boe", "goal": "Entender cambios: disposiciones del BOE que tocan tus áreas.",
                 "needs": "áreas de práctica y comunidad autónoma"},
    "subvenciones": {"label": "Subvenciones", "source": "bdns", "goal": "Encontrar oportunidades: ayudas a las que podrías optar.",
                     "needs": "CNAE, empleados, facturación, ubicación e intereses"},
}
VISIBLE = ("alta", "media")
AI_PER_RUN = 10


def now() -> datetime:
    return clock.now()


def ensure_sources(database: Session) -> dict[str, IntelSource]:
    existing = {source.code: source for source in database.scalars(select(IntelSource)).all()}
    for code, spec in SOURCES.items():
        if code not in existing:
            existing[code] = IntelSource(code=code, name=spec["name"], type=spec["type"], url=spec["url"], status=spec["status"], meta={})
            database.add(existing[code])
    database.flush()
    return existing


def default_transport():
    from app.config import settings

    return FolderTransport(settings.intel_boe_folder) if settings.intel_boe_folder else HttpTransport()


# ---------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------


def store_items(database: Session, rows: list[dict[str, Any]]) -> int:
    """Guarda las disposiciones nuevas (por identificador); devuelve cuántas eran nuevas."""
    ids = [row["external_id"] for row in rows]
    known = set(database.scalars(select(ExternalItem.external_id).where(ExternalItem.source_code == "boe", ExternalItem.external_id.in_(ids))).all()) if ids else set()
    retrieved = now()
    new = 0
    for row in rows:
        if row["external_id"] in known:
            continue
        known.add(row["external_id"])
        database.add(ExternalItem(
            source_code="boe", type="regulation", external_id=row["external_id"], title=row["title"],
            section=row["section"], department=row["department"], category=row["category"],
            publication_date=row["publication_date"], source_url=row["html_url"], pdf_url=row["pdf_url"], xml_url=row["xml_url"],
            raw={"section_code": row["section_code"], **row["raw"]}, subjects=[], requirements={}, retrieved_at=retrieved,
            evidence=[{"claim": "Publicación", "source": "BOE", "document": row["external_id"], "date": row["publication_date"].isoformat(),
                       "url": row["html_url"], "fragment": row["title"], "retrieved_at": retrieved.isoformat()}],
        ))
        new += 1
    database.flush()
    return new


def fetch_boe_day(database: Session, day: date, transport=None, *, payload: bytes | None = None) -> dict[str, Any]:
    source = ensure_sources(database)["boe"]
    transport = transport or default_transport()
    meta = dict(source.meta or {})
    days = dict(meta.get("days") or {})
    source.last_checked = now()
    try:
        content = payload if payload is not None else transport.get(boe.SUMMARY_URL.format(day=day), accept="application/json")
        rows = boe.parse_summary(content, day)
    except NotPublished:
        days[day.isoformat()] = {"items": 0, "new": 0, "note": "sin BOE ese día", "at": now().isoformat()}
        meta["days"] = days
        source.meta = meta
        source.status, source.last_error = "active", None
        return {"day": day.isoformat(), "items": 0, "new": 0, "published": False}
    except SourceUnavailable as error:
        source.status, source.last_error = "error", str(error)
        raise
    except (ValueError, KeyError) as error:
        source.status, source.last_error = "error", f"Sumario ilegible del {day:%d/%m/%Y}: {error}"
        raise SourceUnavailable(source.last_error) from error
    new = store_items(database, rows)
    days[day.isoformat()] = {"items": len(rows), "new": new, "at": now().isoformat()}
    meta["days"] = days
    source.meta = meta
    source.status, source.last_error, source.last_success = "active", None, now()
    return {"day": day.isoformat(), "items": len(rows), "new": new, "published": True}


def refresh_boe(database: Session, *, today: date | None = None, back_days: int = 3, transport=None, force: bool = False) -> dict[str, Any]:
    """Los sumarios de hoy y de los días anteriores que aún no estén descargados."""
    today = today or clock.today()
    source = ensure_sources(database)["boe"]
    done = (source.meta or {}).get("days") or {}
    results, errors = [], []
    for offset in range(back_days):
        day = today - timedelta(days=offset)
        if not force and day.isoformat() in done and day != today:
            continue
        try:
            results.append(fetch_boe_day(database, day, transport))
        except SourceUnavailable as error:
            errors.append(str(error))
            break  # si la fuente no responde, no tiene sentido seguir pidiendo días
    database.flush()
    return {"days": results, "new": sum(item["new"] for item in results), "errors": errors, "status": source.status, "last_error": source.last_error}


def enrich(database: Session, item: ExternalItem, transport=None) -> bool:
    """Ficha oficial (rango, entrada en vigor, materias, texto). Solo para candidatos y una sola vez."""
    if item.detail_fetched:
        return True
    transport = transport or default_transport()
    try:
        detail = boe.parse_document(transport.get(boe.DOCUMENT_URL.format(id=item.external_id), accept="application/xml"))
    except (SourceUnavailable, NotPublished, ValueError) as error:
        logger.info("Sin ficha de %s: %s", item.external_id, error)
        return False
    except Exception as error:  # XML mal formado
        logger.info("Ficha ilegible de %s: %s", item.external_id, error)
        return False
    item.rank = detail["rank"] or item.rank
    item.effective_date = detail["effective_date"]
    item.subjects = detail["subjects"]
    item.content = detail["text"] or None
    item.detail_fetched = True
    retrieved = now().isoformat()
    evidence = list(item.evidence or [])
    url = item.xml_url or boe.DOCUMENT_URL.format(id=item.external_id)
    if detail["rank"]:
        evidence.append({"claim": "Rango", "source": "BOE", "document": item.external_id, "date": item.publication_date.isoformat() if item.publication_date else None,
                         "url": url, "fragment": f"rango: {detail['rank']}", "retrieved_at": retrieved})
    if detail["effective_date"]:
        evidence.append({"claim": "Entrada en vigor", "source": "BOE", "document": item.external_id, "date": detail["effective_date"].isoformat(),
                         "url": url, "fragment": f"fecha_vigencia: {detail['effective_date']:%d/%m/%Y}", "retrieved_at": retrieved})
    if detail["subjects"]:
        evidence.append({"claim": "Materias (clasificación oficial del BOE)", "source": "BOE", "document": item.external_id, "date": None,
                         "url": url, "fragment": "; ".join(detail["subjects"][:12]), "retrieved_at": retrieved})
    item.evidence = evidence
    return True


def add_analysis(item: ExternalItem, areas: list[str]) -> bool:
    from app.intelligence.summarizer import summarize

    result = summarize(item.title, item.content or "", areas)
    if result is None:
        return False
    item.analysis = result
    if result.get("engine") != "reglas":
        url = item.source_url
        evidence = [entry for entry in (item.evidence or []) if not entry.get("claim", "").startswith("IA ·")]
        for field, label in (("que_cambia", "Qué cambia"), ("a_quien_afecta", "A quién afecta"), ("entrada_en_vigor", "Entrada en vigor")):
            for claim in result.get(field) or []:
                evidence.append({"claim": f"IA · {label}: {claim['text']}", "source": "BOE", "document": item.external_id,
                                 "date": item.publication_date.isoformat() if item.publication_date else None, "url": url,
                                 "fragment": claim["quote"], "verified": True})
        item.evidence = evidence
    return True


# ---------------------------------------------------------------------
# Relevancia para el cliente
# ---------------------------------------------------------------------


def as_row(item: ExternalItem) -> dict[str, Any]:
    return {"title": item.title, "section_code": (item.raw or {}).get("section_code"), "section": item.section, "department": item.department,
            "category": item.category, "subjects": item.subjects or [], "rank": item.rank}


def match(database: Session, radar: str = "juridico", *, today: date | None = None, transport=None, use_ai: bool = True) -> dict[str, Any]:
    if radar != "juridico":
        return {"radar": radar, "assessed": 0, "visible": 0, "note": "conector pendiente"}
    today = today or clock.today()
    from app.config import settings

    profile = get_profile(database)
    version = fingerprint(profile, radar)
    since = today - timedelta(days=settings.intel_window_days)
    items = database.scalars(select(ExternalItem).where(ExternalItem.source_code == "boe", ExternalItem.publication_date >= since)).all()
    existing = {entry.item_id: entry for entry in database.scalars(select(IntelMatch).where(IntelMatch.radar == radar)).all()}
    transport = transport or default_transport()
    visible = enriched = analysed = 0
    for item in items:
        current = existing.get(item.id)
        if current is not None and current.profile_version == version and (item.detail_fetched or current.relevance not in VISIBLE):
            visible += current.relevance in VISIBLE
            continue
        verdict = relevance.assess_regulation(as_row(item), profile)
        if verdict["relevance"] in VISIBLE and not item.detail_fetched and enrich(database, item, transport):
            enriched += 1
            verdict = relevance.assess_regulation(as_row(item), profile)  # las materias oficiales pueden cambiarla
        if current is None:
            if verdict["relevance"] not in VISIBLE:
                continue  # lo irrelevante no se guarda por cliente
            current = IntelMatch(item_id=item.id, radar=radar, status="nueva")
            database.add(current)
        current.relevance, current.areas, current.reasons, current.profile_version = verdict["relevance"], verdict["areas"], verdict["reasons"], version
        visible += verdict["relevance"] in VISIBLE
    database.flush()
    if use_ai:
        pending = database.execute(
            select(ExternalItem, IntelMatch).join(IntelMatch, IntelMatch.item_id == ExternalItem.id)
            .where(IntelMatch.radar == radar, IntelMatch.relevance.in_(VISIBLE), ExternalItem.analysis.is_(None), ExternalItem.content.is_not(None))
            .order_by(IntelMatch.relevance, ExternalItem.publication_date.desc()).limit(AI_PER_RUN)
        ).all()
        for item, entry in pending:
            analysed += add_analysis(item, entry.areas)
    database.flush()
    return {"radar": radar, "assessed": len(items), "visible": visible, "enriched": enriched, "analysed": analysed}


def run(database: Session, *, today: date | None = None, transport=None) -> dict[str, Any]:
    """El trabajo diario: descargar lo que falte y recalcular la relevancia de este cliente."""
    fetched = refresh_boe(database, today=today, transport=transport)
    matched = match(database, "juridico", today=today, transport=transport)
    return {"fetched": fetched, "matched": matched}


# ---------------------------------------------------------------------
# Lo que ve la persona
# ---------------------------------------------------------------------


def source_view(source: IntelSource | None, code: str) -> dict[str, Any]:
    spec = SOURCES[code]
    if source is None:
        return {"code": code, "name": spec["name"], "status": spec["status"], "url": spec["url"], "last_checked": None, "last_success": None, "last_error": None}
    return {"code": code, "name": source.name, "status": source.status, "url": source.url, "last_error": source.last_error,
            "last_checked": source.last_checked.isoformat() if source.last_checked else None,
            "last_success": source.last_success.isoformat() if source.last_success else None,
            "days": sorted(((source.meta or {}).get("days") or {}).items(), reverse=True)[:7]}


def card(item: ExternalItem, entry: IntelMatch) -> dict[str, Any]:
    why = next((reason["label"] for reason in entry.reasons if reason.get("ok")), "")
    return {
        "id": entry.id, "radar": entry.radar, "type": item.type, "title": item.title, "relevance": entry.relevance, "status": entry.status,
        "areas": [LEGAL_AREAS.get(area, area) for area in entry.areas], "publication_date": item.publication_date.isoformat() if item.publication_date else None,
        "effective_date": item.effective_date.isoformat() if item.effective_date else None, "rank": item.rank, "department": item.department,
        "external_id": item.external_id, "source": SOURCES[item.source_code]["name"], "source_url": item.source_url, "pdf_url": item.pdf_url,
        "why": why, "has_summary": bool(item.analysis and item.analysis.get("engine") != "reglas"),
    }


def overview(database: Session, radar: str, *, include_dismissed: bool = False) -> dict[str, Any]:
    if radar not in RADARS:
        raise ValueError("Radar desconocido.")
    spec = RADARS[radar]
    sources = {source.code: source for source in database.scalars(select(IntelSource)).all()}
    profile = get_profile(database)
    result: dict[str, Any] = {"radar": radar, "label": spec["label"], "goal": spec["goal"], "source": source_view(sources.get(spec["source"]), spec["source"]),
                              "profile": profile, "radars": [{"code": code, "label": item["label"], "active": SOURCES[item["source"]]["status"] == "active"} for code, item in RADARS.items()]}
    if SOURCES[spec["source"]]["status"] != "active":
        result.update(items=[], counts={}, pending_connector=True,
                      message=f"El conector de {SOURCES[spec['source']]['name']} es el siguiente. Cuando esté, este radar usará: {spec['needs']}.")
        return result
    rows = database.execute(
        select(ExternalItem, IntelMatch).join(IntelMatch, IntelMatch.item_id == ExternalItem.id)
        .where(IntelMatch.radar == radar, IntelMatch.relevance.in_(VISIBLE), IntelMatch.profile_version == fingerprint(profile, radar))
        .order_by(ExternalItem.publication_date.desc(), ExternalItem.id.desc())
    ).all()
    cards = [card(item, entry) for item, entry in rows if include_dismissed or entry.status != "descartada"]
    cards.sort(key=lambda row: (row["status"] != "nueva", row["relevance"] != "alta", -(date.fromisoformat(row["publication_date"]).toordinal() if row["publication_date"] else 0)))
    result.update(
        items=cards, pending_connector=False,
        counts={"total": len(cards), "alta": sum(row["relevance"] == "alta" for row in cards), "media": sum(row["relevance"] == "media" for row in cards),
                "nuevas": sum(row["status"] == "nueva" for row in cards), "revisadas": sum(row["status"] == "revisada" for row in cards)},
        needs_profile=not profile["juridico"]["areas"],
    )
    return result


def detail(database: Session, entry: IntelMatch) -> dict[str, Any]:
    """Las siete preguntas: qué ha pasado, de dónde viene, por qué es relevante, a quién afecta,
    qué significa, qué puedo hacer y qué evidencia lo demuestra."""
    item = database.get(ExternalItem, entry.item_id)
    analysis = item.analysis if item.analysis and item.analysis.get("engine") != "reglas" else None
    excerpt = (item.content or "")[:1200] or None
    return {
        **card(item, entry),
        "what_happened": {"title": item.title, "rank": item.rank, "summary": analysis.get("que_cambia") if analysis else [],
                          "excerpt": excerpt},
        "where_from": {"source": SOURCES[item.source_code]["name"], "document": item.external_id, "section": item.section, "department": item.department,
                       "category": item.category, "publication_date": item.publication_date.isoformat() if item.publication_date else None,
                       "html_url": item.source_url, "pdf_url": item.pdf_url, "xml_url": item.xml_url,
                       "retrieved_at": item.retrieved_at.isoformat() if item.retrieved_at else None},
        "why_relevant": entry.reasons,
        "who_is_affected": analysis.get("a_quien_afecta") if analysis else [],
        "what_it_means": {"effective_date": item.effective_date.isoformat() if item.effective_date else None,
                          "effective_claims": analysis.get("entrada_en_vigor") if analysis else [], "subjects": item.subjects or []},
        "what_to_do": {"suggestions": analysis.get("que_revisar") if analysis else [],
                       "suggestions_note": "Sugerencias de la IA a partir del texto; no forman parte de la norma." if analysis else None},
        "evidence": item.evidence or [],
        "ai": {"engine": analysis.get("engine") if analysis else None, "discarded": analysis.get("discarded", 0) if analysis else 0,
               "fallback": (item.analysis or {}).get("fallback") if not analysis else None, "truncated": bool(analysis and analysis.get("truncated"))},
        "detail_fetched": item.detail_fetched,
    }


def set_status(database: Session, entry: IntelMatch, status: str, actor: str | None) -> IntelMatch:
    if status not in {"nueva", "revisada", "descartada"}:
        raise ValueError("Estado no válido: nueva, revisada o descartada.")
    entry.status, entry.decided_by, entry.decided_at = status, actor, now()
    from app.invoice_service import add_audit_event

    add_audit_event(database, action=f"intelligence.{status}", entity_type="intel_match", entity_id=entry.id, actor=actor or "persona",
                    event_data={"item_id": entry.item_id, "radar": entry.radar})
    database.flush()
    return entry
