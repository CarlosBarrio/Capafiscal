"use strict";

/* Inteligencia: lo relevante que ocurre fuera de la empresa, con su fuente oficial.
   Cada novedad responde siete preguntas: qué ha pasado, de dónde viene, por qué es
   relevante, a quién afecta, qué significa, qué puedo hacer y qué evidencia lo demuestra. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const day = (value) => (value ? window.formatDay(value) : "");

  let radar = "juridico";
  let active = false;
  let options = null;
  let current = null;

  const RELEVANCE = { alta: ["Prioridad alta", "status-danger"], media: ["Informativa", "status-neutral"] };
  const STATUS = { revisada: "Revisada", descartada: "Descartada" };

  function mark(ok) {
    if (ok === true) return `<span class="ev-ok" aria-label="sí">✓</span>`;
    if (ok === false) return `<span class="ev-no" aria-label="no">✗</span>`;
    return `<span class="ev-mid" aria-label="a tener en cuenta">~</span>`;
  }

  function when(iso) {
    if (!iso) return "nunca";
    const date = new Date(iso);
    const today = new Date();
    const time = date.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
    return date.toDateString() === today.toDateString() ? `hoy · ${time}` : `${date.toLocaleDateString("es-ES")} · ${time}`;
  }

  /* ------------------------------------------------------------
  LISTA
  ------------------------------------------------------------ */
  async function load() {
    const list = document.getElementById("intelList");
    try {
      const data = await window.apiRequest(`/intelligence?radar=${radar}`);
      renderStatus(data);
      renderList(data);
      const badge = document.getElementById("cntIntel");
      if (badge && radar === "juridico") {
        const fresh = (data.counts?.nuevas) || 0;
        badge.textContent = fresh;
        badge.classList.toggle("hidden", !fresh);
      }
    } catch (error) {
      list.innerHTML = window.emptyState("alert", "No se pudo cargar Inteligencia", error.message);
    }
  }

  function renderStatus(data) {
    const status = document.getElementById("intelStatus");
    const source = data.source;
    if (data.pending_connector) {
      status.innerHTML = `<p class="intel-goal">${esc(data.goal)}</p>`;
      return;
    }
    const counts = data.counts || {};
    const parts = [`Actualizado ${esc(when(source.last_success))}`];
    if (counts.total !== undefined) {
      parts.push(`<strong>${counts.total}</strong> ${counts.total === 1 ? "novedad relevante" : "novedades relevantes"}`);
      if (counts.alta) parts.push(`${counts.alta} de prioridad alta`);
      if (counts.media) parts.push(`${counts.media} ${counts.media === 1 ? "informativa" : "informativas"}`);
    }
    status.innerHTML = `
      <p class="intel-summary">${parts.join(" · ")} <span class="muted">· fuente: ${esc(source.name)}</span></p>
      ${source.status === "error" ? `
        <div class="intel-source-error" role="status">
          ${window.icon("alert")}
          <div>
            <strong>No se pudo consultar ${esc(source.name)}.</strong>
            <span>${esc(source.last_error || "")} Se muestra lo último descargado; no se inventa nada.</span>
            <form class="intel-import" id="intelImport">
              <label>Sumario descargado (JSON de la API de datos abiertos)
                <input type="file" name="uploaded_file" accept=".json,application/json" required></label>
              <label>Día<input type="date" name="day" required></label>
              <button type="submit" class="btn-ghost">Importar</button>
            </form>
          </div>
        </div>` : ""}
    `;
  }

  function renderList(data) {
    const list = document.getElementById("intelList");
    if (data.pending_connector) {
      list.innerHTML = `
        <div class="intel-empty">
          <strong>Conector en preparación</strong>
          <p>${esc(data.message)}</p>
          <small class="muted">Fuente: ${esc(data.source.name)}. Hasta que esté conectada, aquí no se muestran resultados de ejemplo.</small>
        </div>`;
      return;
    }
    if (data.needs_profile) {
      list.innerHTML = `
        <div class="intel-empty">
          <strong>Indica tus áreas de práctica</strong>
          <p>CapaFiscal lee cada día el BOE, pero solo te enseña lo que toca tus áreas (laboral, mercantil, fiscal…).</p>
          <button type="button" class="act-btn act-primary" data-intel-profile>Configurar el perfil</button>
        </div>`;
      return;
    }
    if (!data.items.length) {
      list.innerHTML = `
        <div class="intel-empty">
          <strong>Nada relevante para tus áreas</strong>
          <p>En los últimos días el BOE no ha publicado nada que toque ${esc(areasText(data.profile))}.</p>
        </div>`;
      return;
    }
    list.innerHTML = data.items.map((item) => {
      const [label, className] = RELEVANCE[item.relevance] || [item.relevance, "status-neutral"];
      return `
        <article class="intel-item ${item.status !== "nueva" ? "is-done" : ""}">
          <div class="intel-meta">
            <span class="status-pill mini ${className}">${esc(label)}</span>
            <span>${esc(item.areas.join(" · "))}</span>
            <span>Publicado ${esc(day(item.publication_date))}</span>
            ${item.rank ? `<span>${esc(item.rank)}</span>` : ""}
            ${item.effective_date ? `<span>En vigor ${esc(day(item.effective_date))}</span>` : ""}
            ${STATUS[item.status] ? `<span class="intel-state">${esc(STATUS[item.status])}</span>` : ""}
          </div>
          <h3>${esc(item.title)}</h3>
          <p class="intel-why">${mark(true)} ${esc(item.why)}</p>
          <div class="intel-actions">
            <button type="button" class="btn-ghost" data-intel-open="${item.id}">Analizar</button>
            <a class="link-button" href="${esc(item.source_url)}" target="_blank" rel="noopener noreferrer">Fuente oficial · ${esc(item.external_id)}</a>
          </div>
        </article>`;
    }).join("");
  }

  function areasText(profile) {
    const areas = (profile?.juridico?.areas || []).map((code) => (options?.legal_areas || {})[code] || code);
    return areas.length ? areas.join(", ").toLowerCase() : "tus áreas";
  }

  /* ------------------------------------------------------------
  DETALLE: las siete preguntas
  ------------------------------------------------------------ */
  function claims(list, empty) {
    if (!list?.length) return `<p class="muted">${esc(empty)}</p>`;
    return `<ul class="intel-claims">${list.map((claim) => `
      <li><span>${esc(claim.text)}</span><q>${esc(claim.quote)}</q></li>`).join("")}</ul>`;
  }

  function section(number, question, body) {
    return `<section class="intel-q"><h3><span class="intel-q-n">${number}</span>${esc(question)}</h3>${body}</section>`;
  }

  async function openItem(id) {
    current = await window.apiRequest(`/intelligence/items/${id}`);
    const item = current;
    const [label] = RELEVANCE[item.relevance] || [item.relevance];
    document.getElementById("intelDialogEyebrow").textContent = `${label} · ${item.areas.join(" · ")} · ${item.source}`;
    document.getElementById("intelDialogTitle").textContent = item.title;
    const where = item.where_from;
    const ai = item.ai;
    document.getElementById("intelDialogBody").innerHTML = `
      ${section(1, "¿Qué ha pasado?", `
        ${item.what_happened.rank ? `<p>${esc(item.what_happened.rank)} · publicación en el BOE: ${esc(day(where.publication_date))}.</p>` : ""}
        ${item.what_happened.summary.length
          ? claims(item.what_happened.summary, "")
          : `<p class="muted">${ai.fallback ? `Sin resumen: ${esc(ai.fallback)}.` : "Sin resumen de IA."} Extracto del texto oficial:</p>
             ${item.what_happened.excerpt ? `<blockquote class="intel-excerpt">${esc(item.what_happened.excerpt)}</blockquote>` : `<p class="muted">El texto aún no se ha descargado.</p>`}`}`)}
      ${section(2, "¿De dónde viene?", `
        <dl class="intel-facts">
          <dt>Fuente</dt><dd>${esc(where.source)} · ${esc(where.document)}</dd>
          ${where.section ? `<dt>Sección</dt><dd>${esc(where.section)}</dd>` : ""}
          ${where.department ? `<dt>Organismo</dt><dd>${esc(where.department)}</dd>` : ""}
          ${where.category ? `<dt>Epígrafe</dt><dd>${esc(where.category)}</dd>` : ""}
          <dt>Publicado</dt><dd>${esc(day(where.publication_date))}</dd>
          <dt>Descargado</dt><dd>${esc(when(where.retrieved_at))}</dd>
        </dl>
        <p class="intel-links">
          <a href="${esc(where.html_url)}" target="_blank" rel="noopener noreferrer">Texto en boe.es</a>
          ${where.pdf_url ? `<a href="${esc(where.pdf_url)}" target="_blank" rel="noopener noreferrer">PDF oficial</a>` : ""}
          ${where.xml_url ? `<a href="${esc(where.xml_url)}" target="_blank" rel="noopener noreferrer">Ficha XML</a>` : ""}
        </p>`)}
      ${section(3, "¿Por qué CapaFiscal cree que es relevante?", `
        <ul class="intel-reasons">${item.why_relevant.map((reason) => `<li>${mark(reason.ok)} ${esc(reason.label)}</li>`).join("")}</ul>
        <small class="muted">Decidido con reglas sobre el título, el epígrafe, las materias oficiales y tu perfil. Sin puntuaciones inventadas.</small>`)}
      ${section(4, "¿A quién afecta?", claims(item.who_is_affected, "No determinado automáticamente: consulta el texto oficial."))}
      ${section(5, "¿Qué significa?", `
        <dl class="intel-facts">
          <dt>Entrada en vigor</dt><dd>${item.what_it_means.effective_date ? `${esc(day(item.what_it_means.effective_date))} <small class="muted">(ficha oficial)</small>` : "No consta en la ficha del BOE"}</dd>
          ${item.what_it_means.subjects.length ? `<dt>Materias</dt><dd>${esc(item.what_it_means.subjects.join(" · "))} <small class="muted">(clasificación oficial)</small></dd>` : ""}
        </dl>
        ${item.what_it_means.effective_claims.length ? claims(item.what_it_means.effective_claims, "") : ""}`)}
      ${section(6, "¿Qué puedo hacer?", `
        ${item.what_to_do.suggestions.length ? `<ul class="intel-reasons">${item.what_to_do.suggestions.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>
          <small class="muted">${esc(item.what_to_do.suggestions_note)}</small>` : `<p class="muted">Lee la disposición y decide si afecta a tus clientes. Márcala como revisada para que no vuelva a aparecer como nueva.</p>`}`)}
      ${section(7, "¿Qué evidencia lo demuestra?", `
        <ol class="intel-evidence">${item.evidence.map((entry) => `
          <li>
            <strong>${esc(entry.claim)}</strong>${entry.verified ? ` <span class="status-pill mini status-success">cita comprobada</span>` : ""}
            <q>${esc(entry.fragment)}</q>
            <small class="muted">${esc(entry.source)} · ${esc(entry.document)}${entry.date ? ` · ${esc(day(entry.date))}` : ""} · <a href="${esc(entry.url)}" target="_blank" rel="noopener noreferrer">ver</a></small>
          </li>`).join("")}</ol>
        ${ai.engine ? `<small class="muted">Resumen de IA (${esc(ai.engine)}): cada afirmación lleva una cita encontrada en el texto oficial${ai.discarded ? `; ${ai.discarded} afirmación(es) descartada(s) por no poder citarse` : ""}${ai.truncated ? "; texto largo, se analizó el principio" : ""}.</small>` : ""}`)}
    `;
    document.getElementById("intelDialogActions").innerHTML = `
      <a class="btn-ghost" href="${esc(where.html_url)}" target="_blank" rel="noopener noreferrer">${window.icon("doc")} Fuente oficial</a>
      <span class="spacer"></span>
      ${item.status !== "descartada" ? `<button type="button" class="btn-ghost" data-intel-status="descartada">No me interesa</button>` : `<button type="button" class="btn-ghost" data-intel-status="nueva">Volver a mostrar</button>`}
      ${item.status !== "revisada" ? `<button type="button" class="act-btn act-primary" data-intel-status="revisada">${window.icon("check")} Marcar revisada</button>` : ""}
    `;
    const dialog = document.getElementById("intelDialog");
    if (!dialog.open) dialog.showModal();
  }

  async function setStatus(status) {
    if (!current) return;
    try {
      await window.jsonRequest(`/intelligence/items/${current.id}/status`, "POST", { status });
      document.getElementById("intelDialog").close();
      await load();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  PERFIL
  ------------------------------------------------------------ */
  async function renderProfile() {
    const form = document.getElementById("intelProfile");
    const data = await window.apiRequest("/intelligence/profile");
    options = data.options;
    const profile = data.profile;
    if (radar !== "juridico") {
      form.innerHTML = `<p class="muted">El perfil de ${esc(radar)} se configurará con su conector. Datos comunes ya guardados: ${esc(profile.common.region || "sin comunidad")}${profile.common.sector ? ` · ${esc(profile.common.sector)}` : ""}.</p>`;
      return;
    }
    form.innerHTML = `
      <fieldset>
        <legend>Áreas de práctica</legend>
        <div class="intel-checks">
          ${Object.entries(options.legal_areas).map(([code, label]) => `
            <label class="check-inline"><input type="checkbox" name="areas" value="${esc(code)}" ${profile.juridico.areas.includes(code) ? "checked" : ""}> ${esc(label)}</label>`).join("")}
        </div>
      </fieldset>
      <div class="intel-profile-row">
        <label>Comunidad autónoma
          <select name="region">
            <option value="">Sin indicar (todas las normas autonómicas)</option>
            ${options.regions.map((region) => `<option ${profile.common.region === region ? "selected" : ""}>${esc(region)}</option>`).join("")}
          </select>
        </label>
        <label>Tipo de clientes
          <input type="text" name="client_types" maxlength="200" value="${esc((profile.juridico.client_types || []).join(", "))}" placeholder="Ej.: pymes, autónomos, particulares">
        </label>
      </div>
      <div class="intel-profile-actions">
        <small class="muted">Al guardar se vuelve a calcular la relevancia con lo ya descargado.</small>
        <button type="submit" class="act-btn act-primary">Guardar perfil</button>
      </div>
    `;
  }

  async function saveProfile(form) {
    const data = new FormData(form);
    const payload = {
      common: { region: data.get("region") || null },
      juridico: {
        areas: data.getAll("areas"),
        client_types: String(data.get("client_types") || "").split(",").map((item) => item.trim()).filter(Boolean),
      },
    };
    try {
      await window.jsonRequest("/intelligence/profile", "PUT", payload);
      window.showMessage("Perfil guardado: relevancia recalculada.", "success");
      toggleProfile(false);
      await load();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  function toggleProfile(open) {
    const form = document.getElementById("intelProfile");
    const button = document.getElementById("intelProfileToggle");
    const show = open ?? form.classList.contains("hidden");
    form.classList.toggle("hidden", !show);
    button.setAttribute("aria-expanded", String(show));
    if (show) renderProfile().catch((error) => { form.innerHTML = `<p class="danger-text">${esc(error.message)}</p>`; });
  }

  function selectRadar(code) {
    radar = code;
    document.querySelectorAll("#intelRadars .segment").forEach((button) => {
      const on = button.dataset.radar === code;
      button.classList.toggle("active", on);
      button.setAttribute("aria-selected", String(on));
    });
    if (!document.getElementById("intelProfile").classList.contains("hidden")) renderProfile();
    load();
  }

  function setup() {
    const section = document.getElementById("tab-inteligencia");
    if (!section) return;
    document.getElementById("intelRadars").addEventListener("click", (event) => {
      const button = event.target.closest("[data-radar]");
      if (button) selectRadar(button.dataset.radar);
    });
    document.getElementById("intelProfileToggle").addEventListener("click", () => toggleProfile());
    document.getElementById("intelProfile").addEventListener("submit", (event) => {
      event.preventDefault();
      saveProfile(event.currentTarget);
    });
    document.getElementById("intelRefresh").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await window.jsonRequest("/intelligence/refresh", "POST", {});
        const fetched = result.fetched;
        window.showMessage(fetched.errors.length ? `No se pudo consultar el BOE: ${fetched.errors[0]}` : `BOE consultado: ${fetched.new} disposición(es) nuevas, ${result.matched.visible} relevante(s).`, fetched.errors.length ? "error" : "success");
        await load();
      } catch (error) {
        window.showMessage(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    section.addEventListener("click", (event) => {
      const open = event.target.closest("[data-intel-open]");
      if (open) return openItem(Number(open.dataset.intelOpen)).catch((error) => window.showMessage(error.message, "error"));
      if (event.target.closest("[data-intel-profile]")) return toggleProfile(true);
    });
    section.addEventListener("submit", async (event) => {
      if (event.target.id !== "intelImport") return;
      event.preventDefault();
      try {
        const result = await window.apiRequest("/intelligence/import", { method: "POST", body: new FormData(event.target) });
        window.showMessage(`Sumario importado: ${result.fetched.new} disposición(es) nuevas.`, "success");
        await load();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
    document.getElementById("intelDialog").addEventListener("click", (event) => {
      const button = event.target.closest("[data-intel-status]");
      if (button) setStatus(button.dataset.intelStatus);
    });
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "inteligencia";
    if (active) load();
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
