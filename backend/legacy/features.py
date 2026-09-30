from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path as _Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


def amount_to_float(a):
    if not a or a == "-":
        return 0.0

    cleaned = (
        str(a)
        .replace("€", "")
        .replace(".", "")
        .replace(",", ".")
        .strip()
    )

    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def eur(v):
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def is_final_state(d):
    status = d.get("status", "").lower()

    return any(
        k in status
        for k in (
            "aprobada",
            "aprobado",
            "incidencia",
            "solicitada",
            "duplicado confirmado",
            "respuesta",
            "respondida",
            "firmada",
            "archivado",
        )
    )


def is_approved_state(d):
    status = d.get("status", "").lower()

    return "aprobada" in status or "aprobado" in status


def is_auto_reliable_state(d):
    status = d.get("status", "").lower()

    return (
        "clasificada automáticamente" in status
        or "clasificada automaticamente" in status
    )


def get_invoice_amount_parts(doc):
    """
    Devuelve base, iva y total de una factura.

    Punto importante:
    Las facturas importadas por el conector simulado antiguo tenían solo amount,
    sin base ni tax. Para que el Panel e Informes no se queden a cero,
    si falta base/tax pero existe total, se calcula una estimación con IVA 21%.

    Esto es válido para demo/MVP, no para producción fiscal definitiva.
    """

    total = amount_to_float(doc.get("amount"))
    base = amount_to_float(doc.get("base"))
    tax = amount_to_float(doc.get("tax"))

    if total <= 0:
        return 0.0, 0.0, 0.0, False

    if base > 0 and tax >= 0:
        return base, tax, total, False

    estimated_base = round(total / 1.21, 2)
    estimated_tax = round(total - estimated_base, 2)

    return estimated_base, estimated_tax, total, True


def descuadre_amount(d):
    base, tax, total, _ = get_invoice_amount_parts(d)

    if base <= 0 or total <= 0:
        return 0.0

    return round(abs(total - (base + tax)), 2)


def build_signature(d):
    supplier_key = (
        d.get("cif")
        or d.get("supplier")
        or ""
    ).strip().lower()

    _, _, total, _ = get_invoice_amount_parts(d)

    invoice_number = (
        d.get("invoice_number")
        or ""
    ).strip().lower()

    return f"{supplier_key}|{total:.2f}|{invoice_number}"


def find_duplicate(new, existing):
    if new.get("document_type") != "Factura proveedor":
        return None

    signature = build_signature(new)

    for doc in existing:
        if doc.get("document_type") == "Factura proveedor" and build_signature(doc) == signature:
            return doc

    return None


def add_business_days(start, n):
    current = start
    added = 0

    while added < n:
        current += timedelta(days=1)

        if current.weekday() < 5:
            added += 1

    return current


def compute_deadline(dtype, days=10, urgency_base=None):
    if dtype != "Notificación AEAT":
        return None

    today = datetime.now()
    deadline = add_business_days(today, days)
    days_left = (deadline.date() - today.date()).days

    urgency = urgency_base

    if not urgency:
        if days_left <= 3:
            urgency = "Crítica"
        elif days_left <= 7:
            urgency = "Alta"
        else:
            urgency = "Media"

    return {
        "deadline_date": deadline.strftime("%d/%m/%Y"),
        "days_left": days_left,
        "urgency": urgency,
        "note": f"Plazo estimado ({days} días hábiles). Pendiente de validación por asesor fiscal.",
    }


URGENCY_WEIGHT = {
    "Crítica": 0,
    "Alta": 1,
    "Media": 2,
    "Baja": 3,
}


