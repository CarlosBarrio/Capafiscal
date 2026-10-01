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
- **Entrada común con idempotencia y estados**: todo lo que llega (subida,
  correo, `POST /api/events`, plazos) se registra con su fuente y su
  identificador externo. La misma notificación o el mismo correo no abren dos
  expedientes. Cada entrada pasa por RECEIVED → PROCESSING → COMPLETED,
  NEEDS_HUMAN (un agente no pudo terminar: se muestra cuál y por qué) o FAILED
  (falló el recorrido entero), y se puede **reanudar** desde *Expedientes →
  Agentes → Entradas* o desde el propio expediente.
- **Conector de correo**: lee el buzón por IMAP, una carpeta local
  (`data/buzon/*.eml`) o un `.eml` importado a mano. Cada adjunto (PDF o
  texto) se lee como una subida y entra por la entrada común. Los agentes no
  saben de dónde viene un documento.
- **DEHú**: la conexión oficial (alta, certificado, apoderamiento) está
  pendiente. Cuando exista, solo tendrá que entregar cada notificación en
  `POST /api/events` con `source: "dehu"` y su identificador.

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

## El Director: qué hacer hoy

La pantalla *Hoy* abre con el Director:

- 🔴 **Requiere tu atención**: lo urgente o importante que espera a una
  persona, con el motivo («vence en 7 días (objetivo interno: en 5 días)»,
  «9,0 veces por encima de lo habitual») y lo que se ha bloqueado.
- 🟠 **Pendiente**: documentos pedidos sin recibir, plazos de los próximos 15
  días, expedientes para revisar sin prisa y escritos listos para presentar.
- **Trabajo realizado e intervención humana**: entradas, documentos,
  expedientes, anomalías y documentos solicitados. El % de intervención humana
  se calcula sobre las entradas: resueltas solas, con IA, enviadas a una
  persona o fallidas. Incluye el **tiempo ahorrado estimado**, con los
  supuestos a la vista y pendientes de ajustar en el piloto.
- 🟢 **Resuelto sin intervención** en los últimos 7 días: documentos que los
  agentes revisaron sin nada que objetar, expedientes preparados de principio
  a fin, documentos conseguidos por el Perseguidor, avisos cerrados solos y
  entradas repetidas ignoradas.

Debajo, «Estas son las 3 cosas que deberías revisar hoy».

## Trabajo continuo

CapaFiscal trabaja aunque nadie suba nada. Tres tipos de disparador acaban
en la misma entrada única y el mismo orquestador:

| Disparador | Qué lo provoca | Automatización |
|---|---|---|
| Externo | Facturas, notificaciones y correos nuevos | Buzón y orquestador (cada 5 min) |
| Temporal | Se acerca un 303/130/111/115; pasan 3 días hábiles sin documentación | Vigilante de plazos y Perseguidor (diarios) |
| Analítico | Anomalías, facturas que faltan, cambios de comportamiento | Detector (diario). Lo grave sobre una factura lo investiga el orquestador entero (Memoria, Fiscal, Gestor, Director) |

El Director muestra si la máquina está en marcha («CapaFiscal está
trabajando · último ciclo hace 3 min»). **Trabajar ahora** lanza un ciclo
completo de los tres disparadores.

## Demo: cero intervención

```bash
cd backend
python scripts/demo_cero_intervencion.py
```

Parte solo de un evento externo y muestra, paso a paso y con la hora, hasta
dónde llega CapaFiscal sin que nadie toque nada. Usa una base de datos
temporal. Recorre dos casos:

1. DEHú → requerimiento → expediente → documentos pedidos → escrito.
2. Correo → factura 6 veces por encima de lo habitual → investigación.

En los dos acaba en «Espera tu revisión». El Director resume después qué
queda para la persona.

## Prueba de resistencia

`tests/workflows/test_resistencia.py` lanza 100 eventos a la vez por la
entrada única:

- 20 facturas, 20 notificaciones, 15 embargos y 15 plazos;
- 10 duplicados, 10 documentos ambiguos y 10 en los que un agente falla.

Comprueba que:

- no se duplican expedientes ni códigos;
- no se pierden eventos;
- los repetidos se cuentan;
- los errores quedan aislados y se pueden reanudar;
- la traza es coherente;
- el Director refleja el estado real.

La prueba y la demo destaparon cuatro problemas, ya corregidos:

1. Un duplicado que llegaba a la vez que el original devolvía un error en vez
   de tratarse como repetido.
