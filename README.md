# CapaFiscal

Administrativo digital para pymes, autónomos y gestorías. Recoge la
documentación (facturas, extractos bancarios, notificaciones), la entiende,
detecta riesgos y plazos, prepara los impuestos y solo pide intervención
humana cuando hace falta criterio.

> Los datos extraídos y los borradores de impuestos son orientativos y deben
> revisarse antes de usarse fiscalmente. CapaFiscal ayuda a preparar, no
> presenta nada ante la Administración.

## Cómo trabaja: un equipo de agentes

Ha ocurrido algo → se abre (o no) un expediente → el expediente decide qué
agentes necesita. Todo acaba en una recomendación con evidencia que la
persona aprueba, cambia o rechaza:

```
FUENTES (subida, correo, DEHú*, facturas, plazos)
   → Vigilante → Expedientes → ORQUESTADOR: ¿qué ruta?
      → Fiscal · Memoria · Detector · Gestor · Perseguidor (según la ruta y los resultados)
         → Director → recomendación + evidencia → TÚ: aprobar / modificar / rechazar
            → acción y memoria (la próxima vez se tiene en cuenta)
```

| Ruta | Agentes |
|---|---|
| Requerimiento o acto administrativo | Vigilante → Expedientes → Fiscal → Memoria → Gestor → Perseguidor → Director |
| Embargo | Vigilante → Expedientes → Memoria → Gestor → Director |
| Documento normal (factura) | Vigilante → Expedientes → Detector → Director |
| Factura sospechosa | Vigilante → Expedientes → Memoria → Detector → Fiscal → Gestor → Director |
| Plazo de presentación (303, 130, 111, 115) | Vigilante → Expedientes → Fiscal → Memoria → Detector → Gestor → Director |

- **Encadenado por resultados**: si el Detector encuentra una anomalía, el
  orquestador añade Memoria, Fiscal y Gestor (y una factura «normal» pasa a
  «sospechosa»); si el Gestor ve documentos que no puede preparar, añade al
  Perseguidor. La traza guarda la ruta y por qué se añadió cada agente.
- **Contratos**: cada agente declara qué consume, qué produce y qué eventos
  atiende (`GET /api/agents/routes`), para reutilizarlo en procesos nuevos.
- **Hallazgos con evidencia** (`Finding`): qué se detectó, por qué, con qué
  datos, riesgo, confianza, documento de origen, fecha, agente y siguiente paso.
- **Entrada común** `POST /api/events` (notificación, factura o plazo): así se
  enganchan nuevas fuentes. *La conexión oficial con DEHú (alta, certificado,
  apoderamiento) está pendiente; cuando exista, entregará aquí.*

Ejemplo real: llega una diligencia de embargo de créditos contra un proveedor.
El agente identifica al proveedor por su NIF, encuentra que le debes una
factura de 318 €, te avisa de que **no se la pagues** (responsabilidad
solidaria, art. 42.2 LGT), prepara la relación de créditos y el escrito de
contestación, y lo pone el primero de tu lista.

- **Traza completa**: cada recorrido guarda qué hizo cada agente, en cuánto
  tiempo, con qué evidencia y con qué motor (reglas o IA).
- **Detector de anomalías**: motor reutilizable (mediana/MAD, sin cajas
  negras) con importe atípico, duplicado, IVA atípico, pago o cobro sin
  factura, factura sin pago, proveedor nuevo, cambio de comportamiento,
  factura que falta y patrón interrumpido. Se usa sobre una factura al
  llegar, en el barrido diario y dentro de cualquier ruta. Aprende de las
  personas: si ya diste por correcto algo parecido del mismo proveedor, el
  aviso baja de riesgo y te lo recuerda.
- **Perseguidor**: pide la documentación con un enlace personal de subida
  (sin usuario), recuerda con cortesía creciente, verifica lo recibido y deja
  de insistir.
- **Memoria**: pregunta en lenguaje natural («¿qué nos pidió Hacienda sobre el
  IVA?») y responde con las fuentes.
- **IA opcional**: con `ANTHROPIC_API_KEY`, Claude lee y redacta; sin clave,
  todo funciona con reglas y plantillas.

## Qué hace

