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

## PostgreSQL: lo que está comprobado (esquema congelado)

Base de producción: PostgreSQL 16. La batería completa pasa igual en PostgreSQL y en SQLite, y
estas garantías tienen su prueba:

| Qué | Prueba |
| --- | --- |
| Una base nueva se crea con las migraciones y coincide con los modelos (sin diferencias) | `test_migrations` |
| Una base anterior a Alembic se adopta sin perder datos; la actualización incremental conserva datos | `test_migrations` |
| Cada migración se deshace y se rehace (una actualización fallida tiene marcha atrás) | `test_migrations` |
| Copia → restauración en una base PostgreSQL vacía → CapaFiscal sigue creando registros (los contadores de id continúan) | `test_backup` |
| Un cliente nunca ve ni toca datos de otro, en todas las rutas | `test_cross_tenant`, `test_multiempresa` |
| Varios procesos (`uvicorn --workers N`, varias instancias): cada automatización se ejecuta una sola vez por cliente (cerrojo consultivo) | `test_concurrency` |
| Dos personas concilian el mismo movimiento a la vez: queda con una sola factura y la otra recibe un aviso (bloqueo de fila) | `test_concurrency` |
| Dos peticiones crean lo mismo (doble clic): 409 explicable, nunca 500 ni duplicado | `test_concurrency` |
| 100 entradas simultáneas sin pérdidas ni duplicados | `test_resistencia` |
| Las simulaciones («¿y si…?») no dejan rastro: se deshacen siempre | `test_treasury`, `test_workcenter` |

Todas las pruebas ven el mismo «hoy» (`app/clock.py`, fijado a 1-10-2026): no hay tests que pasen
un día y fallen al siguiente. **El esquema queda congelado**: un cambio de modelo necesita una
migración revisada y volver a pasar la batería en PostgreSQL.

SQLite sigue sirviendo para una empresa en un solo equipo y un solo proceso.

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
