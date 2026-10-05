"""
Conectores: traen eventos de fuentes externas a la entrada común.

    FUENTE ─→ conector (cliente → parser → mapper) ─→ intake.ingest ─→ orquestador

Cada conector vive aislado aquí: ningún agente (Vigilante, Fiscal…) sabe
de dónde viene un documento. Para añadir una fuente nueva basta con
transformar lo que entrega en un evento (source, external_id, kind,
payload) y llamar a la entrada común; la idempotencia, los estados y la
reanudación ya los da intake.

    email/   correo entrante: IMAP, carpeta local de .eml o importación manual
    dehu/    DEHú: pendiente del alta, el certificado y el apoderamiento
"""
