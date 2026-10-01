"use strict";

(() => {
  let active = false;
  let selected = null;

  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);

  const STATUS = {
    FILED: ["Presentado", "status-success"],
    OVERDUE: ["Vencido", "status-danger"],
    DUE_SOON: ["Vence pronto", "status-warning"],
    UPCOMING: ["Próximo", "status-neutral"],
    NO_DATA: ["Sin datos", "status-neutral"],
  };

  const DRAFTABLE = new Set(["303", "130", "111", "115"]);

  function setup() {
    const yearSelect = document.getElementById("taxYear");
    if (!yearSelect) return;

    const year = new Date().getFullYear();
    yearSelect.innerHTML = [year + 1, year, year - 1]
      .map((value) => `<option ${value === year ? "selected" : ""}>${value}</option>`)
      .join("");
    yearSelect.addEventListener("change", loadAll);

    document.getElementById("taxCalendar")?.addEventListener("click", onCalendarClick);
    document.getElementById("taxPosition")?.addEventListener("click", onCalendarClick);
  }

  const GAP_LABELS = {
    pendiente_revision: "Facturas sin revisar",
    factura_falta: "Facturas habituales que no han llegado",
    movimiento_sin_factura: "Movimientos del banco sin factura",
  };

  async function loadPosition() {
    const container = document.getElementById("taxPosition");
    if (!container) return;
    try {
      renderPosition(await window.apiRequest("/taxes/position"));
    } catch (error) {
      container.innerHTML = `<p class="empty-inline">${esc(error.message)}</p>`;
    }
  }

  function renderPosition(data) {
    document.getElementById("taxPositionSub").textContent = `${data.period_label} · calculado ${new Date().toLocaleString("es-ES", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
    const container = document.getElementById("taxPosition");
    if (!data.models.length) {
      container.innerHTML = `<p class="empty-inline">No hay modelos trimestrales para ${esc(data.period_label)}.</p>`;
      return;
    }
    container.innerHTML = data.models.map((item) => {
      const share = Math.round((item.information_available || 0) * 100);
      const groups = {};
      item.gaps.forEach((gap) => { (groups[gap.type] = groups[gap.type] || []).push(gap); });
      const status = item.status === "FILED" ? ["Presentado", "status-success"] : item.status === "COMPLETE" ? ["Completo", "status-success"] : ["Incompleto", "status-warning"];
      const pending = item.result_with_pending !== null && item.result_with_pending !== undefined;
      return `
        <div class="position-row">
          <div class="position-main">
            <div class="position-title">
              <strong>Modelo ${esc(item.model)}</strong>
              <span class="status-pill mini ${status[1]}">${status[0]}</span>
              <span class="muted">vence ${window.formatDay(item.due_date)}${item.days_left >= 0 ? ` · ${item.days_left} d` : ""}</span>
            </div>
            <dl class="position-figures">
              <div><dt>Con lo aprobado</dt><dd>${money(item.result)}</dd></div>
              ${pending ? `<div><dt>Si apruebas lo pendiente</dt><dd>${money(item.result_with_pending)}</dd></div>` : ""}
              <div><dt>Información disponible</dt><dd>${share} %</dd></div>
            </dl>
            <span class="meter" aria-hidden="true"><span style="width:${share}%"></span></span>
            <small class="muted">${item.approved_invoices} factura(s) aprobadas incluidas · % calculado sobre el importe conocido del periodo</small>
          </div>
          ${item.gaps.length || item.discrepancies.length ? `
            <details class="position-gaps">
              <summary>Qué falta (${item.gaps.length + item.discrepancies.length})</summary>
              ${Object.entries(groups).map(([type, gaps]) => `
                <p class="position-gap-title">${esc(GAP_LABELS[type] || type)}</p>
                <ul>${gaps.map((gap) => `<li><span>${esc(gap.label)}</span><span class="num">${money(gap.amount)}</span></li>`).join("")}</ul>`).join("")}
              ${item.discrepancies.length ? `
                <p class="position-gap-title">Discrepancias</p>
                <ul>${item.discrepancies.map((gap) => `<li><span>${esc(gap.label)}</span><span class="num">${money(gap.amount)}</span></li>`).join("")}</ul>` : ""}
            </details>` : ""}
          <button type="button" class="btn-ghost" data-action="draft" data-model="${esc(item.model)}" data-year="${item.year}" data-period="${item.quarter}">Ver borrador</button>
        </div>`;
    }).join("");
  }

  async function loadAll() {
    const year = Number(document.getElementById("taxYear").value);

    try {
      loadPosition();
      const calendar = await window.apiRequest(`/taxes/calendar?year=${year}`);
      renderCalendar(calendar);

      if (!selected) {
        const next = calendar.entries.find((entry) => DRAFTABLE.has(entry.model) && ["DUE_SOON", "UPCOMING", "OVERDUE"].includes(entry.status));
        if (next) selected = { model: next.model, year: next.period_year, quarter: next.period };
      }

      if (selected) await loadDraft(selected.model, selected.year, selected.quarter);
      await load347(year - 1);
    } catch (error) {
      document.getElementById("taxCalendar").innerHTML =
        window.emptyState("⚠️", "No se pudo cargar el calendario", error.message);
    }
  }

  function renderCalendar(calendar) {
    const container = document.getElementById("taxCalendar");
    document.getElementById("taxFormNote").textContent =
      calendar.legal_form === "AUTONOMO"
        ? "Obligaciones de autónomo (303, 130, renta…). Cámbialo en Mi empresa si no es correcto."
        : "Obligaciones de sociedad (303, 202, 200…). Cámbialo en Mi empresa si eres autónomo.";

    const pending = calendar.entries.filter((entry) => ["OVERDUE", "DUE_SOON"].includes(entry.status)).length;
    document.getElementById("taxCalendarSub").textContent = pending
      ? `${pending} obligación(es) requieren atención`
      : `${calendar.entries.length} obligaciones con vencimiento en ${calendar.year}`;
    document.getElementById("taxCalendarNote").textContent = calendar.note;

    if (!calendar.entries.length) {
      container.innerHTML = window.emptyState("🗓️", "Sin obligaciones", "No hay modelos con vencimiento en este año.");
      return;
    }

    const rank = { OVERDUE: 0, DUE_SOON: 1, UPCOMING: 2, FILED: 3 };
    const actionable = calendar.entries
      .filter((entry) => entry.status !== "NO_DATA")
      .sort((a, b) => (rank[a.status] - rank[b.status]) || a.due_date.localeCompare(b.due_date));
    const withoutData = calendar.entries.filter((entry) => entry.status === "NO_DATA");

    container.innerHTML = actionable.map(entryHtml).join("") + (withoutData.length ? `
      <details class="tax-nodata">
        <summary>${withoutData.length} periodo(s) anteriores sin facturas en CapaFiscal</summary>
        <p class="detail-hint">No hay datos para saber si se presentaron. Márcalos si ya lo hiciste para que el historial quede completo.</p>
        ${withoutData.map(entryHtml).join("")}
      </details>
    ` : "");
  }

  function entryHtml(entry) {
    const [label, className] = STATUS[entry.status] || STATUS.UPCOMING;
    const days = Number(entry.days_left);
    const countdown = entry.status === "FILED"
      ? `Presentado el ${window.formatDay(entry.filed_at)}`
      : entry.status === "NO_DATA"
        ? "Periodo sin facturas en CapaFiscal"
        : days < 0 ? `Venció hace ${Math.abs(days)} días` : days === 0 ? "Vence hoy" : `Faltan ${days} días`;
    const isSelected = selected
      && selected.model === entry.model
      && selected.year === entry.period_year
      && selected.quarter === entry.period;

    return `
      <div class="tax-entry status-${esc(entry.status.toLowerCase())} ${isSelected ? "selected" : ""}">
        <div class="tax-entry-model">
          <strong>${esc(entry.model)}</strong>
          <span>${esc(entry.period_label)}</span>
        </div>
        <div class="tax-entry-body">
          <p class="tax-entry-name">${esc(entry.name)}</p>
          <p class="tax-entry-meta">
            Vence el ${window.formatDay(entry.due_date)} · ${esc(countdown)}
            ${entry.estimate !== null && entry.estimate !== undefined && entry.status !== "NO_DATA"
              ? ` · estimado <strong>${money(entry.estimate)}</strong>` : ""}
            ${entry.filed_amount !== null && entry.filed_amount !== undefined ? ` · presentado ${money(entry.filed_amount)}` : ""}
          </p>
          ${entry.note ? `<p class="tax-entry-note">${esc(entry.note)}</p>` : ""}
        </div>
        <div class="tax-entry-actions">
          <span class="status-pill ${className}">${esc(label)}</span>
          ${DRAFTABLE.has(entry.model)
            ? `<button type="button" class="btn-ghost" data-action="draft" data-model="${esc(entry.model)}" data-year="${entry.period_year}" data-period="${entry.period}">Borrador</button>`
            : ""}
          ${entry.status === "FILED"
            ? `<button type="button" class="btn-ghost" data-action="unfile" data-model="${esc(entry.model)}" data-year="${entry.period_year}" data-period="${entry.period}">Desmarcar</button>`
            : `<button type="button" class="btn-ghost" data-action="file" data-model="${esc(entry.model)}" data-year="${entry.period_year}" data-period="${entry.period}" data-estimate="${entry.estimate ?? ""}">Marcar presentado</button>`}
        </div>
      </div>
    `;
  }

  async function onCalendarClick(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;

    const model = button.dataset.model;
    const year = Number(button.dataset.year);
    const period = Number(button.dataset.period);

    if (button.dataset.action === "draft") {
      selected = { model, year, quarter: period };
      await loadAll();
      document.getElementById("taxDraftCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    if (button.dataset.action === "file") {
      const reference = window.prompt(
        `Modelo ${model}: indica el número de justificante o CSV de la presentación (opcional).`,
        ""
      );
      if (reference === null) return;

      const amountText = window.prompt(
        "Importe presentado (vacío si coincide con la estimación o es cero):",
        button.dataset.estimate || ""
      );
      if (amountText === null) return;

      const amount = amountText.trim() ? Number(amountText.replace(",", ".")) : null;

      try {
        await window.jsonRequest("/taxes/filings", "POST", {
          model,
          year,
          period,
          reference: reference.trim() || null,
          amount: Number.isFinite(amount) ? amount : null,
        });
        window.showMessage(`Modelo ${model} marcado como presentado.`, "success");
        await loadAll();
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
      return;
    }

    if (button.dataset.action === "unfile") {
      if (!window.confirm(`¿Desmarcar el modelo ${model} como presentado?`)) return;
      try {
        await window.apiRequest(`/taxes/filings?model=${model}&year=${year}&period=${period}`, { method: "DELETE" });
        await loadAll();
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    }
  }

  async function loadDraft(model, year, quarter) {
    const container = document.getElementById("taxDraft");

    try {
      const draft = await window.apiRequest(`/taxes/models/${model}?year=${year}&quarter=${quarter}`);
      renderDraft(draft);
    } catch (error) {
      container.innerHTML = window.emptyState("⚠️", "No se pudo calcular el borrador", error.message);
    }
  }

  function renderDraft(draft) {
    document.getElementById("taxDraftTitle").textContent = `Modelo ${draft.model} · ${draft.name}`;
    document.getElementById("taxDraftPeriod").textContent = draft.period_label;

    const formatBox = (box) => {
      if (box.is_rate) return `${String(box.value).replace(".", ",")} %`;
      if (box.is_count) return String(box.value);
      return money(box.value);
    };

    document.getElementById("taxDraft").innerHTML = `
      <div class="tax-result ${draft.result > 0 ? "to-pay" : "no-pay"}">
        <span>${esc(draft.outcome)}</span>
        <strong>${money(draft.result)}</strong>
      </div>
      ${(draft.warnings || []).map((text) => `<div class="report-warning">${window.icon("alert")}<span>${esc(text)}</span></div>`).join("")}
      <table class="data-table compact tax-boxes">
        <thead><tr><th>Casilla</th><th>Concepto</th><th class="num">Importe</th></tr></thead>
        <tbody>
          ${draft.boxes.map((box) => `
            <tr class="${box.highlight ? "total-row" : ""}">
              <td><span class="box-number">${esc(box.box)}</span></td>
              <td>${esc(box.label)}</td>
              <td class="num">${formatBox(box)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      ${draft.invoices?.length ? `
        <h3 class="report-subtitle">Facturas con retención incluidas</h3>
        <ul class="plain-list">
          ${draft.invoices.map((invoice) => `
            <li>
              <button type="button" class="link-button" onclick="showDetail(${Number(invoice.document_id)})">${esc(invoice.name || "Factura")}</button>
              · base ${money(invoice.base)} · retención ${money(invoice.withholding)}
            </li>
          `).join("")}
        </ul>
      ` : ""}
      <p class="report-note">${esc(draft.note)}</p>
    `;
  }

  async function load347(year) {
    const container = document.getElementById("model347");

    try {
      const model = await window.apiRequest(`/taxes/models/347?year=${year}`);
      document.getElementById("model347Sub").textContent = `Ejercicio ${model.year} · se presenta en febrero`;

      if (!model.counterparties.length) {
        container.innerHTML = `<p class="empty-inline">Ningún tercero supera 3.005,06 € en ${model.year} con los datos registrados.</p>`;
        return;
      }

      container.innerHTML = `
        <div class="table-scroll">
          <table class="data-table compact">
            <thead>
              <tr>
                <th>Tercero</th><th>NIF</th><th>Clave</th>
                <th class="num">1T</th><th class="num">2T</th><th class="num">3T</th><th class="num">4T</th>
                <th class="num">Total anual</th>
              </tr>
            </thead>
            <tbody>
              ${model.counterparties.map((item) => `
                <tr>
                  <td>${esc(item.name || "—")}</td>
                  <td>${esc(item.tax_id || "—")}</td>
                  <td>${esc(item.key_label)}</td>
                  ${item.quarters.map((value) => `<td class="num">${money(value)}</td>`).join("")}
                  <td class="num"><strong>${money(item.total)}</strong></td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
        <p class="report-note">${esc(model.note)}</p>
      `;
    } catch (error) {
      container.innerHTML = window.emptyState("⚠️", "No se pudo calcular el 347", error.message);
    }
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "impuestos";
    if (active) loadAll();
  });

  window.addEventListener("capafiscal:data-changed", () => {
    if (active) loadAll();
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
