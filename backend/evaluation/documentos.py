"""
Documentos sintéticos realistas para evaluar CapaFiscal.

Reproducen la ESTRUCTURA de documentos administrativos y comerciales reales
(cabecera institucional, destinatario, expediente, cuerpo, tablas, plazos,
advertencias, pie con código de verificación, paginación) con datos
inventados. Todos llevan la marca «SIMULACIÓN — NO OFICIAL»: no imitan
documentos concretos ni sirven como tales.

Variantes de ruido: texto girado en el margen, varias páginas, tabla
partida entre páginas, dígitos espaciados y escaneo sin texto seleccionable.
"""
from __future__ import annotations

import hashlib
import io
import random
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether
from reportlab.platypus import PageBreak
from reportlab.platypus import Paragraph
from reportlab.platypus import SimpleDocTemplate
from reportlab.platypus import Spacer
from reportlab.platypus import Table
from reportlab.platypus import TableStyle

MARK = "SIMULACIÓN — NO OFICIAL"
STYLES = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=STYLES["Normal"], fontName="Helvetica", fontSize=9, leading=12)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7, leading=9, textColor=colors.HexColor("#444444"))
TITLE = ParagraphStyle("title", parent=BODY, fontName="Helvetica-Bold", fontSize=12, leading=15, spaceAfter=4)
HEAD = ParagraphStyle("head", parent=BODY, fontName="Helvetica-Bold", fontSize=9.5, leading=12, spaceBefore=6)
RIGHT = ParagraphStyle("right", parent=BODY, alignment=TA_RIGHT)

ISSUERS = {
    "AEAT": ("AGENCIA ESTATAL DE ADMINISTRACIÓN TRIBUTARIA", "Delegación Especial de Castilla y León · Dependencia Regional de {area}"),
    "TGSS": ("TESORERÍA GENERAL DE LA SEGURIDAD SOCIAL", "Dirección Provincial de {area}"),
}


def eur(value: Any) -> str:
    text = f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def fecha(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def csv_code(seed: str) -> str:
    """Código de verificación ficticio (no válido en ninguna sede)."""
    digest = hashlib.sha256(seed.encode()).hexdigest().upper()
    return f"SIM-{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:16]}"


@dataclass
class Noise:
    rotated_margin: str | None = None  # texto girado 90° en el margen
    spaced_digits: bool = False  # «8 3 7,69» en los importes del resumen
    scanned: bool = False  # imagen sin texto seleccionable
    page_break_before: list[int] = field(default_factory=list)  # índices de bloque donde saltar de página
    replace: dict[str, str] = field(default_factory=dict)  # errores tipo OCR en el texto («T0TAL», «lVA»)


def _garble(value: Any, replace: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replace.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, (list, tuple)):
        return type(value)(_garble(item, replace) for item in value)
    if isinstance(value, dict):
        return {key: _garble(item, replace) for key, item in value.items()}
    return value


def _decorate(canvas, doc, *, organism: str, area: str, code: str, noise: Noise) -> None:
    canvas.saveState()
    width, height = A4
    name, subtitle = ISSUERS.get(organism, (organism, area))
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(18 * mm, height - 14 * mm, name)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(18 * mm, height - 18 * mm, subtitle.format(area=area))
    canvas.setStrokeColor(colors.HexColor("#999999"))
    canvas.line(18 * mm, height - 20 * mm, width - 18 * mm, height - 20 * mm)
    # Marca de simulación en diagonal y en el pie
    canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.18))
    canvas.setFont("Helvetica-Bold", 34)
    canvas.translate(width / 2, height / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, MARK)
    canvas.restoreState()
    canvas.saveState()
    if noise.rotated_margin:
        canvas.translate(10 * mm, 40 * mm)
        canvas.rotate(90)
        canvas.setFont("Helvetica", 6)
        canvas.drawString(0, 0, noise.rotated_margin)
        canvas.restoreState()
        canvas.saveState()
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.drawString(18 * mm, 12 * mm, f"{MARK} · Código seguro de verificación (ficticio): {code}")
    canvas.drawRightString(width - 18 * mm, 12 * mm, f"Página {doc.page}")
    canvas.restoreState()


