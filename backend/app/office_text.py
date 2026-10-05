"""Texto de documentos que no son PDF: Word (.doc, .docx), OpenDocument (.odt), RTF, HTML y XML.

Sin dependencias: basta con el texto para clasificar el documento (factura, albarán, presupuesto, pedido…) y para
que una persona lo encuentre. Lo que no se pueda leer devuelve "" y el documento queda para revisar.
"""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

OFFICE_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf", ".htm", ".html", ".xml"}

BLOCK_TAGS = re.compile(r"(?i)<\s*/?\s*(?:p|div|br|tr|li|h[1-6]|table|section|article|header|footer|w:p|text:p|text:h)\b[^>]*>")


def office_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        data = path.read_bytes()
        if suffix in {".htm", ".html"}:
            return markup_text(decode(data))
        if suffix == ".xml":
            return markup_text(decode(data), every_tag=True)
        if suffix == ".rtf":
            return rtf_text(data)
        if suffix == ".docx":
            return zipped_xml_text(path, "word/document.xml")
        if suffix == ".odt":
            return zipped_xml_text(path, "content.xml")
        if suffix == ".doc":
            return word97_text(data)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return ""
    return ""


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    declared = re.search(rb"(?i)(?:encoding|charset)\s*=\s*[\"']?([\w-]+)", data[:2048])
    for encoding in ([declared.group(1).decode("ascii", "ignore")] if declared else []) + ["utf-8", "cp1252"]:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")


def markup_text(text: str, every_tag: bool = False) -> str:
    """HTML o XML a líneas de texto: los bloques (en XML, todas las etiquetas) separan líneas; el contenido de
    script/style no cuenta."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = BLOCK_TAGS.sub("\n", text)
    text = re.sub(r"<[^>]+>", "\n" if every_tag else " ", text)
    return tidy(html.unescape(text))


def zipped_xml_text(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return markup_text(archive.read(member).decode("utf-8", errors="replace"))


def rtf_text(data: bytes) -> str:
    text = data.decode("latin-1")
    # Grupos que no son texto del documento: tablas de fuentes, colores, estilos, imágenes, metadatos
    for keyword in ("*", "fonttbl", "colortbl", "stylesheet", "info", "pict", "listtable", "listoverridetable", "rsidtbl"):
        text = remove_group(text, "\\" + keyword)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda match: bytes([int(match.group(1), 16)]).decode("cp1252", "replace"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda match: chr(int(match.group(1)) % 65536), text)
    text = re.sub(r"\\(?:par|line|row|sect|page)\b ?", "\n", text)
    text = re.sub(r"\\(?:tab|cell)\b ?", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = re.sub(r"\\([{}\\])", r"\1", text)
    return tidy(text.replace("{", "").replace("}", ""))


def remove_group(text: str, keyword: str) -> str:
    """Quita el grupo «{\\keyword …}» entero, con sus grupos anidados."""
    while True:
        start = text.find("{" + keyword)
        if start < 0:
            return text
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
                depth += 1
            elif text[index] == "}" and text[index - 1] != "\\":
                depth -= 1
                if depth == 0:
                    text = text[:start] + text[index + 1:]
                    break
        else:
            return text[:start]


# Texto legible en un .doc (Word 97-2003): el documento guarda su texto en 8 bits (cp1252) o en UTF-16; se toman los
# tramos legibles largos de la lectura que más texto da. No reproduce el formato, pero sí las palabras y las cifras.
READABLE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñºª€%&@/\\.,;:()\-+*=#'\"¿?¡! \t\r\n]{12,}")


def word97_text(data: bytes) -> str:
    candidates = []
    for encoding in ("utf-16-le", "cp1252"):
        decoded = data.decode(encoding, errors="replace")
        runs = [run for run in READABLE.findall(decoded) if sum(character.isalpha() for character in run) >= 6]
        candidates.append("\n".join(runs))
    best = max(candidates, key=lambda text: sum(character.isalpha() for character in text))
    return tidy(best.replace("\r", "\n"))


def tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
