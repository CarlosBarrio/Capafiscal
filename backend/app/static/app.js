"use strict";
console.log("CapaFiscal frontend real V41");

const API_BASE = "/api";

let documentsCache = [];
let tasksCache = [];
let approvalInProgress = false;

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
    else if (detail?.message) message = detail.message;
    else if (data?.message) message = data.message;
    throw new ApiError(message, response.status, detail);
  }
  return data;
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

function dateInputValue(value) {
  return value ? String(value).slice(0, 10) : "";
}

function nullableNumber(elementId) {
  const element = document.getElementById(elementId);
  if (!element) return null;
  const value = element.value.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeList(data, keys = []) {
  if (Array.isArray(data)) return data;
  for (const key of keys) {
    if (Array.isArray(data?.[key])) return data[key];
  }
  return [];
}

function normalizeDocumentsResponse(data) {
  return normalizeList(data, ["documents", "items", "results"]);
}

function normalizeTasksResponse(data) {
  return normalizeList(data, ["tasks", "items", "results"]);
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

function requiresReview(documentItem) {
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

function emptyState(icon, title, text) {
  return `
    <div class="section-empty">
      <span class="section-empty-icon">${escapeHtml(icon)}</span>
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

/* ================================================================
PESTAÑAS
================================================================ */
function setupTabs() {
  const buttons = document.querySelectorAll(".nav-tab");
  const panels = document.querySelectorAll(".tab-panel");

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const tabName = button.dataset.tab;

      buttons.forEach((item) => item.classList.toggle("active", item === button));
      panels.forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tabName}`));

      if (tabName === "conectores") {
        loadOutlookStatus();
      }
    });
  });
}

/* ================================================================
DOCUMENTOS
================================================================ */
async function loadDocuments() {
  try {
    const data = await apiRequest("/documents");
    documentsCache = normalizeDocumentsResponse(data);
    renderDocuments(documentsCache);
    renderRecentDocuments(documentsCache);
    renderDashboard(documentsCache);
    renderOpenRisks(documentsCache);

    // Informes (calculado desde approved)
    renderReports(documentsCache);

    // KPI de actividad (activity.js lo actualiza con su propio contador, pero mantenemos cntFacturas aquí)
    setText("cntFacturas", String(documentsCache.length));
    return documentsCache;
  } catch (error) {
    console.error("No se pudieron cargar documentos:", error);
    renderApiError("No se pudieron cargar los documentos", error.message);
    return [];
  }
}

function sumFieldApproved(documents, field) {
  const approved = documents.filter((item) => item?.status === "APPROVED");
  return approved.reduce((sum, item) => {
    const value = Number(item.invoice?.[field] || 0);
    return sum + (Number.isFinite(value) ? value : 0);
  }, 0);
}

function renderDashboard(documents) {
  const pending = documents.filter((item) => ["RECEIVED", "PROCESSING", "EXTRACTED", "NEEDS_REVIEW", "READY_FOR_APPROVAL"].includes(item.status));
  const risks = documents.filter(requiresReview);
  const approved = documents.filter((item) => item.status === "APPROVED");

  const subtotal = sumFieldApproved(documents, "subtotal");
  const taxTotal = sumFieldApproved(documents, "tax_total");
  const total = sumFieldApproved(documents, "total");

  setText("greetingLine", "Resumen de documentación real");
  setText("mRisks", risks.length);
  setText("mPending", pending.length);
  setText("mSpend", formatMoney(total));
  setText("mIva", formatMoney(taxTotal));
  setText("cntFacturas", documents.length);

  // también en informes/cuadros superiores
  setText("txCount", approved.length);
  setText("txBase", formatMoney(subtotal));
  setText("txIva", formatMoney(taxTotal));
  setText("txTotal", formatMoney(total));

  // ROI panel (resumen)
  setText("roiDocs", documents.length);
  setText("roiRisks", risks.length);
  setText("roiHours", "—");
  setText("roiCost", "—");
  setText("roiNote", "Métricas calculadas a partir de documentos reales.");
}

function renderReports(documents) {
  // Ya lo hace renderDashboard; dejamos por claridad (por si ampliamos)
}

function renderDocuments(documents) {
  const container = document.getElementById("documentsList");
  if (!container) return;

  if (!documents.length) {
    container.innerHTML = emptyState("📄", "Todavía no hay documentos", "Sube una factura PDF para comenzar.");
    return;
  }

  container.innerHTML = documents.map(realDocumentCard).join("");
}

