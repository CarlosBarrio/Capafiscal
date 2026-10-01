# Banco de evaluación de la lectura de facturas

Responde a una pregunta: **¿qué aporta Claude frente a las reglas?**

```bash
cd backend
python -m evaluation --dataset reales --engines reglas,claude,hibrido
```

| Motor | Qué hace |
|---|---|
| `reglas` | El extractor determinista (sin IA, sin coste) |
| `claude` | Claude solo: lee el PDF y devuelve los campos |
| `hibrido` | Reglas primero. Si no bastan, Claude propone y las reglas validan cada valor |

Para cada motor mide el acierto por campo, las facturas perfectas, el tiempo
medio, los tokens, el coste estimado y cuántas veces se volvió a las reglas.
El informe (Markdown y JSON) queda en `evaluation/informes/`, que está fuera
de git.

## Protocolo: desarrollo, evaluación y ciego

| Conjunto | Para qué | Regla |
|---|---|---|
| **A · desarrollo** | Mirar, depurar y ajustar reglas | Sus cifras no miden generalización |
| **B · evaluación** | Medir mientras se desarrolla | Se miran los fallos, pero **no** se cambian reglas a partir de documentos concretos |
| **C · ciego** | La medida final | No se abre ni se mira. Solo se evalúa con `--ciego`, y cada uso queda anotado en `informes/ciego.log` |

Por defecto se evalúan A y B. Las 5 facturas reales actuales son conjunto A,
porque se usaron para ajustar las reglas.

Qué conviene reunir (con variedad de proveedores y maquetas):

- facturas de distintos sectores y de varias páginas;
- facturas rectificativas;
- facturas con retención de IRPF;
- tickets y documentos ambiguos;
- requerimientos;
- embargos.

Reparto orientativo: un 20 % a A, un 40 % a B y un 40 % a C. El reparto se
decide **antes** de mirar los documentos.

## Añadir documentos

```bash
# 1. Copia los PDF nuevos en evaluation/datasets/reales/
# 2. Genera el borrador de etiquetas (vacío por defecto, para no sesgar):
python -m evaluation preparar reales --conjunto B
# 3. Rellena cada «expected» mirando el documento y pon "revisado": true
#    (en evaluation/datasets/reales/labels.borrador.json)
# 4. Pásalos a labels.json:
python -m evaluation incorporar reales
```

`--prerrellenar` rellena el borrador con lo que leen las reglas. Ahorra
tiempo, pero si no revisas cada valor estarás midiendo las reglas contra sí
mismas.

Formato de `labels.json`:

```json
{
  "empresa": {"name": "Tu empresa S.L.", "tax_id": "B00000000"},
  "casos": [
    {"id": "proveedor_x", "file": "proveedor_x.pdf", "set": "B", "tags": ["tabla de totales"],
     "expected": {"direction": "RECEIVED", "supplier_name": "...", "supplier_tax_id": "...",
                  "customer_tax_id": "...", "invoice_number": "...", "invoice_date": "AAAA-MM-DD",
                  "due_date": "AAAA-MM-DD", "subtotal": "100.00", "tax_total": "21.00", "total": "121.00",
                  "category": "Servicios profesionales"}}
  ]
}
```

Solo se comparan los campos que pongas en `expected`.

- `datasets/sinteticas/`: réplicas anonimizadas de maquetas reales, generadas
  con `generar.py`. Forman parte de los tests, y fallar aquí es una regresión.
- `datasets/reales/`: tus documentos de verdad. Esta carpeta está **fuera de
  git**, porque el repositorio es público y las facturas llevan IBAN y datos
  personales.

## Ejecutar la comparación con Claude

```bash
# en .env: ANTHROPIC_API_KEY=...
python -m evaluation --dataset reales --engines reglas,claude,hibrido
python -m evaluation --dataset reales --engines claude --model claude-sonnet-5-5   # comparar otro modelo
```

El informe incluye:

- el acierto por campo, incluida la clasificación;
- los documentos perfectos;
- el tiempo medio y el p95;
- el **% de documentos en los que entra Claude**;
- el coste por documento y por cada 1.000 documentos;
- los tokens;
- la tasa de fallback;
- por qué entró Claude en cada documento del híbrido.

Sin clave, la columna de Claude aparece como «no ejecutado».

## Qué mide además del acierto

- **Qué pasa con cada documento**:
  - resuelto solo, con reglas;
  - resuelto con IA;
  - enviado a una persona, porque el sistema detecta que no está seguro;
  - **error silencioso**: no avisa y está mal. Es la cifra que más importa
    vigilar.
