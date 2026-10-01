"""
Banco de expedientes sintéticos (conjuntos B y C) con su verdad de referencia.

Cada caso es una carpeta con documentos relacionados (PDF, .eml, CSV de
banco, eventos de plazo) y un «caso.json» que dice qué debería hacer el
sistema: tipo, ruta, trámite, afectado, plazo, importes, si necesita a una
persona, qué agentes, qué hallazgos y qué documentos pide.

La verdad se escribe a partir del procedimiento real (lo que un gestor
haría con ese documento), no a partir de lo que hace hoy CapaFiscal.

    python -m evaluation generar-b              # banco B (se versiona)
    python -m evaluation generar-c --semilla N  # banco C ciego (no se versiona)

Todos los documentos llevan «SIMULACIÓN — NO OFICIAL» y datos inventados.
Los NIF de empresa usan el prefijo provincial 00, que no existe: tienen
dígito de control válido pero no pueden coincidir con una empresa real.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from dataclasses import dataclass
from dataclasses import replace
from datetime import date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Callable

from evaluation import plantillas as P
from evaluation.documentos import MARK
from evaluation.documentos import Noise
from evaluation.documentos import csv_code
from evaluation.documentos import eur
from evaluation.documentos import fecha
from evaluation.documentos import render_admin
from evaluation.documentos import render_invoice
from evaluation.documentos import scan
from evaluation.documentos import write_bank_csv
from evaluation.documentos import write_eml
from evaluation.plantillas import Party
from evaluation.plantillas import business_days_after
from evaluation.plantillas import cif
from evaluation.plantillas import dni

D = Decimal
ROOT = Path(__file__).resolve().parent / "datasets"
B_DIR = ROOT / "b_sintetico"
C_DIR = ROOT / "c_ciego"

BLOCKS = {
    "aeat": "Bloque 1 · Requerimientos AEAT",
    "embargos": "Bloque 2 · Embargos",
    "seguridad_social": "Bloque 3 · Seguridad Social",
    "normales": "Bloque 4 · Documentos normales",
    "memoria": "Bloque 5 · Memoria",
    "facturas": "Bloque 6 · Facturas difíciles",
    "correo": "Correo",
    "adversarial": "Adversariales",
    "apoyo": "Apoyo (banco y plazos)",
}


@dataclass(frozen=True)
class World:
    """Las partes de un banco: la empresa, sus proveedores y su plantilla."""
    company: Party
    supplier: Party  # suministros: embargo de créditos, expediente de memoria
    carrier: Party  # transportes: pagos sucesivos, duplicado, correo
    cleaner: Party  # limpieza: sin deuda pendiente, correo
    software: Party  # rectificativa
    advisor: Party  # persona física con retención
    employee: Party
    unknown: Party
    ccc: str


def world_b() -> World:
    return World(
        company=Party("Talleres Ejemplo del Arlanza S.L.", cif("B", "0010001"), "Pol. Ind. Ficticio, parcela 7, 09001 Burgos", "admin@talleres-ejemplo.test"),
        supplier=Party("Suministros Ficticios Duero S.L.", cif("B", "0020002"), "C/ Inventada 12, 47001 Valladolid", "facturas@suministros-duero.test", "Valladolid"),
        carrier=Party("Transportes Simulados Esla S.L.", cif("B", "0030003"), "Av. Imaginaria 4, 24001 León", "admin@transportes-esla.test", "León"),
        cleaner=Party("Limpiezas Imaginarias Ubierna S.L.", cif("B", "0040004"), "C/ Ficticia 8, 09003 Burgos", "cobros@limpiezas-ubierna.test"),
        software=Party("Programas Ficticios Vena S.L.", cif("B", "0050005"), "C/ Simulada 21, 28001 Madrid", "facturacion@programas-vena.test", "Madrid"),
        advisor=Party("Laura Inventada Martín", dni(12345), "C/ Ejemplo 3, 2º, 09002 Burgos", "laura@asesoria-inventada.test"),
        employee=Party("Pedro Ficticio Gómez", dni(54321), "C/ Supuesta 5, 09004 Burgos"),
        unknown=Party("Comercial Desconocida Ejemplo S.L.", "B99999997", "C/ Nadie 1, 34001 Palencia"),
        ccc="09 0123456 78 (simulado)",
    )


# ---------------------------------------------------------------------
# Construcción
# ---------------------------------------------------------------------


class Case:
    """Una carpeta de expediente: documentos, contexto, entradas y verdad."""

    def __init__(self, root: Path, case_id: str, *, block: str, title: str, description: str, tags: list[str], world: World):
        self.id = case_id
        self.dir = root / case_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.world = world
        self.data: dict[str, Any] = {
            "case_id": case_id,
            "conjunto": case_id[0] if case_id[0] in "BC" else "B",
            "bloque": block,
            "titulo": title,
            "descripcion": description,
            "etiquetas": tags,
            "marca": MARK,
            "documents": [],
            "contexto": {"empresa": {"name": world.company.name, "tax_id": world.company.tax_id, "email": world.company.email, "address": world.company.address}},
            "entradas": [],
            "expected": {},
        }

    # -- documentos
    def admin(self, name: str, *, organism: str, area: str, blocks: list[dict], kind: str, noise: Noise | None = None, note: str = "") -> str:
        render_admin(self.dir / name, organism=organism, area=area, blocks=blocks, seed=f"{self.id}/{name}", noise=noise)
        self.data["documents"].append({"archivo": name, "tipo": kind, "nota": note})
        return name

    def invoice(self, name: str, *, supplier: Party, number: str, issued: date, lines: list[tuple[str, Decimal, Decimal]], kind: str = "factura", note: str = "",
                customer: Party | None = None, **options: Any) -> dict[str, Any]:
        customer = customer or self.world.company
        amounts = render_invoice(self.dir / name, supplier=supplier.as_dict(), customer=customer.as_dict(), number=number, issued=issued, lines=lines,
                                 seed=f"{self.id}/{name}", **options)
        self.data["documents"].append({"archivo": name, "tipo": kind, "nota": note})
        return {
            "direction": "RECEIVED", "supplier_tax_id": supplier.tax_id, "customer_tax_id": customer.tax_id, "invoice_number": number,
            "invoice_date": issued.isoformat(), **{key: value for key, value in amounts.items() if value is not None},
        }

    def scanned(self, source: str, name: str) -> str:
        (self.dir / name).write_bytes(scan((self.dir / source).read_bytes(), f"{self.id}/{name}"))
        (self.dir / source).unlink()
        for item in self.data["documents"]:
            if item["archivo"] == source:
                item["archivo"] = name
                item["nota"] = (item["nota"] + " Escaneado: sin texto seleccionable.").strip()
        return name

    def eml(self, name: str, *, sender: Party, subject: str, body: str, attachments: list[str], message_id: str, sent: str) -> str:
        write_eml(self.dir / name, sender=f"{sender.name} <{sender.email}>", to=f"{self.world.company.name} <{self.world.company.email}>", subject=subject, body=body,
                  message_id=message_id, sent=sent, attachments=[self.dir / item for item in attachments])
        self.data["documents"].append({"archivo": name, "tipo": "correo", "nota": f"Adjuntos: {', '.join(attachments)}"})
        return name

    def bank(self, name: str, rows: list[tuple[date, str, Decimal]]) -> str:
        write_bank_csv(self.dir / name, rows)
        self.data["documents"].append({"archivo": name, "tipo": "extracto_bancario", "nota": "CSV con marca de simulación en la cabecera de la carpeta"})
        return name

    # -- contexto
    def prior_invoice(self, supplier: Party, number: str, issued: date, base: Decimal, *, paid: date | None = None, vat: Decimal = D("21")) -> None:
        tax = (base * vat / 100).quantize(D("0.01"))
        self.data["contexto"].setdefault("facturas", []).append({
            "supplier_name": supplier.name, "supplier_tax_id": supplier.tax_id, "invoice_number": number, "invoice_date": issued.isoformat(),
            "subtotal": f"{base:.2f}", "tax_total": f"{tax:.2f}", "total": f"{base + tax:.2f}", "paid_at": paid.isoformat() if paid else None,
        })

    def employee(self, party: Party) -> None:
        first, *rest = party.name.split(" ")
        self.data["contexto"].setdefault("empleados", []).append({"first_name": first, "last_name": " ".join(rest), "tax_id": party.tax_id, "hire_date": "2024-02-01"})

    def filing(self, model: str, year: int, quarter: int, filed: date, amount: Decimal) -> None:
        self.data["contexto"].setdefault("presentaciones", []).append({"model": model, "year": year, "period": quarter, "filed_at": filed.isoformat(), "amount": f"{amount:.2f}"})

    # -- entradas
    def upload(self, name: str, expected: dict[str, Any]) -> None:
        self.data["entradas"].append({"tipo": "subida", "archivo": name, "expected": expected})

    def mail(self, name: str, expected_attachments: list[dict[str, Any]]) -> None:
        self.data["entradas"].append({"tipo": "correo", "archivo": name, "expected_adjuntos": expected_attachments})

    def deadline(self, model: str, year: int, quarter: int, due: date, expected: dict[str, Any]) -> None:
        name = f"plazo_{model}_{year}_{quarter}T.json"
        event = {"kind": "deadline", "source": "calendario-simulado", "external_id": f"{self.id}:{model}:{year}-{quarter}", "model": model, "year": year, "quarter": quarter, "due": due.isoformat()}
        (self.dir / name).write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.data["documents"].append({"archivo": name, "tipo": "evento_plazo", "nota": "Evento estructurado, no PDF"})
        self.data["entradas"].append({"tipo": "plazo", "archivo": name, "expected": expected})

    def bank_import(self, name: str) -> None:
        self.data["entradas"].append({"tipo": "banco", "archivo": name})

    def scan_anomalies(self, expected: dict[str, Any]) -> None:
        self.data["entradas"].append({"tipo": "analisis", "expected": expected})

    def finish(self, **expected: Any) -> dict[str, Any]:
        self.data["expected"] = expected
        (self.dir / "caso.json").write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.data


def notification(**values: Any) -> dict[str, Any]:
    """Verdad de una notificación: lo que no se dice no se comprueba."""
    return {"type": "NOTIFICATION", **values}


NOTIFICATION_AGENTS = ["vigilante", "expedientes", "fiscal", "gestor", "director"]
EMBARGO_AGENTS = ["vigilante", "expedientes", "gestor", "director"]


# ---------------------------------------------------------------------
# Banco B
# ---------------------------------------------------------------------


def b01(root: Path, w: World) -> dict:
    c = Case(root, "B01", block="aeat", title="Comprobación limitada IVA 2T 2026", description="Requerimiento limpio: expediente, NIF, impuesto, periodo, documentación, 10 días hábiles, órgano y CSV ficticio.", tags=["limpio"], world=w)
    acceso = date(2026, 9, 28)
    doc = c.admin("requerimiento_iva_2T.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente="202609SIM0001234A", documento="A2360926SIM00451", emitido=date(2026, 9, 24), impuesto="Impuesto sobre el Valor Añadido", ejercicio=2026, periodo="2T",
        pide=["Libro registro de facturas recibidas del periodo.", "Facturas recibidas de importe superior a 1.000 euros.", "Justificantes de pago de dichas facturas (extractos bancarios o transferencias)."],
        puesta=date(2026, 9, 25), acceso=acceso))
    c.upload(doc, notification(
        route="requerimiento", procedure="COMPROBACION_LIMITADA", issuer="AEAT", reference="202609SIM0001234A", deadline=business_days_after(acceso, 10).isoformat(),
        requires_human=True, expected_agents=NOTIFICATION_AGENTS, tax_reference={"model": "303", "year": 2026, "quarter": 2},
        requested_documents=["LIBRO_RECIBIDAS", "FACTURAS", "JUSTIFICANTE_PAGO"],
    ))
    return c.finish()


def b02(root: Path, w: World) -> dict:
    c = Case(root, "B02", block="aeat", title="Requerimiento que menciona «factura» muchas veces", description="La palabra «factura» aparece unas 18 veces porque el requerimiento pide facturas concretas. Sigue siendo un requerimiento, no una factura.", tags=["ambiguo", "adversarial"], world=w)
    acceso = date(2026, 9, 21)
    numbers = [f"FV-2026-{index:03d}" for index in range(101, 113)]
    table = {"tipo": "tabla", "cabecera": ["Factura nº", "Proveedor", "Fecha factura", "Base factura"], "anchos": [35, 70, 30, 35], "filas": [
        [number, w.supplier.name, fecha(date(2026, 4, 3) + timedelta(days=6 * index)), eur(D(950) + index * 37)] for index, number in enumerate(numbers)
    ]}
    doc = c.admin("requerimiento_facturas.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente="202609SIM0002345B", documento="A2360926SIM00452", emitido=date(2026, 9, 17), impuesto="Impuesto sobre el Valor Añadido", ejercicio=2026, periodo="2T",
        pide=["Original o copia de cada factura recibida relacionada en el anexo (12 facturas).", "Justificante de pago de cada factura del anexo.", "Libro registro de facturas recibidas donde consten anotadas dichas facturas."],
        puesta=date(2026, 9, 18), acceso=acceso,
        extra=[{"tipo": "apartado", "texto": "Anexo: facturas recibidas objeto de comprobación"}, table,
               {"tipo": "pequeno", "texto": "Para cada factura indique si la factura fue rectificada y aporte, en su caso, la factura rectificativa. Las facturas deberán cumplir el Reglamento de facturación (RD 1619/2012)."}]))
    c.upload(doc, notification(
        route="requerimiento", procedure="COMPROBACION_LIMITADA", issuer="AEAT", reference="202609SIM0002345B", deadline=business_days_after(acceso, 10).isoformat(),
        requires_human=True, requested_documents=["FACTURAS", "JUSTIFICANTE_PAGO", "LIBRO_RECIBIDAS"], not_invoice=True,
    ))
    return c.finish()


def b03(root: Path, w: World) -> dict:
    c = Case(root, "B03", block="aeat", title="Propuesta de liquidación provisional", description="Trámite de alegaciones con importes declarado/comprobado/diferencia. No es un requerimiento ni una liquidación (y menciona la autoliquidación).", tags=["limpio", "ambiguo"], world=w)
    acceso = date(2026, 9, 22)
    declarado, comprobado, intereses = D("1840.00"), D("3125.40"), D("38.62")
    doc = c.admin("propuesta_liquidacion.pdf", organism="AEAT", area="Gestión Tributaria", kind="propuesta_liquidacion", blocks=P.propuesta_liquidacion(
        party=w.company, expediente="202609SIM0003456C", documento="A2360926SIM00453", emitido=date(2026, 9, 18), ejercicio=2026, periodo="1T",
        declarado=declarado, comprobado=comprobado, intereses=intereses, puesta=date(2026, 9, 19), acceso=acceso))
    c.upload(doc, notification(
        route="requerimiento", procedure="PROPUESTA_LIQUIDACION", procedure_not=["REQUERIMIENTO", "LIQUIDACION", "COMPROBACION_LIMITADA"], issuer="AEAT",
        reference="202609SIM0003456C", deadline=business_days_after(acceso, 10).isoformat(), debt_amount=float(comprobado - declarado + intereses),
        requires_human=True, tax_reference={"model": "303", "year": 2026, "quarter": 1},
    ))
    return c.finish()


def b04(root: Path, w: World) -> dict:
    c = Case(root, "B04", block="aeat", title="Requerimiento sin fecha de notificación", description="No trae diligencia de notificación: el plazo no se puede calcular con certeza. Lo correcto es no inventarlo o marcarlo como estimado y pedir la fecha.", tags=["ambiguo"], world=w)
    doc = c.admin("requerimiento_sin_notificacion.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente="202609SIM0004567D", documento="A2360926SIM00454", emitido=date(2026, 9, 23), impuesto="Impuesto sobre el Valor Añadido", ejercicio=2026, periodo="2T",
        pide=["Libro registro de facturas emitidas del periodo.", "Contratos de arrendamiento vigentes."], puesta=None, acceso=None))
    c.upload(doc, notification(route="requerimiento", procedure="COMPROBACION_LIMITADA", issuer="AEAT", deadline_pending_confirmation=True, requires_human=True,
                               requested_documents=["LIBRO_EMITIDAS", "CONTRATOS"]))
    return c.finish()


def b05(root: Path, w: World) -> dict:
    c = Case(root, "B05", block="embargos", title="Embargo de créditos: crédito mayor que la deuda", description="Debemos 4.100 € al proveedor embargado; su deuda con Hacienda es 2.850 €. Solo se retienen 2.850 €.", tags=["limpio", "expediente"], world=w)
    c.prior_invoice(w.supplier, "SFD-2026-071", date(2026, 8, 4), D("1900.83"))  # 2.300,00 con IVA
    c.prior_invoice(w.supplier, "SFD-2026-083", date(2026, 9, 1), D("1487.60"))  # 1.800,00 con IVA
    acceso = date(2026, 9, 29)
    doc = c.admin("embargo_creditos.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.supplier, diligencia="DE-2026-SIM-55120", emitido=date(2026, 9, 25), principal=D("2375.00"), recargo=D("475.00"), intereses=D("0.00"), costas=D("0.00"), acceso=acceso))
    c.upload(doc, notification(
        route="embargo", procedure="EMBARGO_CREDITOS", issuer="AEAT", affected={"type": "supplier", "tax_id": w.supplier.tax_id}, debt_amount=2850.00, credit_amount=4100.00,
        deadline=business_days_after(acceso, 5).isoformat(), requires_human=True, expected_agents=EMBARGO_AGENTS, expected_findings=["credit_exceeds_debt"],
        requested_documents=["RELACION_CREDITOS"],
    ))
    return c.finish()


def b06(root: Path, w: World) -> dict:
    c = Case(root, "B06", block="embargos", title="Embargo de pagos sucesivos", description="Servicio mensual de 1.250 €; deuda 6.200 €. Hay que retener cada pago mensual hasta completar la deuda, no solo el pendiente de hoy.", tags=["limpio"], world=w)
    for month in (6, 7, 8):
        c.prior_invoice(w.carrier, f"TSE-2026-{month:02d}", date(2026, month, 30), D("1033.06"), paid=date(2026, month + 1, 10))
    c.prior_invoice(w.carrier, "TSE-2026-09", date(2026, 9, 30), D("1033.06"))
    acceso = date(2026, 9, 30)
    doc = c.admin("embargo_pagos_sucesivos.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.carrier, diligencia="DE-2026-SIM-55233", emitido=date(2026, 9, 28), principal=D("5000.00"), recargo=D("1000.00"), intereses=D("150.00"), costas=D("50.00"),
        sucesivos=True, acceso=acceso))
    c.upload(doc, notification(
        route="embargo", procedure="EMBARGO_CREDITOS", affected={"type": "supplier", "tax_id": w.carrier.tax_id}, debt_amount=6200.00, credit_amount=1250.00,
        deadline=business_days_after(acceso, 5).isoformat(), requires_human=True, expected_findings=["successive_payments"],
    ))
    return c.finish()


def b07(root: Path, w: World) -> dict:
    c = Case(root, "B07", block="embargos", title="Embargo sin pago pendiente", description="El proveedor embargado está pagado al día: hay que contestar que no existen créditos.", tags=["limpio"], world=w)
    c.prior_invoice(w.cleaner, "LIU-0712", date(2026, 7, 31), D("400.00"), paid=date(2026, 8, 5))
    c.prior_invoice(w.cleaner, "LIU-0831", date(2026, 8, 31), D("400.00"), paid=date(2026, 9, 4))
    acceso = date(2026, 9, 29)
    doc = c.admin("embargo_sin_deuda.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.cleaner, diligencia="DE-2026-SIM-55301", emitido=date(2026, 9, 24), principal=D("1283.33"), recargo=D("256.67"), intereses=D("0"), costas=D("0"), acceso=acceso))
    c.upload(doc, notification(
        route="embargo", procedure="EMBARGO_CREDITOS", affected={"type": "supplier", "tax_id": w.cleaner.tax_id}, debt_amount=1540.00, credit_amount=0.0,
        requires_human=True, expected_findings=["no_pending_payment"],
    ))
    return c.finish()


def b08(root: Path, w: World) -> dict:
    c = Case(root, "B08", block="embargos", title="Embargo de salario", description="El embargado es una persona de la plantilla, no un proveedor: retención en nómina (art. 607 LEC), sin relación de créditos.", tags=["limpio"], world=w)
    c.employee(w.employee)
    acceso = date(2026, 9, 29)
    doc = c.admin("embargo_salario.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_salarios(
        pagador=w.company, empleado=w.employee, diligencia="DS-2026-SIM-11872", emitido=date(2026, 9, 25), total=D("3410.55"), acceso=acceso))
    c.upload(doc, notification(
        route="embargo", procedure="EMBARGO_SALARIOS", affected={"type": "employee", "tax_id": w.employee.tax_id}, debt_amount=3410.55, requires_human=True,
        requested_documents_excluded=["RELACION_CREDITOS"], insight_contains=["607"],
    ))
    return c.finish()


def b09(root: Path, w: World) -> dict:
    c = Case(root, "B09", block="seguridad_social", title="TGSS: requerimiento de documentación", description="Requerimiento de la Tesorería con código de cuenta de cotización: nóminas, contratos y RNT.", tags=["limpio"], world=w)
    acceso = date(2026, 9, 24)
    doc = c.admin("tgss_requerimiento.pdf", organism="TGSS", area="Burgos (simulada)", kind="requerimiento", blocks=P.tgss_requerimiento(
        party=w.company, ccc=w.ccc, expediente="TGSS-SIM-2026-0042", emitido=date(2026, 9, 22),
        pide=["Nóminas de julio y agosto de 2026 de toda la plantilla.", "Contratos de trabajo de las altas producidas en 2026.", "Relación nominal de trabajadores (RNT) de agosto de 2026."], acceso=acceso))
    c.upload(doc, notification(route="requerimiento", procedure="REQUERIMIENTO", issuer="TGSS", deadline=business_days_after(acceso, 10).isoformat(), requires_human=True,
                               requested_documents=["NOMINAS", "CONTRATOS_TRABAJO", "RNT"]))
    return c.finish()


def b10(root: Path, w: World) -> dict:
    c = Case(root, "B10", block="seguridad_social", title="TGSS: reclamación de deuda", description="Reclamación de cuotas no ingresadas: es un pago con plazo, no un requerimiento de documentación.", tags=["limpio", "ambiguo"], world=w)
    acceso = date(2026, 8, 19)
    doc = c.admin("tgss_reclamacion_deuda.pdf", organism="TGSS", area="Burgos (simulada)", kind="reclamacion_deuda", blocks=P.tgss_reclamacion(
        party=w.company, ccc=w.ccc, documento="RD-SIM-2026-778812", emitido=date(2026, 8, 14), periodo="06/2026", principal=D("2940.18"), recargo=D("588.04"), acceso=acceso))
    c.upload(doc, notification(route="requerimiento", procedure="LIQUIDACION", procedure_not=["REQUERIMIENTO"], issuer="TGSS", debt_amount=3528.22, deadline="2026-09-30", requires_human=True))
    return c.finish()


def b11(root: Path, w: World) -> dict:
    c = Case(root, "B11", block="seguridad_social", title="TGSS: comunicación laboral sin impacto fiscal", description="Resolución informativa de alta. No afecta a ningún modelo fiscal ni requiere actuación.", tags=["limpio"], world=w)
    doc = c.admin("tgss_comunicacion_alta.pdf", organism="TGSS", area="Burgos (simulada)", kind="comunicacion", blocks=P.tgss_comunicacion_laboral(
        party=w.company, ccc=w.ccc, emitido=date(2026, 9, 15), trabajador=w.employee.name))
    c.upload(doc, notification(issuer="TGSS", procedure="COMUNICACION", fiscal_applies=False, requires_human=False, deadline=None))
    return c.finish()


def b12(root: Path, w: World) -> dict:
    c = Case(root, "B12", block="normales", title="Certificado sin acción", description="Certificado de estar al corriente: se archiva, no hay nada que hacer.", tags=["limpio"], world=w)
    doc = c.admin("certificado_corriente.pdf", organism="AEAT", area="Gestión Tributaria", kind="certificado", blocks=P.certificado_corriente(party=w.company, emitido=date(2026, 9, 10), numero="CERT-SIM-2026-90021"))
    c.upload(doc, {"requires_human": False, "deadline": None, "not_invoice": True})
    return c.finish()


def b13(root: Path, w: World) -> dict:
    c = Case(root, "B13", block="normales", title="Justificante de presentación del 303", description="Tras subirlo, la memoria debe poder responder «¿está presentado el 303 del 2T?» y el periodo debería constar presentado.", tags=["limpio"], world=w)
    doc = c.admin("justificante_303_2T.pdf", organism="AEAT", area="Gestión Tributaria", kind="justificante_presentacion", blocks=P.justificante_303(
        party=w.company, ejercicio=2026, trimestre=2, presentado=date(2026, 7, 17), resultado=D("1984.37"), csv=csv_code("B13")))
    c.upload(doc, {"requires_human": False, "deadline": None, "not_invoice": True})
    return c.finish(filed_period={"model": "303", "year": 2026, "quarter": 2},
                    memory={"question": "¿Está presentado el modelo 303 del segundo trimestre de 2026?", "source_contains": "303"})


def b14(root: Path, w: World) -> dict:
    c = Case(root, "B14", block="normales", title="Notificación a un NIF desconocido", description="Va dirigida a una empresa que no es la nuestra (NIF B99999997, válido pero desconocido). No hay que inventar el cliente: a revisión.", tags=["ambiguo"], world=w)
    doc = c.admin("requerimiento_nif_desconocido.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.unknown, expediente="202609SIM0009999Z", documento="A2360926SIM00999", emitido=date(2026, 9, 21), impuesto="Impuesto sobre Sociedades", ejercicio=2025, periodo="0A",
        pide=["Cuentas anuales del ejercicio 2025."], puesta=date(2026, 9, 22), acceso=date(2026, 9, 23)))
    c.upload(doc, notification(issuer="AEAT", requires_human=True, addressee_unknown=w.unknown.tax_id))
    return c.finish()


def _memory_case(root: Path, w: World, case_id: str, *, missing: bool) -> dict:
    title = "Requerimiento con facturas y 303 presentado" + (" (falta la factura 003)" if missing else "")
    description = ("El requerimiento pide tres facturas concretas y el 303 del 2T. " +
                   ("Solo existen la 001 y la 002: el Perseguidor debe pedir la 003." if missing else "Todo está en el sistema: la Memoria debe encontrarlo y el Fiscal ver el 303 presentado."))
    c = Case(root, case_id, block="memoria", title=title, description=description, tags=["expediente"], world=w)
    c.filing("303", 2026, 2, date(2026, 7, 17), D("1984.37"))
    expected_invoices = []
    numbers = ["SFD-2026-001", "SFD-2026-002"] + ([] if missing else ["SFD-2026-003"])
    for index, number in enumerate(numbers):
        name = f"factura_{number}.pdf"
        truth = c.invoice(name, supplier=w.supplier, number=number, issued=date(2026, 4, 8) + timedelta(days=21 * index), lines=[("Material de taller (lote)", D(1), D(820) + 115 * index)])
        c.upload(name, {"type": "INVOICE", "invoice": truth})
        expected_invoices.append(truth)
    acceso = date(2026, 9, 28)
    doc = c.admin("requerimiento_facturas_303.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente=f"202609SIM00{case_id[1:]}777E", documento=f"A2360926SIM00{case_id[1:]}7", emitido=date(2026, 9, 24), impuesto="Impuesto sobre el Valor Añadido",
        ejercicio=2026, periodo="2T",
        pide=[f"Facturas recibidas de {w.supplier.name} números SFD-2026-001, SFD-2026-002 y SFD-2026-003.", "Justificante de presentación del modelo 303 del periodo 2T de 2026."],
        puesta=date(2026, 9, 25), acceso=acceso))
    expected = notification(route="requerimiento", procedure="COMPROBACION_LIMITADA", deadline=business_days_after(acceso, 10).isoformat(), requires_human=True,
                            requested_documents=["FACTURAS", "MODELOS"], expected_findings=["PERIODO_PRESENTADO"], tax_reference={"model": "303", "year": 2026, "quarter": 2})
    if missing:
        expected.update(expected_agents=["memoria", "perseguidor"], document_status={"FACTURAS": ["partial", "missing", "requested"]}, insight_contains=["SFD-2026-003"])
    else:
        expected.update(expected_agents=["memoria"], document_status={"FACTURAS": ["ready", "received", "provided"]})
    c.upload(doc, expected)
    return c.finish(memory={"question": "¿Tenemos la factura SFD-2026-002 de Suministros Ficticios Duero?", "source_contains": "SFD-2026-002"})


def b15(root: Path, w: World) -> dict:
    return _memory_case(root, w, "B15", missing=False)


def b16(root: Path, w: World) -> dict:
    return _memory_case(root, w, "B16", missing=True)


def b17(root: Path, w: World) -> dict:
    c = Case(root, "B17", block="memoria", title="Factura duplicada FAC-2026-145", description="La misma factura llega dos veces en archivos distintos (otro PDF, mismo contenido fiscal). El Detector debe avisar.", tags=["expediente"], world=w)
    first = c.invoice("FAC-2026-145.pdf", supplier=w.carrier, number="FAC-2026-145", issued=date(2026, 9, 12), lines=[("Portes nacionales septiembre", D(1), D("1240.00"))])
    c.invoice("FAC-2026-145_copia.pdf", supplier=w.carrier, number="FAC-2026-145", issued=date(2026, 9, 12), lines=[("Portes nacionales septiembre", D(1), D("1240.00"))],
              noise=Noise(rotated_margin="Copia enviada de nuevo a petición del cliente"), note="Segundo envío de la misma factura")
    c.upload("FAC-2026-145.pdf", {"type": "INVOICE", "invoice": first, "duplicate": False})
    c.upload("FAC-2026-145_copia.pdf", {"type": "INVOICE", "invoice": first, "duplicate": True, "expected_findings": ["POSIBLE_DUPLICADO"], "requires_human": True})
    return c.finish()


def b18(root: Path, w: World) -> dict:
    c = Case(root, "B18", block="facturas", title="Factura rectificativa", description="Una factura y su rectificativa (abono parcial). La rectificativa no es un duplicado y su importe es negativo.", tags=["limpio"], world=w)
    original = c.invoice("factura_2026-0412.pdf", supplier=w.software, number="2026-0412", issued=date(2026, 9, 2), lines=[("Licencia anual de gestión de taller", D(1), D("1000.00"))])
    rect = c.invoice("rectificativa_R-2026-0031.pdf", supplier=w.software, number="R-2026-0031", issued=date(2026, 9, 16), lines=[("Abono por precio unitario erróneo", D(-1), D("200.00"))],
                     rectifies="2026-0412", kind="factura_rectificativa")
    c.upload("factura_2026-0412.pdf", {"type": "INVOICE", "invoice": original})
    c.upload("rectificativa_R-2026-0031.pdf", {"type": "INVOICE", "invoice": rect, "duplicate": False, "findings_excluded": ["POSIBLE_DUPLICADO"]})
    return c.finish()


def b19(root: Path, w: World) -> dict:
    c = Case(root, "B19", block="facturas", title="Factura con retención de IRPF", description="Profesional persona física: base, IVA 21 %, retención 15 % y total.", tags=["limpio"], world=w)
    truth = c.invoice("honorarios_A-26-118.pdf", supplier=w.advisor, number="A-26-118", issued=date(2026, 9, 30), lines=[("Honorarios de asesoría laboral, septiembre", D(1), D("800.00"))],
                      irpf_rate=D("15"), legal_footer=False)
    c.upload("honorarios_A-26-118.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def b20(root: Path, w: World) -> dict:
    c = Case(root, "B20", block="facturas", title="Factura en tres páginas", description="Detalle en la primera, resumen partido entre la segunda y la tercera.", tags=["multipagina"], world=w)
    lines = [(f"Referencia {index:03d} · recambio de taller", D(index % 3 + 1), D("12.50") + index) for index in range(1, 19)]
    truth = c.invoice("factura_multipagina_SFD-2026-120.pdf", supplier=w.supplier, number="SFD-2026-120", issued=date(2026, 9, 18), lines=lines, pages=3)
    c.upload("factura_multipagina_SFD-2026-120.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def b21(root: Path, w: World) -> dict:
    c = Case(root, "B21", block="correo", title="Correo con factura", description="Un correo de proveedor con la factura en PDF.", tags=["limpio"], world=w)
    truth = c.invoice("LIU-0930.pdf", supplier=w.cleaner, number="LIU-0930", issued=date(2026, 9, 30), lines=[("Limpieza de instalaciones, septiembre", D(1), D("400.00"))])
    mail = c.eml("factura_septiembre.eml", sender=w.cleaner, subject="Factura LIU-0930", body="Buenos días,\n\nAdjuntamos la factura de septiembre.\n\nUn saludo.",
                 attachments=["LIU-0930.pdf"], message_id="b21-liu-0930@limpiezas-ubierna.test", sent="Wed, 30 Sep 2026 09:14:00 +0200")
    c.mail(mail, [{"type": "INVOICE", "invoice": truth, "duplicate": False}])
    return c.finish()


def b22(root: Path, w: World) -> dict:
    c = Case(root, "B22", block="correo", title="El mismo correo reenviado", description="El mismo adjunto llega en un reenvío: no debe procesarse dos veces.", tags=["expediente"], world=w)
    truth = c.invoice("LIU-0930.pdf", supplier=w.cleaner, number="LIU-0930", issued=date(2026, 9, 30), lines=[("Limpieza de instalaciones, septiembre", D(1), D("400.00"))])
    first = c.eml("original.eml", sender=w.cleaner, subject="Factura LIU-0930", body="Adjuntamos la factura de septiembre.", attachments=["LIU-0930.pdf"],
                  message_id="b22-liu-0930@limpiezas-ubierna.test", sent="Wed, 30 Sep 2026 09:14:00 +0200")
    second = c.eml("reenviado.eml", sender=w.cleaner, subject="RV: Factura LIU-0930", body="Os la reenvío por si no os llegó.\n\n---- Mensaje original ----\nAdjuntamos la factura de septiembre.",
                   attachments=["LIU-0930.pdf"], message_id="b22-rv-liu-0930@limpiezas-ubierna.test", sent="Thu, 01 Oct 2026 08:02:00 +0200")
    c.mail(first, [{"type": "INVOICE", "invoice": truth, "duplicate": False}])
    c.mail(second, [{"type": "INVOICE", "duplicate": True}])
    return c.finish()


def b23(root: Path, w: World) -> dict:
    c = Case(root, "B23", block="correo", title="RE: Factura septiembre con original y rectificativa", description="Un hilo de respuesta con dos adjuntos: la factura original y su rectificativa.", tags=["expediente", "contradictorio"], world=w)
    original = c.invoice("TSE-2026-09B.pdf", supplier=w.carrier, number="TSE-2026-09B", issued=date(2026, 9, 25), lines=[("Portes adicionales septiembre", D(1), D("640.00"))])
    rect = c.invoice("TSE-R-2026-004.pdf", supplier=w.carrier, number="TSE-R-2026-004", issued=date(2026, 9, 29), lines=[("Abono: portes facturados por error", D(-1), D("140.00"))],
                     rectifies="TSE-2026-09B", kind="factura_rectificativa")
    mail = c.eml("re_factura_septiembre.eml", sender=w.carrier, subject="RE: Factura septiembre",
                 body="Hola,\n\nTenéis razón, había un error. Os mando otra vez la factura y la rectificativa.\n\n> El 26/09/2026 escribisteis:\n> La factura de septiembre tiene portes que no son nuestros.",
                 attachments=["TSE-2026-09B.pdf", "TSE-R-2026-004.pdf"], message_id="b23-re-sept@transportes-esla.test", sent="Tue, 29 Sep 2026 17:40:00 +0200")
    c.mail(mail, [{"type": "INVOICE", "invoice": original, "duplicate": False}, {"type": "INVOICE", "invoice": rect, "duplicate": False, "findings_excluded": ["POSIBLE_DUPLICADO"]}])
    return c.finish()


# -- Adversariales


def adv01(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV01", block="adversarial", title="Embargo que enumera facturas", description="Diligencia de embargo que relaciona las facturas pendientes del deudor: «factura» aparece muchas veces y hay importes y fechas.", tags=["adversarial", "ambiguo"], world=w)
    for index in range(3):
        c.prior_invoice(w.software, f"2026-05{index}0", date(2026, 7, 10) + timedelta(days=15 * index), D("500.00"))
    acceso = date(2026, 9, 28)
    table = {"tipo": "tabla", "cabecera": ["Factura", "Fecha factura", "Importe factura"], "anchos": [45, 40, 45], "filas": [
        [f"Factura nº 2026-05{index}0", fecha(date(2026, 7, 10) + timedelta(days=15 * index)), eur(D("605.00"))] for index in range(3)]}
    doc = c.admin("embargo_con_facturas.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.software, diligencia="DE-2026-SIM-56001", emitido=date(2026, 9, 24), principal=D("1000.00"), recargo=D("200.00"), intereses=D("0"), costas=D("0"), acceso=acceso,
        extra=[{"tipo": "parrafo", "texto": "Consta a esta Dependencia que el deudor ha emitido a ese pagador las siguientes facturas, cuyo importe deberá retener si están pendientes de pago:"}, table]))
    c.upload(doc, notification(route="embargo", procedure="EMBARGO_CREDITOS", affected={"type": "supplier", "tax_id": w.software.tax_id}, debt_amount=1200.00, credit_amount=1815.00,
                               not_invoice=True, requires_human=True, expected_findings=["credit_exceeds_debt"]))
    return c.finish()


def adv02(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV02", block="adversarial", title="«liquidación» dentro de «autoliquidación»", description="Requerimiento de verificación de datos que habla de la autoliquidación varias veces: no es una liquidación.", tags=["adversarial"], world=w)
    acceso = date(2026, 9, 24)
    doc = c.admin("verificacion_autoliquidacion.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente="202609SIM0005678F", documento="A2360926SIM00456", emitido=date(2026, 9, 21), impuesto="IRPF. Retenciones (modelo 111)", ejercicio=2026, periodo="2T",
        procedimiento="verificación de datos", pide=["Explicación de la diferencia entre la autoliquidación del modelo 111 y el resumen anual.", "Copia de las autoliquidaciones complementarias presentadas, si las hubiera."],
        puesta=date(2026, 9, 22), acceso=acceso))
    c.upload(doc, notification(route="requerimiento", procedure="VERIFICACION_DATOS", procedure_not=["LIQUIDACION", "PROPUESTA_LIQUIDACION"], deadline=business_days_after(acceso, 10).isoformat(),
                               requires_human=True, tax_reference={"model": "111", "year": 2026, "quarter": 2}))
    return c.finish()


def adv03(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV03", block="adversarial", title="Requerimiento escaneado", description="Sin texto seleccionable. Sin OCR no se puede leer: debe ir a una persona, nunca darse por bueno ni tomarse por factura.", tags=["adversarial", "ocr"], world=w)
    c.admin("requerimiento_original.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente="202609SIM0006789G", documento="A2360926SIM00457", emitido=date(2026, 9, 21), impuesto="Impuesto sobre el Valor Añadido", ejercicio=2026, periodo="2T",
        pide=["Libro registro de facturas recibidas."], puesta=date(2026, 9, 22), acceso=date(2026, 9, 23)))
    name = c.scanned("requerimiento_original.pdf", "requerimiento_escaneado.pdf")
    c.upload(name, {"ocr": True, "requires_human": True, "not_invoice": True})
    return c.finish()


def adv04(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV04", block="adversarial", title="Datos registrales en texto girado", description="La cabecera solo trae la marca; la razón social y el NIF están en un texto girado en el margen.", tags=["adversarial"], world=w)
    margin = f"{w.supplier.name} Inscrita en el Registro Mercantil de Valladolid, Tomo 1, Folio 1, Hoja VA-0001. CIF {w.supplier.tax_id}"
    truth = c.invoice("factura_texto_girado.pdf", supplier=w.supplier, number="SFD-2026-131", issued=date(2026, 9, 22), lines=[("Consumibles de soldadura", D(4), D("86.40"))],
                      brand="DUERO · suministros industriales", legal_footer=False, noise=Noise(rotated_margin=margin))
    c.upload("factura_texto_girado.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv05(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV05", block="adversarial", title="Factura en dos páginas", description="La tabla de conceptos en la primera página y el resumen fiscal en la segunda.", tags=["adversarial", "multipagina"], world=w)
    lines = [(f"Servicio de transporte {index:02d}/09", D(1), D("95.00") + index * 3) for index in range(1, 15)]
    truth = c.invoice("factura_dos_paginas.pdf", supplier=w.carrier, number="TSE-2026-133", issued=date(2026, 9, 21), lines=lines, pages=2)
    c.upload("factura_dos_paginas.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv06(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV06", block="adversarial", title="NIF del proveedor parcialmente ilegible", description="Un carácter del NIF del emisor es ilegible. Lo correcto es no inventarlo: o se resuelve con otra fuente fiable o se marca.", tags=["adversarial", "ocr"], world=w)
    broken = w.supplier.tax_id[:4] + "?" + w.supplier.tax_id[5:]
    truth = c.invoice("factura_nif_ilegible.pdf", supplier=w.supplier, number="SFD-2026-140", issued=date(2026, 9, 24), lines=[("Material eléctrico", D(1), D("312.00"))],
                      noise=Noise(replace={w.supplier.tax_id: broken}))
    c.upload("factura_nif_ilegible.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv07(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV07", block="adversarial", title="Fecha ambigua 4/5/26", description="Formato corto: en España es día/mes (4 de mayo de 2026), no mes/día.", tags=["adversarial", "ambiguo"], world=w)
    truth = c.invoice("factura_fecha_corta.pdf", supplier=w.cleaner, number="LIU-0504", issued=date(2026, 5, 4), lines=[("Limpieza extraordinaria", D(1), D("180.00"))], issued_text="4/5/26")
    c.upload("factura_fecha_corta.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv08(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV08", block="adversarial", title="Proveedor solo en el pie legal", description="La cabecera es una marca comercial; quién emite la factura solo se sabe por el pie del Registro Mercantil.", tags=["adversarial"], world=w)
    truth = c.invoice("factura_marca_comercial.pdf", supplier=w.carrier, number="TSE-2026-140", issued=date(2026, 9, 26), lines=[("Grupaje Burgos-León", D(2), D("145.00"))], brand="ESLA·LOG")
    c.upload("factura_marca_comercial.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv09(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV09", block="adversarial", title="Importes con dígitos separados", description="El IVA aparece como «1 7 4,72 €», como sale de algunas maquetas.", tags=["adversarial"], world=w)
    truth = c.invoice("factura_digitos_separados.pdf", supplier=w.software, number="2026-0455", issued=date(2026, 9, 23), lines=[("Mantenimiento software, septiembre", D(1), D("832.00"))],
                      noise=Noise(spaced_digits=True))
    c.upload("factura_digitos_separados.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


def adv10(root: Path, w: World) -> dict:
    c = Case(root, "B-ADV10", block="adversarial", title="Texto con errores de OCR", description="Etiquetas mal reconocidas («T0TAL», «lVA», «Base lmponible», «Fecba»).", tags=["adversarial", "ocr"], world=w)
    truth = c.invoice("factura_ocr_malo.pdf", supplier=w.supplier, number="SFD-2026-144", issued=date(2026, 9, 25), lines=[("Herramienta manual (lote)", D(1), D("455.00"))],
                      noise=Noise(replace={"Base imponible": "Base lmponible", "TOTAL FACTURA": "T0TAL FACTURA", "IVA ": "lVA ", "Fecha de": "Fecba de", "Nº factura": "N° fact."}))
    c.upload("factura_ocr_malo.pdf", {"type": "INVOICE", "invoice": truth})
    return c.finish()


# -- Apoyo


def bank01(root: Path, w: World) -> dict:
    c = Case(root, "B-BANCO01", block="apoyo", title="Extracto con descuadres", description="Factura sin pago, pago sin factura, importe distinto y cargo duplicado.", tags=["expediente"], world=w)
    c.prior_invoice(w.supplier, "SFD-2026-090", date(2026, 8, 10), D("1200.00"))  # 1.452,00 · sin pago en el extracto
    c.prior_invoice(w.carrier, "TSE-2026-091", date(2026, 8, 12), D("800.00"))  # 968,00 · pagado por 986,00 (importe distinto)
    c.prior_invoice(w.cleaner, "LIU-0815", date(2026, 8, 15), D("400.00"))  # 484,00 · cargado dos veces
    name = c.bank("extracto_septiembre.csv", [
        (date(2026, 9, 1), f"TRANSFERENCIA A {w.carrier.name.upper()} TSE-2026-091", D("-986.00")),
        (date(2026, 9, 2), f"RECIBO {w.cleaner.name.upper()} LIU-0815", D("-484.00")),
        (date(2026, 9, 3), f"RECIBO {w.cleaner.name.upper()} LIU-0815", D("-484.00")),
        (date(2026, 9, 8), "TRANSFERENCIA A TALLERES IMAGINARIOS PISUERGA SL", D("-2350.00")),
        (date(2026, 9, 15), "INGRESO CLIENTE FICTICIO 0042", D("3100.00")),
    ])
    c.bank_import(name)
    c.scan_anomalies({"expected_findings": ["PAGO_SIN_FACTURA", "FACTURA_SIN_PAGO"]})
    return c.finish()


def deadlines01(root: Path, w: World) -> dict:
    c = Case(root, "B-PLAZOS01", block="apoyo", title="Plazos 303, 130, 111 y 115 del 3T", description="Eventos estructurados de calendario, no PDF. El 130 no aplica a una sociedad.", tags=["limpio"], world=w)
    due = date(2026, 10, 20)
    c.deadline("303", 2026, 3, due, {"route": "plazo", "requires_human": True, "deadline": due.isoformat(), "tax_reference": {"model": "303", "year": 2026, "quarter": 3}, "expected_agents": ["fiscal", "director"]})
    c.deadline("111", 2026, 3, due, {"route": "plazo", "requires_human": True, "deadline": due.isoformat(), "tax_reference": {"model": "111", "year": 2026, "quarter": 3}})
    c.deadline("115", 2026, 3, due, {"route": "plazo", "requires_human": True, "deadline": due.isoformat(), "tax_reference": {"model": "115", "year": 2026, "quarter": 3}})
    c.deadline("130", 2026, 3, due, {"fiscal_applies": False, "insight_contains": ["sociedad"]})
    return c.finish()


B_CASES: list[Callable[[Path, World], dict]] = [
    b01, b02, b03, b04, b05, b06, b07, b08, b09, b10, b11, b12, b13, b14, b15, b16, b17, b18, b19, b20, b21, b22, b23,
    adv01, adv02, adv03, adv04, adv05, adv06, adv07, adv08, adv09, adv10, bank01, deadlines01,
]


def write_index(root: Path, cases: list[dict], **extra: Any) -> dict:
    index = {
        "descripcion": "Banco de expedientes sintéticos con verdad de referencia. " + MARK + ".",
        "casos": [{"case_id": case["case_id"], "bloque": case["bloque"], "titulo": case["titulo"], "etiquetas": case["etiquetas"],
                   "documentos": len(case["documents"]), "entradas": len(case["entradas"])} for case in cases],
        "total_casos": len(cases),
        "total_documentos": sum(len(case["documents"]) for case in cases),
        **extra,
    }
    (root / "indice.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def build_b(root: Path = B_DIR) -> dict:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    world = world_b()
    return write_index(root, [builder(root, world) for builder in B_CASES], conjunto="B")


# ---------------------------------------------------------------------
# Banco C (ciego): mismas familias, otras empresas, cifras, fechas y ruido
# ---------------------------------------------------------------------

C_NAMES = (
    ("Ferretería", "Inventada Tormes"), ("Distribuciones", "Simuladas Carrión"), ("Logística", "Ficticia Órbigo"), ("Servicios", "Imaginarios Bernesga"),
    ("Mantenimientos", "Ejemplo Valderaduey"), ("Recambios", "Supuestos Cea"), ("Montajes", "Irreales Adaja"), ("Asesores", "Ficticios Eresma"),
)
C_PEOPLE = ("Marta Supuesta Ruiz", "Andrés Inventado Sanz", "Elena Ejemplo Pardo", "Jorge Ficticio Calvo")


def world_c(rng: random.Random) -> World:
    names = rng.sample(C_NAMES, 6)
    numbers = rng.sample(range(100000, 999999), 6)

    def company(index: int, city: str) -> Party:
        first, second = names[index]
        slug = second.split()[-1].lower().translate(str.maketrans("áéíóú", "aeiou"))
        return Party(f"{first} {second} S.L.", cif("B", f"00{numbers[index]:05d}"[:7]), f"C/ Simulada {rng.randint(1, 90)}, {city}", f"admin@{slug}.test", city.split()[-1])

    people = rng.sample(C_PEOPLE, 2)
    return World(
        company=company(0, "09007 Burgos"), supplier=company(1, "37001 Salamanca"), carrier=company(2, "24010 León"), cleaner=company(3, "34003 Palencia"),
        software=company(4, "05001 Ávila"), advisor=Party(people[0], dni(rng.randint(10000, 99999)), "C/ Ejemplo 9, 09005 Burgos", "consulta@asesoria-c.test"),
        employee=Party(people[1], dni(rng.randint(100000, 999999)), "C/ Supuesta 11, 09006 Burgos"),
        unknown=Party("Sociedad Ajena Ficticia S.L.", cif("B", f"98{rng.randint(10000, 99999)}"), "C/ Nadie 7, 42001 Soria"), ccc=f"09 0{rng.randint(100000, 999999)} {rng.randint(10, 99)} (simulado)",
    )


def build_c(seed: int, root: Path = C_DIR) -> dict:
    """C01–C12 con variaciones elegidas por la semilla. Quien lo genera no lo mira."""
    rng = random.Random(seed)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    w = world_c(rng)
    cases: list[dict] = []
    day = lambda low, high: date(2026, 9, rng.randint(low, high))  # noqa: E731
    noisy = lambda: rng.random() < 0.4  # noqa: E731

    # C01 requerimiento (con o sin ruido)
    c = Case(root, "C01", block="aeat", title="Requerimiento", description="", tags=["limpio"], world=w)
    acceso = day(14, 25)
    period = rng.choice([1, 2])
    c.upload(c.admin("c01.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente=f"2026C{rng.randint(10**8, 10**9)}", documento=f"C{rng.randint(10**9, 10**10)}", emitido=acceso - timedelta(days=4), impuesto="Impuesto sobre el Valor Añadido",
        ejercicio=2026, periodo=f"{period}T", pide=rng.sample(["Libro registro de facturas recibidas del periodo.", "Extractos bancarios del trimestre.", "Contratos de arrendamiento vigentes.", "Facturas recibidas de importe superior a 3.000 euros."], 2),
        puesta=acceso - timedelta(days=1), acceso=acceso), noise=Noise(rotated_margin="Ref. interna SIM" if noisy() else None)),
        notification(route="requerimiento", procedure="COMPROBACION_LIMITADA", issuer="AEAT", deadline=business_days_after(acceso, 10).isoformat(), requires_human=True,
                     tax_reference={"model": "303", "year": 2026, "quarter": period}))
    cases.append(c.finish())

    # C02 requerimiento ambiguo: verificación de datos sobre autoliquidación, sin notificación
    c = Case(root, "C02", block="aeat", title="Requerimiento ambiguo", description="", tags=["ambiguo"], world=w)
    c.upload(c.admin("c02.pdf", organism="AEAT", area="Gestión Tributaria", kind="requerimiento", blocks=P.requerimiento_aeat(
        party=w.company, expediente=f"2026C{rng.randint(10**8, 10**9)}", documento=f"C{rng.randint(10**9, 10**10)}", emitido=day(10, 25), impuesto="IRPF. Retenciones (modelo 111)", ejercicio=2026,
        periodo=f"{rng.choice([1, 2])}T", procedimiento="verificación de datos", pide=["Aclaración sobre la autoliquidación presentada y las autoliquidaciones complementarias."], puesta=None, acceso=None)),
        notification(route="requerimiento", procedure="VERIFICACION_DATOS", procedure_not=["LIQUIDACION"], deadline_pending_confirmation=True, requires_human=True))
    cases.append(c.finish())

    # C03 embargo a proveedor (con crédito, mayor o menor que la deuda)
    c = Case(root, "C03", block="embargos", title="Embargo de créditos a proveedor", description="", tags=["limpio"], world=w)
    bases = [D(rng.randint(600, 2400)) for _ in range(rng.randint(1, 3))]
    credit = sum((base + (base * 21 / 100).quantize(D("0.01")) for base in bases), D(0))
    for index, base in enumerate(bases):
        c.prior_invoice(w.supplier, f"C3-{index + 1:03d}", date(2026, 8, 3 + index * 9), base)
    principal = D(rng.randint(800, 4000))
    acceso = day(20, 29)
    c.upload(c.admin("c03.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.supplier, diligencia=f"DE-C-{rng.randint(10000, 99999)}", emitido=acceso - timedelta(days=3), principal=principal, recargo=(principal / 5).quantize(D("0.01")),
        intereses=D(0), costas=D(0), acceso=acceso)),
        notification(route="embargo", procedure="EMBARGO_CREDITOS", affected={"type": "supplier", "tax_id": w.supplier.tax_id}, debt_amount=float(principal + (principal / 5).quantize(D("0.01"))),
                     credit_amount=float(credit), requires_human=True, deadline=business_days_after(acceso, 5).isoformat(),
                     expected_findings=["credit_exceeds_debt"] if credit > principal * D("1.2") else []))
    cases.append(c.finish())

    # C04 embargo sin deuda
    c = Case(root, "C04", block="embargos", title="Embargo sin pago pendiente", description="", tags=["limpio"], world=w)
    c.prior_invoice(w.cleaner, "C4-001", date(2026, 8, 20), D(rng.randint(150, 900)), paid=date(2026, 9, 1))
    principal = D(rng.randint(500, 3000))
    c.upload(c.admin("c04.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_creditos(
        pagador=w.company, deudor=w.cleaner, diligencia=f"DE-C-{rng.randint(10000, 99999)}", emitido=day(15, 25), principal=principal, recargo=(principal / 5).quantize(D("0.01")), intereses=D(0),
        costas=D(0), acceso=day(26, 29)), noise=Noise(scanned=False, rotated_margin="Copia" if noisy() else None)),
        notification(route="embargo", procedure="EMBARGO_CREDITOS", affected={"type": "supplier", "tax_id": w.cleaner.tax_id}, credit_amount=0.0, requires_human=True,
                     expected_findings=["no_pending_payment"]))
    cases.append(c.finish())

    # C05 embargo de salario
    c = Case(root, "C05", block="embargos", title="Embargo de salario", description="", tags=["limpio"], world=w)
    c.employee(w.employee)
    total = D(rng.randint(900, 6000)) + D("0.37")
    c.upload(c.admin("c05.pdf", organism="AEAT", area="Recaudación", kind="embargo", blocks=P.embargo_salarios(
        pagador=w.company, empleado=w.employee, diligencia=f"DS-C-{rng.randint(10000, 99999)}", emitido=day(15, 25), total=total, acceso=day(26, 29))),
        notification(route="embargo", procedure="EMBARGO_SALARIOS", affected={"type": "employee", "tax_id": w.employee.tax_id}, debt_amount=float(total), requires_human=True,
                     requested_documents_excluded=["RELACION_CREDITOS"]))
    cases.append(c.finish())

    # C06 factura atípica (importe muy por encima del historial)
    c = Case(root, "C06", block="facturas", title="Factura atípica", description="", tags=["expediente"], world=w)
    usual = D(rng.randint(200, 500))
    for month in range(3, 9):
        c.prior_invoice(w.carrier, f"C6-{month:02d}", date(2026, month, 15), usual + rng.randint(-15, 15), paid=date(2026, month, 28))
    truth = c.invoice("c06.pdf", supplier=w.carrier, number="C6-09", issued=day(15, 25), lines=[("Servicio mensual", D(1), usual * rng.choice([5, 6, 8]))])
    c.upload("c06.pdf", {"type": "INVOICE", "invoice": truth, "route": "factura_sospechosa", "expected_findings": ["IMPORTE_ATIPICO"], "requires_human": True})
    cases.append(c.finish())

    # C07 documento normal
    c = Case(root, "C07", block="normales", title="Documento normal", description="", tags=["limpio"], world=w)
    truth = c.invoice("c07.pdf", supplier=w.software, number=f"C7-{rng.randint(100, 999)}", issued=day(1, 28), lines=[("Licencia mensual", D(1), D(rng.randint(40, 300)))])
    c.upload("c07.pdf", {"type": "INVOICE", "invoice": truth, "requires_human": False})
    cases.append(c.finish())

    # C08 duplicada
    c = Case(root, "C08", block="memoria", title="Factura duplicada", description="", tags=["expediente"], world=w)
    number, issued, amount = f"C8-{rng.randint(100, 999)}", day(1, 25), D(rng.randint(300, 2000))
    truth = c.invoice("c08a.pdf", supplier=w.cleaner, number=number, issued=issued, lines=[("Servicio", D(1), amount)])
    c.invoice("c08b.pdf", supplier=w.cleaner, number=number, issued=issued, lines=[("Servicio", D(1), amount)], noise=Noise(rotated_margin="Duplicado"))
    c.upload("c08a.pdf", {"type": "INVOICE", "invoice": truth, "duplicate": False})
    c.upload("c08b.pdf", {"type": "INVOICE", "duplicate": True, "expected_findings": ["POSIBLE_DUPLICADO"], "requires_human": True})
    cases.append(c.finish())

    # C09 rectificativa
    c = Case(root, "C09", block="facturas", title="Rectificativa", description="", tags=["limpio"], world=w)
    base = D(rng.randint(500, 3000))
    original = c.invoice("c09a.pdf", supplier=w.software, number=f"C9-{rng.randint(100, 999)}", issued=day(1, 10), lines=[("Proyecto", D(1), base)])
    rect = c.invoice("c09b.pdf", supplier=w.software, number=f"C9-R{rng.randint(10, 99)}", issued=day(12, 28), lines=[("Abono", D(-1), (base / rng.choice([4, 5, 10])).quantize(D("0.01")))],
                     rectifies=original["invoice_number"])
    c.upload("c09a.pdf", {"type": "INVOICE", "invoice": original})
    c.upload("c09b.pdf", {"type": "INVOICE", "invoice": rect, "duplicate": False, "findings_excluded": ["POSIBLE_DUPLICADO"]})
    cases.append(c.finish())

    # C10 retención
    c = Case(root, "C10", block="facturas", title="Retención", description="", tags=["limpio"], world=w)
    truth = c.invoice("c10.pdf", supplier=w.advisor, number=f"C10-{rng.randint(10, 99)}", issued=day(1, 28), lines=[("Honorarios", D(1), D(rng.randint(300, 2500)))],
                      irpf_rate=D(rng.choice([7, 15])), legal_footer=False)
    c.upload("c10.pdf", {"type": "INVOICE", "invoice": truth})
    cases.append(c.finish())

    # C11 multipágina (a veces escaneada)
    c = Case(root, "C11", block="facturas", title="Multipágina", description="", tags=["multipagina"], world=w)
    lines = [(f"Partida {index}", D(rng.randint(1, 4)), D(rng.randint(10, 90))) for index in range(rng.randint(14, 22))]
    truth = c.invoice("c11.pdf", supplier=w.supplier, number=f"C11-{rng.randint(100, 999)}", issued=day(1, 28), lines=lines, pages=rng.choice([2, 3]))
    expected: dict[str, Any] = {"type": "INVOICE", "invoice": truth}
    if rng.random() < 0.3:
        name = c.scanned("c11.pdf", "c11_escaneada.pdf")
        expected = {"ocr": True, "requires_human": True}
        c.data["entradas"].append({"tipo": "subida", "archivo": name, "expected": expected})
    else:
        c.upload("c11.pdf", expected)
    cases.append(c.finish())

    # C12 plazo 303
    c = Case(root, "C12", block="apoyo", title="Plazo 303", description="", tags=["limpio"], world=w)
    due = date(2026, 10, 20)
    c.deadline("303", 2026, 3, due, {"route": "plazo", "requires_human": True, "deadline": due.isoformat(), "tax_reference": {"model": "303", "year": 2026, "quarter": 3}})
    cases.append(c.finish())

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return write_index(root, cases, conjunto="C", semilla=seed, sello=digest.hexdigest(), aviso="Banco ciego: no se mira ni se ajusta ninguna regla con él.")
