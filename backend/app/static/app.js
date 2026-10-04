"use strict";
console.log("CapaFiscal frontend real V61");

const API_BASE = "/api";

// Botones generados en HTML: data-call="función" data-args="1, 'texto', this" en lugar de onclick.
// Sin manejadores en línea, la política de seguridad (CSP) puede prohibir todo script que no venga
// de /static: una inyección de HTML no puede ejecutar código.
const CALLABLE = new Set(["showDetail", "approveInvoice", "rejectInvoice", "activateTab", "openTask", "resolveTask", "cancelPayment", "markPaid",
  "saveInvoice", "reopenInvoice", "reprocessDocument", "markAsInvoice", "classifyDocument", "convertToNotification", "connectOutlook", "syncOutlook", "disconnectOutlook"]);
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-call]");
  const handler = target && CALLABLE.has(target.dataset.call) && window[target.dataset.call];
  if (typeof handler !== "function") return;
  const args = (target.dataset.args || "").split(",").map((part) => part.trim()).filter(Boolean)
    .map((part) => (part === "this" ? target : /^-?\d+(\.\d+)?$/.test(part) ? Number(part) : part.replace(/^'(.*)'$/, "$1")));
  handler(...args);
});

let documentsCache = [];
let tasksCache = [];
let categoriesCache = [];
let approvalInProgress = false;
let uploadInProgress = false;

const STANDARD_VAT_RATES = [21, 10, 5, 4, 0];

const PAYMENT_METHOD_LABELS = {
  TRANSFERENCIA: "Transferencia",
  DOMICILIACION: "Domiciliación",
  TARJETA: "Tarjeta",
  EFECTIVO: "Efectivo",
  CONFIRMING: "Confirming",
  OTRO: "Otro",
};

/* ================================================================
API
================================================================ */
class ApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

function apiUrl(path) {
  const value = String(path || "");
  if (value.startsWith("/api/")) return value;
  if (value === "/api") return value;
  if (value.startsWith("/")) return `${API_BASE}${value}`;
  return `${API_BASE}/${value}`;
}

async function apiRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(apiUrl(path), options);
  } catch (error) {
    throw new ApiError("No se ha podido conectar con el servidor.", 0, { originalError: error });
  }

  const contentType = response.headers.get("content-type") || "";
  let data = null;

  try {
    if (contentType.includes("application/json")) {
      data = await response.json();
    } else {
      const text = await response.text();
      data = text || null;
    }
  } catch (error) {
    console.error("Respuesta no interpretable:", error);
  }

  if (!response.ok) {
    const detail = data?.detail ?? data;
    let message = `Error HTTP ${response.status}`;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail) && detail[0]?.msg) message = detail.map((item) => item.msg).join(" · ");
    else if (detail?.message) message = detail.message;
    else if (data?.message) message = data.message;
    throw new ApiError(message, response.status, detail);
  }
  return data;
}

function jsonRequest(path, method, body) {
  return apiRequest(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

/* ================================================================
UTILIDADES
================================================================ */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value ?? "";
}

function formatMoney(value, currency = "EUR") {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return escapeHtml(value);
  try {
    return new Intl.NumberFormat("es-ES", {
      style: "currency",
      currency: currency || "EUR",
    }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency || "EUR"}`;
  }
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatDay(value) {
  if (!value) return "—";
  const [year, month, day] = String(value).slice(0, 10).split("-");
  if (!year || !month || !day) return escapeHtml(value);
  return `${day}/${month}/${year}`;
}

function dateInputValue(value) {
  return value ? String(value).slice(0, 10) : "";
}

function todayIso() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function inputValue(elementId) {
  const element = document.getElementById(elementId);
  if (!element) return null;
  const value = element.value.trim();
  return value || null;
}

/* «1.234,56», «1234,56» o «1234.56» → 1234.56; vacío → null; texto que no es un importe → NaN */
function parseAmount(text) {
  const value = String(text ?? "").replace(/[\s€]/g, "");
  if (!value) return null;
  const normalized = value.includes(",") ? value.replace(/\./g, "").replace(",", ".") : value;
  const number = Number(normalized);
  return Number.isFinite(number) ? number : NaN;
}

function nullableNumber(elementId) {
  const number = parseAmount(inputValue(elementId));
  return Number.isNaN(number) ? null : number;
}

/* Importe para escribir en un campo: coma decimal, sin separador de miles (lo que se teclea en España) */
function amountInputValue(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2).replace(".", ",") : String(value);
}

function normalizeList(data, keys = []) {
  if (Array.isArray(data)) return data;
  for (const key of keys) {
    if (Array.isArray(data?.[key])) return data[key];
  }
  return [];
}

function currentQuarter() {
  return Math.floor(new Date().getMonth() / 3) + 1;
}

function quarterRange(year, quarter) {
  const startMonth = (quarter - 1) * 3 + 1;
  const endMonth = startMonth + 2;
  const endDay = [3, 12].includes(endMonth) ? 31 : 30;
  const pad = (value) => String(value).padStart(2, "0");
  return {
    from: `${year}-${pad(startMonth)}-01`,
    to: `${year}-${pad(endMonth)}-${pad(endDay)}`,
  };
}

function translateStatus(status) {
  const statuses = {
    RECEIVED: "Recibido",
    PROCESSING: "Procesando",
    EXTRACTED: "Extraído",
    NEEDS_REVIEW: "Requiere revisión",
    READY_FOR_APPROVAL: "Pendiente de aprobación",
    APPROVED: "Aprobada",
    REJECTED: "Rechazada",
    FAILED: "Incidencia",
    EXPORTED: "Exportada",
    OPEN: "Abierta",
    PENDING: "Pendiente",
    IN_PROGRESS: "En curso",
    RESOLVED: "Resuelta",
    CANCELLED: "Cancelada",
    CLASSIFIED: "No es factura",
  };
  return statuses[String(status || "").toUpperCase()] || status || "Pendiente";
}

function statusClass(status) {
  const value = String(status || "").toUpperCase();
  if (["FAILED", "REJECTED", "CANCELLED"].includes(value)) return "status-danger";
  if (["RECEIVED", "PROCESSING", "EXTRACTED", "NEEDS_REVIEW", "READY_FOR_APPROVAL", "OPEN", "PENDING", "IN_PROGRESS"].includes(value)) {
    return "status-warning";
  }
  if (["APPROVED", "EXPORTED", "RESOLVED"].includes(value)) return "status-success";
  return "status-neutral";
}

function confidenceClass(confidence) {
  const value = Number(confidence || 0);
  if (value >= 90) return "risk-low";
  if (value >= 75) return "risk-medium";
  return "risk-high";
}

function paymentInfo(invoice) {
  if (!invoice?.id || invoice.review_status !== "APPROVED") return null;
  const issued = invoice.direction === "ISSUED";
  if (invoice.paid_at) {
    return { label: `${issued ? "Cobrada" : "Pagada"} ${formatDay(invoice.paid_at)}`, className: "status-success" };
  }
  if (invoice.due_date && invoice.due_date < todayIso()) {
    return { label: `Vencida ${formatDay(invoice.due_date)}`, className: "status-danger" };
  }
  if (invoice.due_date) {
    return { label: `Vence ${formatDay(invoice.due_date)}`, className: "status-warning" };
  }
  return { label: issued ? "Sin cobrar" : "Sin pagar", className: "status-neutral" };
}

/* Lo que no es factura ni notificación y el propio documento dice qué es (lo archiva el extractor). */
const OTHER_DOCUMENT_LABELS = { ALBARAN: "Albarán", PRESUPUESTO: "Presupuesto u oferta", PEDIDO: "Pedido", PROFORMA: "Proforma", NOMINA: "Nómina" };
const OTHER_DOCUMENT_PHRASES = { ALBARAN: "un albarán", PRESUPUESTO: "un presupuesto u oferta", PEDIDO: "un pedido", PROFORMA: "una factura proforma", NOMINA: "una nómina" };
function otherDocumentLabel(documentItem) {
  return OTHER_DOCUMENT_LABELS[documentItem?.kind] || "";
}
/* Facturas muestra facturas (y lo que aún se está leyendo); Documentos, el resto salvo las notificaciones. */
function isInvoiceDocument(documentItem) {
  return Boolean(documentItem?.invoice?.id) || ["RECEIVED", "PROCESSING"].includes(documentItem?.status);
}
function isUnidentifiedDocument(documentItem) {
  return documentItem?.kind !== "NOTIFICATION" && !otherDocumentLabel(documentItem) && !isInvoiceDocument(documentItem);
}
function unidentifiedReason(documentItem) {
  if (documentItem.extraction_status === "FAILED" || documentItem.status === "FAILED") return "No se pudo leer";
  if (documentItem.requires_ocr) return "Escaneado sin texto";
  return "No parece una factura";
}

function requiresReview(documentItem) {
  if (documentItem.kind === "NOTIFICATION" || otherDocumentLabel(documentItem)) return false;
  if (["APPROVED", "REJECTED", "EXPORTED", "RESOLVED"].includes(documentItem.status)) return false;
  const invoice = documentItem.invoice || {};
  const confidence = Number(invoice.confidence || 0);
  return (
    ["NEEDS_REVIEW", "FAILED"].includes(documentItem.status) ||
    !invoice.supplier_name ||
    !invoice.supplier_tax_id ||
    !invoice.invoice_number ||
    !invoice.invoice_date ||
    invoice.subtotal === null ||
    invoice.subtotal === undefined ||
    invoice.tax_total === null ||
    invoice.tax_total === undefined ||
    invoice.total === null ||
    invoice.total === undefined ||
    confidence < 80
  );
}

const EMOJI_ICONS = {
  "🔍": "search", "📄": "file", "📥": "inbox", "⚠️": "alert", "🗓️": "calendar",
  "💶": "coins", "✅": "check", "🏛️": "landmark", "📚": "activity", "⏳": "clock",
};

function icon(name, className = "") {
  return `<svg class="icon ${className}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function emptyState(symbol, title, text) {
  const iconName = EMOJI_ICONS[symbol] || symbol || "inbox";
  return `
    <div class="section-empty">
      <span class="section-empty-icon">${icon(iconName)}</span>
      <div>
        <p class="section-empty-title">${escapeHtml(title)}</p>
        <p class="section-empty-text">${escapeHtml(text)}</p>
      </div>
    </div>
  `;
}

function notifyActivityChanged() {
  window.dispatchEvent(new CustomEvent("capafiscal:activity-changed"));
}

async function refreshAll() {
  notifyActivityChanged();
  await Promise.all([loadDocuments(), loadTasks(), loadPanelSummary()]);
  window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));
}

