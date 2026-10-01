"""
Conector DEHú (pendiente).

La conexión oficial necesita alta en la DEHú, certificado electrónico de la
empresa y, para una gestoría, apoderamiento de cada cliente. Cuando exista,
solo tendrá que convertir cada notificación en un evento y entregarlo:

    intake.ingest(database, source="dehu", external_id=<identificador DEHú>,
                  kind="notification", payload={"notification": {...}})

o por HTTP: POST /api/events {"kind": "notification", "source": "dehu",
"external_id": ..., "notification": {...}}. El resto del sistema ya está
preparado (idempotencia incluida: la misma notificación no abre dos
expedientes).
"""
