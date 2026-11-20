/** Configure bounded regional crawls and inspect their run state. */
import { badge, esc, get, post, put } from "../api.js";

const TOKEN_KEY = "foundry.admin.token";
const COUNT_FIELDS = [
  "mapped_records",
  "entities",
  "entities_linked",
  "identities_reused",
  "documents",
  "documents_deduplicated",
  "positions",
  "positions_deduplicated",
  "reviews",
  "skipped",
];
let refreshTimer = null;

function statusBadge(status) {
  const variant = {
    succeeded: "ok",
    complete: "ok",
    running: "info",
    queued: "info",
    stopping: "warn",
    stopped: "warn",
    skipped: "warn",
    failed: "error",
    error: "error",
    interrupted: "error",
  }[status] ?? "warn";
  return badge(variant, status ?? "unknown");
}

function number(value) {
  return Number.isInteger(value) && value >= 0 ? new Intl.NumberFormat().format(value) : "—";
}

function mappedRecordTotal(summary) {
  const sources = Object.values(summary?.sources ?? {});
  return sources.reduce((total, source) => {
    if (Number.isInteger(source.mapped_records)) return total + source.mapped_records;
    return total + (source.entities ?? 0) + (source.documents ?? 0);
  }, 0);
}

function elapsedSeconds(run) {
  const start = Date.parse(run.started_at ?? run.created_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : Date.now();
  return Number.isFinite(start) ? Math.max(0, Math.floor((end - start) / 1000)) : 0;
}

function durationLabel(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}h ${minutes}m ${remainder}s`;
  if (minutes) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function renderSources(sources) {
  return sources
    .map((source) => {
      const credential = source.credential_env
        ? source.credential_configured
          ? badge("ok", "credential ready")
          : badge("warn", `set ${esc(source.credential_env)}`)
        : badge("ok", "public endpoint");
      return `<tr>
        <td>${esc(source.name)}<div class="sub mono">${esc(source.id)}</div></td>
        <td>${credential}</td>
        <td><details><summary>mapping and freshness</summary>
          <div class="crawl-details"><strong>${esc(source.freshness_policy)}</strong><br>${esc(source.mapping_rules)}</div>
        </details></td>
      </tr>`;
    })
    .join("");
}

function renderRuns(runs) {
  if (!runs.length) return `<tr><td colspan="6" class="empty">No crawl runs yet.</td></tr>`;
  return runs
    .map((run) => `<tr>
      <td><button class="link-button" data-open-run="${esc(run.run_id)}">${esc(run.run_id)}</button></td>
      <td>${esc(run.source_id)}</td>
      <td>${statusBadge(run.status)}</td>
      <td class="mono">${esc(run.started_at ?? run.created_at)}</td>
      <td>${number(mappedRecordTotal(run.summary))}</td>
      <td>${number(run.summary?.total_events)}</td>
    </tr>`)
    .join("");
}

function renderSourceStats(sources) {
  const entries = Object.entries(sources ?? {});
  if (!entries.length) return `<p class="sub">No per-source summary was produced.</p>`;
  return `<div class="crawl-table-wrap"><table class="data crawl-stats-table">
    <thead><tr><th>Source</th><th>Status</th>${COUNT_FIELDS.map((field) => `<th>${esc(field.replaceAll("_", " "))}</th>`).join("")}<th>Snapshot</th></tr></thead>
    <tbody>${entries.map(([sourceId, source]) => `<tr>
      <td class="mono">${esc(sourceId)}</td>
      <td>${statusBadge(source.status)}</td>
      ${COUNT_FIELDS.map((field) => `<td>${number(source[field])}</td>`).join("")}
      <td class="mono" title="${esc(source.snapshot ?? "")}">${source.snapshot ? esc(source.snapshot.slice(0, 12)) : "—"}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function renderEventStats(eventCounts) {
  const entries = Object.entries(eventCounts ?? {}).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) return `<p class="sub">No event counts were recorded.</p>`;
  return `<div class="crawl-event-counts">${entries.map(([eventType, count]) => `
    <div class="crawl-event-count"><span>${esc(eventType)}</span><strong>${number(count)}</strong></div>
  `).join("")}</div>`;
}

function renderRunDetails(run) {
  const summary = run.summary;
  const mapped = mappedRecordTotal(summary);
  const elapsed = elapsedSeconds(run);
  const rate = elapsed > 0 ? `${(mapped / elapsed).toFixed(2)} records/s` : "—";
  const sources = summary?.sources ?? {};
  const errors = Object.entries(sources).filter(([, source]) => source.status === "error");
  const skipped = Object.entries(sources).filter(([, source]) => source.status === "skipped");

  return `<div class="crawl-run-meta">
      <span>${statusBadge(run.status)}</span>
      <span>Source: <strong>${esc(run.source_id)}</strong></span>
      <span>Started: <strong>${esc(run.started_at ?? run.created_at ?? "—")}</strong></span>
      <span>Finished: <strong>${esc(run.finished_at ?? "running")}</strong></span>
      <span>Elapsed: <strong>${durationLabel(elapsed)}</strong></span>
      <span>Limit: <strong>${number(run.max_records)} per source</strong></span>
      <span>AIS window: <strong>${number(run.duration_seconds)} s</strong></span>
      <span>Exit code: <strong>${run.exit_code ?? "—"}</strong></span>
      ${summary?.generated_at ? `<span>Summary: <strong>${esc(summary.generated_at)}</strong></span>` : ""}
    </div>
    ${summary ? `<div class="crawl-stat-cards">
      <article><span>Mapped source records</span><strong>${number(mapped)}</strong><small>Sum across sources in this run</small></article>
      <article><span>Events</span><strong>${number(summary.total_events)}</strong><small>Canonical events written</small></article>
      <article><span>Deduplicated</span><strong>${number(summary.deduplicated_records)}</strong><small>Repeated assertions and document versions</small></article>
      <article><span>Observed rate</span><strong>${rate}</strong><small>Mapped records / elapsed time</small></article>
      <article><span>Sources</span><strong>${number(Object.keys(sources).length)}</strong><small>${number(errors.length)} errors · ${number(skipped.length)} skipped</small></article>
    </div>
    <h4>Per-source results</h4>
    ${renderSourceStats(sources)}
    <h4>Event types</h4>
    ${renderEventStats(summary.event_counts)}` : `<p class="sub">No summary was produced for this run.</p>`}
    ${run.error ? `<p class="crawl-run-error"><strong>Run error:</strong> ${esc(run.error)}</p>` : ""}
    ${errors.length ? `<p class="crawl-run-error"><strong>Sources with errors:</strong> ${errors.map(([id]) => esc(id)).join(", ")}. See the filtered log below for details.</p>` : ""}
    ${skipped.length ? `<p class="sub"><strong>Skipped sources:</strong> ${skipped.map(([id]) => esc(id)).join(", ")}.</p>` : ""}`;
}

function updateLiveState(view, state) {
  const active = state.active_run;
  const statusEl = view.querySelector("#crawl-active-status");
  const stopButton = view.querySelector("#crawl-stop");
  if (active) {
    statusEl.innerHTML = `${statusBadge(active.status)} <span class="mono">${esc(active.run_id)}</span>`;
    stopButton.hidden = !["queued", "running"].includes(active.status);
    stopButton.dataset.runId = active.run_id;
  } else {
    statusEl.innerHTML = badge("info", "idle");
    stopButton.hidden = true;
    stopButton.dataset.runId = "";
  }
  view.querySelector("#crawl-runs tbody").innerHTML = renderRuns(state.runs);
  const selector = view.querySelector("#crawl-log-run");
  const previous = selector.value;
  selector.innerHTML = state.runs
    .map((run) => `<option value="${esc(run.run_id)}">${esc(run.run_id)} · ${esc(run.source_id)}</option>`)
    .join("");
  if (state.runs.some((run) => run.run_id === previous)) selector.value = previous;
  else if (active) selector.value = active.run_id;
}

export async function renderCrawler(view) {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
  view.innerHTML = `<p class="view-sub">Loading crawl service…</p>`;

  let localSession = false;
  try {
    const session = await post("/api/crawler/session", {});
    localSession = session.authenticated === true;
  } catch {
    localSession = false;
  }
  if (localSession) sessionStorage.removeItem(TOKEN_KEY);

  const state = await get("/api/crawler/state");
  const savedToken = localSession ? "" : sessionStorage.getItem(TOKEN_KEY) ?? "";
  view.innerHTML = `
    <p class="view-sub">Configure and run the existing regional ingestion pipeline. Runs and logs are stored outside the repository.</p>

    <section>
      <h3 class="section-title">Control</h3>
      <div class="panel">
        <div class="row crawl-token-row">
          ${localSession
            ? `<span class="badge ok">Local browser session active; token remains private.</span>`
            : `<label for="crawl-token">Console token</label>
               <input id="crawl-token" type="password" autocomplete="off" placeholder="$FOUNDRY_CONSOLE_TOKEN" value="${esc(savedToken)}">
               <button id="crawl-save-token">Save for this session</button>`}
          <span id="crawl-message" class="sub"></span>
        </div>
        <form id="crawl-config" class="crawl-config">
          <label>Source
            <select id="crawl-source">
              <option value="all">All regional sources</option>
              ${state.sources.map((source) => `<option value="${esc(source.id)}">${esc(source.name)}</option>`).join("")}
            </select>
          </label>
          <label>Maximum records per source
            <input id="crawl-max-records" type="number" min="1" max="1000" value="${state.config.max_records}">
          </label>
          <label>AIS collection window (seconds)
            <input id="crawl-duration" type="number" min="1" max="300" value="${state.config.duration_seconds}">
          </label>
          <button type="submit">Save configuration</button>
          <button type="button" class="primary" id="crawl-start">Start crawl</button>
        </form>
        <div class="row crawl-active-row"><strong>Current run</strong><span id="crawl-active-status"></span>
          <button id="crawl-stop" class="danger-button" hidden>Stop run</button></div>
      </div>
    </section>

    <section>
      <h3 class="section-title">Regional sources (${state.sources.length})</h3>
      <table class="data" id="crawl-sources">
        <thead><tr><th>Source</th><th>Access</th><th>Mapping</th></tr></thead>
        <tbody>${renderSources(state.sources)}</tbody>
      </table>
    </section>

    <section>
      <h3 class="section-title">Recent runs</h3>
      <div class="crawl-table-wrap"><table class="data" id="crawl-runs">
        <thead><tr><th>Run</th><th>Source</th><th>Status</th><th>Started</th><th>Mapped records</th><th>Events</th></tr></thead>
        <tbody>${renderRuns(state.runs)}</tbody>
      </table></div>
      <p class="sub">New assertions and documents use the shared production event log. Repeated source records and document versions are counted as deduplicated.</p>
    </section>

    <section>
      <h3 class="section-title">Run details</h3>
      <div class="row crawl-log-controls">
        <select id="crawl-log-run" aria-label="Select crawl run">
          ${state.runs.map((run) => `<option value="${esc(run.run_id)}">${esc(run.run_id)} · ${esc(run.source_id)}</option>`).join("")}
        </select>
        <button id="crawl-load-log">Refresh details and log</button>
      </div>
      <div id="crawl-run-detail" class="crawl-run-detail">Select a run to view its summary.</div>
      <h4>Run log</h4>
      <pre id="crawl-log" class="crawl-output crawl-log">Run logs load automatically for the selected run.</pre>
    </section>`;

  const tokenInput = view.querySelector("#crawl-token");
  const token = () => localSession ? "" : tokenInput?.value.trim() ?? "";
  const message = (text, isError = false) => {
    const target = view.querySelector("#crawl-message");
    target.textContent = text;
    target.className = isError ? "error-text" : "sub";
  };
  const loadState = async () => {
    const next = await get("/api/crawler/state");
    updateLiveState(view, next);
    return next;
  };

  view.querySelector("#crawl-save-token")?.addEventListener("click", () => {
    sessionStorage.setItem(TOKEN_KEY, token());
    message("Token saved for this browser session.");
  });
  view.querySelector("#crawl-config").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const result = await put("/api/crawler/config", {
        source_id: view.querySelector("#crawl-source").value,
        max_records: Number(view.querySelector("#crawl-max-records").value),
        duration_seconds: Number(view.querySelector("#crawl-duration").value),
      }, token());
      message(`Saved source ${result.config.source_id}.`);
    } catch (error) {
      message(error.message, true);
    }
  };
  view.querySelector("#crawl-start").onclick = async () => {
    try {
      const config = await put("/api/crawler/config", {
        source_id: view.querySelector("#crawl-source").value,
        max_records: Number(view.querySelector("#crawl-max-records").value),
        duration_seconds: Number(view.querySelector("#crawl-duration").value),
      }, token());
      const run = await post("/api/crawler/runs", {}, token());
      if (localSession) sessionStorage.removeItem(TOKEN_KEY);
      else sessionStorage.setItem(TOKEN_KEY, token());
      message(`Started ${config.config.source_id}: ${run.run_id}`);
      await loadState();
      view.querySelector("#crawl-log-run").value = run.run_id;
      await loadRun(run.run_id);
    } catch (error) {
      message(error.message, true);
    }
  };
  view.querySelector("#crawl-stop").onclick = async (event) => {
    const runId = event.currentTarget.dataset.runId;
    if (!runId) return;
    try {
      await post(`/api/crawler/runs/${encodeURIComponent(runId)}/stop`, {}, token());
      message(`Stop requested for ${runId}.`);
      await loadState();
    } catch (error) {
      message(error.message, true);
    }
  };
  const loadRun = async (runId) => {
    try {
      const run = await get(`/api/crawler/runs/${encodeURIComponent(runId)}`);
      view.querySelector("#crawl-run-detail").innerHTML = renderRunDetails(run);
      try {
        const logs = await get(`/api/crawler/runs/${encodeURIComponent(runId)}/logs`, token());
        view.querySelector("#crawl-log").textContent = logs.logs || "No log output yet.";
      } catch (error) {
        view.querySelector("#crawl-log").textContent = `Could not load run log: ${error.message}`;
      }
    } catch (error) {
      view.querySelector("#crawl-run-detail").textContent = `Could not load run details: ${error.message}`;
    }
  };
  view.querySelector("#crawl-load-log").onclick = () => {
    const runId = view.querySelector("#crawl-log-run").value;
    if (runId) loadRun(runId);
  };
  view.querySelector("#crawl-runs tbody").onclick = (event) => {
    const button = event.target.closest("[data-open-run]");
    if (!button) return;
    view.querySelector("#crawl-log-run").value = button.dataset.openRun;
    loadRun(button.dataset.openRun);
  };

  updateLiveState(view, state);
  const initialRun = state.active_run?.run_id
    ?? state.runs.find((run) => run.source_id === "all")?.run_id
    ?? state.runs[0]?.run_id;
  if (initialRun) {
    view.querySelector("#crawl-log-run").value = initialRun;
    await loadRun(initialRun);
  }

  refreshTimer = window.setInterval(async () => {
    try {
      const next = await loadState();
      const runId = view.querySelector("#crawl-log-run").value;
      if (runId && (localSession || token())) await loadRun(runId);
      if (next.active_run && !localSession && !token()) {
        message("Enter a console token to load run logs or control the crawler.", true);
      }
    } catch (error) {
      message(error.message, true);
    }
  }, 5000);
  view._cleanup = () => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
    refreshTimer = null;
  };
}
