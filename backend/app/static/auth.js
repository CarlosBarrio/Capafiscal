"use strict";
/* Acceso multiempresa: sesión por cookie, cliente activo y cabecera anti-CSRF.
   Sin AUTH_REQUIRED (empresa única) no hace nada. */
(function () {
  const nativeFetch = window.fetch.bind(window);

  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url || "";
    if (url.startsWith("/api")) {
      const headers = new Headers(init.headers || {});
      headers.set("X-CapaFiscal", "1");
      init = { ...init, headers, credentials: "same-origin" };
    }
    return nativeFetch(input, init);
  };

  function escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function overlay(html) {
    const box = document.createElement("div");
    box.id = "cf-auth";
    box.style.cssText = "position:fixed;inset:0;background:rgba(15,23,42,.55);display:flex;align-items:center;justify-content:center;z-index:9999";
    box.innerHTML = `<div style="background:#fff;padding:24px;border-radius:12px;min-width:320px;max-width:90vw;font-family:inherit">${html}</div>`;
    document.body.appendChild(box);
    return box;
  }

  function showLogin(message) {
    const box = overlay(`
      <h2 style="margin:0 0 12px">CapaFiscal · Iniciar sesión</h2>
      <p id="cf-auth-error" style="color:#b91c1c;min-height:1em">${escape(message || "")}</p>
      <form id="cf-login" style="display:grid;gap:8px">
        <input name="email" type="email" placeholder="Correo" required autocomplete="username">
        <input name="password" type="password" placeholder="Contraseña" required autocomplete="current-password">
        <button type="submit">Entrar</button>
      </form>`);
    box.querySelector("#cf-login").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const response = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      if (response.ok) window.location.reload();
      else box.querySelector("#cf-auth-error").textContent = "Correo o contraseña incorrectos.";
    });
  }

  async function chooseClient(id) {
    await fetch("/api/auth/client", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: Number(id) }) });
    try { localStorage.setItem("cf-client", String(id)); } catch (error) { /* sin almacenamiento: se elige cada vez */ }
    window.location.reload();
  }

  function userBar(user, current) {
    const bar = document.createElement("div");
    bar.style.cssText = "position:fixed;bottom:12px;right:12px;z-index:9000;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;font-size:13px;display:flex;gap:8px;align-items:center";
    const options = user.clients.map((item) => `<option value="${item.id}" ${String(item.id) === current ? "selected" : ""}>${escape(item.name)}</option>`).join("");
    bar.innerHTML = `<span>${escape(user.email)} · ${escape(user.role)}</span>`
      + (user.clients.length > 1 ? `<select id="cf-client">${options}</select>` : `<span>${escape(user.clients[0]?.name || "")}</span>`)
      + `<button id="cf-logout" type="button">Salir</button>`;
    document.body.appendChild(bar);
    bar.querySelector("#cf-client")?.addEventListener("change", (event) => chooseClient(event.target.value));
    bar.querySelector("#cf-logout").addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST" });
      try { localStorage.removeItem("cf-client"); } catch (error) { /* nada */ }
      window.location.reload();
    });
  }

  async function boot() {
    let status;
    try { status = await (await nativeFetch("/api/auth/status")).json(); } catch (error) { return; }
    if (!status.auth_required) return;
    const me = await fetch("/api/auth/me");
    if (me.status === 401) return showLogin();
    const user = await me.json();
    let current = null;
    try { current = localStorage.getItem("cf-client"); } catch (error) { current = null; }
    if (user.clients.length > 1 && !user.clients.some((item) => String(item.id) === current)) {
      const box = overlay(`<h2 style="margin:0 0 12px">¿Con qué cliente trabajas?</h2>`
        + user.clients.map((item) => `<button type="button" data-id="${item.id}" style="display:block;width:100%;margin:4px 0">${escape(item.name)}</button>`).join(""));
      box.querySelectorAll("button[data-id]").forEach((button) => button.addEventListener("click", () => chooseClient(button.dataset.id)));
      return;
    }
    if (user.clients.length > 1) {
      // La cookie del cliente puede haber caducado: se vuelve a fijar el elegido.
      await fetch("/api/auth/client", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: Number(current) }) });
    }
    userBar(user, current);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
