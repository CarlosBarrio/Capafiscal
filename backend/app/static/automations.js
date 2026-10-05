"use strict";

/* Automatizaciones del agente y cierre trimestral para la gestoría. */
(() => {
  const esc = (value) => window.escapeHtml(value);

  const ICONS = {
    BANK_MATCH: "link",
    RECURRING_INVOICES: "repeat",
    DAILY_DIGEST: "sparkles",
    DUNNING: "coins",
    TIMESHEET_WATCH: "clock",
    PAYROLL_DRAFT: "wallet",
    ADVISOR_PACK: "archive",
  };
  const RUN_STATUS = {
    OK: ["Hecho", "status-success"],
    NOTHING: ["Sin cambios", "status-neutral"],
    ERROR: ["Error", "status-danger"],
    RUNNING: ["En curso", "status-info"],
  };

  let active = false;

  function when(value) {
    if (!value) return "Nunca";
    return window.formatDate(value);
  }

  async function load() {
    const row = document.getElementById("autoItems")?.closest(".kpi-row");
    let data;
    let digest;
    try {
      [data, digest] = await Promise.all([
        window.apiRequest("/automations"),
        window.apiRequest("/digest"),
      ]);
    } catch (error) {
      window.setFigures(row, "error", { what: "las automatizaciones", error, retry: () => { window.setFigures(row, "loading"); load().catch(() => {}); } });
      return;
    }
    // Si ninguna se ha ejecutado nunca, «0 tareas, 0 h, 0 €» no informa: se dice y se deja el estado del planificador.
    const neverRan = !data.automations.some((item) => item.last_status || item.last_run_at);
    window.setFigures(row, neverRan ? "empty" : "ready", {
      keep: ["autoScheduler"],
      empty: "Aún no se ha ejecutado ninguna automatización. Las tareas resueltas y el tiempo ahorrado aparecerán tras la primera ejecución.",
    });

    document.getElementById("autoItems").textContent = data.month.items;
    document.getElementById("autoRuns").textContent = `${window.pl(data.month.runs, "ejecución(es)")} este mes`;
    document.getElementById("autoHours").textContent = `${String(data.month.hours_saved).replace(".", ",")} h`;
    document.getElementById("autoCost").textContent = window.formatMoney(data.month.cost_saved);
    document.getElementById("autoScheduler").textContent = data.scheduler.running ? "Activo" : "Parado";
    document.getElementById("autoSchedulerFoot").textContent = data.scheduler.running
      ? (data.scheduler.last_tick ? `última comprobación ${window.formatDate(data.scheduler.last_tick).split(",").pop().trim()}` : "comprueba cada minuto")
      : "ejecútalas a mano o reinicia el servidor";

    document.getElementById("automationGrid").innerHTML = data.automations.map((item) => {
      const [label, className] = RUN_STATUS[item.last_status] || ["Pendiente", "status-neutral"];
      return `
        <article class="automation-card ${item.enabled ? "" : "is-off"}">
          <header>
            <span class="automation-icon">${window.icon(ICONS[item.code] || "zap")}</span>
            <div class="automation-title">
              <h3>${esc(item.name)}</h3>
              <small>${esc(item.schedule)}</small>
            </div>
            <label class="switch" title="${item.enabled ? "Desactivar" : "Activar"}">
              <input type="checkbox" data-toggle="${esc(item.code)}" ${item.enabled ? "checked" : ""}>
              <span></span>
            </label>
          </header>
          <p>${esc(item.description)}</p>
          <div class="automation-last">
            <span class="status-pill mini ${className}">${esc(label)}</span>
            <span>${esc(item.last_summary || "Aún no se ha ejecutado.")}</span>
          </div>
          <footer>
            <small>${item.enabled ? `Próxima: ${esc(item.next_run)}` : "Desactivada"}${item.month_items ? ` · ${item.month_items} este mes` : ""}</small>
            <span class="automation-actions">
              <button type="button" class="btn-ghost" data-go="${esc(item.tab)}">Ver</button>
              <button type="button" class="btn-ghost" data-run="${esc(item.code)}">${window.icon("play")} Ejecutar ahora</button>
            </span>
          </footer>
        </article>
      `;
    }).join("");

    document.getElementById("automationRuns").innerHTML = data.recent.length
      ? data.recent.slice(0, 14).map((run) => {
        const [label, className] = RUN_STATUS[run.status] || RUN_STATUS.NOTHING;
        return `
          <div class="run-row">
            <span class="status-pill mini ${className}">${esc(label)}</span>
            <span class="run-main"><strong>${esc(run.name)}</strong><small>${esc(run.summary || "")}</small></span>
            <small class="muted">${esc(window.formatDate(run.started_at))}${run.trigger === "MANUAL" ? " · a mano" : ""}</small>
          </div>
        `;
      }).join("")
      : `<p class="empty-inline">Aún no hay ejecuciones. Pulsa «Ejecutar ahora» en cualquier automatización o espera a su hora.</p>`;

    document.getElementById("digestPreview").textContent = digest.text;
  }

  async function run(code, button) {
    button.disabled = true;
    try {
      const result = await window.jsonRequest(`/automations/${code}/run`, "POST", {});
      window.showMessage(result.summary || "Hecho.", result.status === "ERROR" ? "error" : "success");
      await load();
      window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));
    } catch (error) {
      window.showMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------------------
  CIERRE PARA LA GESTORÍA (pestaña Informes)
  ------------------------------------------------------------ */
  function setupAdvisor() {
    const quarterSelect = document.getElementById("advisorQuarter");
    const yearSelect = document.getElementById("advisorYear");
    if (!quarterSelect) return;

    const now = new Date();
    // Por defecto, el trimestre que toca presentar: el recién cerrado durante
    // el primer mes del trimestre siguiente y, si no, el trimestre en curso.
    let quarter = Math.floor(now.getMonth() / 3) + 1;
    let year = now.getFullYear();
    if (now.getMonth() % 3 === 0) {
      quarter -= 1;
      if (quarter === 0) { quarter = 4; year -= 1; }
    }

    quarterSelect.innerHTML = [1, 2, 3, 4].map((item) => `<option value="${item}" ${item === quarter ? "selected" : ""}>${item}T</option>`).join("");
    yearSelect.innerHTML = [now.getFullYear() - 1, now.getFullYear()].map((item) => `<option ${item === year ? "selected" : ""}>${item}</option>`).join("");

    document.getElementById("advisorForm").addEventListener("change", loadAdvisor);
    document.getElementById("advisorSend").addEventListener("click", async () => {
      try {
        const message = await window.jsonRequest(`/advisor/send?year=${yearSelect.value}&quarter=${quarterSelect.value}`, "POST", {});
        window.showMessage(
          message.to_email
            ? "Envío a la gestoría preparado en la bandeja de salida, con el ZIP adjunto."
            : "Envío preparado. Indica el email de tu gestoría (o añádelo en Mi empresa).",
          message.to_email ? "success" : "warning",
        );
        window.openOutboxMessage?.(message.id);
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
  }

  async function loadAdvisor() {
    const quarter = document.getElementById("advisorQuarter").value;
    const year = document.getElementById("advisorYear").value;
    document.getElementById("advisorZip").href = `/api/advisor/pack.zip?year=${year}&quarter=${quarter}`;
    try {
      const data = await window.apiRequest(`/advisor/summary?year=${year}&quarter=${quarter}`);
      // Un trimestre vacío no está «listo para enviar»: no hay nada que enviar.
      if (!data.issued && !data.received && !data.payroll_runs && !data.transactions && !data.issues.length) {
        document.getElementById("advisorSummary").innerHTML =
          `<p class="board-empty">Nada que enviar del ${quarter}T ${year} todavía: no hay facturas, nóminas ni movimientos del banco en el trimestre.</p>`;
        return;
      }
      document.getElementById("advisorSummary").innerHTML = `
        <div class="advisor-stats">
          <span><strong>${data.issued}</strong> ${data.issued === 1 ? "emitida" : "emitidas"}</span>
          <span><strong>${data.received}</strong> ${data.received === 1 ? "recibida" : "recibidas"}</span>
          <span><strong>${data.payroll_runs}</strong> ${data.payroll_runs === 1 ? "nómina" : "nóminas"}</span>
          <span><strong>${data.transactions}</strong> ${data.transactions === 1 ? "movimiento" : "movimientos"}</span>
        </div>
        ${data.issues.length
          ? data.issues.map((issue) => `<div class="report-warning">${window.icon("alert")}<span>${esc(issue)}</span></div>`).join("")
          : `<p class="advisor-ok">${window.icon("check")} Todo listo para enviar: sin incidencias.</p>`}
      `;
    } catch (error) {
      const summary = document.getElementById("advisorSummary");
      summary.innerHTML = window.loadErrorHtml("el resumen para la gestoría", error);
      summary.querySelector("[data-figures-retry]")?.addEventListener("click", () => loadAdvisor());
    }
  }

  function setup() {
    setupAdvisor();
    const section = document.getElementById("tab-automatizaciones");
    if (!section) return;

    section.addEventListener("change", async (event) => {
      const toggle = event.target.closest("[data-toggle]");
      if (!toggle) return;
      try {
        await window.jsonRequest(`/automations/${toggle.dataset.toggle}`, "PATCH", { enabled: toggle.checked });
        window.showMessage(toggle.checked ? "Automatización activada." : "Automatización desactivada.", "success");
        await load();
      } catch (error) {
        toggle.checked = !toggle.checked;
        window.showMessage(error.message, "error");
      }
    });

    section.addEventListener("click", (event) => {
      const runButton = event.target.closest("[data-run]");
      if (runButton) return run(runButton.dataset.run, runButton);
      const go = event.target.closest("[data-go]");
      if (go) window.activateTab(go.dataset.go);
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "automatizaciones";
    if (active) load().catch((error) => window.showMessage(error.message, "error"));
    if (event.detail?.tab === "informes") loadAdvisor();
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
