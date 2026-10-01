"""
Reglas aprendidas de facturas reales (segunda pasada del extractor).

Cada regla resuelve un patrón que aparece en facturas de proveedores reales
y que el extractor genérico no cubría. Todas son generales (no dependen de un
proveedor concreto) y se validan entre sí:

  · texto vertical rotado (registro mercantil en el margen) → se reconstruye
  · identidad legal del emisor en el pie: «Inscrita en el Registro Mercantil
    … CIF», «X es el Responsable del tratamiento», «Responsable del Fichero: X»
  · totales en tabla (cabecera en una línea, valores debajo) y dígitos
    separados por espacios («8 3 7,69») → solver de importes que exige que
    base × tipo de IVA = cuota y base + cuota − retención = total
  · número de factura dentro de una fila de cabecera de tabla
  · fechas con el mes en letra («24 de Septiembre de 2026»)
  · páginas de continuación («2 de 2») que no son otra factura
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Any

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
VAT_RATES = (Decimal("21"), Decimal("10"), Decimal("4"), Decimal("5"), Decimal("2"), Decimal("7"), Decimal("0"))
WITHHOLDING_RATES = (Decimal("15"), Decimal("7"), Decimal("19"), Decimal("1"), Decimal("2"))
CITY_WORDS = {
    "burgos", "madrid", "barcelona", "valencia", "sevilla", "zaragoza", "bilbao", "valladolid", "leon", "palencia",
    "soria", "segovia", "avila", "salamanca", "zamora", "espana", "españa", "logrono", "santander", "vitoria",
}
AMOUNT_TOKEN = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})(?!\d)")
SPACED_DIGITS = re.compile(r"(?<![\d,.])((?:\d ){2,6}\d*),(\d{2})(?!\d)")
SPACE_THOUSANDS = re.compile(r"(?<![\d,.])(\d{1,3}) (\d{3}),(\d{2})(?!\d)")
LEGAL_FORM = r"(?:S\.?\s?L\.?\s?(?:P|U|L)?\.?|S\.?\s?A\.?\s?(?:U)?\.?|S\.?\s?C\.?|C\.?\s?B\.?|S\.?\s?COOP\.?)"


def normalize(text: str) -> str:
    from app.extractor import normalize_search_text

    return normalize_search_text(text or "")


# ---------------------------------------------------------------------
# Texto vertical rotado
# ---------------------------------------------------------------------


def _dedupe_columns(line: str) -> str:
    """«Inscrita Inscrita» (el mismo texto girado en los dos márgenes) → «Inscrita»."""
    tokens = line.split()
    half = len(tokens) // 2
    if len(tokens) % 2 == 0 and half and tokens[:half] == tokens[half:]:
        return " ".join(tokens[:half])
    return line


def restore_rotated_text(text: str) -> str:
    """Reconstruye el texto girado 90° (p. ej. el registro mercantil en el margen).

    pdfplumber lo devuelve como una columna de líneas muy cortas, de dos formas:
    trozos de letras al revés («icp», «ircs») o palabras sueltas en orden
    inverso («Mercantil», «Registro»). Se añaden al final las dos lecturas
    posibles, sin tocar el texto original, para que las demás reglas las vean.
    """
    lines = text.splitlines()
    restored: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 8:
            letters = sum(character.isalpha() for item in run for character in item)
            if letters >= 8:
                restored.append("".join(run)[::-1])  # trozos de letras al revés
                restored.append(" ".join(reversed(run)))  # palabras en orden inverso
        run.clear()

    for line in lines:
        stripped = _dedupe_columns(line.strip())
        tokens = stripped.split()
        if stripped and len(tokens) <= 2 and all(len(token) <= 14 for token in tokens) and not re.search(r"\d+,\d{2}", stripped):
            run.append(stripped)
        else:
            flush()
    flush()
    if not restored:
        return text
    return text + "\n" + "\n".join(f"[texto girado] {item}" for item in restored)


# ---------------------------------------------------------------------
# Identidad legal del emisor
# ---------------------------------------------------------------------


@dataclass
class LegalIdentity:
    name: str | None
    tax_id: str | None
    source: str


def clean_name(value: str) -> str | None:
    # Lo que va delante del nombre (IBAN, importes, etiquetas) no es parte de él.
    value = re.split(r"[\d€|:]", value)[-1]
    value = re.sub(r"\s+", " ", value).strip(" ,;:-")
    if value.endswith(".") and not re.search(r"\b[A-Za-z]\.$", value):
        value = value[:-1]  # el punto final es de la frase, salvo en «S.L.»
    value = re.sub(r"^(?:la empresa|la entidad|el titular)\s+", "", value, flags=re.IGNORECASE)
    if len(value) < 4 or len(value) > 90 or not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3}", value):
        return None
    return value


def legal_identities(text: str) -> list[LegalIdentity]:
    from app.extractor import TAX_ID_PATTERN
    from app.extractor import is_valid_spanish_tax_id
    from app.extractor import normalize_tax_id

    identities: list[LegalIdentity] = []
    flat = re.sub(r"\s+", " ", text)

    def tax_ids_in(fragment: str) -> list[str]:
        found = []
        for match in TAX_ID_PATTERN.finditer(fragment.upper()):
            candidate = repair_tax_id(normalize_tax_id(match.group(0)))
            if candidate and is_valid_spanish_tax_id(candidate):
                found.append(candidate)
        return found

    # 1) «X S.L. Inscrita en el Registro Mercantil de … CIF B…»
    for match in re.finditer(r"([A-ZÁÉÍÓÚÑ0-9][^.|\n]{2,80}?" + LEGAL_FORM + r")[,.]?\s+inscrit[ao] en el registro mercantil([^\n]{0,220})", flat, re.IGNORECASE):
        ids = tax_ids_in(match.group(2))
        identities.append(LegalIdentity(clean_name(match.group(1)), ids[0] if ids else None, "registro_mercantil"))
    # 1b) Registro mercantil sin nombre delante (p. ej. texto girado): solo el CIF
    for line in text.splitlines():
        letters = re.sub(r"[^a-z]", "", normalize(line))
        rotated_registry = line.startswith("[texto girado]") and re.search(r"inscripcion|tomo|folio|hoja", letters)
        if "registromercantil" in letters or rotated_registry:
            ids = tax_ids_in(re.sub(r"(?<=\d) (?=\d)", "", line))
            if ids and not any(item.tax_id == ids[0] for item in identities):
                identities.append(LegalIdentity(None, ids[0], "registro_mercantil"))

    # 2) Protección de datos: quién es el responsable
    patterns = (
        r"tratados por (.{3,90}?)\s*,?\s*como responsable",
        r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9&.,\- ]{2,70}?" + LEGAL_FORM + r")\s*es (?:el )?responsable del tratamiento",
        r"responsable del (?:fichero|tratamiento)\s*:\s*([^,.;]{4,70})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, flat, re.IGNORECASE):
            name = clean_name(match.group(1))
            if name:
                identities.append(LegalIdentity(name, None, "proteccion_de_datos"))
    return identities


def repair_tax_id(value: str | None) -> str | None:
    """«A28333334C» (la «C» es de la dirección que sigue: «C\\ Mayor») → A28333334 si así es válido."""
    from app.extractor import is_valid_spanish_tax_id

    if not value or is_valid_spanish_tax_id(value):
        return value
    if len(value) == 10 and is_valid_spanish_tax_id(value[:-1]):
        return value[:-1]
    return value


def valid_tax_ids(text: str) -> list[str]:
    """Todos los NIF válidos del texto, también los que se solapan con otro texto pegado."""
    from app.extractor import is_valid_spanish_tax_id

    pattern = re.compile(r"(?=(?<![A-Z0-9])([ABCDEFGHJNPQRSUVW][\s.\-]?\d{7}[0-9A-J]|\d{8}[\s.\-]?[A-Z]|[XYZ][\s.\-]?\d{7}[\s.\-]?[A-Z])(?![0-9]))")
    found: list[str] = []
    for match in pattern.finditer(text.upper()):
        value = re.sub(r"[^A-Z0-9]", "", match.group(1))
        if is_valid_spanish_tax_id(value) and value not in found:
            found.append(value)
    return found


def issuer_from_legal_footer(text: str, company_tax_ids: set[str], company_name: str | None) -> LegalIdentity | None:
    """El emisor según el pie legal, si no es la propia empresa."""
    own_name = normalize(company_name or "")
    identities = [
        item for item in legal_identities(text)
        if (item.tax_id is None or item.tax_id not in company_tax_ids)
        and not (item.name and own_name and name_overlap(item.name, own_name) >= 0.6)
    ]
    if not identities:
        return None
    tax_ids = {item.tax_id for item in identities if item.tax_id}
    names = [item.name for item in identities if item.name]
    if len(tax_ids) > 1:
        return None  # ambiguo: mejor no decidir
    source = "+".join(sorted({item.source for item in identities}))
    if not tax_ids and names:
        # Hay un emisor distinto de la empresa: si en el documento solo hay un NIF válido que no es el suyo, es el de ese emisor.
        others = [value for value in valid_tax_ids(text) if value not in company_tax_ids]
        if len(others) == 1 and company_tax_ids & set(valid_tax_ids(text)):
            return LegalIdentity(names[0], others[0], source + "+unico_nif_ajeno")
    return LegalIdentity(names[0] if names else None, next(iter(tax_ids), None), source)


def name_overlap(first: str, second: str) -> float:
    stop = {"sl", "slp", "sa", "sau", "slu", "s", "l", "p", "a", "u", "y", "de", "la", "el"}
    tokens_a = {token for token in re.findall(r"[a-z0-9]+", normalize(first)) if token not in stop}
    tokens_b = {token for token in re.findall(r"[a-z0-9]+", normalize(second)) if token not in stop}
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


def weak_name(value: str | None, company_name: str | None) -> bool:
    if not value:
        return True
    words = re.findall(r"[a-z0-9]+", normalize(value))
    if len(words) <= 1 or all(word in CITY_WORDS for word in words):
        return True
    return bool(company_name) and name_overlap(value, company_name) >= 0.6


# ---------------------------------------------------------------------
# Importes: solver que exige coherencia fiscal
# ---------------------------------------------------------------------


def amount_tokens(text: str) -> list[tuple[Decimal, int]]:
    """Importes con dos decimales y la línea en que aparecen (incluye lecturas alternativas)."""
    tokens: list[tuple[Decimal, int]] = []
    for index, line in enumerate(text.splitlines()):
        for match in AMOUNT_TOKEN.finditer(line):
            tokens.append((Decimal(match.group(1).replace(".", "") + "." + match.group(2)), index))
        for match in SPACED_DIGITS.finditer(line):  # «8 3 7,69» → 837,69
            tokens.append((Decimal(match.group(1).replace(" ", "") + "." + match.group(2)), index))
        for match in SPACE_THOUSANDS.finditer(line):  # «21 210,00» puede ser 21 + 210,00 o 21.210,00
            tokens.append((Decimal(match.group(1) + match.group(2) + "." + match.group(3)), index))
    return tokens


def solve_amounts(text: str) -> dict[str, Any] | None:
    """Busca base, cuota y total que cuadren con un tipo de IVA español.

    Se prefieren los totales que aparecen junto a la palabra «total» y los más
    altos (el total de la factura suele ser el mayor importe que cuadra).
    """
    tokens = amount_tokens(text)
    if len(tokens) < 3:
        return None
    lines = text.splitlines()
    values = sorted({value for value, _index in tokens if value > 0})
    lines_by_value: dict[Decimal, list[int]] = {}
    for value, index in tokens:
        lines_by_value.setdefault(value, []).append(index)
    present = set(values)
    tolerance = Decimal("0.02")

    def near(target: Decimal) -> Decimal | None:
        for value in (target, target + Decimal("0.01"), target - Decimal("0.01"), target + Decimal("0.02"), target - Decimal("0.02")):
            if value in present:
                return value
        return None

    candidates = []
    for base, tax in combinations(values, 2):
        if base < tax:
            base, tax = tax, base
        rate = next((rate for rate in VAT_RATES if rate and abs(base * rate / 100 - tax) <= Decimal("0.015")), None)
        if rate is None:
            continue
        for withholding_rate in (None, *WITHHOLDING_RATES):
            withholding = (base * withholding_rate / 100).quantize(Decimal("0.01")) if withholding_rate else Decimal("0")
            if withholding_rate and near(withholding) is None:
                continue
            total = near(base + tax - withholding)
            if total is None or total == base:
                continue
            score = float(total) / 10_000
            if any("total" in normalize(lines[index]) for index in lines_by_value.get(total, [])):
                score += 3
            if any(re.search(r"base|imponible|neto|subtotal", normalize(lines[index])) for index in lines_by_value.get(base, [])):
                score += 1
            if any(re.search(r"base|imponible|neto|subtotal", normalize(lines[max(0, index - 1)])) for index in lines_by_value.get(base, [])):
                score += 1
            if rate == Decimal("21"):
                score += 0.5
            if withholding_rate:
                score -= 0.5
            candidates.append((score, base, tax, withholding, total, rate))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[4]), reverse=True)
    score, base, tax, withholding, total, rate = candidates[0]
    return {"subtotal": base, "tax_total": tax, "withholding_total": withholding or None, "total": total, "tax_rate": rate, "score": round(score, 2)}


def negative_total(text: str) -> bool:
    """¿El total del documento es negativo? (rectificativa o abono: «TOTAL FACTURA -242,00 €»)."""
    from app.extractor import money_values

    lines = text.splitlines()
    label = re.compile(r"(?<!sub)total(?: factura| a pagar| a abonar| importe)?|importe a (?:pagar|abonar)", re.IGNORECASE)
    for index, line in enumerate(lines):
        for match in label.finditer(normalize(line)):
            tail = [line[match.end():], *lines[index + 1:index + 3]]
            for candidate in tail:
                values = money_values(candidate)
                if values:
                    return values[0][0] < 0
    return False


def plausible_amounts(subtotal: Any, tax: Any, total: Any, withholding: Any = None, surcharge: Any = None) -> bool:
    try:
        subtotal, tax, total = (Decimal(str(value)) if value not in (None, "") else None for value in (subtotal, tax, total))
        withholding = Decimal(str(withholding)) if withholding not in (None, "") else Decimal("0")
        surcharge = Decimal(str(surcharge)) if surcharge not in (None, "") else Decimal("0")
    except Exception:
        return False
    if subtotal is None or tax is None or total is None or total <= 0 or subtotal <= 0:
        return False
    if tax >= subtotal or abs(withholding) >= subtotal:
        return False
    if tax > subtotal * Decimal("0.215"):
        return False
    return abs(subtotal + tax + surcharge - abs(withholding) - total) <= Decimal("0.03")


# ---------------------------------------------------------------------
# Número de factura y fechas
# ---------------------------------------------------------------------

DATE_TOKEN = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
PAGE_TOKEN = re.compile(r"\b\d+\s+de\s+\d+\b", re.IGNORECASE)
NUMBER_LABEL = re.compile(r"\bN[º°o]\s*\.?\s*:\s*([A-Z0-9]+(?:[-/][A-Z0-9]+)+|\d{4,})\b", re.IGNORECASE)


def number_from_header_row(text: str) -> str | None:
    """«Número de Factura  Pág.  Fecha» y debajo «260508 1 31/08/2026»."""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines[:-1]):
        normalized = normalize(line)
        if not re.search(r"\bn(?:umero|º|o\.?)\b|n[º°]\s*factura|numero de factura", normalized):
            continue
        if not re.search(r"fecha|pag|documento|factura", normalized) or len(re.findall(r"\d{3,}", line)) > 0:
            continue
        # La fila de valores puede venir después de otra línea de la maqueta (p. ej. el NIF del cliente).
        for values in lines[index + 1:index + 4]:
            if re.search(r"\b[nc]\.?\s?i\.?\s?f\b", normalize(values)) and not DATE_TOKEN.search(values):
                continue
            values = DATE_TOKEN.sub(" ", values)
            values = PAGE_TOKEN.sub(" ", values)
            tokens = [token for token in re.split(r"\s+", values) if token]
            numbers = [token for token in tokens if re.search(r"\d", token) and len(re.sub(r"[^A-Z0-9]", "", token.upper())) >= 4 and not re.fullmatch(r"[A-Z]?\d{8}[A-Z]?", token.upper()) and not re.fullmatch(r"\d+,\d{2}", token)]
            if numbers:
                return numbers[0]
    return None


def labeled_number(text: str) -> str | None:
    match = NUMBER_LABEL.search(text)
    return match.group(1) if match else None


def suspicious_number(value: str | None) -> bool:
    if not value:
        return True
    if re.fullmatch(r"(?:19|20)\d{2}", value.strip()):
        return True  # un año no es un número de factura
    return " " in value.strip() or len(value) > 20 or bool(re.search(r"\d{2}/\d{2}/\d{2,4}", value)) or bool(re.search(r"\d{2}/\d{2}/\d{4}$", value))


def textual_dates(text: str) -> list[date]:
    found = []
    for match in re.finditer(r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de\s+|del\s+)?(\d{4})\b", normalize(text)):
        month = MONTHS.get(match.group(2))
        if month:
            try:
                found.append(date(int(match.group(3)), month, int(match.group(1))))
            except ValueError:
                continue
    return found


def due_date_from_header_row(text: str) -> date | None:
    """«Vencimientos Importe …» y en la línea siguiente «01/08/2026 266,20 …»."""
    from app.extractor import parse_date_value

    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines[:-1]):
        normalized = normalize(line)
        if re.search(r"\bvencimientos?\b", normalized) and not DATE_TOKEN.search(line):
            match = DATE_TOKEN.search(lines[index + 1])
            if match:
                return parse_date_value(match.group(0))
    return None


def is_continuation_page(page_text: str, page_number: int) -> bool:
    normalized = normalize(page_text[:1500])
    return bool(
        re.search(rf"\b{page_number}\s+de\s+\d+\b", normalized)
        or re.search(rf"\bpag(?:ina)?\.?\s*:?\s*{page_number}\b", normalized)
        or re.search(rf"\b{page_number}\s*/\s*\d+\b", normalized) and "pag" in normalized
    )
