"use strict";

(() => {
  let active = false;

  const esc = (value) => window.escapeHtml(value);

  const STATUS = {
    OK: ["Correcto", "status-success", "✓"],
    PENDING: ["Pendiente", "status-neutral", "…"],
    WARNING: ["Atención", "status-warning", "!"],
    EXPIRED: ["Caducado", "status-danger", "✕"],
    NOT_APPLICABLE: ["No aplica", "status-neutral", "–"],
  };

  async function load() {
    try {
      const status = await window.apiRequest("/compliance");
      render(status);
    } catch (error) {
      document.getElementById("complianceGroups").innerHTML =
        window.emptyState("⚠️", "No se pudo cargar el cumplimiento", error.message);
    }
  }

  function render(status) {
    const score = document.getElementById("complianceScore");
    score.textContent = `${status.score} %`;
    score.className = status.score >= 80 ? "score-good" : status.score >= 50 ? "score-mid" : "score-low";

    document.getElementById("complianceSummary").textContent =
      `${status.ok} de ${status.total} puntos en orden. Marca cada elemento cuando lo tengas resuelto; ` +
      "el agente vigila las caducidades y los controles automáticos.";
    document.getElementById("complianceNote").textContent = status.note;

    const certificate = status.items.find((item) => item.code === "CERT_DIGITAL");
    const info = document.getElementById("certificateInfo");

    if (certificate?.details?.valid_until) {
      const details = certificate.details;
      info.innerHTML = `
        <div class="certificate-card">
          <div><span>Titular</span><strong>${esc(details.subject || details.organization || "—")}</strong></div>
          <div><span>NIF</span><strong>${esc(details.tax_id || "—")}</strong></div>
          <div><span>Emisor</span><strong>${esc(details.issuer || "—")}</strong></div>
          <div><span>Válido hasta</span><strong>${window.formatDay(details.valid_until)}</strong></div>
        </div>
        ${certificate.message ? `<p class="detail-hint">${esc(certificate.message)}</p>` : ""}
      `;
    } else {
      info.innerHTML = "";
    }

    const groups = {};
    for (const item of status.items) {
      (groups[item.group] ||= []).push(item);
    }

    document.getElementById("complianceGroups").innerHTML = Object.entries(groups).map(([group, items]) => `
      <div class="card">
        <div class="card-head"><h2>${esc(group)}</h2></div>
        <div class="compliance-list">
          ${items.map(itemHtml).join("")}
        </div>
      </div>
    `).join("");
  }

  function itemHtml(item) {
    const [label, className, icon] = STATUS[item.status] || STATUS.PENDING;

    const controls = item.automatic ? "" : `
      <form class="compliance-form ${item.supports_expiry ? "" : "no-expiry"}" data-code="${esc(item.code)}">
        <select name="status" aria-label="Estado">
          ${["OK", "PENDING", "WARNING", "NOT_APPLICABLE"].map((value) => `
            <option value="${value}" ${value === item.status ? "selected" : ""}>${esc(STATUS[value][0])}</option>
          `).join("")}
        </select>
        ${item.supports_expiry ? `
          <input type="date" name="expires_at" value="${esc(item.expires_at || "")}" aria-label="Válido hasta" title="Válido hasta">
        ` : ""}
        <input type="text" name="notes" value="${esc(item.notes || "")}" placeholder="Notas" maxlength="2000" aria-label="Notas">
        <button type="submit" class="btn-ghost">Guardar</button>
      </form>
    `;

    return `
      <div class="compliance-item">
        <span class="compliance-icon ${className}" aria-hidden="true">${icon}</span>
        <div class="compliance-body">
          <div class="compliance-head">
            <strong>${esc(item.title)}</strong>
            <span class="status-pill ${className}">${esc(label)}</span>
          </div>
          <p>${esc(item.message || item.description)}</p>
          ${item.message && !item.automatic ? `<small class="muted">${esc(item.description)}</small>` : ""}
          ${controls}
        </div>
      </div>
    `;
  }

  function setup() {
    document.getElementById("complianceGroups")?.addEventListener("submit", async (event) => {
      const form = event.target.closest(".compliance-form");
      if (!form) return;
      event.preventDefault();

      const data = new FormData(form);
      const body = {
        status: data.get("status"),
        notes: data.get("notes") ?? null,
      };

      if (form.elements.expires_at) {
        const value = data.get("expires_at");
        if (value) body.expires_at = value;
        else body.clear_expiry = true;
      }

      try {
        const status = await window.jsonRequest(`/compliance/${form.dataset.code}`, "PATCH", body);
        render(status);
        window.showMessage("Cumplimiento actualizado.", "success");
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("certificateForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;

      try {
        const result = await window.apiRequest("/compliance/certificate", { method: "POST", body: new FormData(form) });
        window.showMessage(result.message, "success");
        form.reset();
        await load();
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "cumplimiento";
    if (active) load();
  });

  window.addEventListener("capafiscal:data-changed", () => {
    if (active) load();
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