/* ================================================================
PESTAÑAS
================================================================ */
function activateTab(tabName) {
  const buttons = document.querySelectorAll(".nav-tab");
  const panels = document.querySelectorAll(".tab-panel");

  buttons.forEach((item) => {
    const current = item.dataset.tab === tabName;
    item.classList.toggle("active", current);
    if (current) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  panels.forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tabName}`));

  if (tabName === "conectores") loadOutlookStatus();
  window.dispatchEvent(new CustomEvent("capafiscal:tab-changed", { detail: { tab: tabName } }));
}

function setupTabs() {
  document.querySelectorAll(".nav-tab").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });
}

/* ================================================================
CATEGORÍAS
================================================================ */
async function loadCategories() {
  try {
    categoriesCache = await apiRequest("/categories");
  } catch (error) {
    console.error("No se pudieron cargar categorías:", error);
    categoriesCache = [];
  }

  const select = document.getElementById("invoiceCategorySelect");
  if (select) {
    select.innerHTML = `<option value="">Todas</option>` + categoriesCache
      .map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`)
      .join("");
  }
}

/* ================================================================
DOCUMENTOS
================================================================ */
async function loadDocuments() {
  try {
    const data = await apiRequest("/documents?limit=500");
    documentsCache = normalizeList(data, ["documents", "items", "results"]);
    window.documentsCache = documentsCache;
    renderRecentDocuments(documentsCache);
    renderOpenRisks(documentsCache);

    // El menú cuenta exactamente lo mismo que la vista «Para revisar» de Facturas (misma regla),
    // incluidos los documentos que no se pudieron leer como factura. Sin «0» si no hay ninguno.
    const toReview = documentsCache.filter(INVOICE_VIEWS.review.test).length;
    const badge = document.getElementById("cntFacturas");
    setText("cntFacturas", String(toReview));
    badge?.classList.toggle("hidden", !toReview);
    badge?.setAttribute("title", window.pl(toReview, "documento(s) para revisar"));

    renderOtherDocuments();
    await loadFilteredDocuments();
    return documentsCache;
  } catch (error) {
    console.error("No se pudieron cargar documentos:", error);
    renderApiError("No se pudieron cargar los documentos", error.message);
    return [];
  }
}

/* ================================================================
DOCUMENTOS: presupuestos y ofertas, pedidos, albaranes, nóminas, proformas y lo que falta por identificar
================================================================ */
const DOCUMENT_KIND_VIEWS = [
  { key: "pending", label: "Por identificar", test: isUnidentifiedDocument },
  { key: "PRESUPUESTO", label: "Presupuestos y ofertas" },
  { key: "PEDIDO", label: "Pedidos" },
  { key: "ALBARAN", label: "Albaranes" },
  { key: "NOMINA", label: "Nóminas" },
  { key: "PROFORMA", label: "Proformas" },
  { key: "all", label: "Todos", test: (doc) => Boolean(otherDocumentLabel(doc)) || isUnidentifiedDocument(doc) },
].map((view) => ({ ...view, test: view.test || ((doc) => doc.kind === view.key) }));
let documentKindView = null;

function renderOtherDocuments() {
  const others = documentsCache.filter(DOCUMENT_KIND_VIEWS.at(-1).test);
  const pending = others.filter(isUnidentifiedDocument).length;
  const badge = document.getElementById("cntDocumentos");
  setText("cntDocumentos", String(pending));
  badge?.classList.toggle("hidden", !pending);
  badge?.setAttribute("title", window.pl(pending, "documento(s) por identificar"));

  const hint = document.getElementById("otherDocsHint");
  if (hint) {
    hint.classList.toggle("hidden", !others.length);
    hint.innerHTML = others.length
      ? `${escapeHtml(others.length === 1 ? "1 documento no es factura" : `${others.length} documentos no son facturas`)}: presupuestos, pedidos, albaranes, nóminas… <button type="button" class="link-button" data-call="activateTab" data-args="'documentos'">Ver en Documentos</button>`
      : "";
  }

  const bar = document.getElementById("docKindViews");
  const list = document.getElementById("otherDocumentsList");
  if (!bar || !list) return;
  const counts = Object.fromEntries(DOCUMENT_KIND_VIEWS.map((view) => [view.key, documentsCache.filter(view.test).length]));
  if (!documentKindView || (!counts[documentKindView] && documentKindView !== "all")) documentKindView = counts.pending ? "pending" : "all";
  bar.innerHTML = DOCUMENT_KIND_VIEWS
    .filter((view) => view.key === "all" || counts[view.key])
    .map((view) => `<button type="button" class="segment ${view.key === documentKindView ? "active" : ""}" data-doc-kind="${view.key}">${escapeHtml(view.label)} <span class="segment-count">${counts[view.key]}</span></button>`)
    .join("");

  const view = DOCUMENT_KIND_VIEWS.find((item) => item.key === documentKindView);
  const query = (document.getElementById("otherDocsSearch")?.value || "").trim().toLowerCase();
  const shown = documentsCache.filter(view.test)
    .filter((doc) => !query || `${doc.original_filename} ${otherDocumentLabel(doc)}`.toLowerCase().includes(query))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  setText("otherDocsSummary", others.length ? `${shown.length} de ${window.pl(others.length, "documento(s)")}.` : "");
  list.innerHTML = shown.length
    ? shown.map(realDocumentCard).join("")
    : others.length
      ? emptyState("check", "Nada aquí", "Ningún documento con ese nombre en esta vista.")
      : emptyState("📄", "Todavía no hay otros documentos", "Los presupuestos, pedidos, albaranes y nóminas que subas aparecerán aquí, cada uno en su sitio.");
}

function setupOtherDocuments() {
  document.getElementById("docKindViews")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-doc-kind]");
    if (!button) return;
    documentKindView = button.dataset.docKind;
    renderOtherDocuments();
  });
  document.getElementById("otherDocsSearch")?.addEventListener("input", () => renderOtherDocuments());
}