def _story_block(block: dict[str, Any]) -> list:
    kind = block["tipo"]
    if kind == "titulo":
        return [Paragraph(block["texto"], TITLE)]
    if kind == "apartado":
        return [Paragraph(block["texto"], HEAD)]
    if kind == "parrafo":
        return [Paragraph(block["texto"], BODY), Spacer(1, 3)]
    if kind == "datos":
        rows = [[Paragraph(f"<b>{key}</b>", BODY), Paragraph(str(value), BODY)] for key, value in block["filas"]]
        table = Table(rows, colWidths=[52 * mm, 118 * mm])
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("TOPPADDING", (0, 0), (-1, -1), 1.5)]))
        return [table, Spacer(1, 4)]
    if kind == "tabla":
        rows = [[Paragraph(f"<b>{cell}</b>", SMALL) for cell in block["cabecera"]]] + [[Paragraph(str(cell), SMALL) for cell in row] for row in block["filas"]]
        widths = block.get("anchos")
        table = Table(rows, colWidths=[w * mm for w in widths] if widths else None, repeatRows=1)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999999")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
        return [table, Spacer(1, 5)]
    if kind == "lista":
        return [Paragraph(f"{item}", BODY) for item in block["elementos"]] + [Spacer(1, 3)]
    if kind == "pequeno":
        return [Paragraph(block["texto"], SMALL), Spacer(1, 2)]
    if kind == "salto":
        return [PageBreak()]
    raise ValueError(kind)


def render_admin(path: Path, *, organism: str, area: str, blocks: list[dict[str, Any]], seed: str, noise: Noise | None = None) -> Path:
    """Documento administrativo de varias páginas con cabecera, pie y marca de simulación."""
    noise = noise or Noise()
    code = csv_code(seed)
    story: list = [Spacer(1, 6)]
    if noise.replace:
        blocks = [_garble(block, noise.replace) for block in blocks]
    for index, block in enumerate(blocks):
        if index in noise.page_break_before:
            story.append(PageBreak())
        story += _story_block(block)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=26 * mm, bottomMargin=20 * mm, title=MARK)
    decorate = lambda canvas, document: _decorate(canvas, document, organism=organism, area=area, code=code, noise=noise)  # noqa: E731
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    data = buffer.getvalue()
    if noise.scanned:
        data = scan(data, seed)
    path.write_bytes(data)
    return path


