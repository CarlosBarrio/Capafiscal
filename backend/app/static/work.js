"use strict";

/* Hoy: un centro de decisión, no un cuadro de mando.
   Orden: 1) lo que tienes que decidir  2) lo que falta  3) estado (cierre, impuestos, caja)
          4) lo que CapaFiscal ya ha resuelto.
   Cada decisión explica qué pasa, por qué importa, qué ha comprobado CapaFiscal, qué propone y qué te toca.
   Solo lee: /work, /work/metrics, /taxes/position y /treasury. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const TOP = 5;  // «Hoy deberías revisar»: pocas cosas, por impacto
  const REFRESH_MS = 60000;
  const YOU = {  // qué te toca, cuando el elemento no lo trae
    invoice: "Aprobar, corregir o rechazar", outbox: "Revisar y enviar", rule: "Aprobar o rechazar la regla",
    bank_conflict: "Elegir qué factura es", liquidity: "Decidir cómo cubrir la caja", pay: "Registrar el pago",
    collect: "Reclamar el cobro", unpaid: "Decir si se pagó por otra vía", event: "Revisar la entrada",
    compliance: "Revisarlo", bank_consent: "Renovar el acceso", bank_consent_soon: "Renovar el acceso", file: "Revisar y presentar",
  };
  const FLOW = {  // tesorería: de qué está hecha la previsión
    collection: ["+", "cobro(s) esperados"], payment: ["−", "pago(s) de facturas"], recurring: ["−", "cargo(s) habituales"],
    tax: ["−", "impuesto(s) estimados"], payroll: ["−", "nómina(s)"], social_security: ["−", "seguro(s) sociales"],
  };
  let data = null;
  let metrics = null;
  let fiscal = null;
  let treasury = null;
  const expanded = new Set();
  let timer = null;

  const money = (value) => (value === null || value === undefined ? "" : window.formatMoney(Math.abs(value)));
  const signed = (value) => `${value < 0 ? "−" : ""}${window.formatMoney(Math.abs(value))}`;
  const ddmm = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}` : "");
  const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

  function mark(ok) {
    return ok === true ? `<span class="work-check is-ok" aria-label="Comprobado">✓</span>`
      : ok === false ? `<span class="work-check is-block" aria-label="Problema">✕</span>`
      : `<span class="work-check is-warn" aria-label="Pendiente">!</span>`;
  }

  function actionAttrs(action) {
    if (!action) return "";
    if (action.case_id) return `data-work-case="${Number(action.case_id)}"`;
    if (action.document_id) return `data-work-document="${Number(action.document_id)}"`;
    return `data-go="${esc(action.tab || "panel")}" data-anchor="${esc(action.anchor || "")}" data-view="${esc(action.view || "")}"`;
  }

  function checksHtml(checks, label = "CapaFiscal ha comprobado") {
    if (!checks.length) return "";
    const shown = checks.slice(0, 3);
    const rest = checks.slice(3);
    return `
      <div class="hoy-block">
        <span class="hoy-label">${esc(label)}</span>
        <ul class="hoy-checks">${shown.map((check) => `<li>${mark(check.ok)}<span>${esc(check.label)}</span></li>`).join("")}</ul>
        ${rest.length ? `<details class="work-checked"><summary>${rest.length} comprobación(es) más</summary>
          <ul>${rest.map((check) => `<li>${mark(check.ok)}<span>${esc(check.label)}</span></li>`).join("")}</ul></details>` : ""}
      </div>`;
  }

  /* «Requerimiento de información de Agencia Tributaria. Pide: …» bajo el título «Requerimiento de información · …»:
     la primera frase repite el título y se quita. */
  function withoutTitle(why, title) {
    const head = (title || "").split(" · ")[0].toLowerCase();
    const cut = why.indexOf(". ");
    return head && cut > 0 && why.toLowerCase().startsWith(head) ? why.slice(cut + 2) : why;
  }

  /* Una decisión completa: qué pasa · por qué importa · comprobado · propone · te toca. */
  function decisionHtml(item, first) {
    const liquidity = item.kind === "liquidity";
    const checks = liquidity ? [] : (item.checked || []);
    const proposal = item.proposal || (liquidity && item.checked?.length ? item.checked[0].label : null);
    const you = item.you || YOU[item.kind] || item.action?.label || "Revisarlo";
    return `
      <li class="work-item hoy-decision">
        <div class="work-item-main">
          <div class="work-item-text">
            <strong>${esc(item.title)}</strong>
            <span class="work-why">${esc(withoutTitle(item.why || "", item.title))}</span>
            ${item.means ? `<span class="hoy-means">${esc(item.means)}</span>` : ""}
          </div>
          ${item.amount !== null && item.amount !== undefined ? `<span class="work-amount">${money(item.amount)}</span>` : ""}
        </div>
        ${checksHtml(checks)}
        ${proposal ? `<p class="hoy-line"><span class="hoy-label">Propone</span><span>${esc(proposal)}</span></p>` : ""}
        <div class="hoy-you">
          <p class="hoy-line"><span class="hoy-label">Te toca</span><span>${esc(you)}</span></p>
          <div class="hoy-actions">
            ${item.secondary ? `<button type="button" class="link-button" ${actionAttrs(item.secondary)}>${esc(item.secondary.label)}</button>` : ""}
            ${item.action ? `<button type="button" class="${first ? "act-btn act-primary" : "btn-ghost"} work-act" ${actionAttrs(item.action)}>${esc(item.action.label)}</button>` : ""}
          </div>
        </div>
        ${item.simulate ? `<button type="button" class="link-button work-sim-link" data-work-simulate='${esc(JSON.stringify(item.simulate))}'>${esc(item.simulate_label)}</button><div class="work-sim" aria-live="polite"></div>` : ""}
      </li>`;
  }

  /* Un bloqueo: qué falta y el botón para resolverlo. */
  function compactHtml(item) {
    return `
      <li class="work-item">
        <div class="work-item-main">
          <div class="work-item-text">
            <strong>${esc(item.title)}</strong>
            <span class="work-why">${esc(item.why)}</span>
          </div>
          <div class="work-item-side">
            ${item.amount !== null && item.amount !== undefined ? `<span class="work-amount">${money(item.amount)}</span>` : ""}
            ${item.action ? `<button type="button" class="btn-ghost work-act" ${actionAttrs(item.action)}>${esc(item.action.label)}</button>` : ""}
          </div>
        </div>
      </li>`;
  }

  function listSection(key, cls, title, sub, items, render) {
    const open = expanded.has(key);
    const shown = open ? items : items.slice(0, TOP);
    const rest = items.length - shown.length;
    return `
      <section class="card work-group ${cls}" aria-labelledby="hoy-${key}">
        <div class="card-head"><h2 class="work-group-title" id="hoy-${key}"><span class="board-dot"></span>${esc(title)}</h2><span class="card-sub">${esc(sub)}</span></div>
        <ol class="work-list">${shown.map((item, index) => render(item, index === 0)).join("")}</ol>
        ${rest > 0 ? `<button type="button" class="link-button work-more" data-work-more="${key}">Ver ${rest} más</button>` : ""}
      </section>`;
  }

  function group(key) {
    return data.groups.find((item) => item.key === key) || { items: [], count: 0 };
  }

  /* Lo que CapaFiscal terminó sin intervención: conciliaciones automáticas + avisos cerrados solos (30 días). */
  function resolvedCount() {
    return metrics ? metrics.without_human : group("resuelto").count;
  }

  function lacking() {
    return group("falta").items.filter((item) => item.kind !== "tax");  // los impuestos tienen su propio bloque
  }

  function summaryHtml() {
    const decide = group("accion").items.length;
    const missing = lacking().length;
    const solved = resolvedCount();
    return `
      <li class="state-red"><span class="board-dot"></span><strong>${decide}</strong> ${decide === 1 ? "requiere" : "requieren"} tu atención</li>
      <li class="state-orange"><span class="board-dot"></span><strong>${missing}</strong> ${missing === 1 ? "espera" : "esperan"} información</li>
      <li class="state-done"><span class="board-dot"></span><strong>${solved}</strong> ${solved === 1 ? "trabajo resuelto" : "trabajos resueltos"} por CapaFiscal en ${metrics?.days || 7} días</li>`;
  }

  function calmHtml() {
    return `
      <section class="card work-group state-done hoy-calm">
        <div class="card-head"><h2 class="work-group-title"><span class="board-dot"></span>Todo al día</h2></div>
        <p>No hay nada que decidir ni información pendiente.</p>
        <p class="board-empty">CapaFiscal sigue vigilando facturas, banco y plazos; si aparece algo, estará aquí arriba.</p>
      </section>`;
  }

  /* ---------- Estado de la empresa ---------- */

  function closeCard() {
    const close = data.close;
    if (!close) return "";
    const figure = close.ready ? "Listo para cerrar" : close.blockers ? plural(close.blockers, "bloqueo", "bloqueos") : `${close.percent} %`;
    return `
      <section class="card hoy-state">
        <div class="card-head"><h2>Cierre del mes</h2><span class="card-sub">${esc(close.label)}</span></div>
        <p class="hoy-figure">${esc(figure)}</p>
        <span class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${close.percent}" aria-label="Revisado y conciliado"><span style="width:${close.percent}%"></span></span>
        <p class="hoy-sub">${close.percent} % de lo registrado está revisado y conciliado${close.blockers && !close.ready ? ` · ${plural(close.blockers, "cosa impide", "cosas impiden")} cerrarlo` : ""}</p>
        <button type="button" class="btn-ghost hoy-state-act" data-go="cierre">${close.blockers ? "Ver qué bloquea" : "Ir al cierre"}</button>
      </section>`;
  }

  function fiscalCard() {
    const head = `<div class="card-head"><h2>Impuestos</h2>`;
    if (fiscal === "error") return `<section class="card hoy-state">${head}</div><p class="board-empty">No se pudo calcular la posición fiscal ahora.</p></section>`;
    const models = (fiscal?.models || []).filter((model) => model.status !== "FILED").sort((a, b) => a.days_left - b.days_left);
    if (!models.length) {
      return `<section class="card hoy-state">${head}</div><p class="hoy-figure">Al día</p><p class="hoy-sub">No hay modelos trimestrales pendientes de presentar.</p></section>`;
    }
    const [main, ...others] = models;
    const count = (type) => main.gaps.filter((gap) => gap.type === type).length;
    const rows = [main.approved_invoices ? { ok: true, label: plural(main.approved_invoices, "factura aprobada", "facturas aprobadas") }
                                         : { ok: null, label: "Ninguna factura aprobada en el trimestre" }];
    const unmatched = count("movimiento_sin_factura");
    if (main.model === "303") rows.push(unmatched ? { ok: null, label: `${plural(unmatched, "movimiento", "movimientos")} del banco sin factura` } : { ok: true, label: "Banco sin movimientos por justificar" });
    if (count("pendiente_revision")) rows.push({ ok: null, label: `${plural(count("pendiente_revision"), "factura", "facturas")} sin revisar` });
    if (count("factura_falta")) rows.push({ ok: null, label: `Faltan ${plural(count("factura_falta"), "factura habitual", "facturas habituales")}` });
    if (main.discrepancies.length) rows.push({ ok: false, label: plural(main.discrepancies.length, "discrepancia", "discrepancias") });
    const info = Math.round((main.information_available || 0) * 100);
    const complete = main.status === "COMPLETE";
    return `
      <section class="card hoy-state">
        ${head}<span class="card-sub">${esc(main.model)} · ${esc(main.period_label)} · vence el ${ddmm(main.due_date)}</span></div>
        <p class="hoy-figure">${money(main.result)} <span class="hoy-sub">${esc((main.outcome || "").toLowerCase())} · estimado</span></p>
        <span class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${info}" aria-label="Información disponible"><span style="width:${info}%"></span></span>
        <p class="hoy-sub">${info} % de la información disponible</p>
        <ul class="hoy-checks">${rows.map((row) => `<li>${mark(row.ok)}<span>${esc(row.label)}</span></li>`).join("")}</ul>
        ${main.result_with_pending !== null && main.result_with_pending !== undefined && Math.abs(main.result_with_pending - main.result) >= 0.01
          ? `<p class="hoy-line"><span class="hoy-label">Si apruebas lo pendiente</span><span>${money(main.result_with_pending)} ${main.result_with_pending > 0 ? "a ingresar" : main.result_with_pending < 0 ? "a compensar" : ""}</span></p>` : ""}
        ${others.map((model) => `<p class="hoy-sub">${esc(model.model)} · ${esc(model.period_label)}: ${money(model.result)} · ${Math.round((model.information_available || 0) * 100)} % de información</p>`).join("")}
        <button type="button" class="btn-ghost hoy-state-act" data-go="impuestos">${complete ? "Ver la posición" : "Ver qué falta"}</button>
      </section>`;
  }

  function treasuryCard() {
    const head = `<div class="card-head"><h2>Tesorería</h2><span class="card-sub">próximos 30 días</span></div>`;
    if (treasury === "error") return `<section class="card hoy-state">${head}<p class="board-empty">No se pudo calcular la previsión ahora.</p></section>`;
    if (!treasury || treasury.current_balance === null || treasury.current_balance === undefined) {
      return `
        <section class="card hoy-state">${head}
          <p class="hoy-sub">Sin saldo del banco no se puede prever la caja. Importa un extracto con saldo o conecta el banco.</p>
          <button type="button" class="btn-ghost hoy-state-act" data-go="negocio" data-anchor="bankCard">Importar extracto</button>
        </section>`;
    }
    const totals = {};
    for (const movement of treasury.movements || []) {
      const entry = totals[movement.type] || (totals[movement.type] = { count: 0, amount: 0 });
      entry.count += 1;
      entry.amount += movement.amount;
    }
    const reasons = Object.entries(FLOW).filter(([type]) => totals[type]).map(([type, [sign, label]]) =>
      `<li><span class="hoy-sign">${sign}</span><span>${totals[type].count} ${esc(label)} · ${money(totals[type].amount)}</span></li>`);
    const risk = treasury.risk || {};
    const listed = group("accion").items.find((item) => item.kind === "liquidity");
    const tension = ["alto", "medio"].includes(risk.level)
      ? `<li class="hoy-risk">${mark(false)}<span>Tensión el ${ddmm(risk.date)}: la caja bajaría a ${signed(treasury.lowest_point?.balance ?? 0)}</span></li>`
      : listed ? `<li class="hoy-risk">${mark(null)}<span>${esc(listed.title)} (más allá de 30 días)</span></li>`
      : `<li>${mark(true)}<span>Sin tensión: ${treasury.cushion ? `no baja del colchón de ${money(treasury.cushion)}` : "la caja no baja de cero"}</span></li>`;
    const confidence = treasury.confidence || {};
    return `
      <section class="card hoy-state">${head}
        <p class="hoy-figure">${signed(treasury.projected_balance ?? treasury.current_balance)} <span class="hoy-sub">saldo previsto</span></p>
        <p class="hoy-sub">Hoy ${signed(treasury.current_balance)}${treasury.balance_date ? ` (extracto del ${ddmm(treasury.balance_date)})` : ""} · confianza ${esc(confidence.level || "—")}: ${esc(confidence.why || "")}</p>
        ${reasons.length ? `<ul class="hoy-checks hoy-flows">${reasons.join("")}</ul>` : `<p class="hoy-sub">Sin cobros ni pagos previstos en estos 30 días.</p>`}
        <ul class="hoy-checks">${tension}</ul>
        <button type="button" class="btn-ghost hoy-state-act" data-go="negocio" data-anchor="cashflowCard">Ver previsión</button>
      </section>`;
  }

  /* ---------- Lo que CapaFiscal ya ha hecho ---------- */

  function doneHtml() {
    const solved = group("resuelto");
    const doing = group("haciendo").items.filter((item) => item.kind !== "tax");  // los impuestos ya tienen su bloque
    const night = data.overnight || { items: [] };
    const pulse = data.pulse || {};
    let thirty = "";
    if (metrics) {
      const done = metrics.finished;
      const parts = [[done.invoices, "factura(s) pagadas o cobradas"], [done.movements, "movimiento(s) conciliados o justificados"],
        [done.cases, "expediente(s) cerrados"], [done.months, "mes(es) cerrados"]].filter(([count]) => count).map(([count, label]) => `${count} ${label}`);
      const errors = metrics.errors_found.anomalies + metrics.errors_found.duplicates + metrics.errors_found.invalid;
      thirty = done.total
        ? `<p class="work-night" title="${esc(metrics.definition)}">En ${metrics.days} días: <strong>${done.total}</strong> trabajos terminados (${esc(parts.join(" · "))})${errors ? ` · ${plural(errors, "error detectado", "errores detectados")}` : ""}.</p>`
        : "";
    }
    return `
      <section class="card work-group state-done" aria-labelledby="hoy-done">
        <div class="card-head"><h2 class="work-group-title" id="hoy-done"><span class="board-dot"></span>CapaFiscal lo ha resuelto</h2><span class="card-sub">últimos ${metrics?.days || 7} días</span></div>
        ${resolvedCount()
          ? `<p class="hoy-figure">${plural(resolvedCount(), "trabajo resuelto", "trabajos resueltos")} sin que tuvieras que intervenir</p>`
          : `<p class="board-empty">Aún no ha terminado trabajos por su cuenta. Lo hará en cuanto entren facturas, extractos o notificaciones.</p>`}
        ${solved.items.length
          ? `<ul class="board-counts">${solved.items.map((item) => `<li><strong>${item.count}</strong> ${esc(item.title.replace(/^\d+\s/, ""))} (7 días)</li>`).join("")}</ul>
             ${data.time_saved?.minutes ? `<p class="board-empty" title="${esc(data.time_saved.note)}">≈ ${String(data.time_saved.hours).replace(".", ",")} h de trabajo ahorradas (estimación).</p>` : ""}` : ""}
        ${night.items.length ? `<p class="work-night">Últimas 24 h: ${night.items.map((line) => `<strong>${line.count}</strong> ${esc(line.label)}`).join(" · ")}</p>` : ""}
        ${thirty}
        ${doing.length ? `
          <div class="hoy-doing state-blue">
            <span class="hoy-label"><span class="board-dot"></span>Ahora está haciendo</span>
            <ol class="work-list">${doing.map(compactHtml).join("")}</ol>
          </div>` : ""}
        <div class="hoy-foot">
          <span class="board-pulse ${pulse.healthy ? "is-ok" : "is-late"}"><span class="pulse-dot"></span>${esc(pulse.label || "")}</span>
          ${data.learning ? `<span class="board-empty">${esc(data.learning.text)}</span>` : ""}
          <button type="button" class="btn-ghost" id="workRun"><svg class="icon"><use href="#i-play"/></svg> Trabajar ahora</button>
        </div>
      </section>`;
  }

  function render() {
    const today = new Date().toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "long" });
    document.getElementById("greetingLine").textContent = data.greeting;
    document.getElementById("panelPeriodNote").textContent = `${today.charAt(0).toUpperCase()}${today.slice(1)}`;
    document.getElementById("hoySummary").innerHTML = summaryHtml();
    const decide = group("accion").items;
    const missing = lacking();
    const parts = [];
    if (decide.length) parts.push(listSection("accion", "state-red", "Hoy deberías revisar", decide.length > TOP ? `${TOP} de ${decide.length}, por impacto` : "por impacto", decide, decisionHtml));
    if (missing.length) parts.push(listSection("falta", "state-orange", "Falta información", "bloquea trabajo o cierre", missing, compactHtml));
    if (!decide.length && !missing.length) parts.push(calmHtml());
    parts.push(`<h2 class="hoy-heading">Estado de la empresa</h2><div class="hoy-state-grid">${closeCard()}${fiscalCard()}${treasuryCard()}</div>`);
    parts.push(doneHtml());
    document.getElementById("workGroups").innerHTML = parts.join("");
  }

  async function load() {
    try {
      const [work, workMetrics, position, cash] = await Promise.all([
        window.apiRequest("/work"),
        window.apiRequest("/work/metrics").catch(() => null),
        window.apiRequest("/taxes/position").catch(() => "error"),
        window.apiRequest("/treasury?horizon=30").catch(() => "error"),
      ]);
      [data, metrics, fiscal, treasury] = [work, workMetrics, position, cash];
      render();
    } catch (error) {
      document.getElementById("hoySummary").innerHTML = "";
      document.getElementById("workGroups").innerHTML = `<section class="card"><p class="danger-text">No se pudo cargar «Hoy»: ${esc(error.message)}</p><button type="button" class="btn-ghost" data-work-retry>Reintentar</button></section>`;
    }
  }

  function schedule(active) {
    window.clearInterval(timer);
    timer = active ? window.setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS) : null;
  }

  async function runNow(button) {
    button.disabled = true;
    try {
      const result = await window.jsonRequest("/agents/pulse/run", "POST", {});
      window.showMessage(`Ciclo completado: ${result.items} elemento(s) trabajados.`, "success");
      window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));  // recarga esta lista y las demás pantallas
    } catch (error) {
      window.showMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function simulate(button) {
    const box = button.nextElementSibling;
    button.disabled = true;
    box.innerHTML = `<p class="board-empty">Calculando…</p>`;
    try {
      const result = await window.jsonRequest("/simulate", "POST", JSON.parse(button.dataset.workSimulate));
      box.innerHTML = `<ul>${result.effects.map((text) => `<li>${esc(text)}</li>`).join("")}</ul><p class="board-empty">${esc(result.note)}</p>`;
    } catch (error) {
      box.innerHTML = `<p class="danger-text">${esc(error.message)}</p>`;
    } finally {
      button.disabled = false;
    }
  }

  function setup() {
    const section = document.getElementById("tab-panel");
    if (!section || !document.getElementById("workGroups")) return;
    section.addEventListener("click", (event) => {
      const run = event.target.closest("#workRun");
      if (run) return runNow(run);
      if (event.target.closest("[data-work-retry]")) return load();
      const sim = event.target.closest("[data-work-simulate]");
      if (sim) return simulate(sim);
      const more = event.target.closest("[data-work-more]");
      if (more) { expanded.add(more.dataset.workMore); return render(); }
      const caseButton = event.target.closest("[data-work-case]");
      if (caseButton) return window.openCase(Number(caseButton.dataset.workCase));
      const doc = event.target.closest("[data-work-document]");
      if (doc) return window.showDetail(Number(doc.dataset.workDocument));
      const go = event.target.closest("[data-go]");
      if (go) {
        window.activateTab(go.dataset.go);
        if (go.dataset.anchor) window.setTimeout(() => document.getElementById(go.dataset.anchor)?.scrollIntoView({ block: "start" }), 300);
        if (go.dataset.view) window.setTimeout(() => document.querySelector(`.tab-panel.active .segmented [data-view="${go.dataset.view}"]`)?.click(), 120);
      }
    });
    load();
    schedule(section.classList.contains("active"));
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    const active = event.detail?.tab === "panel";
    schedule(active);
    if (active) load();
  });
  window.addEventListener("capafiscal:data-changed", () => {
    if (document.getElementById("tab-panel")?.classList.contains("active")) load();
  });
  // Teclado: j / k recorren las acciones y Enter abre la que tiene el foco.
  document.addEventListener("keydown", (event) => {
    if (!["j", "k"].includes(event.key) || event.ctrlKey || event.metaKey || event.altKey) return;
    if (!document.getElementById("tab-panel")?.classList.contains("active")) return;
    if (event.target.closest("input, textarea, select, [contenteditable], dialog[open]") || document.querySelector("dialog[open]")) return;
    const buttons = [...document.querySelectorAll("#workGroups .work-act")];
    if (!buttons.length) return;
    event.preventDefault();
    const current = buttons.indexOf(document.activeElement);
    const next = event.key === "j" ? Math.min(buttons.length - 1, current + 1) : Math.max(0, current < 0 ? 0 : current - 1);
    buttons[next].focus();
    buttons[next].closest(".work-item")?.scrollIntoView({ block: "nearest" });
  });
  window.loadWorkCenter = load;
  document.addEventListener("DOMContentLoaded", setup);
})();