function setupInvoiceFilters() {
  const form = document.getElementById("invoiceFilters");
  const periodSelect = document.getElementById("invoicePeriodSelect");
  if (!form || !periodSelect) return;

  const year = new Date().getFullYear();
  const options = [`<option value="">Todas las fechas</option>`];
  for (const optionYear of [year, year - 1]) {
    for (let quarter = 4; quarter >= 1; quarter -= 1) {
      if (optionYear === year && quarter > currentQuarter()) continue;
      options.push(`<option value="${optionYear}-${quarter}">${quarter}T ${optionYear}</option>`);
    }
    options.push(`<option value="${optionYear}">Año ${optionYear}</option>`);
  }
  periodSelect.innerHTML = options.join("");

  let searchTimer = null;
  form.addEventListener("input", (event) => {
    window.clearTimeout(searchTimer);
    const delay = event.target.name === "q" ? 300 : 0;
    searchTimer = window.setTimeout(loadFilteredDocuments, delay);
  });
  form.addEventListener("reset", () => window.setTimeout(loadFilteredDocuments, 0));

  document.getElementById("reprocessPendingButton")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!await window.askConfirm("Se volverán a extraer todos los documentos pendientes (no aprobados ni rechazados). " +
      "Las correcciones manuales de esos documentos se sustituirán. ¿Continuar?", { confirmLabel: "Reprocesar pendientes" })) return;

    button.disabled = true;
    button.textContent = "Reprocesando…";
    try {
      const result = await apiRequest("/documents/reprocess-pending", { method: "POST" });
      showMessage(result.message, result.success ? "success" : "warning");
      await refreshAll();
    } catch (error) {
      showMessage(`No se pudo reprocesar: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Reprocesar pendientes";
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    loadFilteredDocuments();
  });
  document.getElementById("invoiceMoreFilters")?.addEventListener("click", (event) => {
    const open = form.closest(".filter-card").classList.toggle("filters-open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  document.getElementById("invoiceViews")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-invoice-view]");
    if (!button) return;
    invoiceView = button.dataset.invoiceView;
    form.reset();
    loadFilteredDocuments();
  });
}

function invoiceFilterParams() {
  const form = document.getElementById("invoiceFilters");
  const params = new URLSearchParams();
  if (!form) return params;

  const data = new FormData(form);
  for (const key of ["q", "direction", "review_status", "payment", "category"]) {
    const value = String(data.get(key) || "").trim();
    if (value) params.set(key, value);
  }

  const period = String(data.get("period") || "");
  if (period) {
    const [year, quarter] = period.split("-").map(Number);
    const range = quarter
      ? quarterRange(year, quarter)
      : { from: `${year}-01-01`, to: `${year}-12-31` };
    params.set("date_from", range.from);
    params.set("date_to", range.to);
  }

  return params;
}

async function loadFilteredDocuments() {
  const params = invoiceFilterParams();
  const summary = document.getElementById("invoiceFilterSummary");

  if (![...params.keys()].length) {
    renderInvoiceViews();
    const view = INVOICE_VIEWS[invoiceView] || INVOICE_VIEWS.all;
    const shown = documentsCache.filter(view.test);
    renderDocuments(shown, invoiceView !== "all");
    if (summary) summary.textContent = `${shown.length} de ${window.pl(documentsCache.filter(isInvoiceDocument).length, "factura(s)")}.`;
    return;
  }
  document.querySelectorAll("#invoiceViews .segment").forEach((item) => item.classList.remove("active"));  // con filtros manda el formulario

  params.set("limit", "500");

  try {
    const data = await apiRequest(`/documents?${params.toString()}`);
    const documents = normalizeList(data, ["documents"]);
    renderDocuments(documents, true);

    if (summary) {
      const total = documents.reduce((sum, item) => sum + Number(item.invoice?.total || 0), 0);
      summary.textContent = `${window.pl(documents.length, "resultado(s)")} · importe total ${formatMoney(total)}.`;
    }
  } catch (error) {
    renderDocuments([], true);
    if (summary) summary.textContent = `No se pudo filtrar: ${error.message}`;
  }
}

function renderDocuments(documents, filtered = false) {
  const container = document.getElementById("documentsList");
  if (!container) return;

  if (!documents.length) {
    container.innerHTML = filtered
      ? emptyState("check", "Nada aquí", "Ninguna factura en esta vista.")
      : emptyState("📄", "Todavía no hay documentos", "Sube o arrastra una factura PDF para comenzar.");
    return;
  }

  container.innerHTML = documents.map(realDocumentCard).join("");
}

function renderRecentDocuments(documents) {
  const container = document.getElementById("recentDocuments");
  if (!container) return;

  if (!documents.length) {
    container.innerHTML = emptyState("📥", "No hay documentos", "Sube o arrastra facturas a esta ventana para empezar.");
    return;
  }

  container.innerHTML = documents.slice(0, 5).map(realDocumentCard).join("");
}

/* Una factura, una fila: tercero, número y fecha, estado, importe y una sola acción.
   La lectura (confianza) solo aparece si es baja; el origen y el resto de campos están en el detalle. */
function realDocumentCard(documentItem) {
  const invoice = documentItem.invoice || {};
  const documentId = Number(documentItem.id);
  const confidence = Number(invoice.confidence || 0);
  if (documentItem.kind === "NOTIFICATION") return notificationDocumentCard(documentItem, "");
  if (otherDocumentLabel(documentItem)) return otherDocumentCard(documentItem);
  if (isUnidentifiedDocument(documentItem)) return unidentifiedDocumentCard(documentItem);

  const issued = invoice.direction === "ISSUED";
  const partyName = issued ? invoice.customer_name : invoice.supplier_name;
  const pending = invoice.review_status === "PENDING" || requiresReview(documentItem);
  const payment = paymentInfo(invoice);
  const issue = invoiceIssue(documentItem);
  const state = invoice.review_status === "REJECTED" ? { label: "Rechazada", className: "status-danger" }
    : pending ? { label: "Por revisar", className: "status-warning" } : payment;
  const meta = [invoice.invoice_number || "s/n", invoice.invoice_date ? formatDay(invoice.invoice_date) : null, issued ? "emitida" : null].filter(Boolean).join(" · ");

  return `
  <article class="invoice-row ${pending ? "needs-review" : ""}">
    <div class="invoice-row-main">
      <strong>${escapeHtml(partyName || (invoice.id ? (issued ? "Cliente sin identificar" : "Proveedor sin identificar") : documentItem.original_filename || "Documento"))}</strong>
      <span class="muted">${escapeHtml(meta)}</span>
      ${issue ? `<span class="invoice-row-issue">${escapeHtml(issue)}</span>` : ""}
      ${invoice.id && confidence && confidence < 80 ? `<span class="invoice-row-issue">Lectura dudosa (${confidence} %)</span>` : ""}
    </div>
    ${state ? `<span class="status-pill ${state.className}">${escapeHtml(state.label)}</span>` : "<span></span>"}
    <span class="invoice-row-amount">${invoice.total !== null && invoice.total !== undefined ? formatMoney(invoice.total, invoice.currency) : "—"}</span>
    <button class="${pending ? "act-btn" : "btn-ghost"}" type="button" data-call="showDetail" data-args="${documentId}">${pending ? "Revisar" : "Ver"}</button>
  </article>
  `;
}

/* Un albarán, presupuesto, pedido, proforma o nómina: qué es, de qué archivo y verlo. Sin importe ni «s/n»:
   no es una factura y no entra en la contabilidad. */
function otherDocumentCard(documentItem) {
  return `
  <article class="invoice-row">
    <div class="invoice-row-main">
      <strong>${escapeHtml(otherDocumentLabel(documentItem))}</strong>
      <span class="muted">${escapeHtml([documentItem.original_filename, documentItem.created_at ? formatDay(documentItem.created_at) : null].filter(Boolean).join(" · "))}</span>
    </div>
    <span class="status-pill status-neutral">No es factura</span>
    <span class="invoice-row-amount"></span>
    <button class="btn-ghost" type="button" data-call="showDetail" data-args="${Number(documentItem.id)}">Ver</button>
  </article>
  `;
}

/* Un documento que no se sabe qué es: a una persona, con el motivo. */
function unidentifiedDocumentCard(documentItem) {
  return `
  <article class="invoice-row needs-review">
    <div class="invoice-row-main">
      <strong>${escapeHtml(documentItem.original_filename || "Documento")}</strong>
      <span class="muted">${escapeHtml(documentItem.created_at ? formatDay(documentItem.created_at) : "")}</span>
      <span class="invoice-row-issue">${escapeHtml(unidentifiedReason(documentItem))}</span>
    </div>
    <span class="status-pill status-warning">Por identificar</span>
    <span class="invoice-row-amount"></span>
    <button class="act-btn" type="button" data-call="showDetail" data-args="${Number(documentItem.id)}">Revisar</button>
  </article>
  `;
}

/* Qué tiene de raro esta factura, en pocas palabras (o nada). */
function invoiceIssue(documentItem) {
  const invoice = documentItem.invoice || {};
  if (!invoice.id) return documentItem.extraction_status === "FAILED" ? "No se pudo leer" : "";
  if (invoice.duplicate_status && invoice.duplicate_status !== "NONE") return "Posible duplicado";
  if (invoice.validation_status && invoice.validation_status !== "VALID") return "Los importes no cuadran";
  if (!invoice.supplier_tax_id && invoice.direction !== "ISSUED") return "Sin NIF del proveedor";
  return "";
}

/* Vistas rápidas por estado de trabajo, sobre lo ya cargado (sin pedir nada al servidor). */
const INVOICE_VIEWS = {
  review: { label: "Para revisar", test: (doc) => isInvoiceDocument(doc) && (doc.invoice?.review_status === "PENDING" || requiresReview(doc)) },
  issues: { label: "Con incidencias", test: (doc) => isInvoiceDocument(doc) && Boolean(invoiceIssue(doc)) },
  unpaid: { label: "Sin pagar", test: (doc) => doc.invoice?.review_status === "APPROVED" && !doc.invoice?.paid_at },
  all: { label: "Todas", test: (doc) => isInvoiceDocument(doc) },
};
let invoiceView = null;

function renderInvoiceViews() {
  const bar = document.getElementById("invoiceViews");
  if (!bar) return;
  const counts = Object.fromEntries(Object.entries(INVOICE_VIEWS).map(([key, view]) => [key, documentsCache.filter(view.test).length]));
  if (!invoiceView) invoiceView = counts.review ? "review" : "all";
  bar.innerHTML = Object.entries(INVOICE_VIEWS)
    .filter(([key]) => key === "all" || key === invoiceView || counts[key])
    .map(([key, view]) => `<button type="button" class="segment ${key === invoiceView ? "active" : ""}" data-invoice-view="${key}">${escapeHtml(view.label)} <span class="segment-count">${counts[key]}</span></button>`)
    .join("");
}

function notificationDocumentCard(documentItem, sourceLabel) {
  const documentId = Number(documentItem.id);
  return `
  <article class="document-card notification-card">
    <div class="document-top">
      <div>
        <p class="doc-type">
          <span class="direction-tag direction-notification">Notificación</span>
          ${sourceLabel ? `<span class="source-tag">${escapeHtml(sourceLabel)}</span>` : ""}
        </p>
        <h3>${escapeHtml(documentItem.original_filename || "Notificación")}</h3>
        <p class="filename">Gestiónala en la pestaña Notificaciones.</p>
      </div>
      <div class="badges">
        <span class="status-pill ${statusClass(documentItem.status)}">${escapeHtml(translateStatus(documentItem.status))}</span>
      </div>
    </div>
    <div class="card-actions">
      <button class="btn-ghost" type="button" data-call="activateTab" data-args="'notificaciones'">Ver notificación</button>
      <a class="btn-ghost" href="/api/documents/${documentId}/file" target="_blank" rel="noopener noreferrer">Abrir archivo</a>
    </div>
  </article>
  `;
}

/* ================================================================
PANEL: RIESGOS, RECOMENDACIÓN, KPIs Y PAGOS
================================================================ */
function renderOpenRisks(documents) {
  const panel = document.getElementById("openRisksPanel");
  const text = document.getElementById("openRisksText");
  const list = document.getElementById("openRisksList");

  if (!panel || !text || !list) return;

  const risks = documents.filter(requiresReview);
  if (!risks.length) {
    panel.classList.add("hidden");
    list.innerHTML = "";
    return;
  }

  panel.classList.remove("hidden");
  text.textContent = `${window.pl(risks.length, "documento(s) requiere(n)")} revisión.`;

  list.innerHTML = risks
    .slice(0, 6)
    .map((doc) => {
      const invoice = doc.invoice || {};
      return `
        <div class="risk-row">
          <div>
            <strong>${escapeHtml(invoice.supplier_name || doc.original_filename || "Proveedor sin identificar")}</strong>
            <span>${formatMoney(invoice.total, invoice.currency)} · ${escapeHtml(translateStatus(doc.status))}</span>
          </div>
          <button type="button" class="btn-ghost" data-call="showDetail" data-args="${Number(doc.id)}">
            Revisar
          </button>
        </div>
      `;
    })
    .join("");
}

async function loadPanelSummary() {
  // Hoy es el Centro de trabajo (work.js): una sola lista. Esto solo pinta si siguen las tarjetas antiguas.
  if (!document.getElementById("agendaList")) return;
  const [paymentsResult, dashboardResult, agendaResult] = await Promise.allSettled([
    apiRequest("/payments"),
    apiRequest("/dashboard/today"),
    apiRequest("/agenda?horizon=30"),
  ]);

  const hour = new Date().getHours();
  const greeting = hour < 14 ? "Buenos días" : hour < 21 ? "Buenas tardes" : "Buenas noches";
  const today = new Date().toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "long" });
  setText("greetingLine", greeting);
  setText("panelPeriodNote", `${today.charAt(0).toUpperCase()}${today.slice(1)} · qué ha pasado, qué ha hecho CapaFiscal y qué te toca a ti.`);

  if (paymentsResult.status === "fulfilled") {
    renderPayments(paymentsResult.value);
  } else {
    const list = document.getElementById("paymentsList");
    if (list) list.innerHTML = emptyState("⚠️", "No se pudieron cargar los pagos", paymentsResult.reason?.message || "");
  }

  if (dashboardResult.status === "fulfilled") {
    renderRecommendation(dashboardResult.value.recommendation);
  }

  if (agendaResult.status === "fulfilled") {
    renderAgenda(agendaResult.value);
  }
}

const AGENDA_LEVELS = {
  overdue: ["Vencido", "status-danger"],
  critical: ["Urgente", "status-danger"],
  high: ["Esta semana", "status-warning"],
  normal: ["Próximo", "status-neutral"],
};

const AGENDA_ICONS = {
  tax: "receipt",
  notification: "landmark",
  collection: "coins",
  payment: "card",
  compliance: "shield",
  team: "users",
  payroll: "wallet",
  outbox: "send",
  timesheet: "clock",
};

function renderAgenda(agenda) {
  const container = document.getElementById("agendaList");
  const sub = document.getElementById("agendaSub");
  if (!container) return;

  const urgent = (agenda.counts?.overdue || 0) + (agenda.counts?.critical || 0);
  if (sub) sub.textContent = urgent ? `${window.pl(urgent, "asunto(s) urgente(s)")} · próximos 30 días` : "Próximos 30 días";

  if (!agenda.items.length) {
    container.innerHTML = emptyState("🗓️", "Nada vence en los próximos 30 días", "El agente te avisará en cuanto aparezca un plazo.");
    return;
  }

  container.innerHTML = agenda.items.slice(0, 10).map((item) => {
    const [label, className] = AGENDA_LEVELS[item.level] || AGENDA_LEVELS.normal;
    const days = Number(item.days_left);
    const when = days < 0 ? `hace ${Math.abs(days)} d` : days === 0 ? "hoy" : `en ${days} d`;
    const open = item.document_id && item.kind !== "notification"
      ? `data-call="showDetail" data-args="${Number(item.document_id)}${item.kind === "payment" || item.kind === "collection" ? ", 'payment'" : ""}"`
      : `data-call="activateTab" data-args="'${escapeHtml(item.tab)}'"`;

    return `
      <button type="button" class="agenda-item level-${escapeHtml(item.level)}" ${open}>
        <span class="agenda-icon kind-${escapeHtml(item.kind)}">${icon(AGENDA_ICONS[item.kind] || "calendar")}</span>
        <span class="agenda-body">
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(item.detail)}</span>
        </span>
        <span class="agenda-when">
          <span class="status-pill ${className}">${escapeHtml(label)}</span>
          <small>${formatDay(item.date)} · ${escapeHtml(when)}</small>
        </span>
      </button>
    `;
  }).join("");
}

function renderRecommendation(recommendation) {
  const card = document.getElementById("recommendationCard");
  if (!card || !recommendation) return;

  const severity = String(recommendation.severity || "LOW").toLowerCase();
  card.className = `recommendation-card severity-${escapeHtml(severity)}`;
  card.innerHTML = `
    <div class="recommendation-icon">${icon(severity === "low" ? "check" : "bulb")}</div>
    <div class="recommendation-body">
      <p class="recommendation-eyebrow">Siguiente paso recomendado</p>
      <h3>${escapeHtml(recommendation.title)}</h3>
      <p>${escapeHtml(recommendation.message)}</p>
      <p class="recommendation-action">${escapeHtml(recommendation.action)}</p>
    </div>
    ${
      recommendation.document_id
        ? `<button type="button" class="act-btn act-primary" data-call="showDetail" data-args="${Number(recommendation.document_id)}">Abrir documento</button>`
        : ""
    }
  `;
}

function renderPayments(payments) {
  const list = document.getElementById("paymentsList");
  const sub = document.getElementById("paymentsSub");

  setText("mUnpaid", formatMoney(payments.unpaid_total));
  setText(
    "mUnpaidLabel",
    payments.overdue_count
      ? `pendiente de pago · ${window.pl(payments.overdue_count, "vencida(s)")}`
      : "pendiente de pago"
  );

  if (sub) {
    sub.textContent = payments.unpaid_count
      ? `${window.pl(payments.unpaid_count, "factura(s)")} · ${formatMoney(payments.unpaid_total)}`
      : "Sin pagos pendientes";
  }

  if (!list) return;

  if (!payments.items.length) {
    list.innerHTML = emptyState("💶", "Todo pagado", "No hay facturas aprobadas pendientes de pago.");
    return;
  }

  const stateLabels = {
    OVERDUE: ["Vencida", "status-danger"],
    DUE_SOON: ["Vence pronto", "status-warning"],
    PENDING: ["Pendiente", "status-neutral"],
    NO_DUE_DATE: ["Sin vencimiento", "status-neutral"],
  };

  list.innerHTML = payments.items.slice(0, 8).map((item) => {
    const [label, className] = stateLabels[item.state] || ["Pendiente", "status-neutral"];
    let dueText = "Sin fecha de vencimiento";
    if (item.due_date) {
      const days = Number(item.days_to_due);
      if (days < 0) dueText = `Venció hace ${window.pl(Math.abs(days), "día(s)")} · ${formatDay(item.due_date)}`;
      else if (days === 0) dueText = "Vence hoy";
      else dueText = `Vence en ${window.pl(days, "día(s)")} · ${formatDay(item.due_date)}`;
    }

    return `
      <div class="payment-row">
        <div class="payment-main">
          <strong>${escapeHtml(item.supplier_name || "Proveedor sin identificar")}</strong>
          <span>${escapeHtml(item.invoice_number || "Sin número")} · ${escapeHtml(dueText)}</span>
        </div>
        <div class="payment-side">
          <strong>${formatMoney(item.total, item.currency)}</strong>
          <span class="status-pill ${className}">${escapeHtml(label)}</span>
        </div>
        <button type="button" class="btn-ghost" data-call="showDetail" data-args="${Number(item.document_id)}, 'payment'">
          Registrar pago
        </button>
      </div>
    `;
  }).join("");
}

/* ================================================================
TAREAS
================================================================ */
async function loadTasks() {
  const container = document.getElementById("inboxList");
  if (!container) return;

  try {
    const data = await apiRequest("/tasks/review-inbox");
    tasksCache = normalizeList(data, ["tasks", "items", "results"]);
    renderTasks(tasksCache);

    const sub = document.getElementById("reviewInboxSub");
    if (sub) sub.textContent = `${window.pl(tasksCache.length, "tarea(s) activa(s)")}`;
    setText("mRisks", tasksCache.length);
  } catch (error) {
    console.error("No se pudieron cargar tareas:", error);
    container.innerHTML = emptyState("⚠️", "No se pudo cargar la bandeja", error.message);
  }
}

function renderTasks(tasks) {
  const container = document.getElementById("inboxList");
  if (!container) return;

  if (!tasks.length) {
    container.innerHTML = emptyState("✅", "Bandeja al día", "No hay tareas de revisión pendientes.");
    return;
  }

  const priorityClass = { HIGH: "high", NORMAL: "medium", LOW: "low" };

  container.innerHTML = tasks.map((task) => {
    const priority = priorityClass[String(task.priority || "").toUpperCase()] || "medium";
    const invoice = task.document?.invoice || {};
    const party = invoice.direction === "ISSUED" ? invoice.customer_name : invoice.supplier_name;
    const title = task.task_type === "NOTIFICATION"
      ? `Notificación · ${task.document?.original_filename || ""}`
      : party || task.document?.original_filename || `Documento ${task.document_id}`;

    return `
      <article class="task-item priority-${priority}">
        <span class="task-dot"></span>
        <div class="task-body">
          <p class="task-label">${escapeHtml(title)}</p>
          <p class="task-narrative">
            ${escapeHtml(task.reason || "Revisar documento")}
            · ${escapeHtml(translateStatus(task.status))}
            ${invoice.total !== undefined && invoice.total !== null ? `· ${formatMoney(invoice.total, invoice.currency)}` : ""}
          </p>
          <div class="card-actions">
            ${
              task.task_type === "NOTIFICATION"
                ? `
                  <button type="button" class="btn-ghost" data-call="activateTab" data-args="'notificaciones'">
                    Ver notificación
                  </button>
                `
                : `
                  <button
                    type="button"
                    class="btn-ghost"
                    data-call="openTask" data-args="${Number(task.id)}, ${Number(task.document_id)}"
                  >
                    Abrir revisión
                  </button>
                  <button type="button" class="act-btn act-ghost" data-call="resolveTask" data-args="${Number(task.id)}">
                    Cerrar sin cambios
                  </button>
                `
            }
          </div>
        </div>
      </article>
    `;
  }).join("");
}

async function openTask(taskId, documentId) {
  try {
    await apiRequest(`/tasks/${taskId}/start`, { method: "POST" });
  } catch (error) {
    if (![409, 422].includes(error.status)) {
      showMessage(`No se pudo iniciar la tarea: ${error.message}`, "warning");
    }
  }
  await loadTasks();
  await showDetail(documentId);
}

async function resolveTask(taskId) {
  const note = await window.askText("Nota de resolución (opcional):", { value: "Revisión completada" });
  if (note === null) return;

  try {
    await jsonRequest(`/tasks/${taskId}/resolve`, "POST", {
      resolution: "REVIEWED",
      notes: note.trim() || null,
    });
    showMessage("Tarea resuelta.", "success");
    await refreshAll();
  } catch (error) {
    showMessage(`No se pudo resolver la tarea: ${error.message}`, "error");
  }
}

/* ================================================================
SUBIDA (varios archivos y arrastrar y soltar)
================================================================ */
const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;

function setupUpload() {
  const fileInput = document.getElementById("fileInput");
  // El botón es un <button> de verdad: se alcanza con el teclado y abre el selector de archivos
  document.getElementById("uploadTrigger")?.addEventListener("click", () => fileInput?.click());
  if (fileInput) {
    fileInput.addEventListener("change", async () => {
      const files = [...(fileInput.files || [])];
      fileInput.value = "";
      if (files.length) await uploadFiles(files);
    });
  }

  const overlay = document.getElementById("dropOverlay");
  let dragDepth = 0;

  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");

  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    overlay?.classList.remove("hidden");
  });

  window.addEventListener("dragover", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });

  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) overlay?.classList.add("hidden");
  });

  window.addEventListener("drop", async (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    overlay?.classList.add("hidden");
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length) await uploadFiles(files);
  });
}

async function uploadFiles(files) {
  if (uploadInProgress) {
    showMessage("Espera a que termine la carga en curso.", "warning");
    return;
  }

  const valid = [];
  for (const file of files) {
    const lowerName = file.name.toLowerCase();
    if (!lowerName.endsWith(".pdf") && !lowerName.endsWith(".txt")) {
      showMessage(`"${file.name}" no es PDF ni TXT y se ha omitido.`, "error");
    } else if (file.size > MAX_UPLOAD_BYTES) {
      showMessage(`"${file.name}" supera el límite de 15 MB.`, "error");
    } else {
      valid.push(file);
    }
  }

  if (!valid.length) return;

  uploadInProgress = true;
  const results = { ok: 0, duplicate: 0, review: 0, failed: 0 };
  let lastDocumentId = null;

  try {
    for (const [index, file] of valid.entries()) {
      showMessage(`Procesando ${index + 1}/${valid.length}: "${file.name}"…`, "info");

      const formData = new FormData();
      formData.append("uploaded_file", file, file.name);

      try {
        const result = await apiRequest("/upload", { method: "POST", body: formData });
        lastDocumentId = result?.document?.id ?? lastDocumentId;

        if (result.duplicate) results.duplicate += 1;
        else if (result.document?.status === "READY_FOR_APPROVAL") results.ok += 1;
        else results.review += 1;

        if (valid.length === 1) {
          showMessage(result.message || "Documento procesado.", result.duplicate ? "warning" : "success");
        }
      } catch (error) {
        results.failed += 1;
        showMessage(`No se pudo procesar "${file.name}": ${error.message}`, "error");
      }
    }
  } finally {
    uploadInProgress = false;
  }

  if (valid.length > 1) {
    showMessage(
      `Carga terminada: ${window.pl(results.ok, "lista(s)")} para aprobar, ${results.review} para revisar, ` +
      `${window.pl(results.duplicate, "duplicada(s)")}, ${results.failed} con error.`,
      results.failed ? "warning" : "success"
    );
  }

  await refreshAll();

  if (valid.length === 1 && lastDocumentId) await showDetail(lastDocumentId);
}

/* ================================================================
DETALLE (dialog)
================================================================ */
function ensureDetailDialog() {
  let dialog = document.getElementById("documentDetailDialog");
  if (dialog) return dialog;

  dialog = document.createElement("dialog");
  dialog.id = "documentDetailDialog";
  dialog.className = "document-detail-dialog sheet";  // panel lateral: el detalle sin perder la lista
  dialog.innerHTML = `
    <div class="detail-dialog-header">
      <h2 id="documentDetailTitle">Detalle de factura</h2>
      <button type="button" class="btn-ghost" id="closeDetailDialog">✕ Cerrar</button>
    </div>
    <div id="documentDetailContent"></div>
  `;

  document.body.appendChild(dialog);

  const requestClose = async () => {
    if (detailHasChanges() && !(await window.askConfirm("¿Descartar los cambios sin guardar de esta factura?"))) return;
    dialog.close();
  };
  document.getElementById("closeDetailDialog").addEventListener("click", requestClose);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) requestClose();
  });
  // «Más acciones»: Escape o un clic fuera lo cierran antes que el panel
  const openMenu = () => dialog.querySelector(".detail-more[open]");
  dialog.addEventListener("click", (event) => {
    const menu = openMenu();
    if (menu && !menu.contains(event.target)) menu.removeAttribute("open");
  });
  dialog.addEventListener("cancel", (event) => {
    const menu = openMenu();
    if (menu) {
      event.preventDefault();
      menu.removeAttribute("open");
      menu.querySelector("summary")?.focus();
      return;
    }
    if (!detailHasChanges()) return;
    event.preventDefault();
    requestClose();
  });

  return dialog;
}

function fieldConfidenceAttributes(invoice, fieldName) {
  const info = invoice.field_confidences?.[fieldName];
  if (!info || info.confidence === undefined || info.confidence === null) return "";
  const confidence = Number(info.confidence);
  const evidence = info.evidence ? ` · Evidencia: ${info.evidence}` : "";
  const title = `Confianza de extracción: ${confidence}%${evidence}`;
  const lowClass = confidence > 0 && confidence < 75 ? "field-lowconf" : "";
  return `title="${escapeHtml(title)}" class="${lowClass}"`;
}

function detailInput({ id, field, label, type = "text", value = "", invoice, extra = "", wide = false }) {
  const amount = type === "number";
  return `
    <label${wide ? ' class="span-2"' : ""}>
      ${escapeHtml(label)}
      <input
        id="${id}"
        data-invoice-field="${field}"
        type="${amount ? "text" : type}"
        ${amount ? 'inputmode="decimal" autocomplete="off" data-amount' : ""}
        value="${escapeHtml(amount ? amountInputValue(value) : (value ?? ""))}"
        ${fieldConfidenceAttributes(invoice, field)}
        ${extra}
      >
    </label>
  `;
}

/* Un importe que no se entiende no se guarda como vacío: se marca y se explica */
function checkAmountFields() {
  const invalid = [...document.querySelectorAll("#documentDetailContent input[data-amount]")]
    .filter((input) => Number.isNaN(parseAmount(input.value)));
  document.querySelectorAll("#documentDetailContent input[data-amount]").forEach((input) => {
    input.toggleAttribute("aria-invalid", invalid.includes(input));
    input.classList.toggle("field-missing", invalid.includes(input));
  });
  if (!invalid.length) return;
  invalid[0].focus();
  const label = invalid[0].closest("label")?.firstChild?.textContent.trim() || "Un importe";
  throw new Error(`«${label}» no es un importe válido (usa, por ejemplo, 1234,56)`);
}

function validationMessagesHtml(invoice) {
  const messages = Array.isArray(invoice.validation_messages) ? invoice.validation_messages : [];
  const duplicate = invoice.duplicate_status && invoice.duplicate_status !== "NONE";

  if (!messages.length && !duplicate) {
    return invoice.id
      ? `<div class="detail-check detail-check-ok">✓ Importes cuadrados y campos obligatorios completos.</div>`
      : "";
  }

  let duplicateHtml = "";
  if (duplicate) {
    const original = documentsCache.find((item) => item.invoice?.id === invoice.duplicate_of_invoice_id);
    duplicateHtml = `
      <li class="severity-error">
        ${invoice.duplicate_status === "STRONG"
          ? "Posible duplicado: mismo NIF y número de factura que otra ya registrada."
          : "Posible duplicado: mismo proveedor, fecha e importe que otra factura."}
        ${original ? `<button type="button" class="link-button" data-call="showDetail" data-args="${Number(original.id)}">Ver la otra factura</button>` : ""}
      </li>
    `;
  }

  return `
    <div class="detail-check">
      <span>Comprobaciones</span>
      <ul>
        ${duplicateHtml}
        ${messages.map((item) => `
          <li class="severity-${escapeHtml(item.severity || "warning")}">${escapeHtml(item.message || item.code)}</li>
        `).join("")}
      </ul>
    </div>
  `;
}

function taxLinesHtml(invoice) {
  const lines = Array.isArray(invoice.tax_lines) ? invoice.tax_lines : [];
  if (!lines.length) return "";

  return `
    <div class="detail-section">
      <h3>Desglose de impuestos</h3>
      <table class="data-table compact">
        <thead><tr><th>Impuesto</th><th class="num">Tipo</th><th class="num">Base</th><th class="num">Cuota</th></tr></thead>
        <tbody>
          ${lines.map((line) => `
            <tr>
              <td>${escapeHtml(line.tax_type || "IVA")}</td>
              <td class="num">${line.tax_rate !== null && line.tax_rate !== undefined ? `${Number(line.tax_rate)} %` : "—"}</td>
              <td class="num">${formatMoney(line.tax_base, invoice.currency)}</td>
              <td class="num">${formatMoney(line.tax_amount, invoice.currency)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function paymentSectionHtml(invoice, documentId) {
  if (!invoice.id || invoice.review_status !== "APPROVED") return "";

  const issued = invoice.direction === "ISSUED";
  const noun = issued ? "cobro" : "pago";

  if (invoice.paid_at) {
    return `
      <div class="detail-section payment-box paid" id="paymentSection">
        <h3>${issued ? "Cobro" : "Pago"}</h3>
        <p>
          ${issued ? "Cobrada" : "Pagada"} el <strong>${formatDay(invoice.paid_at)}</strong>
          ${invoice.payment_method ? ` · ${escapeHtml(PAYMENT_METHOD_LABELS[invoice.payment_method] || invoice.payment_method)}` : ""}
        </p>
        <button type="button" class="btn-ghost" data-call="cancelPayment" data-args="${Number(invoice.id)}, ${documentId}">
          Anular ${noun}
        </button>
      </div>
    `;
  }

  return `
    <div class="detail-section payment-box" id="paymentSection">
      <h3>Registrar ${noun}</h3>
      <div class="payment-form">
        <label>
          Fecha de ${noun}
          <input type="date" id="paymentDate" value="${todayIso()}">
        </label>
        <label>
          Forma de ${noun}
          <select id="paymentMethod">
            ${Object.entries(PAYMENT_METHOD_LABELS).map(([value, label]) => `
              <option value="${value}">${escapeHtml(label)}</option>
            `).join("")}
          </select>
        </label>
        <button type="button" class="act-btn act-primary" data-call="markPaid" data-args="${Number(invoice.id)}, ${documentId}">
          Marcar como ${issued ? "cobrada" : "pagada"}
        </button>
      </div>
      <p class="detail-hint">Si importas el extracto del banco, el agente lo concilia por ti en la pestaña Negocio.</p>
    </div>
  `;
}

const AUDIT_LABELS = {
  "document.uploaded": "Documento cargado",
  "document.processing.started": "Extracción iniciada",
  "document.processing.completed": "Extracción completada",
  "document.processing.failed": "Extracción fallida",
  "document.reprocess.requested": "Reprocesamiento solicitado",
  "document.duplicate_upload_attempt": "Se intentó subir de nuevo el mismo archivo",
  "document.not_identified_as_invoice": "No identificado como factura",
  "document.approved_with_warnings": "Aprobado con advertencias",
  "invoice.updated": "Datos corregidos",
  "invoice.approved": "Factura aprobada",
  "invoice.approved_with_warnings": "Aprobada con advertencias",
  "invoice.rejected": "Factura rechazada",
  "invoice.reopened": "Factura reabierta",
  "invoice.paid": "Pago/cobro registrado",
  "invoice.payment_cancelled": "Pago/cobro anulado",
  "bank.reconciled": "Conciliado con el banco",
  "bank.unreconciled": "Conciliación deshecha",
  "supplier_rule.learned": "El agente aprendió la categoría",
};

async function loadDetailHistory(documentId) {
  const container = document.getElementById("detailHistory");
  if (!container) return;

  try {
    const events = await apiRequest(`/documents/${documentId}/audit`);
    if (!events.length) {
      container.innerHTML = `<p class="empty-inline">Sin historial.</p>`;
      return;
    }

    container.innerHTML = `
      <ul class="timeline">
        ${events.slice(0, 15).map((event) => {
          const data = event.event_data || {};
          let extra = "";
          if (event.action === "invoice.updated" && data.changed_fields) {
            const fields = Object.keys(data.changed_fields);
            if (fields.length) extra = ` (${fields.join(", ")})`;
          }
          if (data.reason) extra = ` — ${data.reason}`;
          return `
            <li>
              <span class="tl-time">${formatDate(event.created_at)}</span>
              <span class="tl-label">${escapeHtml(AUDIT_LABELS[event.action] || event.action)}${escapeHtml(extra)} · <em>${escapeHtml(event.actor)}</em></span>
            </li>
          `;
        }).join("")}
      </ul>
    `;
  } catch (error) {
    container.innerHTML = `<p class="empty-inline">No se pudo cargar el historial: ${escapeHtml(error.message)}</p>`;
  }
}

async function showDetail(documentId, focus = null) {
  const dialog = ensureDetailDialog();
  const content = document.getElementById("documentDetailContent");
  content.innerHTML = "<p>Cargando documento…</p>";

  if (!dialog.open) dialog.showModal();

  try {
    const documentItem = await apiRequest(`/documents/${documentId}`);
    const invoice = documentItem.invoice || {};
    const invoiceId = Number(invoice.id);
    const docId = Number(documentItem.id);

    const canReview = Boolean(invoice.id && !["APPROVED", "REJECTED"].includes(documentItem.status));
    const canReopen = Boolean(invoice.id && ["APPROVED", "REJECTED"].includes(invoice.review_status));
    const editable = Boolean(invoice.id && invoice.review_status !== "APPROVED");
    const readonly = editable ? "" : "disabled";

    const otherLabel = otherDocumentLabel(documentItem);
    setText(
      "documentDetailTitle",
      invoice.supplier_name ? `${invoice.supplier_name}` : (otherLabel || documentItem.original_filename || "Detalle de documento")
    );

    const categoryOptions = categoriesCache.map((item) => `
      <option value="${escapeHtml(item.name)}" ${item.name === invoice.category ? "selected" : ""}>
        ${escapeHtml(item.name)} (${escapeHtml(item.account)})
      </option>
    `).join("");

    const fileUrl = `/api/documents/${docId}/file`;
    const payment = paymentInfo(invoice);
    const confidence = Number(invoice.confidence || 0);
    const headMeta = invoice.id ? [
      `Factura ${invoice.invoice_number || "sin número"}`,
      invoice.invoice_date ? formatDay(invoice.invoice_date) : null,
      invoice.direction === "ISSUED" ? "emitida (ingreso)" : "recibida (gasto)",
    ].filter(Boolean).join(" · ") : "";
    const breakdown = [
      invoice.subtotal !== null && invoice.subtotal !== undefined ? `Base ${formatMoney(invoice.subtotal, invoice.currency)}` : null,
      invoice.tax_total !== null && invoice.tax_total !== undefined ? `IVA ${formatMoney(invoice.tax_total, invoice.currency)}` : null,
      Number(invoice.withholding_total) ? `Retención ${formatMoney(invoice.withholding_total, invoice.currency)}` : null,
    ].filter(Boolean).join(" · ");

    content.innerHTML = `
      <header class="detail-head">
        <div class="detail-head-main">
          ${headMeta ? `<p class="detail-head-meta">${escapeHtml(headMeta)}</p>` : ""}
          <p class="detail-head-state">
            <span class="status-pill ${statusClass(documentItem.status)}">${escapeHtml(translateStatus(documentItem.status))}</span>
            ${payment ? `<span class="status-pill ${payment.className}">${escapeHtml(payment.label)}</span>` : ""}
            ${invoice.id ? `<span class="detail-confidence ${confidence && confidence < 80 ? "is-low" : ""}" title="Seguridad con la que CapaFiscal ha leído los datos del documento">Lectura ${confidence} %</span>` : ""}
          </p>
          <p class="detail-file">${escapeHtml(documentItem.original_filename || "—")} · ${formatDate(documentItem.created_at)}
            <a class="link-button detail-see-doc" href="#documentPreview">Ver documento</a></p>
        </div>
        ${invoice.id ? `
          <div class="detail-head-total">
            <span class="detail-head-total-label">Total</span>
            <strong>${invoice.total !== null && invoice.total !== undefined ? formatMoney(invoice.total, invoice.currency) : "—"}</strong>
            ${breakdown ? `<span>${escapeHtml(breakdown)}</span>` : ""}
          </div>` : ""}
      </header>

      <div class="detail-layout">
        <div class="detail-main">

          ${documentItem.failure_reason ? `<div class="detail-check"><span>Error de extracción</span><p>${escapeHtml(documentItem.failure_reason)}</p></div>` : ""}
          ${documentItem.requires_ocr ? `<div class="detail-check"><span>OCR necesario</span><p>El documento no tiene texto seleccionable suficiente. Revisa la vista previa y completa los datos manualmente.</p></div>` : ""}
          ${invoice.rejection_reason ? `<div class="detail-check"><span>Motivo del rechazo</span><p>${escapeHtml(invoice.rejection_reason)}</p></div>` : ""}

          ${invoice.id ? validationMessagesHtml(invoice) : otherLabel ? `
            <div class="detail-check">
              <span>${escapeHtml(otherLabel)}, no factura</span>
              <p>Por su título o su contenido, es ${escapeHtml(OTHER_DOCUMENT_PHRASES[documentItem.kind] || otherLabel.toLowerCase())}. Queda archivado aquí y no entra en la contabilidad ni en el IVA. Si en realidad es una factura, elige «Es una factura» en «Más acciones».</p>
            </div>
          ` : `
            <div class="detail-check">
              <span>Sin factura</span>
              <p>El documento no se ha identificado como factura. Puedes reprocesarlo o revisarlo en la vista previa.</p>
            </div>
          `}

          ${invoice.id ? `
            <fieldset class="detail-form-grid" ${readonly}>
              <p class="span-2 form-section">Quién la emite y quién la recibe</p>
              ${detailInput({ id: "detailSupplierName", field: "supplier_name", label: "Emisor (proveedor)", value: invoice.supplier_name, invoice })}
              ${detailInput({ id: "detailSupplierTaxId", field: "supplier_tax_id", label: "NIF/CIF proveedor", value: invoice.supplier_tax_id, invoice })}
              ${detailInput({ id: "detailCustomerName", field: "customer_name", label: "Destinatario (cliente)", value: invoice.customer_name, invoice })}
              ${detailInput({ id: "detailCustomerTaxId", field: "customer_tax_id", label: "NIF/CIF cliente", value: invoice.customer_tax_id, invoice })}

              <p class="span-2 form-section">Factura</p>
              ${detailInput({ id: "detailInvoiceNumber", field: "invoice_number", label: "Número de factura", value: invoice.invoice_number, invoice })}
              <label>
                Tipo de factura
                <select id="detailDirection" data-invoice-field="direction">
                  <option value="RECEIVED" ${invoice.direction !== "ISSUED" ? "selected" : ""}>Recibida (gasto)</option>
                  <option value="ISSUED" ${invoice.direction === "ISSUED" ? "selected" : ""}>Emitida (ingreso)</option>
                </select>
              </label>
              ${detailInput({ id: "detailInvoiceDate", field: "invoice_date", label: "Fecha de factura", type: "date", value: dateInputValue(invoice.invoice_date), invoice })}
              ${detailInput({ id: "detailDueDate", field: "due_date", label: "Fecha de vencimiento", type: "date", value: dateInputValue(invoice.due_date), invoice })}
              <label class="span-2">
                Categoría
                <select id="detailCategory" data-invoice-field="category">
                  <option value="">Sin categoría</option>
                  ${categoryOptions}
                </select>
              </label>
              ${detailInput({ id: "detailCurrency", field: "currency", label: "Moneda", value: invoice.currency || "EUR", invoice, extra: 'maxlength="3"' })}
              <label class="span-2">
                Concepto
                <textarea id="detailConcept" data-invoice-field="concept" rows="3">${escapeHtml(invoice.concept || "")}</textarea>
              </label>

              <p class="span-2 form-section">Importes</p>
              ${detailInput({ id: "detailSubtotal", field: "subtotal", label: "Base imponible", type: "number", value: invoice.subtotal, invoice })}
              ${detailInput({ id: "detailTaxTotal", field: "tax_total", label: "IVA", type: "number", value: invoice.tax_total, invoice })}
              ${detailInput({ id: "detailWithholding", field: "withholding_total", label: "Retención IRPF", type: "number", value: invoice.withholding_total, invoice })}
              ${detailInput({ id: "detailSurcharge", field: "surcharge_total", label: "Recargo de equivalencia", type: "number", value: invoice.surcharge_total, invoice })}
              ${detailInput({ id: "detailTotal", field: "total", label: "Total", type: "number", value: invoice.total, invoice })}
              <p class="span-2 amount-check" id="amountCheck"></p>
            </fieldset>
            ${!editable ? `<p class="detail-hint">La factura está aprobada. Reábrela para corregir datos.</p>` : ""}
          ` : ""}

          ${taxLinesHtml(invoice)}
          ${paymentSectionHtml(invoice, docId)}

          <details class="detail-section detail-history">
            <summary>Historial</summary>
            <div id="detailHistory"><p class="empty-inline">Cargando…</p></div>
          </details>
        </div>

        <div class="detail-preview" id="documentPreview">
          <iframe
            src="${[".pdf", ".txt"].includes(String(documentItem.extension || "").toLowerCase()) ? fileUrl : `${fileUrl}?as_text=true`}"
            title="Vista previa de ${escapeHtml(documentItem.original_filename || "documento")}"
            loading="lazy"
          ></iframe>
        </div>
      </div>

      <div class="detail-actions-bar">
        ${editable ? `
          <p class="detail-dirty hidden" id="detailDirty" role="status">
            Cambios sin guardar${canReview ? ": «Aprobar» los guarda antes de aprobar" : ""}
          </p>
        ` : ""}
        <div class="detail-actions">
          ${canReview ? `
            <button type="button" class="act-btn act-primary" data-call="approveInvoice" data-args="${invoiceId}, ${docId}, this">Aprobar</button>
            <button type="button" class="btn-ghost" data-call="rejectInvoice" data-args="${invoiceId}, ${docId}">Rechazar</button>
          ` : ""}
          ${editable ? `
            <button type="button" class="${canReview ? "btn-ghost hidden" : "act-btn act-primary"}" id="detailSaveButton"
              ${canReview ? "data-only-dirty" : ""} data-call="saveInvoice" data-args="${invoiceId}, ${docId}">Guardar cambios</button>
          ` : ""}
          ${canReopen ? `
            <button type="button" class="btn-ghost" data-call="reopenInvoice" data-args="${invoiceId}, ${docId}">Reabrir</button>
          ` : ""}
          <details class="detail-more">
            <summary class="btn-ghost">Más acciones</summary>
            <div class="detail-more-menu">
              ${invoice.review_status !== "APPROVED" ? `
                <button type="button" class="btn-ghost" data-call="reprocessDocument" data-args="${docId}">Volver a leer el documento</button>
                ${otherLabel || !invoice.id ? `<button type="button" class="btn-ghost" data-call="markAsInvoice" data-args="${docId}">Es una factura</button>` : ""}
                ${Object.entries(OTHER_DOCUMENT_PHRASES).filter(([kind]) => kind !== documentItem.kind).map(([kind, phrase]) => `
                  <button type="button" class="btn-ghost" data-call="classifyDocument" data-args="${docId}, '${kind}'">Es ${escapeHtml(phrase)}</button>`).join("")}
                <button type="button" class="btn-ghost" data-call="convertToNotification" data-args="${docId}">Es una notificación</button>
              ` : ""}
              <a class="btn-ghost" href="${fileUrl}" target="_blank" rel="noopener noreferrer">Abrir el archivo original</a>
            </div>
          </details>
        </div>
      </div>
    `;

    setupAmountCheck();
    setupDirtyTracking(invoiceId, editable);
    loadDetailHistory(docId);

    if (focus === "payment") {
      document.getElementById("paymentSection")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } catch (error) {
    detailEdit = null;
    content.innerHTML = emptyState("⚠️", "No se pudo abrir el documento", error.message);
  }
}

function setupAmountCheck() {
  const ids = ["detailSubtotal", "detailTaxTotal", "detailWithholding", "detailSurcharge", "detailTotal"];
  const output = document.getElementById("amountCheck");
  if (!output) return;

  const update = () => {
    const subtotal = nullableNumber("detailSubtotal");
    const tax = nullableNumber("detailTaxTotal");
    const total = nullableNumber("detailTotal");

    if (subtotal === null || tax === null || total === null) {
      output.textContent = "";
      output.className = "span-2 amount-check";
      return;
    }

    const expected = subtotal + tax + (nullableNumber("detailSurcharge") || 0) - (nullableNumber("detailWithholding") || 0);
    const difference = Math.abs(expected - total);
    const balanced = difference <= 0.02;

    output.textContent = balanced
      ? `✓ Cuadra: base + IVA + recargo − retención = ${formatMoney(expected)}`
      : `✗ No cuadra: el cálculo da ${formatMoney(expected)} y el total indicado es ${formatMoney(total)} (diferencia ${formatMoney(difference)})`;
    output.className = `span-2 amount-check ${balanced ? "ok" : "error"}`;
  };

  ids.forEach((id) => document.getElementById(id)?.addEventListener("input", update));
  update();
}

function snapVatRate(rate) {
  for (const standard of STANDARD_VAT_RATES) {
    if (Math.abs(rate - standard) <= 0.3) return standard;
  }
  return Math.round(rate * 100) / 100;
}

/* Cambios sin guardar en el detalle: «Aprobar» los guarda antes de aprobar y al cerrar se pregunta. */
let detailEdit = null;  // { invoiceId, snapshot }

function detailFormValues() {
  return JSON.stringify([...document.querySelectorAll("#documentDetailContent [data-invoice-field]")]
    .map((element) => [element.dataset.invoiceField, element.value.trim()]));
}

function detailHasChanges(invoiceId = null) {
  if (!detailEdit || !document.getElementById("documentDetailDialog")?.open) return false;
  if (invoiceId !== null && Number(invoiceId) !== detailEdit.invoiceId) return false;
  return detailFormValues() !== detailEdit.snapshot;
}

function setupDirtyTracking(invoiceId, editable) {
  detailEdit = editable && invoiceId ? { invoiceId: Number(invoiceId), snapshot: detailFormValues() } : null;
  const fieldset = document.querySelector("#documentDetailContent .detail-form-grid");
  const notice = document.getElementById("detailDirty");
  if (!detailEdit || !fieldset || !notice) return;
  const save = document.getElementById("detailSaveButton");
  const concept = document.getElementById("detailConcept");
  const update = () => {
    const dirty = detailHasChanges();
    notice.classList.toggle("hidden", !dirty);
    if (save?.hasAttribute("data-only-dirty")) save.classList.toggle("hidden", !dirty);
  };
  // El concepto crece con su texto en lugar de cortarse a tres líneas
  const fit = () => { if (concept) { concept.style.height = "auto"; concept.style.height = `${concept.scrollHeight + 2}px`; } };
  concept?.addEventListener("input", fit);
  fit();
  fieldset.addEventListener("input", update);
  fieldset.addEventListener("change", update);
}

function invoiceEditBody(documentId) {
  const body = {
    supplier_name: inputValue("detailSupplierName"),
    supplier_tax_id: inputValue("detailSupplierTaxId"),
    customer_name: inputValue("detailCustomerName"),
    customer_tax_id: inputValue("detailCustomerTaxId"),
    invoice_number: inputValue("detailInvoiceNumber"),
    invoice_date: inputValue("detailInvoiceDate"),
    due_date: inputValue("detailDueDate"),
    subtotal: nullableNumber("detailSubtotal"),
    tax_total: nullableNumber("detailTaxTotal"),
    withholding_total: nullableNumber("detailWithholding"),
    surcharge_total: nullableNumber("detailSurcharge"),
    total: nullableNumber("detailTotal"),
    currency: (inputValue("detailCurrency") || "EUR").toUpperCase(),
    category: inputValue("detailCategory"),
    concept: inputValue("detailConcept"),
    direction: inputValue("detailDirection") || "RECEIVED",
  };

  // Con un único tipo de IVA, el desglose se recalcula a partir de base
  // y cuota para que el informe por tipos refleje la corrección.
  const current = documentsCache.find((item) => item.id === documentId)?.invoice;
  const lineCount = current?.tax_lines?.length ?? 0;
  if (lineCount <= 1 && body.subtotal && body.tax_total !== null) {
    body.tax_lines = [{
      tax_type: "IVA",
      tax_rate: snapVatRate((body.tax_total / body.subtotal) * 100),
      tax_base: body.subtotal,
      tax_amount: body.tax_total,
      source: "manual_review",
      confidence: 100,
    }];
  }
  return body;
}

/* Guarda lo que hay en el formulario; lanza el error si el servidor no lo acepta. */
async function persistInvoiceEdits(invoiceId, documentId) {
  checkAmountFields();
  await jsonRequest(`/invoices/${invoiceId}`, "PATCH", invoiceEditBody(documentId));
  if (detailEdit?.invoiceId === Number(invoiceId)) detailEdit.snapshot = detailFormValues();
}

async function saveInvoice(invoiceId, documentId) {
  try {
    await persistInvoiceEdits(invoiceId, documentId);
    showMessage("Correcciones guardadas correctamente.", "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudieron guardar los cambios: ${error.message}`, "error");
  }
}