| Área | Funcionalidad |
|---|---|
| **Expedientes** | La bandeja de trabajo de los agentes: resumen del Director («hoy deberías revisar…»), cada expediente con qué piden, documentación preparada/pedida/recibida, antecedentes, impacto fiscal, borrador editable del escrito, traza de agentes e historial · aprobar → paquete ZIP para presentar en sede → registrar la presentación → resolver |
| **Hoy** | Saludo con el trabajo del agente en las últimas 24 h, IVA estimado del trimestre, resultado, pendiente de pago, siguiente paso recomendado y **agenda unificada** (impuestos, notificaciones, cobros, pagos y caducidades) |
| **Facturas** | Recibidas y **emitidas** (se distinguen con el NIF de tu empresa) · subida múltiple o arrastrando · extracción de NIF/CIF, número, fechas, base, IVA por tipo, IRPF, recargo, total y categoría con cuenta del PGC · control de cuadre, duplicados y confianza por campo · aprobar, rechazar, reabrir, registrar pago o cobro |
| **Memoria del agente** | Cuando corriges la categoría de un proveedor o cliente, la recuerda y la aplica a sus siguientes facturas |
| **Negocio** | Ingresos, gastos, resultado y margen · gráfico de 12 meses · periodo medio de cobro y pago · dependencia de clientes · cobros pendientes y vencidos |
| **Tesorería y banco** | Importación de extractos CSV/Excel de cualquier banco (detecta columnas y formatos) · **conciliación automática** con facturas (propuestas que tú confirmas) · previsión de caja a 90 días con cobros, pagos e impuestos estimados |
| **Impuestos** | Calendario fiscal según seas autónomo o sociedad (303, 130, 111, 115, 390, 190, 180, 347, 202, 200, 100) con festivos nacionales · **borradores continuos** de los modelos 303, 130, 111 y 115 por casillas · modelo 347 por tercero y trimestre · registro de presentaciones |
| **Notificaciones** | Detección automática de requerimientos, liquidaciones, apremios, embargos y sanciones de AEAT, Seguridad Social, DGT, ayuntamientos… · referencia e importe · **plazo calculado** con su regla (días hábiles, art. 62 LGT, art. 43.2 Ley 39/2015) · tarea en la bandeja · registro manual |
| **Cumplimiento** | Caducidad del certificado digital (lee .cer/.pem/.p12 sin guardar el archivo ni la contraseña) · apoderamientos, DEHú, Verifactu, factura electrónica y RGPD · controles automáticos (libros al día, modelos y notificaciones en plazo) |
| **Equipo** | Fichas de personas (datos, puesto, contrato, IBAN, habilidades) · **crear desde un CV** en PDF · documentos por persona (contrato, resguardo de alta, 145, nóminas…) · **checklist de incorporación y baja** con plazos legales (alta en SS antes del primer día, Contrat@ en 10 días hábiles, baja en 3 días) que se marca solo al subir el resguardo · **organigrama** automático · proyectos con asignación de personas y dedicación · calendario de ausencias con saldo de vacaciones · avisos de contratos temporales que vencen |
| **Nóminas** | **Paso de nóminas automático**: borrador mensual de toda la plantilla (12 o 14 pagas, días trabajados, horas extra, incentivos y anticipos) con cotizaciones 2026 (incluye MEI) y retención de IRPF estimada · aprobar → **recibos en PDF**, **remesa SEPA** (pain.001) para subir al banco, resumen en Excel y **asiento contable** (640/642/4751/476/465) · alimenta el modelo 111, la tesorería (neto a fin de mes y seguros sociales el mes siguiente) y la salud del negocio · simulador de coste de contratación |
| **Terceros** | Proveedores y clientes por NIF: importes, IVA, pendiente y última factura |
| **Informes** | IVA soportado y repercutido por trimestre, gasto por categoría y mes · **libros registro de facturas recibidas y expedidas** en Excel o CSV |
| **Ventas y cobros** | **Emisión de facturas** con numeración correlativa por serie, **registro de facturación con huella SHA-256 encadenada** y URL del QR tributario (RD 1007/2023, preparado para VERI\*FACTU), PDF, rectificativas y duplicado · al emitir entran solas en el libro de emitidas (303, 347, cobros y tesorería) · **facturas recurrentes** (cuotas, igualas, alquileres) que el agente genera, emite y deja listas para enviar · clientes con plazo de pago y retención |
| **Reclamación de impagos** | Cada mañana el agente revisa las facturas vencidas y redacta el mensaje que toca: recordatorio amable, segundo aviso a los 15 días y **requerimiento formal con carta PDF** a partir de 30, con **intereses de demora** por semestre y 40 € de costes de cobro entre empresas (Ley 3/2004) · retraso medio histórico por cliente |
| **Bandeja de salida** | Todo lo que redacta el agente (facturas, reclamaciones, recibos de nómina, resumen diario, cierre para la gestoría) espera tu visto bueno · envío por **SMTP** o descarga como **borrador .eml** que Outlook abre listo para enviar, con los adjuntos |
| **Registro de jornada** | Obligatorio (art. 34.9 ET): fichar entrada/salida con un clic, registros manuales y correcciones **con motivo auditado**, resumen mensual frente a la jornada prevista, **alertas** de olvidos, jornadas de más de 9 h, descanso inferior a 12 h y días sin registro, **PDF mensual para firmar** y Excel |
| **Automatizaciones** | El agente trabaja solo con horario: conciliación bancaria, facturas recurrentes, resumen diario, reclamaciones, vigilancia de jornada, **borrador de nómina el día 25** y **cierre trimestral para la gestoría** · activar/desactivar, ejecutar a mano, historial y **horas y coste ahorrados** del mes |
| **Cierre para la gestoría** | Un ZIP con libros registro, borradores de modelos, facturas, nóminas y movimientos bancarios del trimestre, con un LEEME de incidencias, y el email a tu asesor ya redactado |
| **Asistente** | Preguntas sobre IVA y 303 de un trimestre, modelos a presentar, salud del negocio, notificaciones, pagos, un proveedor, riesgos o tareas |
| **Conectores** | Outlook (Microsoft Graph) para importar adjuntos · carga manual · extractos bancarios |
| **Navegación** | Menú lateral agrupado (Operación, Finanzas, Personas, Empresa) · buscador y acciones rápidas con **Ctrl + K** (secciones, facturas, personas) · diseño adaptado a móvil |
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