function renderRecentDocuments(documents) {
  const container = document.getElementById("recentDocuments");
  if (!container) return;

  if (!documents.length) {
    container.innerHTML = emptyState("📥", "No hay documentos", "Los documentos cargados aparecerán aquí.");
    return;
  }

  container.innerHTML = documents.slice(0, 5).map(realDocumentCard).join("");
}

function realDocumentCard(documentItem) {
  const invoice = documentItem.invoice || {};
  const documentId = Number(documentItem.id);
  const invoiceId = Number(invoice.id);
  const confidence = Number(invoice.confidence || 0);

  const reviewRequired = requiresReview(documentItem);

  const canReview = Boolean(invoice.id && !["APPROVED", "REJECTED"].includes(documentItem.status));

  let sourceLabel = "";
  if (documentItem.source === "manual_upload") sourceLabel = "⬆ carga manual";
  else if (["outlook", "outlook_graph", "email"].includes(documentItem.source)) sourceLabel = "✉ Outlook";

  return `
  <article class="document-card ${reviewRequired ? "needs-review" : ""}">
    <div class="document-top">
      <div>
        <p class="doc-type">
          Factura recibida
          ${sourceLabel ? `<span class="source-tag">${escapeHtml(sourceLabel)}</span>` : ""}
        </p>
        <h3>${escapeHtml(invoice.supplier_name || "Proveedor sin identificar")}</h3>
        <p class="filename">${escapeHtml(documentItem.original_filename || "Sin nombre")}</p>
      </div>

      <div class="badges">
        <span class="priority-pill ${reviewRequired ? "priority-high" : "priority-low"}">
          ${reviewRequired ? "Revisar" : "Normal"}
        </span>
        <span class="status-pill ${statusClass(documentItem.status)}">
          ${escapeHtml(translateStatus(documentItem.status))}
        </span>
      </div>
    </div>

    <div class="document-grid">
      <div>
        <span>Número</span>
        <strong>${escapeHtml(invoice.invoice_number || "—")}</strong>
      </div>
      <div>
        <span>Fecha</span>
        <strong>${escapeHtml(invoice.invoice_date || "—")}</strong>
      </div>
      <div>
        <span>Importe</span>
        <strong>${formatMoney(invoice.total, invoice.currency)}</strong>
      </div>
      <div>
        <span>Confianza</span>
        <strong class="${confidenceClass(confidence)}">${confidence}%</strong>
      </div>

      <div class="card-actions" style="grid-column: 1 / -1;">
        <button class="btn-ghost" type="button" onclick="showDetail(${documentId})">
          Ver detalle
        </button>

        <a
          class="btn-ghost"
          href="/api/documents/${documentId}/file"
          target="_blank"
          rel="noopener noreferrer"
        >
          Abrir archivo
        </a>

        ${
          canReview
            ? `
              <button class="act-btn act-primary" type="button" onclick="approveInvoice(${invoiceId}, ${documentId}, this)">
                Aprobar
              </button>
              <button class="act-btn act-danger" type="button" onclick="rejectInvoice(${invoiceId}, ${documentId})">
                Rechazar
              </button>
            `
            : ""
        }
      </div>
    </div>
  </article>
  `;
}

