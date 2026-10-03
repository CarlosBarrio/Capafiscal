"use strict";

/* Libro diario (Informes): asientos generados de facturas y banco, con sus comprobaciones y exportación. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  let timer = null;

  function previousMonth() {
    const now = new Date();
    const first = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const last = new Date(now.getFullYear(), now.getMonth(), 0);
    const iso = (value) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
    return [iso(first), iso(last)];
  }

  function mark(ok) {
    return ok === true ? `<span class="work-check is-ok" aria-label="Correcto">✓</span>`
      : ok === false ? `<span class="work-check is-block" aria-label="No">✕</span>`
      : `<span class="work-check is-warn" aria-label="Aviso">!</span>`;
  }

  async function load() {
    const from = document.getElementById("journalFrom").value;
    const to = document.getElementById("journalTo").value;
    const query = `date_from=${from}&date_to=${to}`;
    document.getElementById("journalXlsx").href = `/api/accounting/journal?${query}&format=xlsx`;
    document.getElementById("journalCsv").href = `/api/accounting/journal?${query}&format=csv`;
    const summary = document.getElementById("journalSummary");
    try {
      const data = await window.apiRequest(`/accounting/journal?${query}`);
      summary.innerHTML = `
        <p class="work-night"><strong>${data.count}</strong> asiento(s) · debe ${window.formatMoney(data.totals.debit)} · haber ${window.formatMoney(data.totals.credit)}</p>
        <ul class="journal-checks">${data.checks.map((check) => `<li>${mark(check.ok)}<span>${esc(check.label)}${check.detail ? ` <span class="muted">· ${esc(check.detail)}</span>` : ""}</span></li>`).join("")}</ul>
        ${data.pending.length ? `
          <details class="work-checked">
            <summary>Sin contabilizar (${data.pending.length})</summary>
            <ul>${data.pending.slice(0, 30).map((item) => `<li><span class="muted">${esc(item.date ? window.formatDay(item.date) : "")}</span><span>${esc(item.description)} · ${window.formatMoney(item.amount)} — ${esc(item.why)}</span></li>`).join("")}</ul>
          </details>` : ""}
        <p class="board-empty">${esc(data.note)}</p>`;
    } catch (error) {
      summary.innerHTML = `<p class="danger-text">${esc(error.message)}</p>`;
    }
  }

  function setup() {
    const form = document.getElementById("journalForm");
    if (!form) return;
    const [from, to] = previousMonth();
    document.getElementById("journalFrom").value = from;
    document.getElementById("journalTo").value = to;
    form.addEventListener("change", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(load, 200);
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    if (event.detail?.tab === "informes") load();
  });
  document.addEventListener("DOMContentLoaded", setup);
})();
