from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pdfplumber

try:
    import pymupdf as fitz
except ImportError:
    fitz = None

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None


EXTRACTOR_NAME = "capafiscal.generic_invoice"
EXTRACTOR_VERSION = "2.4.0"

CENT = Decimal("0.01")
AMOUNT_TOLERANCE = Decimal("0.03")

MONEY_TOKEN_PATTERN = re.compile(
    r"""
    (?<![A-Za-z0-9])
    \(?
    [-+]?
    \.?
    \d{1,3}
    (?:
        [.\s,]\d{3}
    )*
    (?:
        [.,]\d{2,4}
    )?
    \)?
    \s*(?:€|EUR)?
    (?![A-Za-z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)

TAX_ID_PATTERN = re.compile(
    r"""
    (?<![A-Z0-9])
    (?:ES[\s\-]?)?
    (?:
        [ABCDEFGHJNPQRSUVW][\s.\-]*\d[\d\s.\-]{5,10}[0-9A-J]
        |
        \d{1,2}(?:[\s.\-]?\d{3}){2}[\s.\-]?[A-Z]
        |
        \d{8}[\s.\-]?[A-Z]
        |
        [XYZ][\s.\-]?\d{7}[\s.\-]?[A-Z]
    )
    (?![A-Z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)

DATE_PATTERN = re.compile(
    r"\b("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    r"\d{4}[/-]\d{1,2}[/-]\d{1,2}"
    r")\b"
)

INVOICE_NUMBER_PATTERNS = (
    re.compile(
        # "FRA-2026-001": FRA es parte del número, no una etiqueta.
        r"\b(?:factura|fra\.?)(?![-/_]?\d)\s*"
        r"(?:n[úu]mero|n[º°o.]|num\.?)?"
        r"\s*[:#\-]?\s*"
        r"([A-Z0-9][A-Z0-9 ./_-]{1,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:n[úu]mero|n[º°o.]|num\.?)\s*"
        r"(?:de\s+)?(?:factura|fra\.?)?"
        r"\s*[:#\-]?\s*"
        r"([A-Z0-9][A-Z0-9 ./_-]{1,30})",
        re.IGNORECASE,
    ),
)

SUPPLIER_LABELS = (
    "emisor",
    "proveedor",
    "seller",
    "supplier",
    "razon social",
    "datos del proveedor",
)

CUSTOMER_LABELS = (
    "cliente",
    "datos de cliente",
    "destinatario",
    "receptor",
    "facturar a",
    "customer",
    "bill to",
    "direccion fiscal",
    "direccion postal",
)

LEGAL_SUFFIXES = (
    "s.l.",
    "s.l",
    "sl",
    "s.l.u.",
    "slu",
    "s.a.",
    "s.a.u.",
    "sociedad limitada",
    "sociedad anonima",
    "arquitectos",
    "ingenieros",
    "consultores",
    "servicios",
)

NAME_REJECT_WORDS = (
    "registro mercantil",
    "proteccion de datos",
    "responsable del tratamiento",
    "factura",
    "fecha",
    "numero",
    "cif",
    "nif",
    "telefono",
    "tfno",
    "email",
    "e-mail",
    "www.",
    "http",
    "iban",
    "calle",
    "avenida",
    "avda",
    "domicilio",
    "direccion",
    "codigo cliente",
    "descripcion",
    "concepto",
    "importe",
    "base imponible",
    "subtotal",
    "cantidad",
    "precio",
    "forma de pago",
    "vencimiento",
    "pagina",
)


@dataclass
class ExtractedField:
    value: Any = None
    confidence: int = 0
    source: str | None = None
    evidence: str | None = None


@dataclass
class TaxLineResult:
    tax_type: str
    tax_rate: str | None
    tax_base: str | None
    tax_amount: str | None
    confidence: int
    source: str


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    value = value.replace("\x00", " ")
    value = value.replace("\u00a0", " ")
    value = value.replace("\r\n", "\n")
    value = value.replace("\r", "\n")

    cleaned_lines: list[str] = []

    for line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()

        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)

    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )

    return normalized.lower()


def get_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]


def normalize_tax_id(value: str | None) -> str | None:
    if not value:
        return None

    normalized = re.sub(
        r"[^A-Z0-9]",
        "",
        value.upper(),
    )

    if normalized.startswith("ES") and len(normalized) > 9:
        normalized = normalized[2:]

    return normalized or None


def is_valid_spanish_tax_id(value: str | None) -> bool:
    tax_id = normalize_tax_id(value)

    if not tax_id:
        return False

    if re.fullmatch(r"\d{8}[A-Z]", tax_id):
        letters = "TRWAGMYFPDXBNJZSQVHLCKE"
        return letters[int(tax_id[:8]) % 23] == tax_id[-1]

    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", tax_id):
        prefix = {
            "X": "0",
            "Y": "1",
            "Z": "2",
        }[tax_id[0]]

        number = int(prefix + tax_id[1:8])
        letters = "TRWAGMYFPDXBNJZSQVHLCKE"
        return letters[number % 23] == tax_id[-1]

    if re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", tax_id):
        digits = [int(character) for character in tax_id[1:8]]

        even_sum = digits[1] + digits[3] + digits[5]
        odd_sum = 0

        for digit in (digits[0], digits[2], digits[4], digits[6]):
            doubled = digit * 2
            odd_sum += doubled // 10 + doubled % 10

        control_number = (10 - ((even_sum + odd_sum) % 10)) % 10
        control_letter = "JABCDEFGHI"[control_number]
        expected_control = tax_id[-1]

        if tax_id[0] in "PQRSNW":
            return expected_control == control_letter

        if tax_id[0] in "ABEH":
            return expected_control == str(control_number)

        return expected_control in {
            str(control_number),
            control_letter,
        }

    return False


def normalize_amount(
    raw_value: str | int | float | Decimal | None,
) -> Decimal | None:
    if raw_value is None:
        return None

    if isinstance(raw_value, Decimal):
        return raw_value.quantize(CENT, rounding=ROUND_HALF_UP)

    if isinstance(raw_value, (int, float)):
        return Decimal(str(raw_value)).quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )

    cleaned = str(raw_value).strip()

    if not cleaned:
        return None

    negative = False

    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]

    if cleaned.startswith("-"):
        negative = True

    cleaned = re.sub(
        r"(?i)(EUR|EUROS?)",
        "",
        cleaned,
    )
    cleaned = cleaned.replace("€", "")
    cleaned = cleaned.replace("\u00a0", "")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.strip("+-()")

    # Algunos PDF producen importes como .1.177,77
    cleaned = re.sub(r"^\.(?=\d)", "", cleaned)

    if not cleaned:
        return None

    separator_positions = [
        index
        for index, character in enumerate(cleaned)
        if character in ".,"
    ]

    decimal_position: int | None = None

    if separator_positions:
        last_position = separator_positions[-1]
        final_digits = cleaned[last_position + 1:]

        # En facturas, dos decimales es el caso prioritario.
        if final_digits.isdigit() and len(final_digits) == 2:
            decimal_position = last_position
        elif (
            len(separator_positions) == 1
            and final_digits.isdigit()
            and len(final_digits) in {1, 3, 4}
        ):
            decimal_position = last_position

    if decimal_position is not None:
        integer_part = re.sub(
            r"[^\d]",
            "",
            cleaned[:decimal_position],
        )
        decimal_part = re.sub(
            r"[^\d]",
            "",
            cleaned[decimal_position + 1:],
        )

        if not integer_part:
            integer_part = "0"

        normalized = f"{integer_part}.{decimal_part}"
    else:
        normalized = re.sub(r"[^\d]", "", cleaned)

    if not normalized:
        return None

    try:
        amount = Decimal(normalized)

        if negative:
            amount = -amount

        return amount.quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        return None


def decimal_to_string(value: Decimal | None) -> str | None:
    if value is None:
        return None

    return format(value, ".2f")


def parse_date_value(raw_value: str | None) -> date | None:
    if not raw_value:
        return None

    value = raw_value.strip()

    formats = (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y/%m/%d",
        "%Y-%m-%d",
    )

    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format).date()

            if 1990 <= parsed.year <= datetime.now().year + 5:
                return parsed
        except ValueError:
            continue

    return None


def date_to_string(value: date | None) -> str | None:
    return value.isoformat() if value else None


def extract_page_with_pdfplumber(page: Any) -> str:
    try:
        return page.extract_text(
            x_tolerance=2,
            y_tolerance=3,
            layout=False,
        ) or ""
    except Exception:
        return ""

