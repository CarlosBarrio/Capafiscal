# Banco real: puesta en marcha y prueba

El código del banco conectado está terminado y probado contra una API simulada
(`tests/agents/test_bank_gocardless.py`). Falta lo que no es código: credenciales,
red y un titular que autorice. Esta es la prueba para cerrarlo.

## 1. Requisitos

1. Cuenta en GoCardless Bank Account Data (gratuita para empezar) → *User secrets*:
   `BANK_DATA_SECRET_ID` y `BANK_DATA_SECRET_KEY` en `.env`.
2. Salida a internet hacia `bankaccountdata.gocardless.com`.
3. `PUBLIC_BASE_URL` con la dirección desde la que el navegador vuelve a CapaFiscal
   (el banco redirige a `PUBLIC_BASE_URL/api/bank/connections/callback`).
4. Comprobación: `cd backend && python -m app.bank_connect`
   → `✓ Agregador accesible: N bancos en España · banco de pruebas … disponible`.

## 2. Primero con el banco de pruebas

Negocio → Banco → *Conectar banco* → «Sandbox Finance» (`SANDBOXFINANCE_SFIN0000`).
Autoriza con los datos de prueba que muestra la página y vuelve a CapaFiscal.

## 3. Después con un banco real (la cuenta de la empresa piloto)

Marca cada punto al probarlo:

| Comprobación | Cómo | Qué debe pasar |
|---|---|---|
| Autorización | Conectar → autorizar en el banco → volver | «Banco conectado», cuentas con IBAN enmascarado |
| Varias cuentas | Elegir 2 cuentas al autorizar | Ambas aparecen y se leen por separado |
| Movimientos | Tras conectar | Últimos 90 días asentados; los pendientes no entran |
| Duplicados | Importar antes el CSV del mismo mes | «ya estaban»: no se repite ningún movimiento |
| Movimientos nuevos | *Sincronizar* al día siguiente (o esperar a la automática cada 6 h) | Solo entran los nuevos |
| Conciliación | Facturas aprobadas con pagos en el banco | Lo seguro se concilia solo; lo dudoso queda propuesto |
| Anomalías / tesorería / Hoy | Abrir Hoy | Lo que falta justificar y el riesgo de caja, en la lista |
| Errores de la API | Sincronizar varias veces seguidas | «El banco limita las consultas…»; las demás cuentas siguen |
| Banco sin conexión | Cortar la red y sincronizar | Aviso en Hoy, nada se rompe; se reintenta |
| Renovación | A los 80 días aparece «caduca en N días» → *Renovar* | Nueva autorización; la antigua desaparece; sin duplicados |
| Desconexión | *Desconectar* | Acceso retirado en el agregador; los movimientos importados se quedan |
| Movimientos raros | Revisar los que aparecen como «sin usar» | Importe cero u otra divisa: contados, no perdidos en silencio |

Anota los conceptos reales que la conciliación no entienda (sin copiar datos
personales al repositorio): son los que hay que enseñar a las reglas.

## Límites conocidos

- El agregador permite unas 4 lecturas al día por cuenta: la automática va cada 6 h.
- El consentimiento dura 90 días (norma PSD2); CapaFiscal avisa 10 días antes.
- Movimientos en otra divisa no se importan todavía (se cuentan como «sin usar»).
