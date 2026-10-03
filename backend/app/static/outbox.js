"use strict";

/* Bandeja de salida: mensajes que prepara el agente y aprueba una persona. */
(() => {
  const esc = (value) => window.escapeHtml(value);

  const KIND_ICONS = {
    INVOICE: "invoice",
    DUNNING: "coins",
    PAYSLIP: "wallet",
    DIGEST: "sparkles",
    ADVISOR: "archive",
    OTHER: "mail",
  };
  const STATUS = {
    DRAFT: "status-warning",
    FAILED: "status-danger",
    SENT: "status-success",
    DISCARDED: "status-neutral",
  };
  const LEVELS = { 1: "Recordatorio", 2: "Segundo aviso", 3: "Requerimiento" };

  let active = false;
  let status = "DRAFT";
  let messages = [];
  let selectedId = null;
  let smtp = false;

  function relative(value) {
    if (!value) return "";
    const date = new Date(value);
    const minutes = Math.round((Date.now() - date.getTime()) / 60000);
    if (minutes < 1) return "ahora";
    if (minutes < 60) return `hace ${minutes} min`;
    if (minutes < 60 * 24) return `hace ${Math.round(minutes / 60)} h`;
    return window.formatDay(value.slice(0, 10));
  }

  async function loadCounts() {
    try {
      const data = await window.apiRequest("/outbox?status=DRAFT");
      updateBadge(data.counts.draft);
      smtp = data.counts.smtp;
    } catch {
      /* el contador es orientativo */
    }
  }

  function updateBadge(count) {
    const badge = document.getElementById("cntOutbox");
    if (!badge) return;
    badge.textContent = count;
    badge.classList.toggle("hidden", !count);
  }

  async function load() {
    const params = status ? `?status=${status}` : "";
    const data = await window.apiRequest(`/outbox${params}`);
    messages = status === "DRAFT"
      ? [...data.messages, ...(await window.apiRequest("/outbox?status=FAILED")).messages]
      : data.messages;
    smtp = data.counts.smtp;
    updateBadge(data.counts.draft);
    renderBanner();
    renderList();

    if (!messages.some((item) => item.id === selectedId)) selectedId = messages[0]?.id ?? null;
    renderDetail();
  }

  function renderBanner() {
    document.getElementById("smtpBanner").innerHTML = smtp
      ? ""
      : `<div class="info-banner">${window.icon("mail")}<span>Sin servidor de correo: cada mensaje se descarga listo para enviarlo desde tu correo; después márcalo como enviado.</span></div>`;
  }

  function renderList() {
    const container = document.getElementById("outboxList");
    if (!messages.length) {
      container.innerHTML = window.emptyState(
        "send",
        status === "DRAFT" ? "Nada pendiente de enviar" : "Sin mensajes",
        status === "DRAFT" ? "Cuando el agente prepare una factura, una reclamación o un recibo, aparecerá aquí para tu visto bueno." : "Aquí verás el historial de mensajes.",
      );
      return;
    }
    container.innerHTML = messages.map((item) => `
      <button type="button" class="outbox-item ${item.id === selectedId ? "selected" : ""}" data-message="${item.id}">
        <span class="outbox-icon kind-${esc(item.kind.toLowerCase())}">${window.icon(KIND_ICONS[item.kind] || "mail")}</span>
        <span class="outbox-main">
          <span class="outbox-top">
            <strong>${esc(item.to_name || item.to_email || "Sin destinatario")}</strong>
            <small>${esc(relative(item.sent_at || item.created_at))}</small>
          </span>
          <span class="outbox-subject">${esc(item.subject)}</span>
          <span class="outbox-tags">
            <span class="status-pill mini ${item.status === "FAILED" ? "status-danger" : item.status === "SENT" ? "status-success" : "status-neutral"}">${esc(item.kind_label)}${item.level ? ` · ${esc(LEVELS[item.level] || "")}` : ""}</span>
            ${item.needs_email ? `<span class="status-pill mini status-danger">Falta email</span>` : ""}
            ${item.attachments.length ? `<small>${window.icon("doc")} ${item.attachments.length}</small>` : ""}
            ${item.created_by === "agent" ? `<small>${window.icon("sparkles")} agente</small>` : ""}
          </span>
        </span>
      </button>
    `).join("");
  }

  function renderDetail() {
    const container = document.getElementById("outboxDetail");
    const message = messages.find((item) => item.id === selectedId);
    if (!message) {
      container.innerHTML = `<div class="outbox-empty">${window.icon("inbox")}<p>Elige un mensaje para revisarlo.</p></div>`;
      return;
    }
    const editable = message.status === "DRAFT" || message.status === "FAILED";
    const disabled = editable ? "" : "disabled";

    container.innerHTML = `
      <div class="outbox-detail-head">
        <span class="status-pill ${STATUS[message.status]}">${esc(message.status_label)}</span>
        <small class="muted">${message.created_by === "agent" ? "Redactado por el agente" : "Preparado por ti"} · ${window.formatDate(message.created_at)}${message.sent_at ? ` · enviado ${window.formatDate(message.sent_at)}` : ""}</small>
      </div>
      ${message.error ? `<div class="report-warning">${window.icon("alert")}<span>${esc(message.error)}</span></div>` : ""}
      <form class="outbox-form" id="outboxForm">
        <label>Para
          <span class="input-pair">
            <input type="text" name="to_name" value="${esc(message.to_name || "")}" placeholder="Nombre" ${disabled}>
            <input type="email" name="to_email" value="${esc(message.to_email || "")}" placeholder="email@cliente.es" class="${message.needs_email ? "field-missing" : ""}" ${disabled}>
          </span>
        </label>
        <label>Asunto <input type="text" name="subject" value="${esc(message.subject)}" maxlength="255" ${disabled}></label>
        <label>Mensaje <textarea name="body" rows="13" ${disabled}>${esc(message.body)}</textarea></label>
      </form>
      ${message.attachments.length ? `
        <div class="attachment-list">
          ${message.attachments.map((item, index) => `
            <a class="attachment-chip" href="/api/outbox/${message.id}/attachments/${index}" target="_blank" rel="noopener noreferrer">
              ${window.icon(item.type === "advisor_pack" ? "archive" : "doc")}<span>${esc(item.filename)}</span>
            </a>`).join("")}
        </div>` : ""}
      <div class="dialog-footer">
        ${editable ? `
          <button type="button" class="btn-ghost danger-text" data-outbox="discard">${window.icon("trash")} Descartar</button>
          <span class="spacer"></span>
          <a class="btn-ghost" href="/api/outbox/${message.id}/eml" data-outbox-eml>${window.icon("download")} Descargar para Outlook</a>
          <button type="button" class="btn-ghost" data-outbox="mark-sent">Ya lo he enviado</button>
          ${smtp ? `<button type="button" class="act-btn act-primary" data-outbox="send">${window.icon("send")} Enviar</button>` : ""}
        ` : `
          <span class="spacer"></span>
          <a class="btn-ghost" href="/api/outbox/${message.id}/eml">${window.icon("download")} Descargar copia</a>
        `}
      </div>
    `;
  }

  async function saveEdits() {
    const form = document.getElementById("outboxForm");
    const message = messages.find((item) => item.id === selectedId);
    if (!form || !message) return message;
    const body = {
      to_name: form.elements.to_name.value.trim() || null,
      to_email: form.elements.to_email.value.trim() || null,
      subject: form.elements.subject.value,
      body: form.elements.body.value,
    };
    const changed = Object.entries(body).some(([key, value]) => (message[key] || null) !== value);
    if (!changed) return message;
    const saved = await window.jsonRequest(`/outbox/${message.id}`, "PATCH", body);
    Object.assign(message, saved);
    return saved;
  }

  async function action(name) {
    try {
      if (name !== "discard") await saveEdits();
      if (name === "discard" && !await window.askConfirm("¿Descartar este mensaje?")) return;
      await window.jsonRequest(`/outbox/${selectedId}/${name}`, "POST", {});
      window.showMessage(
        name === "send" ? "Mensaje enviado." : name === "mark-sent" ? "Marcado como enviado." : "Mensaje descartado.",
        "success",
      );
      await load();
      window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));
    } catch (error) {
      window.showMessage(error.message, "error");
      await load();
    }
  }

  function setup() {
    const section = document.getElementById("tab-salida");
    if (!section) return;

    document.getElementById("outboxFilter").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (!button) return;
      status = button.dataset.status;
      document.querySelectorAll("#outboxFilter .segment").forEach((item) => item.classList.toggle("active", item === button));
      selectedId = null;
      load().catch((error) => window.showMessage(error.message, "error"));
    });

    document.getElementById("outboxList").addEventListener("click", async (event) => {
      const item = event.target.closest("[data-message]");
      if (!item) return;
      try {
        await saveEdits();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
      selectedId = Number(item.dataset.message);
      renderList();
      renderDetail();
    });

    document.getElementById("outboxDetail").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-outbox]");
      if (button) return action(button.dataset.outbox);

      const eml = event.target.closest("[data-outbox-eml]");
      if (eml) {
        event.preventDefault();
        try {
          await saveEdits();
          window.location.href = eml.href;
        } catch (error) {
          window.showMessage(error.message, "error");
        }
      }
    });

    loadCounts();
  }

  window.openOutboxMessage = async (id) => {
    status = "";
    document.querySelectorAll("#outboxFilter .segment").forEach((item) => item.classList.toggle("active", item.dataset.status === ""));
    selectedId = id;
    window.activateTab("salida");
  };

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "salida";
    if (active) load().catch((error) => window.showMessage(error.message, "error"));
  });
  window.addEventListener("capafiscal:data-changed", () => { if (!active) loadCounts(); });
  window.addEventListener("capafiscal:outbox-changed", () => (active ? load() : loadCounts()));

  document.addEventListener("DOMContentLoaded", setup);
})();
