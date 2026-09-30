# CapaFiscal

Gestión de facturas recibidas para pymes: carga documentos (a mano, arrastrando
o desde Outlook), extrae los datos fiscales, detecta descuadres y duplicados,
organiza la revisión humana y prepara la información para el trimestre
(IVA soportado, retenciones y libro registro de facturas recibidas).

> Los datos extraídos automáticamente deben revisarse antes de usarse
> fiscalmente. CapaFiscal ayuda a preparar los modelos, no los presenta.

## Qué hace

| Área | Funcionalidad |
|---|---|
| **Entrada** | Subida de varios PDF/TXT a la vez o arrastrándolos a la ventana · importación de adjuntos de Outlook (Microsoft Graph) · deduplicación por huella SHA-256 |
| **Extracción** | Emisor y cliente (NIF/CIF con validación del dígito de control), número, fechas de factura y vencimiento, base, IVA por tipo, retención IRPF, recargo, total, concepto y categoría de gasto con su cuenta del PGC · OCR con Tesseract para escaneados |
| **Control** | Cuadre de importes, campos obligatorios, duplicados fuertes (NIF + número) y probables (NIF + fecha + importe), confianza por campo |
| **Revisión** | Bandeja de tareas priorizada · detalle con vista previa del documento, corrección de campos, comprobación de cuadre en vivo e historial · aprobar, aprobar con advertencias, rechazar y reabrir |
| **Pagos** | Registro de pago (fecha y forma), facturas vencidas y próximas a vencer |
| **Informes** | IVA soportado por trimestre y por tipo, gasto por categoría y mes, retenciones (modelo 111) · exportación del **libro registro de facturas recibidas** en Excel o CSV |
| **Proveedores** | Gasto, IVA, pendiente de pago y última factura por NIF |
| **Asistente** | Preguntas en lenguaje natural sobre IVA de un trimestre, pagos pendientes, un proveedor, riesgos, tareas o situación general (reglas sobre datos internos) |
| **Trazabilidad** | Auditoría de todas las acciones (cargas, correcciones, decisiones, exportaciones) |

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

- `COMPANY_NAME` y `COMPANY_TAX_IDS`: tu empresa. Con ellos el extractor
  identifica siempre a tu empresa como cliente y no la confunde con el emisor.
- `DATABASE_URL`: SQLite por defecto; admite PostgreSQL.
- `OUTLOOK_*` y `APP_ENCRYPTION_KEY`: conector de Outlook (ver comentarios en
  el archivo).

### Datos de prueba

```bash
cd backend
python scripts/gen_facturas.py facturas_prueba
```

Genera cinco facturas de ejemplo (una con importes descuadrados a propósito) y
un requerimiento de la AEAT que no es factura.

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
    main.py               API (FastAPI) y servidor del frontend
    extractor.py          Extracción de datos de facturas
    invoice_service.py    Procesado, validación, aprobación, pagos
    reports_service.py    IVA, proveedores, pagos, libro registro, búsqueda
    operations_service.py Panel, riesgos, agentes y asistente
    task_service.py       Bandeja de revisión
    outlook_connector.py  Conector de Outlook
    models.py, schemas.py Modelo de datos y esquemas de la API
    static/               Frontend (HTML, CSS y JS sin dependencias)
  scripts/gen_facturas.py Generador de facturas de prueba
  tests/                  Tests automáticos
```
