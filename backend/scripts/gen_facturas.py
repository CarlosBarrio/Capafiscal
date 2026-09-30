"""
Genera PDFs de prueba realistas para CapaFiscal.
Uso:  python scripts/gen_facturas.py
Requiere: pip install reportlab
"""
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path("facturas_prueba")
OUT.mkdir(exist_ok=True)


def factura(path, emisor, cif, direccion, num, fecha, concepto, base, iva_pct, iva_imp, total):
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 40 * mm
    c.setFont("Helvetica-Bold", 20); c.drawString(25*mm, y, emisor)
    c.setFont("Helvetica", 9); y -= 6*mm
    c.drawString(25*mm, y, direccion); y -= 5*mm
    c.drawString(25*mm, y, f"CIF: {cif}")
    c.setFont("Helvetica-Bold", 13); c.drawString(140*mm, h-40*mm, "FACTURA")
    c.setFont("Helvetica", 9)
    c.drawString(140*mm, h-46*mm, f"Nº: {num}")
    c.drawString(140*mm, h-51*mm, f"Fecha: {fecha}")
    y -= 14*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(25*mm, y, "Cliente: PYME EJEMPLO S.L.   CIF: B87654321")
    y -= 12*mm
    c.setFillColorRGB(0.15, 0.2, 0.55); c.rect(25*mm, y-2*mm, 160*mm, 8*mm, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 9)
    c.drawString(28*mm, y, "Descripción"); c.drawString(150*mm, y, "Importe")
    c.setFillColorRGB(0, 0, 0); c.setFont("Helvetica", 9); y -= 10*mm
    c.drawString(28*mm, y, concepto); c.drawString(150*mm, y, f"{base:.2f} EUR".replace(".", ","))
    y -= 16*mm
    c.setFont("Helvetica", 10)
    c.drawString(120*mm, y, "Base imponible:"); c.drawString(160*mm, y, f"{base:.2f} €".replace(".", ",")); y -= 6*mm
    c.drawString(120*mm, y, f"IVA ({iva_pct}%):"); c.drawString(160*mm, y, f"{iva_imp:.2f} €".replace(".", ",")); y -= 6*mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(120*mm, y, "TOTAL:"); c.drawString(160*mm, y, f"{total:.2f} €".replace(".", ","))
    c.showPage(); c.save(); print("OK", path)


def requerimiento_aeat(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 40 * mm
    c.setFont("Helvetica-Bold", 16); c.drawString(25*mm, y, "AGENCIA TRIBUTARIA")
    c.setFont("Helvetica", 10); y -= 8*mm
    c.drawString(25*mm, y, "Requerimiento de documentacion"); y -= 6*mm
    c.drawString(25*mm, y, "Nº: REQ-2026-0088123   Fecha: 28/07/2026"); y -= 6*mm
    c.drawString(25*mm, y, "Expediente sujeto a plazo de respuesta.")
    c.showPage(); c.save(); print("OK", path)


if __name__ == "__main__":
    factura(OUT/"factura_repsol_2026.pdf", "REPSOL COMERCIAL S.A.", "A28047223",
            "C/ Mendez Alvaro 44, 28045 Madrid", "FRA-2026-00912", "15/07/2026",
            "Carburante Diesel e+ - 850 litros", 396.28, 21, 83.22, 479.50)
    factura(OUT/"factura_endesa_2026.pdf", "ENDESA ENERGIA S.A.U.", "A81948077",
            "C/ Ribera del Loira 60, 28042 Madrid", "E26-4471183", "10/07/2026",
            "Suministro electrico periodo 06/2026", 258.60, 21, 54.31, 312.91)
    factura(OUT/"factura_makro_2026.pdf", "MAKRO AUTOSERVICIO MAYORISTA S.A.", "A28647451",
            "Paseo Imperial 40, 28005 Madrid", "M-2026-778120", "12/07/2026",
            "Compra mercaderia - hosteleria", 635.01, 21, 133.35, 768.36)
    factura(OUT/"factura_vodafone_2026.pdf", "VODAFONE ESPANA S.A.U.", "A80907397",
            "Av. America 115, 28042 Madrid", "VF-2026-556231", "08/07/2026",
            "Telefonia movil y fibra empresa 07/2026", 71.90, 21, 15.10, 87.00)
    # Factura con importes descuadrados a propósito (dispara riesgo)
    factura(OUT/"factura_iberdrola_MAL.pdf", "IBERDROLA CLIENTES S.A.U.", "A95758389",
            "Plaza Euskadi 5, 48009 Bilbao", "IB-2026-99001", "20/07/2026",
            "Suministro electrico", 100.00, 21, 21.00, 200.00)
    # Requerimiento AEAT (dispara plazo)
    requerimiento_aeat(OUT/"requerimiento_aeat_2026.pdf")
    print("\nListo. PDFs en:", OUT.resolve())