def build_notifications(documents):
    notifications = []

    for doc in documents:
        if doc.get("document_type") != "Notificación AEAT":
            continue

        deadline = doc.get("deadline") or {}
        status = doc.get("status", "Pendiente")

        notifications.append(
            {
                "id": doc["id"],
                "concept": doc.get(
                    "aeat_type",
                    doc.get("concept", "Requerimiento"),
                ),
                "supplier": doc.get("supplier", "Agencia Tributaria"),
                "filename": doc.get("filename", ""),
                "date": doc.get("date", "-"),
                "deadline_date": deadline.get("deadline_date", "-"),
                "days_left": deadline.get("days_left") if deadline.get("days_left") is not None else "-",
                "urgency": deadline.get("urgency", "Media"),
                "status": status,
                "resolved": any(
                    k in status.lower()
                    for k in (
                        "respuesta",
                        "incidencia",
                        "respondida",
                    )
                ),
                "summary": doc.get(
                    "summary",
                    doc.get("reason", ""),
                ),
                "requested_docs": doc.get("requested_docs", []),
            }
        )

    notifications.sort(
        key=lambda x: URGENCY_WEIGHT.get(
            x["urgency"],
            2,
        )
    )

    return {
        "count": len(notifications),
        "open_count": len(
            [
                x
                for x in notifications
                if not x["resolved"]
            ]
        ),
        "notifications": notifications,
    }


def build_aeat_summary(text, notification_type):
    text = (text or "").strip()

    if not text:
        return f"{notification_type} recibida. Requiere revisión del contenido."

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    head = " ".join(lines[:4])

    return head[:257] + "…" if len(head) > 260 else head


REQUESTED_KEYWORDS = [
    (
        "libros registro de iva",
        "Libros registro de IVA",
    ),
    (
        "libro registro",
        "Libros registro",
    ),
    (
        "facturas emitidas",
        "Facturas emitidas",
    ),
    (
        "facturas recibidas",
        "Facturas recibidas",
    ),
    (
        "modelo 303",
        "Modelo 303 (IVA)",
    ),
    (
        "modelo 130",
        "Modelo 130 (IRPF)",
    ),
    (
        "modelo 111",
        "Modelo 111 (retenciones)",
    ),
    (
        "justificantes",
        "Justificantes de gasto",
    ),
    (
        "extractos bancarios",
        "Extractos bancarios",
    ),
    (
        "contratos",
        "Contratos",
    ),
]


def detect_requested_docs(text):
    haystack = (text or "").lower()

    found = [
        label
        for keyword, label in REQUESTED_KEYWORDS
        if keyword in haystack
    ]

    return found or [
        "Documentación indicada en el requerimiento",
    ]


def build_aeat_draft(doc):
    fecha = datetime.now().strftime("%d/%m/%Y")

    docs = doc.get(
        "requested_docs",
        [
            "la documentación requerida",
        ],
    )

    docs_text = "\n".join(
        f"   - {item}"
        for item in docs
    )

    ref = (
        doc.get("invoice_number")
        or doc.get("summary", "")[:40]
        or "(referencia)"
    )

    return f"""A LA AGENCIA ESTATAL DE ADMINISTRACIÓN TRIBUTARIA

Fecha: {fecha}
Expediente / Referencia: {ref}

Don/Doña ____________________, con NIF __________, en representación de
____________________, con CIF __________, EXPONE:

Que ha recibido {doc.get('aeat_type','requerimiento')}, en el que se solicita:

{docs_text}

Que aporta en plazo la documentación requerida, que se adjunta.

SOLICITA se tenga por cumplimentado el trámite.

Fdo.: ____________________

---
BORRADOR generado por CapaFiscal. Revísalo y valídalo con un asesor fiscal.
"""


