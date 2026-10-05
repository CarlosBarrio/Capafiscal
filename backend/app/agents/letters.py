"""Plantillas de escritos a la Administración (se revisan siempre antes de presentar)."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.agents.knowledge import ORGANISM_HEADERS

MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def long_date(value: date) -> str:
    return f"{value.day} de {MONTHS[value.month - 1]} de {value.year}"


def money(value: Any) -> str:
    if value is None:
        return "[importe]"
    text = f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def header(facts: dict[str, Any], company: dict[str, Any]) -> list[str]:
    lines = [ORGANISM_HEADERS.get(facts.get("issuer") or "OTRO", ORGANISM_HEADERS["OTRO"])]
    if facts.get("reference"):
        lines.append(f"Referencia: {facts['reference']}")
    lines += [
        "",
        f"D./Dña. [nombre y apellidos], con DNI [DNI], en nombre y representación de {company.get('name') or '[razón social]'}, "
        f"con NIF {company.get('tax_id') or '[NIF]'} y domicilio a efectos de notificaciones en "
        f"{company.get('address') or '[domicilio]'}{', ' + company['city'] if company.get('city') else ''}, ante esa Administración comparece y",
        "",
    ]
    return lines


def received_sentence(facts: dict[str, Any]) -> str:
    when = facts.get("notified_at") or facts.get("available_at")
    when_text = f"con fecha {when:%d/%m/%Y} " if isinstance(when, date) else ""
    return f"Que {when_text}ha recibido {facts.get('type_label', 'notificación').lower()}" + (
        f" con referencia {facts['reference']}" if facts.get("reference") else ""
    )


def closing(company: dict[str, Any], today: date) -> list[str]:
    return [
        "",
        f"En {company.get('city') or '[localidad]'}, a {long_date(today)}.",
        "",
        "Fdo.: [nombre y apellidos]",
        f"{company.get('name') or ''}",
    ]


def documents_list(documents: list[dict[str, Any]]) -> list[str]:
    usable = [item for item in documents if item.get("status") != "not_applicable"]
    if not usable:
        return ["   [relación de la documentación que se aporta]"]
    return [f"   {index}. {item['label']}{' — ' + item['period_label'] if item.get('period_label') else ''}" for index, item in enumerate(usable, start=1)]


def build_letter(kind: str | None, facts: dict[str, Any], company: dict[str, Any], documents: list[dict[str, Any]], today: date) -> str | None:
    if not kind:
        return None

    lines = header(facts, company)

    if kind == "requerimiento":
        lines += [
            "EXPONE",
            "",
            f"Primero.- {received_sentence(facts)}, por el que se le solicita la aportación de determinada documentación.",
            "",
            "Segundo.- Que, en cumplimiento de lo requerido y dentro del plazo concedido, se aporta la siguiente documentación:",
            *documents_list(documents),
            "",
            "Tercero.- Que queda a disposición de esa Administración para cualquier aclaración o documentación adicional que precise.",
            "",
            "Por lo expuesto,",
            "",
            "SOLICITA",
            "",
            "Que se tenga por presentado este escrito junto con la documentación que lo acompaña y por atendido en tiempo y forma el requerimiento de referencia.",
        ]
    elif kind == "alegaciones":
        lines += [
            "EXPONE",
            "",
            f"Primero.- {received_sentence(facts)}, en la que se concede trámite de audiencia para formular alegaciones.",
            "",
            "Segundo.- Que no está conforme con la propuesta por los siguientes motivos:",
            "   [motivo 1: explica qué dato no es correcto y por qué]",
            "   [motivo 2]",
            "",
            "Tercero.- Que para acreditar lo anterior aporta la siguiente documentación:",
            *documents_list(documents),
            "",
            "Por lo expuesto,",
            "",
            "SOLICITA",
            "",
            "Que se tengan por formuladas las presentes alegaciones y, en su virtud, se dicte resolución de acuerdo con lo expuesto.",
        ]
    elif kind == "recurso":
        lines += [
            "EXPONE",
            "",
            f"Primero.- {received_sentence(facts)}, con un importe a ingresar de {money(facts.get('amount'))}.",
            "",
            "Segundo.- Que no está conforme con dicho acto por los siguientes motivos:",
            "   [motivos del recurso]",
            "",
            "Por lo expuesto,",
            "",
            "SOLICITA",
            "",
            "Que se tenga por interpuesto RECURSO DE REPOSICIÓN contra el acto de referencia y se dicte resolución anulándolo o modificándolo conforme a lo expuesto.",
        ]
    elif kind == "aplazamiento":
        lines += [
            "EXPONE",
            "",
            f"Primero.- {received_sentence(facts)}, por importe de {money(facts.get('amount'))}.",
            "",
            "Segundo.- Que atraviesa dificultades transitorias de tesorería que le impiden efectuar el pago en el plazo establecido, sin perjuicio de su capacidad para hacerlo en los plazos que se solicitan.",
            "",
            "Por lo expuesto,",
            "",
            "SOLICITA",
            "",
            "Que se conceda el APLAZAMIENTO/FRACCIONAMIENTO de la deuda en [número] plazos mensuales, con domiciliación en la cuenta [IBAN].",
        ]
    elif kind == "embargo":
        affected = facts.get("affected") or {}
        pending = facts.get("embargo_pending") or []
        total = sum(item["total"] for item in pending)
        lines += [
            "EXPONE",
            "",
            f"Primero.- {received_sentence(facts)}, relativa a {affected.get('name') or '[nombre del embargado]'}"
            f"{', con NIF ' + affected['tax_id'] if affected.get('tax_id') else ''}.",
            "",
            (
                f"Segundo.- Que a la fecha de recepción existen créditos pendientes de pago a favor del embargado por importe total de {money(total)}, "
                "que quedan retenidos a disposición de esa Administración:"
                if pending
                else "Segundo.- Que a la fecha de recepción no existen créditos pendientes de pago a favor del embargado."
            ),
            *[f"   - Factura {item['number'] or 's/n'} de {item['date']}: {money(item['total'])}" for item in pending],
            "",
            "Por lo expuesto,",
            "",
            "SOLICITA",
            "",
            "Que se tenga por contestada la diligencia de embargo de referencia" + (" y se indique la forma de ingreso de las cantidades retenidas." if pending else "."),
        ]
    else:
        return None

    lines += closing(company, today)
    return "\n".join(lines)
