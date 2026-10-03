"use strict";

/* Hoy: un centro de decisión, no un cuadro de mando.
   Orden: 1) lo que tienes que decidir  2) lo que falta  3) estado (cierre, impuestos, caja)
          4) lo que CapaFiscal ya ha resuelto.
   Cada decisión explica qué pasa, por qué importa, qué ha comprobado CapaFiscal, qué propone y qué te toca.
   Solo lee: /work, /work/metrics, /taxes/position y /treasury. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const TOP = 5;  // «Para ti»: pocas cosas, por impacto
  const REFRESH_MS = 60000;
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

  /* «Requerimiento de información de Agencia Tributaria. Pide: …» bajo el título «Requerimiento de información · …»:
     la primera frase repite el título y se quita. */
  function withoutTitle(why, title) {
    const head = (title || "").split(" · ")[0].toLowerCase();
    const cut = why.indexOf(". ");
    return head && cut > 0 && why.toLowerCase().startsWith(head) ? why.slice(cut + 2) : why;
  }

  /* «vence en 2 día(s) · requerimiento: no atenderlo… · 640,00 € en juego»: solo el plazo y el riesgo; lo demás ya
     está en la frase o en el importe. */
  function meansOf(item) {
    const parts = (item.means || "").split(" · ").filter((part) => /vence|venció|riesgo|plazo/i.test(part) && !/en juego/.test(part));
    return parts.join(" · ");
  }

  /* Una decisión: qué pasa, importe y la acción. Lo comprobado y la propuesta se abren bajo demanda. */
  function decisionHtml(item, first) {
    const liquidity = item.kind === "liquidity";
    const checks = liquidity ? [] : (item.checked || []);
    const proposal = item.proposal || (liquidity && item.checked?.length ? item.checked[0].label : null);
    const label = checks.length ? `${checks.length} ${checks.length === 1 ? "comprobación" : "comprobaciones"}` : "Propuesta";
    const detail = checks.length || proposal || item.secondary || item.simulate;
    return `
      <li class="work-item hoy-decision">
        <div class="work-item-main">
          <div class="work-item-text">
            <strong>${esc(item.title)}</strong>
            <span class="work-why hoy-clamp">${esc(withoutTitle(item.why || "", item.title))}</span>
            ${meansOf(item) ? `<span class="hoy-means">${esc(meansOf(item))}</span>` : ""}
          </div>
          <div class="work-item-side">
            ${item.amount !== null && item.amount !== undefined ? `<span class="work-amount">${money(item.amount)}</span>` : ""}
            ${item.action ? `<button type="button" class="${first ? "act-btn act-primary" : "btn-ghost"} work-act" ${actionAttrs(item.action)}>${esc(item.action.label)}</button>` : ""}
          </div>
        </div>
        ${detail ? `
          <details class="work-checked">
            <summary>${esc(label)}</summary>
            ${checks.length ? `<ul class="hoy-checks">${checks.map((check) => `<li>${mark(check.ok)}<span>${esc(check.label)}</span></li>`).join("")}</ul>` : ""}
            ${proposal ? `<p class="hoy-proposal">Propuesta de CapaFiscal: ${esc(proposal)}</p>` : ""}
            ${item.secondary ? `<button type="button" class="link-button" ${actionAttrs(item.secondary)}>${esc(item.secondary.label)}</button>` : ""}
            ${item.simulate ? `<button type="button" class="link-button work-sim-link" data-work-simulate='${esc(JSON.stringify(item.simulate))}'>${esc(item.simulate_label)}</button><div class="work-sim" aria-live="polite"></div>` : ""}
          </details>` : ""}
      </li>`;
  }

  /* Un bloqueo: qué falta, en una línea, y el botón para resolverlo. */
  function compactHtml(item) {
    return `
      <li class="work-item">
        <div class="work-item-main">
          <div class="work-item-text">
            <strong>${esc(item.title)}</strong>
            <span class="work-why hoy-clamp-1">${esc(item.why)}</span>
          </div>
          <div class="work-item-side">
            ${item.amount !== null && item.amount !== undefined ? `<span class="work-amount">${money(item.amount)}</span>` : ""}
            ${item.action ? `<button type="button" class="btn-ghost work-act" ${actionAttrs(item.action)}>${esc(item.action.label)}</button>` : ""}
          </div>
        </div>
      </li>`;
  }

  function listSection(key, cls, title, items, render) {
    const open = expanded.has(key);
    const shown = open ? items : items.slice(0, TOP);
    const rest = items.length - shown.length;
    return `
      <section class="card work-group ${cls}" aria-labelledby="hoy-${key}">
        <div class="card-head"><h2 class="work-group-title" id="hoy-${key}"><span class="board-dot"></span>${esc(title)}</h2><span class="card-sub">${items.length}</span></div>
        <ol class="work-list">${shown.map((item, index) => render(item, index === 0)).join("")}</ol>
        ${rest > 0 ? `<button type="button" class="link-button work-more" data-work-more="${key}">Ver ${rest} más</button>` : ""}
      </section>`;
  }

  function group(key) {
    return data.groups.find((item) => item.key === key) || { items: [], count: 0 };
  }

  function lacking() {
    return group("falta").items.filter((item) => item.kind !== "tax");  // los impuestos están en Situación
  }

  function summaryHtml() {
    const decide = group("accion").items.length;
    const missing = lacking().length;
    if (!decide && !missing) return `<li class="state-done"><span class="board-dot"></span>Nada pendiente</li>`;
    return [decide ? `<li class="state-red"><span class="board-dot"></span><strong>${decide}</strong> por revisar</li>` : "",
            missing ? `<li class="state-orange"><span class="board-dot"></span><strong>${missing}</strong> ${missing === 1 ? "pendiente" : "pendientes"}</li>` : ""].join("");
  }

  function calmHtml() {
    return `
      <section class="card work-group state-done hoy-calm">
        <div class="card-head"><h2 class="work-group-title"><span class="board-dot"></span>Todo al día</h2></div>
        <p class="board-empty">CapaFiscal sigue vigilando facturas, banco y plazos. Si aparece algo, estará aquí.</p>
      </section>`;
  }

  /* ---------- Situación: tres filas, el detalle en su pantalla ---------- */

  function row(go, label, value, sub, tone = "", anchor = "") {
    return `
      <li>
        <button type="button" class="hoy-row" data-go="${go}" data-anchor="${anchor}">
          <span class="hoy-row-label">${esc(label)}</span>
          <span class="hoy-row-value ${tone}">${esc(value)}</span>
          <span class="hoy-row-sub">${esc(sub)}</span>
          ${window.icon("chevron")}
        </button>
      </li>`;
  }

  function closeRow() {
    const close = data.close;
    if (!close) return "";
    const value = close.ready ? "Listo para cerrar" : close.percent >= 100 && close.blockers ? `Listo salvo ${plural(close.blockers, "bloqueo", "bloqueos")}`
      : close.blockers ? plural(close.blockers, "bloqueo", "bloqueos") : `${close.percent} % revisado`;
    return row("cierre", `Cierre ${close.label.toLowerCase()}`, value, close.ready ? "Todo revisado y conciliado" : `${close.percent} % revisado y conciliado`,
               close.ready ? "is-ok" : close.blockers ? "is-warn" : "");
  }

  function fiscalRow() {
    if (fiscal === "error") return row("impuestos", "Impuestos", "No disponible", "No se pudo calcular ahora");
    const models = (fiscal?.models || []).filter((model) => model.status !== "FILED").sort((a, b) => a.days_left - b.days_left);
    if (!models.length) return row("impuestos", "Impuestos", "Al día", "Nada pendiente de presentar", "is-ok");
    const main = models[0];
    const pending = main.gaps.length + main.discrepancies.length;
    const result = main.result > 0 ? `${money(main.result)} a ingresar` : main.result < 0 ? `${money(main.result)} a compensar` : "Sin resultado";
    return row("impuestos", `Impuestos ${main.model} · ${main.period_label}`, result,
               `${pending ? plural(pending, "revisión pendiente", "revisiones pendientes") : "Cálculo completo"} · vence el ${ddmm(main.due_date)}`, pending ? "" : "");
  }

  function treasuryRow() {
    if (treasury === "error") return row("negocio", "Tesorería 30 días", "No disponible", "No se pudo calcular ahora", "", "cashflowCard");
    if (!treasury || treasury.current_balance === null || treasury.current_balance === undefined) {
      return row("negocio", "Tesorería 30 días", "Sin conexión bancaria", "Conecta tu banco para activar la previsión", "", "bankCard");
    }
    const level = treasury.risk?.level;
    const value = level === "alto" ? "Riesgo alto" : level === "medio" ? "Riesgo medio" : "Sin riesgo";
    const sub = ["alto", "medio"].includes(level) ? `Tensión el ${ddmm(treasury.risk.date)} · saldo previsto ${signed(treasury.projected_balance)}`
      : `Saldo previsto ${signed(treasury.projected_balance ?? treasury.current_balance)} · confianza ${treasury.confidence?.level || "—"}`;
    return row("negocio", "Tesorería 30 días", value, sub, level === "alto" ? "is-block" : level === "medio" ? "is-warn" : "is-ok", "cashflowCard");
  }

  /* ---------- Trabajo realizado: una sola definición (la de /work/metrics) ---------- */

  function doneHtml() {
    const doing = group("haciendo").items.filter((item) => item.kind !== "tax");
    const night = data.overnight || { items: [] };
    const done = metrics?.finished;
    return `
      <section class="card work-group" aria-labelledby="hoy-done">
        <div class="card-head"><h2 class="work-group-title" id="hoy-done">Trabajo realizado</h2>
          <button type="button" class="link-button" id="workRun">Trabajar ahora</button></div>
        ${done && done.total
          ? `<p class="hoy-figure">${plural(done.total, "trabajo completado", "trabajos completados")}</p>
             <p class="hoy-sub" title="${esc(metrics.definition)}">Últimos ${metrics.days} días: facturas pagadas o cobradas, movimientos conciliados, expedientes y meses cerrados</p>`
          : `<p class="board-empty">Todavía no hay trabajo automático registrado.</p>`}
        ${night.items.length ? `<p class="hoy-sub">Últimas 24 h: ${night.items.map((line) => `${line.count} ${esc(line.label)}`).join(" · ")}</p>` : ""}
        ${doing.length ? `<ol class="work-list hoy-doing">${doing.map(compactHtml).join("")}</ol>` : ""}
      </section>`;
  }

  function statusPill() {
    const pulse = data.pulse || {};
    const text = !pulse.scheduler ? "Trabajo automático en pausa" : pulse.label || "";
    return text ? `<span class="board-pulse ${pulse.healthy ? "is-ok" : "is-late"}"><span class="pulse-dot"></span>${esc(text)}</span>` : "";
  }

  function render() {
    const today = new Date().toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "long" });
    document.getElementById("greetingLine").textContent = "Hoy";
    document.getElementById("panelPeriodNote").innerHTML = `${esc(today.charAt(0).toUpperCase() + today.slice(1))} ${statusPill()}`;
    document.getElementById("hoySummary").innerHTML = summaryHtml();
    const decide = group("accion").items;
    const missing = lacking();
    const parts = [];
    if (decide.length) parts.push(listSection("accion", "state-red", "Para ti", decide, decisionHtml));
    if (missing.length) parts.push(listSection("falta", "state-orange", "Bloqueos", missing, compactHtml));
    if (!decide.length && !missing.length) parts.push(calmHtml());
    parts.push(`
      <section class="card hoy-situation" aria-labelledby="hoy-situation">
        <div class="card-head"><h2 id="hoy-situation">Situación</h2></div>
        <ul class="hoy-rows">${closeRow()}${fiscalRow()}${treasuryRow()}</ul>
      </section>`);
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
      window.showMessage(`Ciclo completado: ${window.pl(result.items, "elemento(s) trabajado(s)")}.`, "success");
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
