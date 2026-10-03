"use strict";

/* Ventas y cobros: facturas emitidas, recurrentes, clientes y
   reclamación de impagos. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);
  const day = (value) => window.formatDay(value);

  const VAT_RATES = [21, 10, 5, 4, 0];
  const COLLECTION = {
    draft: "status-neutral",
    pending: "status-info",
    overdue: "status-danger",
    paid: "status-success",
  };
  const LEVELS = { 1: "Recordatorio", 2: "Segundo aviso", 3: "Requerimiento" };

  let active = false;
  let view = "facturas";
  let customers = [];
  let current = null;
  let currentRecurring = null;
  let currentCustomer = null;
  let customerCallback = null;
  let filterTimer = null;

  /* ------------------------------------------------------------
  UTILIDADES
  ------------------------------------------------------------ */
  function round2(value) {
    return Math.round((Number(value) + Number.EPSILON) * 100) / 100;
  }

  function number(value, fallback = 0) {
    const parsed = Number(String(value ?? "").replace(",", "."));
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }

  function closeDialog(id) {
    document.getElementById(id)?.close();
  }

  async function loadCustomers() {
    customers = await window.apiRequest("/sales/customers");
    return customers;
  }

  function customerOptions(selected) {
    if (!customers.length) return `<option value="">Crea primero un cliente</option>`;
    return `<option value="">Elige un cliente…</option>` + customers
      .map((item) => `<option value="${item.id}" ${Number(selected) === item.id ? "selected" : ""}>${esc(item.name)}${item.tax_id ? ` · ${esc(item.tax_id)}` : ""}</option>`)
      .join("");
  }

  /* ------------------------------------------------------------
  EDITOR DE LÍNEAS
  ------------------------------------------------------------ */
  function lineRow(line = {}, editable = true) {
    const disabled = editable ? "" : "disabled";
    const rate = line.vat_rate ?? 21;
    return `
      <div class="line-row">
        <input type="text" class="line-desc" data-field="description" value="${esc(line.description || "")}" placeholder="Concepto" maxlength="500" ${disabled} aria-label="Concepto">
        <input type="number" class="line-num" data-field="quantity" value="${line.quantity ?? 1}" step="0.01" ${disabled} aria-label="Cantidad">
        <input type="number" class="line-num" data-field="unit_price" value="${line.unit_price ?? ""}" step="0.01" placeholder="0,00" ${disabled} aria-label="Precio">
        <input type="number" class="line-num line-small" data-field="discount" value="${line.discount || ""}" min="0" max="100" step="0.01" placeholder="0" ${disabled} aria-label="Descuento %">
        <select class="line-vat" data-field="vat_rate" ${disabled} aria-label="IVA">
          ${VAT_RATES.map((item) => `<option value="${item}" ${Number(rate) === item ? "selected" : ""}>${item} %</option>`).join("")}
        </select>
        <span class="line-amount">${money(line.amount ?? 0)}</span>
        ${editable ? `<button type="button" class="icon-button line-remove" aria-label="Quitar línea">${window.icon("close")}</button>` : "<span></span>"}
      </div>
    `;
  }

  function renderLineEditor(container, lines, editable, onChange) {
    container.innerHTML = `
      <div class="line-head">
        <span>Concepto</span><span>Cant.</span><span>Precio</span><span>Dto. %</span><span>IVA</span><span class="num">Importe</span><span></span>
      </div>
      <div class="line-rows">${(lines.length ? lines : [{}]).map((line) => lineRow(line, editable)).join("")}</div>
      ${editable ? `<button type="button" class="btn-ghost line-add">${window.icon("plus")} Añadir línea</button>` : ""}
    `;
    container.oninput = () => { updateAmounts(container); onChange?.(); };
    container.onclick = (event) => {
      if (event.target.closest(".line-add")) {
        container.querySelector(".line-rows").insertAdjacentHTML("beforeend", lineRow({}, true));
        container.querySelector(".line-rows .line-row:last-child .line-desc")?.focus();
      }
      const remove = event.target.closest(".line-remove");
      if (remove) {
        const rows = container.querySelectorAll(".line-row");
        if (rows.length > 1) remove.closest(".line-row").remove();
        else rows[0].querySelectorAll("input").forEach((input) => { input.value = input.dataset.field === "quantity" ? "1" : ""; });
        updateAmounts(container);
        onChange?.();
      }
    };
    updateAmounts(container);
  }

  function collectLines(container) {
    return [...container.querySelectorAll(".line-row")].map((row) => {
      const get = (field) => row.querySelector(`[data-field="${field}"]`).value;
      return {
        description: get("description").trim(),
        quantity: number(get("quantity"), 1),
        unit_price: number(get("unit_price")),
        discount: number(get("discount")),
        vat_rate: number(get("vat_rate"), 21),
      };
    }).filter((line) => line.description || line.unit_price);
  }

  function lineAmount(line) {
    return round2(line.quantity * line.unit_price * (1 - line.discount / 100));
  }

  function updateAmounts(container) {
    container.querySelectorAll(".line-row").forEach((row) => {
      const get = (field) => row.querySelector(`[data-field="${field}"]`).value;
      const amount = lineAmount({ quantity: number(get("quantity"), 1), unit_price: number(get("unit_price")), discount: number(get("discount")) });
      row.querySelector(".line-amount").textContent = money(amount);
    });
  }

  function computeTotals(lines, withholdingRate) {
    const byRate = new Map();
    for (const line of lines) {
      byRate.set(line.vat_rate, round2((byRate.get(line.vat_rate) || 0) + lineAmount(line)));
    }
    const breakdown = [...byRate.entries()].sort((a, b) => b[0] - a[0])
      .map(([rate, base]) => ({ rate, base, tax: round2(base * rate / 100) }));
    const subtotal = round2(breakdown.reduce((sum, item) => sum + item.base, 0));
    const tax = round2(breakdown.reduce((sum, item) => sum + item.tax, 0));
    const withholding = round2(subtotal * number(withholdingRate) / 100);
    return { breakdown, subtotal, tax, withholding, total: round2(subtotal + tax - withholding) };
  }

  function renderTotals() {
    const form = document.getElementById("invoiceForm");
    const totals = computeTotals(collectLines(document.getElementById("invoiceLines")), form.elements.withholding_rate.value);
    document.getElementById("invoiceTotals").innerHTML = `
      <div><span>Base imponible</span><strong>${money(totals.subtotal)}</strong></div>
      ${totals.breakdown.map((item) => `<div><span>IVA ${item.rate} %</span><strong>${money(item.tax)}</strong></div>`).join("")}
      ${totals.withholding ? `<div><span>Retención IRPF ${number(form.elements.withholding_rate.value)} %</span><strong>−${money(totals.withholding)}</strong></div>` : ""}
      <div class="totals-grand"><span>Total factura</span><strong>${money(totals.total)}</strong></div>
    `;
  }

  /* ------------------------------------------------------------
  RESUMEN Y LISTADO
  ------------------------------------------------------------ */
  async function loadOverview() {
    const data = await window.apiRequest("/sales/overview");
    const set = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
    set("salesMonth", money(data.issued_month));
    set("salesYear", money(data.issued_year));
    set("salesYearFoot", `${window.pl(data.count_year, "factura(s)")} · siguiente ${data.next_code}`);
    set("salesPending", money(data.pending));
    set("salesOverdueFoot", data.collections.count ? `${money(data.collections.amount)} vencido en ${window.pl(data.collections.count, "factura(s)")}` : "nada vencido");
    set("salesRecurring", money(data.recurring_monthly));
    set("salesRecurringFoot", `al mes · ${window.pl(data.recurring_active, "activa(s)")}`);
    updateBadge(data.collections.count);

    const pill = document.getElementById("salesOverdueCount");
    if (pill) {
      pill.textContent = data.collections.count;
      pill.classList.toggle("hidden", !data.collections.count);
    }
  }

  function updateBadge(count) {
    const badge = document.getElementById("cntOverdue");
    if (!badge) return;
    badge.textContent = count;
    badge.classList.toggle("hidden", !count);
  }

  async function loadInvoices() {
    const form = document.getElementById("salesFilter");
    const params = new URLSearchParams();
    if (form.elements.q.value.trim()) params.set("q", form.elements.q.value.trim());
    if (form.elements.status.value) params.set("status", form.elements.status.value);
    const [items, chain] = await Promise.all([
      window.apiRequest(`/sales/invoices?${params}`),
      window.apiRequest("/sales/chain"),
    ]);

    document.getElementById("salesChain").innerHTML = chain.records
      ? (chain.valid
        ? `${window.icon("shield")} Cadena de registros íntegra · ${window.pl(chain.records, "registro(s)")}`
        : `<span class="danger-text">${window.icon("alert")} Cadena rota en ${esc(chain.broken_at)}</span>`)
      : "";

    const body = document.querySelector("#salesTable tbody");
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="6">${window.emptyState("invoice", params.toString() ? "Sin resultados" : "Aún no has emitido facturas", params.toString() ? "Prueba con otro filtro." : "Crea tu primera factura: el agente la numera, la registra y la anota en el libro de emitidas.")}</td></tr>`;
      return;
    }

    body.innerHTML = items.map((item) => `
      <tr class="clickable-row" data-open-invoice="${item.id}">
        <td>
          <strong class="mono">${esc(item.code || "Borrador")}</strong>
          ${item.series === "R" ? `<span class="status-pill mini status-warning">Rectificativa</span>` : ""}
          ${item.recurring_id ? `<span class="status-pill mini status-neutral" title="Generada por una recurrente">${window.icon("repeat")}</span>` : ""}
        </td>
        <td>
          ${esc(item.customer_name || "Sin cliente")}
          <small class="muted block">${esc(item.concept || "")}</small>
        </td>
        <td>${day(item.issue_date)}</td>
        <td class="num"><strong>${money(item.total)}</strong></td>
        <td>
          <span class="status-pill ${COLLECTION[item.collection.state] || "status-neutral"}">${esc(item.collection.label)}</span>
          ${item.sent_at ? `<small class="muted block">${window.icon("send")} Enviada</small>` : ""}
        </td>
        <td class="actions-cell">
          ${item.status === "ISSUED" ? `<a class="btn-ghost" href="/api/sales/invoices/${item.id}/pdf" target="_blank" rel="noopener noreferrer" data-stop>PDF</a>` : ""}
          <button type="button" class="btn-ghost" data-open-invoice="${item.id}">${item.status === "DRAFT" ? "Editar" : "Abrir"}</button>
        </td>
      </tr>
    `).join("");
  }

  /* ------------------------------------------------------------
  DIÁLOGO DE FACTURA
  ------------------------------------------------------------ */
  async function openInvoice(id = null) {
    await loadCustomers();
    current = id ? await window.apiRequest(`/sales/invoices/${id}`) : null;
    const editable = !current || current.status === "DRAFT";
    const form = document.getElementById("invoiceForm");
    form.reset();

    const title = current?.code
      ? `${current.series === "R" ? "Factura rectificativa" : "Factura"} ${current.code}`
      : current?.series === "R" ? "Borrador de rectificativa" : current ? "Borrador de factura" : "Nueva factura";
    document.getElementById("invoiceDialogTitle").textContent = title;
    document.getElementById("invoiceDialogSub").textContent = current?.status === "ISSUED"
      ? `Emitida el ${day(current.issue_date)} · ${current.collection.label}${current.rectifies_code ? ` · rectifica ${current.rectifies_code}` : ""}`
      : "Se numera y registra al emitirla. Hasta entonces puedes cambiar lo que quieras.";

    document.getElementById("invoiceCustomer").innerHTML = customerOptions(current?.customer_id);
    form.elements.issue_date.value = current?.issue_date || "";
    form.elements.due_date.value = current?.due_date || "";
    form.elements.withholding_rate.value = current?.withholding_rate || "";
    form.elements.payment_terms.value = current?.payment_terms || "";
    form.elements.notes.value = current?.notes || "";
    form.elements.rectification_reason.value = current?.rectification_reason || "";
    document.getElementById("rectificationField").classList.toggle("hidden", current?.series !== "R");

    [...form.elements].forEach((element) => {
      if (element.tagName !== "BUTTON") element.disabled = !editable;
    });
    form.querySelector("[data-new-customer]").classList.toggle("hidden", !editable);
    if (editable && !current) form.elements.issue_date.placeholder = "Hoy";

    renderLineEditor(document.getElementById("invoiceLines"), current?.lines || [], editable, renderTotals);
    renderTotals();
    renderRecord();
    renderInvoiceActions();
    openDialog("invoiceDialog");
    if (editable && !current) document.getElementById("invoiceCustomer").focus();
  }

  function renderRecord() {
    const box = document.getElementById("invoiceRecord");
    const record = current?.record;
    box.classList.toggle("hidden", !record);
    if (!record) return;
    box.innerHTML = `
      <img src="/api/sales/invoices/${current.id}/qr.svg" alt="QR tributario de la factura" class="record-qr" width="96" height="96">
      <div class="record-body">
        <p class="record-title">${window.icon("shield")} Registro de facturación · ${esc(record.type_label || current.invoice_type)}</p>
        <dl>
          <dt>Huella</dt><dd class="mono">${esc(record.hash)}</dd>
          <dt>Anterior</dt><dd class="mono">${esc(record.previous_hash || "— primera factura de la cadena —")}</dd>
          <dt>Generado</dt><dd>${esc(record.timestamp)}</dd>
        </dl>
        <p class="detail-hint">Registro encadenado conforme al RD 1007/2023, preparado para su remisión. El envío a la AEAT (VERI*FACTU) se activa al conectar el certificado digital.</p>
      </div>
    `;
  }

  function renderInvoiceActions() {
    const actions = [];
    if (!current || current.status === "DRAFT") {
      if (current) actions.push(`<button type="button" class="btn-ghost danger-text" data-invoice-action="delete">${window.icon("trash")} Eliminar</button>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<button type="button" class="btn-ghost" data-invoice-action="preview">${window.icon("eye")} Vista previa</button>`);
      actions.push(`<button type="button" class="btn-ghost" data-invoice-action="save">Guardar borrador</button>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-invoice-action="issue">${window.icon("check")} Emitir factura</button>`);
    } else {
      actions.push(`<button type="button" class="btn-ghost" data-invoice-action="rectify">Rectificar</button>`);
      actions.push(`<button type="button" class="btn-ghost" data-invoice-action="duplicate">Duplicar</button>`);
      actions.push(`<span class="spacer"></span>`);
      if (current.collection.state === "pending" || current.collection.state === "overdue") {
        actions.push(`<button type="button" class="btn-ghost" data-invoice-action="paid">${window.icon("coins")} Marcar cobrada</button>`);
      }
      actions.push(`<a class="btn-ghost" href="/api/sales/invoices/${current.id}/pdf" target="_blank" rel="noopener noreferrer">${window.icon("doc")} PDF</a>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-invoice-action="send">${window.icon("send")} ${current.sent_at ? "Volver a enviar" : "Enviar al cliente"}</button>`);
    }
    document.getElementById("invoiceActions").innerHTML = actions.join("");
  }

  function invoicePayload() {
    const form = document.getElementById("invoiceForm");
    return {
      customer_id: Number(form.elements.customer_id.value) || null,
      issue_date: form.elements.issue_date.value || null,
      due_date: form.elements.due_date.value || null,
      withholding_rate: number(form.elements.withholding_rate.value),
      payment_terms: form.elements.payment_terms.value.trim() || null,
      notes: form.elements.notes.value.trim() || null,
      rectification_reason: form.elements.rectification_reason.value.trim() || null,
      lines: collectLines(document.getElementById("invoiceLines")),
    };
  }

  async function saveInvoice() {
    const payload = invoicePayload();
    current = current
      ? await window.jsonRequest(`/sales/invoices/${current.id}`, "PATCH", payload)
      : await window.jsonRequest("/sales/invoices", "POST", payload);
    return current;
  }

  async function invoiceAction(action) {
    try {
      if (action === "save") {
        await saveInvoice();
        window.showMessage("Borrador guardado.", "success");
        closeDialog("invoiceDialog");
      } else if (action === "preview") {
        await saveInvoice();
        window.open(`/api/sales/invoices/${current.id}/pdf`, "_blank", "noopener");
        renderInvoiceActions();
      } else if (action === "issue") {
        const payload = invoicePayload();
        if (!payload.customer_id) throw new Error("Elige un cliente.");
        if (!payload.lines.length) throw new Error("Añade al menos una línea.");
        await saveInvoice();
        const issued = await window.jsonRequest(`/sales/invoices/${current.id}/issue`, "POST", {});
        window.showMessage(`Factura ${issued.code} emitida y anotada en el libro de emitidas.`, "success");
        await openInvoice(issued.id);
      } else if (action === "delete") {
        if (!await window.askConfirm("¿Eliminar este borrador?")) return;
        await window.apiRequest(`/sales/invoices/${current.id}`, { method: "DELETE" });
        closeDialog("invoiceDialog");
        window.showMessage("Borrador eliminado.", "success");
      } else if (action === "send") {
        const message = await window.jsonRequest(`/sales/invoices/${current.id}/send`, "POST", {});
        closeDialog("invoiceDialog");
        window.showMessage(message.to_email
          ? "Email preparado en la bandeja de salida: revísalo y envíalo."
          : "Email preparado, pero el cliente no tiene email: complétalo en la bandeja de salida.", message.to_email ? "success" : "warning");
        window.openOutboxMessage?.(message.id);
      } else if (action === "paid") {
        await window.jsonRequest(`/invoices/${current.invoice_id}/payment`, "POST", { paid: true });
        window.showMessage("Cobro registrado.", "success");
        await openInvoice(current.id);
      } else if (action === "duplicate") {
        const copy = await window.jsonRequest(`/sales/invoices/${current.id}/duplicate`, "POST", {});
        await openInvoice(copy.id);
      } else if (action === "rectify") {
        const reason = await window.askText("Motivo de la rectificación (aparecerá en la factura):", { value: "Anulación de la factura original" });
        if (reason === null) return;
        const draft = await window.jsonRequest(`/sales/invoices/${current.id}/rectify`, "POST", { reason });
        window.showMessage("Borrador de rectificativa creado con los importes en negativo. Ajústalo y emítelo.", "success");
        await openInvoice(draft.id);
      }
      await refresh();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  CLIENTES
  ------------------------------------------------------------ */
  async function loadCustomersView() {
    await loadCustomers();
    const body = document.querySelector("#customersTable tbody");
    if (!customers.length) {
      body.innerHTML = `<tr><td colspan="6">${window.emptyState("users", "Aún no hay clientes", "Créalos a mano o impórtalos de las facturas emitidas que ya ha leído el agente.")}</td></tr>`;
      return;
    }
    body.innerHTML = customers.map((item) => `
      <tr class="clickable-row" data-edit-customer="${item.id}">
        <td><strong>${esc(item.name)}</strong><small class="muted block">${esc(item.tax_id || "Sin NIF")}${item.tax_id && item.tax_id_valid === false ? " · NIF no válido" : ""}</small></td>
        <td>${item.email ? esc(item.email) : `<span class="status-pill mini status-warning">Sin email</span>`}</td>
        <td class="num">${item.payment_days ?? 30} d</td>
        <td class="num">${money(item.invoiced)}</td>
        <td class="num">${item.pending ? `<strong>${money(item.pending)}</strong>` : "—"}</td>
        <td class="actions-cell"><button type="button" class="btn-ghost" data-edit-customer="${item.id}">Editar</button></td>
      </tr>
    `).join("");
  }

  function openCustomer(customer = null, callback = null) {
    currentCustomer = customer;
    customerCallback = callback;
    const form = document.getElementById("customerForm");
    form.reset();
    for (const [key, value] of Object.entries(customer || {})) {
      if (form.elements[key] && value !== null && value !== undefined) form.elements[key].value = value;
    }
    document.getElementById("customerDialogTitle").textContent = customer ? customer.name : "Nuevo cliente";
    document.getElementById("customerDelete").classList.toggle("hidden", !customer);
    openDialog("customerDialog");
    form.elements.name.focus();
  }

  async function saveCustomer(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {};
    for (const element of form.elements) {
      if (!element.name) continue;
      const value = element.value.trim();
      body[element.name] = ["payment_days", "withholding_rate"].includes(element.name)
        ? (value === "" ? null : number(value))
        : value || null;
    }
    try {
      const saved = currentCustomer
        ? await window.jsonRequest(`/sales/customers/${currentCustomer.id}`, "PATCH", body)
        : await window.jsonRequest("/sales/customers", "POST", body);
      closeDialog("customerDialog");
      window.showMessage("Cliente guardado.", "success");
      await loadCustomers();
      customerCallback?.(saved);
      if (view === "clientes") loadCustomersView();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  RECURRENTES
  ------------------------------------------------------------ */
  async function loadRecurring() {
    const items = await window.apiRequest("/sales/recurring");
    const container = document.getElementById("recurringList");
    if (!items.length) {
      container.innerHTML = window.emptyState("repeat", "Sin facturas recurrentes", "Configura una vez las cuotas que cobras cada mes y olvídate: el agente las genera el día que toca.");
      return;
    }
    container.innerHTML = items.map((item) => `
      <button type="button" class="recurring-item ${item.active ? "" : "is-paused"}" data-edit-recurring="${item.id}">
        <span class="recurring-icon">${window.icon("repeat")}</span>
        <span class="recurring-main">
          <strong>${esc(item.name)}</strong>
          <span>${esc(item.customer_name || "")} · ${esc(item.frequency_label)} · ${money(item.total)}</span>
        </span>
        <span class="recurring-meta">
          <span class="status-pill ${item.active ? (item.auto_issue ? "status-success" : "status-info") : "status-neutral"}">
            ${item.active ? (item.auto_issue ? (item.auto_send ? "Emite y prepara envío" : "Emite sola") : "Deja borrador") : "En pausa"}
          </span>
          <small>${item.active ? `Próxima: ${day(item.next_date)}` : ""}${item.generated_count ? ` · ${window.pl(item.generated_count, "generada(s)")}` : ""}</small>
        </span>
      </button>
    `).join("");
    container.dataset.items = JSON.stringify(items);
  }

  async function openRecurring(template = null) {
    await loadCustomers();
    currentRecurring = template;
    const form = document.getElementById("recurringForm");
    form.reset();
    document.getElementById("recurringCustomer").innerHTML = customerOptions(template?.customer_id);
    document.getElementById("recurringDialogTitle").textContent = template ? template.name : "Nueva factura recurrente";
    document.getElementById("recurringDelete").classList.toggle("hidden", !template);
    form.elements.name.value = template?.name || "";
    form.elements.frequency.value = template?.frequency || "MONTHLY";
    form.elements.next_date.value = template?.next_date || nextFirstOfMonth();
    form.elements.end_date.value = template?.end_date || "";
    form.elements.withholding_rate.value = template?.withholding_rate || "";
    form.elements.auto_issue.checked = template ? template.auto_issue : true;
    form.elements.auto_send.checked = template ? template.auto_send : false;
    form.elements.active.checked = template ? template.active : true;
    renderLineEditor(document.getElementById("recurringLines"), template?.lines || [{ description: "Cuota de mantenimiento {mes}", quantity: 1, vat_rate: 21 }], true);
    openDialog("recurringDialog");
  }

  function nextFirstOfMonth() {
    const now = new Date();
    const next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}-01`;
  }

  async function saveRecurring(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {
      name: form.elements.name.value.trim(),
      customer_id: Number(form.elements.customer_id.value) || null,
      frequency: form.elements.frequency.value,
      next_date: form.elements.next_date.value || null,
      end_date: form.elements.end_date.value || null,
      withholding_rate: number(form.elements.withholding_rate.value),
      auto_issue: form.elements.auto_issue.checked,
      auto_send: form.elements.auto_send.checked,
      active: form.elements.active.checked,
      lines: collectLines(document.getElementById("recurringLines")),
    };
    try {
      if (currentRecurring) await window.jsonRequest(`/sales/recurring/${currentRecurring.id}`, "PATCH", body);
      else await window.jsonRequest("/sales/recurring", "POST", body);
      closeDialog("recurringDialog");
      window.showMessage("Factura recurrente guardada. El agente la generará el día indicado.", "success");
      await refresh();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  COBROS
  ------------------------------------------------------------ */
  async function loadCollections() {
    const data = await window.apiRequest("/collections");
    const summary = data.summary;

    document.getElementById("collectionsHero").innerHTML = summary.count ? `
      <div class="collections-stat"><span>Vencido</span><strong>${money(summary.amount)}</strong><small>${window.pl(summary.count, "factura(s)")}</small></div>
      <div class="collections-stat"><span>Intereses de demora devengados</span><strong>${money(summary.interest)}</strong><small>Ley 3/2004</small></div>
      <div class="collections-stat"><span>Retraso medio ponderado</span><strong>${String(summary.weighted_days).replace(".", ",")} días</strong><small>por importe</small></div>
      <div class="collections-stat"><span>Reclamaciones por preparar</span><strong>${summary.pending_actions}</strong><small>${summary.pending_actions ? "el agente las redacta" : "al día"}</small></div>
    ` : `<div class="collections-ok">${window.icon("check")}<span><strong>Ningún cliente te debe dinero vencido.</strong> El agente revisa los vencimientos cada mañana a las 08:00.</span></div>`;

    document.getElementById("remindAllButton").disabled = !summary.pending_actions;

    const body = document.querySelector("#collectionsTable tbody");
    body.innerHTML = data.overdue.length ? data.overdue.map((row) => {
      const last = row.last_reminder;
      const lastText = last
        ? `${LEVELS[last.level] || "Aviso"} · ${last.status === "SENT" ? `enviado ${day(last.sent_at)}` : "pendiente de enviar"}`
        : "Ninguna aún";
      return `
        <tr>
          <td><strong class="mono">${esc(row.number || "—")}</strong><small class="muted block">venció ${day(row.due_date)}</small></td>
          <td>${esc(row.customer_name || "—")}${row.email ? "" : ` <span class="status-pill mini status-warning">sin email</span>`}${row.average_delay ? `<small class="muted block">suele pagar con ${row.average_delay} d de retraso</small>` : ""}</td>
          <td class="num"><span class="status-pill ${row.days_overdue >= 30 ? "status-danger" : row.days_overdue >= 15 ? "status-warning" : "status-neutral"}">${row.days_overdue} d</span></td>
          <td class="num"><strong>${money(row.total)}</strong></td>
          <td class="num">${money(row.interest)}${row.compensation && row.level_due >= 3 ? `<small class="muted block">+ ${money(row.compensation)} costes de cobro</small>` : ""}</td>
          <td><span class="${last?.status === "SENT" ? "" : "muted"}">${esc(lastText)}</span></td>
          <td class="actions-cell">
            ${row.next_action ? `<button type="button" class="act-btn act-primary" data-remind="${row.invoice_id}">Preparar ${esc(row.next_action.toLowerCase())}</button>` : ""}
            ${last && last.status !== "SENT" ? `<button type="button" class="btn-ghost" data-open-message="${last.id}">Revisar</button>` : ""}
            ${row.level_due >= 3 ? `<a class="btn-ghost" href="/api/collections/${row.invoice_id}/letter.pdf" target="_blank" rel="noopener noreferrer">Carta</a>` : ""}
            <button type="button" class="btn-ghost" data-mark-paid="${row.invoice_id}">Cobrada</button>
          </td>
        </tr>
      `;
    }).join("") : `<tr><td colspan="7" class="empty-cell">Sin facturas vencidas.</td></tr>`;

    document.getElementById("upcomingCollections").innerHTML = data.upcoming.length
      ? `<div class="plain-rows">${data.upcoming.map((row) => `
          <div class="plain-row">
            <span><strong>${esc(row.customer_name || "—")}</strong><small class="muted block">${esc(row.number || "")} · vence ${row.days_left === 0 ? "hoy" : `en ${row.days_left} d`}</small></span>
            <strong>${money(row.total)}</strong>
          </div>`).join("")}</div>`
      : `<p class="empty-inline">Nada vence en los próximos 7 días.</p>`;

    document.getElementById("customerRisk").innerHTML = data.customers.length
      ? `<div class="plain-rows">${data.customers.slice(0, 6).map((row) => `
          <div class="plain-row">
            <span><strong>${esc(row.customer_name || "—")}</strong><small class="muted block">${window.pl(row.count, "factura(s)")} · la más antigua ${row.max_days} d${row.average_delay ? ` · paga con ${row.average_delay} d de media` : ""}</small></span>
            <strong class="value-negative">${money(row.amount)}</strong>
          </div>`).join("")}</div>`
      : `<p class="empty-inline">Nadie te debe dinero vencido.</p>`;
  }

  async function remind(ids = null) {
    try {
      const result = await window.jsonRequest("/collections/remind", "POST", ids ? { invoice_ids: ids } : {});
      if (!result.created) {
        window.showMessage("No había reclamaciones nuevas que preparar.", "info");
      } else {
        window.showMessage(
          `${window.pl(result.created, "reclamación(es) preparada(s)")} en la bandeja de salida${result.without_email ? ` (${result.without_email} sin email del cliente)` : ""}.`,
          "success",
        );
      }
      await refresh();
      window.dispatchEvent(new CustomEvent("capafiscal:outbox-changed"));
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  VISTAS
  ------------------------------------------------------------ */
  function showView(name) {
    view = name;
    document.querySelectorAll("#salesViews .segment").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    document.querySelectorAll(".sales-view").forEach((item) => item.classList.toggle("hidden", item.dataset.salesView !== name));
    loadView().catch((error) => window.showMessage(error.message, "error"));
  }

  async function loadView() {
    if (view === "facturas") await loadInvoices();
    else if (view === "cobros") await loadCollections();
    else if (view === "recurrentes") await loadRecurring();
    else if (view === "clientes") await loadCustomersView();
  }

  async function refresh() {
    if (!active) {
      loadOverview().catch(() => {});
      return;
    }
    await Promise.all([loadOverview(), loadView()]);
  }

  function setup() {
    const section = document.getElementById("tab-ventas");
    if (!section) return;

    document.getElementById("salesViews").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (button) showView(button.dataset.view);
    });

    document.getElementById("newInvoiceButton").addEventListener("click", () => openInvoice().catch((error) => window.showMessage(error.message, "error")));
    document.getElementById("newCustomerButton").addEventListener("click", () => openCustomer());
    document.getElementById("newRecurringButton").addEventListener("click", () => openRecurring());
    document.getElementById("remindAllButton").addEventListener("click", () => remind());
    document.getElementById("importCustomersButton").addEventListener("click", async () => {
      try {
        const result = await window.jsonRequest("/sales/customers/import", "POST", {});
        window.showMessage(result.created ? `${window.pl(result.created, "cliente(s) importado(s)")}.` : "No había clientes nuevos en las facturas leídas.", "success");
        loadCustomersView();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("salesFilter").addEventListener("input", () => {
      window.clearTimeout(filterTimer);
      filterTimer = window.setTimeout(() => loadInvoices().catch((error) => window.showMessage(error.message, "error")), 250);
    });
    document.getElementById("salesFilter").addEventListener("submit", (event) => event.preventDefault());

    section.addEventListener("click", async (event) => {
      if (event.target.closest("[data-stop]")) return;
      const openButton = event.target.closest("[data-open-invoice]");
      if (openButton) return openInvoice(Number(openButton.dataset.openInvoice)).catch((error) => window.showMessage(error.message, "error"));

      const customerButton = event.target.closest("[data-edit-customer]");
      if (customerButton) return openCustomer(customers.find((item) => item.id === Number(customerButton.dataset.editCustomer)));

      const recurringButton = event.target.closest("[data-edit-recurring]");
      if (recurringButton) {
        const items = JSON.parse(document.getElementById("recurringList").dataset.items || "[]");
        return openRecurring(items.find((item) => item.id === Number(recurringButton.dataset.editRecurring)));
      }

      const remindButton = event.target.closest("[data-remind]");
      if (remindButton) return remind([Number(remindButton.dataset.remind)]);

      const messageButton = event.target.closest("[data-open-message]");
      if (messageButton) return window.openOutboxMessage?.(Number(messageButton.dataset.openMessage));

      const paidButton = event.target.closest("[data-mark-paid]");
      if (paidButton) {
        try {
          await window.jsonRequest(`/invoices/${paidButton.dataset.markPaid}/payment`, "POST", { paid: true });
          window.showMessage("Cobro registrado.", "success");
          await refresh();
        } catch (error) {
          window.showMessage(error.message, "error");
        }
      }
    });

    const invoiceForm = document.getElementById("invoiceForm");
    invoiceForm.addEventListener("submit", (event) => event.preventDefault());
    invoiceForm.elements.withholding_rate.addEventListener("input", renderTotals);
    invoiceForm.elements.customer_id.addEventListener("change", () => {
      const customer = customers.find((item) => item.id === Number(invoiceForm.elements.customer_id.value));
      if (customer?.withholding_rate && !invoiceForm.elements.withholding_rate.value) {
        invoiceForm.elements.withholding_rate.value = customer.withholding_rate;
        renderTotals();
      }
    });
    invoiceForm.querySelector("[data-new-customer]").addEventListener("click", () => {
      openCustomer(null, (saved) => {
        document.getElementById("invoiceCustomer").innerHTML = customerOptions(saved.id);
      });
    });
    document.getElementById("invoiceActions").addEventListener("click", (event) => {
      const button = event.target.closest("[data-invoice-action]");
      if (button) invoiceAction(button.dataset.invoiceAction);
    });

    document.getElementById("customerForm").addEventListener("submit", saveCustomer);
    document.getElementById("customerDelete").addEventListener("click", async () => {
      if (!currentCustomer || !await window.askConfirm(`¿Eliminar a ${currentCustomer.name}?`)) return;
      try {
        await window.apiRequest(`/sales/customers/${currentCustomer.id}`, { method: "DELETE" });
        closeDialog("customerDialog");
        loadCustomersView();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("recurringForm").addEventListener("submit", saveRecurring);
    document.getElementById("recurringForm").elements.auto_send.addEventListener("change", (event) => {
      if (event.target.checked) document.getElementById("recurringForm").elements.auto_issue.checked = true;
    });
    document.getElementById("recurringDelete").addEventListener("click", async () => {
      if (!currentRecurring || !await window.askConfirm("¿Eliminar esta factura recurrente? Las ya emitidas no se tocan.")) return;
      try {
        await window.apiRequest(`/sales/recurring/${currentRecurring.id}`, { method: "DELETE" });
        closeDialog("recurringDialog");
        await refresh();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    loadOverview().catch(() => {});
  }

  window.openSalesInvoice = (id) => { window.activateTab("ventas"); openInvoice(id); };
  window.newSalesInvoice = () => { window.activateTab("ventas"); openInvoice(); };

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "ventas";
    if (active) refresh().catch((error) => window.showMessage(error.message, "error"));
  });
  window.addEventListener("capafiscal:data-changed", () => refresh().catch(() => {}));

  document.addEventListener("DOMContentLoaded", setup);
})();
