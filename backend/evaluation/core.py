"""
Banco de evaluación de la lectura de facturas: ¿qué aporta Claude frente a
las reglas?

Un *dataset* es una carpeta con documentos y un ``labels.json`` etiquetado a
mano (ver ``datasets/sinteticas/labels.json``). Cada caso se pasa por uno o
varios motores y se compara campo a campo con lo esperado:

    reglas   el extractor determinista de siempre
    claude   Claude solo (lee el PDF o el texto)
    hibrido  reglas → Claude si hace falta → las reglas validan cada valor

Se mide acierto por campo, facturas perfectas, tiempo, tokens, coste y cuántas
veces se volvió a las reglas (fallback).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Callable

FIELDS = (
    "is_invoice", "direction", "supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number",
    "invoice_date", "due_date", "subtotal", "tax_total", "withholding_total", "total", "category",
)
# Campos que un motor no produce: no se le cuentan como fallo (Claude no decide si algo es factura).
NOT_PRODUCED = {"claude": {"is_invoice"}}
FIELD_LABELS = {
    "is_invoice": "¿Es factura?",
    "direction": "Sentido (recibida/emitida)",
    "supplier_name": "Proveedor",
    "supplier_tax_id": "NIF proveedor",
    "customer_tax_id": "NIF cliente",
    "invoice_number": "Nº de factura",
    "invoice_date": "Fecha",
    "due_date": "Vencimiento",
    "subtotal": "Base",
    "tax_total": "IVA",
    "withholding_total": "Retención IRPF",
    "total": "Total",
    "category": "Clasificación",
}
SETS = {"A": "desarrollo", "B": "evaluación", "C": "ciego"}
AMOUNTS = {"subtotal", "tax_total", "total", "withholding_total"}
LEGAL_WORDS = {"sl", "sa", "slp", "slu", "sau", "sll", "sc", "cb", "sociedad", "limitada", "anonima", "profesional"}


@dataclass
class Case:
    id: str
    path: Path
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    set: str = "A"
    known_errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Dataset:
    name: str
    folder: Path
    company: dict[str, Any]
    cases: list[Case]


def load_dataset(folder: Path) -> Dataset:
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    cases = [
        Case(item["id"], folder / item["file"], item["expected"], item.get("tags", []), item.get("set", "A").upper(), item.get("errores_conocidos", []))
        for item in labels["casos"]
        if (folder / item["file"]).exists()
    ]
    return Dataset(folder.name, folder, labels.get("empresa", {}), cases)


# ---------------------------------------------------------------------
# Comparación campo a campo
# ---------------------------------------------------------------------


def name_tokens(value: str) -> set[str]:
    from app.extractor import normalize_search_text

    return {token for token in re.findall(r"[a-z0-9]+", normalize_search_text(value or "")) if token not in LEGAL_WORDS}


def same(name: str, expected: Any, got: Any) -> bool:
    from app.extractor import normalize_tax_id

    if expected in (None, ""):
        return got in (None, "")
    if got in (None, ""):
        return False
    if name in AMOUNTS:
        try:
            return abs(Decimal(str(expected)) - Decimal(str(got))) <= Decimal("0.01")
        except Exception:
            return False
    if name.endswith("tax_id"):
        return normalize_tax_id(str(expected)) == normalize_tax_id(str(got))
    if name.endswith("_name"):
        wanted, found = name_tokens(str(expected)), name_tokens(str(got))
        return bool(wanted) and len(wanted & found) / len(wanted) >= 0.75
    if name == "invoice_number":
        wanted = re.sub(r"[^A-Z0-9]", "", str(expected).upper())
        found = re.sub(r"[^A-Z0-9]", "", str(got).upper())
        # Se admite la serie delante (1-260190 ≈ 260190)
        return found == wanted or (found.endswith(wanted) and len(found) - len(wanted) <= 2)
    return str(expected).strip() == str(got).strip()


# ---------------------------------------------------------------------
# Motores
# ---------------------------------------------------------------------


def flatten(result: dict[str, Any]) -> dict[str, Any]:
    fields = result.get("fields") or {}
    values = {name: (fields.get(name) or {}).get("value") for name in FIELDS if name not in ("direction", "is_invoice")}
    values["direction"] = result.get("direction")
    values["is_invoice"] = result.get("is_invoice")
    return values


def engine_rules(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.extractor import extract_invoice

    from app.interpretation import needs_help

    ids = [company.get("tax_id")] if company.get("tax_id") else []
    result = extract_invoice(case.path, company_tax_id=ids or None, company_name=company.get("name"))
    # ¿El propio sistema se daría cuenta de que no está seguro? (lo mandaría a una persona)
    flags = needs_help(result, {item.upper() for item in ids})
    return flatten(result), {"engine": "reglas", "flags": flags}


def engine_claude(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.agents import llm
    from app.extractor import normalize_tax_id
    from app.extractor import read_document

    pdf = case.path.read_bytes() if case.path.suffix.lower() == ".pdf" else None
    text = None if pdf else read_document(case.path)[0]
    data, meta = llm.extract_invoice(pdf_bytes=pdf, text=text, company=company.get("name"), model=model)
    if not data:
        return {}, {"engine": "claude", **meta}
    values = {name: (data.get(name) or None) for name in FIELDS if name != "direction"}
    own = normalize_tax_id(company.get("tax_id"))
    values["direction"] = "RECEIVED" if normalize_tax_id(data.get("customer_tax_id")) == own else "ISSUED" if normalize_tax_id(data.get("supplier_tax_id")) == own else None
    return values, {"engine": "claude", **meta}


def engine_hybrid(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.extractor import extract_invoice
    from app.interpretation import refine

    ids = [company.get("tax_id")] if company.get("tax_id") else []
    result = extract_invoice(case.path, company_tax_id=ids or None, company_name=company.get("name"))
    refined = refine(case.path, result, company_tax_ids=ids, company_name=company.get("name"), model=model)
    info = refined.get("interpretation") or {}
    from app.interpretation import needs_help

    flags = needs_help(refined, {item.upper() for item in ids})
    meta = {"engine": "híbrido", **(info.get("meta") or {}), "reasons": info.get("reasons"), "fallback": info.get("fallback"), "claude_called": bool(info.get("meta")), "flags": flags}
    return flatten(refined), meta


ENGINES: dict[str, Callable[[Case, dict[str, Any], str | None], tuple[dict[str, Any], dict[str, Any]]]] = {
    "reglas": engine_rules,
    "claude": engine_claude,
    "hibrido": engine_hybrid,
}


# ---------------------------------------------------------------------
# Ejecución e informe
# ---------------------------------------------------------------------

OUTCOMES = {
    "solo_reglas": "Resueltos solos (reglas, sin IA)",
    "con_ia": "Resueltos con IA (Claude validado por reglas)",
    "humano": "Enviados a una persona (el sistema detecta que no está seguro)",
    "error_silencioso": "Errores silenciosos (no avisa y está mal)",
}
ERROR_TYPES = {
    "is_invoice": "error_tipo_documento",
    "withholding_total": "error_retencion",
    "direction": "error_sentido",
    "supplier_name": "error_proveedor",
    "supplier_tax_id": "error_nif",
    "customer_tax_id": "error_nif",
    "invoice_number": "error_numero",
    "invoice_date": "error_fecha",
    "due_date": "error_fecha",
    "subtotal": "error_importes",
    "total": "error_importes",
    "tax_total": "error_iva",
    "category": "error_clasificacion",
}


def error_matrix(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Documentos con cada tipo de error (un documento cuenta una vez por tipo)."""
    matrix: dict[str, int] = {}
    for row in rows:
        kinds = {ERROR_TYPES[name] for name, ok in row["checks"].items() if not ok}
        for kind in kinds:
            matrix[kind] = matrix.get(kind, 0) + 1
    return dict(sorted(matrix.items(), key=lambda item: -item[1]))


