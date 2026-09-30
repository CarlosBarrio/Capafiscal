"use strict";

(() => {
  let busy = false;
  let greeted = false;

  function chatWindow() {
    return document.getElementById("chatWindow");
  }

  function appendMessage(role, html) {
    const container = chatWindow();
    if (!container) return null;

    const item = document.createElement("div");
    item.className = `chat-msg ${role}`;
    item.innerHTML = `
      <span class="chat-avatar" aria-hidden="true">${role === "user" ? "🧑" : "📒"}</span>
      <div class="chat-text">${html}</div>
    `;
    container.appendChild(item);
    container.scrollTop = container.scrollHeight;
    return item;
  }

  function sourcesHtml(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";

    return `
      <ul class="chat-sources">
        ${sources.map((source) => {
          const label = window.escapeHtml(source.label || "Fuente");
          if (source.document_id) {
            return `<li><button type="button" class="link-button" onclick="showDetail(${Number(source.document_id)})">${label}</button></li>`;
          }
          return `<li>${label}</li>`;
        }).join("")}
      </ul>
    `;
  }

  async function ask(question) {
    const text = String(question || "").trim();
    if (text.length < 2 || busy) return;

    busy = true;
    appendMessage("user", window.escapeHtml(text));
    const pending = appendMessage("bot", `<span class="chat-typing">Consultando tus datos…</span>`);

    try {
      const result = await window.apiRequest("/assistant/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text }),
      });

      pending.querySelector(".chat-text").innerHTML = `
        ${window.escapeHtml(result.answer)}
        ${sourcesHtml(result.sources)}
        ${result.warning ? `<p class="chat-warning">${window.escapeHtml(result.warning)}</p>` : ""}
      `;
    } catch (error) {
      pending.querySelector(".chat-text").innerHTML =
        `No he podido responder: ${window.escapeHtml(error.message)}`;
    } finally {
      busy = false;
      const container = chatWindow();
      if (container) container.scrollTop = container.scrollHeight;
    }
  }

  function greet() {
    if (greeted) return;
    greeted = true;
    appendMessage(
      "bot",
      "Hola. Puedo decirte el IVA soportado de un trimestre (por ejemplo «IVA del 2T»), " +
      "qué pagos tienes pendientes, cuánto has gastado con un proveedor, qué riesgos " +
      "o tareas hay abiertos, o darte un resumen de situación."
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("chatForm");
    const input = document.getElementById("chatInput");

    form?.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value;
      input.value = "";
      ask(value);
    });

    document.getElementById("chatSuggestions")?.addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (chip) ask(chip.textContent);
    });
  });

  window.addEventListener("capafiscal:tab-changed", (event) => {
    if (event.detail?.tab === "asistente") {
      greet();
      document.getElementById("chatInput")?.focus();
    }
  });
})();
