# Resultados congelados del banco de expedientes

Aquí se guardan, versionados, los informes que no deben cambiar: la línea
base de cada banco y cada ejecución que cambie de fase. Los informes de
trabajo diario siguen en `evaluation/informes/` (fuera de git).

| Archivo | Qué es |
|---|---|
| `B_reglas_1_linea_base.md` | Primera ejecución sobre B, solo reglas, sin ver antes ningún resultado. La verdad (`caso.json`) se versionó antes (commit `85ef9b6`). |
| `B_reglas_2_tras_corregir_barrido.md` | La misma ejecución tras corregir un **fallo** (no una regla) que B destapó: el barrido de anomalías daba error 500 al guardar un pago sin factura. |

## Línea base de reglas en B (35 expedientes, 50 documentos)

- **Expedientes completamente correctos: 10/35 (29 %)**
- **Comprobaciones superadas: 174/221 (79 %)**
- **Por entrada**: 24 correctas · 24 detectadas (a una persona) · **2 errores silenciosos** · (1 error del sistema en la línea base, ya corregido)

El 29 % de expedientes perfectos es bajo porque un expediente solo cuenta si
lo acierta todo: ruta, trámite, plazo, importe, documentos y consejo. Lo que
importa es el reparto: casi todo lo que falla acaba delante de una persona;
solo 2 entradas fallan sin avisar.

### Lo que funciona

| | |
|---|---|
| Ruta (requerimiento, embargo, factura, plazo) | 17/17 |
| Tipo de documento | 38/39 |
| Afectado del embargo (proveedor / empleado) | 5/5 |
| Crédito pendiente con el embargado | 4/4 |
| Duplicados (mismo PDF, otro PDF, correo reenviado) | 8/8 |
| Modelo y periodo afectados | 8/8 |
| La Memoria encuentra el documento | 3/3 |
| Escaneado sin texto → a una persona, nunca como factura | 1/1 |
| «liquidación» dentro de «autoliquidación» | ✅ |
| Fecha corta 4/5/26, pie legal, dígitos separados, 2 y 3 páginas | ✅ |

### Lo que falla, agrupado por causa (sin corregir: B no se usa para ajustar reglas)

1. **Plazos (6/16).** No lee la «fecha de notificación (acceso)» del
   documento: calcula desde la puesta a disposición + 10 días naturales
   (B01, B02, B15, B16, B-ADV02) o desde la fecha del documento (B09). En los
   embargos toma la fecha del documento como plazo en vez de los 5 días
   hábiles para contestar (B05, B06). En la reclamación de la TGSS aplica el
   plazo de apremio (B10). Lo que sí hace bien: sin fecha de notificación
   (B04) marca el plazo como estimado y pide la fecha real.
2. **Importe de la deuda (0/7).** Coge el primer importe con palabra clave
   («Principal») y no el total pendiente (B05, B06, B07, B-ADV01, B10). En la
   propuesta de liquidación (tabla declarado/comprobado/diferencia) y en el
   embargo de salario no encuentra ninguno (B03, B08).
3. **Trámite (12/15).** La propuesta de liquidación se toma por liquidación
   (B03) y le aplica el plazo de pago; la reclamación de deuda de la TGSS se
   toma por providencia de apremio porque el texto avisa de que «se emitirá
   providencia de apremio» (B10).
4. **Documentos pedidos (3/7).** No reconoce «facturas recibidas…»,
   «justificantes de pago» ni «justificante de presentación del 303»
   (B01, B02, B15, B16). En consecuencia el Perseguidor no pide la factura
   que falta (B16) y el expediente no sabe que las tres facturas ya están
   en el sistema (B15). El Fiscal ve el 303 del periodo, pero lo señala como
   diferencia con lo presentado y no como «ya presentado».
5. **Embargos: qué retener** (la comprobación de hallazgos, en todo B: 2/8). Si el crédito (4.100 €) supera
   la deuda (2.850 €), aconseja retener todas las facturas pendientes, no
   solo la deuda (B05, B-ADV01). No menciona los pagos sucesivos (B06).
   Sí acierta cuando no hay nada pendiente (B07).
6. **Documentos sin acción.** La comunicación laboral de la TGSS se lee como
   factura (toma el código de cuenta de cotización por NIF) (B11); el
   certificado de estar al corriente abre un expediente que espera a una
   persona (B12); el justificante del 303 se lee como factura y el periodo
   no queda registrado como presentado (B13). En los tres casos va a una
   persona: no es silencioso, pero es trabajo innecesario.
7. **NIF desconocido (B14).** Una notificación para otra empresa se trata
   como propia: no avisa de que el NIF no coincide.
8. **Facturas (16/22 correctas).**
   - ⛔ **Rectificativa con importes positivos** (B18): base 200, total 242 en
     vez de −200 / −242, sin avisar. **Error silencioso.**
   - ⛔ **OCR malo** (B-ADV10): nº de factura «550» en vez de «SFD-2026-144»,
     sin avisar. **Error silencioso.**
   - Detectados (van a persona): profesional persona física con retención
     (B19), NIF girado (B-ADV04) o ilegible (B-ADV06): toma el NIF de la
     propia empresa como emisor y la marca como emitida; rectificativa dentro
     de un correo con número mal leído (B23).
9. **Plazos de modelos.** Crea el 130 a una sociedad (B-PLAZOS01).
10. **Banco.** El barrido daba error 500 con un pago sin factura (corregido,
    con prueba de regresión independiente de B). Tras corregirlo detecta el
    pago sin factura, pero no la factura sin pago.

### Lo que B no responde

- **El error conocido de categoría** (tienda de informática clasificada como
  «Servicios profesionales»): las facturas de B no llevan categoría en su
  verdad, así que B no dice si es aislado o sistemático. Hace falta el
  conjunto B de documentos reales.
- **Claude**: esta es la línea base solo con reglas. La comparación con
  `--motor hibrido` necesita `ANTHROPIC_API_KEY`.

### Límite metodológico

B lo ha escrito el mismo autor que las reglas: no es ciego. Sirve para medir
y priorizar, no como examen. El examen es C (semilla elegida por quien
evalúa, fuera de git, una sola ejecución) y, mejor aún, documentos reales de
otra persona.