def build_narrative(d):
    supplier = d.get("supplier", "un proveedor")
    amount = d.get("amount", "-")
    classification = d.get("classification", "sin clasificar")
    document_type = d.get("document_type", "documento")
    status = (d.get("status") or "").lower()

    if "respondida" in status:
        return f"Has marcado la notificación de {supplier} como respondida. Trámite cerrado."

    if "incidencia" in status:
        return f"Has abierto una incidencia sobre la factura de {supplier} ({amount}). Pendiente de resolución interna."

    if "solicitada" in status:
        return f"He solicitado una nueva factura a {supplier} por el descuadre. A la espera de la corregida."

    if "duplicado confirmado" in status:
        return f"Has confirmado que la factura de {supplier} es un duplicado. Archivada."

    if "respuesta" in status:
        return f"He preparado un borrador de respuesta a la notificación de {supplier}. Revísalo con tu asesor."

    if "firmada" in status:
        return f"La nómina de {supplier} ({amount}) ha quedado marcada como firmada."

    if "aprobada" in status or "aprobado" in status:
        return f"Has aprobado la factura de {supplier} ({amount}). Lista para contabilizar."

    if d.get("is_duplicate"):
        return f"Posible factura duplicada de {supplier} por {amount}. Ya existe la #{d.get('duplicate_of')}. No pagar dos veces."

    if document_type == "Notificación AEAT":
        deadline = d.get("deadline")
        aeat_type = d.get("aeat_type", "notificación")

        if deadline:
            return f"He recibido un(a) {aeat_type.lower()} de la Agencia Tributaria. Fecha límite estimada: {deadline['deadline_date']} (quedan {deadline['days_left']} días)."

        return f"He recibido un(a) {aeat_type.lower()} de la Agencia Tributaria. Requiere revisión prioritaria."

    if document_type == "Documento escaneado":
        return "PDF escaneado sin texto legible. Necesito OCR para extraer los datos."

    if document_type == "Documento laboral":
        employee = d.get("employee") or "un empleado"
        subtype = d.get("laboral_subtype", "documento laboral")

        if subtype == "Nómina del mes":
            return f"He detectado una nómina de {employee} por {amount}. Falta validarla antes de la firma."

        return f"He archivado el/la {subtype.lower()} de {employee} en su ficha de empleado."

    descuadre = descuadre_amount(d)

    if d.get("risk") == "Medio" and descuadre > 0:
        base, tax, _, _ = get_invoice_amount_parts(d)
        suma = eur(base + tax)

        return f"Factura de {supplier} con un descuadre de {eur(descuadre)} entre base+IVA ({suma}) y total ({amount}). Revísala antes de contabilizar."

    if d.get("risk") == "Medio":
        return f"Factura de {supplier} por {amount} sin confianza suficiente en la clasificación. Revisión manual."

    return f"He clasificado automáticamente una factura de {supplier} ({classification}) por {amount} con un {d.get('confidence', 0)}% de confianza. No requiere acción."


def build_actions(d):
    if is_final_state(d):
        return []

    document_type = d.get("document_type")

    if d.get("is_duplicate"):
        return [
            {
                "code": "mark_duplicate",
                "label": "Confirmar duplicado",
                "variant": "danger",
            },
            {
                "code": "flag_issue",
                "label": "Marcar incidencia",
                "variant": "warning",
            },
            {
                "code": "approve",
                "label": "Aprobar igualmente",
                "variant": "primary",
            },
        ]

    if document_type == "Notificación AEAT":
        return [
            {
                "code": "prepare_response",
                "label": "Preparar respuesta",
                "variant": "primary",
            },
            {
                "code": "mark_responded",
                "label": "Marcar como respondida",
                "variant": "ghost",
            },
            {
                "code": "flag_issue",
                "label": "Marcar incidencia",
                "variant": "warning",
            },
        ]

    if document_type == "Documento laboral":
        if d.get("laboral_subtype") == "Nómina del mes":
            return [
                {
                    "code": "sign_payroll",
                    "label": "Marcar como firmada",
                    "variant": "primary",
                },
                {
                    "code": "flag_issue",
                    "label": "Marcar incidencia",
                    "variant": "warning",
                },
            ]

        return [
            {
                "code": "archive_doc",
                "label": "Archivar en ficha",
                "variant": "primary",
            }
        ]

    if document_type == "Documento escaneado":
        return [
            {
                "code": "flag_issue",
                "label": "Marcar incidencia",
                "variant": "warning",
            }
        ]

    if d.get("risk") == "Medio":
        return [
            {
                "code": "request_invoice",
                "label": "Solicitar nueva factura",
                "variant": "primary",
            },
            {
                "code": "flag_issue",
                "label": "Marcar incidencia",
                "variant": "warning",
            },
            {
                "code": "approve",
                "label": "Aprobar igualmente",
                "variant": "ghost",
            },
        ]

    return [
        {
            "code": "approve",
            "label": "Aprobar",
            "variant": "primary",
        },
        {
            "code": "flag_issue",
            "label": "Marcar incidencia",
            "variant": "warning",
        },
    ]


