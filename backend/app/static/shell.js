"use strict";

/* Estructura de la aplicación: menú lateral, ruta de navegación y
   buscador rápido (Ctrl+K). */
(() => {
  const esc = (value) => window.escapeHtml(value);

  const SECTIONS = {
    panel: ["Inicio", "Hoy", "home"],
    asistente: ["Inicio", "Asistente", "sparkles"],
    facturas: ["Operación", "Facturas", "file"],
    notificaciones: ["Operación", "Notificaciones", "landmark"],
    actividad: ["Operación", "Actividad", "activity"],
    negocio: ["Finanzas", "Negocio", "trending"],
    impuestos: ["Finanzas", "Impuestos", "receipt"],
    informes: ["Finanzas", "Informes", "chart"],
    proveedores: ["Finanzas", "Terceros", "briefcase"],
    equipo: ["Personas", "Equipo", "users"],
    nominas: ["Personas", "Nóminas", "wallet"],
    cumplimiento: ["Empresa", "Cumplimiento", "shield"],
    conectores: ["Empresa", "Conectores", "plug"],
    empresa: ["Empresa", "Mi empresa", "building"],
  };

  const ACTIONS = [
    { label: "Subir documentos", hint: "Facturas o notificaciones en PDF", icon: "upload", run: () => document.getElementById("fileInput")?.click() },
    { label: "Nueva persona", hint: "Equipo", icon: "users", run: () => { window.activateTab("equipo"); window.setTimeout(() => document.getElementById("newEmployeeButton")?.click(), 50); } },
    { label: "Preparar nómina del mes", hint: "Nóminas", icon: "wallet", run: () => { window.activateTab("nominas"); document.getElementById("payrollCreateForm")?.scrollIntoView({ behavior: "smooth" }); } },
    { label: "Importar extracto bancario", hint: "Negocio · conciliación", icon: "link", run: () => { window.activateTab("negocio"); window.setTimeout(() => document.getElementById("bankImportForm")?.scrollIntoView({ behavior: "smooth" }), 300); } },
    { label: "Registrar notificación", hint: "AEAT, Seguridad Social…", icon: "landmark", run: () => { window.activateTab("notificaciones"); const form = document.getElementById("notificationFormCard"); if (form) { form.open = true; form.scrollIntoView({ behavior: "smooth" }); } } },
    { label: "Exportar libro registro", hint: "Informes", icon: "download", run: () => window.activateTab("informes") },
    { label: "Ver borrador del 303", hint: "Impuestos", icon: "receipt", run: () => window.activateTab("impuestos") },
  ];

  let employees = null;
  let results = [];
  let selectedIndex = 0;

  /* ------------------------------------------------------------
  MENÚ Y RUTA
  ------------------------------------------------------------ */
  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
  }

  function updateBreadcrumb(tab) {
    const [group, label] = SECTIONS[tab] || ["Inicio", "Hoy"];
    document.getElementById("breadcrumbGroup").textContent = group;
    document.getElementById("breadcrumbCurrent").textContent = label;
    document.title = `${label} · CapaFiscal`;
  }

  /* ------------------------------------------------------------
  BUSCADOR
  ------------------------------------------------------------ */
  async function loadEmployees() {
    if (employees) return employees;

    try {
      employees = await window.apiRequest("/team/employees");
    } catch {
      employees = [];
    }

    return employees;
  }

  function normalize(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  }

  function buildResults(query) {
    const text = normalize(query.trim());
    const matches = (value) => !text || normalize(value).includes(text);
    const list = [];

    for (const [tab, [group, label, iconName]] of Object.entries(SECTIONS)) {
      if (matches(`${label} ${group}`)) {
        list.push({ type: "Ir a", label, hint: group, icon: iconName, run: () => window.activateTab(tab) });
      }
    }

    for (const action of ACTIONS) {
      if (matches(`${action.label} ${action.hint}`)) list.push({ type: "Acción", ...action });
    }

    if (text.length >= 2) {
      const documents = (window.documentsCache || []).filter((item) => {
        const invoice = item.invoice || {};
        return matches([
          item.original_filename, invoice.supplier_name, invoice.customer_name,
          invoice.invoice_number, invoice.supplier_tax_id, invoice.concept,
        ].join(" "));
      }).slice(0, 6);

      for (const item of documents) {
        const invoice = item.invoice || {};
        const party = invoice.direction === "ISSUED" ? invoice.customer_name : invoice.supplier_name;
        list.push({
          type: "Documento",
          label: party || item.original_filename,
          hint: [invoice.invoice_number, invoice.total !== undefined && invoice.total !== null ? window.formatMoney(invoice.total) : null]
            .filter(Boolean).join(" · ") || item.original_filename,
          icon: item.kind === "NOTIFICATION" ? "landmark" : "file",
          run: () => (item.kind === "NOTIFICATION" ? window.activateTab("notificaciones") : window.showDetail(item.id)),
        });
      }

      for (const person of (employees || []).filter((item) => matches(`${item.name} ${item.job_title} ${item.department} ${(item.skills || []).join(" ")}`)).slice(0, 5)) {
        list.push({
          type: "Persona",
          label: person.name,
          hint: [person.job_title, person.department].filter(Boolean).join(" · "),
          icon: "users",
          run: () => { window.activateTab("equipo"); window.openEmployee?.(person.id); },
        });
      }
    }

    return list.slice(0, 14);
  }

  function renderResults() {
    const container = document.getElementById("paletteResults");

    if (!results.length) {
      container.innerHTML = `<p class="palette-empty">Sin resultados. Prueba con el nombre de un proveedor, un número de factura o una sección.</p>`;
      return;
    }

    let lastType = null;
    container.innerHTML = results.map((item, index) => {
      const header = item.type !== lastType ? `<p class="palette-group">${esc(item.type)}</p>` : "";
      lastType = item.type;
      return `
        ${header}
        <button type="button" class="palette-item ${index === selectedIndex ? "selected" : ""}" data-index="${index}">
          <span class="palette-icon">${window.icon(item.icon)}</span>
          <span class="palette-text">
            <strong>${esc(item.label)}</strong>
            ${item.hint ? `<small>${esc(item.hint)}</small>` : ""}
          </span>
          ${index === selectedIndex ? `<kbd>↵</kbd>` : ""}
        </button>
      `;
    }).join("");

    container.querySelector(".palette-item.selected")?.scrollIntoView({ block: "nearest" });
  }

  function refresh() {
    results = buildResults(document.getElementById("paletteInput").value);
    selectedIndex = 0;
    renderResults();
  }

  async function openPalette() {
    const palette = document.getElementById("palette");
    palette.classList.remove("hidden");
    const input = document.getElementById("paletteInput");
    input.value = "";
    refresh();
    input.focus();
    await loadEmployees();
    if (!palette.classList.contains("hidden")) refresh();
  }

  function closePalette() {
    document.getElementById("palette").classList.add("hidden");
  }

  function runSelected(index = selectedIndex) {
    const item = results[index];
    if (!item) return;
    closePalette();
    item.run();
  }

  function setup() {
    document.getElementById("menuToggle")?.addEventListener("click", () => setSidebar(true));
    document.getElementById("sidebarClose")?.addEventListener("click", () => setSidebar(false));
    document.getElementById("sidebarBackdrop")?.addEventListener("click", () => setSidebar(false));
    document.getElementById("searchTrigger")?.addEventListener("click", openPalette);

    const palette = document.getElementById("palette");
    palette.addEventListener("click", (event) => {
      if (event.target === palette) closePalette();
      const item = event.target.closest(".palette-item");
      if (item) runSelected(Number(item.dataset.index));
    });

    const input = document.getElementById("paletteInput");
    input.addEventListener("input", refresh);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        selectedIndex = Math.min(results.length - 1, selectedIndex + 1);
        renderResults();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        selectedIndex = Math.max(0, selectedIndex - 1);
        renderResults();
      } else if (event.key === "Enter") {
        event.preventDefault();
        runSelected();
      }
    });

    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        openPalette();
      } else if (event.key === "Escape") {
        closePalette();
        setSidebar(false);
      }
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    updateBreadcrumb(event.detail?.tab);
    setSidebar(false);
    window.scrollTo({ top: 0 });
  });

  window.addEventListener("capafiscal:data-changed", () => {
    employees = null;
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
