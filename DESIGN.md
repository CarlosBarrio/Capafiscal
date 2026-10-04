---
name: CapaFiscal
description: Capa de trabajo fiscal para pymes; sobria, cálida y precisa.
colors:
  bg: "#f6f5f2"
  surface: "#ffffff"
  surface-2: "#faf9f7"
  surface-3: "#f1efea"
  line: "#e8e5df"
  line-strong: "#d8d3ca"
  ink: "#1c1b19"
  ink-2: "#3f3b36"
  muted: "#655f58"
  faint: "#736c64"
  brand: "#8c1d33"
  brand-600: "#72172a"
  brand-050: "#f8eef0"
  brand-100: "#f0dde1"
  income: "#2563a8"
  expense: "#9e1b32"
  green: "#1a7a4c"
  green-bg: "#e8f4ed"
  amber: "#8f5f00"
  amber-bg: "#fbf2de"
  red: "#b42336"
  red-bg: "#fbeaec"
  blue: "#245fa0"
  blue-bg: "#e9f1fa"
  violet: "#6b3fa0"
  violet-bg: "#f2ecf8"
typography:
  heading:
    fontFamily: "Source Serif 4, Iowan Old Style, Georgia, serif"
    fontWeight: 600
  body:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  ui:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 500
  mono:
    fontFamily: "SFMono-Regular, Consolas, Liberation Mono, monospace"
rounded:
  sm: "8px"
  md: "12px"
  pill: "999px"
spacing:
  sidebar: "244px"
  topbar: "60px"
components:
  button-primary:
    backgroundColor: "{colors.brand}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "0 14px"
  button-primary-hover:
    backgroundColor: "{colors.brand-600}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.sm}"
    height: "36px"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.md}"
    padding: "20px 22px"
  status-pill:
    rounded: "{rounded.pill}"
---

# CapaFiscal · lenguaje visual

Fuente de verdad: `backend/app/static/style.css` (tokens en `:root`). Este documento describe lo que
ya existe y las decisiones tomadas; no es una propuesta de rediseño. Contexto de producto: `PRODUCT.md`.

## Overview

Un despacho bien llevado: neutros cálidos de papel, tinta casi negra, un único color de marca
(granate) y titulares en serif. Premium por precisión, no por adorno. Sobrio, profesional, calmado y
fiable. Modo Operate: la persona viene a terminar trabajo; la interfaz se aparta.

No es: startup genérica de IA, panel cripto, plantilla SaaS, colorido, rejillas de tarjetas,
degradados ni nada infantil.

Jerarquía de cada pantalla: contexto (migas) → título → información principal → acciones →
secundario.

## Colors

- **Neutros** (`bg`, `surface*`, `line*`, `ink*`) hacen el 90 % del trabajo.
- **Marca** (`brand`): la acción principal de cada bloque, la navegación activa y la selección. Una
  acción primaria por zona; las acciones repetidas en filas son secundarias.
- **Estado**, y solo estado: `green` correcto/comprobado, `amber` aviso/pendiente, `red` error o
  vencido, `blue` información, `violet` automatizado/notificación oficial. Siempre en pares texto +
  fondo `*-bg`, y siempre con texto (pill, etiqueta) además del color.
