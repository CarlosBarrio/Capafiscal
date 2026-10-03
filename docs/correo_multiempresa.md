# Correo y fuentes compartidas en multiempresa

## El problema

El buzón IMAP (`IMAP_*`), la carpeta `data/buzon`, la carpeta de la DEHú (`DEHU_INBOX_DIR`) y la conexión
de Outlook se configuran una sola vez para toda la instalación. Los datos de cada cliente viven aislados
(`app/tenancy.py`). Antes de esta corrección, el planificador ejecutaba el buzón dentro de la sesión de
cada cliente, por orden: el primero se quedaba con todo el correo, fuera de quien fuera.

## Lo que hay ahora (mínimo seguro)

| Fuente | Una empresa (`AUTH_REQUIRED=false`) | Multiempresa (`AUTH_REQUIRED=true`) |
|---|---|---|
| Buzón IMAP y `data/buzon` | Como siempre | Solo para el cliente `EMAIL_CLIENT_ID`. Sin él, para nadie: los correos quedan sin leer |
| Carpeta de la DEHú | Como siempre | Solo para el cliente `DEHU_CLIENT_ID`. Sin él, para nadie |
| Outlook (Microsoft Graph) | Como siempre | No disponible (409): una conexión y una caché de tokens para toda la instalación |
| Importar un `.eml` a mano | Como siempre | Permitido: es una decisión explícita de la persona para el cliente que ha elegido |
| `POST /api/connectors/dehu/import` | Como siempre | Permitido: la persona elige el cliente |

Regla: **nunca se reparte una fuente compartida entre clientes**. Es preferible dejar el correo sin asignar
(sin leer) que guardarlo en el cliente equivocado. Código: `app/connectors/assignment.py`; tests:
`tests/integration/test_mail_isolation.py`. `python -m app.production` avisa si falta la asignación.

## Opciones para habilitarlo de verdad

**A. Buzón por cliente.** Cada cliente tiene su cuenta IMAP (o carpeta, o conexión Outlook) y sus credenciales.
- A favor: encaja con el aislamiento actual (todo lo del cliente, en su sesión); sin reparto ni ambigüedad.
- En contra: credenciales por cliente; hoy solo hay variables de entorno globales.
- Falta: tabla `mail_accounts` (con `tenant_id`) con credenciales cifradas (`APP_ENCRYPTION_KEY`, Fernet ya se
  usa para Outlook), caché de tokens de Outlook por cliente, el planificador leyendo la cuenta del cliente en
  su sesión y la subida interna de Outlook sin pasar por HTTP (hoy hace un `POST /api/upload` interno sin
  credenciales, que con `AUTH_REQUIRED=true` no funcionaría).

**B. Buzón común de gestoría con reparto.** Un buzón recibe todo y se reparte por NIF del documento, dirección
o dominio del remitente y reglas explícitas, con una cola de «sin asignar».
- A favor: una sola cuenta que configurar (lo habitual en una gestoría).
- En contra: hace falta guardar el correo ANTES de saber de quién es, y hoy todo documento nace dentro de
  un cliente. Además, el NIF solo se conoce después de leer el PDF, y un error de reparto es justo el fallo
  que queremos evitar.
- Falta: tabla global (sin `tenant_id`) de correo entrante pendiente de asignar, con acceso solo para el
  administrador; reglas de reparto; pantalla de «sin asignar»; mover el documento al cliente una vez decidido;
  y tests de reparto con NIF ambiguos.

**Encaje con la arquitectura actual: A.** Respeta el principio de que todo dato nace dentro de un cliente y
reutiliza el filtro de `tenancy.py` sin excepciones. B obliga a crear la primera zona de datos de cliente
fuera del aislamiento. Si se elige B en el futuro, la cola de «sin asignar» debe ser lo único que viva fuera
del cliente y debe pasar el barrido de `test_cross_tenant.py`.

Ninguna de las dos se ha implementado: ambas necesitan rediseñar el almacenamiento de credenciales (A) o
crear un almacén previo al cliente (B), y esa decisión es de producto.