def extract_page_with_ocr(
    page: Any,
) -> str:
    """
    Ejecuta OCR sobre una página de PyMuPDF.

    Solo se utiliza cuando la página no contiene suficiente texto
    seleccionable.
    """
    if pytesseract is None or Image is None:
        return ""

    try:
        matrix = fitz.Matrix(2.5, 2.5)
        pixmap = page.get_pixmap(
            matrix=matrix,
            alpha=False,
        )

        image = Image.open(
            io.BytesIO(
                pixmap.tobytes("png")
            )
        )

        try:
            text = pytesseract.image_to_string(
                image,
                lang="spa+eng",
                config="--psm 6",
            )
        except pytesseract.TesseractError:
            text = pytesseract.image_to_string(
                image,
                config="--psm 6",
            )

        return clean_text(text)

    except Exception:
        return ""

def read_document(
    path: Path,
) -> tuple[str, int | None, bool, list[str]]:
    suffix = path.suffix.lower()

    if suffix == ".txt":
        text = clean_text(
            path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        )
        return text, 1, not bool(text), [text]

    if suffix != ".pdf":
        return "", None, False, []

    page_texts: list[str] = []
    page_count: int | None = None

    # Primera opción: PyMuPDF.
    if fitz is not None:
        try:
            with fitz.open(path) as document:
                page_count = len(document)

                for page in document:
                    page_text = clean_text(
                        page.get_text(
                            "text",
                            sort=True,
                        )
                    )

                    # Si la página no tiene texto suficiente,
                    # intenta OCR.
                    if len(page_text.strip()) < 20:
                        ocr_text = extract_page_with_ocr(page)

                        if len(ocr_text) > len(page_text):
                            page_text = ocr_text

                    page_texts.append(page_text)

        except Exception:
            page_texts = []

    # Segunda opción: pdfplumber.
    try:
        with pdfplumber.open(path) as pdf:
            if page_count is None:
                page_count = len(pdf.pages)

            for index, page in enumerate(pdf.pages):
                plumber_text = clean_text(
                    extract_page_with_pdfplumber(page)
                )

                if index >= len(page_texts):
                    page_texts.append(plumber_text)
                    continue

                # Conserva el resultado que tenga más información.
                if len(plumber_text) > len(page_texts[index]) * 1.10:
                    page_texts[index] = plumber_text

    except Exception:
        pass

    if page_count is None:
        page_count = len(page_texts)

    while len(page_texts) < page_count:
        page_texts.append("")

    non_empty_pages = [
        page_text
        for page_text in page_texts
        if len(page_text.strip()) >= 20
    ]

    complete_text = clean_text(
        "\n".join(page_texts)
    )

    requires_ocr = not bool(non_empty_pages)

    return (
        complete_text,
        page_count,
        requires_ocr,
        page_texts,
    )


def money_values(line: str) -> list[tuple[Decimal, str]]:
    values: list[tuple[Decimal, str]] = []

    # Evita interpretar porcentajes como importes.
    sanitized = re.sub(
        r"\b\d{1,2}(?:[,.]\d+)?\s*%",
        " ",
        line,
    )

    for match in MONEY_TOKEN_PATTERN.finditer(sanitized):
        raw_value = match.group(0).strip()
        amount = normalize_amount(raw_value)

        if amount is None:
            continue

        values.append((amount, raw_value))

    return values


def normalized_company_tax_ids(
    company_tax_id: Any,
) -> set[str]:
    if not company_tax_id:
        return set()

    if isinstance(company_tax_id, str):
        raw_values = re.split(r"[,;|]", company_tax_id)
    elif isinstance(company_tax_id, (list, tuple, set)):
        raw_values = list(company_tax_id)
    else:
        raw_values = [str(company_tax_id)]

    result: set[str] = set()

    for raw_value in raw_values:
        normalized = normalize_tax_id(str(raw_value))
        if normalized:
            result.add(normalized)

    return result


def raw_invoice_numbers(text: str) -> list[str]:
    numbers: list[str] = []

    for pattern in INVOICE_NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            candidate = clean_invoice_number(match.group(1))

            if candidate and candidate not in numbers:
                numbers.append(candidate)

    return numbers