Los tests usan una base de datos temporal y no tocan tus datos. Están en
`tests/unit`, `tests/integration`, `tests/agents` y `tests/workflows`. Esta
última es la batería de evaluación de agentes: cada caso de
`tests/workflows/scenarios/*.json` describe entrada → agentes ejecutados →
resultado → evidencias → acción esperada; para añadir un caso basta un JSON.

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
    team_routes.py        API de equipo, proyectos, ausencias y nóminas
    team_service.py       Fichas, incorporación, organigrama, ausencias, CV
    payroll_service.py    Cálculo de nóminas, recibos PDF, SEPA, Excel, asiento
    ops_routes.py         API de ventas, cobros, bandeja de salida, jornada,
                          automatizaciones y cierre para la gestoría
    sales_service.py      Clientes, emisión, registro encadenado, recurrentes
    dunning_service.py    Cobros vencidos, intereses de demora, reclamaciones
    outbox_service.py     Bandeja de salida: SMTP y borradores .eml
    timesheet_service.py  Registro de jornada, alertas, PDF y Excel
    automation_service.py Automatizaciones programadas y resumen diario
    advisor_service.py    Paquete trimestral para la gestoría
    agents/               Equipo de agentes: vigilante, expedientes, fiscal,
                          memoria, gestor, perseguidor, director, detector,
                          orquestador, conocimiento y capa de IA opcional
    case_service.py       Expedientes: acciones, adjuntos, escrito PDF, paquete
    agent_routes.py       API de expedientes, agentes, memoria y portal
    operations_service.py Panel, riesgos, agentes y asistente
    task_service.py       Bandeja de revisión
    outlook_connector.py  Conector de Outlook
    models.py, schemas.py Modelo de datos y esquemas de la API
    static/               Frontend (HTML, CSS y JS sin dependencias)
  scripts/gen_facturas.py Generador de facturas de prueba
  tests/                  Tests automáticos
```
