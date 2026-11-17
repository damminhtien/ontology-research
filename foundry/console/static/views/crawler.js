/** Configure bounded regional crawls and inspect their run state. */
import { badge, esc, get, post, put } from "../api.js";

const TOKEN_KEY = "foundry.admin.token";
let refreshTimer = null;

function statusBadge(status) {
  const variant = {
    succeeded: "ok",
    running: "info",
    queued: "info",
    stopping: "warn",
    stopped: "warn",
    failed: "error",
    interrupted: "error",
  }[status] ?? "warn";
  return badge(variant, status ?? "unknown");
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
  if (!runs.length) return `<tr><td colspan="5" class="empty">No crawl runs yet.</td></tr>`;
  return runs
    .map((run) => `<tr>
      <td><button class="link-button" data-open-run="${esc(run.run_id)}">${esc(run.run_id)}</button></td>
      <td>${esc(run.source_id)}</td>
      <td>${statusBadge(run.status)}</td>
      <td class="mono">${esc(run.started_at ?? run.created_at)}</td>
      <td>${run.summary?.total_events ?? "—"}</td>
    </tr>`)
    .join("");
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
  const state = await get("/api/crawler/state");
  const savedToken = sessionStorage.getItem(TOKEN_KEY) ?? "";
  view.innerHTML = `
    <p class="view-sub">Configure and run the existing regional ingestion pipeline. Runs and logs are stored outside the repository.</p>

    <section>
      <h3 class="section-title">Control</h3>
      <div class="panel">
        <div class="row crawl-token-row">
          <label for="crawl-token">Console token</label>
          <input id="crawl-token" type="password" autocomplete="off" placeholder="$FOUNDRY_CONSOLE_TOKEN" value="${esc(savedToken)}">
          <button id="crawl-save-token">Save for this session</button>
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
      <table class="data" id="crawl-runs">
        <thead><tr><th>Run</th><th>Source</th><th>Status</th><th>Started</th><th>Events</th></tr></thead>
        <tbody>${renderRuns(state.runs)}</tbody>
      </table>
    </section>

    <section>
      <h3 class="section-title">Run details</h3>
      <div class="row crawl-log-controls">
        <select id="crawl-log-run" aria-label="Select crawl run">
          ${state.runs.map((run) => `<option value="${esc(run.run_id)}">${esc(run.run_id)} · ${esc(run.source_id)}</option>`).join("")}
        </select>
        <button id="crawl-load-log">Load details and log</button>
      </div>
      <pre id="crawl-run-detail" class="crawl-output">Select a run to view its summary.</pre>
      <pre id="crawl-log" class="crawl-output crawl-log">Logs require the console token.</pre>
    </section>`;

  const tokenInput = view.querySelector("#crawl-token");
  const token = () => tokenInput.value.trim();
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

  view.querySelector("#crawl-save-token").onclick = () => {
    sessionStorage.setItem(TOKEN_KEY, token());
    message("Token saved for this browser session.");
  };
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
      sessionStorage.setItem(TOKEN_KEY, token());
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
      view.querySelector("#crawl-run-detail").textContent = JSON.stringify(run, null, 2);
      const logs = await get(`/api/crawler/runs/${encodeURIComponent(runId)}/logs`, token());
      view.querySelector("#crawl-log").textContent = logs.logs || "No log output yet.";
    } catch (error) {
      view.querySelector("#crawl-log").textContent = `Could not load run log: ${error.message}`;
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
  refreshTimer = window.setInterval(async () => {
    try {
      await loadState();
      const runId = view.querySelector("#crawl-log-run").value;
      if (runId && token()) await loadRun(runId);
    } catch (error) {
      message(error.message, true);
    }
  }, 5000);
  view._cleanup = () => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
    refreshTimer = null;
  };
}
