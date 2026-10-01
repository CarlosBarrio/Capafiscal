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
    "direction", "supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number",
    "invoice_date", "due_date", "subtotal", "tax_total", "total",
)
AMOUNTS = {"subtotal", "tax_total", "total", "withholding_total"}
LEGAL_WORDS = {"sl", "sa", "slp", "slu", "sau", "sll", "sc", "cb", "sociedad", "limitada", "anonima", "profesional"}


@dataclass
class Case:
    id: str
    path: Path
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    name: str
    folder: Path
    company: dict[str, Any]
    cases: list[Case]


def load_dataset(folder: Path) -> Dataset:
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    cases = [
        Case(item["id"], folder / item["file"], item["expected"], item.get("tags", []))
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
    values = {name: (fields.get(name) or {}).get("value") for name in FIELDS if name != "direction"}
    values["direction"] = result.get("direction")
    return values


def engine_rules(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.extractor import extract_invoice

    result = extract_invoice(case.path, company_tax_id=[company.get("tax_id")] if company.get("tax_id") else None, company_name=company.get("name"))
    return flatten(result), {"engine": "reglas"}


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
    meta = {"engine": "híbrido", **(info.get("meta") or {}), "reasons": info.get("reasons"), "fallback": info.get("fallback"), "claude_called": bool(info.get("meta"))}
    return flatten(refined), meta


ENGINES: dict[str, Callable[[Case, dict[str, Any], str | None], tuple[dict[str, Any], dict[str, Any]]]] = {
    "reglas": engine_rules,
    "claude": engine_claude,
    "hibrido": engine_hybrid,
}


# ---------------------------------------------------------------------
# Ejecución e informe
# ---------------------------------------------------------------------


def run(dataset: Dataset, engines: list[str], *, model: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"dataset": dataset.name, "cases": len(dataset.cases), "engines": {}}
    for engine in engines:
        rows = []
        for case in dataset.cases:
            started = time.perf_counter()
            try:
                got, meta = ENGINES[engine](case, dataset.company, model)
                error = None
            except Exception as exc:  # un caso roto no para la evaluación
                got, meta, error = {}, {"engine": engine}, f"{type(exc).__name__}: {exc}"
            ms = int((time.perf_counter() - started) * 1000)
            checks = {name: same(name, case.expected.get(name), got.get(name)) for name in FIELDS if name in case.expected}
            rows.append({
                "id": case.id, "tags": case.tags, "ms": ms, "error": error, "meta": meta,
                "checks": checks, "got": {name: got.get(name) for name in checks}, "expected": {name: case.expected[name] for name in checks},
                "perfect": all(checks.values()),
            })
        per_field = {}
        for name in FIELDS:
            values = [row["checks"][name] for row in rows if name in row["checks"]]
            if values:
                per_field[name] = {"ok": sum(values), "n": len(values), "rate": round(sum(values) / len(values), 3)}
        total_checks = sum(len(row["checks"]) for row in rows)
        report["engines"][engine] = {
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
    lines = [f"# Evaluación de lectura de facturas · dataset «{report['dataset']}» ({report['cases']} casos)", ""]
    lines.append("| Métrica | " + " | ".join(engines) + " |")
    lines.append("|---|" + "---|" * len(engines))

    def row(label: str, getter: Callable[[dict[str, Any]], Any]) -> None:
        lines.append(f"| {label} | " + " | ".join(str(getter(report["engines"][engine])) for engine in engines) + " |")

    row("Acierto por campo", lambda data: f"{data['field_accuracy']:.0%}" if data["field_accuracy"] is not None else "–")
    row("Facturas perfectas", lambda data: f"{data['perfect']}/{report['cases']}")
    row("Tiempo medio", lambda data: f"{data['ms_avg']} ms")
    row("Llamadas a Claude", lambda data: data["claude_calls"])
    row("Tokens (entrada/salida)", lambda data: f"{data['input_tokens']}/{data['output_tokens']}")
    row("Coste", lambda data: f"{data['cost_usd']:.4f} $")
    row("Vuelta a reglas (fallback)", lambda data: data["fallbacks"])
    lines += ["", "## Acierto por campo", "", "| Campo | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
    for name in FIELDS:
        cells = []
        for engine in engines:
            item = report["engines"][engine]["fields"].get(name)
            cells.append(f"{item['ok']}/{item['n']}" if item else "–")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Fallos por caso", ""]
    for engine in engines:
        for data in report["engines"][engine]["rows"]:
            wrong = [name for name, ok in data["checks"].items() if not ok]
            if data["error"]:
                lines.append(f"- **{engine} · {data['id']}**: error {data['error']}")
            elif wrong:
                detail = "; ".join(f"{name}: esperado `{data['expected'][name]}`, leído `{data['got'][name]}`" for name in wrong)
                lines.append(f"- **{engine} · {data['id']}**: {detail}")
            if data["meta"].get("fallback"):
                lines.append(f"  - vuelta a reglas: {data['meta']['fallback']}")
    return "\n".join(lines) + "\n"
