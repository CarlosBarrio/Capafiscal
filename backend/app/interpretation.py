"""
Interpretación híbrida de facturas: reglas primero, Claude cuando hace falta
y las reglas deciden qué se acepta.

    documento → reglas (extractor) → ¿cuadra todo? ── sí → listo
                                         │ no
                                         ▼
                                  Claude propone campos
                                         ▼
                   las reglas verifican CADA valor antes de aceptarlo:
                   · aparece en el documento (no se inventa)
                   · NIF con dígito de control válido
                   · base + IVA − retención = total
                   · fecha válida y presente en el texto

Claude es una capacidad de interpretación dentro del sistema, no un agente
autónomo: nunca decide sin que una regla lo compruebe, y queda registrado
qué dijo cada motor y por qué se eligió cada valor.
"""
from __future__ import annotations

import re
import time
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from app.extractor import is_valid_spanish_tax_id
from app.extractor import normalize_search_text
from app.extractor import normalize_tax_id

AMOUNT_FIELDS = ("subtotal", "tax_total", "withholding_total", "total")
TOLERANCE = Decimal("0.03")
REQUIRED = ("supplier_name", "supplier_tax_id", "invoice_number", "invoice_date", "subtotal", "tax_total", "total")


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_search_text(text or ""))


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def spanish_amount(value: Decimal) -> list[str]:
    """Formas en que un importe puede aparecer escrito: 1.234,56 / 1234,56 / 1234.56."""
    plain = f"{value:.2f}"
    integer, cents = plain.split(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    return [f"{grouped},{cents}", f"{integer},{cents}", plain]


def in_text(value: Any, text: str, *, kind: str = "text") -> bool:
    """¿El valor aparece en el documento? (también leído al revés, por el texto vertical rotado)."""
    if value in (None, ""):
        return False
    haystack = compact(text)
    if kind == "amount":
        amount = to_decimal(value)
        return amount is not None and any(compact(form) in haystack for form in spanish_amount(amount))
    if kind == "date":
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            return False
        forms = [f"{parsed:%d/%m/%Y}", f"{parsed:%d-%m-%Y}", f"{parsed:%d.%m.%Y}", f"{parsed:%d/%m/%y}", parsed.isoformat()]
        months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
        forms.append(f"{parsed.day} de {months[parsed.month - 1]} de {parsed.year}")
        return any(compact(form) in haystack for form in forms)
    needle = compact(str(value))
    return bool(needle) and (needle in haystack or needle[::-1] in haystack)


def reconciles(values: dict[str, Any]) -> bool:
    subtotal, tax, total = (to_decimal(values.get(key)) for key in ("subtotal", "tax_total", "total"))
    withholding = to_decimal(values.get("withholding_total")) or Decimal("0")
    if subtotal is None or tax is None or total is None or total == 0:
        return False
    if total < 0:  # rectificativa o abono: todo en negativo
        if subtotal > 0 or tax > 0:
            return False
        subtotal, tax, total = -subtotal, -tax, -total
    return abs(subtotal + tax - abs(withholding) - total) <= TOLERANCE and tax < total


def rule_values(result: dict[str, Any]) -> dict[str, Any]:
    fields = result.get("fields") or {}
    return {name: (fields.get(name) or {}).get("value") for name in (*REQUIRED, "customer_name", "customer_tax_id", "due_date", "withholding_total")}


def needs_help(result: dict[str, Any], company_tax_ids: set[str]) -> list[str]:
    """Por qué las reglas no bastan (vacío si todo cuadra)."""
    if result.get("requires_ocr"):
        return ["documento escaneado sin texto"]
    values = rule_values(result)
    reasons = [f"falta {name}" for name in REQUIRED if values.get(name) in (None, "")]
    if values.get("subtotal") is not None and values.get("total") is not None and not reconciles(values):
        reasons.append("los importes no cuadran")
    supplier = normalize_tax_id(values.get("supplier_tax_id"))
    if supplier and supplier in company_tax_ids and normalize_tax_id(values.get("customer_tax_id")) not in company_tax_ids:
        reasons.append("el emisor detectado es la propia empresa")
    if (result.get("fields") or {}).get("category", {}).get("value") in {None, "Otros gastos"} and result.get("direction") != "ISSUED":
        reasons.append("categoría sin determinar")
    number = str(values.get("invoice_number") or "")
    if number and (" " in number.strip() or len(number) > 20 or re.search(r"\d{2}/\d{2}/\d{2,4}", number)):
        reasons.append("número de factura sospechoso")
    return reasons


def choose(field: str, rules_value: Any, claude_value: Any, text: str, decisions: list[dict[str, Any]], *, kind: str = "text", prefer_claude: bool = False) -> Any:
    claude_ok = claude_value not in (None, "") and in_text(claude_value, text, kind=kind)
    if kind == "tax_id" and claude_ok:
        claude_ok = is_valid_spanish_tax_id(normalize_tax_id(claude_value))
    rules_ok = rules_value not in (None, "")
    if claude_value not in (None, "") and not claude_ok:
        why = "Claude propuso un valor que no aparece en el documento o no es válido: descartado"
        chosen = rules_value
    elif not rules_ok and claude_ok:
        why, chosen = "las reglas no lo encontraron; Claude sí y está en el documento", claude_value
    elif claude_ok and prefer_claude and str(rules_value) != str(claude_value):
        why, chosen = "el valor de las reglas era sospechoso; el de Claude está en el documento", claude_value
    else:
        why, chosen = "las reglas ya lo tenían" if rules_ok else "ningún motor lo encontró", rules_value
    decisions.append({"field": field, "rules": rules_value, "claude": claude_value, "chosen": chosen, "why": why})
    return chosen


def merge(result: dict[str, Any], claude: dict[str, Any], text: str, company_tax_ids: set[str], reasons: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rules = rule_values(result)
    decisions: list[dict[str, Any]] = []
    merged: dict[str, Any] = {}

    self_issued = "el emisor detectado es la propia empresa" in reasons
    bad_number = "número de factura sospechoso" in reasons
    for field in ("supplier_tax_id", "customer_tax_id"):
        merged[field] = choose(field, normalize_tax_id(rules.get(field)), normalize_tax_id(claude.get(field)), text, decisions, kind="tax_id", prefer_claude=self_issued)
    for field in ("supplier_name", "customer_name"):
        merged[field] = choose(field, rules.get(field), claude.get(field), text, decisions, prefer_claude=self_issued)
    merged["invoice_number"] = choose("invoice_number", rules.get("invoice_number"), claude.get("invoice_number"), text, decisions, prefer_claude=bad_number)
    for field in ("invoice_date", "due_date"):
        merged[field] = choose(field, rules.get(field), claude.get(field) or None, text, decisions, kind="date")

    # Categoría: no se puede comprobar en el texto; se acepta la de Claude si es una categoría válida
    # y las reglas no la tenían clara.
    from app.agents.llm import expense_categories

    rules_category = (result.get("fields") or {}).get("category", {}).get("value")
    claude_category = claude.get("category")
    if claude_category in expense_categories() and rules_category in {None, "Otros gastos"}:
        merged["category"] = claude_category
        decisions.append({"field": "category", "rules": rules_category, "claude": claude_category, "chosen": claude_category, "why": "las reglas no la determinaron; Claude eligió una categoría válida"})
    else:
        merged["category"] = rules_category
        decisions.append({"field": "category", "rules": rules_category, "claude": claude_category, "chosen": rules_category, "why": "se mantiene la de las reglas"})

    # Importes: se elige el trío que cuadra (primero el de las reglas).
    rules_amounts = {key: rules.get(key) for key in AMOUNT_FIELDS}
    claude_amounts = {key: claude.get(key) or None for key in AMOUNT_FIELDS}
    claude_present = all(in_text(claude_amounts[key], text, kind="amount") for key in ("subtotal", "tax_total", "total") if claude_amounts[key])
    if reconciles(rules_amounts):
        chosen_amounts, why = rules_amounts, "los importes de las reglas cuadran"
    elif reconciles(claude_amounts) and claude_present:
        chosen_amounts, why = claude_amounts, "los importes de Claude cuadran (base + IVA − retención = total) y están en el documento"
    else:
        chosen_amounts, why = rules_amounts, "ningún motor da importes que cuadren: se dejan para revisión"
    for key in AMOUNT_FIELDS:
        merged[key] = chosen_amounts.get(key)
    decisions.append({"field": "importes", "rules": rules_amounts, "claude": claude_amounts, "chosen": chosen_amounts, "why": why})

    # El sentido lo deciden los NIF de la empresa, no la IA.
    supplier, customer = normalize_tax_id(merged.get("supplier_tax_id")), normalize_tax_id(merged.get("customer_tax_id"))
    if supplier in company_tax_ids and customer in company_tax_ids:
        direction = result.get("direction")
    elif customer in company_tax_ids:
        direction = "RECEIVED"
    elif supplier in company_tax_ids:
        direction = "ISSUED"
    else:
        direction = result.get("direction")
    merged["direction"] = direction
    return merged, decisions


def apply(result: dict[str, Any], merged: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {name: dict(value) for name, value in (result.get("fields") or {}).items()}
    changed = []
    for name, value in merged.items():
        if name == "direction":
            continue
        current = (fields.get(name) or {}).get("value")
        new_value = str(value) if name in AMOUNT_FIELDS and value is not None else value
        if current != new_value:
            changed.append(name)
            fields[name] = {"value": new_value, "confidence": 85 if new_value not in (None, "") else 0, "source": "claude+reglas", "evidence": next((item["why"] for item in decisions if item["field"] in {name, "importes"}), None)}
    updated = {**result, "fields": fields, "direction": merged.get("direction") or result.get("direction")}
    updated["interpretation"] = {"engine": "híbrido", "changed": changed, "decisions": decisions}
    return updated


def refine(
    file_path: Path,
    result: dict[str, Any],
    *,
    company_tax_ids: set[str] | list[str] | None = None,
    company_name: str | None = None,
    model: str | None = None,
    force: bool = False,
    extra_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Si las reglas no bastan y hay IA configurada, Claude interpreta y las reglas validan.

    ``extra_reasons``: motivos que vienen del aprendizaje (p. ej. campos que las
    personas corrigen a menudo en este proveedor).
    """
    from app.agents import llm

    company_ids = {normalize_tax_id(item) for item in (company_tax_ids or []) if item}
    reasons = needs_help(result, company_ids) + list(extra_reasons or [])
    info: dict[str, Any] = {"reasons": reasons, "engine": "reglas"}
    if not reasons and not force:
        return {**result, "interpretation": info}
    if not force:
        from app.routing import allowed_reasons

        if not allowed_reasons(reasons):
            # Los datos dicen que Claude no mejora este tipo de duda: no se gasta; va a una persona.
            return {**result, "interpretation": {**info, "fallback": "política de routing: Claude no mejora este tipo de duda", "skipped_by_policy": True}}
    if not llm.available():
        return {**result, "interpretation": {**info, "fallback": "sin ANTHROPIC_API_KEY"}}

    started = time.perf_counter()
    pdf_bytes = file_path.read_bytes() if file_path.suffix.lower() == ".pdf" else None
    text = result.get("raw_text") or ""
    claude, meta = llm.extract_invoice(pdf_bytes=pdf_bytes, text=None if pdf_bytes else text, company=company_name, model=model)
    if not claude:
        return {**result, "interpretation": {**info, "fallback": meta.get("fallback"), "meta": meta}}

    merged, decisions = merge(result, claude, text, company_ids, reasons)
    updated = apply(result, merged, decisions)
    updated["interpretation"].update(reasons=reasons, meta=meta, ms=int((time.perf_counter() - started) * 1000), claude=claude)
    return updated
