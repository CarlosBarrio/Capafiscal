"use strict";

/* Registro de jornada: fichar, resumen mensual, incidencias y correcciones. */
(() => {
  const esc = (value) => window.escapeHtml(value);
  const MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
  ];
  const WEEKDAYS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
  const STATES = {
    working: ["Trabajando", "status-success"],
    done: ["Jornada cerrada", "status-neutral"],
    pending: ["Sin fichar", "status-warning"],
    absent: ["Ausencia", "status-info"],
  };

  let active = false;
  let year = new Date().getFullYear();
  let month = new Date().getMonth() + 1;
  let board = null;
  let currentEntry = null;
  let tickTimer = null;
  let showAll = false;

  function hours(minutes) {
    const value = Math.abs(minutes);
    const text = `${Math.floor(value / 60)} h${value % 60 ? ` ${String(value % 60).padStart(2, "0")}` : ""}`;
    return minutes < 0 ? `−${text}` : text;
  }

  function time(value) {
    return value ? value.slice(11, 16) : "—";
  }

  function dayLabel(iso) {
    const date = new Date(`${iso}T12:00:00`);
    return `${WEEKDAYS[date.getDay()]} ${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
  }

  /* ------------------------------------------------------------
  HOY
  ------------------------------------------------------------ */
  async function loadBoard() {
    board = await window.apiRequest("/timesheet/today");
    renderBoard();
  }

  function liveMinutes(person) {
    if (person.state !== "working" || !person.since) return person.worked_minutes;
    return person.worked_minutes;
  }

  function renderBoard() {
    const container = document.getElementById("clockBoard");
    const people = board?.people || [];
    const working = people.filter((item) => item.state === "working").length;
    document.getElementById("tsWorking").textContent = working;
    document.getElementById("tsToday").textContent = hours(people.reduce((sum, item) => sum + liveMinutes(item), 0));
    document.getElementById("tsClock").textContent = board ? `${board.business_day ? "Laborable" : "Festivo o fin de semana"} · ${board.now.slice(11, 16)}` : "";

    const badge = document.getElementById("cntWorking");
    if (badge) {
      badge.textContent = working;
      badge.classList.toggle("hidden", !working);
    }

    if (!people.length) {
      container.innerHTML = window.emptyState("users", "No hay personas en plantilla", "Da de alta a tu equipo en «Equipo» y podrán fichar desde aquí.");
      return;
    }

    container.innerHTML = people.map((person) => {
      const [label, className] = STATES[person.state] || STATES.pending;
      const progress = person.expected_minutes ? Math.min(100, Math.round(liveMinutes(person) / person.expected_minutes * 100)) : 0;
      const canClock = person.state !== "absent";
      return `
        <button type="button" class="clock-tile state-${esc(person.state)}" data-clock="${person.employee_id}" ${canClock ? "" : "disabled"}>
          <span class="clock-top">
            <span class="avatar avatar-md" style="background:${esc(person.color)}1a;color:${esc(person.color)}">${esc(person.initials)}</span>
            <span class="clock-name">
              <strong>${esc(person.name)}</strong>
              <small>${esc(person.job_title || "")}</small>
            </span>
          </span>
          <span class="clock-hours">
            <span class="clock-hours-line">
              <strong>${hours(liveMinutes(person))}</strong>
              <span class="status-pill mini ${className}">${esc(label)}</span>
            </span>
            <small>${person.state === "working" ? `desde las ${time(person.since)}` : person.segments.length ? person.segments.map((item) => `${time(item.clock_in)}–${time(item.clock_out)}`).join(" · ") : "sin registros hoy"}</small>
          </span>
          <span class="clock-progress"><span style="width:${progress}%"></span></span>
          <span class="clock-action">${canClock ? (person.state === "working" ? `${window.icon("clock")} Fichar salida` : `${window.icon("play")} Fichar entrada`) : "Ausencia registrada"}</span>
        </button>
      `;
    }).join("");
  }

  async function clock(employeeId) {
    try {
      const result = await window.jsonRequest("/timesheet/clock", "POST", { employee_id: employeeId });
      const person = board.people.find((item) => item.employee_id === employeeId);
      window.showMessage(
        `${person?.name || "Persona"}: ${result.action === "in" ? "entrada" : "salida"} registrada a las ${time(result.action === "in" ? result.entry.clock_in : result.entry.clock_out)}.`,
        "success",
      );
      await Promise.all([loadBoard(), loadMonth(), loadEntries(), loadAlerts()]);
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  /* ------------------------------------------------------------
  INCIDENCIAS
  ------------------------------------------------------------ */
  async function loadAlerts() {
    const alerts = await window.apiRequest("/timesheet/alerts");
    document.getElementById("tsAlertCount").textContent = alerts.length;
    const container = document.getElementById("timesheetAlerts");
    if (!alerts.length) {
      container.innerHTML = "";
      return;
    }
    container.innerHTML = `
      <div class="card alert-card">
        <div class="card-head">
          <h2>Incidencias que ha detectado el agente</h2>
          <span class="card-sub">${alerts.length} en los últimos 14 días</span>
        </div>
        <div class="alert-list">
          ${alerts.slice(0, 12).map((alert) => `
            <div class="alert-row">
              ${window.icon(alert.kind === "missing" ? "calendar" : alert.kind === "forgotten" ? "clock" : "alert")}
              <span><strong>${esc(alert.employee_name)}</strong> · ${esc(alert.title)}<small class="muted block">${esc(alert.detail)}</small></span>
              ${alert.entry_id
                ? `<button type="button" class="btn-ghost" data-fix-entry="${alert.entry_id}">Corregir</button>`
                : alert.kind === "missing"
                  ? `<button type="button" class="btn-ghost" data-add-for="${alert.employee_id}" data-add-date="${esc(alert.date)}">Registrar</button>`
                  : ""}
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  /* ------------------------------------------------------------
  MES
  ------------------------------------------------------------ */
  async function loadMonth() {
    const data = await window.apiRequest(`/timesheet/month?year=${year}&month=${month}`);
    document.getElementById("tsMonthTitle").textContent = `Resumen de ${MONTHS[month - 1]} ${year}`;
    document.getElementById("tsPdf").href = `/api/timesheet/month.pdf?year=${year}&month=${month}`;
    document.getElementById("tsXlsx").href = `/api/timesheet/month.xlsx?year=${year}&month=${month}`;
    document.getElementById("tsMonthHours").textContent = hours(data.totals.worked_minutes);
    document.getElementById("tsMonthBalance").textContent = data.rows.length
      ? `${data.totals.balance_minutes >= 0 ? "+" : ""}${hours(data.totals.balance_minutes)} sobre lo previsto`
      : "";

    const select = document.getElementById("tsPerson");
    const selected = select.value;
    select.innerHTML = `<option value="">Todas las personas</option>` + data.rows
      .map((row) => `<option value="${row.employee_id}" ${String(row.employee_id) === selected ? "selected" : ""}>${esc(row.name)}</option>`).join("");
    document.getElementById("entryPerson").innerHTML = data.rows
      .map((row) => `<option value="${row.employee_id}">${esc(row.name)}</option>`).join("");

    const body = document.querySelector("#tsMonthTable tbody");
    if (!data.rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">Sin personas en plantilla este mes.</td></tr>`;
      return;
    }
    body.innerHTML = data.rows.map((row) => {
      const issues = [];
      if (row.missing_days.length) issues.push(`<span class="status-pill mini status-warning">${window.pl(row.missing_days.length, "día(s)")} sin registro</span>`);
      if (row.long_days.length) issues.push(`<span class="status-pill mini status-danger">${window.pl(row.long_days.length, "jornada(s)")} &gt; 9 h</span>`);
      if (row.manual_entries) issues.push(`<span class="status-pill mini status-neutral">${window.pl(row.manual_entries, "manual(es)")}</span>`);
      const balanceClass = row.balance_minutes < -60 ? "value-negative" : row.balance_minutes > 60 ? "value-positive" : "";
      return `
        <tr>
          <td>
            <span class="person-inline"><span class="avatar" style="background:${esc(row.color)}1a;color:${esc(row.color)}">${esc(row.initials)}</span>${esc(row.name)}</span>
            ${row.workday_percent < 100 ? `<small class="muted block">Jornada ${row.workday_percent} %</small>` : ""}
          </td>
          <td class="num">${row.days_worked}/${row.working_days}</td>
          <td class="num"><strong>${hours(row.worked_minutes)}</strong></td>
          <td class="num">${hours(row.expected_minutes)}</td>
          <td class="num ${balanceClass}">${row.balance_minutes >= 0 ? "+" : ""}${hours(row.balance_minutes)}</td>
          <td>${issues.join(" ") || `<span class="muted">—</span>`}</td>
          <td class="actions-cell">
            <a class="btn-ghost" href="/api/timesheet/month.pdf?year=${year}&month=${month}&employee_id=${row.employee_id}" target="_blank" rel="noopener noreferrer">PDF</a>
            <button type="button" class="btn-ghost" data-filter-person="${row.employee_id}">Ver</button>
          </td>
        </tr>
      `;
    }).join("");
  }

  async function loadEntries() {
    const from = `${year}-${String(month).padStart(2, "0")}-01`;
    const last = new Date(year, month, 0).getDate();
    const to = `${year}-${String(month).padStart(2, "0")}-${last}`;
    const person = document.getElementById("tsPerson").value;
    const entries = await window.apiRequest(`/timesheet/entries?date_from=${from}&date_to=${to}${person ? `&employee_id=${person}` : ""}`);
    const body = document.querySelector("#tsEntries tbody");
    body.dataset.entries = JSON.stringify(entries);
    if (!entries.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">Sin registros en ${MONTHS[month - 1]}.</td></tr>`;
      return;
    }
    const ordered = [...entries].reverse();
    const visible = showAll ? ordered : ordered.slice(0, 20);
    body.innerHTML = visible.map((entry) => `
      <tr>
        <td>${esc(dayLabel(entry.work_date))}</td>
        <td>${esc(entry.employee_name || "")}</td>
        <td class="mono">${time(entry.clock_in)}</td>
        <td class="mono">${entry.open ? `<span class="status-pill mini status-success">En curso</span>` : time(entry.clock_out)}</td>
        <td class="num">${hours(entry.minutes)}</td>
        <td>
          ${entry.source === "MANUAL" ? `<span class="status-pill mini status-neutral">Manual</span>` : ""}
          ${entry.edit_reason ? `<small class="muted">${esc(entry.edit_reason)}${entry.edited_by ? ` · ${esc(entry.edited_by)}` : ""}</small>` : ""}
          ${entry.note ? `<small class="muted block">${esc(entry.note)}</small>` : ""}
        </td>
        <td class="actions-cell"><button type="button" class="btn-ghost" data-fix-entry="${entry.id}">Corregir</button></td>
      </tr>
    `).join("") + (ordered.length > visible.length
      ? `<tr><td colspan="7" class="empty-cell"><button type="button" class="btn-ghost" data-show-all>Ver los ${ordered.length} registros del mes</button></td></tr>`
      : "");
  }

  /* ------------------------------------------------------------
  REGISTRO MANUAL / CORRECCIÓN
  ------------------------------------------------------------ */
  function openEntry(entry = null, preset = {}) {
    currentEntry = entry;
    const form = document.getElementById("entryForm");
    form.reset();
    document.getElementById("entryDialogTitle").textContent = entry ? "Corregir registro" : "Registro manual";
    document.getElementById("entryDelete").classList.toggle("hidden", !entry);
    form.elements.employee_id.value = entry?.employee_id || preset.employee_id || document.getElementById("tsPerson").value || form.elements.employee_id.options[0]?.value || "";
    form.elements.work_date.value = entry?.work_date || preset.date || window.todayIso?.() || new Date().toISOString().slice(0, 10);
    form.elements.start.value = entry ? time(entry.clock_in) : "09:00";
    form.elements.end.value = entry?.clock_out ? time(entry.clock_out) : entry ? "" : "17:00";
    form.elements.note.value = entry?.note || "";
    form.elements.employee_id.disabled = Boolean(entry);
    document.getElementById("entryDialog").showModal();
    form.elements.reason.focus();
  }

  async function findEntry(id) {
    const cached = JSON.parse(document.querySelector("#tsEntries tbody").dataset.entries || "[]");
    const found = cached.find((item) => item.id === id);
    if (found) return found;
    const today = new Date();
    const from = new Date(today.getTime() - 60 * 86400000).toISOString().slice(0, 10);
    const all = await window.apiRequest(`/timesheet/entries?date_from=${from}&date_to=${today.toISOString().slice(0, 10)}`);
    return all.find((item) => item.id === id);
  }

  async function saveEntry(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {
      employee_id: Number(currentEntry?.employee_id || form.elements.employee_id.value),
      work_date: form.elements.work_date.value,
      start: form.elements.start.value,
      end: form.elements.end.value || null,
      reason: form.elements.reason.value.trim(),
      note: form.elements.note.value.trim() || null,
    };
    try {
      if (currentEntry) await window.jsonRequest(`/timesheet/entries/${currentEntry.id}`, "PATCH", body);
      else await window.jsonRequest("/timesheet/entries", "POST", body);
      document.getElementById("entryDialog").close();
      window.showMessage("Registro guardado. La corrección queda en el historial.", "success");
      await refresh();
    } catch (error) {
      window.showMessage(error.message, "error");
    }
  }

  async function refresh() {
    const row = document.getElementById("tsWorking")?.closest(".kpi-row");
    try {
      await Promise.all([loadBoard(), loadAlerts(), loadMonth()]);
    } catch (error) {
      window.setFigures(row, "error", { what: "el registro de jornada", error, retry: () => { window.setFigures(row, "loading"); refresh().catch(() => {}); } });
      return;
    }
    // Sin personas en plantilla no hay jornada que contar: lo explica el bloque «Hoy».
    window.setFigures(row, board?.people?.length ? "ready" : "empty");
    await loadEntries();
  }

  function setup() {
    const section = document.getElementById("tab-jornada");
    if (!section) return;

    section.addEventListener("click", async (event) => {
      const tile = event.target.closest("[data-clock]");
      if (tile) return clock(Number(tile.dataset.clock));

      const fix = event.target.closest("[data-fix-entry]");
      if (fix) {
        const entry = await findEntry(Number(fix.dataset.fixEntry));
        if (entry) openEntry(entry);
        return;
      }

      const add = event.target.closest("[data-add-for]");
      if (add) return openEntry(null, { employee_id: add.dataset.addFor, date: add.dataset.addDate });

      if (event.target.closest("[data-show-all]")) {
        showAll = true;
        return loadEntries();
      }

      const filter = event.target.closest("[data-filter-person]");
      if (filter) {
        document.getElementById("tsPerson").value = filter.dataset.filterPerson;
        await loadEntries();
        document.getElementById("tsEntries").scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });

    document.getElementById("tsPrev").addEventListener("click", () => {
      month -= 1;
      if (month < 1) { month = 12; year -= 1; }
      Promise.all([loadMonth(), loadEntries()]).catch((error) => window.showMessage(error.message, "error"));
    });
    document.getElementById("tsNext").addEventListener("click", () => {
      month += 1;
      if (month > 12) { month = 1; year += 1; }
      Promise.all([loadMonth(), loadEntries()]).catch((error) => window.showMessage(error.message, "error"));
    });
    document.getElementById("tsPerson").addEventListener("change", () => {
      showAll = false;
      loadEntries().catch((error) => window.showMessage(error.message, "error"));
    });
    document.getElementById("tsAddEntry").addEventListener("click", () => openEntry());
    document.getElementById("entryForm").addEventListener("submit", saveEntry);
    document.getElementById("entryDelete").addEventListener("click", async () => {
      const reason = document.getElementById("entryForm").elements.reason.value.trim();
      if (!reason) {
        window.showMessage("Indica el motivo antes de borrar el registro.", "warning");
        return;
      }
      if (!await window.askConfirm("¿Borrar este registro? Quedará constancia en el historial.")) return;
      try {
        await window.jsonRequest(`/timesheet/entries/${currentEntry.id}/delete`, "POST", { reason });
        document.getElementById("entryDialog").close();
        await refresh();
      } catch (error) {
        window.showMessage(error.message, "error");
      }
    });

    window.apiRequest("/timesheet/today").then((data) => {
      board = data;
      const working = data.people.filter((item) => item.state === "working").length;
      const badge = document.getElementById("cntWorking");
      if (badge) {
        badge.textContent = working;
        badge.classList.toggle("hidden", !working);
      }
    }).catch(() => {});
  }

  window.addEventListener("capafiscal:tab-changed", (event) => {
    active = event.detail?.tab === "jornada";
    window.clearInterval(tickTimer);
    if (active) {
      refresh().catch((error) => window.showMessage(error.message, "error"));
      tickTimer = window.setInterval(() => loadBoard().catch(() => {}), 60000);
    }
  });

  document.addEventListener("DOMContentLoaded", setup);
})();
