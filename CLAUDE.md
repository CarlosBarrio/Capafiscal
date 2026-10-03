# CapaFiscal · instrucciones para agentes

## Producto: «CapaFiscal trabaja por mí»

Solo se añade lo que ahorra horas, evita errores, anticipa problemas o quita decisiones repetitivas.
Los datos se introducen una vez y recorren el circuito (factura → contabilidad → conciliación → IVA →
tesorería → detector). Nada de agentes nuevos, chatbots, CRM, cuadros de mando o automatizaciones de
escaparate. El trabajo se presenta en una sola lista (Centro de trabajo) con lo comprobado y su acción.

## Frontend: mejorar, no rediseñar

CapaFiscal ya tiene un sistema visual (`backend/app/static/style.css`: tokens en `:root`,
componentes `.card`, `.segmented`, `.status-pill`, `.act-btn`/`.btn-ghost`…). Las skills de
diseño sirven para elevarlo poco a poco, no para sustituirlo.

```
interfaz existente → detectar un problema concreto → aplicar la skill → mínima mejora necesaria → comprobar regresiones
```

- Skills de diseño del proyecto en `.claude/skills/`. Léelas antes de tocar su ámbito:
  `frontend-design` (dirección y lenguaje), `emil-design-eng` (revisión general), `mobile-native`
  (responsive), `review-animations` / `improve-animations` (motion), `prototype` (pantallas nuevas).
- Regla de poda: «¿Esto ayuda a decidir o a terminar trabajo? Si no, se va». Sin etiquetas en
  mayúsculas sobre el contenido, sin ceros que no informan, sin degradados ni desenfoques.
- No rediseñar pantallas completas ni cambiar componentes que funcionan por preferencia estética.
- No cambiar APIs, backend, agentes ni modelos de datos en un trabajo de frontend salvo que se pida.
- Revisar cómo está implementado algo antes de modificarlo; reutilizar tokens y componentes.
- Prioridad: jerarquía, espaciado, estados (vacío, carga, error), interacción, accesibilidad,
  responsive y claridad.
- Sin animaciones por defecto: motion solo si comunica algo. Transform/opacity, ease-out,
  `prefers-reduced-motion`.
- Nada decorativo para «llamar la atención»: ni colores, gradientes, sombras, bordes ni badges
  nuevos. El color es para estados.
- Si algo ya está bien resuelto, se deja como está.
- Por cada cambio importante: qué problema, qué principio, qué se cambia y qué NO se cambia.
- Al terminar: tests relevantes y comprobación en escritorio (1280) y móvil (390).

## Datos y seguridad

- El repositorio es público. Nunca subir facturas, IBAN, DNI ni NIF reales. Los documentos
  reales viven en `backend/evaluation/datasets/reales/` (ignorado por git).
- Datos sintéticos: marcados «SIMULACIÓN — NO OFICIAL», NIF inventados (prefijo B00) e IBAN ficticios.
- El banco C es ciego: no mirarlo ni ajustar reglas a partir de él.
- No escribir identificadores de modelos de IA en commits ni en código.
- Una petición GET no escribe en la base de datos (en SQLite, dos lecturas que escriben a la
  vez acaban en «database is locked»).

## Mapa del proyecto

FastAPI + SQLAlchemy 2 (SQLite o PostgreSQL) y frontend en JS/CSS sin framework. Todo bajo `backend/`.

- **Entrada de documentos:** `app/main.py` (rutas principales y subida) → `invoice_service.py`
  (`process_document` por fases; el LLM va fuera de la transacción) → `extractor.py` (lectura por
  reglas, OCR) + `extraction_rules.py` (reglas aprendidas de facturas reales) → `interpretation.py`
  (`needs_help`: cuándo avisar a una persona o pedir a Claude) + `routing.py` (si merece la pena llamarlo).
- **Circuito:** `accounting.py` (asientos) · `reconciliation.py` (banco ↔ facturas) · `tax_service.py`
  y `fiscal_position.py` (303/130/111/115) · `treasury.py` · `closing.py` (cierre del mes) ·
  `workcenter.py` (Centro de trabajo: la lista única).
- **Agentes:** `app/agents/` (`orchestrator.py`, `detector.py`, `director.py`, `perseguidor.py`,
  `llm.py` = única puerta a Claude, con coste y metadatos). Expedientes: `case_service.py`.
- **Fuentes:** `app/connectors/` (correo, DEHú; `assignment.py` reparte en multiempresa),
  `mail_*.py`, `outlook_connector.py`, `bank_*.py`. Inteligencia (BOE): `app/intelligence/`.
- **Transversal:** `models.py`, `schemas.py`, `tenancy.py` (aislamiento por cliente), `deps.py`,
  `request_context.py` (actor de auditoría), `config.py`, `production.py`, `migrations/` (Alembic).
- **Frontend:** `app/static/` — `index.html`, `style.css` (tokens), `shell.js` (navegación),
  `app.js` (facturas, Hoy) y un JS por pantalla (`taxes.js`, `cases.js`, `close.js`…).
  `portal.html` es el portal de subida para proveedores. Lenguaje visual: `DESIGN.md`; producto: `PRODUCT.md`.
- **Evaluación:** `evaluation/` (`core.py` reglas/Claude/híbrido por campo, `casos.py` banco B,
  `README.md` con comandos). Datos: `evaluation/datasets/` (`corpus/` sintético, `b_sintetico/`,
  `catalogo/`, `reales/` fuera de git). Informes en `evaluation/informes/` (no se suben).
- **Tests:** `tests/unit` (reglas del extractor, evaluador), `tests/integration` (API, multiempresa,
  LLM), `tests/workflows` (correo, cierre…), `tests/agents`, `tests/golden` (regresiones).
- **No leer salvo que se pida** (solo gastan contexto): `.claude/skills/*/reference/` y los PDF de
  `evaluation/datasets/`.
- Docs: `docs/` (roadmap, banco real, correo multiempresa, seguridad y copias).
- OCR: Tesseract (`tesseract-ocr-spa`) lo trae la imagen Docker; sin él, los escaneados no se leen
  y el emisor que solo está en imágenes queda vacío y a revisión.

## Comprobaciones

```bash
cd backend
python -m pytest                       # SQLite
TEST_DATABASE_URL=postgresql+psycopg://usuario@host:5432/vacia python -m pytest   # PostgreSQL
python -m evaluation casos             # banco B
```

Cambios de esquema: modelos + `alembic revision --autogenerate -m "…"` (revisar el archivo generado).
