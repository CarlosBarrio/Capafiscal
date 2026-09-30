"use strict";

(() => {
  const esc = (value) => window.escapeHtml(value);

  async function load() {
    try {
      const [company, rules] = await Promise.all([
        window.apiRequest("/company"),
        window.apiRequest("/supplier-rules"),
      ]);
      renderCompany(company);
      renderRules(rules);
    } catch (error) {
      window.showMessage(`No se pudieron cargar los datos de empresa: ${error.message}`, "error");
    }
  }

  function renderCompany(company) {
    const form = document.getElementById("companyForm");
    if (!form) return;

    form.elements.name.value = company.name || "";
    form.elements.tax_id.value = company.tax_id || "";
    form.elements.legal_form.value = company.legal_form || "SOCIEDAD";
    form.elements.activity.value = company.activity || "";
    form.elements.email.value = company.email || "";
    form.elements.hourly_cost.value = company.hourly_cost ?? "";
    form.elements.iban.value = company.iban || "";
    form.elements.bic.value = company.bic || "";
    form.elements.at_ep_rate.value = company.at_ep_rate ?? "";
    for (const key of ["address", "postal_code", "city", "phone", "invoice_footer", "advisor_email"]) {
      form.elements[key].value = company[key] || "";
    }
    form.elements.default_payment_days.value = company.default_payment_days ?? "";
    form.elements.late_interest_rate.value = company.late_interest_rate ?? "";

    const hint = document.getElementById("companyHint");

    if (!company.configured) {
      hint.innerHTML = "Sin NIF configurado, todas las facturas se tratan como <strong>recibidas</strong>. Indícalo para que el agente detecte tus facturas emitidas.";
    } else if (company.tax_id && !company.tax_id_valid) {
      hint.textContent = "El dígito de control del NIF/CIF no es válido. Revísalo.";
    } else {
      hint.textContent = "El agente usa este NIF para distinguir facturas emitidas y recibidas. Tras cambiarlo, usa «Reprocesar pendientes» en Facturas.";
    }
  }

  function renderRules(rules) {
    const body = document.querySelector("#rulesTable tbody");
    if (!body) return;

    if (!rules.length) {
      body.innerHTML = `<tr><td colspan="4" class="empty-cell">Aún no hay categorías aprendidas. Corrige la categoría de una factura y el agente la recordará.</td></tr>`;
      return;
    }

    body.innerHTML = rules.map((rule) => `
      <tr>
        <td>${esc(rule.tax_id)}</td>
        <td>${esc(rule.category)}</td>
        <td class="num">${rule.times_applied}</td>
        <td class="actions-cell">
          <button type="button" class="btn-ghost" data-forget="${rule.id}">Olvidar</button>
        </td>
      </tr>
    `).join("");
  }

  function setup() {
    document.getElementById("companyForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const body = Object.fromEntries(
        [...data.entries()].map(([key, value]) => [key, String(value).trim() || null])
      );
      for (const key of ["hourly_cost", "at_ep_rate", "default_payment_days", "late_interest_rate"]) {
        if (body[key] !== null) body[key] = Number(String(body[key]).replace(",", "."));
      }

      try {
        const company = await window.jsonRequest("/company", "PUT", body);
        renderCompany(company);
        window.showMessage("Datos de empresa guardados.", "success");
        window.refreshAll();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("rulesTable")?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-forget]");
      if (!button) return;

      try {
        await window.apiRequest(`/supplier-rules/${button.dataset.forget}`, { method: "DELETE" });
        window.showMessage("Regla olvidada.", "success");
        load();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    if (event.detail?.tab === "empresa") load();
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