def category_confusion(rows: list[dict[str, Any]]) -> dict[str, int]:
    """«esperado → leído» de la clasificación: ¿es un caso aislado o sistemático?"""
    confusion: dict[str, int] = {}
    for row in rows:
        if "category" in row["checks"] and not row["checks"]["category"]:
            key = f"{row['expected']['category']} → {row['got'].get('category') or '(nada)'}"
            confusion[key] = confusion.get(key, 0) + 1
    return confusion


def by_set(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in sorted({row["set"] for row in rows}):
        subset = [row for row in rows if row["set"] == name]
        fields = {}
        for field_name in FIELDS:
            values = [row["checks"][field_name] for row in subset if field_name in row["checks"]]
            if values:
                fields[field_name] = round(sum(values) / len(values), 3)
        checks = [ok for row in subset for ok in row["checks"].values()]
        result[name] = {"docs": len(subset), "perfect": sum(1 for row in subset if all(row["checks"].values())), "field_accuracy": round(sum(checks) / len(checks), 3) if checks else None, "fields": fields}
    return result


def by_tag(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Fallos por característica del documento (maqueta, sector…): señala lo sistemático."""
    tags: dict[str, dict[str, int]] = {}
    for row in rows:
        for tag in row["tags"] or ["(sin etiqueta)"]:
            item = tags.setdefault(tag, {"docs": 0, "imperfect": 0})
            item["docs"] += 1
            item["imperfect"] += 0 if all(row["checks"].values()) else 1
    return tags


def routing_value(report: dict[str, Any]) -> dict[str, Any] | None:
    """¿Cuándo merece la pena llamar a Claude? Compara reglas e híbrido documento a documento."""
    engines = report["engines"]
    if "reglas" not in engines or "hibrido" not in engines:
        return None
    rules_rows = {row["id"]: row for row in engines["reglas"]["rows"]}
    called, fixed, broken, fixed_docs, cost = 0, 0, 0, 0, 0.0
    for row in engines["hibrido"]["rows"]:
        if not row["meta"].get("claude_called"):
            continue
        called += 1
        cost += float(row["meta"].get("cost_usd") or 0)
        before = rules_rows[row["id"]]["checks"]
        gained = sum(1 for name, ok in row["checks"].items() if ok and not before.get(name))
        lost = sum(1 for name, ok in row["checks"].items() if not ok and before.get(name))
        fixed += gained
        broken += lost
        fixed_docs += 1 if gained else 0
    untouched = [row for row in engines["hibrido"]["rows"] if not row["meta"].get("claude_called")]
    untouched_ok = sum(1 for row in untouched if all(row["checks"].values()))
    return {
        "docs": len(engines["hibrido"]["rows"]),
        "claude_called": called,
        "fields_fixed": fixed,
        "fields_broken": broken,
        "docs_improved": fixed_docs,
        "cost_usd": round(cost, 4),
        "cost_per_fixed_field_usd": round(cost / fixed, 4) if fixed else None,
        "untouched": len(untouched),
        "untouched_perfect": untouched_ok,
    }


def run(dataset: Dataset, engines: list[str], *, model: str | None = None, sets: set[str] | None = None) -> dict[str, Any]:
    cases = [case for case in dataset.cases if sets is None or case.set in sets]
    report: dict[str, Any] = {
        "dataset": dataset.name,
        "cases": len(cases),
        "sets": {name: sum(1 for case in cases if case.set == name) for name in SETS if any(case.set == name for case in cases)},
        "model": model,
        "engines": {},
    }
    for engine in engines:
        rows = []
        for case in cases:
            started = time.perf_counter()
            try:
                got, meta = ENGINES[engine](case, dataset.company, model)
                error = None
            except Exception as exc:  # un caso roto no para la evaluación
                got, meta, error = {}, {"engine": engine}, f"{type(exc).__name__}: {exc}"
            ms = int((time.perf_counter() - started) * 1000)
            skipped = NOT_PRODUCED.get(engine, set())
            checks = {name: same(name, case.expected.get(name), got.get(name)) for name in FIELDS if name in case.expected and name not in skipped}
            perfect = all(checks.values())
            flagged = bool(meta.get("flags"))
            if engine == "claude":
                outcome = None  # Claude solo no sabe decir cuándo duda
            elif flagged:
                outcome = "humano"
            elif not perfect:
                outcome = "error_silencioso"
            else:
                outcome = "con_ia" if meta.get("claude_called") else "solo_reglas"
            rows.append({
                "id": case.id, "set": case.set, "tags": case.tags, "ms": ms, "error": error, "meta": meta, "outcome": outcome,
                "known_errors": case.known_errors,
                "checks": checks, "got": {name: got.get(name) for name in checks}, "expected": {name: case.expected[name] for name in checks},
                "perfect": all(checks.values()),
            })
        per_field = {}
        for name in FIELDS:
            values = [row["checks"][name] for row in rows if name in row["checks"]]
            if values:
                per_field[name] = {"ok": sum(values), "n": len(values), "rate": round(sum(values) / len(values), 3)}
        total_checks = sum(len(row["checks"]) for row in rows)
        times = sorted(row["ms"] for row in rows)
        with_claude = [row for row in rows if row["meta"].get("claude_called") or (engine == "claude" and row["meta"].get("input_tokens"))]
        reasons: dict[str, int] = {}
        for row in rows:
            for reason in row["meta"].get("reasons") or []:
                reasons[reason] = reasons.get(reason, 0) + 1
        cost = sum(float(row["meta"].get("cost_usd") or 0) for row in rows)
        no_key = bool(rows) and all(row["meta"].get("fallback") == "sin ANTHROPIC_API_KEY" for row in rows)
        report["engines"][engine] = {
            "available": not (no_key and engine == "claude"),
            "claude_missing": no_key,
            "docs": len(rows),
            "ms_p95": times[min(len(times) - 1, int(len(times) * 0.95))] if times else 0,
            "cost_per_doc_usd": round(cost / len(rows), 5) if rows else 0,
            "cost_per_1000_docs_usd": round(cost / len(rows) * 1000, 2) if rows else 0,
            "claude_share": round(len(with_claude) / len(rows), 3) if rows else 0,
            "fallback_rate": round(sum(1 for row in rows if row["meta"].get("fallback")) / len(rows), 3) if rows else 0,
            "reasons": reasons,
            "outcomes": {key: sum(1 for row in rows if row["outcome"] == key) for key in OUTCOMES} if engine != "claude" else {},
            "error_matrix": error_matrix(rows),
            "confusion": category_confusion(rows),
            "by_set": by_set(rows),
            "by_tag": by_tag(rows),
            "fields": per_field,
            "field_accuracy": round(sum(sum(row["checks"].values()) for row in rows) / total_checks, 3) if total_checks else None,
            "perfect": sum(row["perfect"] for row in rows),
            "ms_avg": int(sum(row["ms"] for row in rows) / len(rows)) if rows else 0,
            "input_tokens": sum(int(row["meta"].get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row["meta"].get("output_tokens") or 0) for row in rows),
            "cost_usd": round(sum(float(row["meta"].get("cost_usd") or 0) for row in rows), 4),
            "fallbacks": sum(1 for row in rows if row["meta"].get("fallback")),
            "claude_calls": sum(1 for row in rows if row["meta"].get("claude_called") or (engine == "claude" and not row["meta"].get("fallback"))),
            "rows": rows,
        }
    return report


def to_markdown(report: dict[str, Any]) -> str:
    engines = list(report["engines"])
    sets = ", ".join(f"{name} ({SETS[name]}): {count}" for name, count in report.get("sets", {}).items())
    lines = [f"# Evaluación de lectura de facturas · «{report['dataset']}»", "", f"{report['cases']} documentos · conjuntos {sets or '–'}" + (f" · modelo {report['model']}" if report.get("model") else ""), ""]
    if "A" in report.get("sets", {}):
        lines += ["> Ojo: el conjunto A se usó para ajustar las reglas, así que sus cifras no miden generalización. Las que cuentan son las de B y C.", ""]

    if any(data.get("claude_missing") for data in report["engines"].values()):
        lines += ["> **Claude no se ha ejecutado** (falta `ANTHROPIC_API_KEY`): la columna CLAUDE queda vacía y el HÍBRIDO se comporta como las reglas.", ""]

    def cell(engine: str, text: str) -> str:
        return text if report["engines"][engine]["available"] else "no ejecutado"

    header = "| Campo | " + " | ".join(engine.upper() for engine in engines) + " |"
    lines += ["## Acierto por campo", "", header, "|---|" + "---:|" * len(engines)]
    for name in FIELDS:
        cells = []
        for engine in engines:
            item = report["engines"][engine]["fields"].get(name)
            cells.append(cell(engine, f"{item['rate']:.0%} ({item['ok']}/{item['n']})" if item else "–"))
        if any(report["engines"][engine]["fields"].get(name) for engine in engines):
            lines.append(f"| {FIELD_LABELS[name]} | " + " | ".join(cells) + " |")
    lines.append("| **Todos los campos** | " + " | ".join(cell(engine, f"**{report['engines'][engine]['field_accuracy']:.0%}**" if report["engines"][engine]["field_accuracy"] is not None else "–") for engine in engines) + " |")
    lines.append("| **Documentos perfectos** | " + " | ".join(cell(engine, f"**{report['engines'][engine]['perfect']}/{report['cases']}**") for engine in engines) + " |")

    lines += ["", "## Operación", "", header.replace("| Campo |", "| Métrica |", 1), "|---|" + "---:|" * len(engines)]

    def row(label: str, getter: Callable[[dict[str, Any]], Any]) -> None:
        lines.append(f"| {label} | " + " | ".join(cell(engine, str(getter(report["engines"][engine]))) for engine in engines) + " |")

    row("Tiempo por documento (media)", lambda data: f"{data['ms_avg']} ms")
    row("Tiempo por documento (p95)", lambda data: f"{data['ms_p95']} ms")
    row("Documentos en los que entra Claude", lambda data: f"{data['claude_share']:.0%}")
    row("Coste por documento", lambda data: f"{data['cost_per_doc_usd']:.4f} $")
    row("Coste por 1.000 documentos", lambda data: f"{data['cost_per_1000_docs_usd']:.2f} $")
    row("Tokens (entrada / salida)", lambda data: f"{data['input_tokens']} / {data['output_tokens']}")
    row("Tasa de fallback (vuelta a reglas)", lambda data: f"{data['fallback_rate']:.0%}")

    # ¿Cuánto resuelve solo y cuánto manda a una persona?
    deciders = [engine for engine in engines if report["engines"][engine]["outcomes"] and report["engines"][engine]["available"]]
    if deciders:
        lines += ["", "## Qué pasa con cada documento", "", "| Resultado | " + " | ".join(engine.upper() for engine in deciders) + " |", "|---|" + "---:|" * len(deciders)]
        for key, label in OUTCOMES.items():
            cells = []
            for engine in deciders:
                count = report["engines"][engine]["outcomes"].get(key, 0)
                total = report["engines"][engine]["docs"] or 1
                cells.append(f"{count} ({count / total:.0%})")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("Un error silencioso es lo más grave: el sistema no duda y el dato está mal. Lo que va a una persona no es un fallo; es el sistema sabiendo que no sabe.")

    available = [engine for engine in engines if report["engines"][engine]["available"]]
    error_kinds = sorted({kind for engine in available for kind in report["engines"][engine]["error_matrix"]})
    lines += ["", "## Matriz de errores (documentos afectados)", ""]
    if error_kinds:
        lines += ["| Tipo de error | " + " | ".join(engine.upper() for engine in available) + " |", "|---|" + "---:|" * len(available)]
        for kind in error_kinds:
            lines.append(f"| {kind} | " + " | ".join(str(report["engines"][engine]["error_matrix"].get(kind, 0)) for engine in available) + " |")
    else:
        lines.append("Sin errores.")
    for engine in available:
        confusion = report["engines"][engine]["confusion"]
        if confusion:
            lines += ["", f"Clasificación ({engine}), esperado → leído:"]
            lines += [f"- {key}: {count}" for key, count in sorted(confusion.items(), key=lambda item: -item[1])]

    for engine in available:
        sets_data = report["engines"][engine]["by_set"]
        if len(sets_data) > 1:
            names = list(sets_data)
            lines += ["", f"## Por conjunto ({engine})", "", "| Campo | " + " | ".join(f"{name} ({SETS.get(name, name)})" for name in names) + " |", "|---|" + "---:|" * len(names)]
            for field_name in FIELDS:
                if any(field_name in sets_data[name]["fields"] for name in names):
                    lines.append(f"| {FIELD_LABELS[field_name]} | " + " | ".join(f"{sets_data[name]['fields'][field_name]:.0%}" if field_name in sets_data[name]["fields"] else "–" for name in names) + " |")
            lines.append("| **Documentos perfectos** | " + " | ".join(f"{sets_data[name]['perfect']}/{sets_data[name]['docs']}" for name in names) + " |")

    weak_tags = {engine: {tag: data for tag, data in report["engines"][engine]["by_tag"].items() if data["imperfect"]} for engine in available}
    if any(weak_tags.values()):
        lines += ["", "## Fallos por característica del documento", ""]
        for engine, tags in weak_tags.items():
            for tag, data in sorted(tags.items(), key=lambda item: -item[1]["imperfect"]):
                lines.append(f"- {engine} · «{tag}»: {data['imperfect']} de {data['docs']} documento(s) con algún error")

    routing = routing_value(report)
    if routing and report["engines"]["hibrido"]["available"] and not report["engines"]["hibrido"]["claude_missing"]:
        lines += ["", "## ¿Cuándo merece la pena llamar a Claude?", ""]
        lines.append(
            f"El híbrido llamó a Claude en {routing['claude_called']} de {routing['docs']} documentos. "
            f"Corrigió {routing['fields_fixed']} campo(s) en {routing['docs_improved']} documento(s) y empeoró {routing['fields_broken']}. "
            f"Coste total {routing['cost_usd']:.4f} $" + (f", {routing['cost_per_fixed_field_usd']:.4f} $ por campo corregido." if routing["cost_per_fixed_field_usd"] else ".")
        )
        lines.append(f"En los {routing['untouched']} documentos donde no llamó a Claude, {routing['untouched_perfect']} eran perfectos solo con reglas (coste 0).")

    known = [(row["id"], item) for row in next(iter(report["engines"].values()))["rows"] for item in row.get("known_errors") or []]
    if known:
        lines += ["", "## Errores conocidos (se siguen, no se esconden)", ""]
        for case_id, item in known:
            status = []
            for engine in available:
                row = next(row for row in report["engines"][engine]["rows"] if row["id"] == case_id)
                ok = row["checks"].get(item["campo"])
                status.append(f"{engine}: {'corregido' if ok else 'sigue fallando'}")
            lines.append(f"- **{case_id}** · {FIELD_LABELS.get(item['campo'], item['campo'])}: {item.get('nota', '')} → " + "; ".join(status))

    hybrid = report["engines"].get("hibrido")
    if hybrid and hybrid.get("reasons"):
        lines += ["", "## Por qué entró Claude (híbrido)", ""]
        for reason, count in sorted(hybrid["reasons"].items(), key=lambda item: -item[1]):
            lines.append(f"- {reason}: {count} documento(s)")

    lines += ["", "## Fallos por documento", ""]
    any_failure = False
    for engine in engines:
        if not report["engines"][engine]["available"]:
            continue
        for data in report["engines"][engine]["rows"]:
            wrong = [name for name, ok in data["checks"].items() if not ok]
            if data["error"]:
                any_failure = True
                lines.append(f"- **{engine} · {data['id']}** ({data['set']}): error {data['error']}")
            elif wrong:
                any_failure = True
                detail = "; ".join(f"{FIELD_LABELS.get(name, name)}: esperado `{data['expected'][name]}`, leído `{data['got'][name]}`" for name in wrong)
                lines.append(f"- **{engine} · {data['id']}** ({data['set']}): {detail}")
            if data["meta"].get("fallback") and data["meta"]["fallback"] != "sin ANTHROPIC_API_KEY":
                lines.append(f"  - vuelta a reglas: {data['meta']['fallback']}")
    if not any_failure:
        lines.append("Ninguno.")
    return "\n".join(lines) + "\n"


def draft_labels(folder: Path, *, company: dict[str, Any] | None = None, prefill: bool = False, set_name: str = "B") -> Path:
    """Prepara etiquetas para documentos nuevos (los que aún no tienen etiqueta).

    Por defecto deja los valores vacíos: rellenarlos con lo que leen las reglas
    sesga la etiqueta hacia las reglas. Con prefill=True se rellenan, pero hay
    que revisar cada valor contra el documento.
    """
    labels_path = folder / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {"empresa": company or {"name": "", "tax_id": ""}, "casos": []}
    known = {item["file"] for item in labels["casos"]}
    pending = [item for item in sorted(folder.iterdir()) if item.suffix.lower() in {".pdf", ".txt"} and item.name not in known]
    drafts = []
    for path in pending:
        expected = {name: "" for name in FIELDS}
        if prefill:
            from app.extractor import extract_invoice

            tax_id = labels["empresa"].get("tax_id")
            got = flatten(extract_invoice(path, company_tax_id=[tax_id] if tax_id else None, company_name=labels["empresa"].get("name")))
            expected = {name: (got.get(name) or "") for name in FIELDS}
        drafts.append({"id": path.stem, "file": path.name, "set": set_name, "tags": [], "revisado": False, "expected": expected})
    out = folder / "labels.borrador.json"
    out.write_text(json.dumps({"empresa": labels["empresa"], "casos": drafts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def merge_reviewed(folder: Path) -> tuple[int, int]:
    """Pasa a labels.json los casos del borrador marcados como revisados."""
    labels_path, draft_path = folder / "labels.json", folder / "labels.borrador.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {"empresa": {}, "casos": []}
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    labels["empresa"] = labels.get("empresa") or draft.get("empresa", {})
    moved, left = 0, []
    for item in draft["casos"]:
        if item.get("revisado"):
            item = {key: value for key, value in item.items() if key != "revisado"}
            item["expected"] = {key: value for key, value in item["expected"].items() if value not in ("", None)}
            labels["casos"].append(item)
            moved += 1
        else:
            left.append(item)
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    draft_path.write_text(json.dumps({**draft, "casos": left}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return moved, len(left)
