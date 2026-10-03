/* Confirmaciones y preguntas con el estilo de CapaFiscal.
   Sustituyen a confirm() y prompt() del navegador: mismo uso, pero con await.
     if (!(await window.askConfirm("¿Eliminar este borrador?"))) return;
     const motivo = await window.askText("Motivo del rechazo:", { required: true });  // null si se cancela
   El botón principal toma el verbo de la pregunta («¿Eliminar…?» → «Eliminar») y las acciones
   que destruyen algo se muestran en rojo con el foco en «Cancelar». */
(function () {
  const DESTRUCTIVE = /^(eliminar|borrar|descartar|anular|desconectar|desmarcar)$/i;
  let dialog = null;

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "document-detail-dialog ask-dialog";
    dialog.setAttribute("aria-labelledby", "askDialogMessage");
    dialog.innerHTML = `
      <form method="dialog" class="ask-form">
        <p class="ask-message" id="askDialogMessage"></p>
        <label class="ask-field hidden">
          <span class="ask-label"></span>
          <textarea rows="3"></textarea>
          <small class="ask-error" role="alert"></small>
        </label>
        <div class="dialog-footer ask-actions">
          <button type="button" class="btn-ghost" value="cancel" data-ask="cancel">Cancelar</button>
          <button type="submit" class="act-btn act-primary" value="ok" data-ask="ok">Aceptar</button>
        </div>
      </form>`;
    document.body.appendChild(dialog);
    return dialog;
  }

  function verbOf(message) {
    const first = (message.replace(/^[¿\s]+/, "").split(/[\s?,.]/)[0] || "").trim();
    return /^[a-záéíóú]+(ar|er|ir)$/i.test(first) ? first[0].toUpperCase() + first.slice(1).toLowerCase() : "";
  }

  function ask(message, { input = false, value = "", required = false, minLength = 0, label = "", confirmLabel = "", danger = null } = {}) {
    const node = dialog || build();
    const verb = verbOf(message);
    const destructive = danger ?? DESTRUCTIVE.test(verb);
    const okButton = node.querySelector('[data-ask="ok"]');
    const cancelButton = node.querySelector('[data-ask="cancel"]');
    const field = node.querySelector(".ask-field");
    const textarea = field.querySelector("textarea");
    const error = field.querySelector(".ask-error");

    node.querySelector(".ask-message").textContent = message;
    okButton.textContent = confirmLabel || (input ? "Aceptar" : verb || "Confirmar");
    okButton.classList.toggle("act-danger", destructive);
    field.classList.toggle("hidden", !input);
    field.querySelector(".ask-label").textContent = label || (required ? "Obligatorio" : "Opcional");
    textarea.value = value ?? "";
    textarea.required = Boolean(required);
    error.textContent = "";

    return new Promise((resolve) => {
      const finish = (result) => {
        node.removeEventListener("cancel", onCancel);
        okButton.removeEventListener("click", onOk);
        cancelButton.removeEventListener("click", onCancelClick);
        textarea.removeEventListener("keydown", onKey);
        if (node.open) node.close();
        resolve(result);
      };
      const onOk = (event) => {
        event.preventDefault();
        if (!input) return finish(true);
        const text = textarea.value;
        if ((required || minLength) && text.trim().length < Math.max(minLength, required ? 1 : 0)) {
          error.textContent = minLength > 1 ? `Escribe al menos ${minLength} caracteres.` : "Este campo es obligatorio.";
          textarea.focus();
          return;
        }
        finish(text);
      };
      const onCancel = (event) => { event.preventDefault(); finish(input ? null : false); };
      const onCancelClick = () => finish(input ? null : false);
      // Enter confirma; Mayús+Enter hace salto de línea
      const onKey = (event) => { if (event.key === "Enter" && !event.shiftKey) onOk(event); };

      node.addEventListener("cancel", onCancel);
      okButton.addEventListener("click", onOk);
      cancelButton.addEventListener("click", onCancelClick);
      textarea.addEventListener("keydown", onKey);
      node.showModal();
      (input ? textarea : destructive ? cancelButton : okButton).focus();
    });
  }

  window.askConfirm = (message, options = {}) => ask(message, { ...options, input: false });
  window.askText = (message, options = {}) => ask(message, { ...options, input: true });
})();

/* Plurales de verdad en lugar de «documento(s)»:
   pl(1, "asunto(s) urgente(s)") → "1 asunto urgente"; pl(3, "afirmación(es)") → "3 afirmaciones" */
window.pl = function pl(count, phrase) {
  const many = Number(count) !== 1;
  const text = String(phrase)
    .replace(/ón\(es\)/g, many ? "ones" : "ón")
    .replace(/\((es|s|n)\)/g, many ? "$1" : "");
  return `${count} ${text}`;
};
