"use strict";

(() => {
  let active = false;
  let currentRunId = null;
  let simulateTimer = null;

  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);

  const MONTHS = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
    "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
  ];

  const STATUS = {
    DRAFT: ["Borrador", "status-warning"],
    APPROVED: ["Aprobada", "status-info"],
    PAID: ["Pagada", "status-success"],
  };

  function setupForm() {
    const now = new Date();
    document.getElementById("payrollMonth").innerHTML = MONTHS
      .map((name, index) => `<option value="${index + 1}" ${index === now.getMonth() ? "selected" : ""}>${name}</option>`)
      .join("");
    document.getElementById("payrollYear").innerHTML = [now.getFullYear() - 1, now.getFullYear(), now.getFullYear() + 1]
      .map((year) => `<option ${year === now.getFullYear() ? "selected" : ""}>${year}</option>`)
      .join("");
  }

  async function loadRuns() {
    const data = await window.apiRequest("/payroll/runs");
    document.getElementById("payrollParams").textContent = `Parámetros: ${data.params}`;
    const body = document.querySelector("#payrollRunsTable tbody");

    if (!data.runs.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">Aún no hay nóminas. Añade tu equipo con salario y genera el primer borrador.</td></tr>`;
      document.getElementById("payrollRunCard").classList.add("hidden");
      return;
    }

    body.innerHTML = data.runs.map((run) => {
      const [label, className] = STATUS[run.status] || STATUS.DRAFT;
      return `
        <tr class="${run.id === currentRunId ? "row-selected" : ""}">
          <td><button type="button" class="link-button" data-open-run="${run.id}">${esc(run.period.charAt(0).toUpperCase() + run.period.slice(1))}</button></td>
          <td class="num">${run.totals.employees}</td>
          <td class="num">${money(run.totals.gross)}</td>
          <td class="num">${money(run.totals.net)}</td>
          <td class="num">${money(run.totals.company_cost)}</td>
          <td><span class="status-pill ${className}">${esc(label)}</span></td>
          <td class="actions-cell"><button type="button" class="btn-ghost" data-open-run="${run.id}">Abrir</button></td>
        </tr>
      `;
    }).join("");

    if (!currentRunId) currentRunId = data.runs[0].id;
    await loadRun(currentRunId);
  }

  async function loadRun(runId) {
    currentRunId = runId;
    const run = await window.apiRequest(`/payroll/runs/${runId}`);
    renderRun(run);
  }

  function renderRun(run) {
    const card = document.getElementById("payrollRunCard");
    card.classList.remove("hidden");
    const [label, className] = STATUS[run.status] || STATUS.DRAFT;
    const draft = run.status === "DRAFT";

    document.getElementById("payrollRunTitle").textContent =
      `Nómina de ${run.period}`;
    document.getElementById("payrollRunStatus").innerHTML =
      `<span class="status-pill ${className}">${esc(label)}</span>`;

    const actions = [];

    if (draft) {
      actions.push(`<button type="button" class="btn-ghost" data-run-action="recalculate">Recalcular con fichas actuales</button>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-run-action="approve">Aprobar nómina</button>`);
      actions.push(`<button type="button" class="btn-ghost danger-text" data-run-action="delete">Eliminar borrador</button>`);
    } else if (run.status === "APPROVED") {
      actions.push(`<a class="act-btn act-primary" href="/api/payroll/runs/${run.id}/sepa.xml">${window.icon("download")} Remesa SEPA para el banco</a>`);
      actions.push(`<button type="button" class="btn-ghost" data-run-action="paid">Marcar como pagada</button>`);
      actions.push(`<button type="button" class="btn-ghost" data-run-action="reopen">Volver a borrador</button>`);
    }

    if (!draft) {
      actions.push(`<button type="button" class="btn-ghost" data-run-action="email">${window.icon("mail")} Enviar recibos por email</button>`);
    }
    actions.push(`<a class="btn-ghost" href="/api/payroll/runs/${run.id}/payslips.pdf" target="_blank" rel="noopener noreferrer">${window.icon("doc")} Recibos PDF</a>`);
    actions.push(`<a class="btn-ghost" href="/api/payroll/runs/${run.id}/summary.xlsx">${window.icon("download")} Resumen Excel</a>`);

    document.getElementById("payrollRunActions").innerHTML = actions.join("");

    const warnings = [];
    const missingIban = run.payslips.filter((item) => !item.iban_ok).map((item) => item.employee_name);

    if (!run.company_iban_ok) {
      warnings.push("Indica el IBAN de la empresa en Mi empresa para generar la remesa SEPA.");
    }
    if (missingIban.length) {
      warnings.push(`Sin IBAN válido (no entrarán en la remesa): ${missingIban.join(", ")}.`);
    }
    if (run.payslips.some((item) => item.lines?.irpf_source === "estimado")) {
      warnings.push("Hay retenciones de IRPF estimadas: confírmalas con el cálculo oficial de la AEAT e indícalas en cada ficha.");
    }
    if (draft) {
      warnings.push("Revisa horas extra, incentivos y anticipos de cada persona y aprueba la nómina.");
    }

    document.getElementById("payrollRunWarnings").innerHTML = warnings
      .map((text) => `<div class="report-warning">${window.icon("alert")}<span>${esc(text)}</span></div>`)
      .join("");

    const input = (payslip, field) => draft
      ? `<input type="number" class="cell-input" min="0" step="0.01" value="${payslip[field] || ""}" placeholder="0" data-payslip="${payslip.id}" data-field="${field}" aria-label="${field}">`
      : money(payslip[field]);

    document.querySelector("#payslipTable tbody").innerHTML = run.payslips.map((payslip) => `
      <tr>
        <td>
          <strong>${esc(payslip.employee_name)}</strong>
          <small class="muted block">${esc(payslip.job_title || "")}${payslip.irpf_rate ? ` · IRPF ${String(payslip.irpf_rate).replace(".", ",")} %` : ""}</small>
        </td>
        <td class="num">${payslip.lines?.days ?? 30}</td>
        <td class="num">${input(payslip, "overtime")}</td>
        <td class="num">${input(payslip, "bonus")}</td>
        <td class="num">${input(payslip, "advance")}</td>
        <td class="num">${money(payslip.gross)}</td>
        <td class="num">${money(payslip.ss_employee)}</td>
        <td class="num">${money(payslip.irpf)}</td>
        <td class="num"><strong>${money(payslip.net)}</strong></td>
        <td class="num">${money(payslip.company_cost)}</td>
        <td class="actions-cell"><a class="btn-ghost" href="/api/payroll/payslips/${payslip.id}/pdf" target="_blank" rel="noopener noreferrer">Recibo</a></td>
      </tr>
    `).join("");

    const totals = run.totals;
    document.querySelector("#payslipTable tfoot").innerHTML = `
      <tr class="total-row">
        <td>Total · ${totals.employees} persona(s)</td>
        <td></td><td></td><td></td><td></td>
        <td class="num">${money(totals.gross)}</td>
        <td class="num">${money(totals.ss_employee)}</td>
        <td class="num">${money(totals.irpf)}</td>
        <td class="num">${money(totals.net)}</td>
        <td class="num">${money(totals.company_cost)}</td>
        <td></td>
      </tr>
    `;

    document.querySelector("#journalTable tbody").innerHTML = run.journal.map((line) => `
      <tr>
        <td><span class="box-number">${esc(line.account)}</span></td>
        <td>${esc(line.name)}</td>
        <td class="num">${line.debit ? money(line.debit) : ""}</td>
        <td class="num">${line.credit ? money(line.credit) : ""}</td>
      </tr>
    `).join("");

    document.querySelectorAll("#payrollRunsTable tbody tr").forEach((row) => {
      row.classList.toggle("row-selected", Boolean(row.querySelector(`[data-open-run="${run.id}"]`)));
    });
  }

  async function runAction(action) {
    try {
      if (action === "recalculate") {
        renderRun(await window.apiRequest(`/payroll/runs/${currentRunId}/recalculate`, { method: "POST" }));
        window.showMessage("Nómina recalculada con los datos actuales del equipo.", "success");
      } else if (action === "approve") {
        if (!window.confirm("¿Aprobar la nómina? Después no se podrán cambiar las variables sin volver a borrador.")) return;
        await window.jsonRequest(`/payroll/runs/${currentRunId}/status`, "POST", { status: "APPROVED" });
        window.showMessage("Nómina aprobada: ya puedes descargar la remesa SEPA y los recibos.", "success");
      } else if (action === "paid") {
        await window.jsonRequest(`/payroll/runs/${currentRunId}/status`, "POST", { status: "PAID" });
        window.showMessage("Nómina marcada como pagada.", "success");
      } else if (action === "reopen") {
        await window.jsonRequest(`/payroll/runs/${currentRunId}/status`, "POST", { status: "DRAFT" });
      } else if (action === "email") {
        const result = await window.jsonRequest(`/payroll/runs/${currentRunId}/email`, "POST", {});
        window.showMessage(
          result.created
            ? `${result.created} recibo(s) preparado(s) en la bandeja de salida${result.without_email.length ? ` · sin email: ${result.without_email.join(", ")}` : ""}.`
            : "Los recibos de esta nómina ya estaban preparados o enviados.",
          result.without_email.length ? "warning" : "success",
        );
        window.dispatchEvent(new CustomEvent("capafiscal:outbox-changed"));
        return;
      } else if (action === "delete") {
        if (!window.confirm("¿Eliminar este borrador de nómina?")) return;
        await window.apiRequest(`/payroll/runs/${currentRunId}`, { method: "DELETE" });
        currentRunId = null;
      }
      await loadRuns();
      window.refreshAll?.();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function simulate() {
    const form = document.getElementById("simulatorForm");
    const params = new URLSearchParams(new FormData(form));
    const container = document.getElementById("simulatorResult");

    if (!Number(params.get("annual_salary"))) {
      container.innerHTML = "";
      return;
    }

    try {
      const result = await window.apiRequest(`/team/simulate?${params.toString()}`);
      container.innerHTML = `
        <div class="indicator"><span>Neto por paga</span><strong>${money(result.monthly_net)}</strong><small>${params.get("payments_per_year")} pagas · IRPF ${String(result.irpf_rate).replace(".", ",")} %</small></div>
        <div class="indicator"><span>Neto anual</span><strong>${money(result.net)}</strong></div>
        <div class="indicator"><span>Seguridad Social empresa</span><strong>${money(result.ss_employer)}</strong><small>${String(result.employer_ratio).replace(".", ",")} % sobre el bruto</small></div>
        <div class="indicator highlight"><span>Coste anual empresa</span><strong>${money(result.company_cost)}</strong><small>${money(result.company_cost / 12)} al mes</small></div>
      `;
    } catch (error) {
      container.innerHTML = `<p class="empty-inline">${esc(error.message)}</p>`;
    }
  }

  function setup() {
    setupForm();

    document.getElementById("payrollCreateForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      try {
        const run = await window.jsonRequest("/payroll/runs", "POST", {
          year: Number(data.get("year")),
          month: Number(data.get("month")),
        });
        currentRunId = run.id;
        window.showMessage(`Borrador de ${run.period} generado para ${run.totals.employees} persona(s).`, "success");
        await loadRuns();
        document.getElementById("payrollRunCard").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    const section = document.getElementById("tab-nominas");
    section?.addEventListener("click", (event) => {
      const open = event.target.closest("[data-open-run]");
      if (open) loadRun(Number(open.dataset.openRun));

      const action = event.target.closest("[data-run-action]");
      if (action) runAction(action.dataset.runAction);
    });

    section?.addEventListener("change", async (event) => {
      const cell = event.target.closest(".cell-input");
      if (!cell) return;
      try {
        await window.jsonRequest(`/payroll/payslips/${cell.dataset.payslip}`, "PATCH", {
          [cell.dataset.field]: Number(cell.value || 0),
        });
        await loadRun(currentRunId);
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("simulatorForm")?.addEventListener("input", () => {
      window.clearTimeout(simulateTimer);
      simulateTimer = window.setTimeout(simulate, 300);
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "nominas";
    if (active) {
      loadRuns().catch((error) => window.showMessage(error.message, "error"));
      simulate();
    }
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