- **Matriz de errores**: `error_sentido`, `error_proveedor`, `error_nif`,
  `error_numero`, `error_fecha`, `error_importes`, `error_iva` y
  `error_clasificacion`. Incluye la confusión de la clasificación
  («esperado → leído»), para ver si un fallo es aislado o sistemático.
- **Por conjunto** (A, B y C, lado a lado) y **por característica** del
  documento (maqueta, sector…).
- **¿Cuándo merece la pena llamar a Claude?**: en cuántos documentos entró,
  cuántos campos corrigió, cuántos empeoró, el coste por campo corregido y
  cuántos documentos resolvieron las reglas solas a coste 0.
- **Errores conocidos**: los casos que se dejan sin corregir a propósito
  (`"errores_conocidos"` en la etiqueta) aparecen en cada informe como
  «sigue fallando» o «corregido». Ahora mismo, la factura de la tienda está
  clasificada como «Servicios profesionales» cuando es «Software e
  informática». Es un error silencioso en el conjunto A y se usa para ver en
  B si el fallo es aislado o sistemático.
- **Historial**: cada ejecución añade una línea a `informes/historial.csv`
  (fecha, commit, conjuntos, motor, acierto, resultados y errores conocidos
  pendientes), para ver cómo evoluciona el sistema.

## Banco de expedientes sintéticos (B y C)

Además de facturas sueltas, CapaFiscal se evalúa con **expedientes**:
grupos de documentos relacionados (requerimiento + facturas + 303
presentado, embargo + facturas pendientes, correo con original y
rectificativa…) que entran por las mismas puertas que en producción
(subida, correo, eventos de plazo, extracto bancario).

Los documentos son **sintéticos pero realistas**: siguen la estructura de
los procedimientos oficiales (cabecera del organismo, destinatario,
expediente, órgano, asunto, fundamento, documentación, plazo,
advertencias, diligencia de notificación, pie con código de verificación y
paginación), con datos inventados y la marca `SIMULACIÓN — NO OFICIAL`.
No se usan NIF, IBAN ni datos personales reales: los NIF de empresa llevan
el prefijo provincial 00, que no existe. Variantes de ruido: texto girado,
2-3 páginas, tabla partida entre páginas, dígitos separados, errores de
OCR y escaneo sin texto seleccionable.

```bash
python -m evaluation generar-b                       # regenera evaluation/datasets/b_sintetico (versionado)
python -m evaluation casos --dataset b_sintetico     # evalúa (reglas); informe en informes/
python -m evaluation casos --motor hibrido           # con Claude si hay ANTHROPIC_API_KEY

python -m evaluation generar-c --semilla N           # C ciego: lo genera quien evalúa, con su semilla
python -m evaluation casos --dataset c_ciego --ciego # una sola vez, al final (queda en ciego.log)
```

Cada carpeta de caso tiene sus documentos y un `caso.json` con la verdad:

```json
{
  "case_id": "B05",
  "documents": [{"archivo": "embargo_creditos.pdf", "tipo": "embargo"}],
  "contexto": {"empresa": {...}, "facturas": [{"invoice_number": "SFD-2026-071", "total": "2300.00", "paid_at": null}, ...]},
  "entradas": [{"tipo": "subida", "archivo": "embargo_creditos.pdf", "expected": {
    "route": "embargo", "procedure": "EMBARGO_CREDITOS",
    "affected": {"type": "supplier", "tax_id": "B00200022"},
    "debt_amount": 2850.00, "credit_amount": 4100.00, "deadline": "2026-10-06",
    "requires_human": true, "expected_findings": ["credit_exceeds_debt"]}}]
}
```

La verdad se escribe a partir del procedimiento (lo que haría un gestor),
no de lo que hace hoy el sistema, y se versiona **antes** de ejecutar.
Lo que un caso no dice no se comprueba. Las comprobaciones disponibles y los
tres hallazgos semánticos (`credit_exceeds_debt`, `successive_payments`,
`no_pending_payment`) están descritos en `evaluation/casos.py`.

