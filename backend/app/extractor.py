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
        (?<!N\.I\.)(?<!C\.I\.)(?<!N\.I)(?<!C\.I)  # la «F» de «N.I.F. 12.345.678-Z» es de la etiqueta
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
    r"|"
    r"\d{1,2}[/-](?i:ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[/-]\d{2,4}"
    r")\b"
)
SHORT_MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9,
                "oct": 10, "nov": 11, "dic": 12}

PLAIN_VOWELS = str.maketrans("áéíóúÁÉÍÓÚ", "aeiouAEIOU")

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
        r"(?:de\s+)?(?:factura|fra\.?|fact?\.)?"
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
# Cuándo una línea es una etiqueta y no un nombre, por su forma y no por una lista cada vez más larga:
#   - una URL o un correo nunca es un nombre;
#   - las frases de varias palabras («forma de pago», «registro mercantil») y una etiqueta seguida de «:» tampoco;
#   - las palabras de etiqueta cuentan con sus derivados («fechas», «facturación», «números», «importes»), salvo
#     que la línea lleve forma jurídica: «Asesoría Números Claros S.L.» o «Facturas y Servicios S.L.» son empresas.
# Las palabras cortas («cif», «nif», «iban»…) solo cuentan enteras: «Pacífico» o «Uniformes» no son etiquetas.
URL_OR_EMAIL = re.compile(r"://|\bwww\.|@")
BARE_DOMAIN = re.compile(r"\b[a-z0-9-]+\.(?:es|com|net|org|eu|cat|info)\b")  # «empresa.es»; «Ejemplo.com S.L.» sí es nombre
LEGAL_FORM = re.compile(
    r"\b(?:s\.?\s?l\.?(?:\s?u\.?)?|s\.?\s?a\.?(?:\s?u\.?)?|s\.?\s?c\.?(?:\s?p\.?)?|s\.?\s?coop\.?|c\.?\s?b\.?"
    r"|sociedad\s+(?:limitada|anonima|cooperativa)|slu|sau|slp)(?=\W|$)"
)
NAME_REJECT_PHRASES = tuple(word for word in NAME_REJECT_WORDS if " " in word or not word[-1].isalnum() or "-" in word)
NAME_REJECT_STEMS = tuple(word for word in NAME_REJECT_WORDS if word not in NAME_REJECT_PHRASES)
LABEL_WITH_COLON = re.compile(r"\b(?:" + "|".join(re.escape(word) for word in NAME_REJECT_STEMS) + r")\w*\s*:")
LABEL_WORD = re.compile(
    r"\b(?:" + "|".join(re.escape(word) + (r"\w*" if len(word) >= 5 else r"\b") for word in NAME_REJECT_STEMS) + r")"
)


def label_like(normalized: str) -> bool:
    """La línea (ya normalizada) es una URL, un correo, una etiqueta o un campo, no el nombre de una empresa."""
    if URL_OR_EMAIL.search(normalized) or (BARE_DOMAIN.search(normalized) and not LEGAL_FORM.search(normalized)):
        return True
    if any(phrase in normalized for phrase in NAME_REJECT_PHRASES) or LABEL_WITH_COLON.search(normalized):
        return True
    return bool(LABEL_WORD.search(normalized)) and not LEGAL_FORM.search(normalized)


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

    # «05-mar-26»: mes abreviado en castellano
    short = re.fullmatch(r"(\d{1,2})[/-]([A-Za-z]{3})[/-](\d{2}|\d{4})", value)
    if short and short.group(2).lower() in SHORT_MONTHS:
        value = f"{short.group(1)}-{SHORT_MONTHS[short.group(2).lower()]}-{short.group(3)}"

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

        return clean_text(repair_ocr_tax_ids(text))

    except Exception:
        return ""

def repair_ocr_tax_ids(text: str) -> str:
    """«BO0100016»: en un CIF leído por OCR, la O es un 0 y la I/l un 1 (con al menos cinco cifras de verdad)."""
    def fix(match: re.Match[str]) -> str:
        body = match.group(2)
        if sum(character.isdigit() for character in body) < 5:
            return match.group(0)
        return match.group(1) + body[:7].translate(str.maketrans("OoIl", "0011")) + body[7]

    return re.sub(r"(?<![A-Za-z0-9])([ABCDEFGHJNPQRSUVW])[\s.\-]?([0-9OoIl]{7}[0-9A-J])(?![A-Za-z0-9])", fix, text)


