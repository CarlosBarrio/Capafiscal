"""
Notificaciones sintéticas de la DEHú (SIMULACIÓN — NO OFICIAL), en el formato que lee `FolderTransport`:
`<id>.json` con los metadatos de la DEHú y `<id>.pdf` con el acto (si se ha accedido).

    python evaluation/datasets/dehu_sintetico/generar.py      # regenera los ficheros y labels.json
    python -m evaluation dehu                                  # los pasa por el adaptador real y compara

La verdad (`labels.json`) se calcula aquí con las reglas legales y el calendario de `plantillas.py`, NO con el
código de CapaFiscal, para no medir la aplicación contra sí misma:

    plazo de días hábiles      Ley 39/2015, art. 30: desde el día siguiente a la notificación; sin sábados,
                               domingos ni festivos
    puesta a disposición       art. 43.2: sin acceso en 10 días naturales, se entiende notificada (rechazada)
    sin acceder                el día en que vencen esos 10 días
    providencia de apremio     LGT art. 62.5: notificada del 1 al 15 → hasta el día 20 de ese mes;
                               del 16 al último → hasta el día 5 del mes siguiente (o el hábil siguiente)

Casos: requerimiento con acceso, requerimiento con varios documentos, puesta a disposición sin acceso,
comunicación informativa, propuesta de liquidación (alegaciones), providencia de apremio y uno ambiguo
(solo metadatos, sin PDF ni tipo reconocible).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from evaluation import plantillas as P  # noqa: E402
from evaluation.documentos import MARK  # noqa: E402
from evaluation.documentos import render_admin  # noqa: E402
from evaluation.plantillas import HOLIDAYS  # noqa: E402
from evaluation.plantillas import Party  # noqa: E402
from evaluation.plantillas import business_days_after  # noqa: E402

COMPANY = Party("EMPRESA EJEMPLO S.L.", "B00100016", "C/ Mayor 1, 09001 Burgos")
AEAT = "Agencia Estatal de Administración Tributaria"
TGSS = "Tesorería General de la Seguridad Social"


def next_business_day(day: date) -> date:
    while day.weekday() >= 5 or day in HOLIDAYS:
        day += timedelta(days=1)
    return day


def apremio_deadline(notified: date) -> date:
    """LGT art. 62.5."""
    if notified.day <= 15:
        return next_business_day(notified.replace(day=20))
    following = (notified.replace(day=1) + timedelta(days=32)).replace(day=5)
    return next_business_day(following)


def write(identifier: str, meta: dict, blocks: list[dict] | None, organism: str, area: str) -> None:
    (HERE / f"{identifier}.json").write_text(json.dumps({"identifier": identifier, **meta}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if blocks is not None:
        render_admin(HERE / f"{identifier}.pdf", organism=organism, area=area, blocks=blocks, seed=identifier)


def cases() -> list[dict]:
    out = []

    # 1. Requerimiento con plazo y fecha de acceso: 10 días hábiles desde el acceso.
    accessed = date(2026, 9, 28)
    write("D01-REQ-ACCESO", {"issuer": AEAT, "subject": "Requerimiento de información", "holder_tax_id": COMPANY.tax_id,
                             "available_at": "2026-09-25", "accessed_at": accessed.isoformat()},
          P.requerimiento_aeat(party=COMPANY, expediente="202609SIM0001111A", documento="A23SIM1", emitido=date(2026, 9, 23), impuesto="Impuesto sobre el Valor Añadido",
                               ejercicio=2026, periodo="2T", pide=["Libro registro de facturas recibidas."]), "AEAT", "Gestión Tributaria")
    out.append({"id": "D01-REQ-ACCESO", "tags": ["requerimiento", "plazo", "fecha de acceso"], "expected": {
        "organismo": "AEAT", "tipo": "REQUERIMIENTO", "expediente": "202609SIM0001111A", "puesta_disposicion": "2026-09-25",
        "notificada": accessed.isoformat(), "fecha_limite": business_days_after(accessed, 10).isoformat(),
        "documentacion": ["LIBRO_RECIBIDAS"], "accion": "ACTION_REQUIRED", "expediente_creado": True}})

    # 2. Requerimiento que pide varios documentos.
    accessed = date(2026, 9, 30)
    write("D02-REQ-DOCUMENTOS", {"issuer": AEAT, "subject": "Requerimiento de documentación", "holder_tax_id": COMPANY.tax_id,
                                 "available_at": "2026-09-29", "accessed_at": accessed.isoformat()},
          P.requerimiento_aeat(party=COMPANY, expediente="202609SIM0002222B", documento="A23SIM2", emitido=date(2026, 9, 26), impuesto="Impuesto sobre el Valor Añadido",
                               ejercicio=2026, periodo="2T", pide=["Libro registro de facturas recibidas.", "Libro registro de facturas expedidas.",
                                                                  "Extractos de las cuentas bancarias del periodo."]), "AEAT", "Gestión Tributaria")
    out.append({"id": "D02-REQ-DOCUMENTOS", "tags": ["requerimiento", "documentación solicitada"], "expected": {
        "organismo": "AEAT", "tipo": "REQUERIMIENTO", "expediente": "202609SIM0002222B", "notificada": accessed.isoformat(),
        "fecha_limite": business_days_after(accessed, 10).isoformat(), "documentacion": ["EXTRACTOS", "LIBRO_EMITIDAS", "LIBRO_RECIBIDAS"],
        "accion": "ACTION_REQUIRED", "expediente_creado": True}})

    # 3. Puesta a disposición sin acceso: a los 10 días naturales se entiende notificada.
    available = date(2026, 9, 14)
    rejected = available + timedelta(days=10)
    write("D03-PUESTA-SIN-ACCESO", {"issuer": AEAT, "subject": "Requerimiento de información", "holder_tax_id": COMPANY.tax_id,
                                    "available_at": available.isoformat()}, None, "AEAT", "")
    out.append({"id": "D03-PUESTA-SIN-ACCESO", "tags": ["puesta a disposición", "sin acceso", "solo metadatos"], "expected": {
        "organismo": "AEAT", "tipo": "REQUERIMIENTO", "puesta_disposicion": available.isoformat(),
        "fecha_limite": business_days_after(rejected, 10).isoformat(), "accion": "ACTION_REQUIRED", "expediente_creado": True}})

    # 4. Comunicación meramente informativa: no pide nada ni tiene plazo.
    accessed = date(2026, 9, 29)
    write("D04-INFORMATIVA", {"issuer": AEAT, "subject": "Comunicación informativa", "kind": "comunicacion", "holder_tax_id": COMPANY.tax_id,
                              "available_at": "2026-09-28", "accessed_at": accessed.isoformat()},
          [{"tipo": "datos", "filas": P.destinatario(COMPANY) + [("Fecha", "25/09/2026")]},
           {"tipo": "titulo", "texto": "COMUNICACIÓN"},
           {"tipo": "parrafo", "texto": "Le informamos, a efectos meramente informativos, de la publicación del nuevo calendario del contribuyente. "
                                        "Esta comunicación no requiere ninguna actuación por su parte."},
           P.pie_legal(MARK)], "AEAT", "Gestión Tributaria")
    out.append({"id": "D04-INFORMATIVA", "tags": ["informativa", "sin plazo"], "expected": {
        "organismo": "AEAT", "tipo": "COMUNICACION", "notificada": accessed.isoformat(), "fecha_limite": None, "documentacion": [],
        "accion": "INFORMATIONAL"}})

    # 5. Propuesta de liquidación: 10 días hábiles de alegaciones.
    accessed = date(2026, 10, 1)
    write("D05-PROPUESTA", {"issuer": AEAT, "subject": "Propuesta de liquidación provisional", "holder_tax_id": COMPANY.tax_id,
                            "available_at": "2026-09-30", "accessed_at": accessed.isoformat()},
          P.propuesta_liquidacion(party=COMPANY, expediente="202609SIM0003333C", documento="A23SIM3", emitido=date(2026, 9, 28), ejercicio=2026, periodo="2T",
                                  declarado=Decimal("1240.50"), comprobado=Decimal("2480.90"), intereses=Decimal("18.32"), puesta=None, acceso=None), "AEAT", "Gestión Tributaria")
    out.append({"id": "D05-PROPUESTA", "tags": ["propuesta de liquidación", "plazo calculable"], "expected": {
        "organismo": "AEAT", "tipo": "PROPUESTA_LIQUIDACION", "expediente": "202609SIM0003333C", "notificada": accessed.isoformat(),
        "fecha_limite": business_days_after(accessed, 10).isoformat(), "accion": "ACTION_REQUIRED", "expediente_creado": True}})

    # 6. Providencia de apremio (solo metadatos): LGT 62.5.
    accessed = date(2026, 10, 5)
    write("D06-APREMIO", {"issuer": TGSS, "subject": "Providencia de apremio", "holder_tax_id": COMPANY.tax_id,
                          "available_at": "2026-10-02", "accessed_at": accessed.isoformat()}, None, "TGSS", "")
    out.append({"id": "D06-APREMIO", "tags": ["apremio", "plazo calculable", "solo metadatos"], "expected": {
        "organismo": "TGSS", "tipo": "APREMIO", "notificada": accessed.isoformat(), "fecha_limite": apremio_deadline(accessed).isoformat(),
        "accion": "ACTION_REQUIRED", "expediente_creado": True}})

    # 7. Ambiguo: un emisor no habitual, asunto genérico y sin PDF.
    write("D07-AMBIGUO", {"issuer": "Ayuntamiento de Villaejemplo", "subject": "Notificación", "holder_tax_id": COMPANY.tax_id,
                          "available_at": "2026-09-30"}, None, "OTRO", "")
    out.append({"id": "D07-AMBIGUO", "tags": ["ambiguo", "solo metadatos"], "expected": {
        "organismo": "AYUNTAMIENTO", "accion_no_es": "INFORMATIONAL", "expediente_creado": True}})
    return out


def main() -> None:
    for old in list(HERE.glob("D*.json")) + list(HERE.glob("D*.pdf")):
        old.unlink()
    data = {"nota": f"{MARK}. Generado por generar.py: no editar a mano.", "empresa": {"name": COMPANY.name, "tax_id": COMPANY.tax_id}, "casos": cases()}
    (HERE / "labels.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(data['casos'])} notificaciones en {HERE}")


if __name__ == "__main__":
    main()
