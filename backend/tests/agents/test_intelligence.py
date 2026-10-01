"""Inteligencia · radar jurídico: BOE → reglas → ficha oficial → (IA validada) → pantalla, con evidencia.

Los datos son un sumario SINTÉTICO con el formato de la API oficial del BOE (tests/fixtures/boe).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "boe"
DAY = date(2026, 9, 29)
PROFILE = {"common": {"region": "Comunidad de Madrid"}, "juridico": {"areas": ["laboral", "mercantil"]}}


@pytest.fixture()
def boe(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "intel_boe_folder", FIXTURES)
    client.put("/api/company", json={"name": "Despacho Ejemplo S.L.P.", "tax_id": "B00100016", "legal_form": "SOCIEDAD"})
    client.put("/api/intelligence/profile", json=PROFILE).raise_for_status()
    response = client.post("/api/intelligence/refresh", json={"day": DAY.isoformat(), "back_days": 1})
    response.raise_for_status()
    return response.json()


def by_id(overview):
    return {item["external_id"]: item for item in overview["items"]}


def test_parser_accepts_object_or_list_like_the_official_api():
    from app.intelligence.sources.boe import parse_summary

    rows = parse_summary((FIXTURES / "20260929.json").read_bytes(), DAY)
    assert len(rows) == 7 and len({row["external_id"] for row in rows}) == 7
    single = next(row for row in rows if row["external_id"] == "BOE-A-2026-90001")  # epígrafe e item como objeto
    assert single["section_code"] == "1" and single["category"] == "Sociedades" and single["publication_date"] == DAY
    direct = next(row for row in rows if row["external_id"] == "BOE-A-2026-90004")  # item directo en el departamento
    assert direct["department"] == "COMUNIDAD AUTÓNOMA DE CATALUÑA"


def test_rules_decide_what_matters_for_the_profile(client, boe):
    assert boe["fetched"]["new"] == 7 and not boe["fetched"]["errors"]
    overview = client.get("/api/intelligence", params={"radar": "juridico"}).json()
    items = by_id(overview)
    # Relevantes: la ley de sociedades (mercantil) y el real decreto de jornada (laboral), alta;
    # el convenio colectivo (sección III) y la corrección de errores, media.
    assert items["BOE-A-2026-90001"]["relevance"] == "alta"
    assert items["BOE-A-2026-90002"]["relevance"] == "alta"
    assert items["BOE-A-2026-90006"]["relevance"] == "media"
    assert items["BOE-A-2026-90007"]["relevance"] == "media"
    # Fuera: fiscal (no configurado), otra comunidad autónoma, oposiciones.
    assert {"BOE-A-2026-90003", "BOE-A-2026-90004", "BOE-A-2026-90005"}.isdisjoint(items)
    assert overview["counts"] == {"total": 4, "alta": 2, "media": 2, "nuevas": 4, "revisadas": 0}
    assert overview["source"]["status"] == "active" and overview["source"]["last_success"]
    assert items["BOE-A-2026-90002"]["effective_date"] == "2026-11-01"  # de la ficha oficial, no deducido
    assert "jornada" in items["BOE-A-2026-90002"]["why"]


def test_detail_answers_the_seven_questions_with_evidence(client, boe):
    items = by_id(client.get("/api/intelligence", params={"radar": "juridico"}).json())
    detail = client.get(f"/api/intelligence/items/{items['BOE-A-2026-90002']['id']}").json()
    assert detail["what_happened"]["rank"] == "Real Decreto" and "registro diario de jornada" in detail["what_happened"]["excerpt"]
    assert detail["where_from"]["document"] == "BOE-A-2026-90002" and detail["where_from"]["html_url"].startswith("https://www.boe.es/")
    assert any(reason["ok"] and "laboral" in reason["label"].lower() for reason in detail["why_relevant"])
    assert detail["what_it_means"]["effective_date"] == "2026-11-01" and "Jornada de trabajo" in detail["what_it_means"]["subjects"]
    claims = {entry["claim"] for entry in detail["evidence"]}
    assert {"Publicación", "Rango", "Entrada en vigor", "Materias (clasificación oficial del BOE)"} <= claims
    assert all(entry["url"] and entry["fragment"] for entry in detail["evidence"])
    # Sin IA no hay resumen ni «a quién afecta» inventados.
    assert detail["who_is_affected"] == [] and detail["what_happened"]["summary"] == [] and detail["ai"]["engine"] is None


def test_ai_summary_keeps_only_claims_whose_quote_is_in_the_official_text(client, monkeypatch, boe):
    from app.agents import llm
    from app.database import SessionLocal
    from app.intelligence.service import add_analysis
    from app.models import ExternalItem

    answer = {
        "que_cambia": [
            {"texto": "Obliga a conservar el registro de jornada en formato digital cuatro años.",
             "cita": "deberán conservar el registro diario de jornada en formato digital durante cuatro años"},
            {"texto": "Multa de 10.000 € por incumplir.", "cita": "se sancionará con multa de 10.000 euros"},  # no está en el texto
        ],
        "a_quien_afecta": [{"texto": "Empresas con trabajadores por cuenta ajena.", "cita": "todas las empresas con personas trabajadoras por cuenta ajena"}],
        "entrada_en_vigor": [{"texto": "1 de noviembre de 2026.", "cita": "entrará en vigor el 1 de noviembre de 2026"}],
        "que_revisar": ["Revisar cómo guardan hoy el registro los clientes con plantilla."],
    }
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "_complete", lambda **kwargs: (json.dumps(answer), {"model": "simulado", "served_by": "simulado"}))
    with SessionLocal() as database:
        item = database.query(ExternalItem).filter_by(external_id="BOE-A-2026-90002").one()
        assert add_analysis(item, ["laboral"])
        database.commit()

    items = by_id(client.get("/api/intelligence", params={"radar": "juridico"}).json())
    detail = client.get(f"/api/intelligence/items/{items['BOE-A-2026-90002']['id']}").json()
    assert [claim["text"] for claim in detail["what_happened"]["summary"]] == ["Obliga a conservar el registro de jornada en formato digital cuatro años."]
    assert detail["ai"]["discarded"] == 1  # la multa inventada no pasa
    assert detail["who_is_affected"][0]["quote"].startswith("todas las empresas")
    assert detail["what_to_do"]["suggestions"] and "no forman parte de la norma" in detail["what_to_do"]["suggestions_note"]
    verified = [entry for entry in detail["evidence"] if entry["claim"].startswith("IA ·")]
    assert len(verified) == 3 and all(entry.get("verified") for entry in verified)


def test_ai_without_any_verifiable_quote_gives_no_summary(monkeypatch):
    from app.agents import llm
    from app.intelligence.summarizer import summarize

    monkeypatch.setattr(llm, "available", lambda: True)
    invented = {"que_cambia": [{"texto": "Algo", "cita": "esto no aparece en ninguna parte del texto oficial"}], "a_quien_afecta": [],
                "entrada_en_vigor": [], "que_revisar": ["x"]}
    monkeypatch.setattr(llm, "_complete", lambda **kwargs: (json.dumps(invented), {"model": "simulado"}))
    result = summarize("Título", "Artículo 1. Texto oficial de la disposición de prueba con varias palabras.", ["laboral"])
    assert result["engine"] == "reglas" and "cita comprobable" in result["fallback"]


def test_refresh_is_idempotent_and_sunday_is_not_an_error(client, boe):
    again = client.post("/api/intelligence/refresh", json={"day": DAY.isoformat(), "back_days": 1}).json()
    assert again["fetched"]["new"] == 0
    sunday = client.post("/api/intelligence/refresh", json={"day": "2026-09-27", "back_days": 1}).json()
    assert sunday["fetched"]["days"][0]["published"] is False and not sunday["fetched"]["errors"]
    assert client.get("/api/intelligence").json()["counts"]["total"] == 4


def test_source_down_is_reported_and_nothing_is_invented(client, monkeypatch):
    from app.intelligence.sources.base import HttpTransport
    from app.intelligence.sources.base import SourceUnavailable

    def refused(self, url, *, accept):
        raise SourceUnavailable("No se pudo conectar con www.boe.es: ConnectError")

    monkeypatch.setattr(HttpTransport, "get", refused)
    client.put("/api/intelligence/profile", json=PROFILE)
    result = client.post("/api/intelligence/refresh", json={"day": DAY.isoformat(), "back_days": 2}).json()
    assert result["fetched"]["errors"] and result["fetched"]["status"] == "error"
    overview = client.get("/api/intelligence").json()
    assert overview["items"] == [] and "www.boe.es" in overview["source"]["last_error"]


def test_profile_change_recomputes_and_status_is_remembered(client, boe):
    items = by_id(client.get("/api/intelligence").json())
    dismissed = items["BOE-A-2026-90007"]["id"]
    assert client.post(f"/api/intelligence/items/{dismissed}/status", json={"status": "descartada"}).json()["status"] == "descartada"
    client.put("/api/intelligence/profile", json={"juridico": {"areas": ["laboral", "mercantil", "fiscal"]}}).raise_for_status()
    items = by_id(client.get("/api/intelligence").json())
    assert "BOE-A-2026-90003" in items  # ahora toca fiscal
    assert "BOE-A-2026-90007" not in items  # lo descartado sigue descartado
    every = by_id(client.get("/api/intelligence", params={"include_dismissed": True}).json())
    assert every["BOE-A-2026-90007"]["status"] == "descartada"


def test_pending_radars_say_so_instead_of_showing_fake_results(client):
    for radar in ("arquitectura", "subvenciones"):
        overview = client.get("/api/intelligence", params={"radar": radar}).json()
        assert overview["pending_connector"] is True and overview["items"] == [] and "siguiente" in overview["message"]
    assert client.get("/api/intelligence", params={"radar": "astrologia"}).status_code == 404


def test_import_a_downloaded_summary_when_there_is_no_network(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "intel_boe_folder", FIXTURES)  # las fichas sí, desde carpeta
    client.put("/api/intelligence/profile", json=PROFILE)
    response = client.post("/api/intelligence/import", data={"day": DAY.isoformat()},
                           files={"uploaded_file": ("20260929.json", (FIXTURES / "20260929.json").read_bytes(), "application/json")})
    assert response.status_code == 200, response.text
    assert response.json()["fetched"]["new"] == 7
    bad = client.post("/api/intelligence/import", data={"day": DAY.isoformat()}, files={"uploaded_file": ("x.json", b"no es json", "application/json")})
    assert bad.status_code == 422