def ocr_identity_lines(path: Path, page_number: int, legal_form: str) -> list[str]:
    """Emisor que solo está en imágenes (logo, pie, margen vertical): OCR de la página y solo las líneas de identidad.

    Se lee derecho y girado (el CIF del margen suele ir en vertical). Solo se devuelven líneas con
    NIF/CIF, registro mercantil o forma jurídica: los importes siguen saliendo de la capa de texto.
    """
    if pytesseract is None or Image is None or fitz is None or path.suffix.lower() != ".pdf":
        return []
    identity = re.compile(r"(?i)registro\s+mercantil|\bc\.?\s?i\.?\s?f\b|\bn\.?\s?i\.?\s?f\b|" + legal_form + r"(?=\W|$)")
    lines: list[str] = []
    try:
        with fitz.open(path) as document:
            page = document[page_number - 1]
            image = Image.open(io.BytesIO(page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False).tobytes("png")))
            for rotation, psm in ((0, "6"), (270, "11"), (90, "11")):
                text = pytesseract.image_to_string(image.rotate(rotation, expand=True), lang="spa+eng", config=f"--psm {psm}")
                found: list[str] = []
                for line in text.splitlines():
                    line = re.sub(r"\s+", " ", line).strip()
                    if len(line) < 6 or not identity.search(line):
                        continue
                    line = repair_ocr_tax_ids(line)
                    if not TAX_ID_PATTERN.search(line):
                        # «PREVENCIÓN DEMO, S.L. C/ Inventada 1…»: el nombre acaba en la forma jurídica
                        named = re.match(r"(.*?" + legal_form + r")(?=\W|$)", line, re.IGNORECASE)
                        line = named.group(1) if named else line
                        # Restos del logo delante del nombre («P ÓN PREVENCIÓN DEMO…»): fragmentos de una o dos letras
                        words = line.split(" ")
                        while len(words) > 3 and len(words[0]) <= 2:
                            words.pop(0)
                        line = " ".join(words)
                    found.append(line)
                # Girada, la página solo vale si se lee un NIF (el texto del revés sale como ruido)
                from app.extraction_rules import valid_tax_ids

                if rotation and not valid_tax_ids("\n".join(found)):
                    continue
                lines.extend(line for line in found if line not in lines)
    except Exception:
        return []
    return lines


class UnreadableDocument(ValueError):
    """El archivo no se puede abrir (PDF dañado o truncado): reintentar no sirve, hace falta otra copia."""


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

    from app.office_text import OFFICE_EXTENSIONS
    from app.office_text import office_text

    if suffix in OFFICE_EXTENSIONS:
        # Word, RTF, HTML o XML: el texto basta para clasificarlo; no hay páginas que pasar por OCR.
        text = clean_text(office_text(path))
        return text, 1, False, [text]

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

    # «01/02/2026 B-00.900.100»: una fecha seguida de otra cosa no es un número de factura.
    if DATE_PATTERN.match(candidate) and " " in candidate:
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
    # «Razón social» es una etiqueta genérica: vale para las dos partes. Si
    # más arriba, dentro del mismo bloque, hay una cabecera («Cliente»,
    # «Emisor»…), manda la cabecera.
    def generic(text: str) -> bool:
        return normalize_search_text(text).strip(" :|-").startswith("razon social")

    weak: str | None = None
    same_line_role = label_role_in_text(text_before_match)

    if same_line_role and not generic(text_before_match):
        return same_line_role
    weak = same_line_role

    for offset in range(1, 5):
        previous_index = line_index - offset

        if previous_index < 0:
            break

        previous_line = lines[previous_index]

        # Una línea anterior con su propio NIF cierra el bloque:
        # su etiqueta pertenece a ese otro NIF.
        if TAX_ID_PATTERN.search(previous_line):
            break

        role = label_role_in_text(previous_line)

        if role and not generic(previous_line):
            return role
        weak = weak or role

    return weak


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


# Confianza del sentido cuando solo lo decide qué NIF aparece primero.
DIRECTION_BY_POSITION = 80


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
        return "ISSUED", DIRECTION_BY_POSITION

    return "RECEIVED", DIRECTION_BY_POSITION


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


