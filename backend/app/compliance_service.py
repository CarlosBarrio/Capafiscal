"""
Estado de cumplimiento: certificado digital, Verifactu, apoderamientos,
RGPD y controles automáticos sobre los datos de CapaFiscal.
"""
from __future__ import annotations

from app import clock
import re
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar_es import days_until
from app.company_service import legal_form
from app.models import ComplianceItem
from app.models import FiscalNotification
from app.models import Invoice


CERT_WARNING_DAYS = 30

# Fechas de obligatoriedad de Verifactu (RD 1007/2023 y modificaciones).
VERIFACTU_DATES = {
    "SOCIEDAD": date(2027, 1, 1),
    "AUTONOMO": date(2027, 7, 1),
}

MANUAL_ITEMS: dict[str, dict[str, Any]] = {
    "CERT_DIGITAL": {
        "title": "Certificado digital de la empresa",
        "description": (
            "Necesario para la sede de la AEAT, DEHú y Seguridad Social. "
            "Sube el certificado para vigilar su caducidad."
        ),
        "group": "Accesos",
        "expires": True,
    },
    "APODERAMIENTO_AEAT": {
        "title": "Apoderamiento a tu gestoría en la AEAT",
        "description": (
            "Permite que tu asesor presente modelos y atienda "
            "notificaciones en tu nombre. Indica si existe y hasta cuándo."
        ),
        "group": "Accesos",
        "expires": True,
    },
    "DEHU_AVISOS": {
        "title": "Avisos de DEHú activados",
        "description": (
            "Recibir aviso por correo de cada notificación puesta a "
            "disposición evita que se den por notificadas sin enterarte."
        ),
        "group": "Accesos",
        "expires": False,
    },
    "VERIFACTU": {
        "title": "Software de facturación adaptado a Verifactu",
        "description": (
            "Tu programa de facturación debe generar registros verificables "
            "y el código QR de la AEAT."
        ),
        "group": "Facturación",
        "expires": False,
    },
    "FACTURA_ELECTRONICA": {
        "title": "Preparado para factura electrónica B2B",
        "description": (
            "La factura electrónica entre empresas será obligatoria de forma "
            "escalonada según tamaño. Comprueba con tu proveedor de software."
        ),
        "group": "Facturación",
        "expires": False,
    },
    "RGPD_REGISTRO": {
        "title": "Registro de actividades de tratamiento (RGPD)",
        "description": (
            "Documento que describe qué datos personales tratas, para qué y "
            "durante cuánto tiempo."
        ),
        "group": "Protección de datos",
        "expires": False,
    },
    "RGPD_ENCARGADOS": {
        "title": "Contratos de encargado del tratamiento",
        "description": (
            "Con tu gestoría, proveedores de software y cualquiera que "
            "acceda a datos personales por tu cuenta."
        ),
        "group": "Protección de datos",
        "expires": False,
    },
}

STATUSES = ("OK", "PENDING", "WARNING", "NOT_APPLICABLE")


def utc_date(value: datetime) -> date:
    return value.date()


# -------------------------------------------------------------------
# Certificado digital
# -------------------------------------------------------------------

def _name_attribute(name, oid) -> str | None:
    values = name.get_attributes_for_oid(oid)
    return values[0].value if values else None


