"""
Extractos bancarios sintéticos (SIMULACIÓN — NO OFICIAL) con sus facturas y la conciliación correcta.

    python evaluation/datasets/banco_sintetico/generar.py   # regenera los extractos y labels.json
    python -m evaluation banco                              # los pasa por la importación y la conciliación reales

Dos extractos de dos bancos con formatos distintos (normalización):

    cuenta_corriente.csv   Fecha;Concepto;Importe;Saldo         (importe con signo, 1.234,56)
    cuenta_ahorro.csv      F. Operación;Descripción;Cargo;Abono;Saldo

Las facturas (`labels.json → facturas`) se dan ya leídas: aquí se mide la conciliación, no la lectura de PDF
(esa se mide con `python -m evaluation --dataset catalogo`). NIF inventados (B00…), IBAN ficticio (ES00…).

La verdad (`labels.json → movimientos`) es la decisión que tomaría una persona con el extracto y las facturas
delante, con la regla de producto: solo se concilia sin persona lo inequívoco (importe exacto + una prueba de
identidad + una sola factura posible). Por movimiento:

    resultado   (o lista de resultados aceptables) conciliado | propuesta | importe_distinto | duplicado | sin_factura | justificado
    facturas    números de factura que explican el movimiento ([] si ninguna)
    auto        True: debe conciliarse solo · False: nunca solo · None: cualquiera de las dos es aceptable
    tipo        para «justificado»: COMISION, TRASPASO…
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

COMPANY = {"name": "EMPRESA EJEMPLO S.L.", "tax_id": "B00100016"}
IBAN_TRANSPORTES = "ES00 0000 0000 0000 0000 0042"  # ficticio

# Facturas ya registradas y aprobadas. sentido: RECIBIDA (pagamos) o EMITIDA (cobramos).
FACTURAS = [
    {"numero": "F-2026-101", "sentido": "RECIBIDA", "nombre": "PAPELERIA CENTRO S.L.", "nif": "B00001008", "fecha": "2026-08-20", "vence": "2026-09-19", "total": "145.20"},
    {"numero": "TR-5521", "sentido": "RECIBIDA", "nombre": "TRANSPORTES RAPIDOS NORTE S.L.", "nif": "B00001016", "fecha": "2026-08-25", "vence": "2026-09-24", "total": "726.00",
     "iban": IBAN_TRANSPORTES},
    {"numero": "TR-5530", "sentido": "RECIBIDA", "nombre": "TRANSPORTES RAPIDOS NORTE S.L.", "nif": "B00001016", "fecha": "2026-08-29", "vence": "2026-09-28", "total": "121.00",
     "iban": IBAN_TRANSPORTES},
    {"numero": "TR-5544", "sentido": "RECIBIDA", "nombre": "TRANSPORTES RAPIDOS NORTE S.L.", "nif": "B00001016", "fecha": "2026-09-08", "vence": "2026-10-08", "total": "484.00",
     "iban": IBAN_TRANSPORTES},
    {"numero": "SW-0091", "sentido": "RECIBIDA", "nombre": "SOFTWARE GESTION DIGITAL S.L.", "nif": "B00001024", "fecha": "2026-09-01", "vence": "2026-09-05", "total": "363.00"},
    {"numero": "LB-778", "sentido": "RECIBIDA", "nombre": "LIMPIEZAS BRILLO S.L.", "nif": "B00001032", "fecha": "2026-09-01", "vence": "2026-09-30", "total": "242.00"},
    {"numero": "AS-0907", "sentido": "RECIBIDA", "nombre": "ASESORIA NUMEROS CLAROS S.L.", "nif": "B00001040", "fecha": "2026-09-05", "vence": "2026-09-05", "total": "302.50"},
    {"numero": "SE-201", "sentido": "RECIBIDA", "nombre": "SUMINISTROS ELECTRICOS ALFA S.L.", "nif": "B00001057", "fecha": "2026-09-02", "vence": "2026-09-12", "total": "98.01"},
    {"numero": "SE-202", "sentido": "RECIBIDA", "nombre": "SUMINISTROS ELECTRICOS ALFA S.L.", "nif": "B00001057", "fecha": "2026-09-04", "vence": "2026-09-14", "total": "98.01"},
    {"numero": "MO-33", "sentido": "RECIBIDA", "nombre": "MOBILIARIO OFICINA PLUS S.L.", "nif": "B00001065", "fecha": "2026-06-10", "vence": "2026-07-10", "total": "1210.00"},
    {"numero": "E-2026-031", "sentido": "EMITIDA", "nombre": "CLIENTE ALFA S.L.", "nif": "B00001073", "fecha": "2026-08-28", "vence": "2026-09-27", "total": "1815.00"},
    {"numero": "E-2026-032", "sentido": "EMITIDA", "nombre": "CLIENTE BETA S.L.", "nif": "B00001081", "fecha": "2026-09-03", "vence": "2026-10-03", "total": "605.00"},
    {"numero": "E-2026-020", "sentido": "EMITIDA", "nombre": "CLIENTE GAMMA S.L.", "nif": "B00001099", "fecha": "2026-07-01", "vence": "2026-07-31", "total": "968.00"},
]

# (cuenta, fecha, concepto, importe, verdad)
MOVIMIENTOS = [
    ("corriente", "2026-09-01", "COMISION MANTENIMIENTO CUENTA", "-12.00",
     {"resultado": "justificado", "tipo": "COMISION", "facturas": [], "auto": None, "nota": "comisión: sin factura de proveedor"}),
    ("corriente", "2026-09-02", "RECIBO SEGURO MUTUA EJEMPLO POLIZA 778", "-85.40",
     {"resultado": "sin_factura", "facturas": [], "auto": False, "nota": "domiciliación sin factura registrada"}),
    ("corriente", "2026-09-04", "RECIBO SEGURO MUTUA EJEMPLO POLIZA 778", "-85.40",
     {"resultado": "duplicado", "facturas": [], "auto": False, "nota": "el mismo recibo cargado dos veces"}),
    ("corriente", "2026-09-05", "RECIBO SOFTWARE GESTION DIGITAL", "-363.00",
     {"resultado": "conciliado", "facturas": ["SW-0091"], "auto": True, "nota": "domiciliación: nombre + importe exacto + fecha del vencimiento"}),
    ("corriente", "2026-09-06", "PAGO TARJETA 4B ASESORIA", "-302.50",
     {"resultado": "conciliado", "facturas": ["AS-0907"], "auto": True, "nota": "nombre + importe exacto + fecha"}),
    ("corriente", "2026-09-08", "COMPRA TARJETA MARKETPLACE ONLINE", "-37.90",
     {"resultado": "sin_factura", "facturas": [], "auto": False, "nota": "gasto con tarjeta sin factura"}),
    ("corriente", "2026-09-12", "RECIBO SUMINISTROS ELECTRICOS", "-98.01",
     {"resultado": "propuesta", "facturas": ["SE-201", "SE-202"], "auto": False, "nota": "dos facturas iguales del mismo proveedor: decide una persona"}),
    ("corriente", "2026-09-15", "TRANSFERENCIA DE CLIENTE BETA B00001081", "605.00",
     {"resultado": "conciliado", "facturas": ["E-2026-032"], "auto": True, "nota": "cobro con el NIF del cliente"}),
    ("corriente", "2026-09-18", "TRANSFERENCIA PAPELERIA CENTRO F-2026-101", "-145.20",
     {"resultado": "conciliado", "facturas": ["F-2026-101"], "auto": True, "nota": "nº de factura en el concepto"}),
    ("corriente", "2026-09-20", "TRANSFERENCIA MOBILIARIO OFICINA PLUS", "-1210.00",
     {"resultado": "propuesta", "facturas": ["MO-33"], "auto": False, "nota": "nombre e importe, pero dos meses después del vencimiento"}),
    ("corriente", "2026-09-22", "TRANSFERENCIA PAPELERIA CENTRO F-2026-101", "-145.20",
     {"resultado": "duplicado", "facturas": [], "auto": False, "nota": "la misma factura pagada dos veces"}),
    ("corriente", "2026-09-24", f"TRANSF A {IBAN_TRANSPORTES} TRANSPORTES RAPIDOS NORTE", "-847.00",
     {"resultado": ["conciliado", "propuesta"], "facturas": ["TR-5521", "TR-5530"], "auto": None,
      "nota": "un pago por dos facturas del mismo proveedor (IBAN + suma exacta): conciliarlo solo o proponerlo son correctos"}),
    ("corriente", "2026-09-26", "INGRESO EFECTIVO", "200.00",
     {"resultado": "sin_factura", "facturas": [], "auto": False, "nota": "ingreso sin factura"}),
    ("corriente", "2026-09-28", "TRANSFERENCIA DE CLIENTE ALFA S.L. E-2026-031", "1815.00",
     {"resultado": "conciliado", "facturas": ["E-2026-031"], "auto": True, "nota": "cobro con nº de factura"}),
    ("corriente", "2026-09-29", "TRANSFERENCIA LIMPIEZAS BRILLO LB-778", "-240.00",
     {"resultado": "importe_distinto", "facturas": ["LB-778"], "auto": False, "nota": "se reconoce la factura, faltan 2,00 €"}),
    ("corriente", "2026-09-30", "TRASPASO A CUENTA AHORRO", "-500.00",
     {"resultado": "justificado", "tipo": "TRASPASO", "facturas": [], "auto": None, "nota": "traspaso entre cuentas propias"}),
    ("ahorro", "2026-09-30", "TRASPASO DESDE CUENTA CORRIENTE", "500.00",
     {"resultado": "justificado", "tipo": "TRASPASO", "facturas": [], "auto": None, "nota": "la otra pata del traspaso, en el otro banco"}),
    ("ahorro", "2026-09-30", "LIQUIDACION INTERESES", "0.42",
     {"resultado": "sin_factura", "facturas": [], "auto": False, "nota": "intereses: ingreso sin factura"}),
]
# Facturas que una persona marcaría como vencidas y sin pago con estos extractos. Una lista = «una de estas»:
# de las dos facturas iguales de SUMINISTROS solo se ha pagado una, y el extracto no dice cuál.
SIN_PAGO = ["E-2026-020", ["SE-201", "SE-202"]]


def es(amount: Decimal) -> str:
    """1234.5 → 1.234,50 (formato del banco)."""
    text = f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return text


def write_statements() -> dict[str, str]:
    files = {"corriente": "cuenta_corriente.csv", "ahorro": "cuenta_ahorro.csv"}
    balances = {"corriente": Decimal("18250.00"), "ahorro": Decimal("6000.00")}
    lines = {"corriente": ["Fecha;Concepto;Importe;Saldo"], "ahorro": ["F. Operación;Descripción;Cargo;Abono;Saldo"]}
    for account, when, concept, amount, _ in MOVIMIENTOS:
        value = Decimal(amount)
        balances[account] += value
        day = date.fromisoformat(when)
        if account == "corriente":
            lines[account].append(f"{day:%d/%m/%Y};{concept};{'-' if value < 0 else ''}{es(value)};{es(balances[account])}")
        else:
            charge, credit = (es(value), "") if value < 0 else ("", es(value))
            lines[account].append(f"{day:%d-%m-%Y};{concept};{charge};{credit};{es(balances[account])}")
    for account, name in files.items():
        (HERE / name).write_text("\n".join(lines[account]) + "\n", encoding="utf-8")
    return files


def main() -> None:
    files = write_statements()
    data = {
        "nota": f"{MARK}. Generado por generar.py: no editar a mano.",
        "empresa": COMPANY,
        "extractos": [{"cuenta": account, "fichero": name} for account, name in files.items()],
        "facturas": FACTURAS,
        "movimientos": [{"cuenta": account, "fecha": when, "concepto": concept, "importe": amount, "expected": truth}
                        for account, when, concept, amount, truth in MOVIMIENTOS],
        "facturas_sin_pago": SIN_PAGO,
    }
    (HERE / "labels.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(MOVIMIENTOS)} movimientos y {len(FACTURAS)} facturas en {HERE}")


if __name__ == "__main__":
    main()
