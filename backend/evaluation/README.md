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

## Añadir documentos

Cada dataset es una carpeta con los documentos y un `labels.json`:

```json
{
  "empresa": {"name": "Tu empresa S.L.", "tax_id": "B00000000"},
  "casos": [
    {"id": "proveedor_x", "file": "proveedor_x.pdf", "tags": ["tabla de totales"],
     "expected": {"direction": "RECEIVED", "supplier_name": "...", "supplier_tax_id": "...",
                  "customer_tax_id": "...", "invoice_number": "...", "invoice_date": "AAAA-MM-DD",
                  "due_date": "AAAA-MM-DD", "subtotal": "100.00", "tax_total": "21.00", "total": "121.00"}}
  ]
}
```

Solo se comparan los campos que pongas en `expected`.

- `datasets/sinteticas/`: réplicas anonimizadas de maquetas reales, generadas
  con `generar.py`. Forman parte de los tests, y fallar aquí es una regresión.
- `datasets/reales/`: tus documentos de verdad. Esta carpeta está **fuera de
  git**, porque el repositorio es público y las facturas llevan IBAN y datos
  personales.

Ojo al leer los resultados: si ajustas las reglas mirando un dataset, ese
dataset ya no mide la generalización. Guarda siempre una parte de los
documentos sin mirar para la evaluación final.
