# Evaluación por expedientes · banco b_sintetico · motor reglas · 2026-10-01

Cada caso es un expediente (uno o varios documentos relacionados) con su verdad de referencia en `caso.json`. Las reglas NO se han tocado al ver estos resultados.

- **Expedientes completamente correctos:** 10/35 (29 %)
- **Comprobaciones superadas:** 174/221 (79 %)
- **Resultado por entrada:** correcto 24 · detectado 24 · error silencioso 2

Un **error silencioso** es una entrada mal resuelta en la que el sistema no avisa ni la manda a una persona: es lo más grave.

## Por bloque

| Bloque | Expedientes bien | Comprobaciones bien |
|---|---|---|
| Bloque 1 · Requerimientos AEAT | 1/4 | 28/36 (78 %) |
| Bloque 2 · Embargos | 0/4 | 29/37 (78 %) |
| Bloque 3 · Seguridad Social | 0/3 | 14/21 (67 %) |
| Bloque 4 · Documentos normales | 0/3 | 8/12 (67 %) |
| Bloque 5 · Memoria | 1/3 | 33/41 (80 %) |
| Bloque 6 · Facturas difíciles | 1/3 | 8/10 (80 %) |
| Correo | 2/3 | 14/15 (93 %) |
| Adversariales | 5/10 | 27/33 (82 %) |
| Apoyo (banco y plazos) | 0/2 | 13/16 (81 %) |

## Por tipo de comprobación

| Comprobación | Bien | % |
|---|---|---|
| Importe de la deuda | 0/7 | 0 % |
| Periodo presentado registrado | 0/1 | 0 % |
| Destinatario desconocido | 0/1 | 0 % |
| Estado de los documentos pedidos | 0/2 | 0 % |
| Hallazgos | 2/8 | 25 % |
| Plazo | 6/16 | 38 % |
| Documentos pedidos | 3/7 | 43 % |
| Impacto fiscal | 1/2 | 50 % |
| Trámite que NO es | 2/3 | 67 % |
| Lo que recomienda | 2/3 | 67 % |
| Campos de la factura | 16/22 | 73 % |
| Trámite | 12/15 | 80 % |
| No tomarlo por factura | 4/5 | 80 % |
| Organismo | 8/9 | 89 % |
| ¿Necesita a una persona? | 22/23 | 96 % |
| Tipo de documento | 38/39 | 97 % |
| Ruta | 17/17 | 100 % |
| Nº de expediente | 3/3 | 100 % |
| Agentes que intervienen | 5/5 | 100 % |
| Modelo y periodo afectados | 8/8 | 100 % |
| Plazo sin fecha de notificación (no inventar) | 1/1 | 100 % |
| Afectado (embargado) | 5/5 | 100 % |
| Crédito pendiente con el embargado | 4/4 | 100 % |
| Documentos que NO se piden | 1/1 | 100 % |
| La Memoria lo encuentra | 3/3 | 100 % |
| Duplicado | 8/8 | 100 % |
| Hallazgos que NO deben salir | 2/2 | 100 % |
| Escaneado: no darlo por bueno | 1/1 | 100 % |

## Facturas: por campo

| Campo | Bien |
|---|---|
| supplier_tax_id | 19/22 (86 %) |
| customer_tax_id | 19/22 (86 %) |
| invoice_number | 20/22 (91 %) |
| invoice_date | 22/22 (100 %) |
| subtotal | 20/22 (91 %) |
| tax_total | 20/22 (91 %) |
| total | 20/22 (91 %) |
| direction | 19/22 (86 %) |
| withholding_total | 1/1 (100 %) |

Facturas: correcto 16 · detectado 4 · error silencioso 2

## Por etiqueta

| Etiqueta | Expedientes bien |
|---|---|
| adversarial | 5/11 |
| ambiguo | 2/7 |
| contradictorio | 0/1 |
| expediente | 2/7 |
| limpio | 1/15 |
| multipagina | 2/2 |
| ocr | 1/3 |

## Fallos, caso a caso

### ⚠️ B01 · Comprobación limitada IVA 2T 2026
- `requerimiento_iva_2T.pdf` · **Plazo**: esperado 2026-10-13 · observado 2026-10-20
- `requerimiento_iva_2T.pdf` · **Documentos pedidos**: esperado ["LIBRO_RECIBIDAS", "FACTURAS", "JUSTIFICANTE_PAGO"] · observado faltan FACTURAS, JUSTIFICANTE_PAGO

