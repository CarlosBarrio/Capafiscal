"use strict";

(() => {
    const ACTIVITY_API_URL = "/api/activity";

    let activityCache = [];
    let activityLoaded = false;
    let activityLoading = false;

    const IMPORTANT_ACTION_PARTS = [
        "approved",
        "rejected",
        "failed",
        "duplicate",
        "updated",
        "resolved",
        "warning",
        "error",
    ];

    const ACTION_LABELS = {
        "document.uploaded": "Documento cargado",
        "document.processing.started": "Extracción iniciada",
        "document.processing.completed": "Extracción completada",
        "document.processing.failed": "Extracción fallida",
        "document.reprocess.requested": "Reprocesamiento solicitado",
        "document.duplicate_upload_attempt": "Carga duplicada detectada",
        "document.not_identified_as_invoice":
            "Documento no identificado como factura",
        "document.approved_with_warnings":
            "Documento aprobado con advertencias",

        "invoice.updated": "Factura corregida",
        "invoice.approved": "Factura aprobada",
        "invoice.approved_with_warnings":
            "Factura aprobada con advertencias",
        "invoice.rejected": "Factura rechazada",
        "invoice.reopened": "Factura reabierta",
        "invoice.paid": "Factura marcada como pagada",
        "invoice.payment_cancelled": "Pago de factura anulado",

        "ledger.exported": "Libro registro exportado",
        "notification.detected": "Notificación detectada por el agente",
        "notification.created": "Notificación registrada",
        "notification.updated": "Notificación actualizada",
        "bank.imported": "Extracto bancario importado",
        "bank.reconciled": "Movimiento conciliado con factura",
        "bank.unreconciled": "Conciliación deshecha",
        "tax.filed": "Modelo marcado como presentado",
        "tax.filing_removed": "Presentación desmarcada",
        "compliance.updated": "Cumplimiento actualizado",
        "compliance.certificate_loaded": "Certificado digital leído",
        "company.updated": "Datos de empresa actualizados",
        "supplier_rule.learned": "Categoría aprendida por el agente",
        "supplier_rule.deleted": "Categoría olvidada",
        "assistant.query": "Consulta al asistente",

        "task.created": "Tarea creada",
        "task.started": "Tarea iniciada",
        "task.resolved": "Tarea resuelta",
        "task.resolved_automatically":
            "Tarea resuelta automáticamente",

        "connector.sync.completed":
            "Sincronización de conector completada",
    };

    const ENTITY_LABELS = {
        document: "Documento",
        invoice: "Factura",
        task: "Tarea",
        connector: "Conector",
        report: "Informe",
        notification: "Notificación",
        bank: "Banco",
        tax: "Impuestos",
        compliance: "Cumplimiento",
        company: "Mi empresa",
        supplier_rule: "Memoria del agente",
        assistant: "Asistente",
    };

    function activityEscapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function activityFormatDate(value) {
        if (!value) {
            return "Fecha desconocida";
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        return new Intl.DateTimeFormat("es-ES", {
            dateStyle: "medium",
            timeStyle: "short",
        }).format(date);
    }

    function activityActionLabel(action) {
        return ACTION_LABELS[action] || action || "Actividad";
    }

    function activityEntityLabel(entityType) {
        return ENTITY_LABELS[entityType] || entityType || "Sistema";
    }

    function activityTone(action) {
        const normalized = String(action || "").toLowerCase();

        if (
            normalized.includes("failed") ||
            normalized.includes("rejected") ||
            normalized.includes("error")
        ) {
            return "danger";
        }

        if (
            normalized.includes("warning") ||
            normalized.includes("duplicate") ||
            normalized.includes("not_identified")
        ) {
            return "warning";
        }

        if (
            normalized.includes("approved") ||
            normalized.includes("completed") ||
            normalized.includes("resolved")
        ) {
            return "success";
        }

        if (
            normalized.includes("updated") ||
            normalized.includes("started") ||
            normalized.includes("uploaded")
        ) {
            return "info";
        }

        return "neutral";
    }

    function activityIcon(action) {
        const tone = activityTone(action);

        const icons = {
            danger: "alert",
            warning: "alert",
            success: "check",
            info: "doc",
            neutral: "activity",
        };

        return icons[tone];
    }

    function cleanActivityData(value) {
        if (Array.isArray(value)) {
            return value.map(cleanActivityData);
        }

        if (
            value !== null &&
            typeof value === "object"
        ) {
            const hiddenKeys = new Set([
                "sha256",
                "stored_filename",
                "source_hash",
                "access_token",
                "refresh_token",
                "client_secret",
            ]);

            return Object.fromEntries(
                Object.entries(value)
                    .filter(([key]) => !hiddenKeys.has(key))
                    .map(([key, item]) => [
                        key,
                        cleanActivityData(item),
                    ])
            );
        }

        return value;
    }

    function getDocumentIdFromEvent(event) {
        if (event.entity_type === "document") {
            const documentId = Number(event.entity_id);

            return Number.isInteger(documentId)
                ? documentId
                : null;
        }

        const eventDocumentId = Number(
            event.event_data?.document_id
        );

        return Number.isInteger(eventDocumentId)
            ? eventDocumentId
            : null;
    }

    function changedFieldsSummary(event) {
        const changedFields =
            event.event_data?.changed_fields;

        if (
            !changedFields ||
            typeof changedFields !== "object"
        ) {
            return "";
        }

        const fieldNames = Object.keys(changedFields);

        if (!fieldNames.length) {
            return "";
        }

        return `Campos modificados: ${fieldNames.join(", ")}.`;
    }

    function eventSummary(event) {
        const data = event.event_data || {};
        const action = String(event.action || "");

        if (action === "document.uploaded") {
            return (
                `Se incorporó ${data.original_filename || "un documento"} ` +
                `mediante ${data.source || "carga manual"}.`
            );
        }

        if (action === "document.processing.completed") {
            const confidence =
                data.overall_confidence !== undefined &&
                data.overall_confidence !== null
                    ? ` Confianza: ${data.overall_confidence}%.`
                    : "";

            return (
                `Procesamiento completado. ` +
                `Estado: ${data.document_status || "sin indicar"}.` +
                confidence
            );
        }

        if (action === "document.processing.failed") {
            return data.error
                ? `La extracción falló: ${data.error}`
                : "La extracción automática no pudo completarse.";
        }

        if (action === "document.duplicate_upload_attempt") {
            return (
                `Se intentó cargar de nuevo ` +
                `${data.attempted_filename || "el mismo archivo"}.`
            );
        }

        if (action === "invoice.updated") {
            return (
                changedFieldsSummary(event) ||
                "Se guardaron correcciones manuales en la factura."
            );
        }

        if (
            action === "invoice.approved" ||
            action === "invoice.approved_with_warnings"
        ) {
            return data.total
                ? `Factura aprobada por un total de ${data.total}.`
                : "Factura aprobada.";
        }

        if (action === "invoice.rejected") {
            return data.reason
                ? `Motivo: ${data.reason}`
                : "La factura fue rechazada.";
        }

        if (action.startsWith("task.")) {
            return (
                data.reason ||
                data.notes ||
                data.resolution ||
                "Se actualizó una tarea de revisión."
            );
        }

        return (
            data.message ||
            data.error ||
            "Evento registrado en la trazabilidad del sistema."
        );
    }

    function matchesImportantFilter(event) {
        const action = String(event.action || "").toLowerCase();

        return IMPORTANT_ACTION_PARTS.some((part) => {
            return action.includes(part);
        });
    }

    function activitySearchText(event) {
        return [
            event.action,
            activityActionLabel(event.action),
            event.actor,
            event.entity_type,
            event.entity_id,
            eventSummary(event),
            JSON.stringify(event.event_data || {}),
        ]
            .join(" ")
            .toLowerCase();
    }

    function filteredActivity() {
        const searchInput =
            document.getElementById("activitySearch");

        const importantInput =
            document.getElementById("activityImportantOnly");

        const query = String(
            searchInput?.value || ""
        )
            .trim()
            .toLowerCase();

        const importantOnly = Boolean(
            importantInput?.checked
        );

        return activityCache.filter((event) => {
            if (
                importantOnly &&
                !matchesImportantFilter(event)
            ) {
                return false;
            }

            if (
                query &&
                !activitySearchText(event).includes(query)
            ) {
                return false;
            }

            return true;
        });
    }

    function renderActivitySummary(events) {
        const decisions = events.filter((event) => {
            const action = String(event.action || "");

            return (
                action.includes("approved") ||
                action.includes("rejected") ||
                action.includes("resolved")
            );
        }).length;

        const corrections = events.filter((event) => {
            return String(event.action || "")
                .includes("updated");
        }).length;

        const errors = events.filter((event) => {
            const tone = activityTone(event.action);

            return tone === "danger" || tone === "warning";
        }).length;

        const totalElement =
            document.getElementById("activityTotal");

        const decisionsElement =
            document.getElementById("activityDecisions");

        const correctionsElement =
            document.getElementById("activityCorrections");

        const errorsElement =
            document.getElementById("activityErrors");

        const counterElement =
            document.getElementById("cntActivity");

        if (totalElement) {
            totalElement.textContent = String(events.length);
        }

        if (decisionsElement) {
            decisionsElement.textContent = String(decisions);
        }

        if (correctionsElement) {
            correctionsElement.textContent = String(corrections);
        }

        if (errorsElement) {
            errorsElement.textContent = String(errors);
        }

        if (counterElement) {
            // Actividad es un registro, no una tarea: el menú no le pone contador.
            counterElement.classList.add("hidden");
        }
    }

    function activityCard(event) {
        const tone = activityTone(event.action);
        const documentId = getDocumentIdFromEvent(event);
        const cleanedData = cleanActivityData(
            event.event_data || {}
        );

        const detailsAvailable =
            Object.keys(cleanedData).length > 0;

        return `
            <article class="activity-item activity-${tone}">
                <div class="activity-marker">
                    ${window.icon(activityIcon(event.action))}
                </div>

                <div class="activity-content">
                    <div class="activity-head">
                        <div>
                            <h3>
                                ${activityEscapeHtml(
                                    activityActionLabel(event.action)
                                )}
                            </h3>

                            <p class="activity-meta">
                                ${activityEscapeHtml(
                                    activityEntityLabel(event.entity_type)
                                )}
                                #${activityEscapeHtml(event.entity_id)}
                                ·
                                ${activityEscapeHtml(
                                    activityFormatDate(event.created_at)
                                )}
                            </p>
                        </div>

                        <span class="activity-actor">
                            ${activityEscapeHtml(
                                event.actor || "sistema"
                            )}
                        </span>
                    </div>

                    <p class="activity-description">
                        ${activityEscapeHtml(eventSummary(event))}
                    </p>

                    <div class="activity-actions">
                        ${
                            documentId
                                ? `
                                    <button
                                        type="button"
                                        class="btn-ghost"
                                        data-activity-document="${documentId}"
                                    >
                                        Abrir documento
                                    </button>
                                `
                                : ""
                        }

                        ${
                            detailsAvailable
                                ? `
                                    <details class="activity-details">
                                        <summary>
                                            Ver datos técnicos
                                        </summary>

                                        <pre>${activityEscapeHtml(
                                            JSON.stringify(
                                                cleanedData,
                                                null,
                                                2
                                            )
                                        )}</pre>
                                    </details>
                                `
                                : ""
                        }
                    </div>
                </div>
            </article>
        `;
    }

    function renderActivity() {
        const container =
            document.getElementById("activityList");

        if (!container) {
            return;
        }

        const events = filteredActivity();

        renderActivitySummary(activityCache);

        if (!events.length) {
            container.innerHTML = `
                <div class="section-empty">
                    <span class="section-empty-icon">${window.icon("activity")}</span>

                    <div>
                        <p class="section-empty-title">
                            No hay actividad para estos filtros
                        </p>

                        <p class="section-empty-text">
                            Las cargas, correcciones y decisiones
                            aparecerán aquí automáticamente.
                        </p>
                    </div>
                </div>
            `;

            return;
        }

        container.innerHTML = events
            .map(activityCard)
            .join("");

        container
            .querySelectorAll("[data-activity-document]")
            .forEach((button) => {
                button.addEventListener("click", () => {
                    const documentId = Number(
                        button.dataset.activityDocument
                    );

                    if (
                        documentId &&
                        typeof window.showDetail === "function"
                    ) {
                        window.showDetail(documentId);
                    }
                });
            });
    }

    async function activityRequest(url) {
        let response;

        try {
            response = await fetch(url, {
                headers: {
                    Accept: "application/json",
                },
            });
        } catch {
            throw new Error(
                "No se ha podido conectar con el servidor."
            );
        }

        let data = null;

        try {
            data = await response.json();
        } catch {
            data = null;
        }

        if (!response.ok) {
            const detail = data?.detail;

            if (typeof detail === "string") {
                throw new Error(detail);
            }

            if (detail?.message) {
                throw new Error(detail.message);
            }

            throw new Error(
                `Error HTTP ${response.status}`
            );
        }

        return data;
    }

    async function loadActivity(options = {}) {
        if (activityLoading) {
            return;
        }

        const force = Boolean(options.force);

        if (activityLoaded && !force) {
            renderActivity();
            return;
        }

        const container =
            document.getElementById("activityList");

        if (!container) {
            return;
        }

        activityLoading = true;

        container.innerHTML = `
            <div class="section-empty">
                <span class="section-empty-icon">${window.icon("clock")}</span>

                <div>
                    <p class="section-empty-title">
                        Cargando actividad
                    </p>

                    <p class="section-empty-text">
                        Consultando la trazabilidad persistente.
                    </p>
                </div>
            </div>
        `;

        try {
            const entityFilter =
                document.getElementById(
                    "activityEntityFilter"
                );

            const parameters = new URLSearchParams({
                limit: "200",
            });

            if (entityFilter?.value) {
                parameters.set(
                    "entity_type",
                    entityFilter.value
                );
            }

            const data = await activityRequest(
                `${ACTIVITY_API_URL}?${parameters.toString()}`
            );

            activityCache = Array.isArray(data)
                ? data
                : [];

            activityLoaded = true;
            renderActivity();
        } catch (error) {
            container.innerHTML = `
                <div class="section-empty">
                    <span class="section-empty-icon">${window.icon("alert")}</span>

                    <div>
                        <p class="section-empty-title">
                            No se pudo cargar la actividad
                        </p>

                        <p class="section-empty-text">
                            ${activityEscapeHtml(error.message)}
                        </p>
                    </div>
                </div>
            `;
        } finally {
            activityLoading = false;
        }
    }

    function setupActivity() {
        const activityTab = document.querySelector(
            '[data-tab="actividad"]'
        );

        const searchInput =
            document.getElementById("activitySearch");

        const importantInput =
            document.getElementById("activityImportantOnly");

        const entityFilter =
            document.getElementById("activityEntityFilter");

        const refreshButton =
            document.getElementById("refreshActivityButton");

        activityTab?.addEventListener("click", () => {
            loadActivity({
                force: true,
            });
        });

        searchInput?.addEventListener("input", () => {
            renderActivity();
        });

        importantInput?.addEventListener("change", () => {
            renderActivity();
        });

        entityFilter?.addEventListener("change", () => {
            activityLoaded = false;

            loadActivity({
                force: true,
            });
        });

        refreshButton?.addEventListener("click", () => {
            loadActivity({
                force: true,
            });
        });

        window.addEventListener(
            "capafiscal:activity-changed",
            () => {
                activityLoaded = false;
            }
        );
    }

    window.loadActivity = loadActivity;

    document.addEventListener(
        "DOMContentLoaded",
        setupActivity
    );
})();