def needs_action(d):

    status = d.get("status","").lower()

    if "incidencia" in status:
        return True

    if is_final_state(d):
        return False

    return d.get("risk") in (
        "Alto",
        "Medio"
    )


def task_label(d):

    status = d.get("status","").lower()

    if "incidencia" in status:
        return f"Resolver incidencia en {d.get('supplier','proveedor')}"
    
    if d.get("is_duplicate"):
        return f"Revisar posible duplicado de {d.get('supplier','proveedor')}"

    document_type = d.get("document_type")

    if document_type == "Notificación AEAT":
        return f"Atender {d.get('aeat_type','requerimiento').lower()}"

    if document_type == "Documento laboral":
        if d.get("laboral_subtype") == "Nómina del mes":
            return f"Validar nómina de {d.get('employee') or 'empleado'}"

        return f"Archivar {d.get('laboral_subtype','documento').lower()} de {d.get('employee') or 'empleado'}"

    if document_type == "Documento escaneado":
        return "Procesar documento escaneado"

    if descuadre_amount(d) > 0:
        return f"Revisar descuadre en factura de {d.get('supplier','proveedor')}"

    return f"Revisar factura de {d.get('supplier','proveedor')}"


PAYROLL_CHECKLIST = [
    "Contrato de trabajo",
    "Nómina del mes",
    "Alta en Seguridad Social",
    "Modelo 145 (IRPF)",
]


def build_payroll(documents):
    employees = {}

    for doc in documents:
        if doc.get("document_type") != "Documento laboral":
            continue

        name = doc.get("employee") or "Empleado sin identificar"

        if name not in employees:
            employees[name] = {
                "name": name,
                "docs": [],
                "subtypes": set(),
                "amount_total": 0.0,
            }

        subtype = doc.get(
            "laboral_subtype",
            "Nómina del mes",
        )

        employees[name]["docs"].append(
            {
                "id": doc["id"],
                "filename": doc.get("filename", ""),
                "subtype": subtype,
                "amount": doc.get("amount", "-"),
                "date": doc.get("date", "-"),
                "status": doc.get("status", "Pendiente"),
            }
        )

        employees[name]["subtypes"].add(subtype)

        if subtype == "Nómina del mes":
            employees[name]["amount_total"] += amount_to_float(
                doc.get("amount")
            )

    result = []

    for name, data in employees.items():
        checklist = [
            {
                "item": item,
                "done": item in data["subtypes"],
            }
            for item in PAYROLL_CHECKLIST
        ]

        missing = [
            item["item"]
            for item in checklist
            if not item["done"]
        ]

        payrolls = [
            item
            for item in data["docs"]
            if item["subtype"] == "Nómina del mes"
        ]

        result.append(
            {
                "name": name,
                "doc_count": len(data["docs"]),
                "payroll_count": len(payrolls),
                "amount_total": eur(data["amount_total"]),
                "docs": data["docs"],
                "checklist": checklist,
                "missing": missing,
                "complete": len(missing) == 0,
            }
        )

    result.sort(
        key=lambda employee: employee["name"]
    )

    return {
        "employee_count": len(result),
        "employees": result,
    }


def is_reliable_invoice(doc):
    if doc.get("document_type") != "Factura proveedor":
        return False

    if doc.get("is_duplicate"):
        return False

    if doc.get("risk") in ("Alto", "Medio"):
        return False

    if not (
        is_approved_state(doc)
        or is_auto_reliable_state(doc)
    ):
        return False

    base, tax, total, _ = get_invoice_amount_parts(doc)

    if base <= 0 or total <= 0:
        return False

    if abs((base + tax) - total) >= 0.02:
        return False

    return True


