"""
Genera réplicas anonimizadas de maquetas de facturas reales.

Misma estructura que facturas de proveedores reales (tabla de totales,
texto girado en el margen, ticket de tienda, autónomo en dos páginas, CIF
solo en el pie), pero con empresas, NIF, cuentas e importes inventados. Así
la regresión puede vivir en un repositorio público.

    python evaluation/datasets/sinteticas/generar.py
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
COMPANY = {"name": "Estudio Ejemplo Arquitectos S.L.P.", "tax_id": "B49123458"}
WIDTH, HEIGHT = A4


def lines(pdf: canvas.Canvas, x: float, y: float, items: list[str], size: int = 9, leading: float = 12) -> float:
    pdf.setFont("Helvetica", size)
    for item in items:
        pdf.drawString(x, y, item)
        y -= leading
    return y


def rotated(pdf: canvas.Canvas, x: float, y: float, text: str, size: int = 6) -> None:
    """Texto girado 90° en el margen (como el registro mercantil de muchas facturas)."""
    pdf.saveState()
    pdf.translate(x, y)
    pdf.rotate(90)
    pdf.setFont("Helvetica", size)
    pdf.drawString(0, 0, text)
    pdf.restoreState()


def limpieza(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    lines(pdf, 40, 800, ["QR tributario:", "LIMPIEZAS DEMO SL", "B48111116", "CL. EJEMPLO, 10", "09001 BURGOS", "VERI*FACTU"])
    lines(pdf, 320, 740, ["DIRECCIÓN FISCAL", "ESTUDIO EJEMPLO ARQUITECTOS S.L.P.", "CL. FICTICIA 1 BAJO", "09003 BURGOS"])
    rotated(pdf, 20, 300, "Inscrito Registro Mercantil de La Rioja, Tomo 100, Folio 20, Hoja LO-1234, CIF: B48111116")
    y = lines(pdf, 40, 640, ["FACTURA Fecha Contable 15/09/2026", "Nº factura 2609001 Fecha Exped. 15/09/2026", "Cliente 777 CIF/NIF: B49123458",
                              "Artículo Descripción Artículo Unidades Precio Importes", "1 LIMPIEZA DE MANTENIMIENTO SEMANAL 1,00 212,40 212,40", "PERIODO: MES DE SEPTIEMBRE"])
    y = lines(pdf, 40, y - 20, ["Base imponible IVA 21 % Retención 0 % Total factura (Eur)", "212,40 44,60 0,00 257,00", "Condiciones de pago",
                                 "Forma de pago: Transferencia Vencimientos: 15/09/2026 257,00", "CC: ES00 0000 0000 00 0000000000"])
    lines(pdf, 40, 120, ["LIMPIEZAS DEMO, S.L. es el Responsable del tratamiento de los datos personales proporcionados bajo su consentimiento."], size=6)
    pdf.save()
    return {"direction": "RECEIVED", "supplier_name": "LIMPIEZAS DEMO SL", "supplier_tax_id": "B48111116", "customer_tax_id": "B49123458",
            "invoice_number": "2609001", "invoice_date": "2026-09-15", "due_date": "2026-09-15", "subtotal": "212.40", "tax_total": "44.60", "total": "257.00"}


def geotecnia(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    rotated(pdf, 18, 260, "Inscrita en el Registro Mercantil de Burgos, Tomo 100, Libro 20 de la Sección 8, Folio 9, Hoja BU/1234, Inscripción 1ª - C.I.F.: B-47222229")
    rotated(pdf, 575, 260, "Inscrita en el Registro Mercantil de Burgos, Tomo 100, Libro 20 de la Sección 8, Folio 9, Hoja BU/1234, Inscripción 1ª - C.I.F.: B-47222229")
    y = lines(pdf, 60, 790, ["Nº : 20455-03-26", "FACTURA", "Cliente", "Nombre: ESTUDIO EJEMPLO Arquitectos, S.L.P.", "Dirección: Calle Ficticia nº 1, Bajo Fecha: 12 de Septiembre de 2026",
                              "Ciudad: Burgos C.P.: 09003", "C.I.F.: B-49123458", "Cantidad Descripción Importe",
                              "Importe correspondiente al estudio geotécnico para una nave industrial en la parcela 7.", "1 2.150,00"])
    lines(pdf, 60, y - 30, ["Subtotal ................. 2.150,00", "Detalles de pago", "Ingreso en cuenta nº: I.V.A. (21%) ........... 4 5 1,50", "Banco Demo: ES00 0000 0000 0000 0000 0000", "TOTAL: 2.601,50 €"])
    lines(pdf, 60, 110, ["Le informamos que sus datos personales serán tratados por ESTUDIOS DEL SUELO DEMO, S.L., como Responsable del Tratamiento, con la finalidad de mantener las relaciones."], size=6)
    pdf.save()
    return {"direction": "RECEIVED", "supplier_name": "ESTUDIOS DEL SUELO DEMO, S.L.", "supplier_tax_id": "B47222229", "customer_tax_id": "B49123458",
            "invoice_number": "20455-03-26", "invoice_date": "2026-09-12", "subtotal": "2150.00", "tax_total": "451.50", "total": "2601.50"}


def tienda(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    y = lines(pdf, 40, 800, ["ESTUDIO EJEMPLO ARQUITECTOS, S.L.", "CALLE FICTICIA 1 BAJO", "BURGOS, 09003", "ESPAÑA",
                              "Nº Factura Fecha C.I.F./D.N.I. Código Cliente Representante", "MA26090112 03/09/2026 B49123458 KL0001 MA01",
                              "Código Descripción Part Number Uds. I.V.A. I.V.A. incl. Importe", "CAB001 CABLE USB-C 2M DEMO PN001 1 21 30,00 30,00",
                              "SRV002 CONFIGURACIÓN DE EQUIPO SRV002 1 21 50,00 50,00", "Importe Total EUR 80,00", "Especificación importe IVA",
                              "% IVA Base IVA+RE Importe IVA Total", "21 66,12 13,88 80,00", "Total EUR 66,12 13,88 80,00", "Forma pago:", "Tarjeta 80,00", "Gracias por comprar en Tienda Demo"])
    lines(pdf, 40, 120, ["TIENDA DEMO INFORMATICA S.A.U Inscrita en el Registro Mercantil de Madrid, Tomo: 1, Folio: 2, Hoja: M1, inscripción: 2 CIF: A28333334 C\\ Mayor 1 - 28001 - Madrid"], size=6)
    pdf.save()
    return {"direction": "RECEIVED", "supplier_name": "TIENDA DEMO INFORMATICA S.A.U", "supplier_tax_id": "A28333334", "customer_tax_id": "B49123458",
            "invoice_number": "MA26090112", "invoice_date": "2026-09-03", "subtotal": "66.12", "tax_total": "13.88", "total": "80.00"}


def autonomo(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    header = ["Juan Ejemplo Ruiz", "Estudio Ejemplo Arquitectos S.L.P", "Calle Falsa 12 3 B", "09006 Burgos ( BURGOS ) Estudio Ejemplo Arquitectos S.L.P",
              "Tfno.: 900000000 C/ Ficticia 1, Bajo", "C.I.F./N.I.F.: 71234567-W 9003 Burgos", "BURGOS", "C.I.F./N.I.F.: B49123458",
              "DOCUMENTO NÚMERO PÁGINA FECHA"]
    footer = ["QR Tributario: TIPO IMPORTE BASE I.V.A.", "TOTAL:", "FORMA DE PAGO", "Vencimientos Importe Domiciliación Oficina Número de cuenta"]
    y = lines(pdf, 40, 800, header + ["Factura 1 260777 1 de 2 01/09/2026", "DESCRIPCIÓN CANTIDAD PRECIO UNIDAD SUBTOTAL DTO. TOTAL",
                                       "Albarán: 1-260700 15/08/2026", "Configuración de copias de seguridad en el servidor de la oficina 1,00", "Mano de obra 2,50 60,00 150,00 150,00"])
    lines(pdf, 40, y - 40, footer)
    lines(pdf, 40, 90, ["Podrá ejercer sus derechos dirigiéndose a la dirección del Responsable del Fichero: Juan Ejemplo Ruiz, Calle Falsa 12 3B, 09006-Burgos."], size=6)
    pdf.showPage()
    y = lines(pdf, 40, 800, header + ["Factura 1 260777 2 de 2 01/09/2026", "DESCRIPCIÓN CANTIDAD PRECIO UNIDAD SUBTOTAL DTO. TOTAL",
                                       "Revisión de la red y del cortafuegos", "Mano de obra ESPECIALIZADA 2,50 60,00 150,00 150,00"])
    lines(pdf, 40, y - 40, ["QR Tributario: TIPO IMPORTE BASE I.V.A.", "21,00 % 300,00 € 300,00 € 63,00 €", "TOTAL: 363,00 €", "FORMA DE PAGO GIRO A LA VISTA",
                            "Vencimientos Importe Domiciliación Oficina Número de cuenta", "01/09/2026 363,00 BANCO DEMO, SOCIEDAD ANONIMA"])
    lines(pdf, 40, 90, ["Podrá ejercer sus derechos dirigiéndose a la dirección del Responsable del Fichero: Juan Ejemplo Ruiz, Calle Falsa 12 3B, 09006-Burgos."], size=6)
    pdf.save()
    return {"direction": "RECEIVED", "supplier_name": "Juan Ejemplo Ruiz", "supplier_tax_id": "71234567W", "customer_tax_id": "B49123458",
            "invoice_number": "260777", "invoice_date": "2026-09-01", "due_date": "2026-09-01", "subtotal": "300.00", "tax_total": "63.00", "total": "363.00"}


def ingenieria(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    lines(pdf, 40, 815, ["21,00% 0,00"])
    y = lines(pdf, 40, 790, ["ESTUDIO EJEMPLO ARQUITECTOS SL", "CALLE FICTICIA, 1", "09003 BURGOS", "BURGOS", "Número de Factura Pág. Fecha", "N.I.F.: B49123458",
                              "260612 1 30/09/2026", "Cantidad Concepto Precio Total",
                              "1,00 Redacción de la documentación para el proyecto de climatización 1.500,00 1.500,00",
                              "1,00 Redacción de la documentación para el proyecto de ventilación 500,00 500,00"])
    lines(pdf, 40, y - 30, ["Neto % I.V.A.", "Forma de pago: Transferencia 2.000,00 21 420,00", "Número de Cuenta: ES0000000000000000000000",
                            "Vencimiento: 30/09/2026 2.420,00", "TOTAL 2.420,00 €"])
    lines(pdf, 40, 90, ["INGENIERIA DEMO S.L. Inscrita en el Registro Mercantil de Burgos. CIF B-46444444"], size=7)
    pdf.save()
    return {"direction": "RECEIVED", "supplier_name": "INGENIERIA DEMO S.L.", "supplier_tax_id": "B46444444", "customer_tax_id": "B49123458",
            "invoice_number": "260612", "invoice_date": "2026-09-30", "due_date": "2026-09-30", "subtotal": "2000.00", "tax_total": "420.00", "total": "2420.00"}


CASES = (
    ("limpieza_tabla_totales", limpieza, ["tabla de totales", "texto girado", "Verifactu"]),
    ("geotecnia_texto_girado", geotecnia, ["texto girado", "dígitos espaciados", "proveedor solo en el pie", "fecha en letra"]),
    ("tienda_ticket", tienda, ["ticket de tienda", "CIF del proveedor en el pie"]),
    ("autonomo_dos_paginas", autonomo, ["autónomo con DNI", "dos páginas", "totales en la última página"]),
    ("ingenieria_pie_legal", ingenieria, ["CIF del proveedor en el pie", "cabecera de tabla con valores debajo", "21 420,00 no es 21.420,00"]),
)


def main() -> None:
    cases = []
    for name, builder, tags in CASES:
        expected = builder(HERE / f"{name}.pdf")
        cases.append({"id": name, "file": f"{name}.pdf", "tags": tags, "expected": expected})
    labels = {
        "descripcion": "Réplicas anonimizadas de maquetas de facturas reales (datos inventados). Generadas con generar.py.",
        "empresa": COMPANY,
        "casos": cases,
    }
    (HERE / "labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(cases)} facturas generadas en {HERE}")


if __name__ == "__main__":
    main()
