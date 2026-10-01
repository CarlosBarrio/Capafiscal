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
