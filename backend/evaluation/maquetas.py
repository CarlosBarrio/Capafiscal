"""
Maquetas de documentos comerciales sintéticos (SIMULACIÓN — NO OFICIAL) para el corpus de evaluación.

Un mismo `Documento` (emisor, destinatario, título, campos, líneas, totales, notas) se pinta con distintos
estilos visuales para que el corpus no sea una sola plantilla repetida:

    A  corporativo: logotipo y emisor a la izquierda, título a la derecha, tabla con cabecera oscura
    B  cabecera a la derecha, colores suaves, datos fiscales en bloques
    C  minimalista: letra pequeña, sin rejilla, tabla compacta
    D  tipo ERP: letra monoespaciada, casillas con códigos, todo en mayúsculas
    E  antiguo / menos limpio: Times, título centrado subrayado, tonos grises
    F  bilingüe español / inglés
    G  escaneado: cualquiera de los anteriores convertido en imagen (ver `degradar`)

Las tablas largas continúan en páginas siguientes con la cabecera repetida; el emisor completo solo aparece en
la primera página y los totales solo al final, como en los documentos reales.

Todo es ficticio: empresas, NIF (prefijo B00) e IBAN (entidad 0000). Cada página lleva la marca de simulación.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph
from reportlab.platypus import SimpleDocTemplate
from reportlab.platypus import Spacer
from reportlab.platypus import Table
from reportlab.platypus import TableStyle

from evaluation.documentos import MARK

ESTILOS = {
    "A": "corporativo (cabecera izquierda, tabla central)",
    "B": "cabecera derecha, colores suaves, datos fiscales en bloques",
    "C": "minimalista, tabla compacta",
    "D": "tipo ERP (monoespaciado, casillas)",
    "E": "antiguo / menos limpio",
    "F": "bilingüe español / inglés",
    "G": "escaneado (imagen sin texto)",
}

ENGLISH = {
    "Fecha": "Date", "Cliente": "Customer", "Proveedor": "Supplier", "Cantidad": "Qty", "Concepto": "Description",
    "Descripción": "Description", "Precio": "Unit price", "Importe": "Amount", "Referencia": "Ref.", "Base imponible": "Tax base",
    "TOTAL": "TOTAL", "Observaciones": "Remarks", "Condiciones": "Terms", "Forma de pago": "Payment terms", "Vencimiento": "Due date",
    "Dto.": "Disc.", "Ud.": "Unit", "Entrega prevista": "Expected delivery", "Validez": "Valid until",
}


@dataclass(frozen=True)
class Empresa:
    name: str
    nif: str
    street: str
    city: str
    phone: str
    email: str
    web: str = ""
    sector: str = ""
    color: str = "#1f3a5f"
    shape: str = "circle"  # forma del logotipo ficticio: circle, square, diamond, bars

    @property
    def initials(self) -> str:
        words = [word for word in self.name.replace(",", " ").split() if word[0].isalpha() and word.upper() not in {"S.L.", "S.A.", "S.L.P.", "SL", "SA", "Y", "DE", "LA", "EL"}]
        return "".join(word[0] for word in words[:2]).upper()


@dataclass
class Documento:
    titulo: str                                   # «ALBARÁN DE ENTREGA», «FACTURA», «PRESUPUESTO DE REFORMA»
    numero: str | None
    emisor: Empresa
    destinatario: Empresa | None
    fecha: date
    columnas: list[str]
    anchos: list[float]                           # proporciones de cada columna
    filas: list[list[str]]
    marca_numero: str = "Nº"
    titulo_en: str = ""                           # estilo F: título en inglés
    etiqueta_destinatario: str = "Cliente"
    campos: list[tuple[str, str]] = field(default_factory=list)   # datos de cabecera adicionales
    totales: list[tuple[str, str]] = field(default_factory=list)  # vacío: sin bloque de totales
    notas: list[str] = field(default_factory=list)
    condiciones: list[str] = field(default_factory=list)
    firma: str | None = None                      # «Recibí conforme», «Aceptado por el cliente»
    banco: str | None = None
    antes_titulo: list[str] = field(default_factory=list)  # líneas sueltas encima del título (casos ambiguos)
    sin_titulo: bool = False                      # el número va solo como campo («Nº de factura: F-12»)
    sello: str | None = None                      # «ACEPTADO», «PAGADO»…
    pie: str = ""


def _style(name: str, font: str, size: float, **extra: Any) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=size * 1.25, **extra)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


class Tema:
    """Tipografías y colores de un estilo."""

    def __init__(self, estilo: str, doc: Documento):
        self.estilo = estilo
        mono, serif = estilo == "D", estilo == "E"
        self.font = "Courier" if mono else "Times-Roman" if serif else "Helvetica"
        self.bold = "Courier-Bold" if mono else "Times-Bold" if serif else "Helvetica-Bold"
        size = {"A": 9, "B": 9, "C": 7.6, "D": 8, "E": 9.5, "F": 8.4}.get(estilo, 9)
        self.size = size
        self.accent = colors.HexColor(doc.emisor.color)
        self.soft = colors.HexColor({"B": "#eef3f8", "C": "#ffffff", "E": "#f2f0ea"}.get(estilo, "#f4f6f8"))
        self.ink = colors.HexColor("#3a3a3a") if serif else colors.black
        self.body = _style(f"body{estilo}", self.font, size, textColor=self.ink)
        self.small = _style(f"small{estilo}", self.font, size - 1.6, textColor=colors.HexColor("#555555"))
        self.bold_body = _style(f"bold{estilo}", self.bold, size, textColor=self.ink)
        self.right = _style(f"right{estilo}", self.font, size, alignment=TA_RIGHT, textColor=self.ink)
        title_size = {"A": 20, "B": 15, "C": 12, "D": 13, "E": 16, "F": 15}.get(estilo, 14)
        align = {"A": TA_RIGHT, "B": TA_LEFT, "E": TA_CENTER}.get(estilo, TA_LEFT)
        self.title = _style(f"title{estilo}", self.bold, title_size, alignment=align, textColor=self.accent if estilo in "AB" else self.ink)
        self.company = _style(f"company{estilo}", self.bold, size + 3, textColor=self.accent if estilo in "ABF" else self.ink,
                              alignment=TA_RIGHT if estilo == "B" else TA_LEFT)
        self.company_small = _style(f"companysmall{estilo}", self.font, size - 1, textColor=colors.HexColor("#444444"),
                                    alignment=TA_RIGHT if estilo == "B" else TA_LEFT)

    def label(self, text: str) -> str:
        if self.estilo == "F" and text in ENGLISH:
            return f"{text} / {ENGLISH[text]}"
        return text.upper() if self.estilo == "D" else text


def _logo(canvas, empresa: Empresa, x: float, y: float, size: float = 14 * mm) -> None:
    """Logotipo ficticio: una forma con las iniciales, como IMAGEN (igual que los logotipos reales, no es texto)."""
    from PIL import Image
    from PIL import ImageDraw
    from PIL import ImageFont
    from reportlab.lib.utils import ImageReader

    pixels = 240
    image = Image.new("RGBA", (pixels, pixels), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    color = empresa.color
    if empresa.shape == "square":
        draw.rounded_rectangle((0, 0, pixels - 1, pixels - 1), radius=34, fill=color)
    elif empresa.shape == "diamond":
        draw.polygon([(pixels / 2, 0), (pixels - 1, pixels / 2), (pixels / 2, pixels - 1), (0, pixels / 2)], fill=color)
    elif empresa.shape == "bars":
        for index in range(3):
            height = pixels * (0.5 + index * 0.25)
            draw.rectangle((index * pixels / 3, pixels - height, index * pixels / 3 + pixels / 3.6, pixels - 1), fill=color)
    else:
        draw.ellipse((0, 0, pixels - 1, pixels - 1), fill=color)
    if empresa.shape != "bars":
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 92)
        except OSError:
            font = ImageFont.load_default()
        draw.text((pixels / 2, pixels / 2), empresa.initials, fill="white", font=font, anchor="mm")
    canvas.drawImage(ImageReader(image), x, y, size, size, mask="auto")


def _first_page(doc: Documento, tema: Tema):
    def draw(canvas, _template) -> None:
        width, height = A4
        if tema.estilo in "AF":
            _logo(canvas, doc.emisor, 18 * mm, height - 32 * mm)
        elif tema.estilo == "B":
            _logo(canvas, doc.emisor, width - 32 * mm, height - 32 * mm, 13 * mm)
            canvas.setFillColor(tema.soft)
            canvas.rect(0, height - 8 * mm, width, 8 * mm, stroke=0, fill=1)
        elif tema.estilo == "D":
            canvas.setFont("Courier", 7)
            canvas.drawString(15 * mm, height - 10 * mm, f"SISTEMA DE GESTIÓN v4.2  ·  {doc.emisor.name.upper()}  ·  LISTADO {doc.titulo.split()[0]}")
        _footer(canvas, doc, tema)
    return draw


def _later_pages(doc: Documento, tema: Tema):
    def draw(canvas, template) -> None:
        width, height = A4
        canvas.setFont(tema.font, 7.5)
        reference = f"{doc.titulo.title()} {doc.marca_numero} {doc.numero}" if doc.numero and not doc.sin_titulo else doc.titulo.title()
        canvas.drawString(18 * mm, height - 12 * mm, f"{reference} · continuación")
        _footer(canvas, doc, tema)
    return draw


def _footer(canvas, doc: Documento, tema: Tema) -> None:
    width, _height = A4
    canvas.saveState()
    canvas.setFont(tema.font, 6.5)
    canvas.setFillColor(colors.HexColor("#666666"))
    text = doc.pie or f"{doc.emisor.name} · NIF {doc.emisor.nif} · {doc.emisor.street}, {doc.emisor.city}"
    canvas.drawString(18 * mm, 12 * mm, text[:150])
    canvas.drawRightString(width - 18 * mm, 12 * mm, f"Página {canvas.getPageNumber()}")
    canvas.drawCentredString(width / 2, 7 * mm, MARK)
    canvas.restoreState()


def _issuer_block(doc: Documento, tema: Tema) -> list:
    e = doc.emisor
    lines = [_p(e.name, tema.company), _p(f"NIF {e.nif}", tema.company_small), _p(e.street, tema.company_small),
             _p(e.city, tema.company_small), _p(f"Tel. {e.phone} · {e.email}", tema.company_small)]
    if e.web:
        lines.append(_p(e.web, tema.company_small))
    return lines


def _recipient_block(doc: Documento, tema: Tema) -> list:
    if doc.destinatario is None:
        return []
    r = doc.destinatario
    return [_p(tema.label(doc.etiqueta_destinatario), tema.bold_body), _p(r.name, tema.body), _p(f"NIF {r.nif}", tema.body),
            _p(r.street, tema.body), _p(r.city, tema.body)]


def _title_flowables(doc: Documento, tema: Tema) -> list:
    out = [_p(line, tema.body) for line in doc.antes_titulo]
    if doc.sin_titulo:
        return out
    title = doc.titulo if not doc.numero else f"{doc.titulo} {doc.marca_numero} {doc.numero}"
    title = title.upper() if tema.estilo in "ADE" else title
    # El título va en una línea, como en los documentos reales: se reduce la letra hasta que quepa en su columna.
    usable = A4[0] - 36 * mm
    width = usable * {"A": 0.5, "F": 0.5, "B": 0.55}.get(tema.estilo, 1.0) - 4 * mm
    size = tema.title.fontSize
    while size > 9 and stringWidth(title, tema.title.fontName, size) > width:
        size -= 0.5
    style = ParagraphStyle(f"title_fit_{size}", parent=tema.title, fontSize=size, leading=size * 1.2)
    out.append(_p(title, style))
    if tema.estilo == "F" and doc.titulo_en:
        out.append(_p(doc.titulo_en, ParagraphStyle("title_en", parent=style, fontSize=max(9, size * 0.75), leading=max(9, size * 0.75) * 1.2)))
    return out


def _fields_table(doc: Documento, tema: Tema) -> Table:
    rows = []
    if doc.sin_titulo and doc.numero:
        rows.append((doc.titulo, doc.numero))
    rows.append((tema.label("Fecha"), doc.fecha.strftime("%d/%m/%Y")))
    rows += [(tema.label(label), value) for label, value in doc.campos]
    if tema.estilo == "D":  # casillas en horizontal, como un ERP
        table = Table([[_p(label, tema.small) for label, _ in rows], [_p(value, tema.bold_body) for _, value in rows]])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6"))]))
        return table
    table = Table([[_p(label, tema.bold_body), _p(value, tema.body)] for label, value in rows], colWidths=[42 * mm, 58 * mm])
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("TOPPADDING", (0, 0), (-1, -1), 1.5)]
    if tema.estilo == "B":
        style += [("BACKGROUND", (0, 0), (-1, -1), tema.soft), ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d3df"))]
    table.setStyle(TableStyle(style))
    return table


def _header(doc: Documento, tema: Tema) -> list:
    issuer, recipient = _issuer_block(doc, tema), _recipient_block(doc, tema)
    title, fields = _title_flowables(doc, tema), _fields_table(doc, tema)
    usable = A4[0] - 36 * mm
    if tema.estilo == "B":  # título y datos a la izquierda; emisor a la derecha (bajo el logotipo)
        top = Table([[title + [Spacer(1, 3 * mm), fields], [Spacer(1, 14 * mm)] + issuer]], colWidths=[usable * 0.55, usable * 0.45])
        top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        box = Table([[recipient]], colWidths=[usable * 0.55]) if recipient else Spacer(1, 1)
        if recipient:
            box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), tema.soft), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        return [top, Spacer(1, 5 * mm), box, Spacer(1, 6 * mm)]
    if tema.estilo in "AF":  # emisor a la izquierda (junto al logotipo), título a la derecha
        top = Table([[[Spacer(1, 16 * mm)] + issuer, title + [Spacer(1, 3 * mm), fields]]], colWidths=[usable * 0.5, usable * 0.5])
        top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return [top, Spacer(1, 4 * mm)] + ([_boxed(recipient, tema, usable * 0.6)] if recipient else []) + [Spacer(1, 6 * mm)]
    if tema.estilo == "D":
        return issuer[:2] + [Spacer(1, 2 * mm)] + title + [Spacer(1, 2 * mm), fields, Spacer(1, 3 * mm)] + recipient + [Spacer(1, 4 * mm)]
    if tema.estilo == "E":
        return issuer + [Spacer(1, 6 * mm)] + title + [Spacer(1, 4 * mm)] + recipient + [Spacer(1, 3 * mm), fields, Spacer(1, 5 * mm)]
    # C: minimalista, todo en una columna compacta
    return title + issuer[:1] + [_p(f"{doc.emisor.nif} · {doc.emisor.street}, {doc.emisor.city} · {doc.emisor.email}", tema.small),
                                 Spacer(1, 3 * mm), fields, Spacer(1, 2 * mm)] + recipient + [Spacer(1, 4 * mm)]


def _boxed(flowables: list, tema: Tema, width: float) -> Table:
    table = Table([[flowables]], colWidths=[width])
    table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, tema.accent), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    return table


def _lines_table(doc: Documento, tema: Tema) -> Table:
    usable = A4[0] - 36 * mm
    total = sum(doc.anchos)
    widths = [usable * width / total for width in doc.anchos]
    header_style = _style("hdrwhite", tema.bold, tema.size, textColor=colors.white) if tema.estilo == "A" else tema.bold_body
    header = [_p(tema.label(column), header_style) for column in doc.columnas]
    body = [[_p(cell, tema.right if index > 0 and _numeric(cell) else tema.body) for index, cell in enumerate(row)] for row in doc.filas]
    table = Table([header] + body, colWidths=widths, repeatRows=1)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    if tema.estilo == "A":
        style += [("BACKGROUND", (0, 0), (-1, 0), tema.accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc"))]
    elif tema.estilo == "B":
        style += [("BACKGROUND", (0, 0), (-1, 0), tema.soft)] + [("BACKGROUND", (0, row), (-1, row), colors.HexColor("#f8fafc")) for row in range(2, len(body) + 1, 2)]
    elif tema.estilo == "C":
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black), ("LINEBELOW", (0, -1), (-1, -1), 0.4, colors.black)]
    elif tema.estilo == "D":
        style += [("GRID", (0, 0), (-1, -1), 0.5, colors.black)]
    elif tema.estilo == "E":
        style += [("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#555555")), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#555555"))]
    else:
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.6, tema.accent), ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#dddddd"))]
    table.setStyle(TableStyle(style))
    return table


def _numeric(cell: str) -> bool:
    stripped = cell.replace("€", "").replace(".", "").replace(",", "").replace("%", "").replace("-", "").strip()
    return bool(stripped) and stripped.isdigit()


def _totals(doc: Documento, tema: Tema) -> list:
    if not doc.totales:
        return []
    rows = [[_p(tema.label(label), tema.bold_body if index == len(doc.totales) - 1 else tema.body), _p(value, tema.right)]
            for index, (label, value) in enumerate(doc.totales)]
    table = Table(rows, colWidths=[48 * mm, 32 * mm], hAlign="RIGHT")
    style = [("LINEABOVE", (0, -1), (-1, -1), 0.8, tema.accent if tema.estilo in "ABF" else colors.black)]
    if tema.estilo in "BD":
        style.append(("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")))
    table.setStyle(TableStyle(style))
    return [Spacer(1, 4 * mm), table]


def _closing(doc: Documento, tema: Tema) -> list:
    out: list = []
    if doc.notas:
        out += [Spacer(1, 5 * mm), _p(tema.label("Observaciones"), tema.bold_body)] + [_p(note, tema.body) for note in doc.notas]
    if doc.condiciones:
        out += [Spacer(1, 4 * mm), _p(tema.label("Condiciones"), tema.bold_body)] + [_p(f"– {item}", tema.body) for item in doc.condiciones]
    if doc.banco:
        out += [Spacer(1, 3 * mm), _p(doc.banco, tema.body)]
    if doc.sello:
        stamp = _style("stamp", "Helvetica-Bold", 16, textColor=colors.HexColor("#b03030"), alignment=TA_CENTER)
        box = Table([[_p(doc.sello, stamp)]], colWidths=[60 * mm], hAlign="RIGHT")
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.6, colors.HexColor("#b03030"))]))
        out += [Spacer(1, 6 * mm), box]
    if doc.firma:
        sign = Table([[_p(doc.firma, tema.body), _p("Fecha y firma:", tema.body)], ["", ""]], colWidths=[85 * mm, 85 * mm], rowHeights=[None, 18 * mm])
        sign.setStyle(TableStyle([("BOX", (0, 0), (0, -1), 0.5, colors.grey), ("BOX", (1, 0), (1, -1), 0.5, colors.grey)]))
        out += [Spacer(1, 8 * mm), sign]
    return out


def render(path: Path, doc: Documento, estilo: str) -> Path:
    """Pinta el documento con el estilo A–F. Devuelve la ruta del PDF."""
    tema = Tema(estilo, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    template = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=20 * mm,
                                 title=f"{doc.titulo} {doc.numero or ''} · {MARK}", author=doc.emisor.name, subject=MARK)
    story = _header(doc, tema) + [_lines_table(doc, tema)] + _totals(doc, tema) + _closing(doc, tema)
    template.build(story, onFirstPage=_first_page(doc, tema), onLaterPages=_later_pages(doc, tema))
    return path


# ---------------------------------------------------------------------
# Estilo G: documentos escaneados o fotografiados (imagen sin capa de texto)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Degradacion:
    """Cómo se estropea la imagen. Cada perfil imita una forma real de digitalizar un papel."""
    dpi: int = 150
    rotacion: float = 0.8          # grados
    desenfoque: float = 0.5
    ruido: int = 400               # 1 píxel de ruido cada N
    contraste: float = 1.0         # <1 lavado, >1 duro
    brillo: float = 1.0
    calidad_jpeg: int = 60
    fondo: int = 255               # gris del papel (fotografía: <255)
    sombra: bool = False           # degradado de luz de una foto con móvil


def degradar(pdf_bytes: bytes, perfil: Degradacion, seed: str) -> bytes:
    """Convierte cada página en una imagen con el perfil dado (sin texto seleccionable)."""
    import pymupdf
    from PIL import Image
    from PIL import ImageEnhance
    from PIL import ImageFilter

    rng = random.Random(seed)
    source = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    output = pymupdf.open()
    scale = perfil.dpi / 72
    for page in source:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("L")
        if perfil.fondo < 255:  # papel no blanco: lo blanco pasa a gris
            image = image.point(lambda value: int(value * perfil.fondo / 255))
        image = image.rotate(perfil.rotacion + rng.uniform(-0.15, 0.15), fillcolor=perfil.fondo, expand=False, resample=Image.BICUBIC)
        if perfil.desenfoque:
            image = image.filter(ImageFilter.GaussianBlur(perfil.desenfoque))
        image = ImageEnhance.Contrast(image).enhance(perfil.contraste)
        image = ImageEnhance.Brightness(image).enhance(perfil.brillo)
        if perfil.sombra:
            gradient = Image.linear_gradient("L").resize(image.size).point(lambda value: 255 - value // 4)
            image = Image.composite(image, Image.new("L", image.size, 0), gradient)
        pixels = image.load()
        for _ in range(image.width * image.height // max(perfil.ruido, 1)):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            pixels[x, y] = rng.choice((0, 70, 160, 220))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=perfil.calidad_jpeg)
        target = output.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_image(target.rect, stream=buffer.getvalue())
    output.set_metadata({"title": f"Escaneado · {MARK}", "subject": MARK})
    return output.tobytes()


def eur(value: Decimal) -> str:
    """1234.5 → «1.234,50 €»."""
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def num(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
