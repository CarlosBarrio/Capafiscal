"use strict";

(() => {
  let active = false;
  let view = "personas";
  let catalog = null;
  let people = [];
  let currentEmployee = null;
  let calendarMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);

  const esc = (value) => window.escapeHtml(value);
  const money = (value) => window.formatMoney(value);

  const STATUS = {
    ACTIVE: ["En plantilla", "status-success"],
    INCOMING: ["Incorporación próxima", "status-warning"],
    TERMINATED: ["Baja", "status-neutral"],
  };

  const SS_CLASS = {
    ALTA: "status-success",
    PENDIENTE_ALTA: "status-warning",
    BAJA: "status-neutral",
  };

  function avatar(person, size = "") {
    return `<span class="avatar ${size}" style="--avatar:${esc(person.color || "#8c1d33")}">${esc(person.initials || "?")}</span>`;
  }

  async function loadCatalog() {
    if (catalog) return catalog;
    catalog = await window.apiRequest("/team/catalog");

    const options = (items) => items.map((item) => `<option value="${esc(item.code)}">${esc(item.label)}</option>`).join("");
    document.getElementById("employeeContract").innerHTML = options(catalog.contract_types);
    document.getElementById("employeeSsStatus").innerHTML = options(catalog.ss_statuses);
    document.getElementById("employeeDocKind").innerHTML = options(catalog.document_kinds);
    document.getElementById("absenceKind").innerHTML = options(catalog.absence_kinds);

    return catalog;
  }

  /* ------------------------------------------------------------
  RESUMEN
  ------------------------------------------------------------ */
  async function loadOverview() {
    const row = document.getElementById("teamHeadcount")?.closest(".kpi-row");
    try {
      const overview = await window.apiRequest("/team/overview");
      // Sin nadie en plantilla no hay cifras: lo explica la lista de personas.
      window.setFigures(row, overview.headcount || overview.incoming || overview.terminated ? "ready" : "empty");
      document.getElementById("teamHeadcount").textContent = overview.headcount;
      document.getElementById("teamFte").textContent = `${String(overview.fte).replace(".", ",")} jornadas completas`;
      document.getElementById("teamIncoming").textContent = overview.incoming;
      document.getElementById("teamCost").textContent = money(overview.annual_cost);
      document.getElementById("teamAbsent").textContent = overview.absent_today.length;
      document.getElementById("teamAbsentNames").textContent = overview.absent_today
        .map((item) => `${item.employee_name.split(" ")[0]} · ${item.kind_label.toLowerCase()}`)
        .join(", ");

      const alerts = document.getElementById("teamAlerts");
      alerts.innerHTML = overview.alerts.length ? `
        <div class="card alert-card">
          <div class="card-head">
            <h2>Pendientes del equipo</h2>
            <span class="card-sub">${window.pl(overview.alerts.length, "tarea(s)")} con plazo</span>
          </div>
          <div class="alert-list">
            ${overview.alerts.slice(0, 6).map((alert) => `
              <button type="button" class="alert-row ${alert.status}" data-open-employee="${alert.employee_id}">
                ${window.icon(alert.status === "overdue" ? "alert" : "clock")}
                <span><strong>${esc(alert.employee_name)}</strong> · ${esc(alert.title)}</span>
                <small>${alert.due_date ? window.formatDay(alert.due_date) : ""}</small>
              </button>
            `).join("")}
          </div>
        </div>
      ` : "";
    } catch (error) {
      window.setFigures(row, "error", { what: "las cifras del equipo", error, retry: () => { window.setFigures(row, "loading"); loadOverview(); } });
    }
  }

  /* ------------------------------------------------------------
  PERSONAS
  ------------------------------------------------------------ */
  async function loadPeople() {
    people = await window.apiRequest("/team/employees");

    const departments = [...new Set(people.map((person) => person.department).filter(Boolean))].sort();
    const select = document.getElementById("peopleDepartment");
    const current = select.value;
    select.innerHTML = `<option value="">Todos</option>` + departments.map((name) => `<option>${esc(name)}</option>`).join("");
    select.value = current;
    document.getElementById("departmentOptions").innerHTML = departments.map((name) => `<option value="${esc(name)}">`).join("");

    renderPeople();
  }

  function renderPeople() {
    const grid = document.getElementById("peopleGrid");
    const search = (document.getElementById("peopleSearch").value || "").toLowerCase();
    const department = document.getElementById("peopleDepartment").value;
    const status = document.getElementById("peopleStatus").value;

    const filtered = people.filter((person) => {
      const haystack = `${person.name} ${person.job_title || ""} ${person.department || ""} ${person.skills.join(" ")}`.toLowerCase();
      return (!search || haystack.includes(search))
        && (!department || person.department === department)
        && (!status || person.status === status);
    });

    if (!people.length) {
      grid.innerHTML = window.emptyState(
        "users",
        "Aún no hay nadie en el equipo",
        "Crea la primera ficha o sube un CV y el agente rellenará los datos."
      );
      return;
    }

    if (!filtered.length) {
      grid.innerHTML = window.emptyState("search", "Nadie coincide con el filtro", "Prueba con otro nombre o departamento.");
      return;
    }

    grid.innerHTML = filtered.map((person) => {
      const [statusLabel, statusClass] = STATUS[person.status] || STATUS.ACTIVE;
      return `
        <button type="button" class="person-card" data-open-employee="${person.id}">
          <div class="person-top">
            ${avatar(person, "avatar-md")}
            <div class="person-id">
              <strong>${esc(person.name)}</strong>
              <span>${esc(person.job_title || "Sin puesto")}${person.department ? ` · ${esc(person.department)}` : ""}</span>
            </div>
          </div>
          <div class="person-tags">
            <span class="status-pill ${statusClass}">${esc(statusLabel)}</span>
            <span class="status-pill ${SS_CLASS[person.ss_status] || "status-neutral"}">SS: ${esc(person.ss_status_label)}</span>
            ${person.absent_today ? `<span class="status-pill status-info">${esc(person.absent_today)}</span>` : ""}
          </div>
          <dl class="person-facts">
            <div><dt>Contrato</dt><dd>${esc(person.contract_label)}${person.workday_percent < 100 ? ` · ${person.workday_percent} %` : ""}</dd></div>
            <div><dt>Alta</dt><dd>${person.hire_date ? window.formatDay(person.hire_date) : "—"}</dd></div>
            <div><dt>Responsable</dt><dd>${esc(person.manager_name || "—")}</dd></div>
            <div><dt>Proyectos</dt><dd>${person.projects.length ? person.projects.map((project) => `${esc(project.name)} (${project.allocation} %)`).join(", ") : "—"}</dd></div>
          </dl>
          ${person.urgent_checks ? `<p class="person-alert">${window.icon("alert")} ${window.pl(person.urgent_checks, "tarea(s) urgente(s)")} de incorporación</p>` : ""}
        </button>
      `;
    }).join("");
  }

  /* ------------------------------------------------------------
  FICHA
  ------------------------------------------------------------ */
  function showPane(pane) {
    document.querySelectorAll("#employeeTabs .segment").forEach((item) => item.classList.toggle("active", item.dataset.pane === pane));
    document.querySelectorAll("#employeeDialog .dialog-pane").forEach((item) => item.classList.toggle("hidden", item.dataset.pane !== pane));
  }

  function fillManagerOptions(excludeId) {
    const select = document.getElementById("employeeManager");
    select.innerHTML = `<option value="">Sin responsable</option>` + people
      .filter((person) => person.id !== excludeId && person.status !== "TERMINATED")
      .map((person) => `<option value="${person.id}">${esc(person.name)}${person.job_title ? ` · ${esc(person.job_title)}` : ""}</option>`)
      .join("");
  }

  function fillForm(data) {
    const form = document.getElementById("employeeForm");
    form.reset();

    for (const element of form.elements) {
      if (!element.name) continue;
      const value = data[element.name];
      element.value = value === null || value === undefined ? "" : value;
    }

    if (data.skills_text !== undefined) form.elements.skills.value = data.skills_text || "";
    if (!data.contract_type) form.elements.contract_type.value = "INDEFINIDO";
    if (!data.ss_status) form.elements.ss_status.value = "PENDIENTE_ALTA";
    if (!data.payments_per_year) form.elements.payments_per_year.value = "14";
    if (!data.workday_percent) form.elements.workday_percent.value = "100";
    if (data.vacation_days === undefined) form.elements.vacation_days.value = "22";
  }

  async function openEmployee(id, draft = null) {
    await loadCatalog();
    if (!people.length) await loadPeople();

    const dialog = document.getElementById("employeeDialog");
    const isNew = !id;
    currentEmployee = null;

    fillManagerOptions(id);
    document.getElementById("employeeDelete").classList.toggle("hidden", isNew);
    document.querySelectorAll('#employeeTabs .segment:not([data-pane="ficha"])').forEach((tab) => {
      tab.disabled = isNew;
    });
    showPane("ficha");

    if (isNew) {
      document.getElementById("employeeDialogTitle").textContent = draft?.first_name
        ? `${draft.first_name} ${draft.last_name || ""}`.trim()
        : "Nueva persona";
      document.getElementById("employeeDialogSub").textContent = draft
        ? "Datos leídos del CV: revísalos y completa el resto."
        : "Completa lo que tengas; puedes volver a editar la ficha cuando quieras.";
      document.getElementById("employeeAvatar").outerHTML = `<span class="avatar avatar-lg" id="employeeAvatar" style="--avatar:#8c1d33">${esc((draft?.first_name || "N").slice(0, 1).toUpperCase())}</span>`;
      fillForm(draft || {});
    } else {
      const data = await window.apiRequest(`/team/employees/${id}`);
      currentEmployee = data;
      renderEmployeeHeader(data);
      fillForm(data);
      renderDocuments(data);
      renderChecklist(data);
      renderCost(data);
    }

    if (!dialog.open) dialog.showModal();
  }

  function renderEmployeeHeader(data) {
    const [statusLabel] = STATUS[data.status] || STATUS.ACTIVE;
    document.getElementById("employeeDialogTitle").textContent = data.name;
    document.getElementById("employeeDialogSub").textContent =
      [data.job_title, data.department, statusLabel, data.ss_status_label ? `SS: ${data.ss_status_label}` : null]
        .filter(Boolean).join(" · ");
    document.getElementById("employeeAvatar").outerHTML =
      `<span class="avatar avatar-lg" id="employeeAvatar" style="--avatar:${esc(data.color)}">${esc(data.initials)}</span>`;
  }

  function renderDocuments(data) {
    const container = document.getElementById("employeeDocs");

    if (!data.documents.length) {
      container.innerHTML = `<p class="empty-inline">Sin documentos. Sube el CV, el contrato o el resguardo de alta; el resguardo marca el alta en la Seguridad Social.</p>`;
      return;
    }

    container.innerHTML = `
      <ul class="file-list">
        ${data.documents.map((document) => `
          <li>
            ${window.icon("doc")}
            <span class="file-meta">
              <strong>${esc(document.kind_label)}</strong>
              <small>${esc(document.filename)} · ${window.formatDate(document.uploaded_at)}</small>
            </span>
            <a class="btn-ghost" href="/api/team/documents/${document.id}/file" target="_blank" rel="noopener noreferrer">Abrir</a>
            <button type="button" class="btn-ghost danger-text" data-delete-doc="${document.id}">Eliminar</button>
          </li>
        `).join("")}
      </ul>
    `;
  }

  function checkMeta(item) {
    const due = item.due_date
      ? `Plazo: ${window.formatDay(item.due_date)}${item.status === "overdue" ? " (vencido)" : item.status === "due_soon" ? " (vence pronto)" : ""}`
      : null;
    return [due, item.detail, item.automatic ? "Se marca solo" : null].filter(Boolean).join(" · ");
  }

  function renderChecklist(data) {
    const container = document.getElementById("employeeChecklist");

    if (!data.checklist.length) {
      container.innerHTML = `<p class="empty-inline">Indica la fecha de alta en la ficha para generar el checklist de incorporación.</p>`;
      return;
    }

    const done = data.checklist.filter((item) => item.done).length;
    container.innerHTML = `
      <div class="progress-line">
        <span style="width:${Math.round(done / data.checklist.length * 100)}%"></span>
      </div>
      <p class="detail-hint">${done} de ${data.checklist.length} completadas. Las marcadas como automáticas se completan al subir el documento o registrar el alta.</p>
      <ul class="check-list">
        ${data.checklist.map((item) => `
          <li class="check-item ${item.status}">
            ${item.automatic
              ? `<span class="check-box ${item.done ? "checked" : ""}" aria-hidden="true">${item.done ? window.icon("check") : ""}</span>`
              : `<input type="checkbox" class="check-toggle" data-code="${esc(item.code)}" ${item.done ? "checked" : ""} aria-label="${esc(item.title)}">`}
            <span class="check-text">
              <strong>${esc(item.title)}</strong>
              ${checkMeta(item) ? `<small>${esc(checkMeta(item))}</small>` : ""}
            </span>
          </li>
        `).join("")}
      </ul>
    `;
  }

  function renderCost(data) {
    const container = document.getElementById("employeeCost");

    if (!data.cost) {
      container.innerHTML = `<p class="empty-inline">Indica el salario bruto anual en la ficha para calcular el coste.</p>`;
      return;
    }

    const cost = data.cost;
    container.innerHTML = `
      <div class="indicator-list three">
        <div class="indicator"><span>Salario bruto anual</span><strong>${money(cost.gross)}</strong></div>
        <div class="indicator"><span>Seguridad Social empresa</span><strong>${money(cost.ss_employer)}</strong><small>${String(cost.employer_ratio).replace(".", ",")} % sobre el bruto</small></div>
        <div class="indicator highlight"><span>Coste total empresa</span><strong>${money(cost.company_cost)}</strong></div>
        <div class="indicator"><span>Seguridad Social trabajador</span><strong>${money(cost.ss_employee)}</strong></div>
        <div class="indicator"><span>Retención IRPF</span><strong>${money(cost.irpf)}</strong><small>${String(cost.irpf_rate).replace(".", ",")} %${data.irpf_rate === null ? " estimado" : ""}</small></div>
        <div class="indicator"><span>Neto por paga</span><strong>${money(cost.monthly_net)}</strong><small>${data.payments_per_year} pagas</small></div>
      </div>
      <p class="report-note">Estimación anual con los parámetros de cotización configurados. La retención definitiva la da el cálculo oficial de la AEAT.</p>
    `;
  }

  async function saveEmployee(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {};

    for (const element of form.elements) {
      if (!element.name) continue;
      const value = element.value.trim();
      body[element.name] = value === "" ? null : value;
    }

    for (const key of ["manager_id", "workday_percent", "payments_per_year", "children", "vacation_days"]) {
      if (body[key] !== null) body[key] = Number(body[key]);
    }

    try {
      const isNew = !currentEmployee;
      const data = await window.jsonRequest(
        isNew ? "/team/employees" : `/team/employees/${currentEmployee.id}`,
        isNew ? "POST" : "PATCH",
        body
      );
      window.showMessage(isNew ? "Persona creada." : "Ficha guardada.", "success");
      await Promise.all([loadPeople(), loadOverview()]);
      await openEmployee(data.id);
      if (isNew) showPane("incorporacion");
      window.refreshAll?.();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  ORGANIGRAMA
  ------------------------------------------------------------ */
  async function loadOrgChart() {
    const container = document.getElementById("orgChart");
    const chart = await window.apiRequest("/team/org-chart");
    document.getElementById("orgSub").textContent =
      `${window.pl(chart.headcount, "persona(s)")} · ${window.pl(Object.keys(chart.departments).length, "departamento(s)")}`;

    if (!chart.roots.length) {
      container.innerHTML = window.emptyState("org", "Sin organigrama todavía", "Crea fichas e indica el responsable de cada persona.");
      return;
    }

    const node = (item) => `
      <li>
        <button type="button" class="org-node ${item.status === "INCOMING" ? "incoming" : ""}" data-open-employee="${item.id}">
          ${avatar(item)}
          <span>
            <strong>${esc(item.name)}</strong>
            <small>${esc(item.job_title || "Sin puesto")}</small>
          </span>
        </button>
        ${item.children.length ? `<ul>${item.children.map(node).join("")}</ul>` : ""}
      </li>
    `;

    container.innerHTML = `<ul class="org-tree">${chart.roots.map(node).join("")}</ul>`;
  }

  /* ------------------------------------------------------------
  PROYECTOS
  ------------------------------------------------------------ */
  async function loadProjects() {
    const container = document.getElementById("projectList");
    const projects = await window.apiRequest("/team/projects");
    if (!people.length) await loadPeople();

    if (!projects.length) {
      container.innerHTML = window.emptyState("briefcase", "Sin proyectos", "Crea un proyecto y asigna a las personas con su dedicación.");
      return;
    }

    const statusClass = { ACTIVE: "status-success", PAUSED: "status-warning", DONE: "status-neutral" };
    const assignable = people.filter((person) => person.status !== "TERMINATED");

    container.innerHTML = projects.map((project) => `
      <article class="project-card">
        <div class="project-head">
          <div>
            <h3>${esc(project.name)}</h3>
            <p>${esc(project.client_name || "Proyecto interno")}${project.end_date ? ` · hasta ${window.formatDay(project.end_date)}` : ""}</p>
          </div>
          <select class="status-select" data-project-status="${project.id}" aria-label="Estado del proyecto">
            ${["ACTIVE", "PAUSED", "DONE"].map((code) => `<option value="${code}" ${code === project.status ? "selected" : ""}>${esc({ ACTIVE: "Activo", PAUSED: "En pausa", DONE: "Terminado" }[code])}</option>`).join("")}
          </select>
        </div>
        ${project.description ? `<p class="project-description">${esc(project.description)}</p>` : ""}
        <div class="project-members">
          ${project.members.length ? project.members.map((member) => `
            <div class="member-row">
              ${avatar(member)}
              <span><strong>${esc(member.name)}</strong><small>${esc(member.role || "Equipo")}</small></span>
              <span class="allocation"><span style="width:${member.allocation}%"></span></span>
              <small class="allocation-value">${member.allocation} %</small>
              <button type="button" class="icon-button" data-remove-assignment="${member.assignment_id}" aria-label="Quitar">${window.icon("close")}</button>
            </div>
          `).join("") : `<p class="empty-inline">Sin personas asignadas.</p>`}
        </div>
        <form class="member-form" data-project="${project.id}">
          <select name="employee_id" required>
            <option value="">Añadir persona…</option>
            ${assignable.map((person) => `<option value="${person.id}">${esc(person.name)}</option>`).join("")}
          </select>
          <input type="text" name="role" placeholder="Rol" maxlength="100">
          <input type="number" name="allocation_percent" min="1" max="100" value="100" aria-label="Dedicación %">
          <button type="submit" class="btn-ghost">Asignar</button>
        </form>
        <p class="project-foot">
          <span class="status-pill ${statusClass[project.status]}">${esc(project.status_label)}</span>
          ${String(project.fte).replace(".", ",")} jornadas equivalentes
          <button type="button" class="link-button danger-text" data-delete-project="${project.id}">Eliminar</button>
        </p>
      </article>
    `).join("");
  }

  /* ------------------------------------------------------------
  AUSENCIAS
  ------------------------------------------------------------ */
  function iso(date) {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
  }

  async function loadAbsences() {
    await loadCatalog();
    if (!people.length) await loadPeople();

    document.getElementById("absenceEmployee").innerHTML = people
      .filter((person) => person.status !== "TERMINATED")
      .map((person) => `<option value="${person.id}">${esc(person.name)}</option>`).join("");

    const year = calendarMonth.getFullYear();
    const month = calendarMonth.getMonth();
    const first = new Date(year, month, 1);
    const last = new Date(year, month + 1, 0);
    const absences = await window.apiRequest(`/team/absences?date_from=${iso(first)}&date_to=${iso(last)}`);

    document.getElementById("absenceMonthTitle").textContent = new Intl.DateTimeFormat("es-ES", { month: "long", year: "numeric" }).format(first);

    const days = [];
    for (let day = 1; day <= last.getDate(); day += 1) days.push(new Date(year, month, day));

    const today = iso(new Date());
    const staff = people.filter((person) => person.status !== "TERMINATED");
    const kindClass = { VACACIONES: "vac", BAJA_IT: "sick", PERMISO: "leave", ASUNTOS_PROPIOS: "leave", OTRO: "other" };

    const container = document.getElementById("absenceCalendar");

    if (!staff.length) {
      container.innerHTML = `<p class="empty-inline">Añade personas al equipo para planificar ausencias.</p>`;
      return;
    }

    container.innerHTML = `
      <div class="absence-grid" style="--days:${days.length}">
        <div class="absence-corner"></div>
        ${days.map((day) => {
          const weekend = day.getDay() === 0 || day.getDay() === 6;
          return `<div class="absence-day ${weekend ? "weekend" : ""} ${iso(day) === today ? "today" : ""}">${day.getDate()}</div>`;
        }).join("")}
        ${staff.map((person) => `
          <div class="absence-person">${avatar(person)}<span>${esc(person.name)}</span></div>
          ${days.map((day) => {
            const key = iso(day);
            const absence = absences.find((item) => item.employee_id === person.id && item.start_date <= key && item.end_date >= key);
            const weekend = day.getDay() === 0 || day.getDay() === 6;
            return absence
              ? `<button type="button" class="absence-cell ${kindClass[absence.kind] || "other"}" title="${esc(`${absence.kind_label}: ${window.formatDay(absence.start_date)} – ${window.formatDay(absence.end_date)}${absence.notes ? ` · ${absence.notes}` : ""}`)}" data-absence="${absence.id}"></button>`
              : `<span class="absence-cell ${weekend ? "weekend" : ""}"></span>`;
          }).join("")}
        `).join("")}
      </div>
      <div class="absence-legend">
        <span><i class="absence-cell vac"></i>Vacaciones</span>
        <span><i class="absence-cell sick"></i>Baja médica</span>
        <span><i class="absence-cell leave"></i>Permisos</span>
        <span><i class="absence-cell other"></i>Otras</span>
        <small>Pulsa una ausencia para eliminarla.</small>
      </div>
    `;
  }

  /* ------------------------------------------------------------
  VISTAS
  ------------------------------------------------------------ */
  async function showView(name) {
    view = name;
    document.querySelectorAll("#teamViews .segment").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    document.querySelectorAll(".team-view").forEach((item) => item.classList.toggle("hidden", item.dataset.teamView !== name));

    try {
      if (name === "personas") await loadPeople();
      if (name === "organigrama") await loadOrgChart();
      if (name === "proyectos") await loadProjects();
      if (name === "ausencias") await loadAbsences();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function refresh() {
    await Promise.all([loadOverview(), showView(view)]);
  }

  function setup() {
    document.getElementById("teamViews")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-view]");
      if (button) showView(button.dataset.view);
    });

    ["peopleSearch", "peopleDepartment", "peopleStatus"].forEach((id) => {
      document.getElementById(id)?.addEventListener("input", renderPeople);
    });

    document.getElementById("tab-equipo")?.addEventListener("click", async (event) => {
      const opener = event.target.closest("[data-open-employee]");
      if (opener) openEmployee(Number(opener.dataset.openEmployee));

      const removeAssignment = event.target.closest("[data-remove-assignment]");
      if (removeAssignment) {
        await window.apiRequest(`/team/assignments/${removeAssignment.dataset.removeAssignment}`, { method: "DELETE" });
        loadProjects();
      }

      const deleteProject = event.target.closest("[data-delete-project]");
      if (deleteProject && await window.askConfirm("¿Eliminar el proyecto y sus asignaciones?")) {
        await window.apiRequest(`/team/projects/${deleteProject.dataset.deleteProject}`, { method: "DELETE" });
        loadProjects();
      }

      const absenceCell = event.target.closest("[data-absence]");
      if (absenceCell && await window.askConfirm("¿Eliminar esta ausencia?")) {
        await window.apiRequest(`/team/absences/${absenceCell.dataset.absence}`, { method: "DELETE" });
        loadAbsences();
        loadOverview();
      }
    });

    document.getElementById("tab-equipo")?.addEventListener("change", async (event) => {
      const statusSelect = event.target.closest("[data-project-status]");
      if (statusSelect) {
        await window.jsonRequest(`/team/projects/${statusSelect.dataset.projectStatus}`, "PATCH", { status: statusSelect.value });
        loadProjects();
      }
    });

    document.getElementById("tab-equipo")?.addEventListener("submit", async (event) => {
      const memberForm = event.target.closest(".member-form");
      if (!memberForm) return;
      event.preventDefault();
      const data = new FormData(memberForm);

      try {
        await window.jsonRequest(`/team/projects/${memberForm.dataset.project}/members`, "POST", {
          employee_id: Number(data.get("employee_id")),
          role: data.get("role") || null,
          allocation_percent: Number(data.get("allocation_percent") || 100),
        });
        loadProjects();
        loadOverview();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("newEmployeeButton")?.addEventListener("click", () => openEmployee(null));

    document.getElementById("cvInput")?.addEventListener("change", async (event) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file) return;

      const form = new FormData();
      form.append("uploaded_file", file, file.name);

      try {
        window.showMessage("Leyendo el CV…", "info");
        const draft = await window.apiRequest("/team/cv/parse", { method: "POST", body: form });
        await openEmployee(null, draft);
        window.pendingCv = file;
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("employeeForm")?.addEventListener("submit", async (event) => {
      await saveEmployee(event);

      // Si la ficha se creó desde un CV, se adjunta el archivo automáticamente.
      if (window.pendingCv && currentEmployee) {
        const form = new FormData();
        form.append("uploaded_file", window.pendingCv, window.pendingCv.name);
        form.append("kind", "CV");
        window.pendingCv = null;
        try {
          const data = await window.apiRequest(`/team/employees/${currentEmployee.id}/documents`, { method: "POST", body: form });
          renderDocuments(data);
        } catch (error) {
          window.showMessage(`No se pudo adjuntar el CV: ${error.message}`, "warning");
        }
      }
    });

    document.getElementById("employeeDialogClose")?.addEventListener("click", () => {
      document.getElementById("employeeDialog").close();
      window.pendingCv = null;
    });

    document.getElementById("employeeTabs")?.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-pane]");
      if (tab && !tab.disabled) showPane(tab.dataset.pane);
    });

    document.getElementById("employeeDelete")?.addEventListener("click", async () => {
      if (!currentEmployee || !await window.askConfirm(`¿Eliminar la ficha de ${currentEmployee.name}? Si ya tiene nóminas, indica mejor una fecha de baja.`)) return;
      try {
        await window.apiRequest(`/team/employees/${currentEmployee.id}`, { method: "DELETE" });
        document.getElementById("employeeDialog").close();
        window.showMessage("Ficha eliminada.", "success");
        refresh();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("employeeDocForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentEmployee) return;
      try {
        const data = await window.apiRequest(`/team/employees/${currentEmployee.id}/documents`, {
          method: "POST",
          body: new FormData(event.currentTarget),
        });
        event.currentTarget.reset();
        currentEmployee = data;
        renderEmployeeHeader(data);
        renderDocuments(data);
        renderChecklist(data);
        window.showMessage("Documento guardado.", "success");
        loadOverview();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("employeeDocs")?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-delete-doc]");
      if (!button || !await window.askConfirm("¿Eliminar el documento?")) return;
      await window.apiRequest(`/team/documents/${button.dataset.deleteDoc}`, { method: "DELETE" });
      openEmployee(currentEmployee.id);
    });

    document.getElementById("employeeChecklist")?.addEventListener("change", async (event) => {
      const toggle = event.target.closest(".check-toggle");
      if (!toggle || !currentEmployee) return;
      const data = await window.jsonRequest(`/team/employees/${currentEmployee.id}/checklist`, "POST", {
        code: toggle.dataset.code,
        done: toggle.checked,
      });
      currentEmployee = data;
      renderChecklist(data);
      loadOverview();
    });

    document.getElementById("projectForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries([...new FormData(event.currentTarget).entries()].map(([key, value]) => [key, String(value).trim() || null]));
      try {
        await window.jsonRequest("/team/projects", "POST", data);
        event.currentTarget.reset();
        event.currentTarget.closest("details").open = false;
        loadProjects();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    document.getElementById("absenceForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget).entries());
      data.employee_id = Number(data.employee_id);
      data.notes = data.notes || null;
      try {
        await window.jsonRequest("/team/absences", "POST", data);
        window.showMessage("Ausencia registrada.", "success");
        event.currentTarget.reset();
        loadAbsences();
        loadOverview();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    const moveMonth = (delta) => {
      calendarMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + delta, 1);
      loadAbsences();
    };
    document.getElementById("absencePrev")?.addEventListener("click", () => moveMonth(-1));
    document.getElementById("absenceNext")?.addEventListener("click", () => moveMonth(1));
    document.getElementById("absenceToday")?.addEventListener("click", () => {
      const now = new Date();
      calendarMonth = new Date(now.getFullYear(), now.getMonth(), 1);
      loadAbsences();
    });
  }

  window.openEmployee = openEmployee;

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "equipo";
    if (active) {
      loadCatalog().catch(() => {});
      refresh();
    }
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
