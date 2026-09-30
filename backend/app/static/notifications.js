"use strict";

(() => {
  let active = false;
  let onlyOpen = true;
  let catalogLoaded = false;

  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);

  const STATUS_LABELS = {
    PENDING: "Pendiente",
    IN_PROGRESS: "En preparación",
    ANSWERED: "Contestada / pagada",
    CLOSED: "Archivada",
  };

  async function loadCatalog() {
    if (catalogLoaded) return;

    try {
      const catalog = await window.apiRequest("/notifications/catalog");
      document.getElementById("notificationIssuer").innerHTML = catalog.issuers
        .map((item) => `<option value="${esc(item.code)}">${esc(item.label)}</option>`).join("");
      document.getElementById("notificationType").innerHTML = catalog.types
        .map((item) => `<option value="${esc(item.code)}">${esc(item.label)}</option>`).join("");
      catalogLoaded = true;
    } catch (error) {
      console.error("Catálogo de notificaciones no disponible:", error);
    }
  }

  async function updateBadge() {
    try {
      const notifications = await window.apiRequest("/notifications?open=true");
      const badge = document.getElementById("cntNotifications");
      if (!badge) return;
      badge.textContent = String(notifications.length);
      badge.classList.toggle("hidden", notifications.length === 0);
    } catch {
      /* el contador es opcional */
    }
  }

  async function load() {
    const container = document.getElementById("notificationList");

    try {
      const notifications = await window.apiRequest(`/notifications?open=${onlyOpen}`);
      render(notifications);
    } catch (error) {
      container.innerHTML = window.emptyState("⚠️", "No se pudieron cargar las notificaciones", error.message);
    }
  }

  function countdown(notification) {
    if (notification.urgency === "done") return ["✓", STATUS_LABELS[notification.status] || "Cerrada"];
    if (notification.days_left === null || notification.days_left === undefined) return ["—", "sin plazo"];
    const days = Number(notification.days_left);
    if (days < 0) return [String(Math.abs(days)), "días vencida"];
    if (days === 0) return ["HOY", "vence"];
    return [String(days), days === 1 ? "día" : "días"];
  }

  function render(notifications) {
    const container = document.getElementById("notificationList");

    if (!notifications.length) {
      container.innerHTML = window.emptyState(
        "🏛️",
        onlyOpen ? "Sin notificaciones abiertas" : "Sin notificaciones",
        "Cuando subas una notificación de AEAT, Seguridad Social u otro organismo aparecerá aquí con su plazo."
      );
      return;
    }

    container.innerHTML = notifications.map((notification) => {
      const [big, small] = countdown(notification);

      return `
        <details class="notification-item urgency-${esc(notification.urgency)}">
          <summary class="aeat-item">
            <div class="aeat-countdown urgency-${esc(notification.urgency)}">
              <strong>${esc(big)}</strong>
              <span>${esc(small)}</span>
            </div>
            <div class="aeat-body">
              <p class="aeat-title">${esc(notification.title)}</p>
              <p class="aeat-summary">
                ${notification.deadline ? `Plazo orientativo: <strong>${window.formatDay(notification.deadline)}</strong>` : "Sin plazo calculado"}
                ${notification.amount !== null ? ` · importe ${money(notification.amount)}` : ""}
              </p>
              <p class="aeat-meta">
                ${esc(notification.issuer_label)}
                ${notification.reference ? ` · ref. ${esc(notification.reference)}` : ""}
                ${notification.filename ? ` · ${esc(notification.filename)}` : ""}
              </p>
            </div>
            <span class="status-pill ${notification.urgency === "done" ? "status-success" : notification.urgency === "overdue" || notification.urgency === "critical" ? "status-danger" : "status-warning"}">
              ${esc(STATUS_LABELS[notification.status] || notification.status)}
            </span>
          </summary>
          <div class="notification-detail">
            ${notification.summary ? `<div class="detail-check"><span>Lo que pide el documento</span><p>${esc(notification.summary)}</p></div>` : ""}
            <p class="detail-hint">
              <strong>Cómo se ha calculado el plazo:</strong> ${esc(notification.deadline_rule || "—")}
            </p>
            <form class="detail-form-grid notification-edit" data-id="${notification.id}">
              <label>
                Estado
                <select name="status">
                  ${Object.entries(STATUS_LABELS).map(([value, label]) => `
                    <option value="${value}" ${value === notification.status ? "selected" : ""}>${esc(label)}</option>
                  `).join("")}
                </select>
              </label>
              <label>
                Fecha de notificación (acceso)
                <input type="date" name="notified_at" value="${esc(notification.notified_at || "")}">
              </label>
              <label>
                Puesta a disposición
                <input type="date" name="available_at" value="${esc(notification.available_at || "")}">
              </label>
              <label>
                Plazo manual (vacío = calculado)
                <input type="date" name="deadline" value="${notification.deadline_manual ? esc(notification.deadline || "") : ""}">
              </label>
              <label class="span-2">
                Notas y seguimiento
                <textarea name="notes" rows="2">${esc(notification.notes || "")}</textarea>
              </label>
              <div class="span-2 card-actions">
                <button type="submit" class="act-btn act-primary">Guardar</button>
                ${notification.document_id ? `
                  <a class="btn-ghost" href="/api/documents/${Number(notification.document_id)}/file" target="_blank" rel="noopener noreferrer">Abrir documento</a>
                ` : ""}
                <button type="button" class="btn-ghost" data-ask="${notification.id}">Preguntar al asistente</button>
              </div>
            </form>
          </div>
        </details>
      `;
    }).join("");
  }

  async function save(form) {
    const data = new FormData(form);
    const body = {
      status: data.get("status"),
      notified_at: data.get("notified_at") || null,
      available_at: data.get("available_at") || null,
      deadline: data.get("deadline") || null,
      notes: data.get("notes") || null,
    };

    try {
      await window.jsonRequest(`/notifications/${form.dataset.id}`, "PATCH", body);
      window.showMessage("Notificación actualizada y plazo recalculado.", "success");
      await Promise.all([load(), updateBadge()]);
      window.refreshAll();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  function setup() {
    document.getElementById("notificationFilter")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-open]");
      if (!button) return;
      onlyOpen = button.dataset.open === "true";
      document.querySelectorAll("#notificationFilter .segment").forEach((item) => item.classList.toggle("active", item === button));
      load();
    });

    const list = document.getElementById("notificationList");
    list?.addEventListener("submit", (event) => {
      const form = event.target.closest(".notification-edit");
      if (!form) return;
      event.preventDefault();
      save(form);
    });
    list?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-ask]");
      if (!button) return;
      window.activateTab("asistente");
      const input = document.getElementById("chatInput");
      if (input) {
        input.value = "¿Qué notificaciones tengo pendientes?";
        input.focus();
      }
    });

    document.getElementById("notificationForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const body = Object.fromEntries(
        [...data.entries()].map(([key, value]) => [key, String(value).trim() || null])
      );
      if (body.amount !== null) body.amount = Number(String(body.amount).replace(",", "."));

      try {
        await window.jsonRequest("/notifications", "POST", body);
        window.showMessage("Notificación registrada con su plazo.", "success");
        form.reset();
        document.getElementById("notificationFormCard").open = false;
        await Promise.all([load(), updateBadge()]);
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
  }

  window.addEventListener("capafiscal:tab-changed", async (event) => {
    active = event.detail?.tab === "notificaciones";
    if (active) {
      await loadCatalog();
      load();
    }
  });

  window.addEventListener("capafiscal:data-changed", () => {
    updateBadge();
    if (active) load();
  });

  document.addEventListener("DOMContentLoaded", () => {
    setup();
    updateBadge();
  });
})();
