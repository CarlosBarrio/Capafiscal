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
    "invoice_date", "due_date", "subtotal", "tax_total", "total", "category",
)
FIELD_LABELS = {
    "direction": "Sentido (recibida/emitida)",
    "supplier_name": "Proveedor",
    "supplier_tax_id": "NIF proveedor",
    "customer_tax_id": "NIF cliente",
    "invoice_number": "Nº de factura",
    "invoice_date": "Fecha",
    "due_date": "Vencimiento",
    "subtotal": "Base",
    "tax_total": "IVA",
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


@dataclass
class Dataset:
    name: str
    folder: Path
    company: dict[str, Any]
    cases: list[Case]


def load_dataset(folder: Path) -> Dataset:
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    cases = [
        Case(item["id"], folder / item["file"], item["expected"], item.get("tags", []), item.get("set", "A").upper())
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
            checks = {name: same(name, case.expected.get(name), got.get(name)) for name in FIELDS if name in case.expected}
            rows.append({
                "id": case.id, "set": case.set, "tags": case.tags, "ms": ms, "error": error, "meta": meta,
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
