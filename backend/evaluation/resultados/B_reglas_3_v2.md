# Evaluación por expedientes · banco b_sintetico · motor reglas · 2026-10-01

Cada caso es un expediente (uno o varios documentos relacionados) con su verdad de referencia en `caso.json`. Las reglas NO se han tocado al ver estos resultados.

- **Expedientes completamente correctos:** 32/35 (91 %)
- **Comprobaciones superadas:** 218/221 (99 %)
- **Resultado por entrada:** correcto 47 · detectado 3

Un **error silencioso** es una entrada mal resuelta en la que el sistema no avisa ni la manda a una persona: es lo más grave.

## Por bloque

| Bloque | Expedientes bien | Comprobaciones bien |
|---|---|---|
| Bloque 1 · Requerimientos AEAT | 4/4 | 36/36 (100 %) |
| Bloque 2 · Embargos | 4/4 | 37/37 (100 %) |
| Bloque 3 · Seguridad Social | 3/3 | 21/21 (100 %) |
| Bloque 4 · Documentos normales | 3/3 | 12/12 (100 %) |
| Bloque 5 · Memoria | 3/3 | 41/41 (100 %) |
| Bloque 6 · Facturas difíciles | 3/3 | 10/10 (100 %) |
| Correo | 3/3 | 15/15 (100 %) |
| Adversariales | 8/10 | 31/33 (94 %) |
| Apoyo (banco y plazos) | 1/2 | 15/16 (94 %) |

## Por tipo de comprobación

| Comprobación | Bien | % |
|---|---|---|
| Hallazgos | 7/8 | 88 % |
| Campos de la factura | 20/22 | 91 % |
| Tipo de documento | 39/39 | 100 % |
| Ruta | 17/17 | 100 % |
| Trámite | 15/15 | 100 % |
| Organismo | 9/9 | 100 % |
| Nº de expediente | 3/3 | 100 % |
| Plazo | 16/16 | 100 % |
| ¿Necesita a una persona? | 23/23 | 100 % |
| Agentes que intervienen | 5/5 | 100 % |
| Modelo y periodo afectados | 8/8 | 100 % |
| Documentos pedidos | 7/7 | 100 % |
| No tomarlo por factura | 5/5 | 100 % |
| Trámite que NO es | 3/3 | 100 % |
| Importe de la deuda | 7/7 | 100 % |
| Plazo sin fecha de notificación (no inventar) | 1/1 | 100 % |
| Afectado (embargado) | 5/5 | 100 % |
| Crédito pendiente con el embargado | 4/4 | 100 % |
| Documentos que NO se piden | 1/1 | 100 % |
| Lo que recomienda | 3/3 | 100 % |
| Impacto fiscal | 2/2 | 100 % |
| Periodo presentado registrado | 1/1 | 100 % |
| La Memoria lo encuentra | 3/3 | 100 % |
| Destinatario desconocido | 1/1 | 100 % |
| Estado de los documentos pedidos | 2/2 | 100 % |
| Duplicado | 8/8 | 100 % |
| Hallazgos que NO deben salir | 2/2 | 100 % |
| Escaneado: no darlo por bueno | 1/1 | 100 % |

## Facturas: por campo

| Campo | Bien |
|---|---|
| supplier_tax_id | 20/22 (91 %) |
| customer_tax_id | 22/22 (100 %) |
| invoice_number | 22/22 (100 %) |
| invoice_date | 22/22 (100 %) |
| subtotal | 22/22 (100 %) |
| tax_total | 22/22 (100 %) |
| total | 22/22 (100 %) |
| direction | 22/22 (100 %) |
| withholding_total | 1/1 (100 %) |

Facturas: correcto 20 · detectado 2

## Por etiqueta

| Etiqueta | Expedientes bien |
|---|---|
| adversarial | 9/11 |
| ambiguo | 7/7 |
| contradictorio | 1/1 |
| expediente | 6/7 |
| limpio | 15/15 |
| multipagina | 2/2 |
| ocr | 2/3 |

## Fallos, caso a caso

### ✅ B01 · Comprobación limitada IVA 2T 2026
Todo correcto.

### ✅ B02 · Requerimiento que menciona «factura» muchas veces
Todo correcto.

### ✅ B03 · Propuesta de liquidación provisional
Todo correcto.

### ✅ B04 · Requerimiento sin fecha de notificación
Todo correcto.

### ✅ B05 · Embargo de créditos: crédito mayor que la deuda
Todo correcto.

### ✅ B06 · Embargo de pagos sucesivos
Todo correcto.

### ✅ B07 · Embargo sin pago pendiente
Todo correcto.

### ✅ B08 · Embargo de salario
Todo correcto.

### ✅ B09 · TGSS: requerimiento de documentación
Todo correcto.

### ✅ B10 · TGSS: reclamación de deuda
Todo correcto.

### ✅ B11 · TGSS: comunicación laboral sin impacto fiscal
Todo correcto.

### ✅ B12 · Certificado sin acción
Todo correcto.

### ✅ B13 · Justificante de presentación del 303
Todo correcto.

### ✅ B14 · Notificación a un NIF desconocido
Todo correcto.

### ✅ B15 · Requerimiento con facturas y 303 presentado
Todo correcto.

### ✅ B16 · Requerimiento con facturas y 303 presentado (falta la factura 003)
Todo correcto.

### ✅ B17 · Factura duplicada FAC-2026-145
Todo correcto.

### ✅ B18 · Factura rectificativa
Todo correcto.

### ✅ B19 · Factura con retención de IRPF
Todo correcto.

### ✅ B20 · Factura en tres páginas
Todo correcto.

### ✅ B21 · Correo con factura
Todo correcto.

### ✅ B22 · El mismo correo reenviado
Todo correcto.

### ✅ B23 · RE: Factura septiembre con original y rectificativa
Todo correcto.

### ✅ B-ADV01 · Embargo que enumera facturas
Todo correcto.

### ✅ B-ADV02 · «liquidación» dentro de «autoliquidación»
Todo correcto.

### ✅ B-ADV03 · Requerimiento escaneado
Todo correcto.

### ⚠️ B-ADV04 · Datos registrales en texto girado
- `factura_texto_girado.pdf` · **Campos de la factura**: esperado todos los campos · observado detectado (supplier_tax_id)

### ✅ B-ADV05 · Factura en dos páginas
Todo correcto.

### ⚠️ B-ADV06 · NIF del proveedor parcialmente ilegible
- `factura_nif_ilegible.pdf` · **Campos de la factura**: esperado todos los campos · observado detectado (supplier_tax_id)

### ✅ B-ADV07 · Fecha ambigua 4/5/26
Todo correcto.

### ✅ B-ADV08 · Proveedor solo en el pie legal
Todo correcto.

### ✅ B-ADV09 · Importes con dígitos separados
Todo correcto.

### ✅ B-ADV10 · Texto con errores de OCR
Todo correcto.

### ⚠️ B-BANCO01 · Extracto con descuadres
- `análisis` · **Hallazgos**: esperado ["PAGO_SIN_FACTURA", "FACTURA_SIN_PAGO"] · observado faltan FACTURA_SIN_PAGO

### ✅ B-PLAZOS01 · Plazos 303, 130, 111 y 115 del 3T
Todo correcto.
