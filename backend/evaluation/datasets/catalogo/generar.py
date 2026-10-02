"""
Catálogo sintético para evaluar la extracción (SIMULACIÓN — NO OFICIAL).

Complementa a `sinteticas/` (réplicas de maquetas reales que actúan como regresión): aquí hay variedad de
CASOS, no de maquetas. Empresas y NIF inventados (prefijo B00, dígito de control válido), IBAN ficticio.

    python evaluation/datasets/catalogo/generar.py        # regenera los PDF y labels.json

Casos (conjunto A, desarrollo: se pueden mirar y ajustar reglas con ellos):

    normal_*          5 facturas recibidas corrientes (IVA 21, 10 y 4 %) y 1 emitida
    irpf_*            2 con retención de IRPF (profesional 15 %, alquiler 19 %)
    rectificativa_*   2 rectificativas con importes negativos
    ambiguo_*         2 que parecen factura y no lo son (presupuesto, albarán)
    formato_*         2 maquetas distintas (marca comercial con NIF solo en el pie; tres páginas)
    ocr_*             2 con OCR imperfecto (errores de reconocimiento en el texto; escaneado sin texto)
    complejo_*        2 de importes complejos (muchas líneas con decimales; miles con separador)
    notificacion_*    2 notificaciones administrativas (no son facturas)
    administrativo_*  2 documentos administrativos (certificado y justificante: no son facturas)

Los casos de fechas y plazos están en `../dehu_sintetico/`, donde se evalúa el cálculo del plazo.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from evaluation.documentos import MARK  # noqa: E402
from evaluation.documentos import Noise  # noqa: E402
from evaluation.documentos import render_admin  # noqa: E402
from evaluation.documentos import render_invoice  # noqa: E402

COMPANY = {"name": "EMPRESA EJEMPLO S.L.", "tax_id": "B00100016", "address": "C/ Mayor 1, 09001 Burgos"}
IBAN = "ES00 0000 0000 0000 0000 0000"


def party(name: str, tax_id: str, address: str = "C/ Inventada 10, 09002 Burgos") -> dict[str, str]:
    return {"name": name, "tax_id": tax_id, "address": address}


SUPPLIERS = {
    "asesoria": party("ASESORÍA NÚMEROS CLAROS S.L.", "B00001008"),
    "papeleria": party("PAPELERÍA LA PLUMA S.L.", "B00001016"),
    "restaurante": party("RESTAURANTE EL FOGÓN S.L.", "B00001024"),
    "fruteria": party("FRUTAS DEL CAMPO S.L.", "B00001032"),
    "imprenta": party("IMPRENTA GUTENBERG S.L.", "B00001040"),
    "arrendador": party("INMUEBLES CASTILLA S.L.", "B00001057"),
    "taller": party("TALLER MECÁNICO RUEDA S.L.", "B00001065"),
    "software": party("SOFTWARE NUBE S.L.", "B00001073"),
    "transportes": party("TRANSPORTES RÁPIDOS S.L.", "B00001081"),
    "electricidad": party("ELECTRICIDAD VOLTIO S.L.", "B00001099"),
    "cliente": party("CLIENTE MODELO S.L.", "B00001107"),
}


def invoice(name: str, supplier: str, number: str, issued: date, lines: list[tuple[str, str, str]], *, vat: str = "21", irpf: str = "0",
            due: date | None = None, rectifies: str | None = None, emitted: bool = False, tags: list[str], **extra) -> dict:
    """Genera la factura y devuelve su caso con la verdad (los importes los calcula el propio generador)."""
    seller = COMPANY if emitted else SUPPLIERS[supplier]
    buyer = SUPPLIERS[supplier] if emitted else COMPANY
    parsed = [(concept, Decimal(quantity), Decimal(price)) for concept, quantity, price in lines]
    amounts = render_invoice(HERE / f"{name}.pdf", supplier=seller, customer=buyer, number=number, issued=issued, lines=parsed,
                             vat_rate=Decimal(vat), irpf_rate=Decimal(irpf), due=due, rectifies=rectifies, seed=name, iban=IBAN, **extra)
    expected = {
        "is_invoice": True,
        "direction": "ISSUED" if emitted else "RECEIVED",
        "supplier_name": seller["name"], "supplier_tax_id": seller["tax_id"], "customer_tax_id": buyer["tax_id"],
        "invoice_number": number, "invoice_date": issued.isoformat(),
        "subtotal": amounts["subtotal"], "tax_total": amounts["tax_total"], "total": amounts["total"],
    }
    if due:
        expected["due_date"] = due.isoformat()
    if amounts["withholding_total"]:
        expected["withholding_total"] = amounts["withholding_total"]
    return {"id": name, "file": f"{name}.pdf", "set": "A", "tags": tags, "expected": expected}


def not_invoice(name: str, *, organism: str, area: str, blocks: list[dict], tags: list[str]) -> dict:
    render_admin(HERE / f"{name}.pdf", organism=organism, area=area, blocks=blocks, seed=name)
    return {"id": name, "file": f"{name}.pdf", "set": "A", "tags": tags, "expected": {"is_invoice": False}}


def cases() -> list[dict]:
    d = date
    out = [
        # --- 5 recibidas corrientes + 1 emitida ---------------------------------------------------------------
        invoice("normal_papeleria", "papeleria", "PL-2026/0457", d(2026, 9, 3), [("Folios A4 (caja)", "4", "18.50"), ("Tóner impresora", "1", "64.90")],
                due=d(2026, 10, 3), tags=["normal", "IVA 21"]),
        invoice("normal_restaurante", "restaurante", "F-88213", d(2026, 9, 12), [("Comida de trabajo (4 personas)", "1", "136.36")], vat="10",
                tags=["normal", "IVA 10"]),
        invoice("normal_fruteria", "fruteria", "2026-0912", d(2026, 9, 12), [("Fruta para oficina", "1", "48.08")], vat="4", tags=["normal", "IVA 4"]),
        invoice("normal_software", "software", "SN-26-1093", d(2026, 9, 1), [("Licencia mensual gestión (5 usuarios)", "5", "29.00")],
                due=d(2026, 9, 15), tags=["normal", "suscripción"]),
        invoice("normal_transportes", "transportes", "TR/2026/3321", d(2026, 9, 18), [("Portes Burgos-Madrid", "2", "185.00"), ("Seguro mercancía", "1", "22.40")],
                tags=["normal", "varias líneas"]),
        invoice("normal_emitida", "cliente", "EE-2026-0031", d(2026, 9, 25), [("Servicios de consultoría septiembre", "1", "2400.00")], emitted=True,
                due=d(2026, 10, 25), tags=["normal", "emitida"]),
        # --- IRPF -----------------------------------------------------------------------------------------------------
        invoice("irpf_asesoria", "asesoria", "A-2026-118", d(2026, 9, 30), [("Asesoría fiscal y contable septiembre", "1", "350.00")], irpf="15",
                tags=["IRPF", "profesional"]),
        invoice("irpf_alquiler", "arrendador", "ALQ-09-2026", d(2026, 9, 1), [("Alquiler local C/ Mayor 1, septiembre", "1", "1200.00")], irpf="19",
                due=d(2026, 9, 5), tags=["IRPF", "alquiler"]),
        # --- rectificativas ------------------------------------------------------------------------------------------
        invoice("rectificativa_precio", "imprenta", "R-2026-004", d(2026, 9, 20), [("Abono por error en el precio unitario", "1", "-45.00")],
                rectifies="IG-2026-0815", tags=["rectificativa", "negativa"]),
        invoice("rectificativa_devolucion", "taller", "RECT-0012", d(2026, 9, 22), [("Devolución pieza no instalada", "2", "-37.50")],
                rectifies="TM-2026-0402", tags=["rectificativa", "devolución"]),
        # --- formatos distintos ----------------------------------------------------------------------------------------
        invoice("formato_marca", "electricidad", "VOL-2026-77812", d(2026, 9, 8), [("Término de potencia", "1", "32.15"), ("Término de energía", "1", "118.42")],
                brand="voltio · energía para tu negocio", due=d(2026, 9, 28), tags=["formato", "NIF solo en el pie", "marca comercial"]),
        invoice("formato_tres_paginas", "imprenta", "IG-2026-0901", d(2026, 9, 9),
                [(f"Tarjetas de visita modelo {index}", "500", "0.04") for index in range(1, 30)], pages=3, tags=["formato", "varias páginas", "resumen partido"]),
        # --- OCR imperfecto --------------------------------------------------------------------------------------------
        invoice("ocr_errores", "taller", "TM-2026-0517", d(2026, 9, 14), [("Cambio de aceite y filtros", "1", "89.00"), ("Neumático 205/55 R16", "2", "74.50")],
                noise=Noise(replace={"TOTAL": "T0TAL", "IVA": "lVA", "Base": "Ba5e"}, spaced_digits=True), tags=["OCR", "errores de reconocimiento"]),
        invoice("ocr_escaneado", "papeleria", "PL-2026/0471", d(2026, 9, 19), [("Archivadores", "10", "3.20")],
                noise=Noise(scanned=True), tags=["OCR", "escaneado", "necesita Tesseract"]),
        # --- importes complejos -----------------------------------------------------------------------------------------
        invoice("complejo_decimales", "fruteria", "2026-0930", d(2026, 9, 30),
                [("Manzana (kg)", "12.5", "1.87"), ("Pera (kg)", "7.25", "2.13"), ("Naranja (kg)", "18.75", "1.29"), ("Cesta regalo", "3", "17.333")], vat="4",
                tags=["importes complejos", "cantidades con decimales"]),
        invoice("complejo_miles", "arrendador", "OBR-2026-002", d(2026, 9, 26), [("Reforma integral local (certificación 2)", "1", "18345.67")],
                due=d(2026, 10, 26), tags=["importes complejos", "miles"]),
    ]
    # --- no son facturas: ambiguos, notificaciones y administrativos -----------------------------------------------
    supplier = SUPPLIERS["imprenta"]
    out += [
        not_invoice("ambiguo_presupuesto", organism=supplier["name"], area="", tags=["ambiguo", "presupuesto"], blocks=[
            {"tipo": "titulo", "texto": supplier["name"]}, {"tipo": "pequeno", "texto": f"NIF {supplier['tax_id']}"},
            {"tipo": "apartado", "texto": "PRESUPUESTO Nº P-2026-114 (no es una factura)"},
            {"tipo": "datos", "filas": [("Cliente", COMPANY["name"]), ("NIF", COMPANY["tax_id"]), ("Validez", "30 días")]},
            {"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "filas": [["Catálogos 1.000 uds.", "640,00 €"], ["IVA 21 %", "134,40 €"], ["Total presupuestado", "774,40 €"]], "anchos": [60, 40]},
            {"tipo": "parrafo", "texto": "Para aceptar el presupuesto, devuélvalo firmado. La factura se emitirá con la entrega."}]),
        not_invoice("ambiguo_albaran", organism=SUPPLIERS["transportes"]["name"], area="", tags=["ambiguo", "albarán"], blocks=[
            {"tipo": "titulo", "texto": SUPPLIERS["transportes"]["name"]}, {"tipo": "apartado", "texto": "ALBARÁN DE ENTREGA Nº AE-55120"},
            {"tipo": "datos", "filas": [("Destinatario", COMPANY["name"]), ("Fecha de entrega", "18/09/2026"), ("Bultos", "6")]},
            {"tipo": "parrafo", "texto": "Mercancía recibida en buen estado. Firma y sello del receptor. Sin valor fiscal: la factura se enviará a fin de mes."}]),
        not_invoice("notificacion_requerimiento", organism="AEAT", area="Gestión Tributaria", tags=["notificación", "requerimiento"], blocks=[
            {"tipo": "apartado", "texto": "REQUERIMIENTO DE INFORMACIÓN"},
            {"tipo": "datos", "filas": [("Destinatario", COMPANY["name"]), ("NIF", COMPANY["tax_id"]), ("Expediente", "SIM-2026-GT-000123")]},
            {"tipo": "parrafo", "texto": "Se le requiere para que aporte el libro registro de facturas recibidas del tercer trimestre de 2026 en el plazo de diez días hábiles."}]),
        not_invoice("notificacion_providencia", organism="TGSS", area="Burgos", tags=["notificación", "apremio"], blocks=[
            {"tipo": "apartado", "texto": "PROVIDENCIA DE APREMIO"},
            {"tipo": "datos", "filas": [("Deudor", COMPANY["name"]), ("NIF", COMPANY["tax_id"]), ("Importe de la deuda", "1.284,60 €"), ("Recargo", "20 %")]},
            {"tipo": "parrafo", "texto": "Si no se ingresa la deuda en el plazo indicado se procederá al embargo de sus bienes."}]),
        not_invoice("administrativo_certificado", organism="AEAT", area="Gestión Tributaria", tags=["administrativo", "certificado"], blocks=[
            {"tipo": "apartado", "texto": "CERTIFICADO DE ESTAR AL CORRIENTE DE OBLIGACIONES TRIBUTARIAS"},
            {"tipo": "parrafo", "texto": f"La entidad {COMPANY['name']}, NIF {COMPANY['tax_id']}, se encuentra al corriente de sus obligaciones tributarias. {MARK}."}]),
        not_invoice("administrativo_justificante", organism="AEAT", area="Gestión Tributaria", tags=["administrativo", "justificante"], blocks=[
            {"tipo": "apartado", "texto": "JUSTIFICANTE DE PRESENTACIÓN · MODELO 303"},
            {"tipo": "datos", "filas": [("Declarante", COMPANY["name"]), ("NIF", COMPANY["tax_id"]), ("Periodo", "2T 2026"), ("Resultado", "-54,67 €")]},
            {"tipo": "parrafo", "texto": "Presentación realizada correctamente. Número de justificante: SIM3030000000001."}]),
    ]
    return out


def main() -> None:
    data = {
        "nota": f"{MARK}. Generado por generar.py: no editar a mano.",
        "empresa": {"name": COMPANY["name"], "tax_id": COMPANY["tax_id"]},
        "casos": cases(),
    }
    (HERE / "labels.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(data['casos'])} casos en {HERE}")


if __name__ == "__main__":
    main()