STREET_PREFIX = re.compile(
    r"(?i)^\s*(?:c/|c\.|calle|avda?\.?|avenida|av\.|plaza|pza\.?|pl\.|paseo|p[º°]|ctra\.?|crta\.?|cra\.?|carretera|camino|ronda|traves[ií]a"
    r"|pol\.?\s*ind|pol[ií]gono|urb\.?|urbanizaci[oó]n|glorieta|rambla|v[ií]a|apartado|apdo\.?|cl\.?)(?!\w)"
)
POSTAL_CODE = re.compile(r"\b(?:0[1-9]|[1-4]\d|5[0-2])\d{3}\b\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]")


def looks_like_address(line: str) -> bool:
    """«C/ Inventada 12, 47001 Valladolid» es un domicilio, no el nombre de una empresa."""
    cleaned = line.strip(" :-|·,")
    if any(suffix in normalize_search_text(cleaned) for suffix in LEGAL_SUFFIXES):
        return False  # «Plaza Mayor S.L.» sí es una empresa
    if STREET_PREFIX.match(cleaned) and any(character.isdigit() for character in cleaned):
        return True
    return bool(POSTAL_CODE.search(cleaned))


def looks_like_company_name(line: str) -> bool:
    cleaned = line.strip(" :-|·")
    normalized = normalize_search_text(cleaned)

    if len(cleaned) < 3 or len(cleaned) > 140:
        return False

    if looks_like_address(cleaned):
        return False

    if label_like(normalized):
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
        r"|\bregistro mercantil\b"
        r"|\s+N[º°]\s*(?:de\s*)?factura\b",
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
        " \t:-|,;·"
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

    from app import extraction_rules

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

        # Dos columnas (emisor | cliente): «NIF: A… NIF: B…». La columna del NIF dice qué mitad es nuestra.
        distinct_ids = list(dict.fromkeys(item for item in line_tax_ids if item))
        column = distinct_ids.index(normalized_tax_id) if len(distinct_ids) == 2 else None

        # La propia línea es prioritaria porque existen formatos:
        # EMPRESA S.L. | C.I.F.: B12345678
        same_line_candidate = clean_company_name_candidate(
            line,
            tax_id,
        )

        if looks_like_company_name(
            same_line_candidate
        ) and not extraction_rules.not_a_name(same_line_candidate):
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

            # «CLIENTE S.L.» con su CIF justo debajo también es de la otra parte
            if candidate_index + 1 < len(lines) and candidate_index + 1 != index and {
                normalize_tax_id(match.group(0)) for match in TAX_ID_PATTERN.finditer(lines[candidate_index + 1])
            } - {normalized_tax_id}:
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

            if column is not None:
                # «EMISOR S.A. CLIENTE S.L.P.»: se parte por la forma jurídica y se queda la mitad de su columna
                halves = [part.strip(" ,;") for part in re.findall(r".+?" + extraction_rules.LEGAL_FORM + r"(?=\s|$)", candidate)]
                if len(halves) == 2:
                    candidate = halves[column]

            if not looks_like_company_name(candidate) or extraction_rules.not_a_name(candidate):
                continue

            # Una fila con importes («SERVICIO MENSUAL 1 4,50») es una línea de detalle, no un nombre
            if extraction_rules.AMOUNT_TOKEN.search(candidate):
                continue

            # «MADRID BURGOS»: solo nombres de ciudad (provincias de dos columnas) no es una empresa
            if all(word in extraction_rules.CITY_WORDS for word in re.findall(r"[a-z]+", normalize_search_text(candidate))):
                continue

            if extraction_rules.profession_only(candidate):
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
        # La lectura reconstruida del texto girado junta palabras de toda la columna: no da el número
        if line.startswith("[texto girado]"):
            continue

        normalized_line = normalize_search_text(line)

        has_invoice_label = (
            bool(re.search(r"\bfacturas?\b", normalized_line))  # «facturados» no es una etiqueta
            or bool(
                re.search(
                    r"\bfra\.?\b|\bn\s*[º°o.]\s*fact?\b\.?",
                    normalized_line,
                )
            )
        )

        if not has_invoice_label:
            continue

        # «TOTAL FACTURA», «Importe factura», «Base factura»: lo que sigue es un importe, no el número.
        if re.search(r"total|importe|base|cuota|iva", normalized_line) and not re.search(r"\bn\s*[º°o.]|numero|num\.", normalized_line):
            continue

        # Etiqueta y número en la misma línea («Fáctura: 7-26»: la tilde no cambia la etiqueta).
        plain_line = line.translate(PLAIN_VOWELS)
        for pattern in INVOICE_NUMBER_PATTERNS:
            match = pattern.search(plain_line)

            if not match:
                continue

            # «FECHA FACTURA: 01/02/2026»: la etiqueta es de la fecha, no del número.
            if re.search(r"fecha\s*(?:de\s*(?:la\s*)?)?\W*$", normalize_search_text(plain_line[:match.start()])):
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

        # «Factura:» y el número partido en varias líneas cortas («A», «-01», «-26»)
        if re.fullmatch(r"(?:n[º°o.]?\s*(?:de\s*)?)?factura\s*:?", normalized_line):
            pieces = []
            for piece in lines[line_index + 1:line_index + 6]:
                if not re.fullmatch(r"[A-Z0-9/\-]{1,4}", piece.strip(), re.IGNORECASE):
                    break
                pieces.append(piece.strip())
            joined = "".join(pieces)
            if len(pieces) >= 2 and re.search(r"\d", joined) and not DATE_PATTERN.fullmatch(joined):
                candidates.append((95, joined, f"{line} | {' '.join(pieces)}"))

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

            # «C.C.: 0005555», «Cliente 45», «Nº pedido 4500…»: es otro código, no el de la factura
            if re.match(r"(?:c\.?\s?c\.?|cod(?:igo)?\.?|cliente|cuenta|tel(?:efono|f)?\.?|[cn]\.?\s?i\.?\s?f\.?)(?:\s|:|$)", normalized_candidate) or (
                re.search(r"\b(?:pedido|albaran|presupuesto)\b", normalized_candidate) and "factura" not in normalized_candidate
            ):
                continue

            # Un importe («550,55 €») no es un número de factura.
            if re.fullmatch(r"[-+(]?\s*[\d.\s]+,\d{2}\s*(?:€|eur(?:os)?)?\)?", normalized_candidate):
                continue

            compact_candidate = re.sub(
                r"\s+",
                "",
                candidate_line,
            )

            if standalone_number_pattern.fullmatch(
                compact_candidate
            ) and not is_valid_spanish_tax_id(normalize_tax_id(compact_candidate)):
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

                # Un NIF suelto bajo «FACTURA» es de una de las partes
                if is_valid_spanish_tax_id(normalize_tax_id(value)):
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