def clean_invoice_number(value: str | None) -> str | None:
    if not value:
        return None

    candidate = re.sub(r"\s+", " ", value)
    candidate = candidate.strip(" .,:;#-")

    # El patrón puede capturar texto posterior.
    candidate = re.split(
        r"\b(?:fecha|cliente|cif|nif|pagina|página)\b",
        candidate,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .,:;-")

    # Descarta palabras finales sin dígitos ("2026-001 MADRID").
    tokens = candidate.split(" ")
    last_digit_token = max(
        (
            index
            for index, token in enumerate(tokens)
            if re.search(r"\d", token)
        ),
        default=-1,
    )

    if last_digit_token >= 0:
        candidate = " ".join(tokens[:last_digit_token + 1])

    if len(candidate) < 3 or len(candidate) > 30:
        return None

    if not re.search(r"\d", candidate):
        return None

    if re.fullmatch(r"\d{1,2}", candidate):
        return None

    if DATE_PATTERN.fullmatch(candidate):
        return None

    return candidate.upper()


def select_primary_pages(page_texts: list[str]) -> tuple[str, list[int]]:
    if len(page_texts) <= 1:
        return clean_text("\n".join(page_texts)), [1]

    selected: list[str] = []
    selected_indexes: list[int] = []

    first_page = page_texts[0]
    first_numbers = raw_invoice_numbers(first_page)
    primary_number = first_numbers[0] if first_numbers else None

    first_has_summary = (
        bool(find_labeled_amount(first_page, "total", allow_inference=False).value)
        and (
            "base imponible" in normalize_search_text(first_page)
            or "subtotal" in normalize_search_text(first_page)
        )
    )

    selected.append(first_page)
    selected_indexes.append(1)

    for page_index, page_text in enumerate(page_texts[1:], start=2):
        normalized_page = normalize_search_text(page_text)
        page_numbers = raw_invoice_numbers(page_text)

        repeats_primary = (
            primary_number is not None
            and any(
                number.replace(" ", "") == primary_number.replace(" ", "")
                for number in page_numbers
            )
        )

        looks_like_new_invoice = (
            bool(page_numbers)
            and primary_number is not None
            and not repeats_primary
            and "factura" in normalized_page
            and (
                "base" in normalized_page
                or "iva" in normalized_page
                or "total" in normalized_page
            )
        )

        # Si la primera factura ya está completa y la siguiente página
        # empieza otra factura, se trata como anexo. Una página «2 de 2»
        # es la continuación de la misma factura, no otra.
        from app.extraction_rules import is_continuation_page

        if first_has_summary and looks_like_new_invoice and not is_continuation_page(page_text, page_index):
            break

        selected.append(page_text)
        selected_indexes.append(page_index)

    return clean_text("\n".join(selected)), selected_indexes


def label_role_in_text(text: str) -> str | None:
    """
    Devuelve el rol ("customer" o "supplier") de la etiqueta más
    cercana al final del texto, o None si no hay etiqueta.
    """
    normalized = normalize_search_text(text)
    best_role: str | None = None
    best_position = -1

    for role, labels in (
        ("customer", CUSTOMER_LABELS),
        ("supplier", SUPPLIER_LABELS),
    ):
        for label in labels:
            for match in re.finditer(
                rf"\b{re.escape(label)}\b",
                normalized,
            ):
                if match.start() > best_position:
                    best_position = match.start()
                    best_role = role

    return best_role


def tax_id_label_role(
    lines: list[str],
    line_index: int,
    text_before_match: str,
) -> str | None:
    """
    Decide a quién pertenece un NIF/CIF mirando solo el texto previo
    de su propia línea y las líneas anteriores. Las líneas posteriores
    no se usan: suelen pertenecer al bloque de la otra parte.
    """
    same_line_role = label_role_in_text(text_before_match)

    if same_line_role:
        return same_line_role

    for offset in range(1, 4):
        previous_index = line_index - offset

        if previous_index < 0:
            break

        previous_line = lines[previous_index]

        # Una línea anterior con su propio NIF cierra el bloque:
        # su etiqueta pertenece a ese otro NIF.
        if TAX_ID_PATTERN.search(previous_line):
            break

        role = label_role_in_text(previous_line)

        if role:
            return role

    return None


def find_tax_id_candidates(
    text: str,
) -> list[dict[str, Any]]:
    lines = get_lines(text)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for line_index, line in enumerate(lines):
        for match in TAX_ID_PATTERN.finditer(line):
            tax_id = normalize_tax_id(match.group(0))

            if not tax_id:
                continue

            signature = (tax_id, line_index)

            if signature in seen:
                continue

            seen.add(signature)

            start = max(0, line_index - 3)
            end = min(len(lines), line_index + 4)
            context = " | ".join(lines[start:end])

            role = "unknown"
            confidence = 60

            label_role = tax_id_label_role(
                lines,
                line_index,
                line[:match.start()],
            )

            if label_role == "customer":
                role = "customer"
                confidence = 85
            elif label_role == "supplier":
                role = "supplier"
                confidence = 90
            elif line_index <= max(10, len(lines) // 4):
                role = "supplier_candidate"
                confidence = 75

            if is_valid_spanish_tax_id(tax_id):
                confidence += 8
            else:
                confidence -= 8

            candidates.append(
                {
                    "value": tax_id,
                    "role": role,
                    "confidence": max(20, min(confidence, 98)),
                    "line_index": line_index,
                    "context": context[:600],
                    "valid_checksum": is_valid_spanish_tax_id(tax_id),
                }
            )

    return candidates


def choose_tax_ids(
    candidates: list[dict[str, Any]],
    company_tax_id: str | None,
) -> tuple[ExtractedField, ExtractedField]:
    configured_ids = normalized_company_tax_ids(company_tax_id)

    supplier = ExtractedField()
    customer = ExtractedField()

    configured_candidates = [
        candidate
        for candidate in candidates
        if candidate["value"] in configured_ids
    ]

    if configured_candidates:
        selected = configured_candidates[0]
        customer = ExtractedField(
            value=selected["value"],
            confidence=99,
            source="configured_company_tax_id",
            evidence=selected["context"],
        )

    supplier_candidates = [
        candidate
        for candidate in candidates
        if candidate["value"] not in configured_ids
    ]

    def supplier_score(candidate: dict[str, Any]) -> tuple[int, int]:
        score = int(candidate["confidence"])

        if candidate["role"] == "supplier":
            score += 25
        elif candidate["role"] == "supplier_candidate":
            score += 12
        elif candidate["role"] == "customer":
            score -= 20

        if candidate["valid_checksum"]:
            score += 5

        return score, -int(candidate["line_index"])

    if supplier_candidates:
        selected = max(
            supplier_candidates,
            key=supplier_score,
        )

        supplier = ExtractedField(
            value=selected["value"],
            confidence=min(
                98,
                max(45, supplier_score(selected)[0]),
            ),
            source="ranked_supplier_tax_id",
            evidence=selected["context"],
        )

    if customer.value is None:
        customer_candidates = [
            candidate
            for candidate in candidates
            if (
                candidate["role"] == "customer"
                and candidate["value"] != supplier.value
            )
        ]

        if customer_candidates:
            selected = max(
                customer_candidates,
                key=lambda item: item["confidence"],
            )

            customer = ExtractedField(
                value=selected["value"],
                confidence=selected["confidence"],
                source="customer_tax_id_label",
                evidence=selected["context"],
            )

    return supplier, customer


def detect_direction(
    candidates: list[dict[str, Any]],
    company_tax_id: Any,
) -> tuple[str, int]:
    """
    Decide si la factura la emite la propia empresa (ISSUED) o la recibe
    (RECEIVED) según dónde aparece su NIF. Sin NIF configurado se asume
    recibida con confianza baja.
    """
    configured_ids = normalized_company_tax_ids(company_tax_id)

    if not configured_ids or not candidates:
        return "RECEIVED", 50

    own = [
        candidate
        for candidate in candidates
        if candidate["value"] in configured_ids
    ]

    if not own:
        # Nuestro NIF no aparece: puede ser un ticket o factura simplificada.
        return "RECEIVED", 60

    if any(candidate["role"] == "supplier" for candidate in own):
        return "ISSUED", 92

    if any(candidate["role"] == "customer" for candidate in own):
        return "RECEIVED", 95

    first = min(candidates, key=lambda item: item["line_index"])

    if first["value"] in configured_ids:
        return "ISSUED", 80

    return "RECEIVED", 80


def choose_tax_ids_for_issued(
    candidates: list[dict[str, Any]],
    company_tax_id: Any,
) -> tuple[ExtractedField, ExtractedField]:
    configured_ids = normalized_company_tax_ids(company_tax_id)

    own = [
        candidate
        for candidate in candidates
        if candidate["value"] in configured_ids
    ]
    others = [
        candidate
        for candidate in candidates
        if candidate["value"] not in configured_ids
    ]

    supplier = ExtractedField()

    if own:
        supplier = ExtractedField(
            value=own[0]["value"],
            confidence=99,
            source="configured_company_tax_id",
            evidence=own[0]["context"],
        )

    customer = ExtractedField()

    if others:
        def customer_score(candidate: dict[str, Any]) -> tuple[int, int]:
            score = int(candidate["confidence"])

            if candidate["role"] == "customer":
                score += 25

            if candidate["valid_checksum"]:
                score += 5

            return score, -int(candidate["line_index"])

        selected = max(others, key=customer_score)
        customer = ExtractedField(
            value=selected["value"],
            confidence=min(98, max(45, customer_score(selected)[0])),
            source="ranked_customer_tax_id",
            evidence=selected["context"],
        )

    return supplier, customer


def looks_like_company_name(line: str) -> bool:
    cleaned = line.strip(" :-|")
    normalized = normalize_search_text(cleaned)

    if len(cleaned) < 3 or len(cleaned) > 140:
        return False

    if any(word in normalized for word in NAME_REJECT_WORDS):
        return False

    if DATE_PATTERN.search(cleaned):
        return False

    if MONEY_TOKEN_PATTERN.fullmatch(cleaned):
        return False

    letters = sum(character.isalpha() for character in cleaned)
    digits = sum(character.isdigit() for character in cleaned)

    if letters < 4:
        return False

    if digits > max(4, letters // 2):
        return False

    return True

def clean_company_name_candidate(
    line: str,
    tax_id: str | None,
) -> str:
    candidate = line

    if tax_id:
        for match in TAX_ID_PATTERN.finditer(candidate):
            matched_tax_id = normalize_tax_id(
                match.group(0)
            )

            if matched_tax_id == normalize_tax_id(tax_id):
                candidate = candidate[:match.start()]
                break

    # Recorta información legal posterior al nombre.
    candidate = re.split(
        r"(?i)"
        r"\|\s*C\.?\s*I\.?\s*F\.?"
        r"|\bC\.?\s*I\.?\s*F\.?\s*[:.]"
        r"|\bN\.?\s*I\.?\s*F\.?\s*[:.]"
        r"|\binscrita?\b"
        r"|\bregistro mercantil\b",
        candidate,
        maxsplit=1,
    )[0]

    candidate = re.sub(
        r"(?i)\b(?:cif|nif|dni|vat|tax\s*id)\b",
        "",
        candidate,
    )

    # Títulos del documento que comparten línea con el emisor:
    # "EMPRESA S.A.   FACTURA" o "FACTURA SIMPLIFICADA".
    candidate = re.sub(
        r"(?i)\b(?:factura(?:\s+simplificada|\s+rectificativa)?"
        r"|invoice)\b\s*$",
        "",
        candidate.strip(),
    )

    # Etiquetas iniciales: "Cliente: EMPRESA S.L." -> "EMPRESA S.L."
    candidate = re.sub(
        r"(?i)^\s*(?:emisor|proveedor|cliente|destinatario|receptor"
        r"|raz[oó]n social|facturar a|datos del proveedor"
        r"|datos de cliente)\s*[:.\-]\s*",
        "",
        candidate,
    )

    candidate = candidate.strip(
        " \t:-|,;"
    )

    # Conserva el punto final de abreviaturas societarias (S.A., S.L.U.)
    # y elimina el resto de puntos finales.
    candidate = re.sub(
        r"(?<!\b[A-Za-z])\.+$",
        "",
        candidate,
    ).strip()

    candidate = re.sub(
        r"\s+",
        " ",
        candidate,
    )

    return candidate

def company_name_near_tax_id(
    text: str,
    tax_id: str | None,
    role: str,
) -> ExtractedField:
    if not tax_id:
        return ExtractedField()

    lines = get_lines(text)
    normalized_tax_id = normalize_tax_id(tax_id)

    scored: list[tuple[int, str, str]] = []

    for index, line in enumerate(lines):
        line_tax_ids = [
            normalize_tax_id(match.group(0))
            for match in TAX_ID_PATTERN.finditer(line)
        ]

        if normalized_tax_id not in line_tax_ids:
            continue

        # La propia línea es prioritaria porque existen formatos:
        # EMPRESA S.L. | C.I.F.: B12345678
        same_line_candidate = clean_company_name_candidate(
            line,
            tax_id,
        )

        if looks_like_company_name(
            same_line_candidate
        ):
            score = 96

            normalized_candidate = normalize_search_text(
                same_line_candidate
            )

            if any(
                suffix in normalized_candidate
                for suffix in LEGAL_SUFFIXES
            ):
                score += 2

            scored.append(
                (
                    min(score, 99),
                    same_line_candidate,
                    line,
                )
            )

        # Busca también líneas cercanas.
        for offset in (-4, -3, -2, -1, 1, 2):
            candidate_index = index + offset

            if not 0 <= candidate_index < len(lines):
                continue

            candidate_line = lines[candidate_index]

            # Una línea con otro NIF pertenece a la otra parte.
            other_tax_ids = {
                normalize_tax_id(match.group(0))
                for match in TAX_ID_PATTERN.finditer(candidate_line)
            } - {normalized_tax_id}

            if other_tax_ids:
                continue

            opposite_role = (
                "customer"
                if role == "supplier"
                else "supplier"
            )

            if label_role_in_text(candidate_line) == opposite_role:
                continue

            candidate = clean_company_name_candidate(
                candidate_line,
                None,
            )

            if not looks_like_company_name(candidate):
                continue

            normalized_candidate = normalize_search_text(
                candidate
            )

            distance = abs(offset)
            score = 87 - distance * 6

            labels = (
                SUPPLIER_LABELS
                if role == "supplier"
                else CUSTOMER_LABELS
            )

            context_start = max(
                0,
                candidate_index - 2,
            )
            context_end = min(
                len(lines),
                candidate_index + 3,
            )

            context = " ".join(
                lines[context_start:context_end]
            )

            normalized_context = normalize_search_text(
                context
            )

            if any(
                label in normalized_context
                for label in labels
            ):
                score += 10

            if any(
                suffix in normalized_candidate
                for suffix in LEGAL_SUFFIXES
            ):
                score += 8

            if candidate.isupper():
                score += 3

            scored.append(
                (
                    min(score, 98),
                    candidate,
                    candidate_line,
                )
            )

    if not scored:
        return ExtractedField()

    score, value, evidence = max(
        scored,
        key=lambda item: item[0],
    )

    return ExtractedField(
        value=value,
        confidence=score,
        source=f"{role}_name_near_tax_id",
        evidence=evidence[:300],
    )


def company_name_from_header(
    text: str,
) -> ExtractedField:
    """
    Respaldo: el emisor suele figurar en las primeras líneas.
    Se devuelve con confianza moderada para forzar revisión.
    """
    for line in get_lines(text)[:5]:
        if TAX_ID_PATTERN.search(line):
            continue

        if label_role_in_text(line) == "customer":
            continue

        candidate = clean_company_name_candidate(line, None)

        if looks_like_company_name(candidate):
            return ExtractedField(
                value=candidate,
                confidence=65,
                source="supplier_name_header",
                evidence=line[:300],
            )

    return ExtractedField()


def find_invoice_number(
    text: str,
) -> ExtractedField:
    lines = get_lines(text)
    candidates: list[tuple[int, str, str]] = []

    standalone_number_pattern = re.compile(
        r"^[A-Z0-9]{1,12}"
        r"(?:[/_-][A-Z0-9]{1,12})+$",
        re.IGNORECASE,
    )

    for line_index, line in enumerate(lines):
        normalized_line = normalize_search_text(line)

        has_invoice_label = (
            "factura" in normalized_line
            or bool(
                re.search(
                    r"\bfra\.?\b",
                    normalized_line,
                )
            )
        )

        if not has_invoice_label:
            continue

        # Etiqueta y número en la misma línea.
        for pattern in INVOICE_NUMBER_PATTERNS:
            match = pattern.search(line)

            if not match:
                continue

            value = clean_invoice_number(
                match.group(1)
            )

            if value:
                candidates.append(
                    (
                        97,
                        value,
                        line,
                    )
                )

        # Etiqueta en una línea y número debajo.
        for offset in range(1, 5):
            candidate_index = line_index + offset

            if candidate_index >= len(lines):
                break

            candidate_line = lines[candidate_index].strip()
            normalized_candidate = normalize_search_text(
                candidate_line
            )

            if normalized_candidate in {
                "fecha",
                "fecha factura",
                "fecha de factura",
                "fecha exped.",
            }:
                continue

            compact_candidate = re.sub(
                r"\s+",
                "",
                candidate_line,
            )

            if standalone_number_pattern.fullmatch(
                compact_candidate
            ):
                value = clean_invoice_number(
                    compact_candidate
                )

                if value:
                    score = 96 - offset * 2

                    candidates.append(
                        (
                            score,
                            value,
                            f"{line} | {candidate_line}",
                        )
                    )

                    break

            matches = re.findall(
                r"\b[A-Z0-9][A-Z0-9/_\-]{2,24}\b",
                candidate_line,
                re.IGNORECASE,
            )

            for raw_candidate in matches:
                value = clean_invoice_number(
                    raw_candidate
                )

                if not value:
                    continue

                if DATE_PATTERN.fullmatch(value):
                    continue

                score = 90 - offset * 3

                if "/" in value or "-" in value:
                    score += 4

                candidates.append(
                    (
                        score,
                        value,
                        f"{line} | {candidate_line}",
                    )
                )

    if not candidates:
        return ExtractedField()

    # A igual puntuación gana el candidato más completo
    # ("FRA-2026-001" frente a "2026-001").
    score, value, evidence = max(
        candidates,
        key=lambda item: (item[0], len(item[1])),
    )

    return ExtractedField(
        value=value,
        confidence=min(score, 99),
        source="invoice_number_label",
        evidence=evidence[:300],
    )


def find_date_near_labels(
    text: str,
    labels: tuple[str, ...],
    source: str,
    exclude_labels: tuple[str, ...] = (),
) -> ExtractedField:
    lines = get_lines(text)
    candidates: list[tuple[int, date, str]] = []

    def is_excluded(normalized_value: str) -> bool:
        return any(
            label in normalized_value
            for label in exclude_labels
        )

    for line_index, line in enumerate(lines):
        normalized_line = normalize_search_text(line)

        matched_labels = [
            label
            for label in labels
            if label in normalized_line
        ]

        if not matched_labels:
            continue

        # Una etiqueta genérica ("fecha") dentro de una línea con
        # etiqueta excluida ("fecha de vencimiento") no cuenta.
        if is_excluded(normalized_line) and not any(
            len(label) > len("fecha") for label in matched_labels
        ):
            continue

        specificity_bonus = (
            2
            if any(len(label) > len("fecha") for label in matched_labels)
            else 0
        )

        for offset in range(0, 5):
            candidate_index = line_index + offset

            if candidate_index >= len(lines):
                break

            candidate_line = lines[candidate_index]

            if offset > 0 and is_excluded(
                normalize_search_text(candidate_line)
            ):
                break

            for match in DATE_PATTERN.finditer(candidate_line):
                parsed = parse_date_value(match.group(1))

                if not parsed:
                    continue

                score = 95 - offset * 5 + specificity_bonus

                candidates.append(
                    (
                        score,
                        parsed,
                        f"{line} | {candidate_line}",
                    )
                )

                # En la misma línea, la primera fecha tras la etiqueta
                # es la buena.
                break

    if not candidates:
        return ExtractedField()

    score, parsed, evidence = max(
        candidates,
        key=lambda item: item[0],
    )

    return ExtractedField(
        value=date_to_string(parsed),
        confidence=min(score, 98),
        source=source,
        evidence=evidence[:300],
    )


def find_fallback_invoice_date(text: str) -> ExtractedField:
    candidates: list[date] = []

    for raw_date in DATE_PATTERN.findall(text):
        parsed = parse_date_value(raw_date)

        if parsed:
            candidates.append(parsed)

    if not candidates:
        return ExtractedField()

    selected = candidates[0]

    return ExtractedField(
        value=date_to_string(selected),
        confidence=55,
        source="first_plausible_date",
        evidence=selected.isoformat(),
    )


def amount_labels(field_name: str) -> tuple[str, ...]:
    labels = {
        "subtotal": (
            "base imponible",
            "subtotal",
            "importe neto",
            "base iva",
            "suma parcial",
            "honorarios",
        ),
        "tax_total": (
            "total iva",
            "cuota iva",
            "importe iva",
            "i.v.a.",
            "iva 21%",
            "iva",
        ),
        "withholding_total": (
            "retencion",
            "retención",
            "irpf",
        ),
        "surcharge_total": (
            "recargo equivalencia",
            "recargo de equivalencia",
            "cuota r.e.",
        ),
        "total": (
            "total a pagar",
            "total factura",
            "importe total",
            "total impuestos incluidos",
            "total eur",
            "total:",
            "total",
        ),
    }

    return labels[field_name]


def find_labeled_amount(
    text: str,
    field_name: str,
    *,
    allow_inference: bool = True,
) -> ExtractedField:
    del allow_inference

    lines = get_lines(text)
    labels = amount_labels(field_name)
    candidates: list[tuple[int, int, Decimal, str]] = []

    for line_index, line in enumerate(lines):
        normalized_line = normalize_search_text(line)

        matching_labels = [
            label
            for label in labels
            if normalize_search_text(label) in normalized_line
        ]

        if not matching_labels:
            continue

        # Impide que "TOTAL IVA" se interprete como total de factura.
        if (
            field_name == "total"
            and (
                "total iva" in normalized_line
                or "total impuestos" in normalized_line
                and "incluidos" not in normalized_line
            )
        ):
            continue

        values = money_values(line)

        if field_name == "tax_total":
            values = [
                item
                for item in values
                if item[0] <= Decimal("100000000")
            ]

        if values:
            amount, raw_value = values[-1]

            score = 90

            if field_name == "total":
                if "total a pagar" in normalized_line:
                    score = 99
                elif "total factura" in normalized_line:
                    score = 98
                elif "importe total" in normalized_line:
                    score = 97
                elif "total impuestos incluidos" in normalized_line:
                    score = 96

            candidates.append(
                (
                    score,
                    line_index,
                    abs(amount)
                    if field_name == "withholding_total"
                    else amount,
                    line,
                )
            )
            continue

        # Etiqueta e importe pueden estar separados.
        for offset in range(1, 5):
            candidate_index = line_index + offset

            if candidate_index >= len(lines):
                break

            candidate_line = lines[candidate_index]
            values = money_values(candidate_line)

            if not values:
                continue

            # Las líneas puramente monetarias son más fiables.
            amount, raw_value = values[-1]
            score = 85 - offset * 5

            candidates.append(
                (
                    score,
                    candidate_index,
                    abs(amount)
                    if field_name == "withholding_total"
                    else amount,
                    f"{line} | {candidate_line}",
                )
            )
            break

    if not candidates:
        return ExtractedField()

    # En multipágina suele ser más fiable el último resumen.
    if field_name in {
        "subtotal",
        "tax_total",
        "withholding_total",
        "surcharge_total",
        "total",
    }:
        selected = max(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
            ),
        )
    else:
        selected = candidates[-1]

    score, _, amount, evidence = selected

    return ExtractedField(
        value=decimal_to_string(amount),
        confidence=min(score, 99),
        source=f"{field_name}_label",
        evidence=evidence[:300],
    )


def extract_stacked_summary(
    text: str,
) -> dict[str, ExtractedField]:
    """
    Resuelve bloques como:

        HONORARIOS
        21% I.V.A.
        Retención 15% IRPF
        TOTAL
        1.111,11
        233,33
        -166,67
        1.177,77
    """
    lines = get_lines(text)
    result: dict[str, ExtractedField] = {}

    for start_index in range(len(lines)):
        labels: list[tuple[str, int]] = []

        for index in range(start_index, min(len(lines), start_index + 8)):
            normalized = normalize_search_text(lines[index])

            field_name: str | None = None

            if "retencion" in normalized or "irpf" in normalized:
                field_name = "withholding_total"
            elif "iva" in normalized:
                field_name = "tax_total"
            elif (
                "base imponible" in normalized
                or "subtotal" in normalized
                or "honorarios" in normalized
            ):
                field_name = "subtotal"
            elif normalized == "total" or "total factura" in normalized:
                field_name = "total"

            if field_name and field_name not in [
                item[0]
                for item in labels
            ]:
                labels.append((field_name, index))

        if len(labels) < 3:
            continue

        search_start = max(index for _, index in labels) + 1
        amounts: list[tuple[Decimal, str]] = []

        for index in range(
            search_start,
            min(len(lines), search_start + 10),
        ):
            values = money_values(lines[index])

            if len(values) == 1:
                amounts.append((values[0][0], lines[index]))

            if len(amounts) >= len(labels):
                break

        if len(amounts) < len(labels):
            continue

        for position, (field_name, _) in enumerate(labels):
            amount, evidence = amounts[position]

            if field_name == "withholding_total":
                amount = abs(amount)

            result[field_name] = ExtractedField(
                value=decimal_to_string(amount),
                confidence=94,
                source="stacked_fiscal_summary",
                evidence=evidence,
            )

        break

    return result


def detect_tax_rate(text: str) -> Decimal | None:
    rates: list[Decimal] = []

    for match in re.finditer(
        r"\b(?:IVA|I\.V\.A\.)?\s*"
        r"(\d{1,2}(?:[,.]\d{1,3})?)\s*%",
        text,
        re.IGNORECASE,
    ):
        rate = normalize_amount(match.group(1))

        if rate is not None and Decimal("0") <= rate <= Decimal("30"):
            rates.append(rate)

    if Decimal("21.00") in rates:
        return Decimal("21.00")

    return rates[0] if rates else None


def reconcile_amounts(
    subtotal: ExtractedField,
    tax_total: ExtractedField,
    withholding_total: ExtractedField,
    surcharge_total: ExtractedField,
    total: ExtractedField,
    text: str,
) -> tuple[
    ExtractedField,
    ExtractedField,
    ExtractedField,
    ExtractedField,
    ExtractedField,
]:
    base = normalize_amount(subtotal.value)
    tax = normalize_amount(tax_total.value)
    withholding = (
        normalize_amount(withholding_total.value)
        or Decimal("0.00")
    )
    surcharge = (
        normalize_amount(surcharge_total.value)
        or Decimal("0.00")
    )
    grand_total = normalize_amount(total.value)
    rate = detect_tax_rate(text)

    if (
        base is not None
        and grand_total is not None
        and tax is None
    ):
        calculated_tax = (
            grand_total
            - base
            + withholding
            - surcharge
        ).quantize(CENT)

        if calculated_tax >= 0:
            tax_total = ExtractedField(
                value=decimal_to_string(calculated_tax),
                confidence=84,
                source="arithmetic_total_minus_base",
                evidence=(
                    f"{grand_total} - {base} "
                    f"+ {withholding} - {surcharge}"
                ),
            )
            tax = calculated_tax

    if (
        tax is not None
        and grand_total is not None
        and base is None
    ):
        calculated_base = (
            grand_total
            - tax
            + withholding
            - surcharge
        ).quantize(CENT)

        if calculated_base >= 0:
            subtotal = ExtractedField(
                value=decimal_to_string(calculated_base),
                confidence=84,
                source="arithmetic_total_minus_tax",
                evidence=(
                    f"{grand_total} - {tax} "
                    f"+ {withholding} - {surcharge}"
                ),
            )
            base = calculated_base

    if (
        base is not None
        and tax is not None
        and grand_total is None
    ):
        calculated_total = (
            base
            + tax
            + surcharge
            - withholding
        ).quantize(CENT)

        total = ExtractedField(
            value=decimal_to_string(calculated_total),
            confidence=82,
            source="arithmetic_total",
            evidence=(
                f"{base} + {tax} "
                f"+ {surcharge} - {withholding}"
            ),
        )
        grand_total = calculated_total

    # Si solo existe total y un IVA inequívoco, se puede inferir base e IVA.
    if (
        grand_total is not None
        and base is None
        and tax is None
        and rate is not None
        and withholding == 0
        and surcharge == 0
    ):
        divisor = Decimal("1") + rate / Decimal("100")
        inferred_base = (
            grand_total / divisor
        ).quantize(CENT)
        inferred_tax = (
            grand_total - inferred_base
        ).quantize(CENT)

        if abs(
            inferred_base
            + inferred_tax
            - grand_total
        ) <= AMOUNT_TOLERANCE:
            subtotal = ExtractedField(
                value=decimal_to_string(inferred_base),
                confidence=72,
                source="inferred_from_total_and_tax_rate",
                evidence=f"Total {grand_total}; IVA {rate}%",
            )
            tax_total = ExtractedField(
                value=decimal_to_string(inferred_tax),
                confidence=72,
                source="inferred_from_total_and_tax_rate",
                evidence=f"Total {grand_total}; IVA {rate}%",
            )

    return (
        subtotal,
        tax_total,
        withholding_total,
        surcharge_total,
        total,
    )


def extract_tax_lines(
    text: str,
    subtotal: ExtractedField,
    tax_total: ExtractedField,
) -> list[TaxLineResult]:
    base = normalize_amount(subtotal.value)
    tax = normalize_amount(tax_total.value)
    rate = detect_tax_rate(text)

    if base is None or tax is None:
        return []

    if rate is None and base != 0:
        inferred_rate = (
            tax / base * Decimal("100")
        ).quantize(Decimal("0.01"))

        common_rates = (
            Decimal("0.00"),
            Decimal("4.00"),
            Decimal("10.00"),
            Decimal("21.00"),
        )

        closest = min(
            common_rates,
            key=lambda candidate: abs(candidate - inferred_rate),
        )

        if abs(closest - inferred_rate) <= Decimal("0.10"):
            rate = closest

    if rate is None:
        return []

    expected_tax = (
        base * rate / Decimal("100")
    ).quantize(CENT)

    confidence = (
        94
        if abs(expected_tax - tax) <= AMOUNT_TOLERANCE
        else 72
    )

    return [
        TaxLineResult(
            tax_type="IVA",
            tax_rate=decimal_to_string(rate),
            tax_base=decimal_to_string(base),
            tax_amount=decimal_to_string(tax),
            confidence=confidence,
            source="reconciled_tax_summary",
        )
    ]


def extract_concept(text: str) -> ExtractedField:
    lines = get_lines(text)
    start_index: int | None = None

    for index, line in enumerate(lines):
        normalized = normalize_search_text(line)

        if (
            normalized.startswith("concepto")
            or normalized.startswith("descripcion")
            or normalized.startswith("descripción")
        ):
            start_index = index + 1
            break

    if start_index is None:
        return ExtractedField()

    concept_lines: list[str] = []

    for line in lines[start_index:start_index + 12]:
        normalized = normalize_search_text(line)

        if any(
            stop_label in normalized
            for stop_label in (
                "base imponible",
                "total iva",
                "forma de pago",
                "vencimiento",
                "subtotal",
                "total factura",
                "proteccion de datos",
            )
        ):
            break

        if money_values(line) and len(line) < 30:
            continue

        # Quita importes al final de la línea de detalle.
        line = re.sub(
            r"(?:\s+[-+]?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?\s*(?:€|EUR)?)+$",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        if line:
            concept_lines.append(line)

    concept = " ".join(concept_lines)
    concept = re.sub(r"\s+", " ", concept).strip()

    if len(concept) < 5:
        return ExtractedField()

    return ExtractedField(
        value=concept[:1000],
        confidence=78,
        source="description_section",
        evidence=concept[:300],
    )


# Categorías de gasto con su cuenta del Plan General Contable (grupo 6).
# El orden importa solo para desempatar puntuaciones iguales.
EXPENSE_CATEGORIES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Combustible",
        "628",
        (
            "gasoleo", "gasolina", "euro-super", "combustible",
            "carburante", "diesel", "adblue", "litros",
        ),
        ("repsol", "cepsa", "galp", "moeve", "bp oil", "shell", "petronor"),
    ),
    (
        "Suministros",
        "628",
        (
            "suministro electrico", "energia electrica", "electricidad",
            "potencia contratada", "kwh", "gas natural", "suministro de agua",
            "consumo de agua", "termino de energia",
        ),
        ("endesa", "iberdrola", "naturgy", "holaluz", "canal de isabel",
         "aqualia", "totalenergies"),
    ),
    (
        "Telecomunicaciones",
        "629",
        (
            "telefonia", "telefono movil", "fibra", "internet", "linea movil",
            "datos moviles", "centralita",
        ),
        ("vodafone", "movistar", "telefonica", "orange", "masmovil",
         "digi spain", "yoigo", "jazztel"),
    ),
    (
        "Arrendamientos",
        "621",
        ("alquiler", "arrendamiento", "renting", "leasing operativo"),
        (),
    ),
    (
        "Servicios profesionales",
        "623",
        (
            "honorarios", "asesoria", "gestoria", "abogado", "notaria",
            "notario", "auditoria", "consultoria", "arquitecto",
            "direccion de obra", "ingenieria", "estudio geotecnico",
        ),
        (),
    ),
    (
        "Seguros",
        "625",
        ("poliza", "prima de seguro", "seguro de", "aseguradora"),
        ("mapfre", "allianz", "axa ", "zurich", "mutua madrilena",
         "generali", "sanitas", "adeslas"),
    ),
    (
        "Publicidad y marketing",
        "627",
        ("publicidad", "marketing", "anuncio", "campana", "google ads",
         "redes sociales"),
        (),
    ),
    (
        "Reparaciones y mantenimiento",
        "622",
        ("reparacion", "mantenimiento", "averia", "revision tecnica",
         "limpieza de mantenimiento", "servicio de limpieza"),
        (),
    ),
    (
        "Software e informática",
        "629",
        (
            "licencia", "suscripcion", "software", "hosting", "dominio",
            "ordenador", "servidor", "informatica", "openvpn", "cloud",
        ),
        ("microsoft", "google cloud", "amazon web services", "adobe",
         "holded", "ionos"),
    ),
    (
        "Transportes y mensajería",
        "624",
        ("transporte", "mensajeria", "envio", "paqueteria", "porte"),
        ("seur", "mrw", "correos", "nacex", "dhl", "ups ", "gls"),
    ),
    (
        "Viajes y dietas",
        "629",
        ("hotel", "alojamiento", "billete", "vuelo", "restaurante",
         "dietas", "taxi"),
        ("renfe", "iberia", "vueling", "ryanair", "booking"),
    ),
    (
        "Prevención de riesgos laborales",
        "629",
        ("prevencion de riesgos", "vigilancia de la salud",
         "reconocimiento medico"),
        (),
    ),
    (
        "Servicios bancarios",
        "626",
        ("comision", "comisiones bancarias", "mantenimiento de cuenta"),
        (),
    ),
    (
        "Compras y aprovisionamientos",
        "600",
        ("mercaderia", "mercancia", "compra de", "precio unitario",
         "unidades", "producto"),
        ("makro", "mercadona", "leroy merlin", "bricomart"),
    ),
)

DEFAULT_CATEGORY = "Otros gastos"
DEFAULT_ACCOUNT = "629"

# Categorías de ingreso (facturas emitidas), grupo 7 del PGC.
INCOME_SERVICES = "Prestación de servicios"
INCOME_GOODS = "Ventas de mercaderías"
INCOME_CATEGORIES: dict[str, str] = {
    INCOME_SERVICES: "705",
    INCOME_GOODS: "700",
}

CATEGORY_ACCOUNTS: dict[str, str] = {
    name: account
    for name, account, _keywords, _suppliers in EXPENSE_CATEGORIES
}
CATEGORY_ACCOUNTS[DEFAULT_CATEGORY] = DEFAULT_ACCOUNT
CATEGORY_ACCOUNTS.update(INCOME_CATEGORIES)


def classify_income(
    text: str,
    concept: str | None,
) -> ExtractedField:
    normalized = normalize_search_text(f"{concept or ''}\n{text}")

    goods_keywords = (
        "mercaderia", "mercancia", "unidades", "producto", "articulo",
        "precio unitario", "cantidad",
    )
    hits = [keyword for keyword in goods_keywords if keyword in normalized]

    if hits:
        return ExtractedField(
            value=INCOME_GOODS,
            confidence=70,
            source="keyword_income_category",
            evidence=", ".join(hits),
        )

    return ExtractedField(
        value=INCOME_SERVICES,
        confidence=65,
        source="default_income_category",
        evidence=None,
    )


def category_account(category: str | None) -> str:
    return CATEGORY_ACCOUNTS.get(category or "", DEFAULT_ACCOUNT)


def classify_invoice(
    text: str,
    concept: str | None,
    supplier_name: str | None = None,
) -> ExtractedField:
    normalized_text = normalize_search_text(text)
    normalized_concept = normalize_search_text(concept or "")
    normalized_supplier = normalize_search_text(supplier_name or "")

    best: tuple[int, str, list[str]] | None = None

    for category, _account, keywords, suppliers in EXPENSE_CATEGORIES:
        score = 0
        evidence: list[str] = []

        for keyword in keywords:
            if keyword in normalized_concept:
                score += 3
                evidence.append(keyword)
            elif keyword in normalized_text:
                score += 1
                evidence.append(keyword)

        for supplier in suppliers:
            if supplier in normalized_supplier:
                score += 4
                evidence.append(supplier.strip())
            elif supplier in normalized_text:
                score += 2
                evidence.append(supplier.strip())

        if score and (best is None or score > best[0]):
            best = (score, category, evidence)

    if best is None:
        return ExtractedField(
            value=DEFAULT_CATEGORY,
            confidence=40,
            source="default_category",
            evidence=None,
        )

    score, category, evidence = best

    return ExtractedField(
        value=category,
        confidence=min(92, 60 + score * 5),
        source="keyword_category",
        evidence=", ".join(evidence),
    )


def detect_currency(text: str) -> ExtractedField:
    normalized = normalize_search_text(text)

    if "€" in text or " eur" in normalized:
        return ExtractedField(
            value="EUR",
            confidence=98,
            source="currency_symbol",
            evidence="EUR / €",
        )

    if "$" in text or " usd" in normalized:
        return ExtractedField(
            value="USD",
            confidence=85,
            source="currency_symbol",
            evidence="USD / $",
        )

    if "£" in text or " gbp" in normalized:
        return ExtractedField(
            value="GBP",
            confidence=85,
            source="currency_symbol",
            evidence="GBP / £",
        )

    return ExtractedField(
        value="EUR",
        confidence=45,
        source="default_currency",
        evidence=None,
    )


def detect_invoice_likelihood(
    text: str,
    fields: dict[str, ExtractedField],
) -> tuple[bool, int, list[str]]:
    normalized = normalize_search_text(text)
    score = 0
    signals: list[str] = []

    rules = (
        (
            "invoice_keyword",
            "factura" in normalized,
            25,
        ),
        (
            "invoice_number",
            fields["invoice_number"].value is not None,
            15,
        ),
        (
            "invoice_date",
            fields["invoice_date"].value is not None,
            10,
        ),
        (
            "supplier_tax_id",
            fields["supplier_tax_id"].value is not None,
            15,
        ),
        (
            "subtotal",
            fields["subtotal"].value is not None,
            10,
        ),
        (
            "tax_total",
            fields["tax_total"].value is not None,
            10,
        ),
        (
            "total",
            fields["total"].value is not None,
            20,
        ),
        (
            "vat_keyword",
            "iva" in normalized,
            10,
        ),
    )

    for signal, condition, points in rules:
        if condition:
            score += points
            signals.append(signal)

    score = min(score, 100)

    return score >= 45, score, signals


def calculate_overall_confidence(
    fields: dict[str, ExtractedField],
    invoice_likelihood: int,
) -> int:
    important_fields = (
        "supplier_name",
        "supplier_tax_id",
        "invoice_number",
        "invoice_date",
        "subtotal",
        "tax_total",
        "total",
    )

    confidences = [
        fields[field_name].confidence
        for field_name in important_fields
        if fields[field_name].value is not None
    ]

    if not confidences:
        return min(invoice_likelihood, 30)

    average = sum(confidences) / len(confidences)
    completeness = len(confidences) / len(important_fields)

    result = (
        average * 0.70
        + invoice_likelihood * 0.20
        + completeness * 100 * 0.10
    )

    return min(int(round(result)), 99)


def serialize_field(field: ExtractedField) -> dict[str, Any]:
    return asdict(field)


def extract_invoice(
    path: Path,
    company_tax_id: str | None = None,
    company_name: str | None = None,
) -> dict[str, Any]:
    (
        complete_text,
        page_count,
        requires_ocr,
        page_texts,
    ) = read_document(path)

    if requires_ocr:
        return {
            "extractor_name": EXTRACTOR_NAME,
            "extractor_version": EXTRACTOR_VERSION,
            "document_type": "unknown",
            "is_invoice": False,
            "invoice_likelihood": 0,
            "requires_ocr": True,
            "page_count": page_count,
            "raw_text": complete_text,
            "fields": {},
            "tax_lines": [],
            "candidates": {
                "tax_ids": [],
            },
            "signals": [
                "ocr_required",
            ],
            "overall_confidence": 10,
        }

    primary_text, primary_pages = select_primary_pages(
        page_texts
    )

    from app import extraction_rules

    primary_text = extraction_rules.restore_rotated_text(primary_text)
    real_world_signals: list[str] = []

    tax_id_candidates = find_tax_id_candidates(
        primary_text
    )
    for candidate in tax_id_candidates:
        repaired = extraction_rules.repair_tax_id(candidate["value"])
        if repaired != candidate["value"] and is_valid_spanish_tax_id(repaired):
            candidate["value"] = repaired
            candidate["valid_checksum"] = True
            real_world_signals.append("tax_id_repaired")

    direction, direction_confidence = detect_direction(
        tax_id_candidates,
        company_tax_id,
    )

    if direction == "ISSUED":
        (
            supplier_tax_id,
            customer_tax_id,
        ) = choose_tax_ids_for_issued(
            tax_id_candidates,
            company_tax_id,
        )
    else:
        (
            supplier_tax_id,
            customer_tax_id,
        ) = choose_tax_ids(
            tax_id_candidates,
            company_tax_id,
        )

    supplier_name = company_name_near_tax_id(
        primary_text,
        supplier_tax_id.value,
        "supplier",
    )

    if supplier_name.value is None and supplier_tax_id.value:
        supplier_name = company_name_from_header(primary_text)

    customer_name = company_name_near_tax_id(
        primary_text,
        customer_tax_id.value,
        "customer",
    )

    if direction == "ISSUED":
        if company_name:
            supplier_name = ExtractedField(
                value=company_name,
                confidence=99,
                source="configured_company_name",
                evidence=supplier_name.evidence,
            )
    elif customer_tax_id.value and company_name:
        customer_name = ExtractedField(
            value=company_name,
            confidence=99,
            source="configured_company_name",
            evidence=customer_name.evidence,
        )

    # El emisor según el pie legal (Registro Mercantil, protección de datos).
    company_ids = normalized_company_tax_ids(company_tax_id)
    issuer = extraction_rules.issuer_from_legal_footer(primary_text, company_ids, company_name)
    if issuer is not None:
        own_in_text = any(candidate["value"] in company_ids for candidate in tax_id_candidates)
        if issuer.tax_id and supplier_tax_id.value != issuer.tax_id and (own_in_text or not supplier_tax_id.value):
            supplier_tax_id = ExtractedField(value=issuer.tax_id, confidence=90, source="legal_footer", evidence=issuer.source)
            if own_in_text:
                customer_tax_id = ExtractedField(value=sorted(company_ids & {c["value"] for c in tax_id_candidates})[0], confidence=90, source="company_tax_id", evidence="configured")
                if company_name:
                    customer_name = ExtractedField(value=company_name, confidence=99, source="configured_company_name")
            if direction == "ISSUED":
                direction, direction_confidence = "RECEIVED", 88
                real_world_signals.append("direction_from_legal_footer")
            supplier_name = ExtractedField(value=issuer.name, confidence=85, source="legal_footer", evidence=issuer.source) if issuer.name else supplier_name
        elif direction != "ISSUED" and issuer.name and extraction_rules.weak_name(supplier_name.value, company_name):
            supplier_name = ExtractedField(value=issuer.name, confidence=80, source="legal_footer", evidence=issuer.source)
            real_world_signals.append("supplier_name_from_legal_footer")

    invoice_number = find_invoice_number(
        primary_text
    )
    if extraction_rules.suspicious_number(invoice_number.value):
        better = extraction_rules.labeled_number(primary_text) or extraction_rules.number_from_header_row(primary_text)
        if better:
            invoice_number = ExtractedField(value=clean_invoice_number(better) or better, confidence=80, source="table_header_row", evidence=better)
            real_world_signals.append("invoice_number_from_table")

    invoice_date = find_date_near_labels(
        primary_text,
        (
            "fecha factura",
            "fecha de factura",
            "fecha exped.",
            "fecha expedicion",
            "fecha expedición",
            "fecha",
        ),
        source="invoice_date_label",
        exclude_labels=(
            "vencimiento",
            "fecha de pago",
            "fecha limite",
            "fecha de entrega",
            "fecha de alta",
            "periodo",
        ),
    )

    if invoice_date.value is None:
        textual = extraction_rules.textual_dates(primary_text)
        if textual:
            invoice_date = ExtractedField(value=date_to_string(textual[0]), confidence=80, source="textual_date")

    if invoice_date.value is None:
        invoice_date = find_fallback_invoice_date(
            primary_text
        )

    due_date = find_date_near_labels(
        primary_text,
        (
            "fecha de vencimiento",
            "vencimiento",
            "vencimientos",
        ),
        source="due_date_label",
    )
    if due_date.value is None:
        header_due = extraction_rules.due_date_from_header_row(primary_text)
        if header_due:
            due_date = ExtractedField(value=date_to_string(header_due), confidence=75, source="due_date_table")

    subtotal = find_labeled_amount(
        primary_text,
        "subtotal",
    )
    tax_total = find_labeled_amount(
        primary_text,
        "tax_total",
    )
    withholding_total = find_labeled_amount(
        primary_text,
        "withholding_total",
    )
    surcharge_total = find_labeled_amount(
        primary_text,
        "surcharge_total",
    )
    total = find_labeled_amount(
        primary_text,
        "total",
    )

    stacked = extract_stacked_summary(
        primary_text
    )

    for field_name, stacked_field in stacked.items():
        if field_name == "subtotal":
            subtotal = stacked_field
        elif field_name == "tax_total":
            tax_total = stacked_field
        elif field_name == "withholding_total":
            withholding_total = stacked_field
        elif field_name == "surcharge_total":
            surcharge_total = stacked_field
        elif field_name == "total":
            total = stacked_field

    (
        subtotal,
        tax_total,
        withholding_total,
        surcharge_total,
        total,
    ) = reconcile_amounts(
        subtotal,
        tax_total,
        withholding_total,
        surcharge_total,
        total,
        primary_text,
    )

    # Importes que no cuadran fiscalmente: el solver busca base × IVA = cuota y base + cuota − retención = total.
    if not extraction_rules.plausible_amounts(subtotal.value, tax_total.value, total.value, withholding_total.value, surcharge_total.value):
        solved = extraction_rules.solve_amounts(primary_text)
        if solved:
            subtotal = ExtractedField(value=decimal_to_string(solved["subtotal"]), confidence=85, source="vat_solver", evidence=f"IVA {solved['tax_rate']} %")
            tax_total = ExtractedField(value=decimal_to_string(solved["tax_total"]), confidence=85, source="vat_solver", evidence=f"IVA {solved['tax_rate']} %")
            total = ExtractedField(value=decimal_to_string(solved["total"]), confidence=85, source="vat_solver", evidence="base + cuota − retención")
            withholding_total = ExtractedField(value=decimal_to_string(solved["withholding_total"]) if solved["withholding_total"] else None, confidence=80 if solved["withholding_total"] else 0, source="vat_solver")
            real_world_signals.append("amounts_from_vat_solver")

    concept = extract_concept(primary_text)
    if direction == "ISSUED":
        category = classify_income(
            primary_text,
            concept.value,
        )
    else:
        category = classify_invoice(
            primary_text,
            concept.value,
            supplier_name.value,
        )
    currency = detect_currency(primary_text)

    tax_lines = extract_tax_lines(
        primary_text,
        subtotal,
        tax_total,
    )

    fields = {
        "supplier_name": supplier_name,
        "supplier_tax_id": supplier_tax_id,
        "customer_name": customer_name,
        "customer_tax_id": customer_tax_id,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "subtotal": subtotal,
        "tax_total": tax_total,
        "withholding_total": withholding_total,
        "surcharge_total": surcharge_total,
        "total": total,
        "currency": currency,
        "concept": concept,
        "category": category,
    }

    (
        is_invoice,
        invoice_likelihood,
        signals,
    ) = detect_invoice_likelihood(
        primary_text,
        fields,
    )

    if len(primary_pages) < len(page_texts):
        signals.append("attachments_excluded")
    signals.extend(real_world_signals)

    overall_confidence = calculate_overall_confidence(
        fields,
        invoice_likelihood,
    )

    return {
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "document_type": (
            "invoice"
            if is_invoice
            else "unknown"
        ),
        "is_invoice": is_invoice,
        "invoice_likelihood": invoice_likelihood,
        "direction": direction,
        "direction_confidence": direction_confidence,
        "requires_ocr": False,
        "page_count": page_count,
        "processed_pages": primary_pages,
        "excluded_pages": [
            page_number
            for page_number in range(1, len(page_texts) + 1)
            if page_number not in primary_pages
        ],
        "raw_text": complete_text,
        "primary_text": primary_text,
        "fields": {
            name: serialize_field(field)
            for name, field in fields.items()
        },
        "tax_lines": [
            asdict(tax_line)
            for tax_line in tax_lines
        ],
        "candidates": {
            "tax_ids": tax_id_candidates,
        },
        "signals": signals,
        "overall_confidence": overall_confidence,
    }


def extract(path: Path) -> dict[str, Any]:
    """
    Compatibilidad con módulos anteriores.
    """
    result = extract_invoice(path)
    fields = result.get("fields", {})

    def value(field_name: str) -> Any:
        return fields.get(field_name, {}).get("value")

    return {
        "raw_text": result.get("raw_text", ""),
        "raw_text_found": bool(
            result.get("raw_text", "").strip()
        ),
        "needs_ocr": result.get(
            "requires_ocr",
            False,
        ),
        "supplier": value("supplier_name"),
        "cif": value("supplier_tax_id"),
        "concept": value("concept"),
        "classification": value("category"),
        "base": normalize_amount(value("subtotal")),
        "tax": normalize_amount(value("tax_total")),
        "amount": normalize_amount(value("total")),
        "date": value("invoice_date"),
        "invoice_number": value("invoice_number"),
        "iva_pct": None,
        "employee": None,
    }


def compute_confidence(data: dict[str, Any]) -> int:
    if data.get("needs_ocr"):
        return 10

    score = 0

    if data.get("supplier"):
        score += 20
    if data.get("cif"):
        score += 15
    if data.get("invoice_number"):
        score += 15
    if data.get("date"):
        score += 10
    if data.get("base") is not None:
        score += 10
    if data.get("tax") is not None:
        score += 10
    if data.get("amount") is not None:
        score += 20

    return min(score, 99)


def eur(
    value: Decimal | float | int | None,
) -> str:
    if value is None:
        return "-"

    amount = normalize_amount(value)

    if amount is None:
        return "-"

    formatted = f"{amount:,.2f}"

    return (
        formatted
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
        + " €"
    )


def is_aeat(text: str, filename: str) -> bool:
    haystack = normalize_search_text(
        f"{text} {filename}"
    )

    keywords = (
        "agencia tributaria",
        "aeat",
        "requerimiento",
        "providencia de apremio",
        "diligencia de embargo",
        "procedimiento sancionador",
    )

    return any(
        keyword in haystack
        for keyword in keywords
    )


def is_laboral(text: str, filename: str) -> bool:
    haystack = normalize_search_text(
        f"{text} {filename}"
    )

    keywords = (
        "nomina",
        "liquido a percibir",
        "contrato de trabajo",
        "alta en seguridad social",
        "modelo 145",
        "comunicacion de datos al pagador",
    )

    return any(
        keyword in haystack
        for keyword in keywords
    )


def detect_laboral_subtype(
    text: str,
    filename: str,
) -> str:
    haystack = normalize_search_text(
        f"{text} {filename}"
    )

    if "contrato" in haystack:
        return "Contrato de trabajo"

    if (
        "modelo 145" in haystack
        or "comunicacion de datos al pagador" in haystack
    ):
        return "Modelo 145 (IRPF)"

    if (
        "alta" in haystack
        and "seguridad social" in haystack
    ):
        return "Alta en Seguridad Social"

    return "Nómina del mes"


def detect_aeat_type(
    text: str,
    filename: str,
) -> tuple[str, int, str | None]:
    haystack = normalize_search_text(
        f"{text} {filename}"
    )

    if "embargo" in haystack:
        return "Diligencia de embargo", 3, "Crítica"

    if "apremio" in haystack:
        return "Providencia de apremio", 15, "Crítica"

    if (
        "procedimiento sancionador" in haystack
        or "sancion" in haystack
    ):
        return "Procedimiento sancionador", 15, "Alta"

    if "requerimiento" in haystack:
        return "Requerimiento de documentación", 10, None

    return "Notificación AEAT", 10, None