| Bloque | Casos |
|---|---|
| AEAT | B01 comprobación limitada · B02 requerimiento que dice «factura» 18 veces · B03 propuesta de liquidación · B04 sin fecha de notificación |
| Embargos | B05 crédito > deuda · B06 pagos sucesivos · B07 sin pago pendiente · B08 salario |
| Seguridad Social | B09 requerimiento (CCC) · B10 reclamación de deuda · B11 comunicación laboral sin impacto fiscal |
| Normales | B12 certificado · B13 justificante 303 · B14 NIF desconocido |
| Memoria | B15 requerimiento + facturas + 303 · B16 falta la factura 003 · B17 factura duplicada |
| Facturas | B18 rectificativa · B19 retención · B20 tres páginas |
| Correo | B21 factura · B22 reenviado · B23 «RE:» con original y rectificativa |
| Adversariales | ADV01 embargo que enumera facturas · ADV02 «autoliquidación» · ADV03 escaneado · ADV04 texto girado · ADV05 dos páginas · ADV06 NIF ilegible · ADV07 fecha 4/5/26 · ADV08 solo pie legal · ADV09 dígitos separados · ADV10 OCR malo |
| Apoyo | BANCO01 extracto con descuadres · PLAZOS01 eventos 303/111/115/130 |

C (12 casos: requerimiento, requerimiento ambiguo, embargo a proveedor,
embargo sin deuda, embargo de salario, factura atípica, normal, duplicada,
rectificativa, retención, multipágina, plazo 303) usa las mismas familias
con otras empresas, cifras, fechas y ruido elegidos por la semilla. Se
guarda fuera de git con un sello (hash) en su `indice.json`.

Resultados versionados y análisis de fallos: [`resultados/`](resultados/README.md).

### Golden set

Cada fallo que destapa B se convierte en una regresión permanente en
`tests/golden/test_golden.py` (caso → qué fallaba). Si un cambio rompe uno
de esos expedientes, la batería de tests falla. B v1 y B v2 se conservan en
`resultados/` para ver la evolución, no solo el resultado final.


## Reglas vs Claude vs híbrido (por expedientes)

```bash
export ANTHROPIC_API_KEY=...                              # o en backend/.env
python -m evaluation comparar --dataset b_sintetico       # B v2 (motor congelado en v0.4-b-v2)
python -m evaluation comparar --dataset c_ciego --ciego   # C: una sola vez, al final
```

Mismo código y misma verdad; solo cambia la configuración, puesta desde el
evaluador (el sistema no se toca):

| Motor | Qué es |
|---|---|
| `reglas` | Sin clave de IA. |
| `claude` | Claude interpreta **todos** los documentos (`refine(force=True)`); las reglas validan cada valor. |
| `hibrido` | El producto: Claude solo donde las reglas dudan (`needs_help`) y en notificaciones. |

El informe responde a cuatro preguntas frente a las reglas: **A** qué
corrige, **B** qué rompe (regresiones introducidas por la IA, campo a
campo), **C** en qué documentos entró y si mejoró, empató o empeoró, y
**D** cuánto cuesta (total, por documento, por 1.000 y por comprobación
corregida). El coste incluye todas las llamadas (facturas, notificaciones y
escritos): se mide envolviendo `llm._complete` desde el evaluador.

Y dos métricas de producto:

- **Autonomía**: autónomo (bien, sin persona) · asistido (a persona con el
  trabajo hecho y bien) · humano (no pudo o dudó) · error silencioso.
- **Calidad de la escalada**: precisión (de lo que se manda a una persona,
  cuánto hacía falta) y cobertura (de lo que hacía falta, cuánto se mandó).

Reglas de uso: no se cambian reglas ni etiquetas al ver el resultado. Claude
no es una fuente de etiquetas. Si sale un problema sistemático, se corrige en
v0.5 y se vuelve a medir B.

El error conocido de categoría (factura real de la tienda de informática)
se mide a nivel de documento, con las 5 facturas reales:

```bash
python -m evaluation --dataset reales --conjuntos A --engines reglas,claude,hibrido
```

## Banco C (examen)

12 casos con las familias acordadas: C01 requerimiento AEAT · C02
requerimiento ambiguo · C03 propuesta de liquidación · C04 embargo de
créditos · C05 embargo sin deuda · C06 embargo salarial · C07 factura normal
· C08 factura con anomalía · C09 rectificativa · C10 retención · C11
multipágina / OCR difícil (a veces escaneada) · C12 expediente multifuente
(correo con dos facturas, justificante del 303, extracto y requerimiento
que pide tres facturas, el 303 y los extractos: falta una factura).

Empresas, cifras, fechas, peticiones y ruido salen de la semilla. Orden:
motor congelado → generar C con tu semilla → no abrirlo → `comparar --ciego`
una vez → informe. Nada se corrige después del primer resultado.