- **Contraste**: `muted` (#655f58) y `faint` (#736c64) cumplen ≥ 4,5:1 sobre todas las superficies.
  Antes eran #756f67 y #a49e95 (2,5:1); se oscurecieron sin cambiar el tono.
- `income` / `expense` solo para importes con signo.

## Typography

- **Source Serif 4** para títulos de pantalla y de bloque (h1, h2, cifras destacadas). **Inter**
  para todo lo demás. Se mantienen: forman parte de la identidad y funcionan.
- Cifras con `font-variant-numeric: tabular-nums` y alineadas a la derecha en tablas.
- Tamaño mínimo de texto legible: 11 px (pills mini) y 11,5 px para notas; cuerpo de 13–14 px.
- Cabeceras de tabla, títulos de grupo y etiquetas en minúscula normal (frase), nunca en versalitas;
  las etiquetas de cifras empiezan en mayúscula. Titulares con `text-wrap: balance`.
- Etiquetas de grupo de la navegación en minúscula normal (12 px, `muted`), no en versalitas: sin
  etiquetas en mayúsculas sobre el contenido.

## Layout

- Barra lateral fija de 244 px con grupos (Operación, Finanzas, Empresa, Más) + barra superior de
  60 px con migas. En ≤ 1024 px la barra lateral pasa a cajón.
- Saltos: 1180 / 1024 / 640 px. En ≤ 640 px las rejillas de filtros pasan a una columna y los filtros
  secundarios de Facturas se pliegan bajo «Más filtros».
- Listas antes que rejillas de tarjetas: filas con separadores finos dentro de una sola tarjeta.

## Elevation & Depth

- Casi plano. `--shadow-sm` (1 px) en tarjetas; `--shadow` en hover de elementos clicables;
  `--shadow-lg` (contenida, 32 px de desenfoque máximo) solo en diálogos, cajones y menús.
- La profundidad se expresa con bordes `line` y fondos `surface-2/3`, no con sombras.

## Shapes

- `--radius` 12 px para tarjetas y diálogos; `--radius-sm` 8 px para botones, campos y filas;
  999 px para pills. Nada fuera de esa escala.
- Sin franjas laterales de color (borde izquierdo grueso) en tarjetas, avisos o filas: el estado se
  dice con pill, punto + texto o icono.

## Components

- **Botones**: `.act-btn act-primary` (granate, uno por zona), `.act-btn` / `.btn-ghost`
  (secundarios), `.btn-outline`, `.btn-clear`. Altura 36 px; 40 px mínimo en móvil.
- **Tarjeta** `.card`: un nivel. Nunca tarjetas dentro de tarjetas: dentro se usan filas
  (`.invoice-row`, `.tax-entry`, `.doc-row`) separadas por `border-top: 1px solid var(--line)`.
- **Estado** `.status-pill` (+ `.mini`), punto `.level-dot` con texto al lado.
- **Segmentado** `.segmented` para alternar vistas; **migas** en la barra superior.
- **Mensajes** `.app-message`: superficie blanca con punto de estado; `role="alert"` en errores.
- **Confirmaciones y preguntas** `dialogs.js`: `await askConfirm("¿Eliminar…?")` y `await askText(…, { minLength })`.
  Nunca `confirm()`/`prompt()` del navegador. El botón toma el verbo de la pregunta; lo destructivo, en rojo
  suave y con el foco en «Cancelar».
- **Plurales** `pl(n, "factura(s) aprobada(s)")`: nunca «(s)» a la vista.
- **Formularios largos**: agrupados con `.form-section` (p. ej. detalle de factura: quién, factura, importes);
  importes alineados a la derecha con cifras tabulares, escritos con coma decimal («1234,56»). Un importe que
  no se entiende no se guarda: se marca y se explica.
- **Detalle de un documento**: cabecera de decisión (`.detail-head`: qué es, estado, lectura y total arriba) y
  barra de acciones fija al pie (`.detail-actions-bar`): una acción principal, una secundaria y el resto en
  «Más acciones». «Guardar cambios» solo aparece cuando hay cambios.
- **Documentos que no son factura** (albarán, presupuesto u oferta, pedido, proforma, nómina): no van a «Para
  revisar» ni se pintan como facturas rotas («s/n», «—»). Fila con su tipo, archivo y fecha, pill neutra «No es
  factura» y «Ver», en la vista «Otros documentos». En el detalle, qué es y que no entra en la contabilidad; si la
  clasificación se equivoca, «Es una factura» en «Más acciones».
- **Campos**: borde `line-strong`, foco con contorno de marca (`:focus-visible`), deshabilitado
  con fondo `surface-3`.
- **Estados vacíos** `.board-empty` / `emptyState()`: una frase con contexto y, si existe, la acción.

## Do's and Don'ts

- Sí: una acción primaria por bloque; color = estado; texto además del color; foco visible;
  `prefers-reduced-motion`; transiciones de 120–200 ms en transform/opacity con ease-out.
- Sí: comprobar a 1280 y 390 px cada cambio.
- No: degradados, desenfoques, texto con degradado, sombras amplias, badges decorativos, franjas
  laterales de color, tarjetas anidadas, etiquetas en mayúsculas sobre el contenido, ceros que no
  informan, animaciones de adorno.
- No: rediseñar pantallas que funcionan; se mejora el sistema existente paso a paso.