2. El contador de repetidos podía perder sumas: ahora es una actualización
   atómica.
3. Con escrituras concurrentes, SQLite perdía trabajo en silencio. Ahora
   SQLAlchemy emite el `BEGIN`, la base usa el modo WAL y espera ante
   bloqueos. La entrada común pide el turno de escritura al empezar
   (`BEGIN IMMEDIATE`) y reintenta si la base está ocupada, lo que es seguro
   gracias a la idempotencia.
4. Una notificación con fechas llegada por `/api/events` (como lo hará DEHú)
   fallaba.

SQLite sirve para una empresa. Para una gestoría con muchos usuarios a la
vez, PostgreSQL, y repetir esta prueba.

## Lectura de facturas reales y evaluación

El extractor lee facturas con reglas deterministas. Con facturas reales de
proveedores se encontraron patrones que no cubría, y ahora sí:

- totales en tabla (cabecera en una línea, valores debajo);
- dígitos separados por espacios («8 3 7,69»);
- un «21 420,00» que no son miles, sino el tipo de IVA y la cuota;
- texto girado en el margen;
- el emisor que solo aparece en el pie legal (Registro Mercantil, protección
  de datos);
- el número de factura dentro de una fila de cabecera;
- fechas con el mes en letra;
- páginas «2 de 2».

Los importes se resuelven con un solver que exige base × tipo de IVA = cuota y
base + cuota − retención = total.

**Claude como capacidad, no como agente.** Con `ANTHROPIC_API_KEY`, si las
reglas no bastan (faltan campos, los importes no cuadran, el emisor parece la
propia empresa…), Claude lee el PDF y propone los campos. Las reglas validan
cada valor antes de aceptarlo:

- aparece en el documento;
- el NIF tiene dígito de control válido;
- los importes cuadran;
- el sentido de la factura lo deciden los NIF de tu empresa.

Queda registrado qué dijo cada motor, por qué se eligió cada valor, los
tokens y el coste.

**Banco de evaluación** (`backend/evaluation/`):

```bash
cd backend
python -m evaluation                                   # réplicas sintéticas, reglas
python -m evaluation --dataset reales                  # tus facturas reales (carpeta local)
python -m evaluation --engines reglas,claude,hibrido   # comparar motores (necesita ANTHROPIC_API_KEY)
python -m evaluation --engines claude --model claude-sonnet-5-5
python -m evaluation preparar reales --conjunto B      # etiquetar documentos nuevos
```

Los documentos se separan en conjuntos A (desarrollo), B (evaluación) y C
(ciego, que solo se evalúa con `--ciego` y queda anotado). El protocolo
completo está en `backend/evaluation/README.md`.

Mide el acierto por campo, las facturas perfectas, el tiempo, los tokens, el
coste y cuántas veces se volvió a las reglas, y deja un informe en
`evaluation/informes/`. Las facturas reales van en
`evaluation/datasets/reales/` (PDF + `labels.json`). Esa carpeta está fuera de
git: el repositorio es público y las facturas llevan IBAN y datos personales.
Las réplicas de `evaluation/datasets/sinteticas/` reproducen las mismas
maquetas con datos inventados y sí son parte de los tests.

## Proactividad: trabajo que nadie ha pedido

CapaFiscal no solo procesa lo que le llega; en cada barrido busca problemas:

- **Documentos que faltan**: un proveedor que factura cada mes y este mes no lo ha hecho.
- **Movimientos sin justificar**: pagos y cobros del banco sin factura.
- **Obligaciones incompletas**: «Faltan 2 facturas para cerrar el 303» cuando el plazo
  vence en menos de 25 días. El aviso se actualiza mientras se completa y se cierra solo.

**Conciliación banco ↔ facturas** (`GET /api/bank/reconciliation`): cada movimiento lleva un
nivel de confianza y la evidencia que lo sostiene:

| Nivel | Cuándo | Qué hace CapaFiscal |
|---|---|---|
| SEGURO | importe exacto + una prueba de identidad (nº de factura, NIF, IBAN, o nombre con fecha coherente) + una única factura posible | concilia solo |
| PROBABLE | encaja una factura, pero solo por importe o con una diferencia dentro de la tolerancia (1 € o 0,5 %) | lo propone; decide una persona |
| CONFLICTO | dos o más facturas igual de plausibles, importe que no cuadra con la factura identificada, o pago duplicado | nunca concilia solo |
| SIN MATCH | ninguna factura lo justifica | lo señala |

