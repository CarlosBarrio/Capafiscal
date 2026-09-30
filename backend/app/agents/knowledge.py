"""
Base de conocimiento de los agentes: qué documentos se piden habitualmente,
cuáles puede preparar el propio sistema y qué hay que hacer en cada tipo de
trámite. Es conocimiento versionado y revisable, no una caja negra.
"""
from __future__ import annotations

from typing import Any

KNOWLEDGE_VERSION = "2026.09"

# source: system (lo genera CapaFiscal), internal (lo aporta la empresa),
#         third (lo emite un tercero: banco, notario, cliente…)
DOCUMENTS: dict[str, dict[str, Any]] = {
    "LIBRO_EMITIDAS": {
        "label": "Libro registro de facturas expedidas",
        "keywords": ("libro registro de facturas expedidas", "libro de facturas expedidas", "facturas expedidas", "facturas emitidas", "libro registro de facturas emitidas", "libros registro"),
        "source": "system",
    },
    "LIBRO_RECIBIDAS": {
        "label": "Libro registro de facturas recibidas",
        "keywords": ("libro registro de facturas recibidas", "facturas recibidas", "libros registro"),
        "source": "system",
    },
    "FACTURAS": {
        "label": "Copia de las facturas del periodo",
        "keywords": ("copia de las facturas", "copias de facturas", "justificantes de los gastos", "facturas justificativas", "originales de las facturas", "facturas que justifiquen"),
        "source": "system",
    },
    "EXTRACTOS": {
        "label": "Extractos bancarios del periodo",
        "keywords": ("extractos bancarios", "extracto bancario", "movimientos bancarios", "extractos de las cuentas", "movimientos de las cuentas"),
        "source": "system",
    },
    "MODELOS": {
        "label": "Autoliquidaciones del periodo",
        "keywords": ("autoliquidacion", "declaracion presentada", "copia del modelo"),
        "source": "system",
    },
    "NOMINAS": {
        "label": "Recibos de salarios (nóminas)",
        "keywords": ("nominas", "recibos de salarios", "recibos de salario"),
        "source": "system",
    },
    "CONTRATOS_TRABAJO": {
        "label": "Contratos de trabajo",
        "keywords": ("contratos de trabajo", "contrato de trabajo", "contratos laborales", "contrato laboral"),
        "source": "system",
    },
    "RELACION_CREDITOS": {
        "label": "Relación de créditos pendientes con el deudor embargado",
        "keywords": ("creditos pendientes", "relacion de creditos", "saldos pendientes"),
        "source": "system",
    },
    "RNT": {
        "label": "Relación nominal de trabajadores (RNT) y liquidación de cotizaciones (RLC)",
        "keywords": ("relacion nominal de trabajadores", "rnt", "rlc", "tc2", "boletines de cotizacion", "liquidacion de cotizaciones"),
        "source": "internal",
    },
    "JUSTIFICANTE_PAGO": {
        "label": "Justificante de pago",
        "keywords": ("justificante de pago", "justificante del ingreso", "justificantes de pago", "carta de pago", "acreditacion del pago"),
        "source": "internal",
    },
    "CONTABILIDAD": {
        "label": "Libros contables (diario, mayor, balance)",
        "keywords": ("libro diario", "libro mayor", "balance de sumas", "cuenta de perdidas y ganancias", "contabilidad", "libros contables"),
        "source": "internal",
    },
    "CONTRATOS": {
        "label": "Contratos mercantiles o de arrendamiento",
        "keywords": ("contrato de arrendamiento", "contratos de arrendamiento", "contrato de prestacion", "contratos suscritos", "contrato mercantil"),
        "source": "third",
    },
    "ESCRITURAS": {
        "label": "Escrituras y poderes",
        "keywords": ("escritura", "escrituras", "estatutos", "poder notarial", "poderes"),
        "source": "third",
    },
    "REPRESENTACION": {
        "label": "Acreditación de la representación",
        "keywords": ("acreditacion de la representacion", "documento que acredite la representacion", "representacion"),
        "source": "internal",
    },
    "CERTIFICADO_BANCARIO": {
        "label": "Certificado bancario de titularidad",
        "keywords": ("certificado de titularidad", "titularidad de la cuenta", "certificado bancario"),
        "source": "third",
    },
    "PRESTAMOS": {
        "label": "Contratos de préstamo o financiación",
        "keywords": ("prestamo", "prestamos", "financiacion", "leasing", "renting"),
        "source": "third",
    },
}

SOURCE_LABELS = {
    "system": "Lo prepara el agente con tus datos",
    "internal": "Tienes que aportarlo tú",
    "third": "Hay que pedirlo a un tercero",
}