def render_invoice(path: Path, *, supplier: dict[str, str], customer: dict[str, str], number: str, issued: date, lines: list[tuple[str, Decimal, Decimal]],
                   vat_rate: Decimal = Decimal("21"), irpf_rate: Decimal = Decimal("0"), due: date | None = None, rectifies: str | None = None,
                   pages: int = 1, legal_footer: bool = True, seed: str = "", noise: Noise | None = None, iban: str = "ES00 0000 0000 0000 0000 0000",
                   issued_text: str | None = None, brand: str | None = None, intro: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Factura con resumen fiscal. Devuelve los importes (la verdad para la evaluación)."""
    noise = noise or Noise()
    base = sum((quantity * price for _concept, quantity, price in lines), Decimal("0")).quantize(Decimal("0.01"))
    vat = (base * vat_rate / 100).quantize(Decimal("0.01"))
    irpf = (base * irpf_rate / 100).quantize(Decimal("0.01"))
    total = base + vat - irpf
    title = "FACTURA RECTIFICATIVA" if rectifies else "FACTURA"
    # Con «brand», la cabecera solo lleva la marca comercial: la razón social y el NIF quedan en el pie legal.
    identity = [{"tipo": "titulo", "texto": brand}] if brand else [
        {"tipo": "titulo", "texto": f"{supplier['name']}"},
        {"tipo": "pequeno", "texto": f"{supplier['address']} · NIF {supplier['tax_id']}"},
    ]
    header = identity + [
        {"tipo": "apartado", "texto": title},
        {"tipo": "datos", "filas": [("Nº factura", number), ("Fecha de expedición", issued_text or fecha(issued))] + ([("Rectifica a", rectifies), ("Motivo", "Error en el precio unitario")] if rectifies else [])},
        {"tipo": "apartado", "texto": "Cliente"},
        {"tipo": "datos", "filas": [("Razón social", customer["name"]), ("NIF", customer["tax_id"]), ("Domicilio", customer["address"])]},
    ]
    concept_rows = [[concept, f"{quantity:g}".replace(".", ","), eur(price), eur(quantity * price)] for concept, quantity, price in lines]
    detail = [{"tipo": "tabla", "cabecera": ["Concepto", "Cantidad", "Precio", "Importe"], "filas": concept_rows, "anchos": [95, 20, 27, 28]}]
    vat_text = eur(vat)
    if noise.spaced_digits:
        vat_text = " ".join(vat_text.split(",")[0]) + "," + vat_text.split(",")[1]
    summary_rows = [["Base imponible", eur(base)], [f"IVA {vat_rate:g} %", vat_text]]
    if irpf:
        summary_rows.append([f"Retención IRPF {irpf_rate:g} %", f"-{eur(irpf)}"])
    summary_rows.append(["TOTAL FACTURA", eur(total)])
    summary = [{"tipo": "apartado", "texto": "Resumen"}, {"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "filas": summary_rows, "anchos": [60, 40]}]
    payment = [{"tipo": "parrafo", "texto": f"Forma de pago: transferencia a {iban}" + (f". Vencimiento: {fecha(due)}" if due else "")}]
    footer = [{"tipo": "pequeno", "texto": f"{supplier['name']} Inscrita en el Registro Mercantil de {supplier.get('registry', 'Burgos')}, Tomo 1, Folio 1, Hoja BU-0001. CIF {supplier['tax_id']}. {MARK}."}] if legal_footer else []
    blocks = header + (intro or []) + detail
    if pages >= 2:
        blocks.append({"tipo": "salto"})
    blocks += summary[:1] + [{"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "filas": summary_rows[:2], "anchos": [60, 40]}] if pages >= 3 else summary
    if pages >= 3:
        blocks += [{"tipo": "salto"}, {"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "filas": summary_rows[2:], "anchos": [60, 40]}]
    blocks += payment + footer
    render_admin(path, organism=brand or supplier["name"], area="", blocks=blocks, seed=seed or number, noise=noise)
    return {"subtotal": f"{base:.2f}", "tax_total": f"{vat:.2f}", "withholding_total": f"{irpf:.2f}" if irpf else None, "total": f"{total:.2f}"}


def scan(pdf_bytes: bytes, seed: str) -> bytes:
    """Convierte el PDF en imágenes (sin texto seleccionable), algo torcidas y con ruido."""
    import pymupdf
    from PIL import Image
    from PIL import ImageFilter

    rng = random.Random(seed)
    source = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    output = pymupdf.open()
    for page in source:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("L")
        image = image.rotate(rng.uniform(-1.2, 1.2), fillcolor=255, expand=False).filter(ImageFilter.GaussianBlur(0.6))
        pixels = image.load()
        for _ in range(image.width * image.height // 400):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            pixels[x, y] = rng.choice((0, 90, 200))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=55)
        target = output.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_image(target.rect, stream=buffer.getvalue())
    return output.tobytes()


def write_bank_csv(path: Path, rows: list[tuple[date, str, Decimal]]) -> Path:
    lines = ["Fecha;Concepto;Importe;Saldo"]
    balance = Decimal("25000.00")
    for when, concept, amount in rows:
        balance += amount
        lines.append(f"{fecha(when)};{concept};{str(amount).replace('.', ',')};{str(balance).replace('.', ',')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_eml(path: Path, *, sender: str, to: str, subject: str, body: str, message_id: str, sent: str, attachments: list[Path]) -> Path:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}>"
    message["Date"] = sent
    message["X-Simulacion"] = MARK
    message.set_content(body + f"\n\n-- {MARK}")
    for attachment in attachments:
        message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    path.write_bytes(message.as_bytes())
    return path
