# Hoja de ruta

Línea base congelada: PR #1, commit `96f848a`, 104 tests. Lo que viene
después no rehace el núcleo y mantiene esos tests en verde.

| # | Hito | Estado |
|---|---|---|
| 1-10 | Agentes, contratos, rutas, Detector, factura sospechosa, memoria y evidencias, humano en el bucle, tests de escenarios, plazos, UI y traza | ✅ |
| 11 | Claude real + evaluación | 🟡 Integrado como capacidad validada por reglas, con banco de evaluación. Falta ejecutarlo con `ANTHROPIC_API_KEY` y ampliar el dataset (notificaciones, embargos, documentos ambiguos) |
| 12 | DEHú real | ⏳ Pendiente de alta, certificado y apoderamiento. Entregará en `POST /api/events` |
| 13 | Idempotencia + estados + recuperación | ✅ `ingested_events`: `(source, external_id)` único; RECEIVED/PROCESSING/COMPLETED/NEEDS_HUMAN/FAILED; reanudar |
| 14 | Correo (IMAP / .eml) | ✅ `app/connectors/email`, mismo endpoint y mismo orquestador |
| 15 | Director como centro operativo | ⏳ |
| 16 | Ejecución continua | 🟡 Buzón cada 5 min, plazos diarios y barrido del Detector. Faltan disparadores analíticos |
| 17 | Primer piloto real | ⏳ |

## Primera medida con documentos reales

Primera medida con 5 facturas reales de proveedores:

- **Antes**: el extractor acertaba 22 de 48 campos (46 %) y no leía bien
  ninguna factura entera.
- **Después**: con las reglas aprendidas de esas facturas, 48/48. Al haberse
  ajustado con esas mismas facturas, la cifra no mide generalización. Hacen
  falta documentos nuevos sin mirar.
- **Réplicas sintéticas** (otra maqueta y otros datos): 5/5.
