"""
Reglas vs Claude vs híbrido sobre el mismo banco de expedientes.

    python -m evaluation comparar --dataset b_sintetico            # necesita ANTHROPIC_API_KEY
    python -m evaluation comparar --dataset c_ciego --ciego        # C: una sola vez, al final

El sistema NO se toca. Los tres motores son el mismo código con distinta
configuración, puesta desde fuera:

    reglas   sin clave de IA.
    hibrido  con clave: el comportamiento del producto. Claude entra solo
             donde las reglas dudan (interpretation.needs_help) y en las
             notificaciones; las reglas validan cada valor de Claude.
    claude   con clave y Claude forzado en TODOS los documentos
             (refine(force=True)); las reglas siguen validando.

Responde a cuatro preguntas, más dos métricas de producto:

    A. ¿Qué corrige Claude?      comprobaciones mal con reglas y bien con el motor
    B. ¿Qué rompe Claude?        bien con reglas y mal con el motor (regresión por IA)
    C. ¿Cuándo merece la pena?   en qué entradas entró Claude y si mejoró, empató o empeoró
    D. ¿Cuánto cuesta?           coste total, por documento, por 1.000 y por comprobación corregida

    Autonomía:  autónomo (bien y sin persona) · asistido (a persona con el
                trabajo hecho y bien) · humano (a persona porque el sistema no
                pudo o dudó) · error silencioso (mal y sin avisar).
    Escalada:   precisión = escaladas que hacían falta / escaladas. Hace falta
                si la verdad dice que necesita persona o si algo estaba mal.

El coste incluye TODAS las llamadas a Claude (facturas, notificaciones y
escritos): se mide envolviendo app.agents.llm._complete desde el evaluador.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from collections import defaultdict
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any

from evaluation import casos

ENGINES = ("reglas", "claude", "hibrido")
ENGINE_LABELS = {"reglas": "REGLAS", "claude": "CLAUDE (siempre)", "hibrido": "HÍBRIDO (producto)"}
CALLS: list[dict[str, Any]] = []  # llamadas a Claude del caso en curso


def instrument() -> None:
    """Anota cada llamada a Claude (tokens, coste, tiempo, motivo de fallo) sin tocar el producto."""
    from app.agents import llm

    if getattr(llm._complete, "_instrumented", False):
        return
    original = llm._complete

    @wraps(original)
    def recorded(*args: Any, **kwargs: Any):
        text, meta = original(*args, **kwargs)
        system = kwargs.get("system") or ""
        purpose = "factura" if system == getattr(llm, "SYSTEM_INVOICE", None) else "notificación" if system == getattr(llm, "SYSTEM_EXTRACT", None) else "otro"
        CALLS.append({**meta, "purpose": purpose})
        return text, meta

    recorded._instrumented = True  # type: ignore[attr-defined]
    llm._complete = recorded


def configure(engine: str, key: str) -> None:
    """Mismo código, otra configuración: clave sí/no y Claude forzado o no."""
    import app.interpretation as interpretation
    from app.config import settings

    settings.anthropic_api_key = "" if engine == "reglas" else key
    original = getattr(interpretation, "_original_refine", None) or interpretation.refine
    interpretation._original_refine = original  # type: ignore[attr-defined]
    if engine == "claude":
        interpretation.refine = lambda *args, **kwargs: original(*args, **{**kwargs, "force": True})  # type: ignore[assignment]
    else:
        interpretation.refine = original  # type: ignore[assignment]


def run_engine(client, folder: Path, cases: list[str], engine: str, key: str) -> dict[str, Any]:
    configure(engine, key)
    results = []
    started = time.perf_counter()
    for case_id in cases:
        CALLS.clear()
        result = casos.run_case(client, folder / case_id)
        result["calls"] = list(CALLS)
        results.append(result)
    return {"engine": engine, "cases": results, "seconds": round(time.perf_counter() - started, 1), "summary": casos.summarize_run(results)}


def run(dataset: str, *, engines: list[str], blind: bool = False) -> dict[str, Any]:
    folder = Path(dataset) if Path(dataset).is_dir() else casos.DATASETS / dataset
    index = json.loads((folder / "indice.json").read_text(encoding="utf-8"))
    if index.get("conjunto") == "C" and not blind:
        raise SystemExit("El banco C es ciego: solo se ejecuta con --ciego, una vez y sin cambiar reglas después.")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    work = casos.prepare_environment("hibrido")  # deja la clave; cada motor la pone o la quita
    from fastapi.testclient import TestClient

    from app.main import app

    instrument()
    report: dict[str, Any] = {"dataset": dataset, "date": date.today().isoformat(), "engines": {}}
    try:
        with TestClient(app) as client:
            for engine in engines:
                if engine != "reglas" and not key:
                    report["engines"][engine] = {"engine": engine, "available": False, "reason": "sin ANTHROPIC_API_KEY"}
                    continue
                report["engines"][engine] = {**run_engine(client, folder, [item["case_id"] for item in index["casos"]], engine, key), "available": True}
    finally:
        configure("reglas", "")
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    report["analysis"] = analyse(report)
    return report


# ---------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------


def entries(engine_report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(case["case_id"], entry["entrada"]): entry for case in engine_report["cases"] for entry in case["entries"]}


def check_map(entry: dict[str, Any]) -> dict[str, bool]:
    """Cada comprobación, con los campos de factura por separado (para ver qué corrige o rompe)."""
    result = {}
    for item in entry["checks"]:
        if item["check"] == "invoice" and entry.get("invoice"):
            for name, field in entry["invoice"]["fields"].items():
                result[f"invoice.{name}"] = field["ok"]
        else:
            result[item["check"]] = item["ok"]
    return result


def sent_to_person(observed: dict[str, Any]) -> bool:
    """Va a una persona: expediente abierto, evento detenido, escaneado, o factura con dudas en revisión."""
    return bool(observed.get("requires_human") or observed.get("flags"))


def autonomy(entry: dict[str, Any]) -> str:
    observed = entry.get("observed") or {}
    ok = all(item["ok"] for item in entry["checks"])
    if entry["outcome"] == "error_sistema":
        return "humano"
    if "requires_human" not in observed:
        return "autónomo" if ok else "error_silencioso"
    human = sent_to_person(observed)
    if not human:
        return "autónomo" if ok else "error_silencioso"
    doubted = observed.get("event_status") in {"NEEDS_HUMAN", "FAILED"} or observed.get("requires_ocr") or observed.get("flags")
    return "asistido" if ok and not doubted else "humano"


def escalation(entry: dict[str, Any]) -> tuple[bool, bool] | None:
    """(¿se escaló?, ¿hacía falta?) o None si no aplica."""
    observed = entry.get("observed") or {}
    if "requires_human" not in observed:
        return None
    expected = next((item["expected"] for item in entry["checks"] if item["check"] == "requires_human"), None)
    wrong = not all(item["ok"] for item in entry["checks"] if item["check"] != "requires_human")
    return sent_to_person(observed), bool(expected) or wrong


def engine_metrics(engine_report: dict[str, Any]) -> dict[str, Any]:
    summary = engine_report["summary"]
    all_entries = [entry for case in engine_report["cases"] for entry in case["entries"] if entry.get("observed") is not None or entry["outcome"] == "error_sistema"]
    levels = Counter(autonomy(entry) for entry in all_entries)
    pairs = [pair for pair in (escalation(entry) for entry in all_entries) if pair]
    escalated = [needed for sent, needed in pairs if sent]
    needing = [sent for sent, needed in pairs if needed]
    false_positives = sum(
        1 for entry in all_entries
        for item in entry["checks"]
        if (item["check"] == "requires_human" and item["expected"] is False and not item["ok"]) or (item["check"] == "not_invoice" and not item["ok"])
    )
    calls = [call for case in engine_report["cases"] for call in case.get("calls", [])]
    documents = sum(1 for entry in all_entries if (entry.get("observed") or {}).get("type") in {"INVOICE", "NOTIFICATION", "OTHER"})
    cost = round(sum(call.get("cost_usd") or 0 for call in calls), 4)
    return {
        "cases_ok": summary["cases_ok"], "cases": summary["cases"], "checks_ok": summary["checks_ok"], "checks": summary["checks"],
        "silent": levels.get("error_silencioso", 0), "system_errors": summary["outcomes"].get("error_sistema", 0),
        "entries": len(all_entries), "autonomy": dict(levels),
        "human_rate": round(sum(1 for sent, _ in pairs if sent) / len(pairs), 3) if pairs else None,
        "escalated": len(escalated), "escalation_precision": round(sum(escalated) / len(escalated), 3) if escalated else None,
        "escalation_recall": round(sum(needing) / len(needing), 3) if needing else None,
        "false_positives": false_positives,
        "calls": len(calls), "calls_by_purpose": dict(Counter(call["purpose"] for call in calls)),
        "fallbacks": dict(Counter(call["fallback"] for call in calls if call.get("fallback"))),
        "served_by": dict(Counter(call.get("served_by") or call.get("model") for call in calls)),
        "input_tokens": sum(call.get("input_tokens") or 0 for call in calls), "output_tokens": sum(call.get("output_tokens") or 0 for call in calls),
        "cost_usd": cost, "documents": documents,
        "cost_per_document": round(cost / documents, 5) if documents else None,
        "seconds": engine_report["seconds"], "seconds_per_case": round(engine_report["seconds"] / max(1, summary["cases"]), 2),
    }


def versus(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """A (corrige), B (rompe) y C (cuándo merece la pena) de un motor frente a las reglas."""
    base_entries, other_entries = entries(base), entries(other)
    fixed, broken = [], []
    routed: Counter = Counter()
    routed_detail = []
    for key, entry in other_entries.items():
        before = base_entries.get(key)
        if before is None:
            continue
        old, new = check_map(before), check_map(entry)
        for name in sorted(set(old) & set(new)):
            if not old[name] and new[name]:
                fixed.append({"case": key[0], "entrada": key[1], "check": name})
            elif old[name] and not new[name]:
                broken.append({"case": key[0], "entrada": key[1], "check": name})
        ai = (entry.get("observed") or {}).get("ai")
        if ai and not ai.get("fallback"):
            delta = sum(new.values()) - sum(old[name] for name in new if name in old)
            verdict = "mejora" if delta > 0 else "empeora" if delta < 0 else "igual"
            routed[verdict] += 1
            routed_detail.append({"case": key[0], "entrada": key[1], "reasons": ai.get("reasons"), "changed": ai.get("changed"), "verdict": verdict})
    return {"fixed": fixed, "broken": broken, "routed": dict(routed), "routed_detail": routed_detail, "documents_with_ai": len(routed_detail)}


def analyse(report: dict[str, Any]) -> dict[str, Any]:
    engines = {name: data for name, data in report["engines"].items() if data.get("available")}
    analysis: dict[str, Any] = {"metrics": {name: engine_metrics(data) for name, data in engines.items()}, "versus": {}}
    if "reglas" in engines:
        for name, data in engines.items():
            if name == "reglas":
                continue
            comparison = versus(engines["reglas"], data)
            cost = analysis["metrics"][name]["cost_usd"]
            comparison["cost_per_fix"] = round(cost / len(comparison["fixed"]), 4) if comparison["fixed"] else None
            analysis["versus"][name] = comparison
    return analysis


# ---------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------


def pct(value: float | None) -> str:
    return f"{value * 100:.1f} %" if value is not None else "—"


def to_markdown(report: dict[str, Any]) -> str:
    analysis = report["analysis"]
    metrics = analysis["metrics"]
    names = [name for name in ENGINES if name in report["engines"]]

    def cell(name: str, render) -> str:
        data = report["engines"][name]
        if not data.get("available"):
            return "no ejecutado"
        return render(metrics[name])

    rows = [
        ("Expedientes perfectos", lambda m: f"{m['cases_ok']}/{m['cases']}"),
        ("Comprobaciones correctas", lambda m: f"{m['checks_ok']}/{m['checks']} ({pct(m['checks_ok'] / m['checks'])})"),
        ("**Errores silenciosos**", lambda m: f"**{m['silent']}**"),
        ("Errores del sistema", lambda m: str(m["system_errors"])),
        ("Autónomo (bien, sin persona)", lambda m: f"{m['autonomy'].get('autónomo', 0)}/{m['entries']} ({pct(m['autonomy'].get('autónomo', 0) / m['entries'])})"),
        ("Asistido (a persona, trabajo hecho)", lambda m: f"{m['autonomy'].get('asistido', 0)}/{m['entries']} ({pct(m['autonomy'].get('asistido', 0) / m['entries'])})"),
        ("Humano (no pudo o dudó)", lambda m: f"{m['autonomy'].get('humano', 0)}/{m['entries']} ({pct(m['autonomy'].get('humano', 0) / m['entries'])})"),
        ("Intervención humana", lambda m: pct(m["human_rate"])),
        ("Precisión de la escalada", lambda m: pct(m["escalation_precision"])),
        ("Cobertura de la escalada", lambda m: pct(m["escalation_recall"])),
        ("Falsos positivos", lambda m: str(m["false_positives"])),
        ("Llamadas a Claude", lambda m: f"{m['calls']} " + (f"({', '.join(f'{k} {v}' for k, v in m['calls_by_purpose'].items())})" if m["calls"] else "")),
        ("Coste total", lambda m: f"{m['cost_usd']:.4f} $"),
        ("Coste por documento / por 1.000", lambda m: f"{m['cost_per_document'] or 0:.5f} $ / {(m['cost_per_document'] or 0) * 1000:.2f} $"),
        ("Tiempo (total · por expediente)", lambda m: f"{m['seconds']} s · {m['seconds_per_case']} s"),
        ("Respaldos (fallback)", lambda m: ", ".join(f"{k} {v}" for k, v in m["fallbacks"].items()) or "ninguno"),
    ]
    lines = [
        f"# Reglas vs Claude vs híbrido · banco {report['dataset']} · {report['date']}",
        "",
        "Mismo código, misma verdad (`caso.json`); solo cambia la configuración. "
        "**Claude (siempre)** fuerza la interpretación en todos los documentos; **híbrido** es el producto: Claude solo donde las reglas dudan.",
        "",
        "| | " + " | ".join(ENGINE_LABELS[name] for name in names) + " |",
        "|---|" + "---:|" * len(names),
    ]
    for label, render in rows:
        lines.append(f"| {label} | " + " | ".join(cell(name, render) for name in names) + " |")
    lines += ["", "Los porcentajes son sobre las entradas de cada ejecución; si el número de entradas cambia, compara porcentajes, no números absolutos.", ""]

    for name, comparison in analysis.get("versus", {}).items():
        label = ENGINE_LABELS[name]
        lines += [f"## {label} frente a REGLAS", ""]
        lines.append(f"**A. Qué corrige** ({len(comparison['fixed'])} comprobaciones):")
        lines += [f"- {item['case']} · `{item['entrada']}` · {item['check']}" for item in comparison["fixed"]] or ["- nada"]
        lines.append("")
        lines.append(f"**B. Qué rompe — regresiones introducidas por la IA** ({len(comparison['broken'])}):")
        lines += [f"- ⛔ {item['case']} · `{item['entrada']}` · {item['check']}" for item in comparison["broken"]] or ["- nada"]
        lines.append("")
        routed = comparison["routed"]
        total = metrics[name]["documents"]
        lines.append(f"**C. Cuándo entra**: en {comparison['documents_with_ai']} de {total} documentos. "
                     f"Mejora {routed.get('mejora', 0)} · igual {routed.get('igual', 0)} · empeora {routed.get('empeora', 0)}.")
        reasons = Counter(reason for item in comparison["routed_detail"] for reason in (item["reasons"] or ["forzado"]))
        if reasons:
            lines.append("Por qué entró: " + " · ".join(f"{reason} ({count})" for reason, count in reasons.most_common()))
        lines.append("")
        cost = metrics[name]["cost_usd"]
        lines.append(f"**D. Cuánto cuesta**: {cost:.4f} $ en total · {(metrics[name]['cost_per_document'] or 0) * 1000:.2f} $ por 1.000 documentos · "
                     + (f"{comparison['cost_per_fix']:.4f} $ por comprobación corregida." if comparison["cost_per_fix"] is not None else "ninguna comprobación corregida."))
        lines.append("")
    if not analysis.get("versus"):
        lines += ["Claude no se ha ejecutado (falta `ANTHROPIC_API_KEY`): solo hay columna de reglas.", ""]
    lines += [
        "## Cómo leerlo",
        "",
        "- **Error silencioso**: mal y sin avisar. Es lo único que no puede subir.",
        "- **Precisión de la escalada**: de lo que se manda a una persona, cuánto hacía falta (necesita persona según la verdad, o había algo mal). Baja = ruido.",
        "- **Cobertura de la escalada**: de lo que necesitaba persona, cuánto se mandó. Baja = riesgo.",
        "- Una regresión de la IA (B) cuenta aunque el total suba: es un dato que estaba bien y Claude lo cambió.",
        "- Esta tabla no cambia reglas ni etiquetas. Si sale un problema sistemático, se corrige en una versión nueva (v0.5) y se vuelve a medir.",
    ]
    return "\n".join(lines)


def save(report: dict[str, Any]) -> Path:
    casos.REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = f"comparar_{report['dataset']}_{report['date']}"
    (casos.REPORTS / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    path = casos.REPORTS / f"{stamp}.md"
    path.write_text(to_markdown(report), encoding="utf-8")
    return path
