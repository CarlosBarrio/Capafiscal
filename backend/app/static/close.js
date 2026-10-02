"use strict";

/* Cierre del mes: CapaFiscal hace su parte y dice qué bloquea el cierre. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);
  const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
  const MARK = { ok: ["✓", "is-ok", "Correcto"], warn: ["!", "is-warn", "Aviso"], block: ["✕", "is-block", "Bloquea"] };

  let period = null;
  let current = null;
  let busy = false;

  function shift(value, delta) {
    const [year, month] = value.split("-").map(Number);
    const date = new Date(year, month - 1 + delta, 1);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
  }

  function monthName(value) {
    const [year, month] = value.split("-").map(Number);
    return `${MONTHS[month - 1]} ${year}`;
  }

  function ago(iso) {
    if (!iso) return null;
    const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (minutes < 1) return "ahora mismo";
    if (minutes < 60) return `hace ${minutes} min`;
    const hours = Math.round(minutes / 60);
    return hours < 24 ? `hace ${hours} h` : new Date(iso).toLocaleDateString("es-ES");
  }

  async function load() {
    const query = period ? `?period=${period}` : "";
    try {
      current = await window.apiRequest(`/close${query}`);
      period = current.period;
      render();
    } catch (error) {
      document.getElementById("closeCard").innerHTML = `<p class="danger-text">${esc(error.message)}</p>`;
    }
  }

  function render() {
    const data = current;
    const name = monthName(data.period);
    const short = name.split(" ")[0];
    document.getElementById("closeTitle").textContent = `Cierre de ${name}`;
    document.getElementById("closePeriodLabel").textContent = name;
    const closed = data.status === "CLOSED";
    const badge = document.getElementById("cntClose");
    if (badge) {
      badge.textContent = (data.to_resolve || []).length || data.blockers;
      badge.classList.toggle("hidden", closed || !data.blockers);
    }

    const SHORT = { recibidas: "Facturas recibidas", emitidas: "Facturas emitidas", extracto: "Banco", conciliacion: "Conciliación", pagos: "Pagos",
                    documentos: "Documentos", duplicados: "Duplicados", iva: "IVA", anomalias: "Anomalías", impuestos: "Impuestos", incidencias: "Notificaciones" };
    const checklist = `<ul class="close-checklist">${data.checks.map((item) => `<li class="is-${esc(item.status)}"><span aria-hidden="true">${MARK[item.status][0]}</span> ${esc(SHORT[item.key] || item.label)}</li>`).join("")}</ul>`;
    const resolve = (data.to_resolve || []).map((item, index) => `
      <li>
        <span class="close-resolve-number">${index + 1}</span>
        <span class="close-resolve-text">${esc(item.text)}</span>
        ${item.action ? `<button type="button" class="btn-ghost" ${item.action.document_id ? `data-close-document="${Number(item.action.document_id)}"` : item.action.case_id ? `data-close-case="${Number(item.action.case_id)}"`
          : `data-go="${esc(item.action.tab || "panel")}" data-anchor="${esc(item.action.anchor || "")}" data-view="${esc(item.action.view || "")}"`}>${esc(item.action.label || "Resolver")}</button>` : ""}
      </li>`).join("");
    const report = `<a class="btn-ghost" href="/api/close/${esc(data.period)}/report">${window.icon("download")} ${closed ? "Informe de cierre" : "Informe provisional"}</a>`;

    document.getElementById("closeCard").innerHTML = closed ? `
      <div class="close-done">
        <span class="close-done-mark" aria-hidden="true">✓</span>
        <div>
          <h2 class="close-headline">${esc(name.charAt(0).toUpperCase() + name.slice(1))} cerrado</h2>
          <p class="close-work">${data.closed ? `Por ${esc(data.closed.by || "—")} el ${esc(new Date(data.closed.at).toLocaleString("es-ES"))} · ${data.closed.percent} % resuelto${data.closed.blockers ? ` · ${data.closed.blockers} salvedad(es): «${esc(data.closed.note || "")}»` : ""}` : ""}</p>
        </div>
      </div>
      ${checklist}
      <div class="close-actions">${report}<button type="button" class="btn-ghost" data-close-action="reopen">Reabrir</button></div>
    ` : `
      <div class="close-head">
        <div>
          <p class="close-state"><span class="status-pill status-neutral">Abierto</span>
            ${data.last_run_at ? `<span class="muted">Última comprobación de CapaFiscal ${esc(ago(data.last_run_at))}</span>` : `<span class="muted">CapaFiscal aún no ha preparado este cierre</span>`}</p>
          <h2 class="close-headline">${esc(data.headline)}</h2>
        </div>
        <div class="close-actions">
          <button type="button" class="${data.last_run_at ? "btn-ghost" : "act-btn act-primary"}" data-close-action="run" ${busy ? "disabled" : ""}>${window.icon("play")} ${busy ? "Trabajando…" : `Preparar el cierre de ${esc(short)}`}</button>
          ${data.ready ? `<button type="button" class="act-btn act-primary" data-close-action="close">${window.icon("lock")} Cerrar ${esc(short)}</button>` : ""}
        </div>
      </div>
      <span class="meter close-meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${data.percent}" aria-label="Porcentaje cerrado"><span style="width:${data.percent}%"></span></span>
      ${checklist}
      ${resolve ? `
        <div class="close-resolve">
          <h3>Resolver ${data.to_resolve.length} ${data.to_resolve.length === 1 ? "bloqueo" : "bloqueos"} → cerrar ${esc(short)}</h3>
          <ol>${resolve}</ol>
          <div class="close-actions"><button type="button" class="link-button" data-close-action="close-note">Cerrar igualmente, con salvedades…</button>${report}</div>
        </div>` : `<div class="close-actions">${report}</div>`}
      <p class="close-formula muted">${data.units.done} de ${data.units.total} elementos resueltos (${esc(data.units.formula)}).</p>
      ${data.work ? `<p class="close-work muted">En la última preparación CapaFiscal concilió ${data.work.auto_matched} movimiento(s) con evidencia suficiente, abrió ${data.work.anomalies_created} anomalía(s) y cerró ${data.work.anomalies_closed}.</p>` : ""}
    `;

    document.getElementById("closeChecksSub").textContent = data.blockers
      ? `${data.blockers} bloquea(n) · ${data.warnings} aviso(s)`
      : data.warnings ? `Nada bloquea · ${data.warnings} aviso(s)` : "Todo correcto";
    document.getElementById("closeChecks").innerHTML = data.checks.map((item) => {
      const [symbol, className, label] = MARK[item.status];
      return `
        <li class="close-check ${className}">
          <span class="close-mark" aria-label="${label}">${symbol}</span>
          <div class="close-check-body">
            <div class="close-check-top"><strong>${esc(item.label)}</strong>
              ${item.action && item.status !== "ok" ? `<button type="button" class="link-button" data-go="${esc(item.action.tab)}" data-anchor="${esc(item.action.anchor || "")}" data-view="${esc(item.action.view || "")}">${esc(item.action.label)}</button>` : ""}</div>
            <span>${esc(item.detail)}</span>
            ${item.items.length && item.status !== "ok" ? `
              <details class="close-items"><summary>Ver ${item.items.length}</summary>
                <ul>${item.items.map((row) => `<li><span>${esc(row.label)}${row.why ? ` <small class="muted">· ${esc(row.why)}</small>` : ""}</span>${row.amount !== undefined ? `<span class="num">${money(row.amount)}</span>` : ""}</li>`).join("")}</ul>
              </details>` : ""}
          </div>
        </li>`;
    }).join("");

    const history = data.history || [];
    document.getElementById("closeHistoryCard").classList.toggle("hidden", !history.length);
    document.getElementById("closeHistory").innerHTML = history.length ? `
      <ul class="close-history">${history.map((row) => `
        <li><button type="button" class="link-button" data-period="${esc(row.period)}">${esc(row.label)}</button>
          <span class="status-pill mini ${row.status === "CLOSED" ? "status-success" : "status-neutral"}">${row.status === "CLOSED" ? "Cerrado" : "Abierto"}</span>
          <span class="muted">${row.percent ?? "—"} %${row.closed_by ? ` · ${esc(row.closed_by)}` : ""}${row.note ? " · con salvedades" : ""}</span></li>`).join("")}</ul>` : "";
  }

  async function act(action) {
    try {
      if (action === "run") {
        busy = true;
        render();
        current = { ...(await window.jsonRequest(`/close/${period}/run`, "POST", {})), history: current.history };
        window.showMessage(`Cierre preparado: ${current.percent} % · ${current.blockers ? `${current.blockers} bloqueo(s)` : "listo para cerrar"}.`, "success");
      } else if (action === "close" || action === "close-note") {
        let note = null;
        if (action === "close-note") {
          note = window.prompt(`Quedan ${current.blockers} bloqueo(s). Escribe por qué se cierra igualmente (queda registrado):`, "");
          if (note === null || !note.trim()) return;
        }
        await window.jsonRequest(`/close/${period}/close`, "POST", { note });
        window.showMessage(`${monthName(period)} cerrado.`, "success");
      } else if (action === "reopen") {
        if (!window.confirm(`¿Reabrir ${monthName(period)}?`)) return;
        await window.jsonRequest(`/close/${period}/reopen`, "POST", {});
      }
    } catch (error) {
      window.showMessage(error.message, "error");
    } finally {
      busy = false;
    }
    await load();
  }

  function setup() {
    const section = document.getElementById("tab-cierre");
    if (!section) return;
    document.getElementById("closePrev").addEventListener("click", () => { period = shift(period, -1); load(); });
    document.getElementById("closeNext").addEventListener("click", () => { period = shift(period, 1); load(); });
    section.addEventListener("click", (event) => {
      const action = event.target.closest("[data-close-action]");
      if (action) return act(action.dataset.closeAction);
      const other = event.target.closest("[data-period]");
      if (other) { period = other.dataset.period; return load(); }
      const doc = event.target.closest("[data-close-document]");
      if (doc) return window.showDetail(Number(doc.dataset.closeDocument));
      const caseButton = event.target.closest("[data-close-case]");
      if (caseButton) return window.openCase(Number(caseButton.dataset.closeCase));
      const go = event.target.closest("[data-go]");
      if (go) {
        window.activateTab(go.dataset.go);
        if (go.dataset.anchor) window.setTimeout(() => document.getElementById(go.dataset.anchor)?.scrollIntoView({ block: "start" }), 300);
        if (go.dataset.view) window.setTimeout(() => document.querySelector(`#caseViews [data-view="${go.dataset.view}"]`)?.click(), 120);
      }
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    if (event.detail?.tab === "cierre") load();
  });
  window.loadCloseState = load;
  window.setCloseFocus = (value) => { period = value; };  // la búsqueda universal abre un mes concreto
  document.addEventListener("DOMContentLoaded", setup);
})();