def build_tax_summary(documents):
    total_base = 0.0
    total_iva = 0.0
    total_amount = 0.0
    invoice_count = 0
    pending_count = 0
    categories = {}

    for doc in documents:
        if doc.get("document_type") != "Factura proveedor":
            continue

        if not is_reliable_invoice(doc):
            pending_count += 1
            continue

        base, iva, amount, estimated = get_invoice_amount_parts(doc)

        total_base += base
        total_iva += iva
        total_amount += amount
        invoice_count += 1

        category = doc.get("classification") or "Sin clasificar"

        if estimated:
            category = f"{category} · estimado 21%"

        if category not in categories:
            categories[category] = {
                "count": 0,
                "base": 0.0,
                "iva": 0.0,
                "amount": 0.0,
            }

        categories[category]["count"] += 1
        categories[category]["base"] += base
        categories[category]["iva"] += iva
        categories[category]["amount"] += amount

    category_rows = [
        {
            "category": name,
            "count": values["count"],
            "base": eur(values["base"]),
            "iva": eur(values["iva"]),
            "amount": eur(values["amount"]),
        }
        for name, values in sorted(
            categories.items(),
            key=lambda item: -item[1]["amount"],
        )
    ]

    return {
        "invoice_count": invoice_count,
        "pending_count": pending_count,
        "total_base": eur(total_base),
        "total_iva": eur(total_iva),
        "total_amount": eur(total_amount),
        "raw_total_base": total_base,
        "raw_total_iva": total_iva,
        "raw_total_amount": total_amount,
        "categories": category_rows,
    }


TIME_PER_DOC_MIN = 8
TIME_PER_RISK_MIN = 15
COST_PER_HOUR = 25.0


def build_monthly_impact(documents, activity):
    docs = len(documents)

    risks = len(
        [
            doc
            for doc in documents
            if doc.get("risk") in ("Alto", "Medio")
            or doc.get("is_duplicate")
        ]
    )

    minutes = (
        docs * TIME_PER_DOC_MIN
        + risks * TIME_PER_RISK_MIN
    )

    hours = round(minutes / 60, 1)
    cost = round(
        (minutes / 60) * COST_PER_HOUR,
        2,
    )

    return {
        "docs_processed": docs,
        "risks_found": risks,
        "automations": len(activity),
        "hours_saved": hours,
        "cost_saved": eur(cost),
        "note": f"Estimación: {TIME_PER_DOC_MIN} min por documento y {TIME_PER_RISK_MIN} min por riesgo analizado, a {eur(COST_PER_HOUR)}/hora.",
    }


def build_excel(documents):
    wb = Workbook()
    ws = wb.active
    ws.title = "Facturas"

    brand = "9E1B32"

    header_fill = PatternFill(
        "solid",
        fgColor=brand,
    )

    header_font = Font(
        color="FFFFFF",
        bold=True,
        size=11,
    )

    title_font = Font(
        color=brand,
        bold=True,
        size=16,
    )

    subtitle_font = Font(
        color="6F778A",
        size=10,
    )

    money_format = '#,##0.00 €'

    thin_side = Side(
        style="thin",
        color="E8EBF3",
    )

    border = Border(
        left=thin_side,
        right=thin_side,
        top=thin_side,
        bottom=thin_side,
    )

    center = Alignment(
        horizontal="center",
        vertical="center",
    )

    left = Alignment(
        horizontal="left",
        vertical="center",
    )

    ws.merge_cells("A1:H1")
    ws["A1"] = "CapaFiscal · Informe de facturas para gestoría"
    ws["A1"].font = title_font

    ws.merge_cells("A2:H2")
    ws["A2"] = f"Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ws["A2"].font = subtitle_font

    headers = [
        "Proveedor",
        "CIF",
        "Nº factura",
        "Fecha",
        "Base",
        "IVA",
        "Total",
        "Estado",
    ]

    for col, title in enumerate(headers, 1):
        cell = ws.cell(
            row=4,
            column=col,
            value=title,
        )

        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center
        cell.border = border

    row = 5

    total_base = 0.0
    total_iva = 0.0
    total_amount = 0.0

    for doc in documents:
        if doc.get("document_type") != "Factura proveedor":
            continue

        base, iva, amount, _ = get_invoice_amount_parts(doc)

        total_base += base
        total_iva += iva
        total_amount += amount

        values = [
            doc.get("supplier", "-"),
            doc.get("cif", "-") or "-",
            doc.get("invoice_number", "-") or "-",
            doc.get("date", "-") or "-",
            base,
            iva,
            amount,
            doc.get("status", "-"),
        ]

        for col, value in enumerate(values, 1):
            cell = ws.cell(
                row=row,
                column=col,
                value=value,
            )

            cell.border = border
            cell.alignment = left

            if col in (5, 6, 7):
                cell.number_format = money_format
                cell.alignment = Alignment(
                    horizontal="right",
                    vertical="center",
                )

        row += 1

    total_cell = ws.cell(
        row=row,
        column=4,
        value="TOTAL",
    )

    total_cell.font = Font(bold=True)
    total_cell.alignment = Alignment(
        horizontal="right",
        vertical="center",
    )

    for col, value in zip(
        (5, 6, 7),
        (total_base, total_iva, total_amount),
    ):
        cell = ws.cell(
            row=row,
            column=col,
            value=value,
        )

        cell.number_format = money_format
        cell.font = Font(bold=True)
        cell.alignment = Alignment(
            horizontal="right",
            vertical="center",
        )
        cell.border = border

    widths = [
        22,
        14,
        18,
        12,
        14,
        14,
        14,
        24,
    ]

    for col, width in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + col)].width = width

    ws.freeze_panes = "A5"

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return output


