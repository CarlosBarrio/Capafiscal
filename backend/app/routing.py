"""
Routing de Claude basado en datos: cuándo merece la pena llamarlo.

    reglas claras          → reglas (no se llama a nadie)
    reglas dudosas         → ¿este TIPO de duda la resuelve Claude? → la política lo dice
    resultado de Claude    → validación determinista (interpretation.merge)
    sin evidencia          → persona

La política se guarda en DATA_DIR/routing_policy.json y se genera con datos:

    python -m evaluation politica --informe evaluation/informes/comparar_<banco>_<fecha>.json

Por cada tipo de motivo (falta el NIF, importes que no cuadran, categoría…)
cuenta en cuántos documentos Claude mejoró, empató o empeoró, y lo desactiva
si nunca mejora o empeora tanto como mejora. Las correcciones humanas a
campos que cambió Claude también cuentan como empeorar. Sin política (o sin
datos para un motivo), Claude entra como hasta ahora.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

MIN_EVIDENCE = 3  # documentos con ese motivo para decidir algo


def reason_key(reason: str) -> str:
    """«falta supplier_tax_id» → falta_supplier_tax_id; «invoice_number corregido 2 veces…» → aprendizaje."""
    text = reason.lower()
    if text.startswith("falta "):
        return "falta_" + text.split(" ", 1)[1].strip().replace(" ", "_")
    rules = (
        (r"importes no cuadran", "importes"), (r"propia empresa", "emisor_empresa"), (r"categor", "categoria"),
        (r"n[uú]mero de factura", "numero"), (r"escaneado", "escaneado"), (r"corregido \d+ veces", "aprendizaje"),
    )
    for pattern, key in rules:
        if re.search(pattern, text):
            return key
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:40] or "otro"


def policy_path() -> Path:
    from app.config import settings

    return settings.data_dir / "routing_policy.json"


def load_policy() -> dict[str, Any]:
    path = policy_path()
    if not path.exists():
        return {"source": "por defecto: Claude entra con cualquier duda", "reasons": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"source": "política ilegible: se usa la de por defecto", "reasons": {}}


def allowed_reasons(reasons: list[str], policy: dict[str, Any] | None = None) -> list[str]:
    """Los motivos para los que la política permite llamar a Claude."""
    policy = policy if policy is not None else load_policy()
    rules = policy.get("reasons") or {}
    return [reason for reason in reasons if (rules.get(reason_key(reason)) or {}).get("use_claude", True)]


def derive_policy(report: dict[str, Any], *, engine: str = "hibrido", corrections: dict[str, int] | None = None) -> dict[str, Any]:
    """Política a partir de un informe de `evaluation comparar` (y, si las hay, de correcciones humanas a Claude)."""
    versus = (report.get("analysis") or {}).get("versus", {}).get(engine)
    if not versus:
        raise ValueError(f"El informe no tiene resultados del motor «{engine}» (¿se ejecutó con ANTHROPIC_API_KEY?).")
    tally: dict[str, dict[str, int]] = defaultdict(lambda: {"mejora": 0, "igual": 0, "empeora": 0})
    for item in versus.get("routed_detail", []):
        for reason in item.get("reasons") or ["forzado"]:
            tally[reason_key(reason)][item["verdict"]] += 1
    for key, count in (corrections or {}).items():
        tally[key]["empeora"] += count
    reasons = {}
    for key, counts in tally.items():
        seen = sum(counts.values())
        if seen < MIN_EVIDENCE:
            decision, why = True, f"pocos datos ({seen}): se mantiene Claude"
        elif counts["mejora"] == 0:
            decision, why = False, f"nunca mejoró en {seen} documentos"
        elif counts["empeora"] >= counts["mejora"]:
            decision, why = False, f"empeora tanto como mejora ({counts['empeora']} frente a {counts['mejora']})"
        else:
            decision, why = True, f"mejora en {counts['mejora']} de {seen}"
        reasons[key] = {"use_claude": decision, "why": why, **counts}
    return {
        "source": f"informe {report.get('dataset')} del {report.get('date')} (motor {engine})",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reasons": reasons,
        "cost_per_fix": versus.get("cost_per_fix"),
    }


def save_policy(policy: dict[str, Any]) -> Path:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def claude_corrections(database) -> dict[str, int]:
    """Correcciones humanas a campos que puso Claude, agrupadas por el motivo que lo llamó."""
    from sqlalchemy import select

    from app.models import AuditEvent
    from app.models import DecisionRecord

    counts: dict[str, int] = defaultdict(int)
    records = database.scalars(select(DecisionRecord).where(DecisionRecord.engine == "claude", DecisionRecord.outcome == "corregido")).all()
    for record in records:
        audit = database.scalar(
            select(AuditEvent).where(AuditEvent.action == "document.interpretation", AuditEvent.entity_id == str(record.document_id)).order_by(AuditEvent.id.desc()).limit(1)
        )
        for reason in ((audit.event_data or {}).get("reasons") or []) if audit else []:
            counts[reason_key(reason)] += 1
    return dict(counts)