/* ================================================================
RIESGOS (panel abierto)
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
  text.textContent = `${risks.length} documento(s) requieren revisión.`;

  list.innerHTML = risks
    .slice(0, 6)
    .map((doc) => {
      const invoice = doc.invoice || {};
      return `
        <div class="risk-row">
          <div>
            <strong>${escapeHtml(invoice.supplier_name || "Proveedor sin identificar")}</strong>
            <span>${formatMoney(invoice.total, invoice.currency)}</span>
          </div>
          <button type="button" class="btn-ghost" onclick="showDetail(${Number(doc.id)})">
            Revisar
          </button>
        </div>
      `;
    })
    .join("");
}

/* ================================================================
TAREAS
================================================================ */
async function loadTasks() {
  const container = document.getElementById("inboxList");
  if (!container) return;

  try {
    const data = await apiRequest("/tasks/review-inbox");
    tasksCache = normalizeTasksResponse(data);
    renderTasks(tasksCache);

    const sub = document.getElementById("reviewInboxSub");
    if (sub) sub.textContent = `${tasksCache.length} tarea(s) activas`;
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

  container.innerHTML = tasks.map((task) => {
    const priority = String(task.priority || "medium").toLowerCase();

    return `
      <article class="task-item priority-${escapeHtml(priority)}">
        <span class="task-dot"></span>
        <div class="task-body">
          <p class="task-label">${escapeHtml(task.reason || task.title || "Revisar documento")}</p>
          <p class="task-narrative">
            Estado: ${escapeHtml(translateStatus(task.status))}
            · Documento: ${escapeHtml(task.document_id || "—")}
            · Factura: ${escapeHtml(task.invoice_id || "—")}
          </p>
          <div class="card-actions">
            ${
              task.document_id
                ? `
                  <button
                    type="button"
                    class="btn-ghost"
                    onclick="openTask(${Number(task.id)}, ${Number(task.document_id)})"
                  >
                    Abrir revisión
                  </button>
                `
                : ""
            }
            <button type="button" class="act-btn act-primary" onclick="resolveTask(${Number(task.id)})">
              Resolver
            </button>
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
  const note = window.prompt("Nota de resolución (opcional):", "Revisión completada");
  if (note === null) return;

  try {
    await apiRequest(`/tasks/${taskId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        resolution: note.trim() || "Revisión completada",
        notes: note.trim() || null,
      }),
    });
    showMessage("Tarea resuelta.", "success");
    await loadTasks();
    await loadDocuments();
  } catch (error) {
    showMessage(`No se pudo resolver la tarea: ${error.message}`, "error");
  }
}

/* ================================================================
UPLOAD
================================================================ */
function setupUpload() {
  const fileInput = document.getElementById("fileInput");
  if (!fileInput) return;

  fileInput.addEventListener("change", async () => {
    if (fileInput.files?.length) {
      await uploadSelectedFile(fileInput);
    }
  });
}