MAILBOX_DIR = _Path("mailbox")
MAILBOX_DIR.mkdir(exist_ok=True)


def fetch_mailbox_files():
    return [
        path
        for path in sorted(MAILBOX_DIR.glob("*"))
        if path.suffix.lower() in (
            ".pdf",
            ".txt",
        )
    ]


def _facturas(docs):
    return [
        doc
        for doc in docs
        if doc.get("document_type") == "Factura proveedor"
    ]


def _aeat(docs):
    return [
        doc
        for doc in docs
        if doc.get("document_type") == "Notificación AEAT"
    ]


def _laboral(docs):
    return [
        doc
        for doc in docs
        if doc.get("document_type") == "Documento laboral"
    ]


def answer_assistant(question, documents, activity):
    q = (question or "").lower().strip()

    if not q:
        return {
            "answer": "Hazme una pregunta sobre tus documentos: IVA, plazos de Hacienda, facturas, gasto o empleados.",
            "sources": [],
        }

    tax = build_tax_summary(documents)
    notifications = build_notifications(documents)
    payroll = build_payroll(documents)

    if "iva" in q:
        if tax["invoice_count"] == 0:
            return {
                "answer": "Todavía no tengo facturas fiables para calcular el IVA. Aprueba facturas en la pestaña «Facturas» o sincroniza el correo.",
                "sources": [],
            }

        sources = [
            f"{doc['supplier']} · {doc.get('amount','-')}"
            for doc in _facturas(documents)
            if is_reliable_invoice(doc)
        ][:6]

        pending_text = ""

        if tax["pending_count"]:
            pending_text = f" Hay {tax['pending_count']} factura(s) excluida(s) por estar pendientes, descuadradas o sin validar."

        return {
            "answer": f"Llevas {tax['total_iva']} de IVA soportado este trimestre, sobre una base de {tax['total_base']} en {tax['invoice_count']} factura(s) fiable(s).{pending_text}",
            "sources": sources,
        }

    if any(
        keyword in q
        for keyword in (
            "plazo",
            "vence",
            "vencimiento",
            "urgente",
            "hacienda",
            "aeat",
            "notificacion",
            "notificación",
            "requerimiento",
        )
    ):
        if notifications["count"] == 0:
            return {
                "answer": "No tienes ninguna notificación de Hacienda registrada. Cuando subas un requerimiento o sanción, calcularé aquí su plazo.",
                "sources": [],
            }

        open_notifications = [
            item
            for item in notifications["notifications"]
            if not item["resolved"]
        ]

        if not open_notifications:
            return {
                "answer": f"Tienes {notifications['count']} notificación(es), pero todas están resueltas. No hay plazos abiertos.",
                "sources": [],
            }

        lines = [
            f"{item['concept']} · vence {item['deadline_date']} (quedan {item['days_left']} días, urgencia {item['urgency']})"
            for item in open_notifications
        ]

        next_notification = open_notifications[0]

        return {
            "answer": f"Tienes {len(open_notifications)} plazo(s) abierto(s) con Hacienda. El más urgente: {next_notification['concept']}, vence el {next_notification['deadline_date']} (quedan {next_notification['days_left']} días).",
            "sources": lines,
        }

    if any(
        keyword in q
        for keyword in (
            "gasto",
            "gastado",
            "gasté",
            "cuanto he",
            "cuánto he",
            "total",
        )
    ):
        if tax["invoice_count"] == 0:
            return {
                "answer": "Aún no tengo facturas fiables para sumar el gasto. Aprueba facturas o sincroniza el correo para verlo.",
                "sources": [],
            }

        categories = [
            f"{category['category']}: {category['amount']} ({category['count']} fra)"
            for category in tax["categories"]
        ]

        return {
            "answer": f"Tu gasto validado este trimestre es {tax['total_amount']} en {tax['invoice_count']} factura(s), repartido por categorías:",
            "sources": categories,
        }

    if any(
        keyword in q
        for keyword in (
            "riesgo",
            "problema",
            "descuadre",
            "duplicad",
        )
    ):
        risks = [
            doc
            for doc in documents
            if doc.get("risk") in ("Alto", "Medio")
            and not is_final_state(doc)
        ]

        if not risks:
            return {
                "answer": "No tengo riesgos abiertos ahora mismo. Todo lo procesado está correcto o resuelto.",
                "sources": [],
            }

        lines = [
            f"{task_label(doc)} ({doc.get('supplier','')})"
            for doc in risks
        ]

        return {
            "answer": f"He detectado {len(risks)} riesgo(s) que requieren tu atención:",
            "sources": lines,
        }

    if any(
        keyword in q
        for keyword in (
            "empleado",
            "nomina",
            "nómina",
            "trabajador",
            "laboral",
            "contrato",
        )
    ):
        if payroll["employee_count"] == 0:
            return {
                "answer": "No tengo documentación laboral todavía. Sube nóminas o contratos y crearé la ficha de cada empleado.",
                "sources": [],
            }

        lines = []

        for employee in payroll["employees"]:
            if employee["complete"]:
                status = "documentación completa"
            else:
                status = f"faltan {len(employee['missing'])} doc. ({', '.join(employee['missing'])})"

            lines.append(
                f"{employee['name']}: {employee['doc_count']} doc, {status}"
            )

        return {
            "answer": f"Tienes {payroll['employee_count']} empleado(s) con documentación registrada:",
            "sources": lines,
        }

    if "factura" in q or "proveedor" in q:
        invoices = _facturas(documents)

        if not invoices:
            return {
                "answer": "Aún no hay facturas. Súbelas en la pestaña «Facturas» o sincroniza el correo.",
                "sources": [],
            }

        lines = [
            f"{doc.get('supplier','?')} · {doc.get('amount','-')} · {doc.get('status','')}"
            for doc in invoices
        ][:8]

        return {
            "answer": f"Tienes {len(invoices)} factura(s) registrada(s):",
            "sources": lines,
        }

    if any(
        keyword in q
        for keyword in (
            "resumen",
            "qué tengo",
            "que tengo",
            "hoy",
            "situacion",
            "situación",
            "ayuda",
            "puedes",
        )
    ):
        return {
            "answer": (
                f"Ahora mismo tienes: {len(_facturas(documents))} factura(s), "
                f"{notifications['count']} notificación(es) AEAT ({notifications['open_count']} abierta/s), "
                f"{payroll['employee_count']} empleado(s), IVA soportado {tax['total_iva']} y gasto validado {tax['total_amount']}. "
                f"Puedes preguntarme por IVA, plazos, gasto, riesgos, facturas o empleados."
            ),
            "sources": [],
        }

    return {
        "answer": "No estoy seguro de haber entendido. Puedo responder sobre: IVA soportado, plazos de Hacienda, gasto por categorías, riesgos detectados, facturas y empleados. Prueba con «¿cuánto IVA llevo?» o «¿qué me vence?».",
        "sources": [],
    }