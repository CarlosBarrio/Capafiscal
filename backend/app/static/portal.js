"use strict";
// Portal de subida para terceros (enlace sin usuario). Fuera del HTML para cumplir la CSP.
const token = location.pathname.split("/").filter(Boolean).pop();
const $ = (id) => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };

fetch(`/api/portal/${encodeURIComponent(token)}`).then(async (response) => {
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Enlace no válido.");
  if (data.company) text("company", data.company);
  text("label", data.label);
  const parts = [data.case_title, data.reference ? `ref. ${data.reference}` : null,
    data.deadline ? `antes del ${data.deadline.split("-").reverse().join("/")}` : null].filter(Boolean);
  text("meta", parts.join(" · "));
  $("what").classList.remove("hidden");
  if (data.status === "RECEIVED") {
    text("intro", "Ya recibimos este documento. Si quieres, puedes enviar una versión nueva.");
  } else if (data.status === "CANCELLED") {
    text("intro", "Esta petición ya no está activa. No hace falta que envíes nada.");
    return;
  } else {
    text("intro", "Sube el documento y lo incorporamos al expediente. No necesitas usuario ni contraseña.");
  }
  $("form").classList.remove("hidden");
}).catch((error) => text("intro", error.message));

const input = $("file");
const drop = $("drop");
input.addEventListener("change", () => {
  text("fileName", input.files[0]?.name || "Pulsa para elegir el archivo");
  $("send").disabled = !input.files.length;
});
["dragover", "dragenter"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, () => drop.classList.remove("over")));
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  input.files = event.dataTransfer.files;
  input.dispatchEvent(new Event("change"));
});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = new FormData();
  body.append("uploaded_file", input.files[0]);
  $("send").disabled = true;
  text("send", "Enviando…");
  try {
    const response = await fetch(`/api/portal/${encodeURIComponent(token)}`, { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "No se pudo enviar.");
    const ok = data.verification?.status === "ok";
    const result = $("result");
    result.className = `result ${ok || data.verification?.status === "unverified" ? "ok" : "warn"}`;
    result.textContent = ok || data.verification?.status === "unverified"
      ? "¡Recibido! Ya está en el expediente. Gracias."
      : "Recibido. Lo revisaremos porque no hemos podido confirmar que sea el documento pedido; si no lo es, te avisaremos.";
    $("form").classList.add("hidden");
  } catch (error) {
    const result = $("result");
    result.className = "result warn";
    result.textContent = error.message;
    $("send").disabled = false;
    text("send", "Enviar");
  }
});