OTHER_DOCUMENT_DATE = re.compile(r"albaran|\bpedido\b|presupuesto")


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

        # «NºALBARÁN: … FECHA: 02/03»: la fecha del albarán (o del pedido) que cita la factura no es la suya.
        if OTHER_DOCUMENT_DATE.search(normalized_line) and "factura" not in normalized_line:
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

            if offset > 0 and OTHER_DOCUMENT_DATE.search(normalize_search_text(candidate_line)) and "factura" not in normalize_search_text(candidate_line):
                continue

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

    # Un presupuesto, un albarán, una proforma o un pedido llevan IVA y total, pero no son facturas:
    # si la cabecera lleva ese título y nada la identifica como factura, no se registra como gasto.
    kind = non_invoice_title(text)
    if kind:
        signals.append(f"not_invoice:{kind}")
        return False, min(score, 30), signals

    return score >= 45, score, signals


# Títulos de documentos que llevan IVA y total pero no son facturas. Se reconocen por la FORMA de la línea:
# la palabra encabeza la línea y la siguen, como mucho, unas pocas palabras, su número, una fecha o un paréntesis
# («ALBARÁN Nº AE-55120», «PRESUPUESTO DE REFORMA Nº 7», «PRESUPUESTO Nº P-1 (no es una factura)»). No son
# títulos: un campo («Albarán: 4471»), una cabecera de tabla («Albarán  Fecha  Importe») ni una referencia que no
# encabeza la línea («Ref. presupuesto 88»). El título de factura (más abajo) es más estricto a propósito.
TITLE_TAIL = (
    r"(?:\s+(?:n\.?[o°º]\.?|num(?:ero)?\.?|#)\s*:?)?"                 # «Nº», «Núm.», «#»
    r"(?:\s*(?=[\w/.\-]*\d)[\w/.\-]+)?"                                # el número, con al menos una cifra
    r"(?:\s+(?:de\s+)?(?:fecha\s*:?\s*)?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})?"  # una fecha
    r"(?:\s*\([^)]*\))?\s*$"                                           # «(no es una factura)»
)
# Un título de no-factura admite algunas palabras más («PRESUPUESTO DE REFORMA Nº 7», «ALBARÁN DE SALIDA Nº 123»,
# «PRESUPUESTO Nº 88 ACEPTADO»), pero SOLO si lleva la marca de número («Nº», «Núm.», «#») con su identificador:
# sin ella, «Albarán pendiente de firmar» o «Pedido urgente» son frases, no títulos. Y nunca palabras de columna:
# «Albarán  Fecha  Importe» es una cabecera de tabla.
COLUMN_WORDS = (r"fecha|importe|cantidad|cant|descripcion|concepto|precio|total|unidades|uds|ud|referencia|ref|base|iva|dto"
                r"|descuento|cliente|proveedor|codigo|articulo|factura|subtotal|neto|bruto|euros")
