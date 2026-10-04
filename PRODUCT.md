# CapaFiscal · producto

**Qué es.** Una capa de trabajo fiscal y administrativo para pymes y autónomos en España (y para la
gestoría que les lleva los papeles). Lee facturas, notificaciones de la AEAT/TGSS/DEHú y movimientos
del banco; los contabiliza, concilia, calcula el IVA y la tesorería, y detecta lo que no cuadra.

**Quién lo usa.** La persona que lleva la administración de una empresa pequeña (a menudo el propio
gerente) y su gestor o asesor fiscal. No son diseñadores ni técnicos: abren la aplicación unos
minutos al día para terminar trabajo y quieren irse sabiendo que nada se les escapa.

**Qué problema resuelve.** Los datos se introducen una vez y recorren el circuito
(factura → contabilidad → conciliación → IVA → tesorería → detector). El trabajo pendiente aparece
en una sola lista (Centro de trabajo) con lo que CapaFiscal ya ha comprobado y la acción siguiente.
Solo entra lo que ahorra horas, evita errores, anticipa problemas o quita decisiones repetitivas.

**Cómo debe sentirse.** Calmado, preciso y fiable: como un despacho bien llevado. La interfaz no
compite con los datos; deja claro qué está comprobado, qué necesita una persona y qué vence pronto.

**Lo que no es.** Ni un chatbot, ni un CRM, ni un cuadro de mando para enseñar, ni un panel cripto o
una plantilla SaaS. Nada de automatizaciones de escaparate.

**Modo de diseño (Impeccable).** Operate: la persona viene a terminar una tarea. Escaneabilidad,
coherencia y estados claros por encima de la expresión.

**Cifras: cero, sin datos o error.** 0 significa cero. «—» significa que aún no lo sabemos (cargando
o error). Sin datos, se dice con una frase y no con una fila de ceros. Si algo no se pudo cargar, se
dice qué y se ofrece «Reintentar» (`setFigures` y `loadErrorHtml` en `app.js`, el mismo aviso que «Hoy»).

**Decisión pendiente: el Asistente.** Sigue disponible solo en «Más», sin protagonismo y con su
funcionalidad actual. Está por decidir si se mantiene como asistente conversacional o se convierte en
una ayuda contextual que actúe sobre la pantalla en curso. Se decidirá cuando esté terminado el resto
de la revisión del frontend.

> Redactado a partir de `CLAUDE.md` y del propio producto; si alguna frase no encaja con la visión,
> corrígela aquí: las skills de diseño leen este archivo.
