"""
Conector DEHú: DEHú → adaptador → evento → /api/events (entrada común).

    adapter.py   metadatos + PDF de una notificación → documento + notificación
                 con las fechas oficiales → evento «dehu:<id>» (idempotente)
    client.py    transportes: carpeta con lo descargado de la DEHú (hoy) y,
                 cuando haya alta, certificado y apoderamientos, la conexión
                 directa (mismo contrato `pending()`)

Por HTTP: POST /api/connectors/dehu/import con los metadatos y el PDF en
base64, o POST /api/connectors/dehu/poll para leer la carpeta configurada
(DEHU_INBOX_DIR).
"""