TITLE_WORD = rf"(?!(?:{COLUMN_WORDS})\b)[a-z]+"
NUMBERED_TITLE_TAIL = (
    rf"(?:\s+{TITLE_WORD}){{0,3}}"                                   # «de reforma», «de salida»
    r"\s+(?:n\.?[o°º]\.?|num(?:ero)?\.?|#)\s*:?"                    # la marca de número es obligatoria
    r"\s*(?=[\w/.\-]*\d)[\w/.\-]+"                                  # y su identificador, con al menos una cifra
    rf"(?:\s+{TITLE_WORD}){{0,2}}"                                   # «aceptado»
    r"(?:\s+(?:de\s+)?(?:fecha\s*:?\s*)?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})?"
    r"(?:\s*\([^)]*\))?\s*$"
)
# «OFERTA DE HONORARIOS 12/24»: sin la marca de número, unas pocas palabras y un identificador con separador
# («12/24», «P-7») también hacen título. Una fecha completa no es un identificador: «Pedido realizado el 01/09/2026»
# es una frase.
SEPARATED_TITLE_TAIL = (
    rf"(?:\s+{TITLE_WORD}){{0,3}}"
    r"\s+(?!\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b)(?=[\w.\-]*\d)[\w.\-]*\d[\w.\-]*[/\-][\w/.\-]*"
    r"(?:\s*\([^)]*\))?\s*$"
)
NON_INVOICE_HEADS = (
    ("proforma", r"(?:factura\s+)?pro\s*-?\s*forma"),
    ("presupuesto", r"presupuesto|oferta(?:\s+comercial)?|cotizacion"),
    ("albarán", r"albaran(?:\s+(?:de\s+entrega|valorado))?|nota\s+de\s+entrega"),
    ("pedido", r"(?:(?:nota|orden|hoja)\s+de\s+)?pedido|orden\s+de\s+compra"),
)
NON_INVOICE_TITLES = tuple(
    (kind, re.compile(rf"^\s*(?:{head})(?:{NUMBERED_TITLE_TAIL}|{TITLE_TAIL}|{SEPARATED_TITLE_TAIL})"))
    for kind, head in NON_INVOICE_HEADS
)
# El número propio del documento como campo, en cualquier punto de la línea (las columnas se juntan al leer el
# PDF): «Albarán: 26-14», «Nº albarán AE-5», «… S.L. Pedido nº 4500012345». Con «:» o con la marca de número, y
# siempre con un identificador que lleva cifras: «Albarán  Fecha  Importe» o «Ref. presupuesto 88» no lo son.
# Es una pista más débil que un título: solo cuenta si el documento no habla de facturar en ninguna parte. Las facturas
# reales citan «Nº de pedido: …» o «Albarán: …» junto a un «Factura: 24-07» o un «Factura de venta …» que no tiene la
# forma estricta de título; un albarán o un pedido que dice «factura» se queda en lo que diga su título.
INVOICING_WORDS = re.compile(r"\bfactur|\bfra\.?\b")
# Lo que solo dice un pedido (orden de compra): sus condiciones de compra, cómo se acepta o qué hay que citar al
# facturarlo. Una factura no da instrucciones para facturarse a sí misma.
PURCHASE_ORDER_SIGNS = re.compile(
    r"condiciones (?:generales )?de compra|purchase conditions|terms (?:and conditions )?of purchase"
    r"|acepta(?:cion|r) (?:del |de este |este |el )?pedido"
    r"|(?:citese|indique|indicar\w*|reflej\w+)[^\n]{0,40}(?:pedido|order)[^\n]{0,60}(?:factura|\bfra\b|albaran|invoice)"
)
# Una oferta en forma de carta no lleva título: habla de ofertar o presupuestar varias veces.
OFFER_WORDS = re.compile(r"\bofert\w*|\bpresupuest\w*")
NUMBER_MARK = r"(?:n\.?\s?[o°º]\.?|num(?:ero)?\.?)"
FIELD_ID = r"(?=[\w/.\-]*\d)[\w/.\-]+"
NON_INVOICE_FIELDS = tuple(
    (kind, re.compile(
        rf"(?:^|\s)(?:{head})\s*(?::|{NUMBER_MARK}\s*:?)\s*{FIELD_ID}"
        rf"|(?:^|\s){NUMBER_MARK}\s*(?:de(?:l)?\s+)?(?:{head})\s*:?\s+{FIELD_ID}"
    ))
    for kind, head in NON_INVOICE_HEADS if kind != "proforma"
)
INVOICE_TITLE = re.compile(
    r"^\s*(?:albaran\s*-\s*)?factura(?:\s*-\s*albaran|\s+(?:simplificada|rectificativa|completa|original|duplicado|copia"
    rf"|recapitulativa|de\s+(?:venta|servicios|compra|abono)))*{TITLE_TAIL}"
)
# «Nº de factura: F-12», «Número factura 77», «Factura nº 12»: el documento se identifica como factura.
INVOICE_NUMBER_FIELD = re.compile(r"\b(?:n\.?[o°º]\.?|num(?:ero)?\.?)\s*(?:de\s+)?factura\b|\bfactura\s+n\.?[o°º]")
SPACED_LETTERS = re.compile(r"\b(?:[a-z] ){3,}[a-z]\b")


