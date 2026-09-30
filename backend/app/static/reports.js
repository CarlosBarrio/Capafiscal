"use strict";

(() => {
  let activeTab = "panel";
  let supplierTimer = null;
  let supplierDirection = "RECEIVED";

  function money(value) {
    return window.formatMoney(value);
  }

  function esc(value) {
    return window.escapeHtml(value);
  }

  function monthLabel(key) {
    const [year, month] = String(key).split("-").map(Number);
    if (!year || !month) return key;
    return new Intl.DateTimeFormat("es-ES", { month: "short", year: "numeric" })
      .format(new Date(year, month - 1, 1));
  }

  /* ==============================================================
  INFORMES
  ============================================================== */
  function setupReportControls() {
    const yearSelect = document.getElementById("reportYear");
    const quarterSelect = document.getElementById("reportQuarter");
    if (!yearSelect || !quarterSelect) return;

    const year = new Date().getFullYear();
    yearSelect.innerHTML = [year, year - 1, year - 2]
      .map((value) => `<option value="${value}">${value}</option>`)
      .join("");
    quarterSelect.value = String(window.currentQuarter());

    yearSelect.addEventListener("change", loadReports);
    quarterSelect.addEventListener("change", loadReports);

    document.getElementById("exportXlsx")?.addEventListener("click", () => exportLedger("xlsx"));
    document.getElementById("exportCsv")?.addEventListener("click", () => exportLedger("csv"));
  }

  function reportPeriodParams() {
    const params = new URLSearchParams();
    const year = document.getElementById("reportYear")?.value;
    const quarter = document.getElementById("reportQuarter")?.value;
    if (year) params.set("year", year);
    if (quarter) params.set("quarter", quarter);
    return params;
  }

  function exportLedger(format) {
    const params = reportPeriodParams();
    params.set("format", format);
    params.set("book", document.getElementById("exportBook")?.value || "received");
    if (document.getElementById("exportIncludePending")?.checked) {
      params.set("include_pending", "true");
    }
    // Descarga directa: el navegador gestiona el archivo adjunto.
    window.location.href = `/api/exports/ledger?${params.toString()}`;
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("capafiscal:activity-changed"));
    }, 1500);
  }

  async function loadReports() {
    const params = reportPeriodParams();

    const [vatResult, impactResult] = await Promise.allSettled([
      window.apiRequest(`/reports/vat?${params.toString()}`),
      window.apiRequest("/dashboard/monthly-impact"),
    ]);

    if (vatResult.status === "fulfilled") {
      renderVatReport(vatResult.value);
    } else {
      const warnings = document.getElementById("reportWarnings");
      if (warnings) {
        warnings.innerHTML = window.emptyState("⚠️", "No se pudo calcular el informe", vatResult.reason?.message || "");
      }
    }

    if (impactResult.status === "fulfilled") {
      renderImpact(impactResult.value);
    }
  }

  function renderVatReport(report) {
    document.getElementById("reportTitle").textContent = `Resumen ${report.period}`;
    document.getElementById("reportRange").textContent =
      `Del ${window.formatDay(report.date_from)} al ${window.formatDay(report.date_to)}`;

    document.getElementById("txCount").textContent = report.approved_invoices;
    document.getElementById("txBase").textContent = money(report.total_base);
    document.getElementById("txIva").textContent = money(report.total_tax);
    document.getElementById("txTotal").textContent = money(report.total_amount);

    const extras = [];
    if (report.total_withholding) extras.push(`Retenciones IRPF practicadas: ${money(report.total_withholding)} (modelo 111)`);
    if (report.total_surcharge) extras.push(`Recargo de equivalencia: ${money(report.total_surcharge)}`);
    document.getElementById("reportExtra").textContent = extras.join(" · ");

    const issuedLine = document.getElementById("reportIssued");
    if (issuedLine) {
      issuedLine.textContent = report.issued_invoices
        ? `Facturas emitidas: ${report.issued_invoices} · base ${money(report.issued_base)} · IVA repercutido ${money(report.issued_tax)} · ` +
          `IVA repercutido − soportado: ${money(report.vat_balance)} (detalle en Impuestos → 303).`
        : "Sin facturas emitidas aprobadas en el periodo (el IVA repercutido aparecerá al subirlas).";
    }

    const warnings = document.getElementById("reportWarnings");
    warnings.innerHTML = (report.warnings || [])
      .map((text) => `<div class="report-warning">${window.icon("alert")}<span>${esc(text)}</span></div>`)
      .join("");

    const rateBody = document.querySelector("#rateTable tbody");
    rateBody.innerHTML = report.by_rate.length
      ? report.by_rate.map((row) => `
          <tr>
            <td>${esc(row.label)}</td>
            <td class="num">${row.invoices}</td>
            <td class="num">${money(row.base)}</td>
            <td class="num">${money(row.tax)}</td>
          </tr>
        `).join("") + `
          <tr class="total-row">
            <td>Total</td>
            <td></td>
            <td class="num">${money(report.total_base)}</td>
            <td class="num">${money(report.total_tax)}</td>
          </tr>
        `
      : `<tr><td colspan="4" class="empty-cell">Sin facturas aprobadas en el periodo.</td></tr>`;

    const categoryBody = document.querySelector("#categoryTable tbody");
    categoryBody.innerHTML = report.by_category.length
      ? report.by_category.map((row) => `
          <tr>
            <td>${esc(row.category)}</td>
            <td>${esc(row.account)}</td>
            <td class="num">${row.invoices}</td>
            <td class="num">${money(row.base)}</td>
            <td class="num">${money(row.tax)}</td>
            <td class="num">${money(row.total)}</td>
          </tr>
        `).join("")
      : `<tr><td colspan="6" class="empty-cell">Sin datos.</td></tr>`;

    document.getElementById("reportNote").textContent = report.note || "";

    renderMonthChart(report.by_month);
  }

  function renderMonthChart(months) {
    const container = document.getElementById("monthChart");
    if (!container) return;

    if (!months.length) {
      container.innerHTML = `<p class="empty-inline">Sin gasto aprobado en el periodo.</p>`;
      return;
    }

    const max = Math.max(...months.map((item) => item.total), 0) || 1;

    container.innerHTML = months.map((item) => {
      const width = Math.max(2, (item.total / max) * 100);
      const tooltip = `${monthLabel(item.month)}: ${money(item.total)} · base ${money(item.base)} · IVA ${money(item.tax)} · ${item.invoices} factura(s)`;
      return `
        <div class="bar-row" title="${esc(tooltip)}" tabindex="0" aria-label="${esc(tooltip)}">
          <span class="bar-label">${esc(monthLabel(item.month))}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${width.toFixed(1)}%"></span></span>
          <span class="bar-value">${money(item.total)}</span>
        </div>
      `;
    }).join("");
  }

  function renderImpact(impact) {
    document.getElementById("roiDocs").textContent = impact.documents_processed;
    document.getElementById("roiRisks").textContent = impact.risks_detected;
    document.getElementById("roiHours").textContent =
      `${String(impact.estimated_hours_saved).replace(".", ",")} h`;
    document.getElementById("roiCost").textContent = money(impact.estimated_cost_saved);
    document.getElementById("roiNote").textContent = `${impact.period} · ${impact.calculation_note}`;
  }

  /* ==============================================================
  PROVEEDORES
  ============================================================== */
  async function loadSuppliers() {
    const search = document.getElementById("supplierSearch")?.value.trim() || "";
    const body = document.querySelector("#supplierTable tbody");
    const summary = document.getElementById("supplierSummary");
    if (!body) return;

    try {
      const params = new URLSearchParams();
      if (search) params.set("q", search);
      params.set("direction", supplierDirection);
      const suppliers = await window.apiRequest(`/suppliers?${params.toString()}`);
      const clients = supplierDirection === "ISSUED";

      document.getElementById("supplierColName").textContent = clients ? "Cliente" : "Proveedor";
      document.getElementById("supplierColTotal").textContent = clients ? "Facturado" : "Gasto aprobado";
      document.getElementById("supplierColPending").textContent = clients ? "Pendiente de cobro" : "Pendiente de pago";

      if (summary) {
        const total = suppliers.reduce((sum, item) => sum + item.total_approved, 0);
        summary.textContent = `${suppliers.length} ${clients ? "cliente(s)" : "proveedor(es)"} · ${money(total)} aprobado`;
      }

      if (!suppliers.length) {
        body.innerHTML = `<tr><td colspan="8" class="empty-cell">${search ? "Nadie coincide con la búsqueda." : clients ? "Aún no hay clientes: sube tus facturas emitidas." : "Aún no hay proveedores. Sube facturas para empezar."}</td></tr>`;
        return;
      }

      body.innerHTML = suppliers.map((item) => `
        <tr>
          <td>
            <button type="button" class="link-button" data-supplier="${esc(item.tax_id || item.name || "")}">
              ${esc(item.name || "Sin nombre")}
            </button>
          </td>
          <td>
            ${esc(item.tax_id || "—")}
            ${item.tax_id && !item.tax_id_valid ? `<span class="nif-warning" title="El dígito de control no es válido. Revisa el NIF/CIF.">revisar</span>` : ""}
          </td>
          <td>${esc(item.main_category || "—")}</td>
          <td class="num">${item.approved_invoices}${item.pending_invoices ? ` <span class="muted">(+${item.pending_invoices} pend.)</span>` : ""}</td>
          <td class="num">${money(item.total_approved)}</td>
          <td class="num">${money(item.tax_approved)}</td>
          <td class="num">${item.unpaid_amount ? money(item.unpaid_amount) : "—"}</td>
          <td>${window.formatDay(item.last_invoice_date)}</td>
        </tr>
      `).join("");
    } catch (error) {
      body.innerHTML = `<tr><td colspan="8" class="empty-cell">No se pudieron cargar los proveedores: ${esc(error.message)}</td></tr>`;
    }
  }

  function setupSuppliers() {
    document.getElementById("supplierDirection")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-direction]");
      if (!button) return;
      supplierDirection = button.dataset.direction;
      document.querySelectorAll("#supplierDirection .segment").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      loadSuppliers();
    });

    document.getElementById("supplierSearch")?.addEventListener("input", () => {
      window.clearTimeout(supplierTimer);
      supplierTimer = window.setTimeout(loadSuppliers, 250);
    });

    // Al pulsar un proveedor se abren sus facturas filtradas.
    document.querySelector("#supplierTable tbody")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-supplier]");
      if (!button) return;

      const form = document.getElementById("invoiceFilters");
      if (form) {
        form.reset();
        form.elements.q.value = button.dataset.supplier;
        form.elements.direction.value = supplierDirection;
      }
      window.activateTab("facturas");
      form?.dispatchEvent(new Event("input"));
    });
  }

  /* ==============================================================
  CICLO DE VIDA
  ============================================================== */
  function refreshActive() {
    if (activeTab === "informes") loadReports();
    if (activeTab === "proveedores") loadSuppliers();
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    activeTab = event.detail?.tab || "panel";
    refreshActive();
  });

  window.addEventListener("capafiscal:data-changed", refreshActive);

  document.addEventListener("DOMContentLoaded", () => {
    setupReportControls();
    setupSuppliers();
  });
})();
