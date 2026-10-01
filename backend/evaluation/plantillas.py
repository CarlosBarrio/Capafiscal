"""
Plantillas de documentos para los expedientes sintéticos.

Cada función devuelve los bloques de un tipo de documento (requerimiento,
propuesta de liquidación, diligencia de embargo, reclamación de la
Seguridad Social…) siguiendo la estructura de los procedimientos oficiales:
destinatario, expediente, órgano, asunto, fundamento, documentación,
plazo, advertencias y diligencia de notificación. Los datos son inventados
y todos los documentos llevan «SIMULACIÓN — NO OFICIAL».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from decimal import Decimal

from evaluation.documentos import eur
from evaluation.documentos import fecha

# Festivos nacionales que caen en las fechas del banco (los autonómicos y
# locales no se simulan: los documentos se sitúan fuera de ellos).
HOLIDAYS = {date(2026, 10, 12), date(2026, 12, 8), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 6)}


def business_days_after(start: date, days: int) -> date:
    """Plazo en días hábiles (sin sábados, domingos ni festivos) desde el día siguiente."""
    current = start
    while days:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in HOLIDAYS:
            days -= 1
    return current


def cif(letter: str, digits: str) -> str:
    """NIF de persona jurídica con dígito de control válido."""
    odd = sum(sum(divmod(int(digit) * 2, 10)) for digit in digits[0::2])
    even = sum(int(digit) for digit in digits[1::2])
    return f"{letter}{digits}{(10 - (odd + even) % 10) % 10}"


def dni(number: int) -> str:
    return f"{number:08d}{'TRWAGMYFPDXBNJZSQVHLCKE'[number % 23]}"


@dataclass(frozen=True)
class Party:
    name: str
    tax_id: str
    address: str
    email: str = ""
    registry: str = "Burgos"

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "tax_id": self.tax_id, "address": self.address, "registry": self.registry}


def destinatario(party: Party) -> list[tuple[str, str]]:
    return [("Destinatario", party.name), ("NIF", party.tax_id), ("Domicilio", party.address)]


def notificacion(puesta: date | None, acceso: date | None) -> list[dict]:
    """Diligencia de notificación electrónica (la que da la fecha para el plazo)."""
    if puesta is None and acceso is None:
        return []
    rows = []
    if puesta:
        rows.append(("Puesta a disposición", fecha(puesta)))
    if acceso:
        rows.append(("Fecha de notificación (acceso al contenido)", fecha(acceso)))
    return [
        {"tipo": "apartado", "texto": "DILIGENCIA DE NOTIFICACIÓN ELECTRÓNICA"},
        {"tipo": "datos", "filas": rows},
        {"tipo": "pequeno", "texto": "Notificación practicada por comparecencia en la sede electrónica / Dirección Electrónica Habilitada única (simulación)."},
    ]


def pie_legal(texto: str) -> dict:
    return {"tipo": "pequeno", "texto": texto}


# ---------------------------------------------------------------------
# AEAT
# ---------------------------------------------------------------------


def requerimiento_aeat(*, party: Party, expediente: str, documento: str, emitido: date, impuesto: str, ejercicio: int, periodo: str,
                       pide: list[str], procedimiento: str = "comprobación limitada", puesta: date | None = None, acceso: date | None = None,
                       dias: int = 10, area: str = "Gestión Tributaria", extra: list[dict] | None = None) -> list[dict]:
    return [
        {"tipo": "datos", "filas": destinatario(party) + [
            ("Nº de expediente", expediente), ("Nº de documento", documento), ("Fecha", fecha(emitido)),
            ("Órgano", f"Dependencia Regional de {area} · Unidad de Gestión de Grandes y Medianas Empresas (simulada)"),
        ]},
        {"tipo": "titulo", "texto": "REQUERIMIENTO"},
        {"tipo": "apartado", "texto": "Asunto"},
        {"tipo": "parrafo", "texto": f"Procedimiento de {procedimiento}. Concepto: {impuesto}. Ejercicio {ejercicio}. Periodo {periodo}."},
        {"tipo": "apartado", "texto": "Fundamento"},
        {"tipo": "parrafo", "texto": (
            "En el ejercicio de las funciones de comprobación atribuidas por los artículos 136 a 140 de la Ley 58/2003, de 17 de diciembre, "
            "General Tributaria, y de conformidad con el artículo 93 del mismo texto legal y los artículos 87 y 163 del Reglamento General "
            "aprobado por el Real Decreto 1065/2007, se le requiere para que aporte la documentación que se indica a continuación, "
            f"relativa a su autoliquidación del {impuesto}, ejercicio {ejercicio}, periodo {periodo}."
        )},
        {"tipo": "apartado", "texto": "Documentación que debe aportar"},
        {"tipo": "lista", "elementos": [f"{chr(97 + index)}) {item}" for index, item in enumerate(pide)]},
        *(extra or []),
        {"tipo": "apartado", "texto": "Plazo"},
        {"tipo": "parrafo", "texto": (
            f"Dispone de un plazo de {dias} días hábiles, contados a partir del día siguiente al de la notificación del presente "
            "requerimiento, para aportar la documentación solicitada a través del registro electrónico de la sede (trámite «Aportar "
            "documentación complementaria»), indicando el número de expediente."
        )},
        {"tipo": "apartado", "texto": "Advertencias"},
        {"tipo": "pequeno", "texto": (
            "La falta de atención a este requerimiento en el plazo concedido podrá constituir infracción tributaria según el artículo 203 de "
            "la Ley General Tributaria. Las actuaciones de comprobación interrumpen el plazo de prescripción (artículo 68 LGT)."
        )},
        *notificacion(puesta, acceso),
    ]


def propuesta_liquidacion(*, party: Party, expediente: str, documento: str, emitido: date, ejercicio: int, periodo: str,
                          declarado: Decimal, comprobado: Decimal, intereses: Decimal, puesta: date | None, acceso: date | None) -> list[dict]:
    diferencia = comprobado - declarado
    return [
        {"tipo": "datos", "filas": destinatario(party) + [
            ("Nº de expediente", expediente), ("Nº de documento", documento), ("Fecha", fecha(emitido)),
            ("Órgano", "Dependencia Regional de Gestión Tributaria (simulada)"),
        ]},
        {"tipo": "titulo", "texto": "TRÁMITE DE ALEGACIONES Y PROPUESTA DE LIQUIDACIÓN PROVISIONAL"},
        {"tipo": "apartado", "texto": "Asunto"},
        {"tipo": "parrafo", "texto": f"Procedimiento de comprobación limitada. Impuesto sobre el Valor Añadido. Ejercicio {ejercicio}. Periodo {periodo}. Modelo 303."},
        {"tipo": "apartado", "texto": "Hechos y fundamentos"},
        {"tipo": "parrafo", "texto": (
            "Como resultado de las actuaciones de comprobación se ha constatado que parte de las cuotas de IVA soportado deducidas en su "
            "autoliquidación no se corresponden con facturas que cumplan los requisitos del artículo 97 de la Ley 37/1992. Se formula la "
            "presente propuesta de liquidación provisional de acuerdo con el artículo 138 de la Ley 58/2003, General Tributaria."
        )},
        {"tipo": "tabla", "cabecera": ["Concepto", "Declarado", "Comprobado", "Diferencia"], "anchos": [70, 33, 33, 34], "filas": [
            ["Resultado de la autoliquidación (casilla 71)", eur(declarado), eur(comprobado), eur(diferencia)],
            ["Intereses de demora (propuesta)", "", "", eur(intereses)],
            ["Total propuesta", "", "", eur(diferencia + intereses)],
        ]},
        {"tipo": "apartado", "texto": "Trámite de alegaciones"},
        {"tipo": "parrafo", "texto": (
            "De acuerdo con el artículo 99.8 de la Ley General Tributaria, dispone de un plazo de 10 días hábiles, contados a partir del día "
            "siguiente al de la notificación de esta propuesta, para formular las alegaciones y aportar los documentos que estime oportunos. "
            "Esta comunicación no es una liquidación: no debe ingresar ningún importe en este momento."
        )},
        {"tipo": "pequeno", "texto": "Si no presenta alegaciones, se dictará liquidación provisional en los términos de la propuesta."},
        *notificacion(puesta, acceso),
    ]


def embargo_creditos(*, pagador: Party, deudor: Party, diligencia: str, emitido: date, principal: Decimal, recargo: Decimal,
                     intereses: Decimal, costas: Decimal, sucesivos: bool = False, acceso: date | None = None, extra: list[dict] | None = None) -> list[dict]:
    total = principal + recargo + intereses + costas
    cuerpo = (
        f"En el procedimiento de apremio seguido contra el deudor {deudor.name}, con NIF {deudor.tax_id}, por las deudas que se detallan, "
        "y de conformidad con los artículos 169 y 170 de la Ley 58/2003, General Tributaria, y 79 a 81 del Reglamento General de "
        "Recaudación, SE DECLARAN EMBARGADOS los créditos que el deudor tenga a su favor frente a ese pagador, hasta cubrir el importe "
        f"pendiente de {eur(total)}."
    )
    if sucesivos:
        cuerpo += (
            " El embargo se extiende a los pagos sucesivos que deba efectuar al deudor (créditos de tracto sucesivo), que deberá retener "
            "a medida que venzan hasta completar el importe indicado."
        )
    return [
        {"tipo": "datos", "filas": [
            ("Pagador (destinatario)", pagador.name), ("NIF del pagador", pagador.tax_id), ("Domicilio", pagador.address),
            ("Nº de diligencia", diligencia), ("Fecha", fecha(emitido)), ("Órgano", "Dependencia Regional de Recaudación (simulada)"),
        ]},
        {"tipo": "titulo", "texto": "DILIGENCIA DE EMBARGO DE CRÉDITOS"},
        {"tipo": "apartado", "texto": "Datos del deudor"},
        {"tipo": "datos", "filas": [("Deudor", deudor.name), ("NIF del deudor", deudor.tax_id), ("Domicilio fiscal", deudor.address)]},
        {"tipo": "apartado", "texto": "Deudas"},
        {"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "anchos": [90, 40], "filas": [
            ["Principal", eur(principal)], ["Recargo de apremio", eur(recargo)], ["Intereses de demora", eur(intereses)], ["Costas", eur(costas)],
            ["Importe pendiente", eur(total)],
        ]},
        {"tipo": "parrafo", "texto": cuerpo},
        *(extra or []),
        {"tipo": "apartado", "texto": "Obligaciones del pagador"},
        {"tipo": "parrafo", "texto": (
            "Deberá ingresar en el Tesoro Público las cantidades retenidas, con el límite del importe pendiente, y comunicar a esta "
            "Dependencia en el plazo de 5 días hábiles la existencia o inexistencia de créditos a favor del deudor. Si a la fecha de esta "
            "diligencia no existe pago pendiente susceptible de retención, deberá comunicarlo igualmente."
        )},
        {"tipo": "pequeno", "texto": "El incumplimiento de la orden de embargo podrá dar lugar a la responsabilidad solidaria del artículo 42.2 LGT."},
        *notificacion(None, acceso),
    ]


def embargo_salarios(*, pagador: Party, empleado: Party, diligencia: str, emitido: date, total: Decimal, acceso: date | None) -> list[dict]:
    return [
        {"tipo": "datos", "filas": [
            ("Pagador (destinatario)", pagador.name), ("NIF del pagador", pagador.tax_id), ("Domicilio", pagador.address),
            ("Nº de diligencia", diligencia), ("Fecha", fecha(emitido)), ("Órgano", "Dependencia Regional de Recaudación (simulada)"),
        ]},
        {"tipo": "titulo", "texto": "DILIGENCIA DE EMBARGO DE SUELDOS, SALARIOS O PENSIONES"},
        {"tipo": "apartado", "texto": "Datos del deudor"},
        {"tipo": "datos", "filas": [("Deudor", empleado.name), ("NIF del deudor", empleado.tax_id), ("Relación con el pagador", "Persona trabajadora por cuenta ajena")]},
        {"tipo": "parrafo", "texto": (
            f"Se declaran embargados los sueldos, salarios y retribuciones que el deudor perciba de ese pagador, en la cuantía que resulte "
            f"de aplicar los límites del artículo 607 de la Ley de Enjuiciamiento Civil, hasta cubrir el importe pendiente de {eur(total)}. "
            "Deberá retener mensualmente la parte embargable e ingresarla en el Tesoro Público."
        )},
        {"tipo": "pequeno", "texto": "La parte inembargable equivalente al salario mínimo interprofesional no podrá ser retenida."},
        *notificacion(None, acceso),
    ]


def justificante_303(*, party: Party, ejercicio: int, trimestre: int, presentado: date, resultado: Decimal, csv: str) -> list[dict]:
    return [
        {"tipo": "titulo", "texto": "JUSTIFICANTE DE PRESENTACIÓN"},
        {"tipo": "datos", "filas": [
            ("Modelo", "303 · IVA. Autoliquidación"), ("Ejercicio", str(ejercicio)), ("Periodo", f"{trimestre}T"),
            ("NIF declarante", party.tax_id), ("Razón social", party.name), ("Fecha y hora de presentación", f"{fecha(presentado)} 10:42:17"),
            ("Número de justificante", f"303{ejercicio}{trimestre}0{csv[-6:]}"), ("Resultado de la autoliquidación", eur(resultado)),
            ("Tipo de declaración", "Ingreso" if resultado > 0 else "A compensar"),
        ]},
        {"tipo": "parrafo", "texto": "La presentación se ha realizado correctamente. Conserve este justificante (simulación)."},
    ]


def certificado_corriente(*, party: Party, emitido: date, numero: str) -> list[dict]:
    return [
        {"tipo": "titulo", "texto": "CERTIFICADO DE ESTAR AL CORRIENTE DE OBLIGACIONES TRIBUTARIAS"},
        {"tipo": "datos", "filas": [("Nº de certificado", numero), ("Fecha de emisión", fecha(emitido)), ("Solicitante", party.name), ("NIF", party.tax_id)]},
        {"tipo": "parrafo", "texto": (
            "La Agencia Estatal de Administración Tributaria CERTIFICA que, conforme a los datos que obran en esta Administración, el "
            f"solicitante arriba indicado se encuentra al corriente de sus obligaciones tributarias a los efectos del artículo 74 del "
            "Reglamento General (RD 1065/2007). Este certificado tiene validez de doce meses desde su emisión. No requiere ninguna actuación."
        )},
    ]


# ---------------------------------------------------------------------
# Seguridad Social
# ---------------------------------------------------------------------


def tgss_requerimiento(*, party: Party, ccc: str, expediente: str, emitido: date, pide: list[str], acceso: date | None) -> list[dict]:
    return [
        {"tipo": "datos", "filas": destinatario(party) + [
            ("Código de cuenta de cotización", ccc), ("Nº de expediente", expediente), ("Fecha", fecha(emitido)),
            ("Órgano", "Unidad de Recaudación Ejecutiva / Área de Afiliación (simulada)"),
        ]},
        {"tipo": "titulo", "texto": "REQUERIMIENTO DE DOCUMENTACIÓN"},
        {"tipo": "parrafo", "texto": (
            "En relación con las liquidaciones de cuotas presentadas para el código de cuenta de cotización indicado, y al amparo del "
            "artículo 18 del texto refundido de la Ley General de la Seguridad Social, se le requiere para que aporte:"
        )},
        {"tipo": "lista", "elementos": [f"{index + 1}. {item}" for index, item in enumerate(pide)]},
        {"tipo": "parrafo", "texto": "Plazo: 10 días hábiles desde el día siguiente al de la notificación, a través del Registro Electrónico de la Seguridad Social."},
        *notificacion(None, acceso),
    ]


def tgss_reclamacion(*, party: Party, ccc: str, documento: str, emitido: date, periodo: str, principal: Decimal, recargo: Decimal, acceso: date | None) -> list[dict]:
    return [
        {"tipo": "datos", "filas": destinatario(party) + [
            ("Código de cuenta de cotización", ccc), ("Nº de reclamación", documento), ("Fecha", fecha(emitido)),
        ]},
        {"tipo": "titulo", "texto": "RECLAMACIÓN DE DEUDA POR DESCUBIERTO TOTAL"},
        {"tipo": "parrafo", "texto": (
            f"No consta el ingreso de las cuotas correspondientes al periodo {periodo}. De acuerdo con el artículo 30 del texto refundido de "
            "la Ley General de la Seguridad Social, se le reclama la deuda que se detalla, que deberá ingresar en el plazo indicado."
        )},
        {"tipo": "tabla", "cabecera": ["Concepto", "Importe"], "anchos": [90, 40], "filas": [
            ["Principal (cuotas)", eur(principal)], ["Recargo (20 %)", eur(recargo)], ["Importe total a ingresar", eur(principal + recargo)],
        ]},
        {"tipo": "parrafo", "texto": (
            "Plazo de ingreso: hasta el último día del mes siguiente al de la notificación. Transcurrido dicho plazo sin ingreso, se "
            "emitirá providencia de apremio con el recargo correspondiente."
        )},
        *notificacion(None, acceso),
    ]


def tgss_comunicacion_laboral(*, party: Party, ccc: str, emitido: date, trabajador: str) -> list[dict]:
    return [
        {"tipo": "datos", "filas": destinatario(party) + [("Código de cuenta de cotización", ccc), ("Fecha", fecha(emitido))]},
        {"tipo": "titulo", "texto": "COMUNICACIÓN DE RESOLUCIÓN SOBRE ALTA"},
        {"tipo": "parrafo", "texto": (
            f"Le comunicamos que se ha reconocido el alta en el Régimen General de la persona trabajadora {trabajador} con efectos del día "
            "indicado en la resolución adjunta. Esta comunicación es meramente informativa y no requiere ninguna actuación por su parte."
        )},
        {"tipo": "pequeno", "texto": "Contra esta resolución podrá interponer recurso de alzada en el plazo de un mes (art. 121 Ley 39/2015)."},
    ]