function formatMissingFields(missingFields) {
  if (!Array.isArray(missingFields) || !missingFields.length) return "• Campos sin identificar";
  return missingFields
    .map((item) => `• ${item?.label || item?.field || "Campo desconocido"}`)
    .join("\n");
}

function highlightMissingFields(missingFields) {
  document.querySelectorAll("[data-invoice-field]").forEach((el) => {
    el.classList.remove("field-missing");
    el.removeAttribute("aria-invalid");
  });

  if (!Array.isArray(missingFields)) return;

  let firstElement = null;

  missingFields.forEach((item) => {
    if (!item?.field) return;
    const element = document.querySelector(`[data-invoice-field="${item.field}"]`);
    if (!element) return;
    element.classList.add("field-missing");
    element.setAttribute("aria-invalid", "true");
    firstElement ||= element;
  });

  if (firstElement) {
    firstElement.scrollIntoView({ behavior: "smooth", block: "center" });
    firstElement.focus();
  }
}

function sendApproval(invoiceId, force) {
  return jsonRequest(`/invoices/${invoiceId}/approve`, "POST", { force: Boolean(force) });
}

async function approveInvoice(invoiceId, documentId = null, button = null) {
  if (!invoiceId || approvalInProgress) return;

  approvalInProgress = true;

  const originalText = button?.textContent || "Aprobar";
  if (button) {
    button.disabled = true;
    button.textContent = "Comprobando…";
  }
  let savedFirst = false;

  try {
    if (detailHasChanges(invoiceId)) {
      if (button) button.textContent = "Guardando cambios…";
      try {
        await persistInvoiceEdits(invoiceId, documentId);
      } catch (error) {
        showMessage(`La factura no se ha aprobado porque no se pudieron guardar tus cambios: ${error.message}. Siguen en pantalla para que los corrijas.`, "error");
        return;
      }
      savedFirst = true;
      if (button) button.textContent = "Aprobando…";
    }

    const result = await sendApproval(invoiceId, false);

    showMessage(
      savedFirst ? `Cambios guardados. ${result.message || "Factura aprobada."}` : (result.message || "Factura aprobada."),
      result.approved_with_warnings ? "warning" : "success"
    );

    await refreshAll();
    if (documentId && document.getElementById("documentDetailDialog")?.open) await showDetail(documentId);
  } catch (error) {
    const detail = error.data || {};
    const missingFieldsWarning = error.status === 409 && detail.code === "MISSING_FIELDS";

    if (!missingFieldsWarning) {
      showMessage(savedFirst ? `Tus cambios se guardaron, pero la factura no se ha aprobado: ${error.message}` : error.message, "error");
      if (savedFirst) await refreshAll();
      return;
    }

    const missingFields = detail.missing_fields || [];

    const confirmed = await window.askConfirm(`${detail.message || "La factura está incompleta."}\n\n` +
      `Campos pendientes:\n${formatMissingFields(missingFields)}`,
      { confirmLabel: "Aprobar con advertencias" });

    if (!confirmed) {
      if (documentId) {
        await showDetail(documentId);
        window.setTimeout(() => highlightMissingFields(missingFields), 100);
      }
      return;
    }

    try {
      const result = await sendApproval(invoiceId, true);
      showMessage(result.message || "Factura aprobada con advertencias.", "warning");
      await refreshAll();
      if (documentId) await showDetail(documentId);
    } catch (forceError) {
      showMessage(forceError.message, "error");
    }
  } finally {
    approvalInProgress = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function rejectInvoice(invoiceId, documentId) {
  const reason = await window.askText("¿Por qué rechazas la factura?", { label: "Motivo del rechazo", minLength: 3, confirmLabel: "Rechazar factura", danger: true });
  if (reason === null) return;

  if (reason.trim().length < 3) {
    showMessage("Debes indicar un motivo de al menos 3 caracteres.", "warning");
    return;
  }

  try {
    await jsonRequest(`/invoices/${invoiceId}/reject`, "POST", { reason: reason.trim() });
    showMessage("Factura rechazada.", "success");
    await refreshAll();
    if (documentId && document.getElementById("documentDetailDialog")?.open) await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo rechazar: ${error.message}`, "error");
  }
}

async function reopenInvoice(invoiceId, documentId) {
  const reason = await window.askText("¿Por qué reabres la factura? Quedará en el historial.", { label: "Motivo", minLength: 3, confirmLabel: "Reabrir factura" });
  if (reason === null) return;

  if (reason.trim().length < 3) {
    showMessage("Indica un motivo de al menos 3 caracteres.", "warning");
    return;
  }

  try {
    await jsonRequest(`/invoices/${invoiceId}/reopen`, "POST", { reason: reason.trim() });
    showMessage("Factura reabierta para revisión.", "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo reabrir: ${error.message}`, "error");
  }
}

async function markPaid(invoiceId, documentId) {
  try {
    await jsonRequest(`/invoices/${invoiceId}/payment`, "POST", {
      paid: true,
      paid_at: inputValue("paymentDate"),
      payment_method: inputValue("paymentMethod"),
    });
    showMessage("Pago registrado.", "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo registrar el pago: ${error.message}`, "error");
  }
}

async function cancelPayment(invoiceId, documentId) {
  if (!await window.askConfirm("¿Anular el pago registrado de esta factura?")) return;

  try {
    await jsonRequest(`/invoices/${invoiceId}/payment`, "POST", { paid: false });
    showMessage("Pago anulado.", "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo anular el pago: ${error.message}`, "error");
  }
}

async function convertToNotification(documentId) {
  if (!await window.askConfirm("Se tratará el documento como notificación administrativa (AEAT, Seguridad Social…) " +
    "y dejará de contar como factura. ¿Continuar?", { confirmLabel: "Tratar como notificación" })) return;

  try {
    await apiRequest(`/notifications/from-document/${documentId}`, { method: "POST" });
    showMessage("Documento registrado como notificación. Revisa su plazo.", "success");
    document.getElementById("documentDetailDialog")?.close();
    await refreshAll();
    activateTab("notificaciones");
  } catch (error) {
    showMessage(`No se pudo convertir: ${error.message}`, "error");
  }
}

async function reprocessDocument(documentId) {
  if (!await window.askConfirm("¿Quieres volver a ejecutar la extracción? Se sobrescribirán los datos extraídos.", { confirmLabel: "Volver a extraer" })) return;

  try {
    const result = await apiRequest(`/documents/${documentId}/reprocess`, { method: "POST" });
    showMessage(result.message || "Documento reprocesado.", result.success === false ? "error" : "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo reprocesar: ${error.message}`, "error");
  }
}

/* Lo archivado como albarán, presupuesto, pedido o nómina que en realidad es una factura: se vuelve a leer como
   factura y queda para revisar. */
async function markAsInvoice(documentId) {
  if (!await window.askConfirm("¿Es una factura? Se leerá como factura y quedará para revisar.", { confirmLabel: "Leer como factura" })) return;

  try {
    const result = await apiRequest(`/documents/${documentId}/reprocess?as_invoice=true`, { method: "POST" });
    showMessage(result.success === false ? (result.message || "No se pudo leer como factura.") : "Leída como factura: revisa los datos.", result.success === false ? "error" : "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo leer como factura: ${error.message}`, "error");
  }
}

/* Una persona dice qué es: sale de las facturas (si se leyó como tal y no está aprobada) y va a su sitio en Documentos. */
async function classifyDocument(documentId, kind) {
  const phrase = OTHER_DOCUMENT_PHRASES[kind] || "otro documento";
  if (!await window.askConfirm(`¿Es ${phrase}? Se guardará en Documentos y no contará como factura.`, { confirmLabel: "Guardar en Documentos" })) return;

  try {
    await jsonRequest(`/documents/${documentId}/classify`, "POST", { kind });
    showMessage(`Guardado en Documentos como ${phrase}.`, "success");
    await refreshAll();
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo clasificar: ${error.message}`, "error");
  }
}

/* ================================================================
OUTLOOK CONNECTOR
================================================================ */
async function loadOutlookStatus() {
  const container = document.getElementById("connectorsList");
  if (!container) return;

  container.innerHTML = "<p>Comprobando Outlook…</p>";

  try {
    const status = await apiRequest("/connectors/outlook/status");
    renderOutlookConnector(status);
  } catch (error) {
    renderOutlookConnector({
      configured: false,
      connected: false,
      error: error.message,
    });
  }
}

function renderOutlookConnector(status) {
  const container = document.getElementById("connectorsList");
  if (!container) return;

  const configured = Boolean(status.configured);
  const connected = Boolean(status.connected);

  container.innerHTML = `
    <article class="connector-card">
      <div class="connector-head">
        <div>
          <div class="connector-title-row">
            <h3>Microsoft Outlook</h3>
            <span class="connector-demo-badge">MICROSOFT GRAPH</span>
          </div>
          <p>
            Importa adjuntos PDF del buzón de entrada y los envía al mismo proceso real de carga,
            extracción, revisión y aprobación.
          </p>
        </div>

        <span class="connector-status ${connected ? "connector-success" : "connector-neutral"}">
          ${connected ? "Conectado" : (configured ? "Sin conectar" : "Sin configurar")}
        </span>
      </div>

      <div class="connector-metrics">
        <div>
          <span>Cuenta</span>
          <strong>${escapeHtml(status.account || "No conectada")}</strong>
        </div>
        <div>
          <span>Último resultado</span>
          <strong>${escapeHtml(status.message || status.error || "Sin sincronizaciones")}</strong>
        </div>
      </div>

      ${
        !configured
          ? `<p class="detail-hint">Configura OUTLOOK_CLIENT_ID, OUTLOOK_CLIENT_SECRET, OUTLOOK_TENANT_ID y APP_ENCRYPTION_KEY en el archivo .env (ver README).</p>`
          : ""
      }

      <div class="card-actions connector-actions">
        ${
          configured && !connected
            ? `
              <button type="button" class="act-btn act-primary" data-call="connectOutlook">
                Conectar Outlook
              </button>
            `
            : ""
        }

        ${
          connected
            ? `
              <button type="button" class="act-btn act-primary" data-call="syncOutlook" data-args="this">
                Sincronizar ahora
              </button>
              <button type="button" class="act-btn act-danger" data-call="disconnectOutlook">
                Desconectar
              </button>
            `
            : ""
        }
      </div>
    </article>
  `;
}

function connectOutlook() {
  window.location.href = "/api/connectors/outlook/login";
}

async function syncOutlook(button = null) {
  const originalText = button?.textContent || "Sincronizar ahora";
  if (button) {
    button.disabled = true;
    button.textContent = "Sincronizando…";
  }

  try {
    const result = await apiRequest("/connectors/outlook/sync", { method: "POST" });
    showMessage(
      result.message || `Sincronización terminada: ${window.pl(result.imported || 0, "adjunto(s) importado(s)")}.`,
      "success"
    );

    await Promise.all([loadOutlookStatus(), refreshAll()]);
  } catch (error) {
    showMessage(`No se pudo sincronizar Outlook: ${error.message}`, "error");
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function disconnectOutlook() {
  if (!await window.askConfirm("¿Desconectar la cuenta de Outlook?")) return;

  try {
    await apiRequest("/connectors/outlook/disconnect", { method: "POST" });
    showMessage("Outlook desconectado.", "success");
    await loadOutlookStatus();
  } catch (error) {
    showMessage(`No se pudo desconectar: ${error.message}`, "error");
  }
}

/* ================================================================
MENSAJES
================================================================ */
function showMessage(message, type = "info") {
  let container = document.getElementById("appMessages");
  if (!container) {
    container = document.createElement("div");
    container.id = "appMessages";
    container.className = "app-messages";
    document.body.appendChild(container);
  }

  // Los mensajes deben verse también con el diálogo modal abierto.
  const dialog = document.getElementById("documentDetailDialog");
  if (dialog?.open && container.parentElement !== dialog) dialog.appendChild(container);
  else if (!dialog?.open && container.parentElement !== document.body) document.body.appendChild(container);

  const item = document.createElement("div");
  item.className = `app-message app-message-${type}`;
  item.setAttribute("role", type === "error" ? "alert" : "status");
  item.textContent = message;
  container.appendChild(item);

  while (container.children.length > 4) container.firstElementChild.remove();

  window.setTimeout(() => item.remove(), 6000);
}

function renderApiError(title, detail) {
  const containers = [document.getElementById("documentsList"), document.getElementById("recentDocuments")].filter(Boolean);
  containers.forEach((container) => {
    container.innerHTML = emptyState("⚠️", title, detail);
  });
}

/* ================================================================
CIFRAS: CERO, SIN DATOS O ERROR
0 es cero. «—» es que aún no lo sabemos (cargando o error). Sin datos, se dice con
una frase en lugar de una fila de ceros. El error usa el patrón de «Hoy»: qué no se
pudo cargar, por qué y «Reintentar».
  setFigures(fila, "loading" | "ready" | "empty" | "error",
             { what, error, retry, empty, keep })
  · empty: frase para el estado sin datos ("" = solo ocultar, si la pantalla ya lo explica).
  · keep: ids de cifras que siguen visibles sin datos (p. ej. el estado del planificador).
================================================================ */
const FIGURE_VALUES = ".kpi-value, .taxhealth-metrics > div > span, .roi-item > strong";
const FIGURE_BOX = ".kpi, .taxhealth-metrics > div, .roi-item";

/* El aviso de error de «Hoy», reutilizable: qué no se pudo cargar, por qué y «Reintentar». */
function loadErrorHtml(what, error, retry = true) {
  return `<p class="danger-text">No se pudieron cargar ${escapeHtml(what)}: ${escapeHtml(error?.message || "error desconocido")}</p>`
    + (retry ? `<div><button type="button" class="btn-ghost" data-figures-retry>Reintentar</button></div>` : "");
}

function setFigures(container, state, { what = "los datos", error = null, retry = null, empty = "", keep = [] } = {}) {
  if (!container) return;
  let note = container.nextElementSibling?.classList.contains("figures-note") ? container.nextElementSibling : null;

  if (state === "loading" || state === "error") {
    container.querySelectorAll(FIGURE_VALUES).forEach((element) => {
      element.textContent = "—";
      element.classList.remove("value-negative");
    });
    container.querySelectorAll(".kpi-foot[id]").forEach((element) => { element.textContent = ""; });
  }
  container.setAttribute("aria-busy", String(state === "loading"));

  const boxes = [...container.querySelectorAll(FIGURE_BOX)];
  const kept = (box) => keep.some((id) => box.querySelector(`#${id}`));
  boxes.forEach((box) => box.classList.toggle("hidden", state === "empty" && !kept(box)));
  container.classList.toggle("hidden", state === "empty" && !boxes.some(kept));

  if (state === "ready" || state === "loading" || (state === "empty" && !empty)) {
    note?.remove();
    return;
  }
  if (!note) {
    note = document.createElement("section");
    note.className = "card figures-note";
    container.after(note);
  }
  if (state === "error") {
    note.setAttribute("role", "alert");
    note.innerHTML = loadErrorHtml(what, error, Boolean(retry));
    note.querySelector("[data-figures-retry]")?.addEventListener("click", () => retry());
  } else {
    note.setAttribute("role", "status");
    note.innerHTML = `<p class="board-empty">${escapeHtml(empty)}</p>`;
  }
}

/* ================================================================
FUNCIONES GLOBALES
================================================================ */
Object.assign(window, {
  setFigures,
  loadErrorHtml,
  icon,
  convertToNotification,
  jsonRequest,
  formatDate,
  todayIso,
  refreshAll,
  translateStatus,
  statusClass,
  quarterRange,
  showDetail,
  saveInvoice,
  approveInvoice,
  rejectInvoice,
  reopenInvoice,
  markPaid,
  cancelPayment,
  reprocessDocument,
  markAsInvoice,
  classifyDocument,
  openTask,
  resolveTask,
  connectOutlook,
  syncOutlook,
  disconnectOutlook,
  activateTab,
  apiRequest,
  escapeHtml,
  formatMoney,
  formatDay,
  showMessage,
  emptyState,
  currentQuarter,
});

/* ================================================================
INICIALIZACIÓN
================================================================ */
document.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  setupUpload();
  setupInvoiceFilters();
  setupOtherDocuments();

  const query = new URLSearchParams(window.location.search);
  if (query.get("outlook") === "connected") {
    showMessage("Cuenta de Outlook conectada correctamente.", "success");
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  await loadCategories();
  await Promise.all([
    loadDocuments(),
    loadTasks(),
    loadPanelSummary(),
    loadOutlookStatus(),
  ]);
});
