# Hoja de ruta

Líneas base: `96f848a` (104 tests, motor de agentes) y **`v0.3-base-evaluacion` = `c33ff9c`** (131 tests, base congelada para la fase de evaluación). Lo que viene
después no rehace el núcleo y mantiene esos tests en verde.

| # | Hito | Estado |
|---|---|---|
| 1-10 | Agentes, contratos, rutas, Detector, factura sospechosa, memoria y evidencias, humano en el bucle, tests de escenarios, plazos, UI y traza | ✅ |
| 11 | Claude real + evaluación | 🟡 Banco listo (tabla por campo, coste/doc, tiempo, fallback, % de documentos con Claude, conjuntos A/B/C). **Falta ejecutarlo con `ANTHROPIC_API_KEY` y conseguir documentos nuevos para B y C** |
| 12 | DEHú real | ⏳ Pendiente de alta, certificado y apoderamiento. Entregará en `POST /api/events` |
| 13 | Idempotencia + estados + recuperación | ✅ `ingested_events`: `(source, external_id)` único; RECEIVED/PROCESSING/COMPLETED/NEEDS_HUMAN/FAILED; reanudar |
| 14 | Correo (IMAP / .eml) | ✅ `app/connectors/email`, mismo endpoint y mismo orquestador |
| 15 | Director como centro operativo | ✅ Hoy: atención (con motivo), pendiente, resuelto sin intervención, las 3 cosas de hoy y estado del trabajo continuo |
| 16 | Ejecución continua | ✅ Disparadores externo, temporal y analítico por la entrada única; lo grave del Detector lo investiga el orquestador; «Trabajar ahora» |
| 17 | Primer piloto real | ⏳ |

## Primera medida con documentos reales

Primera medida con 5 facturas reales de proveedores:

- **Antes**: el extractor acertaba 22 de 48 campos (46 %) y no leía bien
  ninguna factura entera.
- **Después**: con las reglas aprendidas de esas facturas, 48/48. Al haberse
  ajustado con esas mismas facturas, la cifra no mide generalización. Hacen
  falta documentos nuevos sin mirar.
- **Réplicas sintéticas** (otra maqueta y otros datos): 5/5.

## Fase actual: validar

Fase 1 (construir agentes) ✅ · Fase 2 (plataforma) ✅ · **Fase 3 (validar que funciona) ← aquí** ·
Fase 4 (fuentes reales: DEHú, correo) · Fase 5 (piloto) · Fase 6 (escalar casos de uso).

Fuera de alcance hasta el piloto: agentes nuevos (nóminas, altas,
contratos, WhatsApp), un chatbot fiscal y cambios en el núcleo (agentes,
expedientes, traza, memoria, contratos, Detector, reanudación).

## Fase 3 · validar (en curso)

| Paso | Estado |
|---|---|
| Medición: errores silenciosos / persona / IA / solos, matriz de errores, A-B-C, valor de Claude, errores conocidos, historial | ✅ |
| Director: trabajo realizado, % de intervención humana y tiempo ahorrado estimado | ✅ |
| Prueba de resistencia (100 eventos simultáneos) | ✅ Encontró y corrigió 3 problemas de concurrencia (+1 de fechas que destapó la demo) |
| Demo «cero intervención» (DEHú y correo) | ✅ `scripts/demo_cero_intervencion.py` |
| 1. Conjunto B (documentos nuevos, sin ajustar reglas con ellos) | ⏳ Necesita documentos |
| 2. Línea base de reglas en B y en C | ⏳ |
| 3. Claude real (`ANTHROPIC_API_KEY`) | ⏳ |
| 4. Híbrido + enrutado (¿cuándo merece la pena Claude?) | ⏳ Medición lista |
| 5. Conjunto C ciego (examen final) | ⏳ |
| 6. DEHú real (adaptador: DEHú → Event → `/api/events`) | ⏳ Pendiente de acceso |

Error conocido que se sigue: factura de una tienda de informática
clasificada como «Servicios profesionales» (conjunto A). No se corrige hasta
ver en B si es aislado o sistemático.