Cada decisión lista las comprobaciones (✓ importe exacto, ✓ nº de factura, ✗ NIF…) y los
candidatos considerados. Regla de producto: **CapaFiscal nunca se inventa seguridad**;
«Confirmar coincidencias seguras» solo confirma lo SEGURO. Las facturas vencidas sin
movimiento quedan como «factura sin pago» y las excepciones las investiga el Detector.

**Impuestos continuos** (`GET /api/taxes/position`): cómo va hoy cada modelo
(303/130/111/115): «303 estimado: 2.340 € a ingresar · 97 % de información disponible ·
faltan 2 facturas · 1 discrepancia con el banco».

**Memoria financiera** (`GET /api/memory/profiles`): perfil de cada proveedor y cliente
(rango de importe, frecuencia, IVA, forma y plazo de pago, última anomalía y última
decisión humana). El Detector explica cada aviso contra ese perfil y detecta cambios:
gasto anormal (y quién lo explica), caída de facturación y clientes que dejan de pagar.

**Hoy** (`GET /api/today`): lo que requiere atención ordenado por impacto (urgencia +
dinero en juego + tipo de asunto), con el porqué en una línea, más el trimestre y el banco.

**Aprendizaje** (`GET /api/learning`): cada corrección, aprobación, rechazo o descarte
queda registrado (predicción, decisión, tipo de error, motor). Sirve para medir la
precisión real, para que lo que las personas corrigen a menudo en un proveedor deje de
darse por bueno solo con reglas, y como etiquetas reales para evaluar.

**Routing de Claude** (`GET /api/agents/routing`): Claude solo entra en las dudas donde
los datos dicen que mejora (`python -m evaluation politica --informe …`).

**DEHú**: `POST /api/connectors/dehu/import` (metadatos + PDF) o una carpeta con lo
descargado (`DEHU_INBOX_DIR`, `POST /api/connectors/dehu/poll`). Las fechas de la DEHú
son las oficiales: el plazo deja de ser estimado. La conexión directa necesita alta,
certificado y apoderamientos; entrará como otro transporte, sin tocar el resto.

## Multiempresa (gestoría → clientes) y permisos

Con `AUTH_REQUIRED=true`:

1. `POST /api/auth/setup` crea la gestoría, el primer cliente y el administrador (y, si
   vienes de una instalación de una sola empresa, le pasa sus datos).
2. El administrador crea clientes (`/api/admin/clients`) y usuarios (`/api/admin/users`)
   con su rol y los clientes a los que accede.
3. En la web aparece el acceso y, si hay varios clientes, el selector.

| Rol | Puede |
|---|---|
| Admin | Todo, en todos los clientes; gestiona clientes y usuarios |
| Gestor | Todo en sus clientes, salvo administrar usuarios |
| Revisor | Ver y decidir (aprobar, rechazar, corregir, resolver); no configura ni borra |
| Cliente | Ver su empresa y aportar documentos |
| Solo lectura | Ver |

**Aislamiento**: todas las tablas llevan `tenant_id` y la sesión de base de datos filtra
y marca cada consulta e inserción por el cliente activo (`app/tenancy.py`). Ninguna
consulta puede olvidarse del filtro, y sin cliente elegido no se ve nada (falla cerrado).
La API admite `Authorization: Bearer` + `X-Client-Id`; el navegador usa una cookie
HttpOnly y las escrituras exigen la cabecera `X-CapaFiscal` (protección CSRF). Sin
`AUTH_REQUIRED`, todo funciona como siempre, para una sola empresa.

Nota para bases de datos ya creadas: las columnas nuevas se añaden solas, pero las
restricciones de unicidad por cliente solo se crean en bases nuevas; para pasar una
instalación existente a multiempresa con varios clientes, conviene empezar con una base
nueva (o migrar con Alembic).

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
`tests/workflows/test_email_inbox.py` es el hito de punta a punta: una
factura llega por correo, se lee, el Detector ve que es 6 veces lo habitual,
se abre el expediente de «factura sospechosa» y una persona lo aprueba.

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
    extraction_rules.py   Reglas aprendidas de facturas reales (2.ª pasada del extractor)
    interpretation.py     Híbrido: Claude propone, las reglas validan cada valor
    connectors/           Fuentes externas → entrada común (correo; DEHú pendiente)
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