async function uploadSelectedFile(fileInput) {
  const file = fileInput.files?.[0];
  if (!file) return;

  const lowerName = file.name.toLowerCase();
  if (!lowerName.endsWith(".pdf") && !lowerName.endsWith(".txt")) {
    showMessage("El archivo debe ser PDF o TXT.", "error");
    fileInput.value = "";
    return;
  }

  // backend valida ~15MB también, aquí anticipamos
  if (file.size > 15 * 1024 * 1024) {
    showMessage("El archivo supera el límite de 15 MB.", "error");
    fileInput.value = "";
    return;
  }

  try {
    showMessage(`Procesando "${file.name}"…`, "info");

    const formData = new FormData();
    formData.append("uploaded_file", file, file.name);

    const result = await apiRequest("/upload", {
      method: "POST",
      body: formData,
    });

    notifyActivityChanged();
    showMessage(result.message || "Documento procesado.", result.duplicate ? "warning" : "success");

    fileInput.value = "";

    await Promise.all([loadDocuments(), loadTasks()]);

    const documentId = result?.document?.id;
    if (documentId) await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo procesar el archivo: ${error.message}`, "error");
    fileInput.value = "";
  }
}

/* ================================================================
DETALLE (dialog)
================================================================ */
function ensureDetailDialog() {
  let dialog = document.getElementById("documentDetailDialog");
  if (dialog) return dialog;

  dialog = document.createElement("dialog");
  dialog.id = "documentDetailDialog";
  dialog.className = "document-detail-dialog";
  dialog.innerHTML = `
    <div class="detail-dialog-header">
      <h2>Detalle de factura</h2>
      <button type="button" class="btn-ghost" id="closeDetailDialog">✕ Cerrar</button>
    </div>
    <div id="documentDetailContent"></div>
  `;

  document.body.appendChild(dialog);

  document.getElementById("closeDetailDialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  return dialog;
}

async function showDetail(documentId) {
  const dialog = ensureDetailDialog();
  const content = document.getElementById("documentDetailContent");
  content.innerHTML = "<p>Cargando documento…</p>";

  if (!dialog.open) dialog.showModal();

  try {
    const documentItem = await apiRequest(`/documents/${documentId}`);
    const invoice = documentItem.invoice || {};
    const invoiceId = Number(invoice.id);

    const canReview = Boolean(
      invoice.id && !["APPROVED", "REJECTED"].includes(documentItem.status)
    );

    content.innerHTML = `
      <div class="detail-grid detail-form-grid">
        <label>
          Proveedor
          <input id="detailSupplierName" data-invoice-field="supplier_name" type="text" value="${escapeHtml(invoice.supplier_name || "")}">
        </label>

        <label>
          NIF proveedor
          <input id="detailSupplierTaxId" data-invoice-field="supplier_tax_id" type="text" value="${escapeHtml(invoice.supplier_tax_id || "")}">
        </label>

        <label>
          Número de factura
          <input id="detailInvoiceNumber" data-invoice-field="invoice_number" type="text" value="${escapeHtml(invoice.invoice_number || "")}">
        </label>

        <label>
          Fecha de factura
          <input id="detailInvoiceDate" data-invoice-field="invoice_date" type="date" value="${escapeHtml(dateInputValue(invoice.invoice_date))}">
        </label>

        <label>
          Base imponible
          <input id="detailSubtotal" data-invoice-field="subtotal" type="number" step="0.01" value="${escapeHtml(invoice.subtotal ?? "")}">
        </label>

        <label>
          IVA
          <input id="detailTaxTotal" data-invoice-field="tax_total" type="number" step="0.01" value="${escapeHtml(invoice.tax_total ?? "")}">
        </label>

        <label>
          Total
          <input id="detailTotal" data-invoice-field="total" type="number" step="0.01" value="${escapeHtml(invoice.total ?? "")}">
        </label>

        <label>
          Moneda
          <input id="detailCurrency" data-invoice-field="currency" type="text" maxlength="3" value="${escapeHtml(invoice.currency || "EUR")}">
        </label>
      </div>

      <div class="detail-summary">
        <p><strong>Estado:</strong> ${escapeHtml(translateStatus(documentItem.status))}</p>
        <p><strong>Confianza:</strong> ${Number(invoice.confidence || 0)}%</p>
        <p><strong>Archivo:</strong> ${escapeHtml(documentItem.original_filename || "—")}</p>
        <p><strong>Creado:</strong> ${formatDate(documentItem.created_at)}</p>
      </div>

      <div class="card-actions">
        ${
          invoice.id
            ? `
              <button type="button" class="act-btn act-primary" onclick="saveInvoice(${invoiceId}, ${Number(documentItem.id)})">
                Guardar correcciones
              </button>
            `
            : ""
        }

        ${
          canReview
            ? `
              <button type="button" class="act-btn act-primary" onclick="approveInvoice(${invoiceId}, ${Number(documentItem.id)}, this)">
                Aprobar
              </button>
              <button type="button" class="act-btn act-danger" onclick="rejectInvoice(${invoiceId}, ${Number(documentItem.id)})">
                Rechazar
              </button>
            `
            : ""
        }

        <button type="button" class="btn-ghost" onclick="reprocessDocument(${Number(documentItem.id)})">
          Reprocesar
        </button>

        <a class="btn-ghost"
           href="/api/documents/${Number(documentItem.id)}/file"
           target="_blank"
           rel="noopener noreferrer"
        >
          Abrir archivo
        </a>
      </div>
    `;
  } catch (error) {
    content.innerHTML = emptyState("⚠️", "No se pudo abrir el documento", error.message);
  }
}

async function saveInvoice(invoiceId, documentId) {
  const body = {
    supplier_name: document.getElementById("detailSupplierName")?.value.trim() || null,
    supplier_tax_id: document.getElementById("detailSupplierTaxId")?.value.trim() || null,
    invoice_number: document.getElementById("detailInvoiceNumber")?.value.trim() || null,
    invoice_date: document.getElementById("detailInvoiceDate")?.value || null,
    subtotal: nullableNumber("detailSubtotal"),
    tax_total: nullableNumber("detailTaxTotal"),
    total: nullableNumber("detailTotal"),
    currency: document.getElementById("detailCurrency")?.value.trim().toUpperCase() || "EUR",
  };

  try {
    await apiRequest(`/invoices/${invoiceId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    notifyActivityChanged();
    showMessage("Correcciones guardadas correctamente.", "success");

    await Promise.all([loadDocuments(), loadTasks()]);
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

async function sendApproval(invoiceId, force) {
  return apiRequest(`/invoices/${invoiceId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: Boolean(force) }),
  });
}

async function approveInvoice(invoiceId, documentId = null, button = null) {
  if (!invoiceId || approvalInProgress) return;

  approvalInProgress = true;

  const originalText = button?.textContent || "Aprobar";
  if (button) {
    button.disabled = true;
    button.textContent = "Comprobando…";
  }

  try {
    const result = await sendApproval(invoiceId, false);
    notifyActivityChanged();

    showMessage(
      result.message || "Factura aprobada.",
      result.approved_with_warnings ? "warning" : "success"
    );

    await Promise.all([loadDocuments(), loadTasks()]);
    if (documentId) await showDetail(documentId);
  } catch (error) {
    const detail = error.data || {};
    const missingFieldsWarning = error.status === 409 && detail.code === "MISSING_FIELDS";

    if (!missingFieldsWarning) {
      showMessage(error.message, "error");
      return;
    }

    const missingFields = detail.missing_fields || [];

    const confirmed = window.confirm(
      `${detail.message || "La factura está incompleta."}\n\n` +
      `Campos pendientes:\n${formatMissingFields(missingFields)}\n\n` +
      "Aceptar: aprobar con advertencias.\n" +
      "Cancelar: completar los campos."
    );

    if (!confirmed) {
      if (documentId) {
        await showDetail(documentId);
        window.setTimeout(() => highlightMissingFields(missingFields), 100);
      }
      return;
    }

    const result = await sendApproval(invoiceId, true);
    notifyActivityChanged();

    showMessage(
      result.message || "Factura aprobada con advertencias.",
      "warning"
    );

    await Promise.all([loadDocuments(), loadTasks()]);
    if (documentId) await showDetail(documentId);
  } finally {
    approvalInProgress = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function rejectInvoice(invoiceId, documentId) {
  const reason = window.prompt("Indica el motivo del rechazo:");
  if (reason === null) return;

  if (!reason.trim()) {
    showMessage("Debes indicar un motivo.", "warning");
    return;
  }

  try {
    await apiRequest(`/invoices/${invoiceId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason.trim() }),
    });

    notifyActivityChanged();
    showMessage("Factura rechazada.", "success");

    await Promise.all([loadDocuments(), loadTasks()]);
    if (documentId) await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo rechazar: ${error.message}`, "error");
  }
}

async function reprocessDocument(documentId) {
  if (!window.confirm("¿Quieres volver a ejecutar la extracción?")) return;

  try {
    const result = await apiRequest(`/documents/${documentId}/reprocess`, { method: "POST" });
    notifyActivityChanged();
    showMessage(result.message || "Documento reprocesado.", result.success === false ? "error" : "success");
    await Promise.all([loadDocuments(), loadTasks()]);
    await showDetail(documentId);
  } catch (error) {
    showMessage(`No se pudo reprocesar: ${error.message}`, "error");
  }
}

/* ================================================================
OUTLOOK CONNECTOR (según tu UI)
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

        <span class="connector-status ${connected ? "connector-success" : (configured ? "connector-neutral" : "connector-neutral")}">
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

      <div class="card-actions connector-actions">
        ${
          configured && !connected
            ? `
              <button type="button" class="act-btn act-primary" onclick="connectOutlook()">
                Conectar Outlook
              </button>
            `
            : ""
        }

        ${
          connected
            ? `
              <button type="button" class="act-btn act-primary" onclick="syncOutlook(this)">
                Sincronizar ahora
              </button>
              <button type="button" class="act-btn act-danger" onclick="disconnectOutlook()">
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
      result.message || `Sincronización terminada: ${result.imported || 0} adjunto(s) importado(s).`,
      "success"
    );

    await Promise.all([loadOutlookStatus(), loadDocuments(), loadTasks()]);
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
  if (!window.confirm("¿Desconectar la cuenta de Outlook?")) return;

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

  const item = document.createElement("div");
  item.className = `app-message app-message-${type}`;
  item.textContent = message;
  container.appendChild(item);

  window.setTimeout(() => item.remove(), 6000);
}

function renderApiError(title, detail) {
  const containers = [document.getElementById("documentsList"), document.getElementById("recentDocuments")].filter(Boolean);
  containers.forEach((container) => {
    container.innerHTML = emptyState("⚠️", title, detail);
  });
}

/* ================================================================
FUNCIONES GLOBALES
================================================================ */
window.showDetail = showDetail;
window.saveInvoice = saveInvoice;
window.approveInvoice = approveInvoice;
window.rejectInvoice = rejectInvoice;
window.reprocessDocument = reprocessDocument;
window.openTask = openTask;
window.resolveTask = resolveTask;
window.connectOutlook = connectOutlook;
window.syncOutlook = syncOutlook;
window.disconnectOutlook = disconnectOutlook;

/* ================================================================
INICIALIZACIÓN
================================================================ */
document.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  setupUpload();

  const query = new URLSearchParams(window.location.search);
  if (query.get("outlook") === "connected") {
    showMessage("Cuenta de Outlook conectada correctamente.", "success");
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  await Promise.all([
    loadDocuments(),
    loadTasks(),
    loadOutlookStatus(),
  ]);
});