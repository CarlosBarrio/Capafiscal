# Seguridad, copias y recuperación

## Copias de seguridad

Una copia es un único archivo con **todas las tablas** (todos los clientes, en formato
portable SQLite ⇄ PostgreSQL) y **todos los documentos** (subidas, adjuntos de expedientes),
con la huella SHA-256 de cada fichero. Con `BACKUP_PASSPHRASE` se cifra.

```bash
cd backend
export BACKUP_PASSPHRASE='una frase larga que no esté en el servidor'
python -m app.backup create /ruta/copias      # crea, verifica y deja las 14 últimas (BACKUP_KEEP)
python -m app.backup verify /ruta/copias/capafiscal-AAAAMMDD-HHMMSS.zip.enc
```

Programarla cada noche (cron del servidor):

```
15 2 * * *  cd /app/backend && BACKUP_PASSPHRASE=… BACKUP_DIR=/copias python -m app.backup create
```

Con Docker: `docker compose exec app python -m app.backup create /data/copias` y copia esa
carpeta **fuera del servidor** (otro proveedor o un disco que no esté siempre conectado).
Con PostgreSQL, además, conviene el `pg_dump` diario del proveedor de la base.

## Recuperación

`restore` solo trabaja sobre una base **vacía** (nunca pisa datos) y con la **misma versión
de esquema** que la copia:

```bash
DATABASE_URL=postgresql+psycopg://…/capafiscal_nueva python -m app.backup restore copia.zip.enc
```

Ensayo de recuperación: `tests/integration/test_backup.py` crea una copia cifrada, la verifica,
la restaura en una base nueva y comprueba que las filas y los documentos son los mismos (también
de PostgreSQL a SQLite). Además, una vez al mes, restaura la última copia real en una base de
prueba y abre CapaFiscal contra ella: una copia que nunca se ha restaurado no es una copia.

## Seguridad en producción

| | |
|---|---|
| Acceso | `AUTH_REQUIRED=true`: usuarios, roles (ADMIN, GESTOR, REVISOR, CLIENTE, LECTURA) y aislamiento por cliente (probado sobre todas las rutas) |
| HTTPS | Siempre, detrás de un proxy (Caddy, nginx). Con `PUBLIC_BASE_URL=https://…` las cookies van marcadas `Secure` |
| Contraseñas | Mínimo 10 caracteres, PBKDF2-SHA256 (200.000 iteraciones). 5 fallos en 15 min bloquean ese correo desde esa IP |
| Sesiones | Cookie `HttpOnly`, `SameSite=Lax`, caducan (`SESSION_HOURS`); las escrituras con cookie exigen la cabecera `X-CapaFiscal` (CSRF) |
| Cabeceras | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store` en la API |
| Secretos | Solo en `.env` del servidor (nunca en el repositorio): claves de IA, agregador bancario, SMTP, `BACKUP_PASSPHRASE` |
| Datos | El repositorio es público: nunca facturas, IBAN, DNI ni NIF reales |

**Pendiente (conocido)**: segundo factor de autenticación; el freno al login vive en memoria
de cada proceso (con varios procesos, cada uno cuenta los suyos); no hay CSP estricta porque
la interfaz usa manejadores en línea.