def title_text(line: str) -> str:
    """Minúsculas, sin tildes y con las letras espaciadas juntas («F A C T U R A» → «factura»)."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", line.lower()) if not unicodedata.combining(char))
    return SPACED_LETTERS.sub(lambda match: match.group(0).replace(" ", ""), folded)


# La nómina se reconoce por la estructura del recibo oficial de salarios (Orden ESS/2098/2014), no por la palabra
# «nómina» (la gestoría que factura «confección de nóminas» emite una factura). El encabezado de las bases de
# cotización es obligatorio y sobrevive a un escaneo malo; si no está, hacen falta tres apartados del recibo.
PAYSLIP_HEADING = re.compile(r"determinacion de (?:las )?bases de cotizacion")
PAYSLIP_PARTS = (
    r"liquido(?: total)? a percibir",
    r"total devengado|total devengo|\bdevengos\b",
    r"total a deducir|total dedu|\bdeducciones\b",
    r"bases? de cotizacion",
    r"aportacion(?:es)? del trabajador|apor\.?\s*trab",
)


def non_invoice_kind(signals: list[str]) -> str | None:
    """«albaran», «presupuesto», «pedido», «proforma» o «nomina» según la señal not_invoice (sin tildes)."""
    kind = next((signal.split(":", 1)[1] for signal in signals if signal.startswith("not_invoice:")), None)
    return unicodedata.normalize("NFKD", kind).encode("ascii", "ignore").decode() if kind else None


def is_payslip(text: str) -> bool:
    normalized = normalize_search_text(text or "")
    if PAYSLIP_HEADING.search(normalized):
        return True
    return sum(1 for part in PAYSLIP_PARTS if re.search(part, normalized)) >= 3


def non_invoice_title(text: str, head_lines: int = 25) -> str | None:
    """«proforma», «presupuesto», «albarán», «pedido» o «nómina» si el documento lo dice y nada lo identifica como factura.

    Identifica como factura un título de factura («FACTURA Nº 12», «F A C T U R A», «Factura simplificada») o el
    campo de su número («Nº de factura: F-12»), esté donde esté en la cabecera: una factura que lleva
    «Albarán: 4471» o «Pedido nº 45» encima de su título sigue siendo factura. Si no, lo que aparece primero
    dice qué es: un título, o el número propio del documento si el documento no habla de facturar
    («Pedido nº 45» arriba manda entonces sobre un «según oferta 23-45» más abajo)."""
    lines = [title_text(line) for line in (text or "").splitlines() if line.strip()][:head_lines]
    if any(INVOICE_TITLE.match(line) or INVOICE_NUMBER_FIELD.search(line) for line in lines):
        return None
    whole = title_text(text or "")
    if PURCHASE_ORDER_SIGNS.search(whole):
        return "pedido"
    fields = not INVOICING_WORDS.search(whole)
    for line in lines:
        for kind, pattern in NON_INVOICE_TITLES:
            if pattern.match(line):
                return kind
        for kind, pattern in NON_INVOICE_FIELDS if fields else ():
            if pattern.search(line):
                return kind
    if is_payslip(text):
        return "nómina"
    if len(OFFER_WORDS.findall(whole)) >= 3:
        return "presupuesto"
    return None


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

    if path.suffix.lower() == ".pdf" and not page_count:
        # Ni PyMuPDF ni pdfplumber lo abren (o no tiene páginas): no es un escaneado, está dañado.
        raise UnreadableDocument("El PDF está dañado o incompleto y no se puede abrir. Pide al emisor una copia nueva.")

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
    repaired_text = extraction_rules.repair_ocr_labels(primary_text)
    real_world_signals: list[str] = ["ocr_labels_repaired"] if repaired_text != primary_text else []
    primary_text = repaired_text

    def tax_ids_of(text: str) -> list[dict[str, Any]]:
        found = find_tax_id_candidates(text)
        for candidate in found:
            repaired = extraction_rules.repair_tax_id(candidate["value"])
            if repaired != candidate["value"] and is_valid_spanish_tax_id(repaired):
                candidate["value"] = repaired
                candidate["valid_checksum"] = True
                real_world_signals.append("tax_id_repaired")
        return found

    tax_id_candidates = tax_ids_of(primary_text)

    # En el texto solo está nuestro NIF: el emisor puede estar solo en imágenes. Se busca con OCR.
    own_ids = normalized_company_tax_ids(company_tax_id)
    if own_ids and not any(candidate["value"] not in own_ids for candidate in tax_id_candidates):
        image_lines = ocr_identity_lines(path, primary_pages[0] if primary_pages else 1, extraction_rules.LEGAL_FORM)
        if any(value not in own_ids for value in extraction_rules.valid_tax_ids("\n".join(image_lines))):
            primary_text = primary_text + "\n" + "\n".join(f"[imagen] {line}" for line in image_lines)
            tax_id_candidates = tax_ids_of(primary_text)
            real_world_signals.append("issuer_from_image_ocr")

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
            if issuer.name:
                supplier_name = ExtractedField(value=issuer.name, confidence=85, source="legal_footer", evidence=issuer.source)
            elif supplier_name.source == "configured_company_name" or extraction_rules.weak_name(supplier_name.value, company_name):
                # El nombre que había era el nuestro (se creyó emitida): se busca junto al CIF del emisor; si no, vacío
                supplier_name = company_name_near_tax_id(primary_text, issuer.tax_id, "supplier")
        elif direction != "ISSUED" and issuer.name and (
            extraction_rules.weak_name(supplier_name.value, company_name)
            or (issuer.tax_id == supplier_tax_id.value and len(issuer.name) > len(supplier_name.value)
                and extraction_rules.name_overlap(supplier_name.value, issuer.name) >= 0.5)
        ):
            supplier_name = ExtractedField(value=issuer.name, confidence=80, source="legal_footer", evidence=issuer.source)
            real_world_signals.append("supplier_name_from_legal_footer")

    # Lo que la factura dice expresamente («Emisor:», «Destinatario:», «Cliente / Nombre») manda sobre la cercanía al
    # NIF, que a menudo cae en una línea del domicilio. Solo la otra parte: la nuestra ya es la configurada.
    other_role = "customer" if direction == "ISSUED" else "supplier"
    labelled = extraction_rules.labelled_party(primary_text, other_role)
    if labelled and not (company_name and extraction_rules.name_overlap(labelled, company_name) >= 0.6):
        labelled_field = ExtractedField(value=labelled, confidence=90, source=f"{other_role}_name_label", evidence=labelled)
        if other_role == "customer":
            customer_name = labelled_field
        else:
            supplier_name = labelled_field
    elif direction != "ISSUED" and (
        not supplier_name.value
        or (supplier_name.confidence < 85 and not any(suffix in normalize_search_text(supplier_name.value) for suffix in LEGAL_SUFFIXES))
    ):
        # Sin etiqueta y sin un nombre claro junto al NIF: la empresa de la cabecera que no es la nuestra, o el
        # profesional que firma en la primera línea
        fallback = extraction_rules.other_company_in_header(primary_text, company_name)
        if not fallback and not supplier_name.value:
            fallback = extraction_rules.first_line_person(primary_text, company_name)
        if fallback:
            supplier_name = ExtractedField(value=fallback, confidence=70, source="supplier_name_header", evidence=fallback)
            real_world_signals.append("supplier_name_from_header")

    if direction == "ISSUED":
        customer_name.value = extraction_rules.without_own_company(customer_name.value, company_name)
    else:
        supplier_name.value = extraction_rules.without_own_company(supplier_name.value, company_name)

    # Solo nuestro NIF y ningún cliente: no es una emitida nuestra si la pagamos por domiciliación
    # («recibo domiciliado»): el emisor está en imágenes que no se han podido leer. Mejor vacío que nosotros.
    if direction == "ISSUED" and not customer_tax_id.value and re.search(r"domiciliad|domiciliaci", normalize_search_text(primary_text)):
        direction, direction_confidence = "RECEIVED", 70
        customer_tax_id = ExtractedField(value=supplier_tax_id.value, confidence=90, source="company_tax_id", evidence="configured")
        customer_name = ExtractedField(value=company_name, confidence=99, source="configured_company_name") if company_name else customer_name
        supplier_tax_id, supplier_name = ExtractedField(), ExtractedField()
        real_world_signals.append("direction_from_direct_debit")

    invoice_number = find_invoice_number(
        primary_text
    )
    if extraction_rules.suspicious_number(invoice_number.value):
        better = extraction_rules.labeled_number(primary_text) or extraction_rules.number_from_header_row(primary_text)
        if better:
            invoice_number = ExtractedField(value=clean_invoice_number(better) or better, confidence=80, source="table_header_row", evidence=better)
            real_world_signals.append("invoice_number_from_table")
    with_series = extraction_rules.number_with_series(primary_text, invoice_number.value)
    if with_series:
        invoice_number = ExtractedField(value=with_series, confidence=invoice_number.confidence, source="invoice_number_with_series", evidence=with_series)

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

    # Rectificativas y abonos: el signo va aparte; la coherencia fiscal se comprueba con los valores absolutos.
    negative = extraction_rules.negative_total(primary_text)

    def magnitude(field: ExtractedField) -> str | None:
        return decimal_to_string(abs(Decimal(field.value))) if field.value not in (None, "") else None

    # Importes que no cuadran fiscalmente: el solver busca base × IVA = cuota y base + cuota − retención = total.
    vat_misread = extraction_rules.declared_vat_without_tax(primary_text, tax_total.value)
    if vat_misread or not extraction_rules.plausible_amounts(magnitude(subtotal), magnitude(tax_total), magnitude(total), withholding_total.value, surcharge_total.value):
        solved = extraction_rules.solve_amounts(primary_text)
        # Si los importes ya cuadraban y solo faltaba la cuota, el solver debe respetar el total leído
        if solved and vat_misread and total.value not in (None, "") and abs(solved["total"] - abs(Decimal(total.value))) > Decimal("0.02"):
            solved = None
        if solved:
            subtotal = ExtractedField(value=decimal_to_string(solved["subtotal"]), confidence=85, source="vat_solver", evidence=f"IVA {solved['tax_rate']} %")
            tax_total = ExtractedField(value=decimal_to_string(solved["tax_total"]), confidence=85, source="vat_solver", evidence=f"IVA {solved['tax_rate']} %")
            total = ExtractedField(value=decimal_to_string(solved["total"]), confidence=85, source="vat_solver", evidence="base + cuota − retención")
            withholding_total = ExtractedField(value=decimal_to_string(solved["withholding_total"]) if solved["withholding_total"] else None, confidence=80 if solved["withholding_total"] else 0, source="vat_solver")
            real_world_signals.append("amounts_from_vat_solver")

    if negative:
        for field in (subtotal, tax_total, total):
            if field.value not in (None, "") and Decimal(field.value) > 0:
                field.value = decimal_to_string(-Decimal(field.value))
        real_world_signals.append("credit_note")

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

    # «Emitida» solo porque nuestro NIF aparece antes que el otro (ni etiqueta de emisor/cliente ni pie legal):
    # es la única pista y, si falla, el IVA cae del lado equivocado. Una persona lo confirma: la confianza queda
    # por debajo de la aprobación automática y el motivo queda en las señales.
    if direction == "ISSUED" and direction_confidence <= DIRECTION_BY_POSITION:
        from app.config import settings

        signals.append("direction_by_position")
        overall_confidence = min(overall_confidence, settings.minimum_auto_confidence - 1)

    return {
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        # «invoice», el tipo de no-factura que dice el propio documento («albaran», «nomina»…) o «unknown»
        "document_type": "invoice" if is_invoice else non_invoice_kind(signals) or "unknown",
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