### ⚠️ B02 · Requerimiento que menciona «factura» muchas veces
- `requerimiento_facturas.pdf` · **Plazo**: esperado 2026-10-05 · observado 2026-10-13
- `requerimiento_facturas.pdf` · **Documentos pedidos**: esperado ["FACTURAS", "JUSTIFICANTE_PAGO", "LIBRO_RECIBIDAS"] · observado faltan FACTURAS, JUSTIFICANTE_PAGO

### ⚠️ B03 · Propuesta de liquidación provisional
- `propuesta_liquidacion.pdf` · **Trámite**: esperado PROPUESTA_LIQUIDACION · observado LIQUIDACION
- `propuesta_liquidacion.pdf` · **Trámite que NO es**: esperado ["REQUERIMIENTO", "LIQUIDACION", "COMPROBACION_LIMITADA"] · observado LIQUIDACION
- `propuesta_liquidacion.pdf` · **Plazo**: esperado 2026-10-06 · observado 2026-11-05
- `propuesta_liquidacion.pdf` · **Importe de la deuda**: esperado 1324.02 · observado None

### ✅ B04 · Requerimiento sin fecha de notificación
Todo correcto.

### ⚠️ B05 · Embargo de créditos: crédito mayor que la deuda
- `embargo_creditos.pdf` · **Importe de la deuda**: esperado 2850.0 · observado 2375.0
- `embargo_creditos.pdf` · **Plazo**: esperado 2026-10-06 · observado 2026-09-25
- `embargo_creditos.pdf` · **Hallazgos**: esperado ["credit_exceeds_debt"] · observado faltan credit_exceeds_debt

### ⚠️ B06 · Embargo de pagos sucesivos
- `embargo_pagos_sucesivos.pdf` · **Importe de la deuda**: esperado 6200.0 · observado 5000.0
- `embargo_pagos_sucesivos.pdf` · **Plazo**: esperado 2026-10-07 · observado 2026-09-28
- `embargo_pagos_sucesivos.pdf` · **Hallazgos**: esperado ["successive_payments"] · observado faltan successive_payments

### ⚠️ B07 · Embargo sin pago pendiente
- `embargo_sin_deuda.pdf` · **Importe de la deuda**: esperado 1540.0 · observado 1283.33

### ⚠️ B08 · Embargo de salario
- `embargo_salario.pdf` · **Importe de la deuda**: esperado 3410.55 · observado None

### ⚠️ B09 · TGSS: requerimiento de documentación
- `tgss_requerimiento.pdf` · **Plazo**: esperado 2026-10-08 · observado 2026-10-06

### ⚠️ B10 · TGSS: reclamación de deuda
- `tgss_reclamacion_deuda.pdf` · **Trámite**: esperado LIQUIDACION · observado APREMIO
- `tgss_reclamacion_deuda.pdf` · **Importe de la deuda**: esperado 3528.22 · observado 2940.18
- `tgss_reclamacion_deuda.pdf` · **Plazo**: esperado 2026-09-30 · observado 2026-08-20

### ⚠️ B11 · TGSS: comunicación laboral sin impacto fiscal
- `tgss_comunicacion_alta.pdf` · **Tipo de documento**: esperado NOTIFICATION · observado INVOICE
- `tgss_comunicacion_alta.pdf` · **Organismo**: esperado TGSS · observado None
- `tgss_comunicacion_alta.pdf` · **Trámite**: esperado COMUNICACION · observado None

### ⚠️ B12 · Certificado sin acción
- `certificado_corriente.pdf` · **¿Necesita a una persona?**: esperado False · observado True (evento COMPLETED, expediente WAITING_HUMAN)

### ⚠️ B13 · Justificante de presentación del 303
- `justificante_303_2T.pdf` · **No tomarlo por factura**: esperado True · observado INVOICE
- `expediente` · **Periodo presentado registrado**: esperado {"model": "303", "year": 2026, "quarter": 2} · observado no consta

### ⚠️ B14 · Notificación a un NIF desconocido
- `requerimiento_nif_desconocido.pdf` · **Destinatario desconocido**: esperado B99999997 · observado titular=company avisos=1

