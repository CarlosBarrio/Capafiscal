"""
Corpus de evaluación documental (SIMULACIÓN — NO OFICIAL): albaranes, presupuestos, proformas, pedidos, otros
documentos comerciales, casos ambiguos, escaneados (OCR) y multipágina, con su verdad y sus metadatos.

    python evaluation/datasets/corpus/generar.py                       # regenera PDF y labels.json
    python -m evaluation --dataset corpus --engines reglas,claude,hibrido   # los tres motores, mismo corpus

Las facturas «normales» NO se generan aquí: las aportan las facturas reales (datasets/reales/, fuera de git).
Las facturas sintéticas de este corpus existen solo para cubrir dificultades concretas (ambiguas, escaneadas,
multipágina).

La verdad se escribe aquí a partir de lo que se pinta en cada documento (importes calculados con Decimal), sin
usar el extractor de CapaFiscal. Esquema del evaluador (evaluation/core.py):

    expected   solo los campos que se evalúan. Un campo que no está NO se evalúa; un campo con null debe
               salir vacío. En lo que no es factura solo se evalúan «is_invoice» y «document_type».
    meta       synthetic, document_type (fino: recibo, contrato…), difficulty, ambiguous, multipage, ocr,
               language, visual_template.

Todo es ficticio: empresas inventadas, NIF con prefijo B00 y dígito de control válido, IBAN de la entidad 0000,
teléfonos 900 000 xxx y dominios .example.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from dataclasses import replace
from datetime import date
from datetime import timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from evaluation.documentos import MARK  # noqa: E402
from evaluation.maquetas import Degradacion  # noqa: E402
from evaluation.maquetas import Documento  # noqa: E402
from evaluation.maquetas import Empresa  # noqa: E402
from evaluation.maquetas import degradar  # noqa: E402
from evaluation.maquetas import eur  # noqa: E402
from evaluation.maquetas import num  # noqa: E402
from evaluation.maquetas import render  # noqa: E402
from evaluation.plantillas import cif  # noqa: E402

CENT = Decimal("0.01")
FOLDERS = ("albaranes", "presupuestos", "proformas", "pedidos", "otros", "ambiguos", "ocr", "multipagina", "facturas")


def iban(account: int) -> str:
    """IBAN ficticio (entidad 0000, oficina 0000) con dígito de control válido."""
    bban = f"00000000{account:012d}"
    check = 98 - int("".join(str(int(char, 36)) for char in bban + "ES00")) % 97
    raw = f"ES{check:02d}{bban}"
    return " ".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def empresa(name, digits, street, city, sector, color, shape, user) -> Empresa:
    domain = user + ".example"
    return Empresa(name, cif("B", digits), street, city, f"+34 900 000 {int(digits[-3:]):03d}", f"administracion@{domain}", f"www.{domain}", sector, color, shape)


# Nuestra empresa (la que usa CapaFiscal): destinataria de lo recibido y emisora de lo emitido.
NOSOTROS = empresa("Ibernova Servicios, S.L.", "0090010", "Calle de la Imaginación, 14, 3º B", "28045 Madrid", "servicios", "#1f3a5f", "circle", "ibernova")

PROVEEDORES = {
    "construccion": empresa("Construcciones Valdeorno, S.L.", "0090021", "Polígono El Cardizal, nave 7", "09007 Burgos", "construcción", "#8a4b1f", "square", "valdeorno"),
    "informatica": empresa("Datavértice Sistemas, S.L.", "0090032", "Avenida de los Bitios, 101", "47011 Valladolid", "informática", "#0f6d8c", "diamond", "datavertice"),
    "consultoria": empresa("Consultoría Albaterra, S.L.P.", "0090043", "Plaza del Consejo, 3, 1º", "37002 Salamanca", "consultoría", "#3b4a6b", "circle", "albaterra"),
    "hosteleria": empresa("Hostelería Fuenclara, S.L.", "0090054", "Calle Mesón Nuevo, 22", "34001 Palencia", "hostelería", "#9c2f2f", "circle", "fuenclara"),
    "transporte": empresa("Transportes Riberduero, S.L.", "0090065", "Centro de Transportes, parcela 18", "42005 Soria", "transporte", "#2f6b3a", "bars", "riberduero"),
    "suministros": empresa("Suministros Industriales Calmerón, S.A.", "0090076", "Camino de la Fragua, 45", "24009 León", "suministros", "#5a5a5a", "square", "calmeron"),
    "mantenimiento": empresa("Mantenimientos Ortegal Norte, S.L.", "0090087", "Calle del Engranaje, 9", "39011 Santander", "mantenimiento", "#c46a00", "diamond", "ortegalnorte"),
    "comercio": empresa("Comercial Quintamar, S.L.", "0090098", "Calle Mayor de Quintamar, 61", "26001 Logroño", "comercio", "#6b2f6b", "circle", "quintamar"),
    "papeleria": empresa("Papelería y Oficina Lumbrera, S.L.", "0090109", "Travesía de la Pluma, 2", "40001 Segovia", "comercio", "#2c5f8a", "bars", "lumbrera"),
    "climatizacion": empresa("Climatización Ventisca Sur, S.L.", "0090110", "Avenida del Cierzo, 33", "50012 Zaragoza", "mantenimiento", "#1b7a7a", "square", "ventiscasur"),
    "imprenta": empresa("Gráficas Tintaclara, S.L.", "0090121", "Calle del Linotipo, 5", "05001 Ávila", "comercio", "#7a1b4f", "diamond", "tintaclara"),
}
CLIENTES = {
    "taller": empresa("Talleres Mecánicos Brezal, S.L.", "0090132", "Carretera de Arcos, km 3", "09001 Burgos", "automoción", "#333333", "square", "brezal"),
    "clinica": empresa("Clínica Dental Sonrisaval, S.L.P.", "0090143", "Calle del Esmalte, 8, bajo", "28010 Madrid", "salud", "#2a7ab0", "circle", "sonrisaval"),
    "restaurante": empresa("Restaurante El Alcornocal de Abajo, S.L.", "0090154", "Plaza de la Encina, 1", "10003 Cáceres", "hostelería", "#7a4a1b", "circle", "alcornocal"),
    "colegio": empresa("Academia Pupitre Azul, S.L.", "0090165", "Calle de la Tiza, 17", "46005 Valencia", "formación", "#1b4f7a", "bars", "pupitreazul"),
    "bodega": empresa("Bodegas Cerro Tinajal, S.A.", "0090176", "Camino de las Cubas, s/n", "47300 Peñafiel (Valladolid)", "alimentación", "#5c1b2e", "diamond", "cerrotinajal"),
    "hotel": empresa("Hotel Mirador del Nogal, S.L.", "0090187", "Paseo del Nogal, 40", "33004 Oviedo", "hostelería", "#2e5c4a", "square", "miradornogal"),
    "logistica": empresa("Logística Brumal, S.L.", "0090198", "Polígono La Niebla, calle 3", "15008 A Coruña", "logística", "#4a4a7a", "bars", "brumal"),
    "inmobiliaria": empresa("Gestiones Inmobiliarias Tejaroja, S.L.", "0090209", "Calle del Alero, 12", "18001 Granada", "inmobiliaria", "#a0522d", "circle", "tejaroja"),
}

CATALOGO = {
    "construccion": [("CEM-25", "Saco de cemento CEM II/B-L 32,5 R 25 kg", "ud", "5.40"), ("LAD-PAL", "Ladrillo perforado 24x11,5x7 (palé 288 ud)", "palé", "189.00"),
                     ("ARE-04", "Arena lavada 0/4", "m³", "32.50"), ("MO-OF1", "Mano de obra oficial de primera", "h", "28.00"),
                     ("ALI-BAN", "Alicatado de baño, azulejo 20x60 colocado", "m²", "34.00"), ("PLD-120", "Plato de ducha de resina 80x120 blanco", "ud", "245.00"),
                     ("DEM-TAB", "Demolición de tabique y retirada de escombro", "m²", "18.50"), ("YES-PRO", "Guarnecido y enlucido de yeso", "m²", "12.80")],
    "informatica": [("NB-14I5", "Portátil 14\" i5 16 GB / 512 GB SSD", "ud", "879.00"), ("MON-27Q", "Monitor 27\" QHD IPS", "ud", "249.00"),
                    ("LIC-AV1", "Licencia anual antivirus empresa (puesto)", "ud", "39.90"), ("SOP-H", "Horas de técnico de soporte", "h", "45.00"),
                    ("SW-24G", "Switch 24 puertos gestionable", "ud", "189.00"), ("UTP-305", "Cable UTP Cat6 (bobina 305 m)", "ud", "112.00"),
                    ("INST-SRV", "Instalación y configuración de servidor", "ud", "360.00"), ("SAI-1500", "SAI 1500 VA línea interactiva", "ud", "215.00")],
    "consultoria": [("ASE-FIS", "Asesoramiento fiscal mensual", "mes", "180.00"), ("CON-H", "Horas de consultoría estratégica", "h", "95.00"),
                    ("PLN-NEG", "Elaboración de plan de negocio", "ud", "1200.00"), ("REV-CTR", "Revisión de contratos mercantiles", "ud", "150.00"),
                    ("FOR-H", "Formación in company", "h", "70.00")],
    "hosteleria": [("MEN-DIA", "Menú del día (cubierto)", "ud", "14.50"), ("CAT-30", "Servicio de catering 30 personas", "ud", "690.00"),
                   ("CAF-BOL", "Café y bollería", "ud", "3.20"), ("SAL-JOR", "Alquiler de sala de reuniones (jornada)", "ud", "220.00")],
    "transporte": [("POR-GRP", "Porte en grupaje Burgos – Madrid", "ud", "145.00"), ("PAL-FIL", "Paletización y filmado", "palé", "12.00"),
                   ("KM-ADC", "Kilómetro adicional", "km", "1.15"), ("URG-24", "Suplemento servicio urgente 24 h", "ud", "65.00")],
    "suministros": [("GNT-100", "Guantes de nitrilo talla L (caja 100)", "caja", "8.90"), ("T933-830", "Tornillo DIN 933 M8x30 cincado (caja 100)", "caja", "11.40"),
                    ("DIS-230", "Disco de corte metal 230 mm", "ud", "3.75"), ("ACH-46", "Aceite hidráulico ISO 46 (20 L)", "ud", "64.00"),
                    ("BRI-300", "Brida de nylon 300 mm (bolsa 100)", "bolsa", "4.20"), ("ROD-6205", "Rodamiento 6205-2RS", "ud", "6.80"),
                    ("CIN-50", "Cinta americana 50 mm x 50 m", "ud", "5.95"), ("BRO-HSS", "Juego de brocas HSS 1-10 mm", "ud", "18.40")],
    "mantenimiento": [("REV-CAL", "Revisión preventiva de caldera", "ud", "120.00"), ("FIL-CLI", "Cambio de filtros de climatización", "ud", "85.00"),
                      ("DES-TEC", "Desplazamiento de técnico", "ud", "30.00"), ("MAN-H", "Horas de técnico de mantenimiento", "h", "38.00"),
                      ("GAS-R32", "Recarga de gas R32", "kg", "70.00")],
    "comercio": [("PAP-A4", "Papel A4 80 g (caja 5 paquetes)", "caja", "21.50"), ("TON-NEG", "Tóner compatible negro", "ud", "39.00"),
                 ("ARC-AZ", "Archivador A-Z lomo ancho", "ud", "2.95"), ("BOL-GEL", "Bolígrafo de gel azul (caja 12)", "caja", "9.60"),
                 ("ETQ-105", "Etiquetas adhesivas 105x37 (caja 100 hojas)", "caja", "14.20"), ("CUA-A5", "Cuaderno A5 espiral 80 hojas", "ud", "1.85")],
}

# Empresas solo para las facturas normales (carpeta facturas/): que el corpus no repita siempre las mismas.
EMISORES_FACTURAS = {
    "limpieza": empresa("Limpiezas Brillamar, S.L.", "0090220", "Calle del Estropajo, 4", "03001 Alicante", "limpieza", "#2b6f9e", "circle", "brillamar"),
    "oficina": empresa("Distribuciones Escribanía Norte, S.L.", "0090231", "Calle del Tintero, 19", "20001 San Sebastián", "comercio", "#45607a", "bars", "escribania"),
    "cafeteria": empresa("Cafetería Los Soportales del Puente, S.L.", "0090242", "Plaza del Puente, 6", "49001 Zamora", "hostelería", "#8c3b2a", "square", "soportales"),
    "mensajeria": empresa("Mensajería Vencejo Exprés, S.L.", "0090253", "Calle del Paquete, 77", "41001 Sevilla", "transporte", "#3a7a2f", "diamond", "vencejo"),
    "diseno": empresa("Estudio Gráfico Caligrama, S.L.P.", "0090264", "Calle de la Tipografía, 11, 2º", "06001 Badajoz", "diseño", "#7a2f5c", "circle", "caligrama"),
    "ferreteria": empresa("Ferretería Industrial Yunque, S.L.", "0090275", "Polígono del Martillo, 30", "13001 Ciudad Real", "suministros", "#555555", "square", "yunque"),
    "redes": empresa("Redes y Cableados Lindero, S.L.", "0090286", "Avenida de la Fibra, 8", "30001 Murcia", "informática", "#1f6f6f", "bars", "lindero"),
    "abogados": empresa("Bufete Encinar y Asociados, S.L.P.", "0090297", "Calle de los Pleitos, 2", "02001 Albacete", "legal", "#2f3f6b", "diamond", "encinar"),
    "libreria": empresa("Librería Técnica Marginalia, S.L.", "0090308", "Calle del Colofón, 15", "11001 Cádiz", "libros", "#6b4a1b", "circle", "marginalia"),
    "obras": empresa("Reformas Alféizar, S.L.", "0090319", "Calle del Andamio, 23", "35001 Las Palmas", "construcción", "#9a5a1f", "square", "alfeizar"),
}
CLIENTES_FACTURAS = {
    "gimnasio": empresa("Gimnasio Zancada Activa, S.L.", "0090320", "Calle del Esfuerzo, 9", "31001 Pamplona", "deporte", "#2f6b5c", "bars", "zancada"),
    "farmacia": empresa("Farmacia Albahaca, S.L.", "0090331", "Calle de la Botica, 3", "44001 Teruel", "salud", "#3a8a3a", "circle", "albahaca"),
    "exportadora": empresa("Conservas Mareaviva Export, S.A.", "0090342", "Muelle de Poniente, nave 2", "36202 Vigo", "alimentación", "#1b3f7a", "diamond", "mareaviva"),
}

CATALOGO_FACTURAS = {
    "limpieza": [("LIM-OF", "Limpieza de oficinas (servicio mensual)", "mes", "420.00"), ("CRI-H", "Limpieza de cristales", "h", "22.00"),
                 ("FRE-MQ", "Fregado a máquina de suelo", "m²", "1.10"), ("DES-INS", "Desinfección de aseos", "ud", "35.00")],
    "diseno": [("LOG-ID", "Diseño de logotipo e identidad corporativa", "ud", "850.00"), ("MAQ-PAG", "Maquetación de catálogo (página)", "ud", "45.00"),
               ("WEB-LND", "Diseño de página de aterrizaje", "ud", "690.00"), ("DIS-H", "Horas de diseño gráfico", "h", "48.00")],
    "abogados": [("EST-JUR", "Estudio jurídico y emisión de dictamen", "ud", "950.00"), ("RED-CTR", "Redacción de contrato de arrendamiento", "ud", "380.00"),
                 ("ASI-JUI", "Asistencia a acto de conciliación", "ud", "300.00"), ("CON-ABG", "Horas de consulta jurídica", "h", "110.00")],
    "libreria": [("LIB-NIIF", "Manual de contabilidad y NIIF, 6ª ed.", "ud", "58.00"), ("LIB-FIS", "Memento práctico fiscal 2026", "ud", "149.00"),
                 ("LIB-LAB", "Código laboral anotado", "ud", "72.00"), ("LIB-MER", "Derecho mercantil esencial", "ud", "39.50")],
}
CATALOGO.update(CATALOGO_FACTURAS)


def d(value) -> Decimal:
    return Decimal(str(value))


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def iso(value: date) -> str:
    return value.isoformat()


def pick_lines(sector: str, count: int, seed: str, *, discount: bool = False, quantities: list | None = None) -> list[dict]:
    """Líneas de un sector: referencia, descripción, unidad, cantidad, precio, dto. %, importe.

    quantities: cantidades posibles (por defecto, las de siempre; las facturas normales usan unas más pequeñas)."""
    rng = random.Random(seed)
    items = CATALOGO[sector]
    out = []
    for index in range(count):
        ref, text, unit, price = items[index % len(items)]
        if quantities:
            quantity = d(rng.choice(quantities))
        else:
            quantity = d(rng.choice([1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 20, 25]) if unit not in {"h", "m²", "m³", "km", "kg"} else rng.choice([1.5, 2, 3, 4, 6, 7.5, 8, 12, 16]))
        if index >= len(items):  # líneas extra: misma referencia con variante, para tablas largas
            ref, text = f"{ref}-{index // len(items) + 1}", f"{text} (lote {index // len(items) + 1})"
        dto = d(rng.choice([0, 0, 5, 10, 15])) if discount else d(0)
        amount = q(quantity * d(price) * (100 - dto) / 100)
        out.append({"ref": ref, "text": text, "unit": unit, "qty": quantity, "price": d(price), "dto": dto, "amount": amount})
    return out


def qty(value: Decimal) -> str:
    return f"{value.normalize():f}".replace(".", ",")


def rows(lines: list[dict], columns: str) -> tuple[list[str], list[float], list[list[str]]]:
    """columns: combinación de r (referencia), d (descripción), u (unidad), c (cantidad), p (precio), t (dto), i (importe)."""
    spec = {"r": ("Referencia", 1.3, lambda l: l["ref"]), "d": ("Descripción", 4.2, lambda l: l["text"]), "u": ("Ud.", 0.7, lambda l: l["unit"]),
            "c": ("Cantidad", 0.9, lambda l: qty(l["qty"])), "p": ("Precio", 1.1, lambda l: num(l["price"])),
            "t": ("Dto.", 0.7, lambda l: f"{l['dto']:g} %" if l["dto"] else ""), "i": ("Importe", 1.2, lambda l: num(l["amount"]))}
    chosen = [spec[key] for key in columns]
    return [c[0] for c in chosen], [c[1] for c in chosen], [[c[2](line) for c in chosen] for line in lines]


def invoice_totals(base: Decimal, vat_rate: Decimal, irpf_rate: Decimal = d(0), label: str = "TOTAL FACTURA") -> tuple[list[tuple[str, str]], dict]:
    vat = q(base * vat_rate / 100)
    irpf = q(base * irpf_rate / 100)
    total = base + vat - irpf
    rows_ = [("Base imponible", eur(base)), (f"IVA {vat_rate:g} %", eur(vat))]
    if irpf:
        rows_.append((f"Retención IRPF {irpf_rate:g} %", f"-{eur(irpf)}"))
    rows_.append((label, eur(total)))
    truth = {"subtotal": f"{base:.2f}", "tax_total": f"{vat:.2f}", "total": f"{total:.2f}"}
    if irpf:
        truth["withholding_total"] = f"{irpf:.2f}"
    return rows_, truth


CASES: list[dict] = []


def add(folder: str, name: str, doc: Documento, style: str, expected: dict, meta: dict, tags: list[str], *, scan: Degradacion | None = None) -> None:
    path = HERE / folder / f"{name}.pdf"
    render(path, doc, style)
    if scan is not None:
        path.write_bytes(degradar(path.read_bytes(), scan, seed=name))
    import pymupdf

    pages = len(pymupdf.open(path))
    full_meta = {"synthetic": True, "difficulty": "medium", "ambiguous": False, "ocr": scan is not None, "language": "es-en" if style == "F" else "es",
                 "visual_template": "G" if scan is not None else style, "base_template": style, "pages": pages, **meta}
    full_meta["multipage"] = pages > 1
    CASES.append({"id": name, "file": f"{folder}/{name}.pdf", "set": "B", "tags": tags, "meta": full_meta, "expected": expected})


def not_invoice(kind: str) -> dict:
    return {"is_invoice": False, "document_type": kind}


def invoice_truth(doc: Documento, number: str, issued: date, totals: dict, *, due: date | None = None) -> dict:
    received = doc.destinatario == NOSOTROS
    out = {"is_invoice": True, "document_type": "factura", "direction": "RECEIVED" if received else "ISSUED",
           "supplier_name": doc.emisor.name, "supplier_tax_id": doc.emisor.nif, "customer_tax_id": doc.destinatario.nif,
           "invoice_number": number, "invoice_date": iso(issued), **totals}
    if due:
        out["due_date"] = iso(due)
    return out


def factura(*, emisor: Empresa, destinatario: Empresa, numero: str, fecha: date, sector: str, count: int, columns: str = "dcpi",
            vat: str = "21", irpf: str = "0", due_days: int | None = 30, titulo: str = "FACTURA", discount: bool = False,
            seed: str, quantities: list | None = None, **extra) -> tuple[Documento, dict]:
    lines = pick_lines(sector, count, seed, discount=discount, quantities=quantities)
    base = sum((line["amount"] for line in lines), d(0))
    totals_rows, truth = invoice_totals(base, d(vat), d(irpf))
    due = fecha + timedelta(days=due_days) if due_days else None
    cols, widths, body = rows(lines, columns)
    campos = list(extra.pop("campos", []))
    if due:
        campos.append(("Vencimiento", due.strftime("%d/%m/%Y")))
    doc = Documento(titulo=titulo, numero=numero, emisor=emisor, destinatario=destinatario, fecha=fecha, columnas=cols, anchos=widths, filas=body,
                    totales=totals_rows, campos=campos, banco=extra.pop("banco", f"Forma de pago: transferencia a {iban(int(emisor.nif[3:8]))}"), **extra)
    return doc, invoice_truth(doc, numero, fecha, truth, due=due)


def comercial(*, titulo: str, numero: str | None, emisor: Empresa, destinatario: Empresa, fecha: date, sector: str, count: int, columns: str,
              seed: str, discount: bool = False, totals: str | None = None, vat: str = "21", **extra) -> Documento:
    """Albaranes, presupuestos, proformas y pedidos. totals: None, «neto» (sin IVA), «iva» (base + IVA + total)."""
    lines = pick_lines(sector, count, seed, discount=discount)
    cols, widths, body = rows(lines, columns)
    base = sum((line["amount"] for line in lines), d(0))
    label = extra.pop("total_label", "TOTAL")
    totales = []
    if totals == "neto":
        totales = [(label, eur(base))]
    elif totals == "iva":
        totales, _ = invoice_totals(base, d(vat), label=label)
    return Documento(titulo=titulo, numero=numero, emisor=emisor, destinatario=destinatario, fecha=fecha, columnas=cols, anchos=widths, filas=body,
                     totales=totales, **extra)


P, C, N = PROVEEDORES, CLIENTES, NOSOTROS
D = date


def albaranes() -> None:
    f = "albaranes"
    add(f, "alb_basico_12345", comercial(titulo="ALBARÁN", numero="12345", emisor=P["suministros"], destinatario=N, fecha=D(2026, 9, 2),
        sector="suministros", count=6, columns="rdcu", seed="a1", campos=[("Su pedido", "PC-2026-18"), ("Transportista", "Medios propios")],
        notas=["Mercancía revisada en el momento de la entrega."], firma="Recibí conforme (nombre y DNI):"), "A",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "easy"}, ["albarán", "firma"])
    add(f, "alb_entrega_2026_00481", comercial(titulo="ALBARÁN DE ENTREGA", numero="2026-00481", emisor=P["construccion"], destinatario=C["taller"],
        fecha=D(2026, 9, 8), sector="construccion", count=5, columns="dcu", seed="a2", etiqueta_destinatario="Entregar a",
        campos=[("Obra", "Ampliación de nave, Carretera de Arcos km 3"), ("Matrícula", "0000-XXX")],
        notas=["Descarga con grúa del camión. Material paletizado."], firma="Recibido por:"), "B",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "easy"}, ["albarán", "obra"])
    add(f, "alb_salida_7812", comercial(titulo="ALBARÁN DE SALIDA", numero="7812", emisor=P["comercio"], destinatario=C["colegio"], fecha=D(2026, 9, 10),
        sector="comercio", count=8, columns="rdcu", seed="a3", campos=[("Almacén", "ALM-01 Central"), ("Ruta", "R-07"), ("Bultos", "3")]), "D",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "medium"}, ["albarán", "erp"])
    add(f, "alb_valorado_descuentos", comercial(titulo="ALBARÁN VALORADO", numero="AV-2026-0317", emisor=P["suministros"], destinatario=N,
        fecha=D(2026, 9, 14), sector="suministros", count=8, columns="rdcptj"[:0] or "rdcpti", seed="a4", discount=True, totals="neto",
        total_label="Importe neto mercancía", notas=["Precios netos sin impuestos. La factura se emitirá a fin de mes con todos los albaranes del periodo."]), "A",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "hard"}, ["albarán", "descuentos", "importes sin IVA"])
    add(f, "alb_referencias_producto", comercial(titulo="ALBARÁN Nº", numero=None, emisor=P["informatica"], destinatario=C["clinica"],
        fecha=D(2026, 9, 16), sector="informatica", count=7, columns="rdc", seed="a5", sin_titulo=False,
        campos=[("Número", "DV-ALB-55102"), ("Nº de serie equipos", "Ver hoja adjunta")]) if False else
        comercial(titulo="ALBARÁN", numero="DV-ALB-55102", emisor=P["informatica"], destinatario=C["clinica"], fecha=D(2026, 9, 16),
                  sector="informatica", count=7, columns="rdc", seed="a5", campos=[("Pedido cliente", "SV-0912")],
                  notas=["Nº de serie: NB-00A1, NB-00A2, MON-77F3.", "Garantía de 3 años desde la fecha de entrega."], firma="Conforme cliente:"), "C",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "easy"}, ["albarán", "referencias"])
    add(f, "alb_nota_entrega_antigua", comercial(titulo="NOTA DE ENTREGA", numero="NE-339", emisor=P["papeleria"], destinatario=N, fecha=D(2026, 9, 18),
        sector="comercio", count=5, columns="dcu", seed="a6", notas=["Entregado en recepción."], firma="Firma del receptor:"), "E",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "medium"}, ["albarán", "nota de entrega", "antiguo"])


def presupuestos() -> None:
    f = "presupuestos"
    add(f, "pre_2026_001_con_iva", comercial(titulo="PRESUPUESTO", numero="2026-001", emisor=P["mantenimiento"], destinatario=N, fecha=D(2026, 9, 1),
        sector="mantenimiento", count=5, columns="dcpi", seed="p1", totals="iva", total_label="TOTAL PRESUPUESTO",
        campos=[("Validez", "30 días")], condiciones=["Forma de pago: 50 % a la aceptación y 50 % a la finalización.", "Plazo de ejecución: 5 días hábiles."]), "A",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["presupuesto", "con IVA"])
    add(f, "pre_reforma_7", comercial(titulo="PRESUPUESTO DE REFORMA", numero="7", emisor=P["construccion"], destinatario=C["clinica"], fecha=D(2026, 9, 4),
        sector="construccion", count=7, columns="dupi"[:0] or "ducpi", seed="p2", totals="iva", total_label="TOTAL PRESUPUESTO",
        campos=[("Obra", "Reforma de baños planta baja"), ("Validez", "60 días")],
        condiciones=["Presupuesto válido 60 días.", "No incluye licencias municipales.", "Pago: 30 % inicio, 40 % mitad de obra, 30 % fin."],
        firma="Aceptado por el cliente:"), "B",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["presupuesto", "condiciones", "aceptación pendiente"])
    add(f, "pre_servicios_informaticos", comercial(titulo="PRESUPUESTO PARA SERVICIOS INFORMÁTICOS", numero=None, emisor=P["informatica"], destinatario=C["colegio"],
        fecha=D(2026, 9, 7), sector="informatica", count=6, columns="dcpi", seed="p3", totals="iva", total_label="TOTAL",
        campos=[("Referencia", "PR-0457"), ("Validez", "15 días")], condiciones=["Instalación en horario no lectivo."]), "C",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "hard"}, ["presupuesto", "título largo sin nº en el título"])
    add(f, "pre_mantenimiento_sin_iva", comercial(titulo="PRESUPUESTO DE MANTENIMIENTO", numero="M-2026-12", emisor=P["climatizacion"], destinatario=C["hotel"],
        fecha=D(2026, 9, 9), sector="mantenimiento", count=4, columns="dcpi", seed="p4", totals="neto", total_label="TOTAL (IVA no incluido)",
        condiciones=["Contrato anual con cuatro visitas preventivas.", "Los precios no incluyen IVA."]), "C",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["presupuesto", "sin IVA"])
    add(f, "pre_suministro_descuento", comercial(titulo="PRESUPUESTO DE SUMINISTRO", numero="S-88", emisor=P["suministros"], destinatario=C["logistica"],
        fecha=D(2026, 9, 11), sector="suministros", count=8, columns="rdcpti", seed="p5", discount=True, totals="iva", total_label="TOTAL PRESUPUESTO",
        condiciones=["Portes pagados para pedidos superiores a 300 €."]), "D",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["presupuesto", "descuentos", "erp"])
    add(f, "pre_88_aceptado", comercial(titulo="PRESUPUESTO", numero="88 ACEPTADO", emisor=P["consultoria"], destinatario=N, fecha=D(2026, 9, 12),
        sector="consultoria", count=3, columns="dcpi", seed="p6", totals="iva", total_label="TOTAL PRESUPUESTO", sello="ACEPTADO",
        notas=["Aceptado por el cliente el 15/09/2026."]), "E",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "hard"}, ["presupuesto", "aceptado", "sello"])
    add(f, "pre_pendiente_emitido", comercial(titulo="PRESUPUESTO", numero="IB-P-2026-031", emisor=N, destinatario=C["bodega"], fecha=D(2026, 9, 15),
        sector="consultoria", count=4, columns="dcpi", seed="p7", totals="iva", total_label="TOTAL PRESUPUESTO",
        campos=[("Estado", "Pendiente de aceptación")], condiciones=["Validez: 30 días.", "Forma de pago: transferencia a 30 días."],
        firma="Conforme y aceptado (firma y sello):"), "F",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["presupuesto", "emitido", "bilingüe"])


def proformas() -> None:
    f = "proformas"
    bank = f"Datos bancarios para el pago anticipado: {iban(9001)} (Banco Ficticio del Duero)"
    add(f, "prof_2026_001_banco", comercial(titulo="PROFORMA", numero="2026-001", emisor=P["informatica"], destinatario=N, fecha=D(2026, 9, 3),
        sector="informatica", count=4, columns="dcpi", seed="f1", totals="iva", total_label="TOTAL", banco=bank,
        notas=["Documento sin validez fiscal. La factura se emitirá tras el pago."]), "A",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "medium"}, ["proforma", "datos bancarios"])
    add(f, "prof_factura_proforma_77", comercial(titulo="FACTURA PROFORMA", numero="77", emisor=P["hosteleria"], destinatario=C["inmobiliaria"],
        fecha=D(2026, 9, 6), sector="hosteleria", count=3, columns="dcpi", seed="f2", totals="iva", vat="10", total_label="TOTAL", banco=bank), "B",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "hard"}, ["proforma", "IVA 10", "título con FACTURA"])
    add(f, "prof_comercial_bilingue_2026_15", comercial(titulo="PROFORMA COMERCIAL", numero="2026-15", titulo_en="Commercial proforma", emisor=P["suministros"],
        destinatario=C["bodega"], fecha=D(2026, 9, 9), sector="suministros", count=6, columns="rdcpi", seed="f3", totals="iva", total_label="TOTAL",
        banco=bank, condiciones=["Incoterm EXW León.", "Pago anticipado 100 %."]), "F",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "medium"}, ["proforma", "bilingüe"])
    add(f, "prof_descuentos_erp", comercial(titulo="PROFORMA", numero="PF-00912", emisor=P["comercio"], destinatario=C["colegio"], fecha=D(2026, 9, 13),
        sector="comercio", count=9, columns="rdcpti", seed="f4", discount=True, totals="iva", total_label="TOTAL PROFORMA"), "D",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "medium"}, ["proforma", "descuentos", "erp"])
    add(f, "prof_condiciones_anticipo", comercial(titulo="PROFORMA", numero="2026/044", emisor=P["imprenta"], destinatario=N, fecha=D(2026, 9, 17),
        sector="comercio", count=3, columns="dcpi", seed="f5", totals="iva", total_label="TOTAL", banco=bank,
        condiciones=["Se requiere anticipo del 40 % para iniciar la producción.", "Plazo de entrega: 10 días tras el pago."]), "C",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "medium"}, ["proforma", "condiciones"])


def pedidos() -> None:
    f = "pedidos"
    add(f, "ped_4500123_erp", comercial(titulo="PEDIDO", numero="4500123", emisor=N, destinatario=P["suministros"], etiqueta_destinatario="Proveedor",
        fecha=D(2026, 9, 1), sector="suministros", count=6, columns="rdcpi", seed="o1", totals="neto", total_label="TOTAL PEDIDO (sin IVA)",
        campos=[("Entrega prevista", "08/09/2026"), ("Centro", "Madrid")]), "D",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "easy"}, ["pedido", "erp"])
    add(f, "ped_compra_pc_2026_18", comercial(titulo="PEDIDO DE COMPRA", numero="PC-2026-18", emisor=N, destinatario=P["informatica"],
        etiqueta_destinatario="Proveedor", fecha=D(2026, 9, 5), sector="informatica", count=5, columns="rdcpi", seed="o2", totals="neto",
        total_label="Total pedido (IVA no incluido)", campos=[("Entrega prevista", "19/09/2026")],
        condiciones=["Indicar el nº de pedido en el albarán y en la factura.", "Pago a 60 días fecha factura."]), "A",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "easy"}, ["pedido", "compra"])
    add(f, "ped_orden_compra_2026_008", comercial(titulo="ORDEN DE COMPRA", numero="2026-008", emisor=C["hotel"], destinatario=N, etiqueta_destinatario="Proveedor",
        fecha=D(2026, 9, 9), sector="consultoria", count=3, columns="dcpi", seed="o3", totals="neto", total_label="Importe total",
        campos=[("Fecha de servicio", "01/10/2026")]), "B",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "hard"}, ["pedido", "orden de compra"])
    add(f, "ped_hoja_pedido_311", comercial(titulo="HOJA DE PEDIDO", numero="311", emisor=C["restaurante"], destinatario=P["hosteleria"],
        etiqueta_destinatario="Proveedor", fecha=D(2026, 9, 12), sector="hosteleria", count=4, columns="dcu", seed="o4",
        notas=["Servir antes de las 10:00 por la puerta de carga."]), "E",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "medium"}, ["pedido", "sin precios", "antiguo"])
    add(f, "ped_purchase_order_po_7781", comercial(titulo="PEDIDO", numero="PO-7781", titulo_en="Purchase order", emisor=C["bodega"],
        destinatario=P["transporte"], etiqueta_destinatario="Proveedor", fecha=D(2026, 9, 19), sector="transporte", count=4, columns="dcpi", seed="o5",
        totals="neto", total_label="TOTAL", campos=[("Entrega prevista", "26/09/2026")]), "F",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "medium"}, ["pedido", "bilingüe"])


def otros() -> None:
    f = "otros"
    add(f, "otr_confirmacion_pedido", comercial(titulo="CONFIRMACIÓN DE PEDIDO", numero="CP-1182", emisor=P["informatica"], destinatario=N,
        fecha=D(2026, 9, 6), sector="informatica", count=5, columns="rdcpi", seed="x1", totals="neto", total_label="Total confirmado (sin IVA)",
        campos=[("Su pedido", "PC-2026-18"), ("Entrega confirmada", "19/09/2026")]), "A",
        not_invoice("pedido"), {"document_type": "confirmacion_pedido", "difficulty": "hard"}, ["confirmación de pedido"])
    recibo = Documento(titulo="RECIBO", numero="0057", emisor=P["consultoria"], destinatario=N, fecha=D(2026, 9, 20), etiqueta_destinatario="Recibido de",
                       columnas=["Concepto", "Importe"], anchos=[5, 1.3], filas=[["Cuota de asesoramiento de septiembre (factura A-2026-118)", num(d("371.00"))]],
                       notas=["Recibí la cantidad de TRESCIENTOS SETENTA Y UN EUROS (371,00 €) en efectivo."], firma="Firma y sello:")
    add(f, "otr_recibo_0057", recibo, "E", not_invoice("otro"), {"document_type": "recibo", "difficulty": "medium"}, ["recibo"])
    banco = Empresa("Banco Ficticio del Duero, S.A.", cif("A", "0090220"), "Calle del Ahorro, 1", "47001 Valladolid", "+34 900 000 220",
                    "atencion@bancoficticio.example", "", "banca", "#00507a", "bars")
    justificante = Documento(titulo="JUSTIFICANTE DE TRANSFERENCIA", numero=None, emisor=banco, destinatario=N, fecha=D(2026, 9, 21),
                             etiqueta_destinatario="Ordenante", columnas=["Dato", "Valor"], anchos=[2, 4],
                             filas=[["Beneficiario", P["construccion"].name], ["Cuenta destino", iban(2101)], ["Importe", eur(d("2420.00"))],
                                    ["Concepto", "Pago factura CV-2026-0233"], ["Referencia", "TRF-20260921-000184"]],
                             notas=["Este documento es un justificante de la operación y no sustituye a la factura."])
    add(f, "otr_justificante_transferencia", justificante, "C", not_invoice("otro"), {"document_type": "justificante_pago", "difficulty": "medium"},
        ["justificante de pago", "banco"])
    add(f, "otr_solicitud_presupuesto", comercial(titulo="SOLICITUD DE PRESUPUESTO", numero="SP-14", emisor=N, destinatario=P["construccion"],
        etiqueta_destinatario="Destinatario", fecha=D(2026, 9, 2), sector="construccion", count=5, columns="dcu", seed="x4",
        notas=["Rogamos nos remitan su mejor oferta antes del 15/09/2026, indicando plazo de ejecución."]), "B",
        not_invoice("otro"), {"document_type": "solicitud_presupuesto", "difficulty": "hard"}, ["solicitud de presupuesto"])
    parte = Documento(titulo="PARTE DE TRABAJO", numero="PT-5521", emisor=P["mantenimiento"], destinatario=C["hotel"], fecha=D(2026, 9, 23),
                      columnas=["Hora", "Tarea realizada", "Técnico"], anchos=[1, 5, 1.6],
                      filas=[["09:10", "Revisión de caldera y quemador", "J. Ficticio"], ["10:30", "Cambio de filtros en climatizadoras 1 y 2", "J. Ficticio"],
                             ["12:00", "Prueba de funcionamiento y limpieza", "J. Ficticio"]],
                      campos=[("Horas totales", "3,5"), ("Contrato", "CM-2026-03")], notas=["Sin incidencias."], firma="Conforme cliente:")
    add(f, "otr_parte_trabajo", parte, "D", not_invoice("otro"), {"document_type": "hoja_servicio", "difficulty": "medium"}, ["parte de trabajo"])
    contrato = Documento(titulo="CONTRATO DE MANTENIMIENTO", numero="CM-2026-03", emisor=P["mantenimiento"], destinatario=C["hotel"], fecha=D(2026, 1, 15),
                         columnas=["Cláusula", "Contenido"], anchos=[1.2, 5],
                         filas=[["1. Objeto", "Mantenimiento preventivo y correctivo de calderas y climatización."],
                                ["2. Duración", "Un año desde la firma, prorrogable por periodos iguales."],
                                ["3. Precio", "Cuota anual de 1.200,00 € más IVA, facturada trimestralmente."],
                                ["4. Visitas", "Cuatro visitas preventivas al año y atención a avisos en 24 h."],
                                ["5. Resolución", "Cualquiera de las partes con un preaviso de 30 días."]],
                         firma="Por el cliente:")
    add(f, "otr_contrato_mantenimiento", contrato, "E", not_invoice("otro"), {"document_type": "contrato", "difficulty": "medium"}, ["contrato"])


def ambiguos() -> None:
    f = "ambiguos"
    amb = {"ambiguous": True, "difficulty": "hard"}
    doc, truth = factura(emisor=P["suministros"], destinatario=N, numero="12", fecha=D(2026, 9, 4), sector="suministros", count=5, seed="m1",
                         antes_titulo=["ALBARÁN Nº 4471"], columns="rdcpi")
    add(f, "amb_A_albaran_luego_factura", doc, "C", truth, {**amb, "document_type": "factura"}, ["ambiguo", "ALBARÁN Nº encima de FACTURA"])
    add(f, "amb_B_presupuesto_con_impuestos", comercial(titulo="PRESUPUESTO", numero="88", emisor=P["construccion"], destinatario=N, fecha=D(2026, 9, 5),
        sector="construccion", count=6, columns="dcpi", seed="m2", totals="iva", total_label="TOTAL", condiciones=["Forma de pago: transferencia."]), "A",
        not_invoice("presupuesto"), {**amb, "document_type": "presupuesto"}, ["ambiguo", "presupuesto con IVA y total"])
    doc, truth = factura(emisor=P["comercio"], destinatario=N, numero="CQ-2026-0571", fecha=D(2026, 9, 7), sector="comercio", count=4, seed="m3",
                         notas=["Albarán pendiente de firmar: se adjunta copia para su devolución firmada."])
    add(f, "amb_C_factura_albaran_pendiente", doc, "B", truth, {**amb, "document_type": "factura"}, ["ambiguo", "frase con albarán"])
    doc, truth = factura(emisor=P["consultoria"], destinatario=N, numero="F-12", fecha=D(2026, 9, 8), sector="consultoria", count=2, seed="m4",
                         titulo="Nº de factura", sin_titulo=True, irpf="15")
    add(f, "amb_D_sin_titulo_numero_de_factura", doc, "C", truth, {**amb, "document_type": "factura"}, ["ambiguo", "sin título", "IRPF"])
    lines = pick_lines("transporte", 4, "m5")
    base = sum((line["amount"] for line in lines), d(0))
    totals_rows, totals_truth = invoice_totals(base, d(21))
    albaranes_rows = [[f"ALB-{3100 + index}", (D(2026, 9, 1) + timedelta(days=index * 3)).strftime("%d/%m/%Y"), num(line["amount"]), qty(line["qty"])]
                      for index, line in enumerate(lines)]
    doc = Documento(titulo="FACTURA", numero="TR-2026-0812", emisor=P["transporte"], destinatario=N, fecha=D(2026, 9, 30),
                    columnas=["Albarán", "Fecha", "Importe", "Cantidad"], anchos=[1.5, 1.3, 1.3, 1], filas=albaranes_rows, totales=totals_rows,
                    campos=[("Vencimiento", "30/10/2026")], notas=["Factura recapitulativa de los albaranes del mes de septiembre."],
                    banco=f"Forma de pago: transferencia a {iban(9065)}")
    add(f, "amb_E_cabecera_albaran_fecha_importe", doc, "D", invoice_truth(doc, "TR-2026-0812", D(2026, 9, 30), totals_truth, due=D(2026, 10, 30)),
        {**amb, "document_type": "factura"}, ["ambiguo", "cabecera de tabla «Albarán Fecha Importe»"])
    doc, truth = factura(emisor=P["mantenimiento"], destinatario=N, numero="MO-2026-221", fecha=D(2026, 9, 10), sector="mantenimiento", count=3, seed="m6",
                         condiciones=["Presupuesto válido 30 días para trabajos adicionales no incluidos en esta factura."])
    add(f, "amb_F_factura_presupuesto_valido", doc, "A", truth, {**amb, "document_type": "factura"}, ["ambiguo", "frase «Presupuesto válido 30 días»"])
    doc, truth = factura(emisor=P["informatica"], destinatario=N, numero="DV-2026-1043", fecha=D(2026, 9, 11), sector="informatica", count=4, seed="m7",
                         notas=["Presupuesto aceptado por el cliente el 02/09/2026 (ref. PR-0457)."])
    add(f, "amb_G_factura_presupuesto_aceptado", doc, "B", truth, {**amb, "document_type": "factura"}, ["ambiguo", "frase «Presupuesto aceptado por el cliente»"])
    doc, truth = factura(emisor=P["suministros"], destinatario=N, numero="SC-2026-3381", fecha=D(2026, 9, 12), sector="suministros", count=5, seed="m8",
                         antes_titulo=["Pedido nº 4500123"], columns="rdcpi")
    add(f, "amb_H_pedido_encima_de_factura", doc, "A", truth, {**amb, "document_type": "factura"}, ["ambiguo", "Pedido nº encima de FACTURA"])
    add(f, "amb_I_albaran_factura_se_emitira", comercial(titulo="ALBARÁN", numero="AL-2026-912", emisor=P["papeleria"], destinatario=N, fecha=D(2026, 9, 13),
        sector="comercio", count=5, columns="rdcu", seed="m9", notas=["Factura: se emitirá a fin de mes junto con el resto de entregas."]), "C",
        not_invoice("albaran"), {**amb, "document_type": "albaran"}, ["ambiguo", "albarán que menciona la factura"])
    doc, truth = factura(emisor=P["comercio"], destinatario=N, numero="R-2026-005", fecha=D(2026, 9, 15), sector="comercio", count=2, seed="m10",
                         titulo="FACTURA RECTIFICATIVA", campos=[("Rectifica a", "Factura CQ-2026-0571"), ("Motivo", "Devolución de mercancía")])
    # Rectificativa por devolución: importes en negativo.
    neg = {key: f"-{value}" for key, value in truth.items() if key in {"subtotal", "tax_total", "total"}}
    doc.filas = [row[:-1] + [f"-{row[-1]}"] for row in doc.filas]
    doc.totales = [(label, f"-{value}") for label, value in doc.totales]
    add(f, "amb_J_rectificativa_cita_factura_original", doc, "E", {**truth, **neg}, {**amb, "document_type": "factura"},
        ["ambiguo", "rectificativa", "importes negativos"])
    doc, truth = factura(emisor=P["papeleria"], destinatario=N, numero="33", fecha=D(2026, 9, 16), sector="comercio", count=5, seed="m11", titulo="ALBARÁN-FACTURA")
    add(f, "amb_K_albaran_factura_33", doc, "D", truth, {**amb, "document_type": "factura"}, ["ambiguo", "albarán-factura"])
    carta = comercial(titulo="Asunto: presupuesto de reparación de cubierta", numero=None, emisor=P["construccion"], destinatario=N, fecha=D(2026, 9, 17),
                      sector="construccion", count=4, columns="dcpi", seed="m12", totals="iva", total_label="TOTAL", marca_numero="",
                      antes_titulo=["Muy señores nuestros:", "Atendiendo a su solicitud, les remitimos nuestra valoración de los trabajos."])
    add(f, "amb_L_presupuesto_en_carta", carta, "E", not_invoice("presupuesto"), {**amb, "document_type": "presupuesto"},
        ["ambiguo", "presupuesto en formato carta, sin título"])
    doc, truth = factura(emisor=N, destinatario=C["restaurante"], numero="IB-2026-0144", fecha=D(2026, 9, 18), sector="consultoria", count=3, seed="m13",
                         notas=["Albarán adjunto firmado por el cliente.", "Pedido urgente atendido el mismo día."])
    add(f, "amb_M_factura_emitida_frases_varias", doc, "F", truth, {**amb, "document_type": "factura"}, ["ambiguo", "emitida", "frases que empiezan por albarán/pedido"])


def ocr() -> None:
    f = "ocr"
    perfiles = {
        "buen_escaner": Degradacion(dpi=200, rotacion=0.5, desenfoque=0.3, ruido=2000, calidad_jpeg=75),
        "escaneo_pobre": Degradacion(dpi=110, rotacion=1.8, desenfoque=0.7, ruido=500, contraste=0.8, calidad_jpeg=50),
        "foto_movil": Degradacion(dpi=130, rotacion=-2.5, desenfoque=0.6, ruido=900, fondo=228, sombra=True, calidad_jpeg=45),
        "fax": Degradacion(dpi=150, rotacion=0.9, desenfoque=0.2, ruido=250, contraste=1.6, calidad_jpeg=60),
        "lavado": Degradacion(dpi=120, rotacion=-0.8, desenfoque=0.5, ruido=1500, contraste=0.85, brillo=1.12, calidad_jpeg=55),
        "baja_resolucion": Degradacion(dpi=100, rotacion=-1.2, desenfoque=0.4, ruido=800, calidad_jpeg=40),
    }
    base = {"ocr": True}
    doc, truth = factura(emisor=P["consultoria"], destinatario=N, numero="CA-2026-0301", fecha=D(2026, 9, 2), sector="consultoria", count=3, seed="r1", irpf="15")
    add(f, "ocr_factura_buen_escaner", doc, "A", truth, {**base, "document_type": "factura", "difficulty": "medium", "scan_profile": "buen_escaner"},
        ["OCR", "factura", "IRPF"], scan=perfiles["buen_escaner"])
    doc, truth = factura(emisor=P["hosteleria"], destinatario=N, numero="FC-26/0912", fecha=D(2026, 9, 6), sector="hosteleria", count=4, seed="r2", vat="10")
    add(f, "ocr_factura_escaneo_pobre", doc, "E", truth, {**base, "document_type": "factura", "difficulty": "hard", "scan_profile": "escaneo_pobre"},
        ["OCR", "factura", "IVA 10"], scan=perfiles["escaneo_pobre"])
    doc, truth = factura(emisor=P["transporte"], destinatario=N, numero="RD-2026-4410", fecha=D(2026, 9, 9), sector="transporte", count=3, seed="r3")
    add(f, "ocr_factura_foto_movil", doc, "C", truth, {**base, "document_type": "factura", "difficulty": "hard", "scan_profile": "foto_movil"},
        ["OCR", "factura", "fotografiado"], scan=perfiles["foto_movil"])
    add(f, "ocr_albaran_fax", comercial(titulo="ALBARÁN", numero="55821", emisor=P["suministros"], destinatario=N, fecha=D(2026, 9, 10),
        sector="suministros", count=6, columns="rdcu", seed="r4", firma="Recibí conforme:"), "D",
        not_invoice("albaran"), {**base, "document_type": "albaran", "difficulty": "hard", "scan_profile": "fax"}, ["OCR", "albarán"], scan=perfiles["fax"])
    add(f, "ocr_presupuesto_escaneado", comercial(titulo="PRESUPUESTO", numero="2026-019", emisor=P["climatizacion"], destinatario=N, fecha=D(2026, 9, 12),
        sector="mantenimiento", count=4, columns="dcpi", seed="r5", totals="iva", total_label="TOTAL PRESUPUESTO"), "B",
        not_invoice("presupuesto"), {**base, "document_type": "presupuesto", "difficulty": "hard", "scan_profile": "buen_escaner"},
        ["OCR", "presupuesto"], scan=perfiles["buen_escaner"])
    add(f, "ocr_proforma_lavada", comercial(titulo="FACTURA PROFORMA", numero="PF-2026-88", titulo_en="Proforma invoice", emisor=P["imprenta"],
        destinatario=N, fecha=D(2026, 9, 14), sector="comercio", count=4, columns="dcpi", seed="r6", totals="iva", total_label="TOTAL"), "F",
        not_invoice("proforma"), {**base, "document_type": "proforma", "difficulty": "hard", "scan_profile": "lavado"},
        ["OCR", "proforma", "bilingüe"], scan=perfiles["lavado"])
    add(f, "ocr_pedido_baja_resolucion", comercial(titulo="PEDIDO", numero="4500177", emisor=N, destinatario=P["comercio"], etiqueta_destinatario="Proveedor",
        fecha=D(2026, 9, 16), sector="comercio", count=5, columns="rdcpi", seed="r7", totals="neto", total_label="TOTAL PEDIDO (sin IVA)"), "A",
        not_invoice("pedido"), {**base, "document_type": "pedido", "difficulty": "hard", "scan_profile": "baja_resolucion"},
        ["OCR", "pedido"], scan=perfiles["baja_resolucion"])
    doc, truth = factura(emisor=P["informatica"], destinatario=N, numero="DV-2026-1101", fecha=D(2026, 9, 20), sector="informatica", count=34, seed="r8",
                         columns="rdcpi")
    add(f, "ocr_factura_multipagina_escaneada", doc, "B", truth,
        {**base, "document_type": "factura", "difficulty": "hard", "scan_profile": "escaneo_pobre"}, ["OCR", "factura", "multipágina"],
        scan=replace(perfiles["escaneo_pobre"], rotacion=1.1))


def multipagina() -> None:
    f = "multipagina"
    doc, truth = factura(emisor=P["suministros"], destinatario=N, numero="SC-2026-3402", fecha=D(2026, 9, 3), sector="suministros", count=100, seed="g1",
                         columns="rdcpi")
    add(f, "mp_factura_3_paginas", doc, "A", truth, {"document_type": "factura", "difficulty": "hard"},
        ["multipágina", "total solo en la última página", "proveedor solo en la primera"])
    doc, truth = factura(emisor=P["comercio"], destinatario=N, numero="CQ-2026-0601", fecha=D(2026, 9, 6), sector="comercio", count=40, seed="g2",
                         columns="rdcpti", discount=True)
    add(f, "mp_factura_erp_descuentos", doc, "D", truth, {"document_type": "factura", "difficulty": "hard"}, ["multipágina", "erp", "descuentos"])
    doc, truth = factura(emisor=N, destinatario=C["bodega"], numero="IB-2026-0150", fecha=D(2026, 9, 9), sector="consultoria", count=30, seed="g3")
    add(f, "mp_factura_bilingue_emitida", doc, "F", truth, {"document_type": "factura", "difficulty": "hard"}, ["multipágina", "bilingüe", "emitida"])
    doc, truth = factura(emisor=P["consultoria"], destinatario=N, numero="CA-2026-0330", fecha=D(2026, 9, 30), sector="consultoria", count=32, seed="g4",
                         irpf="15", columns="dcpi")
    add(f, "mp_factura_irpf_servicios", doc, "B", truth, {"document_type": "factura", "difficulty": "hard"}, ["multipágina", "IRPF"])
    add(f, "mp_albaran_3_paginas", comercial(titulo="ALBARÁN DE ENTREGA", numero="2026-00517", emisor=P["construccion"], destinatario=C["taller"],
        fecha=D(2026, 9, 12), sector="construccion", count=120, columns="rdcu", seed="g5", firma="Recibido por:"), "C",
        not_invoice("albaran"), {"document_type": "albaran", "difficulty": "medium"}, ["multipágina", "albarán", "muchas líneas"])
    add(f, "mp_presupuesto_condiciones", comercial(titulo="PRESUPUESTO DE REFORMA", numero="12", emisor=P["construccion"], destinatario=C["hotel"],
        fecha=D(2026, 9, 14), sector="construccion", count=36, columns="ducpi"[:0] or "rducpi", seed="g6", totals="iva", total_label="TOTAL PRESUPUESTO",
        condiciones=[f"Condición {index}: texto de condiciones generales de contratación, plazos, garantías y forma de pago." for index in range(1, 13)],
        firma="Aceptado por el cliente:"), "B",
        not_invoice("presupuesto"), {"document_type": "presupuesto", "difficulty": "medium"}, ["multipágina", "presupuesto", "condiciones"])
    add(f, "mp_pedido_2_paginas", comercial(titulo="PEDIDO DE COMPRA", numero="PC-2026-31", emisor=N, destinatario=P["suministros"], etiqueta_destinatario="Proveedor",
        fecha=D(2026, 9, 17), sector="suministros", count=45, columns="rdcpi", seed="g7", totals="neto", total_label="Total pedido (sin IVA)",
        campos=[("Entrega prevista", "30/09/2026")]), "A",
        not_invoice("pedido"), {"document_type": "pedido", "difficulty": "medium"}, ["multipágina", "pedido"])
    add(f, "mp_proforma_2_paginas", comercial(titulo="PROFORMA", numero="2026-061", emisor=P["informatica"], destinatario=C["colegio"], fecha=D(2026, 9, 22),
        sector="informatica", count=40, columns="rdcpi", seed="g8", totals="iva", total_label="TOTAL",
        banco=f"Pago anticipado: {iban(9032)}"), "E",
        not_invoice("proforma"), {"document_type": "proforma", "difficulty": "medium"}, ["multipágina", "proforma"])


def facturas() -> None:
    """Facturas normales, sin trampas: 5 fáciles, 5 medias y 5 difíciles (largas, IRPF, IVA reducido, series raras)."""
    f = "facturas"
    E, K = EMISORES_FACTURAS, CLIENTES_FACTURAS

    def normal(name, difficulty, style, tags, **kwargs):
        doc, truth = factura(quantities=kwargs.pop("quantities", [1, 1, 1, 2, 2, 3, 4]), **kwargs)
        add(f, name, doc, style, truth, {"document_type": "factura", "difficulty": difficulty}, ["factura normal", *tags])

    # Fáciles: una página, título claro, pocas líneas, IVA general
    normal("fac_facil_limpieza_2026_0091", "easy", "A", ["servicios"], emisor=E["limpieza"], destinatario=N, numero="2026-0091",
           fecha=D(2026, 9, 1), sector="limpieza", count=3, seed="n1", quantities=[1])
    normal("fac_facil_oficina_eno_3312", "easy", "B", ["material de oficina"], emisor=E["oficina"], destinatario=N, numero="ENO-3312",
           fecha=D(2026, 9, 3), sector="comercio", count=4, seed="n2", columns="dcpi")
    normal("fac_facil_cafeteria_iva10", "easy", "C", ["IVA 10 %"], emisor=E["cafeteria"], destinatario=N, numero="T-2026-0457",
           fecha=D(2026, 9, 5), sector="hosteleria", count=3, seed="n3", vat="10", due_days=None, campos=[("Forma de pago", "tarjeta")],
           quantities=[1, 2, 4, 6])
    normal("fac_facil_emitida_gimnasio", "easy", "A", ["emitida"], emisor=N, destinatario=K["gimnasio"], numero="IB-2026-0161",
           fecha=D(2026, 9, 8), sector="consultoria", count=2, seed="n4")
    normal("fac_facil_mensajeria_vx_88120", "easy", "E", ["transporte"], emisor=E["mensajeria"], destinatario=N, numero="VX-88120",
           fecha=D(2026, 9, 9), sector="transporte", count=3, seed="n5")

    # Medias: retenciones, descuentos, referencias, vencimientos largos, emitidas bilingües
    normal("fac_media_diseno_irpf15", "medium", "B", ["IRPF", "profesional"], emisor=E["diseno"], destinatario=N, numero="CAL-26/045",
           fecha=D(2026, 9, 10), sector="diseno", count=3, seed="n6", irpf="15", columns="dcpi")
    normal("fac_media_ferreteria_descuentos", "medium", "D", ["descuentos", "referencias", "erp"], emisor=E["ferreteria"], destinatario=N,
           numero="FY-0003391", fecha=D(2026, 9, 11), sector="suministros", count=8, seed="n7", columns="rducpti", discount=True, due_days=60,
           quantities=[1, 2, 5, 10, 20])
    normal("fac_media_emitida_irpf_farmacia", "medium", "E", ["emitida", "IRPF"], emisor=N, destinatario=K["farmacia"], numero="IB-2026-0163",
           fecha=D(2026, 9, 14), sector="consultoria", count=3, seed="n8", irpf="15")
    normal("fac_media_redes_10_lineas", "medium", "C", ["informática", "vencimiento 60 días"], emisor=E["redes"], destinatario=N,
           numero="LIN-2026-0718", fecha=D(2026, 9, 15), sector="informatica", count=10, seed="n9", columns="rdcpi", due_days=60)
    normal("fac_media_emitida_bilingue_export", "medium", "F", ["emitida", "bilingüe"], emisor=N, destinatario=K["exportadora"], numero="IB-2026-0165",
           fecha=D(2026, 9, 16), sector="consultoria", count=4, seed="n10", titulo_en="Invoice")

    # Difíciles: largas, varias páginas, IRPF 7 %, IVA superreducido, series con barras, sin vencimiento
    normal("fac_dificil_obras_2_paginas", "hard", "A", ["multipágina", "construcción"], emisor=E["obras"], destinatario=N, numero="RA-2026-0204",
           fecha=D(2026, 9, 17), sector="construccion", count=48, seed="n11", columns="rducpi")
    normal("fac_dificil_abogados_irpf7", "hard", "D", ["IRPF 7 %", "profesional", "erp"], emisor=E["abogados"], destinatario=N, numero="BE/2026/0377",
           fecha=D(2026, 9, 18), sector="abogados", count=4, seed="n12", irpf="7", columns="rdcpi")
    normal("fac_dificil_libreria_iva4", "hard", "B", ["IVA 4 %"], emisor=E["libreria"], destinatario=N, numero="M-2026-1120",
           fecha=D(2026, 9, 21), sector="libreria", count=6, seed="n13", vat="4", columns="rdcpi")
    normal("fac_dificil_serie_barras_descuentos", "hard", "E", ["serie con barras", "descuentos"], emisor=E["oficina"], destinatario=N,
           numero="A/2026/000128", fecha=D(2026, 9, 22), sector="comercio", count=12, seed="n14", columns="rdcpti", discount=True,
           quantities=[1, 2, 5, 10, 12, 20])
    normal("fac_dificil_emitida_larga_sin_vencimiento", "hard", "F", ["emitida", "multipágina", "bilingüe", "sin vencimiento"], emisor=N,
           destinatario=K["exportadora"], numero="IB-2026-0170", fecha=D(2026, 9, 25), sector="consultoria", count=36, seed="n15",
           due_days=None, titulo_en="Invoice", campos=[("Forma de pago", "contado")])


def main() -> None:
    for folder in FOLDERS:  # solo se regeneran las carpetas del corpus sintético
        shutil.rmtree(HERE / folder, ignore_errors=True)
    for build in (albaranes, presupuestos, proformas, pedidos, otros, ambiguos, ocr, multipagina, facturas):
        build()
    data = {
        "descripcion": f"{MARK}. Corpus sintético de evaluación documental. Generado por generar.py: no editar a mano.",
        "synthetic": True,
        "empresa": {"name": NOSOTROS.name, "tax_id": NOSOTROS.nif},
        "casos": CASES,
    }
    (HERE / "labels.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(CASES)} documentos en {HERE}")


if __name__ == "__main__":
    main()
