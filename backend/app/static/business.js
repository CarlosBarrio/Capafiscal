"use strict";

(() => {
  let active = false;
  let bankStatus = "";

  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);

  function monthShort(key) {
    const [year, month] = String(key).split("-").map(Number);
    return new Intl.DateTimeFormat("es-ES", { month: "short" }).format(new Date(year, month - 1, 1));
  }

  function setupPeriod() {
    const yearSelect = document.getElementById("healthYear");
    const quarterSelect = document.getElementById("healthQuarter");
    if (!yearSelect || !quarterSelect) return;

    const year = new Date().getFullYear();
    yearSelect.innerHTML = [year, year - 1, year - 2].map((value) => `<option>${value}</option>`).join("");
    quarterSelect.value = String(window.currentQuarter());

    yearSelect.addEventListener("change", loadHealth);
    quarterSelect.addEventListener("change", loadHealth);
  }

  /* ------------------------------------------------------------
  SALUD DEL NEGOCIO
  ------------------------------------------------------------ */
  async function loadHealth() {
    const params = new URLSearchParams();
    params.set("year", document.getElementById("healthYear").value);
    const quarter = document.getElementById("healthQuarter").value;
    if (quarter) params.set("quarter", quarter);

    try {
      const [health, receivables] = await Promise.all([
        window.apiRequest(`/business-health?${params.toString()}`),
        window.apiRequest("/payments?direction=ISSUED"),
      ]);
      renderHealth(health);
      renderReceivables(receivables);
    } catch (error) {
      document.getElementById("healthInsights").innerHTML =
        window.emptyState("⚠️", "No se pudo calcular la salud del negocio", error.message);
    }
  }

  function renderHealth(health) {
    document.getElementById("hIncome").textContent = money(health.income);
    document.getElementById("hExpenses").textContent = money(health.expenses);

    const result = document.getElementById("hResult");
    result.textContent = money(health.result);
    result.classList.toggle("value-negative", health.result < 0);
    document.getElementById("hResultLabel").textContent =
      health.margin !== null && health.margin !== undefined
        ? `resultado ${health.period} · margen ${String(health.margin).replace(".", ",")} %`
        : `resultado ${health.period}`;

    document.getElementById("hBalance").textContent =
      health.bank_balance !== null && health.bank_balance !== undefined ? money(health.bank_balance) : "—";
    document.getElementById("hBalanceLabel").textContent = health.bank_balance_date
      ? `saldo en banco a ${window.formatDay(health.bank_balance_date)}`
      : "saldo en banco (importa un extracto)";

    document.getElementById("healthInsights").innerHTML = (health.insights || [])
      .map((text) => `<div class="report-warning insight">${window.icon("bulb")}<span>${esc(text)}</span></div>`)
      .join("");

    renderMonthChart(health.months);

    const indicator = (label, value, hint = "") => `
      <div class="indicator">
        <span>${esc(label)}</span>
        <strong>${value}</strong>
        ${hint ? `<small>${esc(hint)}</small>` : ""}
      </div>
    `;

    document.getElementById("healthIndicators").innerHTML = [
      indicator("IVA repercutido − soportado", money(health.vat_balance), `Repercutido ${money(health.vat_output)} · soportado ${money(health.vat_input)}`),
      indicator(
        "Periodo medio de cobro",
        health.collection_days !== null ? `${String(health.collection_days).replace(".", ",")} días` : "—",
        "Máximo legal entre empresas: 60 días"
      ),
      indicator(
        "Periodo medio de pago",
        health.payment_days !== null ? `${String(health.payment_days).replace(".", ",")} días` : "—"
      ),
      indicator("Pendiente de cobro", money(health.receivables_total), health.receivables_overdue ? `Vencido: ${money(health.receivables_overdue)}` : ""),
      indicator("Pendiente de pago", money(health.payables_total), health.payables_overdue ? `Vencido: ${money(health.payables_overdue)}` : ""),
      ...(health.top_expenses.length
        ? [indicator(
          "Mayor partida de gasto",
          esc(health.top_expenses[0].category),
          money(health.top_expenses[0].amount)
        )]
        : []),
    ].join("");
  }

  // Dos series (ingresos y gastos), un eje, barras finas con leyenda fija.
  function renderMonthChart(months) {
    const container = document.getElementById("healthChart");
    if (!container) return;

    const max = Math.max(...months.flatMap((item) => [item.income, item.expenses]), 0);

    if (!max) {
      container.innerHTML = `<p class="empty-inline">Aún no hay facturas aprobadas en los últimos 12 meses.</p>`;
      return;
    }

    container.innerHTML = `
      <div class="month-columns" role="img" aria-label="Ingresos y gastos por mes">
        ${months.map((item) => {
          const incomeHeight = (item.income / max) * 100;
          const expenseHeight = (item.expenses / max) * 100;
          const tooltip = `${monthShort(item.month)} ${item.month.slice(0, 4)}: ingresos ${money(item.income)} · gastos ${money(item.expenses)} · resultado ${money(item.result)}`;
          return `
            <div class="month-column" title="${esc(tooltip)}" tabindex="0" aria-label="${esc(tooltip)}">
              <div class="month-bars">
                <span class="bar-income" style="height:${incomeHeight.toFixed(1)}%"></span>
                <span class="bar-expense" style="height:${expenseHeight.toFixed(1)}%"></span>
              </div>
              <span class="month-label">${esc(monthShort(item.month))}</span>
            </div>
          `;
        }).join("")}
      </div>
      <details class="table-view">
        <summary>Ver como tabla</summary>
        <table class="data-table compact">
          <thead><tr><th>Mes</th><th class="num">Ingresos</th><th class="num">Gastos</th><th class="num">Resultado</th></tr></thead>
          <tbody>
            ${months.map((item) => `
              <tr>
                <td>${esc(item.month)}</td>
                <td class="num">${money(item.income)}</td>
                <td class="num">${money(item.expenses)}</td>
                <td class="num">${money(item.result)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </details>
    `;
  }

  function renderReceivables(overview) {
    const list = document.getElementById("receivablesList");
    const sub = document.getElementById("receivablesSub");

    if (sub) {
      sub.textContent = overview.unpaid_count
        ? `${overview.unpaid_count} factura(s) · ${money(overview.unpaid_total)}`
        : "Todo cobrado";
    }

    if (!overview.items.length) {
      list.innerHTML = window.emptyState("💶", "Nada pendiente de cobro", "Las facturas emitidas aprobadas sin cobrar aparecerán aquí.");
      return;
    }

    const labels = {
      OVERDUE: ["Vencida", "status-danger"],
      DUE_SOON: ["Vence pronto", "status-warning"],
      PENDING: ["Pendiente", "status-neutral"],
      NO_DUE_DATE: ["Sin vencimiento", "status-neutral"],
    };

    list.innerHTML = overview.items.slice(0, 8).map((item) => {
      const [label, className] = labels[item.state] || labels.PENDING;
      const days = Number(item.days_to_due);
      const dueText = item.due_date
        ? (days < 0 ? `Venció hace ${Math.abs(days)} día(s)` : `Vence en ${days} día(s)`)
        : "Sin fecha de vencimiento";

      return `
        <div class="payment-row">
          <div class="payment-main">
            <strong>${esc(item.supplier_name || "Cliente sin identificar")}</strong>
            <span>${esc(item.invoice_number || "Sin número")} · ${esc(dueText)}</span>
          </div>
          <div class="payment-side">
            <strong>${money(item.total)}</strong>
            <span class="status-pill ${className}">${esc(label)}</span>
          </div>
          <button type="button" class="btn-ghost" onclick="showDetail(${Number(item.document_id)}, 'payment')">
            Registrar cobro
          </button>
        </div>
      `;
    }).join("");
  }

  /* ------------------------------------------------------------
  TESORERÍA
  ------------------------------------------------------------ */
  async function loadCashflow() {
    try {
      const forecast = await window.apiRequest("/cashflow?horizon=90");
      renderCashflow(forecast);
    } catch (error) {
      document.getElementById("cashflowWarnings").innerHTML =
        window.emptyState("⚠️", "No se pudo calcular la previsión", error.message);
    }
  }

  function renderCashflow(forecast) {
    const sub = document.getElementById("cashflowSub");
    sub.textContent =
      `Entradas ${money(forecast.expected_inflows)} · salidas ${money(forecast.expected_outflows)}` +
      (forecast.projected_balance !== null ? ` · saldo final previsto ${money(forecast.projected_balance)}` : "");

    document.getElementById("cashflowWarnings").innerHTML = (forecast.warnings || [])
      .map((text) => `<div class="report-warning">${window.icon("alert")}<span>${esc(text)}</span></div>`)
      .join("");

    const body = document.querySelector("#cashflowTable tbody");
    const rows = [];

    if (forecast.current_balance !== null) {
      rows.push(`
        <tr class="total-row">
          <td>${window.formatDay(forecast.balance_date)}</td>
          <td>Saldo actual en banco</td>
          <td class="num"></td>
          <td class="num">${money(forecast.current_balance)}</td>
        </tr>
      `);
    }

    for (const movement of forecast.movements) {
      const negativeAfter = movement.balance_after !== undefined && movement.balance_after < 0;
      rows.push(`
        <tr class="${negativeAfter ? "row-danger" : ""}">
          <td>
            ${window.formatDay(movement.date)}
            ${movement.overdue ? `<span class="status-pill status-danger mini">vencido ${window.formatDay(movement.original_date)}</span>` : ""}
            ${movement.estimated_date ? `<span class="muted">(estimada)</span>` : ""}
          </td>
          <td>
            ${movement.document_id
              ? `<button type="button" class="link-button" onclick="showDetail(${Number(movement.document_id)}, 'payment')">${esc(movement.label)}</button>`
              : esc(movement.label)}
          </td>
          <td class="num ${movement.amount < 0 ? "value-negative" : "value-positive"}">${money(movement.amount)}</td>
          <td class="num">${movement.balance_after !== undefined ? money(movement.balance_after) : "—"}</td>
        </tr>
      `);
    }

    body.innerHTML = rows.length
      ? rows.join("")
      : `<tr><td colspan="4" class="empty-cell">No hay cobros, pagos ni impuestos previstos en los próximos 90 días.</td></tr>`;

    document.getElementById("cashflowNote").textContent = forecast.note;
  }

  /* ------------------------------------------------------------
  BANCO
  ------------------------------------------------------------ */
  const MATCH_LABELS = {
    UNMATCHED: ["Sin conciliar", "status-neutral"],
    SUGGESTED: ["Propuesta del agente", "status-warning"],
    MATCHED: ["Conciliado", "status-success"],
    IGNORED: ["Ignorado", "status-neutral"],
  };

  // Nivel de confianza de la conciliación (app/reconciliation.py): nunca se concilia solo lo que no es SEGURO.
  const LEVEL_LABELS = {
    SEGURO: ["Seguro", "status-success"],
    PROBABLE: ["Probable", "status-info"],
    CONFLICTO: ["Conflicto", "status-danger"],
    SIN_MATCH: ["Sin match", "status-warning"],
  };

  // ✓ / ✗ / ~ de cada comprobación y, si hay varios, los candidatos considerados.
  function evidenceHtml(recon) {
    const mark = (ok) => ok === true ? `<span class="ev-ok" aria-label="sí">✓</span>` : ok === false ? `<span class="ev-no" aria-label="no">✗</span>` : `<span class="ev-mid" aria-label="aproximado">~</span>`;
    return `
      <details class="evidence">
        <summary>Evidencia</summary>
        ${recon.checks?.length ? `<ul>${recon.checks.map((check) => `<li>${mark(check.ok)} ${esc(check.label)}</li>`).join("")}</ul>` : ""}
        ${recon.candidates?.length > 1 ? `
          <p class="evidence-title">Candidatos considerados</p>
          <ul>${recon.candidates.map((item) => `<li><span>${esc(item.label)}</span> <span class="num">${money(item.amount)} · ${item.score} %</span></li>`).join("")}</ul>` : ""}
      </details>`;
  }

  const SUMMARY_LEVELS = [
    ["SEGURO", "seguros"],
    ["PROBABLE", "probables"],
    ["CONFLICTO", "en conflicto"],
    ["SIN_MATCH", "sin match"],
  ];
  let reconciliation = {};

  function renderBankSummary(data) {
    const container = document.getElementById("bankSummary");
    if (!container) return;
    if (!data?.total) {
      container.innerHTML = "";
      return;
    }
    const levels = data.levels || {};
    container.innerHTML = `
      <div><dt>movimientos</dt><dd>${data.total}</dd></div>
      <div><dt>conciliados</dt><dd>${data.counts.CONCILIADO || 0}</dd></div>
      ${SUMMARY_LEVELS.map(([key, text]) => `<div class="${key === "CONFLICTO" && levels[key] ? "is-risk" : ""}"><dt>${text}</dt><dd>${levels[key] || 0}</dd></div>`).join("")}
      <div><dt>facturas sin pago</dt><dd>${data.counts.FACTURA_SIN_PAGO || 0}</dd></div>
    `;
    container.title = data.rule || "";
  }

  async function loadBank() {
    const params = new URLSearchParams();
    if (bankStatus) params.set("status", bankStatus);

    try {
      const [transactions, imports] = await Promise.all([
        window.apiRequest(`/bank/transactions?${params.toString()}`),
        window.apiRequest("/bank/imports"),
      ]);
      try {
        const data = await window.apiRequest("/bank/reconciliation");
        reconciliation = Object.fromEntries((data.movements || []).map((item) => [item.transaction_id, item]));
        renderBankSummary(data);
      } catch {
        reconciliation = {};
      }
      renderBank(transactions);

      const sub = document.getElementById("bankSub");
      sub.textContent = imports.length
        ? `Última importación: ${imports[0].filename} (${imports[0].rows_imported} nuevos)`
        : "Aún no has importado movimientos";
    } catch (error) {
      document.querySelector("#bankTable tbody").innerHTML =
        `<tr><td colspan="5" class="empty-cell">${esc(error.message)}</td></tr>`;
    }
  }

  function renderBank(transactions) {
    const body = document.querySelector("#bankTable tbody");

    if (!transactions.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty-cell">Sin movimientos${bankStatus ? " con este estado" : ". Importa el extracto de tu banco para conciliar cobros y pagos"}.</td></tr>`;
      return;
    }

    body.innerHTML = transactions.map((transaction) => {
      const [label, className] = MATCH_LABELS[transaction.match_status] || MATCH_LABELS.UNMATCHED;
      const invoice = transaction.invoice;
      const candidates = transaction.candidates || [];

      const recon = transaction.match_status === "IGNORED" ? null : reconciliation[transaction.id];
      const [levelLabel, levelClass] = (recon?.level && LEVEL_LABELS[recon.level]) || [label, className];
      let invoiceCell = `<span class="status-pill ${levelClass}">${esc(levelLabel)}${recon?.confidence && ["SEGURO", "PROBABLE"].includes(recon.level) ? ` · ${recon.confidence} %` : ""}</span>`;
      if (recon?.decision && transaction.match_status !== "MATCHED") invoiceCell += `<small class="muted block">${esc(recon.decision)}</small>`;
      if (recon?.state === "DUPLICADO") invoiceCell += `<small class="muted block">${esc(recon.evidence?.[0] || "")}</small>`;
      if (recon && (recon.checks?.length || recon.candidates?.length > 1)) invoiceCell += evidenceHtml(recon);
      if (invoice) {
        invoiceCell += `
          <button type="button" class="link-button block" onclick="showDetail(${Number(invoice.document_id)})">
            ${esc(invoice.name || "Factura")} · ${esc(invoice.number || "s/n")} · ${money(invoice.total)}
          </button>
          ${transaction.match_score ? `<small class="muted">Coincidencia ${transaction.match_score} %</small>` : ""}
        `;
      } else if (candidates.length) {
        invoiceCell += `
          <select class="candidate-select" data-transaction="${transaction.id}" aria-label="Elegir factura">
            <option value="">Elegir factura…</option>
            ${candidates.map((candidate) => `
              <option value="${candidate.invoice_id}">${esc(candidate.name || "Factura")} · ${esc(candidate.number || "s/n")} (${candidate.score} %)</option>
            `).join("")}
          </select>
        `;
      }

      let actions = "";

      if (transaction.match_status === "SUGGESTED") {
        actions = `
          <button type="button" class="act-btn act-primary" data-action="confirm" data-id="${transaction.id}">Confirmar</button>
          <button type="button" class="btn-ghost" data-action="reject" data-id="${transaction.id}">No es</button>
        `;
      } else if (transaction.match_status === "MATCHED") {
        actions = `<button type="button" class="btn-ghost" data-action="unmatch" data-id="${transaction.id}">Deshacer</button>`;
      } else if (transaction.match_status === "UNMATCHED") {
        actions = `<button type="button" class="btn-ghost" data-action="ignore" data-id="${transaction.id}">Ignorar</button>`;
      } else {
        actions = `<button type="button" class="btn-ghost" data-action="reject" data-id="${transaction.id}">Restaurar</button>`;
      }

      return `
        <tr>
          <td>${window.formatDay(transaction.booking_date)}</td>
          <td>
            ${esc(transaction.description)}
            ${transaction.account_label ? `<small class="muted block">${esc(transaction.account_label)}</small>` : ""}
          </td>
          <td class="num ${transaction.amount < 0 ? "" : "value-positive"}">${money(transaction.amount)}</td>
          <td>${invoiceCell}</td>
          <td class="actions-cell">${actions}</td>
        </tr>
      `;
    }).join("");
  }

  async function bankAction(action, id, invoiceId = null) {
    try {
      if (action === "confirm") {
        await window.jsonRequest(`/bank/transactions/${id}/confirm`, "POST", { invoice_id: invoiceId });
        window.showMessage("Movimiento conciliado: la factura queda marcada como pagada/cobrada.", "success");
      } else {
        await window.jsonRequest(`/bank/transactions/${id}/unmatch`, "POST", { ignore: action === "ignore" });
      }
      await refreshBusiness();
      window.refreshAll();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  function setupBank() {
    document.getElementById("bankImportForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      button.textContent = "Importando…";

      try {
        const result = await window.apiRequest("/bank/import", { method: "POST", body: new FormData(form) });
        window.showMessage(result.message, "success");
        form.reset();
        await refreshBusiness();
      } catch (error) {
        window.showMessage(`No se pudo importar: ${error.message}`, "error");
      } finally {
        button.disabled = false;
        button.textContent = "Importar movimientos";
      }
    });

    document.getElementById("bankFilter")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-status]");
      if (!button) return;
      bankStatus = button.dataset.status;
      document.querySelectorAll("#bankFilter .segment").forEach((item) => item.classList.toggle("active", item === button));
      loadBank();
    });

    const table = document.getElementById("bankTable");
    table?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      bankAction(button.dataset.action, Number(button.dataset.id));
    });
    table?.addEventListener("change", (event) => {
      const select = event.target.closest(".candidate-select");
      if (!select || !select.value) return;
      bankAction("confirm", Number(select.dataset.transaction), Number(select.value));
    });

    document.getElementById("confirmSafeMatches")?.addEventListener("click", async () => {
      try {
        // Solo concilia lo SEGURO (importe exacto + prueba de identidad + una única factura).
        const result = await window.jsonRequest("/bank/reconcile", "POST", {});
        window.showMessage(result.auto_matched ? `${result.auto_matched} movimiento(s) conciliados con evidencia suficiente.` : "Nada con evidencia suficiente para conciliar solo: revisa los probables y los conflictos.", "success");
        await refreshBusiness();
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
  }

  async function refreshBusiness() {
    await Promise.all([loadHealth(), loadCashflow(), loadBank()]);
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "negocio";
    if (active) refreshBusiness();
  });

  window.addEventListener("capafiscal:data-changed", () => {
    if (active) refreshBusiness();
  });

  document.addEventListener("DOMContentLoaded", () => {
    setupPeriod();
    setupBank();
  });
})();