# Cómo se contesta cada tipo de trámite.
PROCEDURES: dict[str, dict[str, Any]] = {
    "REQUERIMIENTO": {
        "label": "Requerimiento de información",
        "base_score": 35,
        "default_documents": [],
        "actions": [
            "Revisar qué se pide exactamente y de qué periodo",
            "Reunir la documentación (el agente ya ha preparado la que tiene)",
            "Revisar y aprobar el escrito de contestación",
            "Presentarlo en la sede electrónica antes del plazo y guardar el justificante",
        ],
        "letter": "requerimiento",
        "note": "No atender un requerimiento puede suponer una sanción (art. 203 LGT).",
    },
    "PROPUESTA_LIQUIDACION": {
        "label": "Propuesta de liquidación (trámite de alegaciones)",
        "base_score": 35,
        "default_documents": ["FACTURAS", "LIBRO_RECIBIDAS"],
        "actions": [
            "Comparar la propuesta con tus datos (el agente ha calculado la diferencia)",
            "Decidir: alegar con documentación o aceptar la propuesta",
            "Revisar y aprobar el escrito de alegaciones",
            "Presentarlo antes del plazo",
        ],
        "letter": "alegaciones",
        "note": "Si no se alega, la Administración dictará la liquidación en los términos propuestos.",
    },
    "LIQUIDACION": {
        "label": "Liquidación con deuda a ingresar",
        "base_score": 30,
        "default_documents": [],
        "actions": [
            "Comprobar el importe frente a tus datos",
            "Pagar en periodo voluntario (art. 62.2 LGT) o pedir aplazamiento",
            "Si no estás de acuerdo: recurso de reposición en 1 mes o reclamación económico-administrativa",
            "Guardar el justificante de pago en el expediente",
        ],
        "letter": "recurso",
        "note": "Si no se paga en plazo se inicia el periodo ejecutivo con recargos del 5 %, 10 % o 20 %.",
    },
    "APREMIO": {
        "label": "Providencia de apremio",
        "base_score": 50,
        "default_documents": [],
        "actions": [
            "Pagar antes del plazo del art. 62.5 LGT para quedarte en el recargo reducido del 10 %",
            "Si no puedes pagar: solicitar aplazamiento o fraccionamiento",
            "Guardar el justificante de pago en el expediente",
        ],
        "letter": "aplazamiento",
        "note": "Pasado el plazo, el recargo sube al 20 % más intereses y puede haber embargo.",
    },
    "EMBARGO": {
        "label": "Diligencia de embargo",
        "base_score": 55,
        "default_documents": [],
        "actions": [
            "Identificar a quién se embarga y qué hay que retener",
            "Retener los pagos o importes afectados desde hoy",
            "Contestar la diligencia informando de lo retenido",
            "Ingresar lo retenido en la cuenta que indique la Administración",
        ],
        "letter": "embargo",
        "note": "Pagar al embargado ignorando la diligencia te convierte en responsable solidario (art. 42.2 LGT).",
    },
    "SANCION": {
        "label": "Procedimiento sancionador",
        "base_score": 35,
        "default_documents": [],
        "actions": [
            "Valorar alegar o dar la conformidad",
            "Si das la conformidad: reducción del 30 %; y si pagas en plazo sin recurrir, un 25 % adicional (art. 188 LGT)",
            "Revisar y aprobar el escrito",
        ],
        "letter": "alegaciones",
        "note": "El plazo habitual de alegaciones es de 15 días hábiles.",
    },
    "COMUNICACION": {
        "label": "Comunicación informativa",
        "base_score": 10,
        "default_documents": [],
        "actions": ["Leer la comunicación y archivarla si no requiere acción"],
        "letter": None,
        "note": None,
    },
    "OTRO": {
        "label": "Otra notificación",
        "base_score": 15,
        "default_documents": [],
        "actions": ["Revisar la notificación y decidir si requiere respuesta"],
        "letter": None,
        "note": None,
    },
}

ORGANISM_HEADERS = {
    "AEAT": "A LA AGENCIA ESTATAL DE ADMINISTRACIÓN TRIBUTARIA",
    "TGSS": "A LA TESORERÍA GENERAL DE LA SEGURIDAD SOCIAL",
    "DGT": "A LA JEFATURA PROVINCIAL DE TRÁFICO",
    "AYUNTAMIENTO": "AL AYUNTAMIENTO",
    "CCAA": "A LA ADMINISTRACIÓN TRIBUTARIA AUTONÓMICA",
    "OTRO": "A LA ADMINISTRACIÓN ACTUANTE",
}


def match_documents(normalized_text: str) -> list[str]:
    """Documentos del catálogo que el texto (ya normalizado) menciona."""
    found: list[str] = []
    for code, item in DOCUMENTS.items():
        if any(keyword in normalized_text for keyword in item["keywords"]):
            found.append(code)
    # "libros registro" a secas: los dos libros; si se piden facturas
    # expedidas explícitamente, no añadir por error el de recibidas.
    return found