### ⚠️ B15 · Requerimiento con facturas y 303 presentado
- `requerimiento_facturas_303.pdf` · **Plazo**: esperado 2026-10-13 · observado 2026-10-20
- `requerimiento_facturas_303.pdf` · **Documentos pedidos**: esperado ["FACTURAS", "MODELOS"] · observado faltan FACTURAS, MODELOS
- `requerimiento_facturas_303.pdf` · **Hallazgos**: esperado ["PERIODO_PRESENTADO"] · observado faltan PERIODO_PRESENTADO
- `requerimiento_facturas_303.pdf` · **Estado de los documentos pedidos**: esperado {"FACTURAS": ["ready", "received", "provided"]} · observado FACTURAS=no pedido

### ⚠️ B16 · Requerimiento con facturas y 303 presentado (falta la factura 003)
- `requerimiento_facturas_303.pdf` · **Plazo**: esperado 2026-10-13 · observado 2026-10-20
- `requerimiento_facturas_303.pdf` · **Documentos pedidos**: esperado ["FACTURAS", "MODELOS"] · observado faltan FACTURAS, MODELOS
- `requerimiento_facturas_303.pdf` · **Hallazgos**: esperado ["PERIODO_PRESENTADO"] · observado faltan PERIODO_PRESENTADO
- `requerimiento_facturas_303.pdf` · **Estado de los documentos pedidos**: esperado {"FACTURAS": ["partial", "missing", "requested"]} · observado FACTURAS=no pedido

### ✅ B17 · Factura duplicada FAC-2026-145
Todo correcto.

### ⛔ B18 · Factura rectificativa
- `rectificativa_R-2026-0031.pdf` · **Campos de la factura**: esperado todos los campos · observado error_silencioso (subtotal, tax_total, total)
- ⛔ Error silencioso en: rectificativa_R-2026-0031.pdf

### ⚠️ B19 · Factura con retención de IRPF
- `honorarios_A-26-118.pdf` · **Campos de la factura**: esperado todos los campos · observado detectado (supplier_tax_id, customer_tax_id, direction)

### ✅ B20 · Factura en tres páginas
Todo correcto.

### ✅ B21 · Correo con factura
Todo correcto.

### ✅ B22 · El mismo correo reenviado
Todo correcto.

### ⚠️ B23 · RE: Factura septiembre con original y rectificativa
- `re_factura_septiembre.eml#2` · **Campos de la factura**: esperado todos los campos · observado detectado (invoice_number, subtotal, tax_total, total)

### ⚠️ B-ADV01 · Embargo que enumera facturas
- `embargo_con_facturas.pdf` · **Importe de la deuda**: esperado 1200.0 · observado 1000.0
- `embargo_con_facturas.pdf` · **Hallazgos**: esperado ["credit_exceeds_debt"] · observado faltan credit_exceeds_debt

### ⚠️ B-ADV02 · «liquidación» dentro de «autoliquidación»
- `verificacion_autoliquidacion.pdf` · **Plazo**: esperado 2026-10-08 · observado 2026-10-19

### ✅ B-ADV03 · Requerimiento escaneado
Todo correcto.

### ⚠️ B-ADV04 · Datos registrales en texto girado
- `factura_texto_girado.pdf` · **Campos de la factura**: esperado todos los campos · observado detectado (supplier_tax_id, customer_tax_id, direction)

### ✅ B-ADV05 · Factura en dos páginas
Todo correcto.

### ⚠️ B-ADV06 · NIF del proveedor parcialmente ilegible
- `factura_nif_ilegible.pdf` · **Campos de la factura**: esperado todos los campos · observado detectado (supplier_tax_id, customer_tax_id, direction)

### ✅ B-ADV07 · Fecha ambigua 4/5/26
Todo correcto.

### ✅ B-ADV08 · Proveedor solo en el pie legal
Todo correcto.

### ✅ B-ADV09 · Importes con dígitos separados
Todo correcto.

### ⛔ B-ADV10 · Texto con errores de OCR
- `factura_ocr_malo.pdf` · **Campos de la factura**: esperado todos los campos · observado error_silencioso (invoice_number)
- ⛔ Error silencioso en: factura_ocr_malo.pdf

### ⚠️ B-BANCO01 · Extracto con descuadres
- `análisis` · **Hallazgos**: esperado ["PAGO_SIN_FACTURA", "FACTURA_SIN_PAGO"] · observado faltan FACTURA_SIN_PAGO

### ⚠️ B-PLAZOS01 · Plazos 303, 130, 111 y 115 del 3T
- `plazo_130_2026_3T.json` · **Impacto fiscal**: esperado False · observado con modelos
- `plazo_130_2026_3T.json` · **Lo que recomienda**: esperado ["sociedad"] · observado no dice sociedad