def parse_certificate(content: bytes, password: str | None) -> dict[str, Any]:
    """
    Lee los metadatos de un certificado (.cer/.crt/.pem o .p12/.pfx).
    El fichero y la contraseña se usan solo en memoria y no se guardan.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    certificate = None
    errors: list[str] = []

    for loader in (x509.load_pem_x509_certificate, x509.load_der_x509_certificate):
        try:
            certificate = loader(content)
            break
        except ValueError as error:
            errors.append(str(error))

    if certificate is None:
        try:
            _key, certificate, _extra = pkcs12.load_key_and_certificates(
                content,
                password.encode("utf-8") if password else None,
            )
        except ValueError as error:
            raise ValueError(
                "No se pudo leer el certificado. Si es .p12/.pfx, comprueba "
                "la contraseña."
            ) from error

    if certificate is None:
        raise ValueError("El archivo no contiene un certificado.")

    subject = certificate.subject
    not_after = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after
    not_before = getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before

    serial_attribute = _name_attribute(subject, NameOID.SERIAL_NUMBER) or ""
    organization_id = None

    try:
        organization_id = _name_attribute(
            subject,
            x509.ObjectIdentifier("2.5.4.97"),  # organizationIdentifier
        )
    except Exception:  # noqa: BLE001 - atributo opcional
        organization_id = None

    tax_id = None

    for raw in (organization_id or "", serial_attribute):
        match = re.search(r"([A-Z]?\d{7,8}[A-Z0-9])", raw.replace("-", ""))

        if match:
            tax_id = match.group(1)
            break

    return {
        "subject": _name_attribute(subject, NameOID.COMMON_NAME),
        "organization": _name_attribute(subject, NameOID.ORGANIZATION_NAME),
        "issuer": _name_attribute(certificate.issuer, NameOID.COMMON_NAME)
        or _name_attribute(certificate.issuer, NameOID.ORGANIZATION_NAME),
        "tax_id": tax_id,
        "serial_number": format(certificate.serial_number, "X"),
        "valid_from": utc_date(not_before).isoformat(),
        "valid_until": utc_date(not_after).isoformat(),
    }


def store_certificate(
    database: Session,
    *,
    content: bytes,
    password: str | None,
    actor: str,
) -> ComplianceItem:
    from app.invoice_service import add_audit_event

    info = parse_certificate(content, password)
    item = get_or_create_item(database, "CERT_DIGITAL")
    item.details = info
    item.expires_at = date.fromisoformat(info["valid_until"])
    item.status = "OK"
    database.flush()

    add_audit_event(
        database,
        action="compliance.certificate_loaded",
        entity_type="compliance",
        entity_id=item.id,
        actor=actor,
        event_data={
            "subject": info["subject"],
            "valid_until": info["valid_until"],
            "issuer": info["issuer"],
        },
    )

    return item


# -------------------------------------------------------------------
# Elementos manuales
# -------------------------------------------------------------------

def get_or_create_item(database: Session, code: str) -> ComplianceItem:
    if code not in MANUAL_ITEMS:
        raise ValueError("Elemento de cumplimiento no reconocido.")

    item = database.scalar(select(ComplianceItem).where(ComplianceItem.code == code))

    if item is None:
        item = ComplianceItem(code=code, status="PENDING", details={})
        database.add(item)
        database.flush()

    return item


def update_item(
    database: Session,
    *,
    code: str,
    status: str | None,
    expires_at: date | None,
    notes: str | None,
    clear_expiry: bool,
    actor: str,
) -> ComplianceItem:
    from app.invoice_service import add_audit_event

    item = get_or_create_item(database, code)

    if status is not None:
        if status not in STATUSES:
            raise ValueError("Estado no válido.")
        item.status = status

    if expires_at is not None:
        item.expires_at = expires_at
    elif clear_expiry:
        item.expires_at = None

    if notes is not None:
        item.notes = notes.strip() or None

    database.flush()

    add_audit_event(
        database,
        action="compliance.updated",
        entity_type="compliance",
        entity_id=item.id,
        actor=actor,
        event_data={
            "code": code,
            "status": item.status,
            "expires_at": item.expires_at,
        },
    )

    return item


def effective_status(
    code: str,
    item: ComplianceItem | None,
    today: date,
) -> tuple[str, str | None]:
    """Estado mostrado y mensaje, teniendo en cuenta caducidades."""
    if item is None:
        return "PENDING", None

    if item.status == "NOT_APPLICABLE":
        return "NOT_APPLICABLE", None

    if item.expires_at is not None:
        remaining = days_until(item.expires_at, today)

        if remaining < 0:
            return "EXPIRED", f"Caducó el {item.expires_at.strftime('%d/%m/%Y')}."

        if remaining <= CERT_WARNING_DAYS:
            return "WARNING", f"Caduca en {remaining} día(s): renuévalo ya."

        if item.status == "OK":
            return "OK", f"Válido hasta el {item.expires_at.strftime('%d/%m/%Y')}."

    return item.status, None


# -------------------------------------------------------------------
# Controles automáticos
# -------------------------------------------------------------------

def automatic_checks(database: Session, today: date) -> list[dict[str, Any]]:
    from app.tax_service import build_tax_calendar

    checks: list[dict[str, Any]] = []

    stale_limit = today - timedelta(days=30)
    stale = database.scalars(
        select(Invoice).where(
            Invoice.review_status == "PENDING",
            Invoice.created_at <= datetime.combine(stale_limit, datetime.min.time()),
        )
    ).all()
    checks.append(
        {
            "code": "AUTO_LIBROS",
            "title": "Libros registro de IVA al día",
            "description": "Facturas revisadas en menos de 30 días desde su recepción.",
            "group": "Controles automáticos",
            "status": "WARNING" if stale else "OK",
            "message": (
                f"{len(stale)} factura(s) llevan más de 30 días sin revisar."
                if stale
                else "Todas las facturas están revisadas a tiempo."
            ),
            "automatic": True,
        }
    )

    overdue_models = [
        entry
        for entry in build_tax_calendar(database, year=today.year, today=today)["entries"]
        if entry["status"] == "OVERDUE"
    ]
    checks.append(
        {
            "code": "AUTO_MODELOS",
            "title": "Modelos tributarios presentados en plazo",
            "description": "Según el calendario fiscal y los modelos marcados como presentados.",
            "group": "Controles automáticos",
            "status": "WARNING" if overdue_models else "OK",
            "message": (
                "Sin marcar como presentados: "
                + ", ".join(
                    f"{entry['model']} {entry['period_label']}"
                    for entry in overdue_models[:5]
                )
                if overdue_models
                else "No hay modelos vencidos pendientes."
            ),
            "automatic": True,
        }
    )

    overdue_notifications = database.scalars(
        select(FiscalNotification).where(
            FiscalNotification.status.in_({"PENDING", "IN_PROGRESS"}),
            FiscalNotification.deadline < today,
        )
    ).all()
    checks.append(
        {
            "code": "AUTO_NOTIFICACIONES",
            "title": "Notificaciones atendidas en plazo",
            "description": "Notificaciones abiertas cuyo plazo ya ha vencido.",
            "group": "Controles automáticos",
            "status": "WARNING" if overdue_notifications else "OK",
            "message": (
                f"{len(overdue_notifications)} notificación(es) con plazo vencido."
                if overdue_notifications
                else "Ninguna notificación con plazo vencido."
            ),
            "automatic": True,
        }
    )

    return checks


def build_compliance_status(
    database: Session,
    today: date | None = None,
) -> dict[str, Any]:
    current_day = today or clock.today()
    stored = {
        item.code: item
        for item in database.scalars(select(ComplianceItem)).all()
    }
    form = legal_form(database)

    items: list[dict[str, Any]] = []

    for code, meta in MANUAL_ITEMS.items():
        item = stored.get(code)
        status, message = effective_status(code, item, current_day)
        extra: dict[str, Any] = {}

        if code == "VERIFACTU":
            obligation = VERIFACTU_DATES.get(form, VERIFACTU_DATES["SOCIEDAD"])
            remaining = days_until(obligation, current_day)
            extra["obligation_date"] = obligation.isoformat()

            if status != "OK" and status != "NOT_APPLICABLE":
                message = (
                    f"Obligatorio para {'autónomos' if form == 'AUTONOMO' else 'sociedades'} "
                    f"desde el {obligation.strftime('%d/%m/%Y')}"
                    + (f" (faltan {remaining} días)." if remaining >= 0 else ".")
                )

                if 0 <= remaining <= 180:
                    status = "WARNING"

        items.append(
            {
                "code": code,
                "title": meta["title"],
                "description": meta["description"],
                "group": meta["group"],
                "status": status,
                "message": message,
                "expires_at": item.expires_at.isoformat() if item and item.expires_at else None,
                "supports_expiry": meta["expires"],
                "notes": item.notes if item else None,
                "details": item.details if item else {},
                "automatic": False,
                **extra,
            }
        )

    items.extend(automatic_checks(database, current_day))

    applicable = [item for item in items if item["status"] != "NOT_APPLICABLE"]
    ok = [item for item in applicable if item["status"] == "OK"]
    score = round(len(ok) / len(applicable) * 100) if applicable else 100

    return {
        "score": score,
        "ok": len(ok),
        "total": len(applicable),
        "items": items,
        "note": (
            "Checklist de ayuda, no un dictamen legal. Los controles "
            "automáticos se calculan con los datos registrados en CapaFiscal."
        ),
    }
