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
    "is_invoice", "document_type", "direction", "supplier_name", "supplier_tax_id", "customer_tax_id", "invoice_number",
    "invoice_date", "due_date", "subtotal", "tax_total", "withholding_total", "total", "category",
)
# Campos que un motor no produce: no se le cuentan como fallo. Hoy los tres motores producen todos.
NOT_PRODUCED: dict[str, set[str]] = {}
# Tipos de documento que distingue CapaFiscal (las reglas: título del documento; ver extractor.non_invoice_title).
# Claude devuelve exactamente los mismos (llm.DOCUMENT_TYPES); un test lo comprueba.
DOCUMENT_TYPES = ("factura", "albaran", "presupuesto", "proforma", "pedido", "nomina", "otro")
# Dimensiones de los metadatos por las que se desglosa el acierto.
META_DIMENSIONS = ("document_type", "difficulty", "ambiguous", "ocr", "multipage", "language", "visual_template", "synthetic")
FIELD_LABELS = {
    "is_invoice": "¿Es factura?",
    "document_type": "Tipo de documento",
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
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Dataset:
    name: str
    folder: Path
    company: dict[str, Any]
    cases: list[Case]


def load_dataset(folder: Path) -> Dataset:
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    # ¿Sintético o real? El caso manda; si no lo dice, el dataset; si tampoco, «reales/» es real y lo demás, desconocido.
    default_synthetic = labels.get("synthetic", False if "reales" in folder.parts else None)
    cases = [
        Case(item["id"], folder / item["file"], item["expected"], item.get("tags", []), item.get("set", "A").upper(), item.get("errores_conocidos", []),
             {"synthetic": default_synthetic, **item.get("meta", {})})
        for item in labels["casos"]
        if (folder / item["file"]).exists()
    ]
    return Dataset(folder.name, folder, labels.get("empresa", {}), cases)


# ---------------------------------------------------------------------
# Comparación campo a campo
# ---------------------------------------------------------------------


def name_tokens(value: str) -> set[str]:
    from app.extractor import normalize_search_text

    text = normalize_search_text(value or "")
    # «S.L.» o «S.L.P.» llegan como letras sueltas: se juntan en una sigla (sl, slp) antes de quitar la forma jurídica
    text = re.sub(r"\b([a-z])\W{0,2}(?=[a-z]\b)", r"\1", text)
    return {token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_WORDS}


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
    values["document_type"] = document_type(result)
    return values


def document_type(result: dict[str, Any]) -> str | None:
    """Tipo de documento según las reglas: el título de no-factura que detectaron, o «factura», o «otro»."""
    from app.extractor import non_invoice_kind

    kind = non_invoice_kind(result.get("signals") or [])
    if kind:
        return kind
    if result.get("is_invoice") is None:
        return None
    return "factura" if result.get("is_invoice") else "otro"


def engine_rules(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.extractor import extract_invoice

    from app.interpretation import needs_help

    ids = [company.get("tax_id")] if company.get("tax_id") else []
    result = extract_invoice(case.path, company_tax_id=ids or None, company_name=company.get("name"))
    # ¿El propio sistema se daría cuenta de que no está seguro? (lo mandaría a una persona)
    flags = needs_help(result, {item.upper() for item in ids})
    return flatten(result), {"engine": "reglas", "flags": flags, "confidence": result.get("overall_confidence")}


def engine_claude(case: Case, company: dict[str, Any], model: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.agents import llm
    from app.extractor import normalize_tax_id
    from app.extractor import read_document

    pdf = case.path.read_bytes() if case.path.suffix.lower() == ".pdf" else None
    text = None if pdf else read_document(case.path)[0]
    data, meta = llm.extract_invoice(pdf_bytes=pdf, text=text, company=company.get("name"), model=model)
    if not data:
        return {}, {"engine": "claude", **meta}
    values = {name: (data.get(name) or None) for name in FIELDS if name not in ("direction", "is_invoice")}
    values["is_invoice"] = data.get("is_invoice") if isinstance(data.get("is_invoice"), bool) else None  # False es una respuesta
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
    rules_flags = needs_help(result, {item.upper() for item in ids})
    # ¿Habría llamado a Claude? Hay motivos y la política de routing no lo descarta (se sabe también sin clave).
    requested = bool(info.get("reasons")) and not info.get("skipped_by_policy")
    meta = {"engine": "híbrido", **(info.get("meta") or {}), "reasons": info.get("reasons"), "fallback": info.get("fallback"), "claude_called": bool(info.get("meta")),
            "claude_requested": requested, "rules_got": flatten(result), "rules_flags": rules_flags, "flags": flags, "confidence": refined.get("overall_confidence"),
            # El tipo de documento del híbrido es el de las reglas; lo que opinó Claude se guarda para comparar.
            "claude_document_type": (info.get("claude") or {}).get("document_type")}
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
    "document_type": "error_tipo_documento",
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
        result[name] = {"docs": len(subset), "evaluable": sum(1 for row in subset if row["checks"]), "perfect": sum(1 for row in subset if row["checks"] and all(row["checks"].values())), "field_accuracy": round(sum(checks) / len(checks), 3) if checks else None, "fields": fields}
    return result


def evaluation_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Casos totales, evaluables (con alguna comprobación), no evaluables, aciertos, errores y % sobre evaluables."""
    evaluable = [row for row in rows if row["checks"]]
    correct = sum(1 for row in evaluable if all(row["checks"].values()))
    return {"evaluable": len(evaluable), "not_evaluated": len(rows) - len(evaluable), "correct": correct,
            "incorrect": len(evaluable) - correct, "accuracy": round(correct / len(evaluable), 3) if evaluable else None}


def by_meta(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Acierto por cada dimensión de los metadatos (tipo de documento, dificultad, ambiguo, OCR, multipágina…)."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension in META_DIMENSIONS:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = row["doc"].get(dimension)
            if value is None and dimension != "synthetic":
                continue
            groups.setdefault(str(value).lower() if isinstance(value, bool) or value is None else str(value), []).append(row)
        if groups:
            result[dimension] = {value: evaluation_counts(subset) | {"docs": len(subset)} for value, subset in sorted(groups.items())}
    return result


def classification(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """¿Es factura? como clasificación binaria (positivo = factura) y confusión del tipo de documento."""
    pairs = [(row["expected"]["is_invoice"], row["got"].get("is_invoice")) for row in rows if "is_invoice" in row["checks"]]
    if not pairs:
        return None
    # Sin respuesta (fallo de la llamada, sin clave…) no es «no es factura»: no regala verdaderos negativos.
    tp = sum(1 for want, got in pairs if want and got is True)
    fp = sum(1 for want, got in pairs if not want and got is True)
    fn = sum(1 for want, got in pairs if want and got is not True)
    tn = sum(1 for want, got in pairs if not want and got is False)
    no_answer = sum(1 for _want, got in pairs if not isinstance(got, bool))
    precision = round(tp / (tp + fp), 3) if tp + fp else None
    recall = round(tp / (tp + fn), 3) if tp + fn else None
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else None
    confusion: dict[str, int] = {}
    typed = [row for row in rows if "document_type" in row["checks"]]
    for row in typed:
        key = f"{row['expected']['document_type']} → {row['got'].get('document_type') or '(nada)'}"
        confusion[key] = confusion.get(key, 0) + 1
    type_ok = sum(1 for row in typed if row["checks"]["document_type"])
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "no_answer": no_answer, "precision": precision, "recall": recall, "f1": f1,
            "is_invoice_accuracy": round((tp + tn) / len(pairs), 3),
            "document_type_n": len(typed), "document_type_ok": type_ok,
            "document_type_accuracy": round(type_ok / len(typed), 3) if typed else None,
            "document_type_confusion": dict(sorted(confusion.items(), key=lambda item: (-item[1], item[0])))}


def attempted_call(meta: dict[str, Any]) -> bool:
    """¿Se llegó a llamar a la IA? (sí también si la llamada falló)."""
    if "outcome" in meta:
        return meta["outcome"] in {"ok", "fallback", "error"}
    return "ms" in meta or bool(meta.get("input_tokens"))


def precision_recall(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """(señalado, hacía falta) → precisión, recall y F1. None cuando el denominador es cero (no se inventa)."""
    tp = sum(1 for flagged, needed in pairs if flagged and needed)
    fp = sum(1 for flagged, needed in pairs if flagged and not needed)
    fn = sum(1 for flagged, needed in pairs if not flagged and needed)
    precision = round(tp / (tp + fp), 3) if tp + fp else None
    recall = round(tp / (tp + fn), 3) if tp + fn else None
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else None
    return {"docs": len(pairs), "flagged": tp + fp, "needed": tp + fn, "tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1}


def escalation_report(rows: list[dict[str, Any]], engine: str) -> dict[str, Any] | None:
    """Calidad de la escalada, solo con lo que se puede medir.

    - A una persona (reglas e híbrido): escalado = el sistema levanta avisos (needs_help); hacía falta = el
      documento tiene algún campo mal. Claude solo no da avisos ni confianza: no hay escalada que medir.
    - A Claude (híbrido): escalado = el híbrido pediría a Claude (hay motivos y el routing no lo descarta, se
      sepa o no la clave); hacía falta = las reglas, solas, tenían algún campo mal en ese documento.
    """
    if engine == "claude":
        return None
    evaluated = [row for row in rows if row["evaluated"]]
    if not evaluated:
        return None
    result = {"to_person": precision_recall([(bool(row["meta"].get("flags")), not row["perfect"]) for row in evaluated])}
    routed = [row for row in evaluated if "rules_perfect" in row]
    if engine == "hibrido" and routed:
        result["to_claude"] = precision_recall([(bool(row["meta"].get("claude_requested")), not row["rules_perfect"]) for row in routed])
        result["claude_document_type_disagreements"] = sum(
            1 for row in rows if "document_type" in row["got"] and row["meta"].get("claude_document_type") not in (None, row["got"]["document_type"]))
    return result


def hybrid_vs_rules(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Lo que importa del híbrido: de lo que las reglas fallaban, ¿cuánto arregla Claude? ¿Qué rompe?

    Documento a documento, frente a las reglas solas sobre el mismo documento (misma ejecución). Un error
    detectable (las reglas avisaban) que pasa a silencioso es peor que no llamar a Claude.
    """
    paired = [row for row in rows if row["evaluated"] and "rules_perfect" in row]
    if not paired:
        return None
    rules_wrong = [row for row in paired if not row["rules_perfect"]]
    rescued = [row for row in rules_wrong if row["perfect"]]
    broken = [row for row in paired if row["rules_perfect"] and not row["perfect"]]
    fields_fixed = sum(1 for row in paired for name, ok in row["checks"].items() if ok and not row["rules_checks"].get(name))
    fields_broken = sum(1 for row in paired for name, ok in row["checks"].items() if not ok and row["rules_checks"].get(name))
    rules_silent = [row for row in paired if not row["rules_perfect"] and not row["rules_flagged"]]
    return {
        "docs": len(paired),
        "rules_wrong": len(rules_wrong),
        "rescued": len(rescued),
        "rescued_ids": [row["id"] for row in rescued],
        "still_wrong": len(rules_wrong) - len(rescued),
        "new_errors": len(broken),
        "new_error_ids": [row["id"] for row in broken],
        "fields_fixed": fields_fixed,
        "fields_broken": fields_broken,
        "rules_silent_errors": len(rules_silent),
        "detectable_to_silent": sum(1 for row in paired if not row["rules_perfect"] and row["rules_flagged"] and row["outcome"] == "error_silencioso"),
        "silent_fixed": sum(1 for row in rules_silent if row["perfect"]),
        "claude_called_on": sum(1 for row in paired if row["meta"].get("claude_called")),
    }


def confidence_report(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """¿La confianza del motor separa aciertos de errores? Un error con confianza alta es el más peligroso."""
    scored = [row for row in rows if row["evaluated"] and isinstance(row["meta"].get("confidence"), (int, float))]
    if not scored:
        return None
    right = [row["meta"]["confidence"] for row in scored if row["perfect"]]
    wrong = [row["meta"]["confidence"] for row in scored if not row["perfect"]]
    return {"docs": len(scored), "avg_correct": round(sum(right) / len(right), 1) if right else None,
            "avg_incorrect": round(sum(wrong) / len(wrong), 1) if wrong else None,
            "incorrect_with_confidence_80_plus": sum(1 for value in wrong if value >= 80)}


def by_tag(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Fallos por característica del documento (maqueta, sector…): señala lo sistemático."""
    tags: dict[str, dict[str, int]] = {}
    for row in rows:
        for tag in row["tags"] or ["(sin etiqueta)"]:
            item = tags.setdefault(tag, {"docs": 0, "imperfect": 0})
            item["docs"] += 1
            item["imperfect"] += 1 if row["checks"] and not all(row["checks"].values()) else 0
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
    untouched_ok = sum(1 for row in untouched if row["checks"] and all(row["checks"].values()))
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
            # Sin ninguna comprobación aplicable (p. ej. Claude ante algo que no es factura) la fila NO es un acierto:
            # queda como no evaluada y fuera del denominador. all({}) sería True y la contaría como perfecta.
            evaluated = bool(checks)
            perfect = evaluated and all(checks.values())
            flagged = bool(meta.get("flags"))
            if engine == "claude" or not evaluated:
                outcome = None  # Claude solo no sabe decir cuándo duda; sin comprobaciones no hay resultado que juzgar
            elif flagged:
                outcome = "humano"
            elif not perfect:
                outcome = "error_silencioso"
            else:
                outcome = "con_ia" if meta.get("claude_called") else "solo_reglas"
            extra = {}
            if "rules_got" in meta:  # híbrido: ¿las reglas solas lo tenían bien? (para medir la escalada a Claude)
                rules_got = meta.pop("rules_got")
                extra["rules_checks"] = {name: same(name, case.expected.get(name), rules_got.get(name)) for name in checks}
                extra["rules_perfect"] = evaluated and all(extra["rules_checks"].values())
                extra["rules_flagged"] = bool(meta.pop("rules_flags", None))
            rows.append({
                **extra,
                "id": case.id, "set": case.set, "tags": case.tags, "ms": ms, "error": error, "meta": meta, "outcome": outcome,
                "known_errors": case.known_errors,
                "checks": checks, "got": {name: got.get(name) for name in checks}, "expected": {name: case.expected[name] for name in checks},
                "evaluated": evaluated, "perfect": perfect, "doc": case.meta,
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
            "by_meta": by_meta(rows),
            "classification": classification(rows),
            "confidence": confidence_report(rows),
            "escalation": escalation_report(rows, engine),
            "versus_rules": hybrid_vs_rules(rows) if engine == "hibrido" else None,
            "escalated_to_person": sum(1 for row in rows if row["meta"].get("flags")) if engine != "claude" else None,
            "escalated_to_claude": sum(1 for row in rows if row["meta"].get("claude_requested")) if engine == "hibrido" else None,
            "silent_errors": sum(1 for row in rows if row["outcome"] == "error_silencioso") if engine != "claude" else None,
            "fields": per_field,
            "field_accuracy": round(sum(sum(row["checks"].values()) for row in rows) / total_checks, 3) if total_checks else None,
            "perfect": sum(row["perfect"] for row in rows),
            **evaluation_counts(rows),
            "ms_avg": int(sum(row["ms"] for row in rows) / len(rows)) if rows else 0,
            "input_tokens": sum(int(row["meta"].get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row["meta"].get("output_tokens") or 0) for row in rows),
            "cost_usd": round(sum(float(row["meta"].get("cost_usd") or 0) for row in rows), 4),
            "fallbacks": sum(1 for row in rows if row["meta"].get("fallback")),
            # Llamadas intentadas (también las que fallaron). Con metadatos completos manda «outcome» (una llamada
            # omitida, sin clave, no cuenta); con metadatos antiguos o simulados, que haya tiempo o tokens anotados.
            "claude_calls": sum(1 for row in rows if row["meta"].get("claude_called") or (engine == "claude" and attempted_call(row["meta"]))),
            "rows": rows,
        }
    return report


DIMENSION_LABELS = {"document_type": "Tipo de documento", "difficulty": "Dificultad", "ambiguous": "Ambiguo", "ocr": "OCR (escaneado)",
                    "multipage": "Multipágina", "language": "Idioma", "visual_template": "Plantilla visual", "synthetic": "Sintético"}


def meta_markdown(report: dict[str, Any], engines: list[str]) -> list[str]:
    """Desglose por metadatos (motores lado a lado), clasificación y confianza."""
    lines: list[str] = []
    dims = [dim for dim in META_DIMENSIONS if any(dim in report["engines"][engine]["by_meta"] for engine in engines)]
    if dims and engines:
        lines += ["", "## Acierto por tipo y dificultad del documento", "",
                  "Documentos correctos / evaluables de cada grupo (los no evaluables no cuentan).", ""]
        lines += ["| Dimensión | Valor | " + " | ".join(engine.upper() for engine in engines) + " |", "|---|---|" + "---:|" * len(engines)]
        for dim in dims:
            values = sorted({value for engine in engines for value in report["engines"][engine]["by_meta"].get(dim, {})})
            for value in values:
                cells = []
                for engine in engines:
                    entry = report["engines"][engine]["by_meta"].get(dim, {}).get(value)
                    cells.append(f"{entry['correct']}/{entry['evaluable']}" if entry and entry["evaluable"] else "–")
                lines.append(f"| {DIMENSION_LABELS.get(dim, dim)} | {value} | " + " | ".join(cells) + " |")
    classified = [engine for engine in engines if report["engines"][engine]["classification"]]
    if classified:
        lines += ["", "## Tipo de documento y ¿es factura?", "",
                  "Misma verdad para los tres motores. Tipo: factura, presupuesto, albaran, proforma, pedido u otro. "
                  "¿Es factura? como clasificación binaria (positivo = factura).", "",
                  "| Métrica | " + " | ".join(engine.upper() for engine in classified) + " |", "|---|" + "---:|" * len(classified)]
        lines.append("| Acierto del tipo de documento | " + " | ".join(
            (f"{data['document_type_accuracy']:.0%} ({data['document_type_ok']}/{data['document_type_n']})" if data["document_type_accuracy"] is not None else "–")
            for data in (report["engines"][engine]["classification"] for engine in classified)) + " |")
        lines.append("| Acierto de ¿es factura? | " + " | ".join(f"{report['engines'][engine]['classification']['is_invoice_accuracy']:.0%}" for engine in classified) + " |")
        for key, label in (("tp", "Verdaderos positivos"), ("fp", "Falsos positivos (no factura leída como factura)"),
                           ("fn", "Falsos negativos (factura no reconocida)"), ("tn", "Verdaderos negativos"),
                           ("no_answer", "Sin respuesta (cuenta como error)"),
                           ("precision", "Precisión"), ("recall", "Recall"), ("f1", "F1")):
            lines.append(f"| {label} | " + " | ".join(str(report["engines"][engine]["classification"][key] if report["engines"][engine]["classification"][key] is not None else "–") for engine in classified) + " |")
        for engine in classified:
            confusion = {key: count for key, count in report["engines"][engine]["classification"]["document_type_confusion"].items()
                         if key.split(" → ")[0] != key.split(" → ")[1]}
            if confusion:
                lines += ["", f"Tipo de documento mal leído ({engine}): " + "; ".join(f"{key} ×{count}" for key, count in confusion.items())]
    not_classifying = [engine for engine in engines if not report["engines"][engine]["classification"]]
    if not_classifying:
        lines += ["", f"Sin clasificación: {', '.join(not_classifying)} no decide el tipo de documento (no se le cuenta ni a favor ni en contra)."]
    escalating = [engine for engine in engines if report["engines"][engine].get("escalation")]
    if escalating:
        lines += ["", "## Calidad de la escalada", "",
                  "A una persona: escalado = el sistema avisa de que duda; hacía falta = el documento tiene algún campo mal.",
                  "A Claude (híbrido): escalado = pediría a Claude; hacía falta = las reglas solas tenían algún campo mal.", "",
                  "| Escalada | Motor | Escalados | Hacía falta | Precisión | Recall | F1 |", "|---|---|---:|---:|---:|---:|---:|"]
        for engine in escalating:
            for key, label in (("to_person", "A una persona"), ("to_claude", "A Claude")):
                data = report["engines"][engine]["escalation"].get(key)
                if data:
                    lines.append(f"| {label} | {engine} | {data['flagged']} | {data['needed']} | " + " | ".join(
                        str(data[name]) if data[name] is not None else "–" for name in ("precision", "recall", "f1")) + " |")
        hybrid = report["engines"].get("hibrido", {}).get("escalation") or {}
        if hybrid.get("claude_document_type_disagreements"):
            lines.append(f"\nEn {hybrid['claude_document_type_disagreements']} documento(s) Claude leyó otro tipo de documento que las reglas; el híbrido se queda con el de las reglas.")
    if "claude" in engines:
        lines += ["", "Claude solo no da avisos ni confianza: para él no hay escalada, errores silenciosos ni confianza que medir (se indica, no se inventa)."]
    scored = [engine for engine in engines if report["engines"][engine]["confidence"]]
    if scored:
        lines += ["", "## Confianza", "", "| Métrica | " + " | ".join(engine.upper() for engine in scored) + " |", "|---|" + "---:|" * len(scored)]
        for key, label in (("avg_correct", "Confianza media en aciertos"), ("avg_incorrect", "Confianza media en errores"),
                           ("incorrect_with_confidence_80_plus", "Errores con confianza ≥ 80")):
            lines.append(f"| {label} | " + " | ".join(str(report["engines"][engine]["confidence"][key] if report["engines"][engine]["confidence"][key] is not None else "–") for engine in scored) + " |")
    return lines


def summary_markdown(report: dict[str, Any], engines: list[str]) -> list[str]:
    """La tabla para decidir: los tres motores en las mismas filas. N/A = no se puede medir para ese motor."""
    def value(engine: str, key: str) -> str:
        data = report["engines"][engine]
        if not data["available"]:
            return "no ejecutado"
        classification = data.get("classification") or {}
        values = {
            "docs": data["docs"],
            "field_accuracy": f"{data['field_accuracy']:.0%}" if data["field_accuracy"] is not None else "–",
            "perfect": f"{data['perfect']}/{data['evaluable']}",
            "document_type": f"{classification['document_type_accuracy']:.0%}" if classification.get("document_type_accuracy") is not None else "–",
            "f1": classification.get("f1") if classification.get("f1") is not None else "–",
            "silent": data["silent_errors"] if data.get("silent_errors") is not None else "N/A",
            "person": data["escalated_to_person"] if data.get("escalated_to_person") is not None else "N/A",
            "claude": data["escalated_to_claude"] if data.get("escalated_to_claude") is not None else ("N/A" if engine == "claude" else "–"),
            "calls": data["claude_calls"],
            "cost": f"{data['cost_usd']:.4f} $",
            "time": f"{data['ms_avg']} ms",
        }
        return str(values[key])

    rows = (("docs", "Documentos"), ("field_accuracy", "Acierto por campo"), ("perfect", "Documentos perfectos"),
            ("document_type", "Tipo de documento"), ("f1", "F1 ¿es factura?"), ("silent", "Errores silenciosos"),
            ("person", "Escalados a una persona"), ("claude", "Escalados a Claude"), ("calls", "Llamadas a Claude"),
            ("cost", "Coste"), ("time", "Tiempo medio por documento"))
    lines = ["## Resumen", "", "| Métrica | " + " | ".join(engine.upper() for engine in engines) + " |", "|---|" + "---:|" * len(engines)]
    lines += [f"| {label} | " + " | ".join(value(engine, key) for engine in engines) + " |" for key, label in rows]
    lines += ["", "N/A: Claude solo no avisa ni decide escalar, así que no tiene errores silenciosos ni escalada que medir.", ""]
    versus = (report["engines"].get("hibrido") or {}).get("versus_rules")
    if versus and report["engines"]["hibrido"]["available"]:
        lines += ["## Híbrido frente a reglas (documento a documento)", "",
                  "| Pregunta | Documentos |", "|---|---:|",
                  f"| Las reglas fallaban | {versus['rules_wrong']} de {versus['docs']} |",
                  f"| …y el híbrido lo resuelve | {versus['rescued']} |",
                  f"| …y sigue mal | {versus['still_wrong']} |",
                  f"| Errores nuevos (las reglas acertaban, el híbrido no) | {versus['new_errors']} |",
                  f"| Error detectable que pasa a silencioso | {versus['detectable_to_silent']} |",
                  f"| Errores silenciosos de las reglas que el híbrido arregla | {versus['silent_fixed']} de {versus['rules_silent_errors']} |",
                  f"| Campos arreglados / estropeados | {versus['fields_fixed']} / {versus['fields_broken']} |",
                  f"| Documentos en los que entró Claude | {versus['claude_called_on']} |", ""]
        if versus["new_error_ids"]:
            lines += ["Errores nuevos: " + ", ".join(versus["new_error_ids"]), ""]
    return lines


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
    lines += summary_markdown(report, engines)
    lines += ["## Acierto por campo", "", header, "|---|" + "---:|" * len(engines)]
    for name in FIELDS:
        cells = []
        for engine in engines:
            item = report["engines"][engine]["fields"].get(name)
            cells.append(cell(engine, f"{item['rate']:.0%} ({item['ok']}/{item['n']})" if item else "–"))
        if any(report["engines"][engine]["fields"].get(name) for engine in engines):
            lines.append(f"| {FIELD_LABELS[name]} | " + " | ".join(cells) + " |")
    lines.append("| **Todos los campos** | " + " | ".join(cell(engine, f"**{report['engines'][engine]['field_accuracy']:.0%}**" if report["engines"][engine]["field_accuracy"] is not None else "–") for engine in engines) + " |")
    lines.append("| **Documentos perfectos** | " + " | ".join(cell(engine, f"**{report['engines'][engine]['perfect']}/{report['engines'][engine]['evaluable']}**") for engine in engines) + " |")
    lines.append("| Casos totales | " + " | ".join(cell(engine, str(report["engines"][engine]["docs"])) for engine in engines) + " |")
    lines.append("| Casos evaluables | " + " | ".join(cell(engine, str(report["engines"][engine]["evaluable"])) for engine in engines) + " |")
    lines.append("| Casos no evaluables (sin ningún campo que comprobar) | " + " | ".join(cell(engine, str(report["engines"][engine]["not_evaluated"])) for engine in engines) + " |")
    lines.append("| Aciertos / errores | " + " | ".join(cell(engine, f"{report['engines'][engine]['correct']} / {report['engines'][engine]['incorrect']}") for engine in engines) + " |")
    lines.append("| % de acierto sobre evaluables | " + " | ".join(cell(engine, f"{report['engines'][engine]['accuracy']:.0%}" if report["engines"][engine]["accuracy"] is not None else "–") for engine in engines) + " |")

    lines += ["", "## Operación", "", header.replace("| Campo |", "| Métrica |", 1), "|---|" + "---:|" * len(engines)]

    def row(label: str, getter: Callable[[dict[str, Any]], Any]) -> None:
        lines.append(f"| {label} | " + " | ".join(cell(engine, str(getter(report["engines"][engine]))) for engine in engines) + " |")

    row("Tiempo por documento (media)", lambda data: f"{data['ms_avg']} ms")
    row("Tiempo por documento (p95)", lambda data: f"{data['ms_p95']} ms")
    row("Documentos en los que entra Claude", lambda data: f"{data['claude_share']:.0%}")
    row("Llamadas a Claude", lambda data: data["claude_calls"])
    row("Coste total", lambda data: f"{data['cost_usd']:.4f} $")
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
            lines.append("| **Documentos perfectos** | " + " | ".join(f"{sets_data[name]['perfect']}/{sets_data[name]['evaluable']}" for name in names) + " |")

    lines += meta_markdown(report, available)

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
    # También subcarpetas: reales/emitidas/ y reales/recibidas/ (el sentido sale de la carpeta).
    pending = [item for item in sorted(folder.rglob("*")) if item.suffix.lower() in {".pdf", ".txt"} and item.relative_to(folder).as_posix() not in known]
    drafts = []
    for path in pending:
        relative = path.relative_to(folder).as_posix()
        expected = {name: "" for name in FIELDS}
        direction = {"emitidas": "ISSUED", "recibidas": "RECEIVED"}.get(path.parent.name)
        if direction:  # lo dice la carpeta en la que lo dejaste, no las reglas
            expected.update(direction=direction, is_invoice=True, document_type="factura")
        if prefill:
            from app.extractor import extract_invoice

            tax_id = labels["empresa"].get("tax_id")
            got = flatten(extract_invoice(path, company_tax_id=[tax_id] if tax_id else None, company_name=labels["empresa"].get("name")))
            expected = {name: (got.get(name) or "") for name in FIELDS} | ({"direction": direction, "is_invoice": True, "document_type": "factura"} if direction else {})
        meta = {"synthetic": False, "document_type": "factura" if direction else "", "difficulty": "", "ambiguous": False,
                "multipage": None, "ocr": None, "language": "es", "visual_template": "real"}
        drafts.append({"id": path.stem, "file": relative, "set": set_name, "tags": [], "revisado": False, "meta": meta, "expected": expected})
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
