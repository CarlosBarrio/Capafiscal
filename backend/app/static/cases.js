"use strict";

/* Expedientes: el trabajo de los agentes, listo para revisar y aprobar. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);
  const day = (value) => window.formatDay(value);

  const LEVEL_LABELS = { critical: "Urgente", high: "Revisión necesaria", normal: "Para revisar", low: "Informativo" };
  const STATUS_CLASS = {
    WAITING_HUMAN: "status-warning",
    WAITING_DOCS: "status-info",
    READY_TO_FILE: "status-success",
    FILED: "status-success",
    RESOLVED: "status-neutral",
    DISMISSED: "status-neutral",
    OPEN: "status-neutral",
  };
  const DOC_CLASS = {
    ready: "status-success",
    received: "status-success",
    provided: "status-success",
    partial: "status-warning",
    requested: "status-info",
    missing: "status-danger",
    not_applicable: "status-neutral",
  };
  const KIND_LABELS = { document: "Documento", notification: "Notificación", case: "Expediente", filing: "Presentación" };
  const RISK_CLASS = { high: "status-danger", medium: "status-warning", low: "status-neutral" };
  const RISK_LABELS = { high: "Riesgo alto", medium: "Riesgo medio", low: "Riesgo bajo" };
  const AGENT_NAMES = { vigilante: "Vigilante", expedientes: "Expedientes", fiscal: "Fiscal", memoria: "Memoria", detector: "Detector", gestor: "Gestor", perseguidor: "Perseguidor", director: "Director" };
  const AGENT_ICONS = { vigilante: "eye", expedientes: "archive", fiscal: "receipt", memoria: "brain", detector: "alert", gestor: "briefcase", perseguidor: "send", director: "chart" };
  const INTAKE_CLASS = { COMPLETED: "status-success", NEEDS_HUMAN: "status-warning", FAILED: "status-danger", PROCESSING: "status-info", RECEIVED: "status-neutral" };
  const SOURCE_LABELS = { upload: "Subida", email: "Correo", dehu: "DEHú", api: "API", calendario: "Calendario", pendientes: "Pendientes", test: "Prueba" };
  const KIND_EVENT_LABELS = { document: "Documento", notification: "Notificación", invoice: "Factura", deadline: "Plazo" };
  const PIPELINE_LABELS = { notification: "Notificación", invoice: "Factura", deadline: "Plazo", anomalies: "Barrido del Detector" };
  const SOURCE_ICONS = { case: "archive", tax: "receipt", collection: "coins", payment: "card", compliance: "shield", team: "users", payroll: "wallet", outbox: "send", timesheet: "clock" };

  let active = false;
  let view = "open";
  let current = null;
  let pane = "resumen";
  let pendingItemCode = null;

  function daysText(days) {
    if (days === null || days === undefined) return ["Sin plazo", "confírmalo"];
    if (days < 0) return [`${Math.abs(days)} d`, "vencido"];
    if (days === 0) return ["Hoy", "vence hoy"];
    return [`${days} d`, days === 1 ? "queda 1 día" : `quedan ${days} días`];
  }

  // El titular del expediente es el trámite; a quién afecta solo se dice cuando no es tu propia empresa.
  function subjectLine(item, always = false) {
    const subject = item.subject || {};
    if (!subject.name) return "";
    if (subject.type === "company") return always ? `Afecta a tu empresa${subject.tax_id ? ` · ${subject.tax_id}` : ""}` : "";
    const kinds = { employee: "persona de la plantilla", customer: "cliente", supplier: "proveedor", unknown: "tercero" };
    return `Afecta a ${subject.name}${kinds[subject.type] ? ` · ${kinds[subject.type]}` : ""}`;
  }

  /* ------------------------------------------------------------
  DIRECTOR: lo que hay que revisar hoy
  ------------------------------------------------------------ */
  function briefingHtml(data, compact) {
    if (!data.items.length) {
      return `
        <div class="briefing-empty">${window.icon("check")}<div><strong>Nada requiere tu atención hoy.</strong><span>Los agentes siguen vigilando buzones, facturas y banco.</span></div></div>
      `;
    }
    const items = compact ? data.items.slice(0, 5) : data.items;
    return `
      <div class="briefing-head">
        <span class="briefing-eyebrow">${window.icon("chart")} Director de cartera · ${esc(window.formatDay(data.date))}</span>
        <h2>${esc(data.headline)}</h2>
      </div>
      <ol class="briefing-list">
        ${items.map((item, index) => `
          <li>
            <button type="button" class="briefing-item lvl-${esc(item.level)}" data-brief="${index}">
              <span class="briefing-rank">${index + 1}</span>
              <span class="briefing-icon">${window.icon(SOURCE_ICONS[item.source] || "calendar")}</span>
              <span class="briefing-body">
                <strong>${esc(item.title)}</strong>
                <span>${esc(item.detail || "")}</span>
              </span>
              <span class="briefing-when">
                ${item.deadline ? `<small>${esc(day(item.deadline))}</small>` : ""}
                ${window.icon("chevron")}
              </span>
            </button>
          </li>
        `).join("")}
      </ol>
      ${compact && data.items.length > 5 ? `<button type="button" class="link-button briefing-more" data-go-cases>Ver las ${data.total} en Expedientes</button>` : ""}
    `;
  }

  function percent(value) {
    return `${Math.round((value || 0) * 100)} %`;
  }

  function fiscalHtml(models) {
    if (!models?.length) return `<p class="board-empty">Sin obligaciones trimestrales que vigilar.</p>`;
    return models.map((item) => {
      const amount = (item.headline || "").replace(/^\S+ estimado:\s*/, "");
      const rest = (item.summary || "").split(" · ").slice(1).join(" · ");
      const days = item.days_left;
      return `
        <div class="today-fiscal-row">
          <div class="today-fiscal-top">
            <strong>Modelo ${esc(item.model)} <span class="muted">${esc(item.period)}</span></strong>
            <span class="muted">${item.status === "FILED" ? "presentado" : days === null || days === undefined ? "" : days < 0 ? `venció hace ${Math.abs(days)} d` : `vence ${esc(day(item.due_date))} · ${days} d`}</span>
          </div>
          <span class="today-fiscal-amount">${esc(amount)}</span>
          <span class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round((item.information_available || 0) * 100)}" aria-label="Información disponible del ${esc(item.model)}"><span style="width:${Math.round((item.information_available || 0) * 100)}%"></span></span>
          <small class="muted">${percent(item.information_available)} de la información${rest ? ` · ${esc(rest)}` : ""}</small>
        </div>`;
    }).join("");
  }

  function bankHtml(bank) {
    if (!bank?.total) return `<p class="board-empty">Importa el extracto del banco para conciliar pagos y cobros.</p>`;
    const counts = bank.counts || {};
    const levels = bank.levels || {};
    return `
      <dl class="today-stats">
        <div><dt>movimientos</dt><dd>${bank.total}</dd></div>
        <div><dt>conciliados</dt><dd>${counts.CONCILIADO || 0}</dd></div>
        <div><dt>posibles</dt><dd>${levels.PROBABLE || 0}</dd></div>
        <div><dt>sin justificar</dt><dd>${levels.SIN_MATCH ?? counts.SIN_FACTURA ?? 0}</dd></div>
      </dl>
      ${levels.CONFLICTO || counts.FACTURA_SIN_PAGO ? `<small class="muted">${[
        levels.CONFLICTO ? `${levels.CONFLICTO} en conflicto (varias facturas posibles, importe distinto o duplicado): los decides tú` : "",
        counts.FACTURA_SIN_PAGO ? `${counts.FACTURA_SIN_PAGO} factura(s) aprobadas sin pago en el banco` : "",
      ].filter(Boolean).join(" · ")}</small>` : ""}
    `;
  }

  function boardHtml(board, pulse) {
    if (!board) return "";
    const attention = board.attention.items;
    const countList = (items, empty) => items.length
      ? `<ul class="board-counts">${items.map((item) => `<li><strong>${item.count}</strong> ${esc(item.label)}</li>`).join("")}</ul>`
      : `<p class="board-empty">${esc(empty)}</p>`;
    const topItem = (item, index) => `
      <li>
        <button type="button" class="today-top-item lvl-${esc(item.level)}" ${item.case_id ? `data-open-case="${item.case_id}"` : item.event_id ? `data-go-intake="${item.event_id}"` : ""}>
          <span class="briefing-rank">${index + 1}</span>
          <span class="today-top-body">
            <strong>${esc(item.title)}</strong>
            <span class="today-top-why">${esc(item.impact?.why || item.reason || "")}</span>
          </span>
          <span class="today-top-go">Abrir ${window.icon("chevron")}</span>
        </button>
      </li>`;
    const extra = attention.length - board.top.length;
    return `
      <div class="board-head">
        <span class="briefing-eyebrow">${window.icon("chart")} Director · ${esc(window.formatDay(board.date))}</span>
        <span class="board-head-side">
          ${pulse ? `<span class="board-pulse ${pulse.healthy ? "is-ok" : "is-late"}" title="${esc(pulse.triggers.map((item) => `${item.label}: ${item.events_today} hoy`).join(" · "))}"><span class="pulse-dot"></span>${esc(pulse.label)}</span>` : ""}
          <button type="button" class="btn-ghost" data-run-pulse>${window.icon("play")} Trabajar ahora</button>
        </span>
      </div>
      <div class="today-states">
        <section class="today-state state-red">
          <h3><span class="board-dot"></span>Requiere tu atención</h3>
          <strong class="today-count">${attention.length}</strong>
          <p class="board-empty">${attention.length ? "Decisiones tuyas, ordenadas abajo por impacto." : "Ningún plazo encima ni nada bloqueado."}</p>
        </section>
        <section class="today-state state-orange">
          <h3><span class="board-dot"></span>Pendiente</h3>
          <strong class="today-count">${board.pending.count}</strong>
          ${countList(board.pending.items, "Nada a medias.")}
        </section>
        <section class="today-state state-green">
          <h3><span class="board-dot"></span>Resuelto sin ti</h3>
          <strong class="today-count">${board.resolved.count}</strong>
          ${countList(board.resolved.items, "Aún no hay trabajo automático esta semana.")}
        </section>
      </div>
      <div class="today-grid">
        <section class="today-block">
          <div class="today-block-head"><h3>Fiscal</h3><button type="button" class="link-button" data-go-tab="impuestos">Ver impuestos</button></div>
          ${fiscalHtml(board.fiscal)}
        </section>
        <section class="today-block">
          <div class="today-block-head"><h3>Banco</h3><button type="button" class="link-button" data-go-tab="negocio" data-go-anchor="bankCard">Ver conciliación</button></div>
          ${bankHtml(board.bank)}
        </section>
      </div>
      ${board.top.length ? `
        <div class="today-top">
          <h3>${board.top.length === 1 ? "Lo que deberías revisar hoy" : `Las ${board.top.length} cosas que deberías revisar hoy`}</h3>
          <ol>${board.top.map(topItem).join("")}</ol>
          ${extra > 0 ? `<button type="button" class="link-button" data-go-cases>Y ${extra} más en Expedientes</button>` : ""}
        </div>` : ""}
      ${board.work ? `
        <p class="today-work">
          Últimos ${board.period_days} días: ${board.work.events} entradas trabajadas · ${board.work.documents} documentos · ${board.work.cases} expedientes · ${board.work.anomalies} anomalías · ${board.work.requests} documentos pedidos${board.intervention.total ? ` · <span title="${esc(`${board.intervention.solo} solos · ${board.intervention.with_ai} con IA validada por reglas · ${board.intervention.human} a una persona · ${board.intervention.failed} fallidos`)}">intervención humana ${(board.intervention.human_rate * 100).toFixed(1).replace(".", ",")} %</span>` : ""}${board.time_saved.minutes ? ` · <span title="${esc(board.time_saved.note + " " + board.time_saved.assumptions.map((item) => `${item.count} × ${item.minutes} min (${item.what})`).join("; "))}">≈ ${String(board.time_saved.hours).replace(".", ",")} h ahorradas (estimación)</span>` : ""}
        </p>` : ""}
    `;
  }

  let briefingItems = [];

  async function loadBriefing() {
    const data = await window.apiRequest("/briefing");
    briefingItems = data.items;
    const badge = document.getElementById("cntCases");
    if (badge) {
      badge.textContent = data.counts.waiting_human;
      badge.classList.toggle("hidden", !data.counts.waiting_human);
    }
    // Una línea, sin ceros: solo lo que hay.
    const set = (id, value) => {
      const element = document.getElementById(id);
      if (!element) return;
      element.textContent = value;
      element.closest("li")?.classList.toggle("hidden", !value);
    };
    set("casesWaiting", data.counts.waiting_human);
    set("casesDocs", data.counts.waiting_docs);
    set("casesReady", data.counts.ready_to_file);
    set("casesAnomalies", data.counts.anomalies);

    const home = document.getElementById("homeBriefing");
    if (home) {
      home.innerHTML = data.board ? boardHtml(data.board, data.pulse) : briefingHtml(data, true);
      home.classList.remove("hidden");
    }
  }

  function openBriefingItem(index) {
    const item = briefingItems[index];
    if (!item) return;
    if (item.case_id) return openCase(item.case_id);
    if (item.document_id && (item.source === "payment" || item.source === "collection")) return window.showDetail?.(item.document_id, "payment");
    window.activateTab(item.tab || "panel");
  }

  /* ------------------------------------------------------------
  LISTADO
  ------------------------------------------------------------ */
  async function loadList() {
    const items = await window.apiRequest(`/cases?view=${view}`);
    const container = document.getElementById("caseList");
    if (!items.length) {
      const empty = {
        open: ["archive", "Sin expedientes abiertos", "Cuando llegue una notificación, los agentes la trabajarán y aparecerá aquí lista para revisar."],
        anomalies: ["check", "Todo cuadra", "El detector revisa cada mañana facturas, banco e IVA. Pulsa «Buscar anomalías» para hacerlo ahora."],
        closed: ["inbox", "Aún no hay expedientes cerrados", "Aquí quedará el historial, que la memoria usa como antecedentes."],
      }[view];
      container.innerHTML = window.emptyState(...empty);
      return;
    }
    container.innerHTML = items.map((item) => {
      const [big, small] = daysText(item.days_left);
      const progress = item.documents_total
        ? `<span>${window.icon("doc")} ${item.documents_ready}/${item.documents_total} documentos</span>`
        : "";
      return `
        <button type="button" class="case-card lvl-${esc(item.level)}" data-case="${item.id}">
          <span class="case-main">
            <span class="case-top">
              <span class="mono">${esc(item.code || "")}</span>
              <span>${esc(item.kind === "ANOMALY" ? item.procedure_label : item.organism_label || "")}</span>
              ${item.reference ? `<span>ref. ${esc(item.reference)}</span>` : ""}
            </span>
            <strong class="case-headline">${esc(item.title || item.headline)}</strong>
            ${subjectLine(item) ? `<span class="case-title">${esc(subjectLine(item))}</span>` : ""}
            ${item.summary ? `<span class="case-summary">${esc(item.summary)}</span>` : ""}
            <span class="case-meta">
              ${progress}
              ${item.requests_pending ? `<span>${window.icon("send")} ${item.requests_pending} pedido(s)</span>` : ""}
              ${item.has_draft ? `<span>${window.icon("edit")} Borrador listo</span>` : ""}
              ${item.amount ? `<span>${window.icon("coins")} ${money(item.amount)}</span>` : ""}
            </span>
          </span>
          <span class="case-side">
            <span class="status-pill ${STATUS_CLASS[item.status] || "status-neutral"}">${esc(item.status_label)}</span>
            ${item.kind === "NOTIFICATION" ? `<span class="case-deadline ${item.days_left !== null && item.days_left <= 3 ? "is-urgent" : ""}"><strong>${esc(big)}</strong><small>${esc(small)}</small></span>` : ""}
          </span>
        </button>
      `;
    }).join("");
  }

  /* ------------------------------------------------------------
  DETALLE
  ------------------------------------------------------------ */
  async function openCase(id) {
    current = await window.apiRequest(`/cases/${id}`);
    const dialog = document.getElementById("caseDialog");
    render();
    if (!dialog.open) dialog.showModal();
  }

  function render() {
    renderBanner();
    renderSummary();
    renderDocs();
    renderDraft();
    renderTrace();
    renderHistory();
    renderActions();
    showPane(pane);
  }

  function showPane(name) {
    pane = name;
    document.querySelectorAll("#caseTabs .segment").forEach((item) => item.classList.toggle("active", item.dataset.pane === name));
    document.querySelectorAll("#caseDialog .case-pane").forEach((item) => item.classList.toggle("hidden", item.dataset.pane !== name));
    const docsTab = document.querySelector('#caseTabs [data-pane="documentacion"]');
    const draftTab = document.querySelector('#caseTabs [data-pane="respuesta"]');
    docsTab.classList.toggle("hidden", current?.kind !== "NOTIFICATION" && !(current?.documents || []).length);
    draftTab.classList.toggle("hidden", !current?.has_draft && current?.kind === "ANOMALY");
  }

  function renderBanner() {
    const item = current;
    const [big, small] = daysText(item.days_left);
    document.getElementById("caseBanner").className = `case-banner lvl-${item.level}`;
    document.getElementById("caseBanner").innerHTML = `
      <div class="case-banner-main">
        <span class="case-banner-eyebrow"><span class="level-dot"></span>${esc(LEVEL_LABELS[item.level])} · <span class="mono">${esc(item.code)}</span>${item.reference ? ` · ref. ${esc(item.reference)}` : ""}</span>
        <h2>${esc(item.title || item.headline)}</h2>
        ${subjectLine(item, true) ? `<p>${esc(subjectLine(item, true))}</p>` : ""}
      </div>
      <div class="case-banner-side">
        ${["NOTIFICATION", "DEADLINE"].includes(item.kind) ? `<div class="case-countdown"><strong>${esc(big)}</strong><small>${esc(small)}</small></div>` : ""}
        <span class="status-pill ${STATUS_CLASS[item.status] || "status-neutral"}">${esc(item.status_label)}</span>
        <button type="button" class="icon-button" data-close-dialog aria-label="Cerrar">${window.icon("close")}</button>
      </div>
    `;
  }

  function factRow(label, value) {
    return value ? `<div class="fact"><span>${esc(label)}</span><strong>${value}</strong></div>` : "";
  }

  function renderSummary() {
    const item = current;
    const facts = item.facts || {};
    const insights = [...(facts.intake_warnings || []), ...(facts.insights || [])];
    const subjectTypes = { company: "Tu empresa", employee: "Persona de la plantilla", customer: "Cliente", supplier: "Proveedor", unknown: "Tercero" };

    const references = (facts.tax_references || []).map((ref) => `
      <div class="impact-row">
        <strong>Modelo ${esc(ref.model)}${ref.quarter ? ` · ${ref.quarter}T` : ""}${ref.year ? ` ${ref.year}` : ""}</strong>
        <span>${ref.draft_result !== undefined && ref.draft_result !== null ? `Tus datos: ${money(ref.draft_result)}` : "Sin borrador"}</span>
        <span>${ref.filed ? `Presentado: ${money(ref.filed.amount)}` : "No consta presentado"}</span>
        ${ref.invoices_pending ? `<span class="value-negative">${ref.invoices_pending} factura(s) sin revisar</span>` : ""}
      </div>
    `).join("");

    const embargo = (facts.embargo_pending || []).length ? `
      <div class="summary-block">
        <h3>Pagos que debes retener</h3>
        ${facts.embargo_pending.map((row) => `<div class="impact-row"><strong>${esc(row.number || "s/n")}</strong><span>${esc(row.date)}</span><span>${money(row.total)}</span></div>`).join("")}
      </div>` : "";

    const antecedents = (item.antecedents || []).length ? `
      <div class="summary-block">
        <h3>Antecedentes que ha encontrado la Memoria</h3>
        ${item.antecedents.map((ref) => `
          <button type="button" class="antecedent" ${ref.case_id ? `data-open-case="${ref.case_id}"` : ref.kind === "case" ? `data-open-case="${ref.ref_id}"` : ""}>
            <span class="antecedent-kind">${esc(KIND_LABELS[ref.kind] || ref.kind)}</span>
            <span><strong>${esc(ref.title)}</strong><small>${esc(ref.why || "")}${ref.outcome ? ` · ${esc(ref.outcome)}` : ""}${ref.date ? ` · ${esc(day(ref.date))}` : ""}</small></span>
          </button>
        `).join("")}
      </div>` : "";

    const findings = (item.findings || []).length ? `
      <div class="summary-block">
        <h3>Hallazgos con evidencia</h3>
        ${item.findings.map((finding) => `
          <div class="finding risk-${esc(finding.riesgo)}">
            <div class="finding-top">
              <span class="status-pill mini ${RISK_CLASS[finding.riesgo] || "status-neutral"}">${esc(RISK_LABELS[finding.riesgo] || finding.riesgo)}</span>
              <strong>${esc(finding.resultado)}</strong>
            </div>
            <p>${esc(finding.por_que)}</p>
            ${finding.evidencia?.length ? `<div class="trace-evidence">${finding.evidencia.slice(0, 5).map((ev) => `<button type="button" class="chip" ${ev.document_id ? `data-open-document="${ev.document_id}"` : ""}>${esc(ev.label)}</button>`).join("")}</div>` : ""}
            <small class="muted">Detectado por: ${esc(AGENT_NAMES[finding.agente] || finding.agente)} · confianza ${Math.round((finding.confianza || 0) * 100)} %${finding.fecha ? ` · ${esc(day(finding.fecha))}` : ""}${finding.siguiente ? ` · Siguiente: ${esc(finding.siguiente)}` : ""}</small>
          </div>`).join("")}
      </div>` : "";

    const history = facts.supplier_history;
    const supplier = history ? `
      <div class="summary-block">
        <h3>Historial del proveedor</h3>
        ${factRow("Facturas anteriores", history.invoices ? `${history.invoices}<small>desde ${esc(day(history.first_date))}</small>` : "Ninguna: es la primera")}
        ${factRow("Importe habitual", history.median_total !== null && history.median_total !== undefined ? `${money(history.median_total)}<small>mediana · máx. ${money(history.max_total)}</small>` : "")}
        ${factRow("Últimos 12 meses", history.invoices ? money(history.total_12m) : "")}
        ${factRow("Sin pagar", history.invoices ? String(history.unpaid) : "")}
      </div>` : "";

    const anomalyEvidence = item.kind === "ANOMALY" && !(item.findings || []).length && facts.evidence?.length ? `
      <div class="summary-block">
        <h3>Evidencia</h3>
        ${facts.evidence.map((ev) => `<button type="button" class="antecedent" ${ev.document_id ? `data-open-document="${ev.document_id}"` : ""}><span class="antecedent-kind">Factura</span><span><strong>${esc(ev.label)}</strong><small>${ev.document_id ? "Abrir el documento" : ""}</small></span></button>`).join("")}
      </div>` : "";

    const processing = facts.processing ? `
      <div class="processing-alert">
        ${window.icon("alert")}
        <span><strong>El agente ${esc(facts.processing.agent_name || facts.processing.failed_agent)} no pudo terminar.</strong> ${esc(facts.processing.error || "")} El resto del expediente sí está trabajado.</span>
        <button type="button" class="act-btn" data-retry-event="${facts.processing.event_id}">${window.icon("repeat")} Reanudar</button>
      </div>` : "";

    // Lo que ya dice el resumen no se repite como hallazgo.
    const lead = (item.summary || "").toLowerCase();
    const found = insights.filter((text) => !lead.includes(String(text).toLowerCase().replace(/\.$/, "")));

    const steps = item.run?.steps || [];
    const checked = steps.length ? `
      <div class="summary-block">
        <h3>Qué ha comprobado</h3>
        <ol class="brief-list">
          ${steps.map((step) => `
            <li class="${step.status !== "OK" ? "is-failed" : ""}">
              <span class="brief-agent">${esc(step.agent_name)}</span>
              <span>${esc(step.summary)}${step.engine && step.engine !== "reglas" ? ` <span class="status-pill mini status-info">${esc(step.engine)}</span>` : ""}${step.status !== "OK" ? ` <span class="status-pill mini status-danger">${esc(step.status)}</span>` : ""}</span>
            </li>`).join("")}
        </ol>
        <button type="button" class="link-button brief-more" data-show-pane="traza">Ver la traza con evidencias</button>
      </div>` : "";

    const docs = item.documents || [];
    const missingDocs = docs.filter((doc) => ["missing", "partial", "requested"].includes(doc.status));
    const docsBlock = docs.length ? `
      <div class="summary-block">
        <h3>Qué documentos necesita</h3>
        <p class="brief-line"><strong>${item.documents_ready ?? docs.filter((doc) => ["ready", "received", "provided"].includes(doc.status)).length} de ${item.documents_total ?? docs.length}</strong> preparados${missingDocs.length ? `; faltan ${missingDocs.length}:` : "."}</p>
        ${missingDocs.length ? `<ul class="brief-list plain">${missingDocs.map((doc) => `<li><span class="status-pill mini ${DOC_CLASS[doc.status] || "status-neutral"}">${esc(doc.status_label)}</span><span>${esc(doc.label)}</span></li>`).join("")}</ul>` : ""}
        <button type="button" class="link-button brief-more" data-show-pane="documentacion">Ver la documentación</button>
      </div>` : "";

    const placeholders = (item.draft_response || "").match(/\[[^\]]+\]/g) || [];
    const open = ["WAITING_HUMAN", "WAITING_DOCS", "OPEN"].includes(item.status);
    const auto = [];
    const yours = [];
    const readyDocs = docs.filter((doc) => doc.status === "ready").length;
    if (readyDocs) auto.push(`${readyDocs} documento(s) localizados y preparados para el paquete`);
    if (item.has_draft) auto.push(item.draft_edited ? "Borrador de respuesta (editado por ti)" : "Borrador de respuesta redactado");
    if (item.requests_pending) auto.push(`Ha pedido ${item.requests_pending} documento(s) y enviará recordatorios`);
    if (item.internal_deadline && open) auto.push(`Vigila el plazo y te avisa antes del ${day(item.internal_deadline)}`);
    if (open) {
      if (placeholders.length) yours.push(`Completar ${placeholders.length} hueco(s) del borrador`);
      const toProvide = missingDocs.filter((doc) => doc.status !== "requested");
      if (toProvide.length) yours.push(`Aportar o pedir ${toProvide.length} documento(s)`);
      yours.push(item.kind === "ANOMALY" ? "Aprobar la recomendación o descartarla" : item.kind === "DEADLINE" ? "Confirmar que está listo para presentar" : item.has_draft ? "Aprobar la respuesta y presentarla en la sede" : "Confirmar que está resuelto");
    } else if (item.status === "READY_TO_FILE") {
      yours.push("Presentarlo en la sede y anotar el justificante");
    }
    const split = auto.length || yours.length ? `
      <div class="summary-block brief-split">
        <div>
          <h3>Lo hace CapaFiscal</h3>
          ${auto.length ? `<ul class="brief-list plain">${auto.map((text) => `<li>${window.icon("check")}<span>${esc(text)}</span></li>`).join("")}</ul>` : `<p class="board-empty">Nada automático en este trámite.</p>`}
        </div>
        <div>
          <h3>Lo apruebas tú</h3>
          ${yours.length ? `<ul class="brief-list plain">${yours.map((text) => `<li>${window.icon("users")}<span>${esc(text)}</span></li>`).join("")}</ul>` : `<p class="board-empty">Nada pendiente.</p>`}
        </div>
      </div>` : "";

    document.getElementById("casePaneSummary").innerHTML = `
      ${processing}
      <p class="case-lead">${esc(item.summary || "")}</p>
      ${split}
      <div class="case-summary-grid">
        <div>
          ${found.length || findings ? `
            <div class="summary-block">
              <h3>Qué ha encontrado</h3>
              ${found.length ? `<ul class="brief-list found">${found.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>` : ""}
            </div>` : ""}
          ${findings}
          <div class="summary-block">
            <h3>Qué recomienda</h3>
            ${facts.recommendation ? `<p class="recommendation">${window.icon("sparkles")}<span>${esc(facts.recommendation)}</span></p>` : ""}
            <ul class="check-list">
              ${(item.actions || []).map((action, index) => `
                <li class="check-item">
                  <input type="checkbox" class="check-toggle" data-action-index="${index}" ${action.done ? "checked" : ""} aria-label="${esc(action.label)}">
                  <span class="check-text"><strong class="${action.done ? "is-done" : ""}">${esc(action.label)}</strong>${action.by === "human" ? `<small class="muted">añadida por ti</small>` : ""}</span>
                </li>`).join("")}
            </ul>
            <form class="add-action" id="caseAddAction">
              <input type="text" maxlength="200" placeholder="Añadir o cambiar lo que hay que hacer…" aria-label="Nueva acción">
              <button type="submit" class="btn-ghost">Añadir</button>
            </form>
          </div>
          ${docsBlock}
          ${checked}
          ${antecedents}
          ${anomalyEvidence}
        </div>
        <div>
          <div class="summary-block facts-block">
            <h3>Datos clave</h3>
            ${factRow("Organismo", esc(item.organism_label || ""))}
            ${factRow("Trámite", esc(item.procedure_label))}
            ${factRow("Afecta a", item.subject?.name ? `${esc(item.subject.name)} <small>${esc(subjectTypes[item.subject.type] || "")}${item.subject.tax_id ? ` · ${esc(item.subject.tax_id)}` : ""}</small>` : "")}
            ${facts.affected ? factRow("Embargado", `${esc(facts.affected.name || facts.affected.tax_id)} <small>${esc(subjectTypes[facts.affected.type] || "")}</small>`) : ""}
            ${factRow("Importe", item.amount ? money(item.amount) : "")}
            ${factRow("Plazo", item.deadline ? `${esc(day(item.deadline))}${facts.deadline_rule ? `<small>${esc(facts.deadline_rule)}</small>` : ""}` : "Sin plazo conocido")}
            ${factRow("Objetivo interno", item.internal_deadline ? `${esc(day(item.internal_deadline))} <small>margen de seguridad de 2 días hábiles</small>` : "")}
            ${factRow("Origen", esc(facts.document_name || facts.source || ""))}
            ${item.route ? factRow("Ruta", `${esc(item.route.label)}<small>${esc(item.route.reason || "")}</small>`) : ""}
            ${item.document_id ? `<button type="button" class="btn-ghost" data-open-document="${item.document_id}">${window.icon("doc")} Ver el documento original</button>` : ""}
          </div>
          ${references ? `<div class="summary-block"><h3>Impacto fiscal</h3>${references}</div>` : ""}
          ${supplier}
          ${embargo}
        </div>
      </div>
    `;
  }

  function renderDocs() {
    const item = current;
    const docs = item.documents || [];
    const needsRequest = docs.some((doc) => ["missing", "partial"].includes(doc.status) && doc.source !== "system");
    document.getElementById("casePaneDocs").innerHTML = `
      ${docs.length ? `
        <div class="doc-checklist">
          ${docs.map((doc, index) => `
            <div class="doc-row">
              <span class="status-pill ${DOC_CLASS[doc.status] || "status-neutral"}">${esc(doc.status_label)}</span>
              <div class="doc-body">
                <strong>${esc(doc.label)}${doc.period_label ? ` <small>${esc(doc.period_label)}</small>` : ""}</strong>
                <span>${esc(doc.source_label)}${doc.note ? ` · ${esc(doc.note)}` : ""}</span>
                ${doc.detail && doc.code !== "OTRO" ? `<small class="muted">Lo que pide el texto: «${esc(doc.detail)}»</small>` : ""}
                ${doc.verification ? `<small class="${doc.verification === "ok" ? "value-positive" : "value-negative"}">${doc.verification === "ok" ? "Verificado automáticamente" : doc.verification === "doubtful" ? "Revisa el contenido: no coincide del todo con lo pedido" : "No se pudo leer: revísalo tú"}</small>` : ""}
                ${doc.request?.status === "PENDING" ? `<small class="muted">Pedido${doc.request.reminders ? ` · ${doc.request.reminders} recordatorio(s)` : ""} · <button type="button" class="link-button" data-copy="${esc(doc.request.url)}">copiar enlace de subida</button></small>` : ""}
              </div>
              <div class="doc-actions">
                ${doc.status === "ready" ? `<span class="muted doc-auto">${window.icon("sparkles")} Va en el paquete</span>` : ""}
                ${["missing", "partial", "requested"].includes(doc.status) ? `<button type="button" class="btn-ghost" data-attach="${index}">${window.icon("upload")} Adjuntar</button>` : ""}
                ${doc.status !== "not_applicable" && doc.status !== "ready" ? `<button type="button" class="btn-ghost" data-doc-status="${index}" data-value="not_applicable">No aplica</button>` : ""}
                ${doc.status === "not_applicable" ? `<button type="button" class="btn-ghost" data-doc-status="${index}" data-value="missing">Sí aplica</button>` : ""}
              </div>
            </div>
          `).join("")}
        </div>` : `<p class="empty-inline">Este trámite no pide documentación.</p>`}
      <div class="doc-footer">
        ${needsRequest ? `<button type="button" class="act-btn" data-case-action="request">${window.icon("send")} Pedir lo que falta</button>` : ""}
        <button type="button" class="btn-ghost" data-attach="-1">${window.icon("upload")} Adjuntar otro documento</button>
        <a class="btn-ghost" href="/api/cases/${item.id}/package.zip">${window.icon("archive")} Descargar paquete</a>
      </div>
      ${item.attachments?.length ? `
        <div class="summary-block">
          <h3>Documentos aportados</h3>
          ${item.attachments.map((att) => `
            <a class="attachment-chip" href="/api/case-attachments/${att.id}" target="_blank" rel="noopener noreferrer">
              ${window.icon("doc")}<span>${esc(att.filename)}</span>
              <small>${att.source === "client" ? "subido por enlace" : "aportado por ti"}${att.verification?.status === "ok" ? " · verificado" : att.verification?.status === "doubtful" ? " · revisar" : ""}</small>
            </a>`).join("")}
        </div>` : ""}
    `;
  }

  function renderDraft() {
    const item = current;
    const placeholders = (item.draft_response || "").match(/\[[^\]]+\]/g) || [];
    document.getElementById("casePaneDraft").innerHTML = item.draft_response ? `
      <div class="draft-head">
        <p class="detail-hint">${item.draft_edited ? "Borrador editado por ti." : "Borrador preparado por el Gestor de incidencias."} ${placeholders.length ? `<strong class="value-negative">Quedan ${placeholders.length} hueco(s) entre [corchetes] por completar.</strong>` : "Sin huecos pendientes."}</p>
        <div class="card-actions">
          <a class="btn-ghost" href="/api/cases/${item.id}/letter.pdf" target="_blank" rel="noopener noreferrer">${window.icon("doc")} Ver en PDF</a>
          <button type="button" class="act-btn" data-case-action="save-draft">Guardar cambios</button>
        </div>
      </div>
      <textarea class="draft-editor" id="caseDraft" spellcheck="true">${esc(item.draft_response)}</textarea>
    ` : `<p class="empty-inline">Este trámite no necesita un escrito de respuesta.</p>`;
  }

  function renderTrace() {
    const run = current.run;
    if (!run) {
      document.getElementById("casePaneTrace").innerHTML = `<p class="empty-inline">Sin recorridos registrados.</p>`;
      return;
    }
    const total = run.steps.reduce((sum, step) => sum + step.duration_ms, 0);
    const engines = [...new Set(run.steps.map((step) => step.engine))];
    let elapsed = 0;
    document.getElementById("casePaneTrace").innerHTML = `
      ${current.route ? `
        <div class="route-banner">
          <strong>Ruta: ${esc(current.route.label)}</strong><span>${esc(current.route.reason || "")}</span>
          ${current.route.escalated ? `<span class="value-negative">Empezó como «documento normal»; el Detector la escaló a «${esc(current.route.label)}».</span>` : ""}
          ${(current.route.chained || []).map((link) => `<span>+ ${link.added.map((code) => esc(AGENT_NAMES[code] || code)).join(", ")} tras ${esc(AGENT_NAMES[link.after] || link.after)}: ${esc(link.reason)}</span>`).join("")}
        </div>` : ""}
      <p class="detail-hint">Recorrido del ${esc(window.formatDate(run.started_at))} · ${run.steps.length} agentes · ${total} ms · motor: ${esc(engines.join(", "))} · disparado por ${esc({ upload: "la subida del documento", manual: "ti", schedule: "el planificador", system: "el sistema" }[run.trigger] || run.trigger)}</p>
      <ol class="trace">
        ${run.steps.map((step) => {
          elapsed += step.duration_ms;
          return `
            <li class="trace-step status-${esc(step.status.toLowerCase())}">
              <span class="trace-icon">${window.icon(step.icon || "sparkles")}</span>
              <div class="trace-body">
                <div class="trace-top">
                  <strong>${esc(step.agent_name)}</strong>
                  <span class="trace-time">+${elapsed} ms</span>
                  <span class="status-pill mini ${step.engine === "reglas" ? "status-neutral" : "status-info"}">${esc(step.engine)}</span>
                  ${step.status !== "OK" ? `<span class="status-pill mini status-danger">${esc(step.status)}</span>` : ""}
                </div>
                <p>${esc(step.summary)}</p>
                ${step.evidence?.length ? `<div class="trace-evidence">${step.evidence.slice(0, 6).map((ev) => `<span class="chip">${esc(ev.label)}</span>`).join("")}</div>` : ""}
              </div>
            </li>
          `;
        }).join("")}
      </ol>
    `;
  }

  function renderHistory() {
    document.getElementById("casePaneHistory").innerHTML = `
      <ol class="history">
        ${[...(current.events || [])].reverse().map((ev) => `
          <li class="history-item kind-${esc(ev.kind)}">
            <span class="history-dot">${window.icon(ev.icon || (ev.kind === "human" ? "users" : ev.kind === "system" ? "zap" : "sparkles"))}</span>
            <div><strong>${esc(ev.title)}</strong><small>${esc(ev.kind === "human" ? `Por ${ev.actor}` : ev.actor_name)} · ${esc(window.formatDate(ev.created_at))}</small>${ev.detail ? `<p>${esc(ev.detail)}</p>` : ""}</div>
          </li>`).join("")}
      </ol>
    `;
  }

  function renderActions() {
    const item = current;
    const actions = [];
    const open = ["WAITING_HUMAN", "WAITING_DOCS", "OPEN"].includes(item.status);
    if (item.kind === "ANOMALY" && open) {
      if (item.route) actions.push(`<button type="button" class="btn-ghost" data-case-action="rerun">${window.icon("repeat")} Volver a pasar los agentes</button>`);
      actions.push(`<button type="button" class="btn-ghost" data-case-action="dismiss">Es correcto, descartar</button>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<button type="button" class="btn-ghost" data-case-action="resolve">Resuelto de otra forma</button>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-case-action="approve">${window.icon("check")} Aprobar recomendación</button>`);
    } else if (item.kind === "DEADLINE" && open) {
      actions.push(`<button type="button" class="btn-ghost" data-case-action="rerun">${window.icon("repeat")} Recalcular</button>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-case-action="approve">${window.icon("check")} Listo para presentar</button>`);
    } else if (open) {
      if (item.notification_id) actions.push(`<button type="button" class="btn-ghost" data-case-action="rerun">${window.icon("repeat")} Volver a pasar los agentes</button>`);
      actions.push(`<button type="button" class="btn-ghost" data-case-action="dismiss">Descartar</button>`);
      actions.push(`<span class="spacer"></span>`);
      if (item.has_draft) actions.push(`<button type="button" class="act-btn act-primary" data-case-action="approve">${window.icon("check")} Aprobar respuesta</button>`);
      else actions.push(`<button type="button" class="act-btn act-primary" data-case-action="resolve">${window.icon("check")} Marcar como resuelto</button>`);
    } else if (item.status === "READY_TO_FILE") {
      actions.push(`<button type="button" class="btn-ghost" data-case-action="reopen">Volver a revisión</button>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<a class="btn-ghost" href="/api/cases/${item.id}/package.zip">${window.icon("archive")} Paquete para presentar</a>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-case-action="file">${window.icon("check")} Ya lo he presentado</button>`);
    } else if (item.status === "FILED") {
      actions.push(`<span class="muted">Presentado el ${esc(day(item.filed_at))}${item.filing_reference ? ` · registro ${esc(item.filing_reference)}` : ""}</span>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<button type="button" class="act-btn act-primary" data-case-action="resolve">Cerrar expediente</button>`);
    } else {
      actions.push(`<span class="muted">${esc(item.resolution || "")}</span>`);
      actions.push(`<span class="spacer"></span>`);
      actions.push(`<button type="button" class="btn-ghost" data-case-action="reopen">Reabrir</button>`);
    }
    document.getElementById("caseActions").innerHTML = actions.join("");
  }

  async function patch(body) {
    current = await window.jsonRequest(`/cases/${current.id}`, "PATCH", body);
    render();
  }

  async function caseAction(action) {
    const id = current.id;
    try {
      if (action === "approve") {
        const missing = current.documents.filter((doc) => ["missing", "requested", "partial"].includes(doc.status)).length;
        const draft = document.getElementById("caseDraft");
        if (draft && draft.value !== current.draft_response) await patch({ draft_response: draft.value });
        if (missing && !window.confirm(`Faltan ${missing} documento(s). ¿Aprobar la respuesta igualmente?`)) return;
        const kind = current.kind;
        current = await window.jsonRequest(`/cases/${id}/approve`, "POST", {});
        window.showMessage(
          kind === "ANOMALY" ? "Recomendación aprobada y anotada en la memoria." : kind === "DEADLINE" ? "Listo para presentar." : "Respuesta aprobada: descarga el paquete y preséntalo en la sede electrónica.",
          "success",
        );
      } else if (action === "file") {
        const reference = window.prompt("Número de registro de entrada del justificante (opcional):", "");
        if (reference === null) return;
        current = await window.jsonRequest(`/cases/${id}/file`, "POST", { reference });
        window.showMessage("Presentación registrada en el expediente.", "success");
      } else if (action === "resolve" || action === "dismiss") {
        const text = window.prompt(action === "dismiss" ? "¿Por qué se descarta? (queda en la memoria)" : "¿Cómo se ha resuelto? (queda en la memoria para la próxima vez)", "");
        if (text === null) return;
        current = await window.jsonRequest(`/cases/${id}/resolve`, "POST", { resolution: text, dismiss: action === "dismiss" });
        window.showMessage(action === "dismiss" ? "Expediente descartado." : "Expediente resuelto.", "success");
      } else if (action === "reopen") {
        current = await window.jsonRequest(`/cases/${id}/reopen`, "POST", {});
      } else if (action === "rerun") {
        current = await window.jsonRequest(`/cases/${id}/rerun`, "POST", {});
        window.showMessage("Los agentes han vuelto a trabajar el expediente.", "success");
        pane = "traza";
      } else if (action === "request") {
        const result = await window.jsonRequest(`/cases/${id}/request-documents`, "POST", {});
        window.showMessage(result.message ? "Petición preparada en la bandeja de salida." : "No había nada nuevo que pedir.", "success");
        current = await window.apiRequest(`/cases/${id}`);
        if (result.message) {
          document.getElementById("caseDialog").close();
          window.openOutboxMessage?.(result.message.id);
          return;
        }
      } else if (action === "save-draft") {
        await patch({ draft_response: document.getElementById("caseDraft").value });
        window.showMessage("Borrador guardado.", "success");
        return refreshBackground();
      }
      render();
      refreshBackground();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function uploadAttachment(file) {
    const form = new FormData();
    form.append("uploaded_file", file);
    if (pendingItemCode) form.append("item_code", pendingItemCode);
    try {
      current = await window.apiRequest(`/cases/${current.id}/attachments`, { method: "POST", body: form });
      render();
      window.showMessage("Documento incorporado al expediente.", "success");
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  AGENTES Y MEMORIA
  ------------------------------------------------------------ */
  async function loadIntake() {
    const data = await window.apiRequest("/events?limit=15");
    const counts = data.counts || {};
    document.getElementById("intakeCounts").textContent = [
      `${counts.COMPLETED || 0} completadas`,
      counts.NEEDS_HUMAN ? `${counts.NEEDS_HUMAN} necesitan a una persona` : null,
      counts.FAILED ? `${counts.FAILED} fallidas` : null,
    ].filter(Boolean).join(" · ");
    document.getElementById("intakeList").innerHTML = data.events.length ? data.events.map((event) => `
      <div class="intake-row">
        <span class="status-pill mini ${INTAKE_CLASS[event.status] || "status-neutral"}">${esc(event.status_label)}</span>
        <span class="intake-main">
          <strong>${esc(SOURCE_LABELS[event.source] || event.source)} · ${esc(KIND_EVENT_LABELS[event.kind] || event.kind)}</strong>
          <small class="mono">${esc(event.external_id.length > 48 ? `${event.external_id.slice(0, 45)}…` : event.external_id)}</small>
          ${event.error ? `<small class="value-negative">${event.failed_agent_name ? `${esc(event.failed_agent_name)}: ` : ""}${esc(event.error.slice(0, 160))}</small>` : ""}
        </span>
        <span class="intake-meta">
          ${event.duplicates ? `<small class="muted">${event.duplicates} repetido(s) ignorado(s)</small>` : ""}
          ${event.attempts > 1 ? `<small class="muted">${event.attempts} intentos</small>` : ""}
          <small class="muted">${esc(window.formatDate(event.created_at))}</small>
        </span>
        <span class="intake-actions">
          ${event.case_id ? `<button type="button" class="btn-ghost" data-open-case="${event.case_id}">Ver expediente</button>` : ""}
          ${["FAILED", "NEEDS_HUMAN"].includes(event.status) ? `<button type="button" class="act-btn" data-retry-event="${event.id}">${window.icon("repeat")} Reanudar</button>` : ""}
        </span>
      </div>
    `).join("") : `<p class="empty-inline">Aún no ha entrado nada.</p>`;
  }

  async function retryEvent(id) {
    try {
      const result = await window.jsonRequest(`/events/${id}/retry`, "POST", {});
      window.showMessage(result.event.status === "COMPLETED" ? "Reanudado: todos los agentes terminaron bien." : `Sigue sin poder terminar: ${result.event.error || result.event.status_label}`, result.event.status === "COMPLETED" ? "success" : "error");
      if (current && result.case && result.case.id === current.id) {
        current = result.case;
        render();
      }
      refreshBackground();
      if (view === "agents") loadIntake();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function importEml(file) {
    const form = new FormData();
    form.append("uploaded_file", file);
    try {
      const result = await window.apiRequest("/connectors/email/import", { method: "POST", body: form });
      const cases = result.attachments.filter((item) => item.case_code).map((item) => item.case_code);
      window.showMessage(
        `Correo «${result.subject || "sin asunto"}»: ${result.attachments.length} documento(s)` + (cases.length ? `, expediente ${cases.join(", ")}` : ", sin nada que revisar") + (result.skipped.length ? ` · ignorados: ${result.skipped.join(", ")}` : "") + ".",
        "success",
      );
      refreshBackground();
      loadIntake();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  const RULE_STATUS = { PROPUESTA: ["Propuesta", "status-warning"], APROBADA: ["En vigor", "status-success"], RECHAZADA: ["Rechazada", "status-neutral"], RETIRADA: ["Retirada", "status-neutral"] };

  async function loadLearningRules() {
    const data = await window.apiRequest("/learning/rules");
    document.getElementById("learningSub").textContent = `Versión ${data.version} · ${data.policy}`;
    const container = document.getElementById("learningRules");
    if (!data.rules.length) {
      container.innerHTML = `<p class="empty-inline">Sin patrones todavía. Cuando corrijas varias veces el mismo campo de un proveedor, aquí aparecerá una propuesta para que decidas.</p>`;
      return;
    }
    container.innerHTML = data.rules.map((rule) => {
      const [label, className] = RULE_STATUS[rule.status] || [rule.status, "status-neutral"];
      return `
        <div class="rule-row">
          <div class="rule-main">
            <div class="rule-title"><span class="status-pill mini ${className}">${esc(label)}${rule.version ? ` · v${rule.version}` : ""}</span><strong>${esc(rule.subject_name || rule.subject_key)} · «${esc(rule.field)}»</strong></div>
            <p>${esc(rule.effect)}</p>
            <small class="muted">Evidencia: corregido en ${rule.evidence.corrections} factura(s)${(rule.evidence.examples || []).slice(-2).map((ex) => ` · «${esc(ex.predicted ?? "vacío")}» → «${esc(ex.human ?? "vacío")}»`).join("")}</small>
            <small class="muted block">Simulación: ${esc(rule.simulation.summary || "")}</small>
            ${rule.decided_by ? `<small class="muted block">${esc(label)} por ${esc(rule.decided_by)} el ${esc(window.formatDate(rule.decided_at))}${rule.note ? ` · ${esc(rule.note)}` : ""}</small>` : ""}
          </div>
          <div class="rule-actions">
            ${rule.status === "PROPUESTA" ? `<button type="button" class="btn-ghost" data-rule="${rule.id}" data-decision="rechazar">Rechazar</button><button type="button" class="act-btn act-primary" data-rule="${rule.id}" data-decision="aprobar">Aprobar</button>` : ""}
            ${rule.status === "APROBADA" ? `<button type="button" class="btn-ghost" data-rule="${rule.id}" data-decision="retirar">Retirar</button>` : ""}
          </div>
        </div>`;
    }).join("");
  }

  async function loadAgents() {
    loadIntake().catch((error) => window.showMessage(error.message, "error"));
    loadLearningRules().catch((error) => { document.getElementById("learningRules").innerHTML = `<p class="empty-inline">${esc(error.message)}</p>`; });
    const [data, runs] = await Promise.all([window.apiRequest("/agents"), window.apiRequest("/agents/runs?limit=12")]);
    document.getElementById("agentsIntro").innerHTML = `
      <div class="agents-intro-body">
        <div>
          <h2>Tu equipo de ${data.agents.length} agentes</h2>
          <p>Cada cosa que ocurre (una notificación, una factura, un plazo) abre un expediente y el orquestador decide qué agentes hacen falta. Si un agente encuentra algo, se encadenan los siguientes. Todo acaba en una recomendación con evidencia que tú apruebas, cambias o rechazas.</p>
        </div>
        <div class="agents-engine">
          <span class="status-pill ${data.ai_enabled ? "status-info" : "status-neutral"}">${data.ai_enabled ? `IA: ${esc(data.engine)}` : "Reglas y plantillas"}</span>
          <small>${data.ai_enabled ? "Claude lee las notificaciones y redacta las respuestas." : "Añade ANTHROPIC_API_KEY en .env para que Claude lea y redacte."}</small>
          <small>${data.runs_this_month} recorrido(s) este mes</small>
        </div>
      </div>
    `;
    document.getElementById("agentsGrid").innerHTML = data.agents.map((agent) => `
      <article class="agent-card">
        <header>
          <span class="agent-avatar">${window.icon(agent.icon)}</span>
          <div><h3>${esc(agent.name)}</h3><small>«${esc(agent.need)}»</small></div>
        </header>
        <p>${esc(agent.role)}</p>
        <div class="agent-stats">
          <span><strong>${agent.steps}</strong> tareas este mes</span>
          ${agent.avg_ms ? `<span><strong>${agent.avg_ms}</strong> ms de media</span>` : ""}
        </div>
        ${agent.contract ? `
          <dl class="agent-contract">
            <dt>Entrada</dt><dd>${esc(agent.contract.input.join(" · "))}</dd>
            <dt>Salida</dt><dd>${esc(agent.contract.output.join(" · "))}</dd>
          </dl>` : ""}
        ${agent.last_summary ? `<div class="agent-last">${window.icon("clock")}<span>${esc(agent.last_summary)}</span></div>` : ""}
      </article>
    `).join("");
    document.getElementById("agentRoutes").innerHTML = (data.routes || []).map((route) => `
      <div class="route-row">
        <span class="route-name"><strong>${esc(route.label)}</strong><small>${esc(PIPELINE_LABELS[route.event] || route.event)}</small></span>
        <span class="route-chain">${route.steps.map((code) => `<span class="route-node ${route.only_if_anomaly.includes(code) ? "is-conditional" : ""}" title="${route.only_if_anomaly.includes(code) ? "Solo si el Detector encuentra algo" : ""}">${window.icon(AGENT_ICONS[code] || "sparkles")}${esc(AGENT_NAMES[code] || code)}</span>`).join(`<span class="route-arrow">→</span>`)}<span class="route-arrow">→</span><span class="route-node is-human">${window.icon("users")}Tú</span></span>
      </div>
    `).join("");
    document.getElementById("agentRuns").innerHTML = runs.length ? runs.map((run) => `
      <button type="button" class="run-item" ${run.case_id ? `data-open-case="${run.case_id}"` : ""}>
        <span class="run-chain">${run.steps.map((step) => `<span class="run-node status-${esc(step.status.toLowerCase())}" title="${esc(step.agent_name)}: ${esc(step.summary)}">${window.icon(step.icon)}</span>`).join("")}</span>
        <span class="run-text"><strong>${esc(run.summary || run.pipeline)}</strong><small>${esc(PIPELINE_LABELS[run.pipeline] || run.pipeline)} · ${esc(window.formatDate(run.started_at))}</small></span>
      </button>
    `).join("") : `<p class="empty-inline">Aún no hay recorridos.</p>`;
  }

  async function ask(question) {
    const container = document.getElementById("memoryAnswer");
    container.innerHTML = `<p class="muted">Buscando en la documentación…</p>`;
    try {
      const data = await window.jsonRequest("/memory/ask", "POST", { question });
      container.innerHTML = `
        <div class="memory-answer">
          <p class="memory-text">${esc(data.answer)}</p>
          <small class="muted">Motor: ${esc(data.engine)}</small>
        </div>
        ${data.sources.length ? `<div class="memory-sources">${data.sources.map((source, index) => `
          <button type="button" class="memory-source" data-source-kind="${esc(source.kind)}" data-source-id="${source.ref_id}">
            <span class="memory-index">${index + 1}</span>
            <span><strong>${esc(source.title)}</strong><small>${esc(KIND_LABELS[source.kind] || source.kind)}${source.date ? ` · ${esc(day(source.date))}` : ""}</small><em>${esc(source.snippet)}</em></span>
          </button>`).join("")}</div>` : ""}
      `;
    } catch (error) {
      container.innerHTML = `<p class="danger-text">${esc(error.message)}</p>`;
    }
  }

  /* ------------------------------------------------------------
  VISTAS
  ------------------------------------------------------------ */
  function showView(name) {
    view = name;
    document.querySelectorAll("#caseViews .segment").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    const panel = ["agents", "memory"].includes(name) ? name : "list";
    document.querySelectorAll(".case-view").forEach((item) => item.classList.toggle("hidden", item.dataset.caseView !== panel));
    if (name === "agents") loadAgents().catch((error) => window.showMessage(error.message, "error"));
    else if (panel === "list") loadList().catch((error) => window.showMessage(error.message, "error"));
  }

  async function refresh() {
    await loadBriefing();
    if (active && !["agents", "memory"].includes(view)) await loadList();
  }

  function refreshBackground() {
    refresh().catch(() => {});
  }

  function setup() {
    const section = document.getElementById("tab-expedientes");
    if (!section) return;

    document.getElementById("caseViews").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (button) showView(button.dataset.view);
    });

    document.addEventListener("click", (event) => {
      const brief = event.target.closest("[data-brief]");
      if (brief) return openBriefingItem(Number(brief.dataset.brief));
      if (event.target.closest("[data-go-cases]")) return window.activateTab("expedientes");
      const goTab = event.target.closest("#homeBriefing [data-go-tab]");
      if (goTab) {
        window.activateTab(goTab.dataset.goTab);
        const anchor = goTab.dataset.goAnchor;
        if (anchor) window.setTimeout(() => document.getElementById(anchor)?.scrollIntoView({ block: "start" }), 300);
        return;
      }
      const boardCase = event.target.closest("#homeBriefing [data-open-case]");
      if (boardCase) return openCase(Number(boardCase.dataset.openCase)).catch((error) => window.showMessage(error.message, "error"));
      const pulseButton = event.target.closest("[data-run-pulse]");
      if (pulseButton) {
        pulseButton.disabled = true;
        window.jsonRequest("/agents/pulse/run", "POST", {}).then((result) => {
          window.showMessage(`Ciclo completo: buzón, pendientes, plazos, seguimiento y detector (${result.items} elemento(s) trabajados).`, "success");
          refreshBackground();
        }).catch((error) => window.showMessage(error.message, "error")).finally(() => { pulseButton.disabled = false; });
        return;
      }
      if (event.target.closest("#homeBriefing [data-go-intake]")) {
        window.activateTab("expedientes");
        return window.setTimeout(() => showView("agents"), 60);
      }
    });

    section.addEventListener("click", (event) => {
      const ruleButton = event.target.closest("[data-rule]");
      if (ruleButton) {
        const decision = ruleButton.dataset.decision;
        const note = decision === "aprobar" ? "" : window.prompt(`Motivo para ${decision} (opcional):`, "");
        if (note === null) return;
        ruleButton.disabled = true;
        return window.jsonRequest(`/learning/rules/${ruleButton.dataset.rule}/${decision}`, "POST", { note: note || null })
          .then(() => { window.showMessage(decision === "aprobar" ? "Regla aprobada: entra en vigor con una versión nueva." : "Hecho.", "success"); return loadLearningRules(); })
          .catch((error) => { ruleButton.disabled = false; window.showMessage(error.message, "error"); });
      }
      const card = event.target.closest("[data-case]");
      if (card) return openCase(Number(card.dataset.case)).catch((error) => window.showMessage(error.message, "error"));
      const run = event.target.closest("[data-open-case]");
      if (run) return openCase(Number(run.dataset.openCase));
      const source = event.target.closest("[data-source-kind]");
      if (source) {
        const id = Number(source.dataset.sourceId);
        if (source.dataset.sourceKind === "case") return openCase(id);
        if (source.dataset.sourceKind === "document") return window.showDetail?.(id);
        return window.activateTab("notificaciones");
      }
      const chip = event.target.closest("[data-question]");
      if (chip) {
        document.querySelector("#memoryForm input").value = chip.dataset.question;
        return ask(chip.dataset.question);
      }
    });

    document.getElementById("memoryForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const question = event.currentTarget.elements.question.value.trim();
      if (question.length >= 3) ask(question);
    });

    document.getElementById("scanAnomalies").addEventListener("click", async () => {
      try {
        const result = await window.jsonRequest("/agents/anomalies/scan", "POST", {});
        window.showMessage(result.created ? `${result.created} anomalía(s) nuevas.` : result.found ? "Sin novedades: las anomalías ya estaban avisadas." : "Todo cuadra: sin anomalías.", "success");
        showView(result.created ? "anomalies" : view);
        refreshBackground();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
    document.getElementById("importEml").addEventListener("click", () => document.getElementById("emlInput").click());
    document.getElementById("emlInput").addEventListener("change", (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      if (file) importEml(file);
    });
    document.getElementById("pollInbox").addEventListener("click", async () => {
      try {
        const result = await window.jsonRequest("/connectors/email/poll", "POST", {});
        window.showMessage(result.messages ? `${result.messages} correo(s), ${result.documents} documento(s)` + (result.cases.length ? `; expedientes: ${result.cases.join(", ")}` : "") + "." : "Sin correos nuevos (configura IMAP en .env o deja .eml en data/buzon).", "success");
        refreshBackground();
        loadIntake();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
    document.getElementById("intakeList").addEventListener("click", (event) => {
      const paneLink = event.target.closest("[data-show-pane]");
      if (paneLink) return showPane(paneLink.dataset.showPane);

      const retryButton = event.target.closest("[data-retry-event]");
      if (retryButton) retryEvent(Number(retryButton.dataset.retryEvent));
    });
    document.getElementById("watchDeadlines").addEventListener("click", async () => {
      try {
        const result = await window.jsonRequest("/agents/deadlines/watch", "POST", {});
        window.showMessage(result.created ? `${result.created} plazo(s) con expediente: ${result.cases.map((item) => item.title).join(", ")}.` : "Ningún modelo vence en los próximos 15 días sin expediente.", "success");
        refreshBackground();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });
    document.getElementById("processPending").addEventListener("click", async () => {
      try {
        const result = await window.jsonRequest("/agents/process-pending", "POST", {});
        window.showMessage(result.processed ? `${result.processed} notificación(es) trabajadas por los agentes.` : "No había notificaciones pendientes de trabajar.", "success");
        refreshBackground();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    const dialog = document.getElementById("caseDialog");
    document.getElementById("caseTabs").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (button) showPane(button.dataset.pane);
    });
    dialog.addEventListener("submit", (event) => {
      if (event.target.id !== "caseAddAction") return;
      event.preventDefault();
      const input = event.target.querySelector("input");
      const label = input.value.trim();
      if (!label) return;
      patch({ add_action: label }).then(() => window.showMessage("Acción añadida: queda anotada como decisión tuya.", "success")).catch((error) => window.showMessage(error.message, "error"));
    });
    dialog.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-case-action]");
      if (action) return caseAction(action.dataset.caseAction);

      const retryButton = event.target.closest("[data-retry-event]");
      if (retryButton) return retryEvent(Number(retryButton.dataset.retryEvent));

      const toggle = event.target.closest("[data-action-index]");
      if (toggle) return patch({ action_index: Number(toggle.dataset.actionIndex), done: toggle.checked }).catch((error) => window.showMessage(error.message, "error"));

      const status = event.target.closest("[data-doc-status]");
      if (status) {
        const doc = current.documents[Number(status.dataset.docStatus)];
        return patch({ document_code: doc.code, document_detail: doc.detail, document_status: status.dataset.value }).catch((error) => window.showMessage(error.message, "error"));
      }

      const attach = event.target.closest("[data-attach]");
      if (attach) {
        const index = Number(attach.dataset.attach);
        const doc = current.documents[index];
        pendingItemCode = doc ? (doc.code !== "OTRO" ? doc.code : `OTRO:${(doc.detail || doc.label).slice(0, 30)}`) : null;
        document.getElementById("caseFileInput").click();
        return;
      }

      const copy = event.target.closest("[data-copy]");
      if (copy) {
        try {
          await navigator.clipboard.writeText(copy.dataset.copy);
          window.showMessage("Enlace copiado.", "success");
        } catch {
          window.prompt("Copia el enlace:", copy.dataset.copy);
        }
        return;
      }

      const other = event.target.closest("[data-open-case]");
      if (other) return openCase(Number(other.dataset.openCase));
      const doc = event.target.closest("[data-open-document]");
      if (doc) {
        dialog.close();
        return window.showDetail?.(Number(doc.dataset.openDocument));
      }
    });
    document.getElementById("caseFileInput").addEventListener("change", (event) => {
      const file = event.target.files[0];
      event.target.value = "";
      if (file) uploadAttachment(file);
    });
    dialog.addEventListener("close", () => { pane = "resumen"; refreshBackground(); });

    loadBriefing().catch(() => {});
  }

  window.openCase = (id) => { window.activateTab("expedientes"); openCase(id); };

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "expedientes";
    if (active) {
      showView(view);
      loadBriefing().catch(() => {});
    } else if (event.detail?.tab === "panel") {
      loadBriefing().catch(() => {});
    }
  });
  window.addEventListener("capafiscal:data-changed", refreshBackground);

  document.addEventListener("DOMContentLoaded", setup);
})();
