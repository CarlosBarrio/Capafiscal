"use strict";

/* Centro de trabajo (Hoy): una sola lista. Decides tú / falta información / CapaFiscal lo está haciendo / resuelto. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const GROUP_CLASS = { accion: "state-red", falta: "state-orange", haciendo: "state-green", resuelto: "state-done" };
  const EMPTY = {
    accion: "Nada que decidir ahora.",
    falta: "No falta ningún dato ni documento.",
    haciendo: "Nada en marcha en este momento.",
    resuelto: "Aún no hay trabajo resuelto esta semana.",
  };
  const VISIBLE = 8;
  const REFRESH_MS = 60000;
  let data = null;
  let expanded = new Set();
  let timer = null;

  function money(value) {
    return value === null || value === undefined ? "" : window.formatMoney(Math.abs(value));
  }

  function mark(ok) {
    return ok === true ? `<span class="work-check is-ok" aria-label="Correcto">✓</span>`
      : ok === false ? `<span class="work-check is-block" aria-label="No">✕</span>`
      : `<span class="work-check is-warn" aria-label="Aproximado">~</span>`;
  }

  function actionAttrs(action) {
    if (!action) return "";
    if (action.case_id) return `data-work-case="${Number(action.case_id)}"`;
    if (action.document_id) return `data-work-document="${Number(action.document_id)}"`;
    return `data-go="${esc(action.tab || "panel")}" data-anchor="${esc(action.anchor || "")}" data-view="${esc(action.view || "")}"`;
  }

  function itemHtml(item) {
    const checks = item.checked || [];
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
        ${item.simulate ? `<button type="button" class="link-button work-sim-link" data-work-simulate='${esc(JSON.stringify(item.simulate))}'>${esc(item.simulate_label)}</button><div class="work-sim" aria-live="polite"></div>` : ""}
        ${checks.length ? `
          <details class="work-checked">
            <summary>Qué ha comprobado CapaFiscal</summary>
            <ul>${checks.map((check) => `<li>${mark(check.ok)}<span>${esc(check.label)}</span></li>`).join("")}</ul>
          </details>` : ""}
      </li>`;
  }

  function groupHtml(group) {
    const cls = GROUP_CLASS[group.key] || "";
    const head = `<div class="card-head"><h2 class="work-group-title"><span class="board-dot"></span>${esc(group.label)}</h2><span class="card-sub">${group.count}</span></div>`;
    if (group.key === "resuelto") {
      return `<section class="card work-group ${cls}">${head}${group.items.length
        ? `<ul class="board-counts">${group.items.map((item) => `<li><strong>${item.count}</strong> ${esc(item.title.replace(/^\d+\s/, ""))}</li>`).join("")}</ul>
           <p class="board-empty">Últimos 7 días, sin que nadie tuviera que intervenir.${data.time_saved?.minutes ? ` <span title="${esc(data.time_saved.note)}">≈ ${String(data.time_saved.hours).replace(".", ",")} h ahorradas (estimación).</span>` : ""}</p>`
        : `<p class="board-empty">${EMPTY.resuelto}</p>`}</section>`;
    }
    if (!group.items.length) {
      return `<section class="card work-group ${cls}">${head}<p class="board-empty">${EMPTY[group.key]}</p></section>`;
    }
    const open = expanded.has(group.key);
    const shown = open ? group.items : group.items.slice(0, VISIBLE);
    const rest = group.items.length - shown.length;
    return `
      <section class="card work-group ${cls}">
        ${head}
        <ol class="work-list">${shown.map(itemHtml).join("")}</ol>
        ${rest > 0 ? `<button type="button" class="link-button work-more" data-work-more="${esc(group.key)}">Ver ${rest} más</button>` : ""}
      </section>`;
  }

  function statusHtml() {
    const pulse = data.pulse || {};
    const night = data.overnight || { items: [] };
    const close = data.close;
    const work = night.items.length
      ? `En las últimas 24 h: ${night.items.map((line) => `<strong>${line.count}</strong> ${esc(line.label)}`).join(" · ")}`
      : "Sin trabajo nuevo en las últimas 24 h.";
    return `
      <div class="work-status-row">
        <span class="board-pulse ${pulse.healthy ? "is-ok" : "is-late"}" title="${esc((pulse.triggers || []).map((item) => `${item.label}: ${item.events_today} hoy`).join(" · "))}"><span class="pulse-dot"></span>${esc(pulse.label || "")}</span>
      </div>
      <p class="work-night">${work}</p>
      ${data.learning ? `<p class="work-night">${esc(data.learning.text)}</p>` : ""}
      ${close ? `
        <button type="button" class="work-close" data-go="cierre">
          <span class="work-close-text"><strong>${esc(close.headline)}</strong><span class="muted">Ir al cierre ${window.icon("chevron")}</span></span>
          <span class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${close.percent}" aria-label="Cierre de ${esc(close.label)}"><span style="width:${close.percent}%"></span></span>
        </button>` : ""}`;
  }

  function render() {
    const today = new Date().toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "long" });
    document.getElementById("greetingLine").textContent = data.greeting;
    document.getElementById("panelPeriodNote").textContent = `${today.charAt(0).toUpperCase()}${today.slice(1)} · ${data.headline}.`;
    document.getElementById("workStatus").innerHTML = statusHtml();
    document.getElementById("workGroups").innerHTML = data.groups.map(groupHtml).join("");
  }

  async function load() {
    try {
      data = await window.apiRequest("/work");
      render();
    } catch (error) {
      document.getElementById("workGroups").innerHTML = `<section class="card"><p class="danger-text">No se pudo cargar el centro de trabajo: ${esc(error.message)}</p><button type="button" class="btn-ghost" data-work-retry>Reintentar</button></section>`;
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
        if (go.dataset.view) window.setTimeout(() => document.querySelector(`#caseViews [data-view="${go.dataset.view}"]`)?.click(), 120);
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
  // Teclado: j / k recorren la lista y Enter abre la acción del elemento con el foco.
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
