"use strict";

/* Banco conectado (PSD2): conectar, autorizar en el banco, sincronizar y renovar el consentimiento. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const STATUS = {
    LINKED: ["Conectado", "status-success"],
    PENDING: ["Falta autorizar", "status-warning"],
    EXPIRED: ["Renovar acceso", "status-danger"],
    ERROR: ["Error", "status-danger"],
  };

  function when(iso) {
    return iso ? new Date(iso).toLocaleString("es-ES", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "nunca";
  }

  function connectionHtml(item) {
    const [label, cls] = STATUS[item.status] || [item.status, "status-neutral"];
    const accounts = (item.accounts || []).map((account) => account.iban || account.name).filter(Boolean).join(" · ");
    const actions = item.status === "LINKED"
      ? `<button type="button" class="btn-ghost" data-bank-sync="${item.id}">Sincronizar</button>`
      : item.status === "PENDING" && item.link
        ? `<a class="act-btn act-primary" href="${esc(item.link)}">Autorizar en el banco</a>`
        : `<button type="button" class="act-btn act-primary" data-bank-renew="${esc(item.institution_id)}" data-bank-name="${esc(item.institution_name || "")}">Renovar acceso</button>`;
    return `
      <li class="bank-connection">
        <div class="work-item-text">
          <strong>${esc(item.institution_name || item.institution_id)} <span class="status-pill ${cls}">${esc(label)}</span></strong>
          <span class="work-why">${esc(accounts || "Sin cuentas todavía")} · última lectura ${esc(when(item.last_sync_at))}${item.last_result ? ` · ${esc(item.last_result)}` : ""}</span>
          ${item.last_error ? `<span class="danger-text">${esc(item.last_error)}</span>` : ""}
        </div>
        <div class="work-item-side">${actions}<button type="button" class="link-button" data-bank-remove="${item.id}">Desconectar</button></div>
      </li>`;
  }

  async function load() {
    const box = document.getElementById("bankConnections");
    if (!box) return;
    try {
      const data = await window.apiRequest("/bank/connections");
      if (!data.configured) {
        box.innerHTML = `<p class="board-empty">${esc(data.message)}</p>`;
        return;
      }
      box.innerHTML = `
        ${data.connections.length ? `<ul class="work-list">${data.connections.map(connectionHtml).join("")}</ul>` : `<p class="board-empty">Conecta tu banco y los movimientos llegarán solos cada 6 horas, sin descargar extractos.</p>`}
        <div class="card-actions bank-connect-row">
          <select id="bankInstitution" aria-label="Banco"><option value="">Elegir banco…</option></select>
          <button type="button" class="btn-ghost" id="bankConnect">Conectar banco</button>
        </div>`;
      const institutions = await window.apiRequest("/bank/institutions");
      document.getElementById("bankInstitution").innerHTML = `<option value="">Elegir banco…</option>` +
        institutions.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    } catch (error) {
      box.innerHTML = `<p class="danger-text">${esc(error.message)}</p>`;
    }
  }

  async function start(institutionId, name) {
    const created = await window.jsonRequest("/bank/connections", "POST", { institution_id: institutionId, institution_name: name || null });
    window.location.href = created.link;  // el titular autoriza en la web de su banco y vuelve aquí
  }

  async function act(event) {
    const target = event.target;
    try {
      if (target.closest("#bankConnect")) {
        const select = document.getElementById("bankInstitution");
        if (!select.value) return window.showMessage("Elige tu banco.", "error");
        return await start(select.value, select.options[select.selectedIndex].textContent);
      }
      const renew = target.closest("[data-bank-renew]");
      if (renew) return await start(renew.dataset.bankRenew, renew.dataset.bankName);
      const sync = target.closest("[data-bank-sync]");
      if (sync) {
        sync.disabled = true;
        const result = await window.jsonRequest(`/bank/connections/${sync.dataset.bankSync}/sync`, "POST", {});
        window.showMessage(result.last_error || result.last_result, result.last_error ? "error" : "success");
        window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));
        return load();
      }
      const remove = target.closest("[data-bank-remove]");
      if (remove && await window.askConfirm("¿Desconectar este banco? Los movimientos ya importados se quedan.")) {
        await window.apiRequest(`/bank/connections/${remove.dataset.bankRemove}`, { method: "DELETE", headers: { "X-CapaFiscal": "1" } });
        return load();
      }
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function returnFromBank() {
    const reference = new URLSearchParams(window.location.search).get("banco");
    if (!reference) return;
    window.history.replaceState({}, document.title, window.location.pathname);
    window.activateTab("negocio");
    try {
      const result = await window.jsonRequest(`/bank/connections/by-reference/${encodeURIComponent(reference)}/confirm`, "POST", {});
      window.showMessage(`Banco conectado. ${result.last_result || ""}`, "success");
      window.dispatchEvent(new CustomEvent("capafiscal:data-changed"));
    } catch (error) {
      window.showMessage(error.message, "error");
    }
    load();
  }

  function setup() {
    document.getElementById("bankConnections")?.addEventListener("click", act);
    returnFromBank();
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    if (event.detail?.tab === "negocio") load();
  });
  document.addEventListener("DOMContentLoaded", setup);
})();
