# CapaFiscal

Administrativo digital para pymes, autónomos y gestorías. Recoge la
documentación (facturas, extractos bancarios, notificaciones), la entiende,
detecta riesgos y plazos, prepara los impuestos y solo pide intervención
humana cuando hace falta criterio.

> Los datos extraídos y los borradores de impuestos son orientativos y deben
> revisarse antes de usarse fiscalmente. CapaFiscal ayuda a preparar, no
> presenta nada ante la Administración.

## Qué hace

| Área | Funcionalidad |
|---|---|
| **Hoy** | Saludo con el trabajo del agente en las últimas 24 h, IVA estimado del trimestre, resultado, pendiente de pago, siguiente paso recomendado y **agenda unificada** (impuestos, notificaciones, cobros, pagos y caducidades) |
| **Facturas** | Recibidas y **emitidas** (se distinguen con el NIF de tu empresa) · subida múltiple o arrastrando · extracción de NIF/CIF, número, fechas, base, IVA por tipo, IRPF, recargo, total y categoría con cuenta del PGC · control de cuadre, duplicados y confianza por campo · aprobar, rechazar, reabrir, registrar pago o cobro |
| **Memoria del agente** | Cuando corriges la categoría de un proveedor o cliente, la recuerda y la aplica a sus siguientes facturas |
| **Negocio** | Ingresos, gastos, resultado y margen · gráfico de 12 meses · periodo medio de cobro y pago · dependencia de clientes · cobros pendientes y vencidos |
| **Tesorería y banco** | Importación de extractos CSV/Excel de cualquier banco (detecta columnas y formatos) · **conciliación automática** con facturas (propuestas que tú confirmas) · previsión de caja a 90 días con cobros, pagos e impuestos estimados |
| **Impuestos** | Calendario fiscal según seas autónomo o sociedad (303, 130, 111, 115, 390, 190, 180, 347, 202, 200, 100) con festivos nacionales · **borradores continuos** de los modelos 303, 130, 111 y 115 por casillas · modelo 347 por tercero y trimestre · registro de presentaciones |
| **Notificaciones** | Detección automática de requerimientos, liquidaciones, apremios, embargos y sanciones de AEAT, Seguridad Social, DGT, ayuntamientos… · referencia e importe · **plazo calculado** con su regla (días hábiles, art. 62 LGT, art. 43.2 Ley 39/2015) · tarea en la bandeja · registro manual |
| **Cumplimiento** | Caducidad del certificado digital (lee .cer/.pem/.p12 sin guardar el archivo ni la contraseña) · apoderamientos, DEHú, Verifactu, factura electrónica y RGPD · controles automáticos (libros al día, modelos y notificaciones en plazo) |
| **Terceros** | Proveedores y clientes por NIF: importes, IVA, pendiente y última factura |
| **Informes** | IVA soportado y repercutido por trimestre, gasto por categoría y mes · **libros registro de facturas recibidas y expedidas** en Excel o CSV |
| **Asistente** | Preguntas sobre IVA y 303 de un trimestre, modelos a presentar, salud del negocio, notificaciones, pagos, un proveedor, riesgos o tareas |
| **Conectores** | Outlook (Microsoft Graph) para importar adjuntos · carga manual · extractos bancarios |
| **Trazabilidad** | Auditoría de todas las acciones de personas y del agente |

## Puesta en marcha

### En local (Python 3.11+)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abre <http://127.0.0.1:8000>. La documentación de la API está en
<http://127.0.0.1:8000/docs>.

Para leer documentos escaneados instala también
[Tesseract](https://github.com/tesseract-ocr/tesseract) con el idioma español
(`spa`). Sin él, los PDF sin texto se marcan como «OCR necesario» para
completarlos a mano.

### Con Docker

```bash
cp .env.example .env   # opcional
docker compose up --build
```

La imagen incluye Tesseract y guarda base de datos y documentos en el
volumen `capafiscal-data`.

### Configuración

Copia `.env.example` a `.env` en la raíz del proyecto. Lo más útil:

- Lo más importante se configura en la propia aplicación, en **Mi empresa**:
  NIF (para distinguir emitidas y recibidas), forma jurídica (para saber qué
  modelos te tocan) y coste por hora (para el ahorro estimado).
- `COMPANY_NAME` y `COMPANY_TAX_IDS`: alternativa por configuración al NIF
  de Mi empresa (admite varios NIF separados por comas).
- `DATABASE_URL`: SQLite por defecto; admite PostgreSQL.
- `OUTLOOK_*` y `APP_ENCRYPTION_KEY`: conector de Outlook (ver comentarios en
  el archivo).

### Datos de prueba

```bash
cd backend
python scripts/gen_facturas.py facturas_prueba
```

Genera cinco facturas de ejemplo (una con importes descuadrados a propósito) y
un requerimiento de la AEAT que el agente reconoce como notificación. En
`backend/tests/fixtures/` hay más ejemplos en texto (factura emitida, factura
con retención, providencia de apremio).

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

Los tests usan una base de datos temporal y no tocan tus datos.

## Actualizar una instalación existente

Al arrancar, CapaFiscal añade automáticamente las columnas nuevas que falten
en una base de datos creada con una versión anterior (sin borrar datos).

Si el extractor ha mejorado, en **Facturas → Reprocesar pendientes** se
vuelven a leer todos los documentos que aún no están aprobados ni rechazados.
Las facturas aprobadas nunca se modifican.

## Estructura

```
backend/
  app/
    main.py               API principal (documentos, facturas, informes) y frontend
    business_routes.py    API de empresa, impuestos, notificaciones, banco,
                          cumplimiento, agenda y memoria del agente
    extractor.py          Extracción de datos de facturas
    invoice_service.py    Procesado, validación, aprobación, pagos, memoria
    reports_service.py    IVA, terceros, pagos, libros registro, búsqueda
    tax_service.py        Borradores 303/130/111/115/347 y calendario fiscal
    notification_service.py  Notificaciones administrativas y sus plazos
    bank_service.py       Extractos, conciliación, tesorería y salud del negocio
    compliance_service.py Certificado digital y checklist de cumplimiento
    agenda_service.py     Agenda unificada de vencimientos
    calendar_es.py        Días hábiles y festivos nacionales
    company_service.py    Datos de la empresa usuaria
    operations_service.py Panel, riesgos, agentes y asistente
    task_service.py       Bandeja de revisión
    outlook_connector.py  Conector de Outlook
    models.py, schemas.py Modelo de datos y esquemas de la API
    static/               Frontend (HTML, CSS y JS sin dependencias)
  scripts/gen_facturas.py Generador de facturas de prueba
  tests/                  Tests automáticos
```
