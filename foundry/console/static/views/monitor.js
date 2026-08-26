/** Data Monitor view: event log, SHACL gate, competency-query regression. */
import { get, esc, badge } from "../api.js";

export async function renderMonitor(view) {
  const [stats, validation, cq] = await Promise.all([
    get("/api/monitor/events/stats"),
    get("/api/monitor/validation"),
    get("/api/monitor/cq"),
  ]);
  const recent = stats.exists ? await get("/api/monitor/events/recent?limit=20") : null;

  const eventRows = recent
    ? recent.events
        .map(
          (e) =>
            `<tr><td class="mono">${esc(e.event_id.slice(0, 8))}…</td><td>${badge("info", e.event_type)}</td><td class="mono">${esc(e.occurred_at)}</td><td>${esc(JSON.stringify(e.payload).slice(0, 90))}…</td></tr>`
        )
        .join("")
    : "";

  view.innerHTML = `
    <section><h3 class="section-title">Event log</h3>
      ${stats.exists
        ? `<div class="cards">
             <div class="card"><h4>Total events</h4><div class="value">${stats.total}</div></div>
             ${Object.entries(stats.by_type)
               .map(([type, count]) => `<div class="card"><h4>${esc(type)}</h4><div class="value">${count}</div></div>`)
               .join("")}
           </div>
           <table class="data">
             <thead><tr><th>ID</th><th>Type</th><th>Time (UTC)</th><th>Payload</th></tr></thead>
             <tbody>${eventRows}</tbody>
           </table>`
        : `<span class="empty">${esc(stats.message ?? "No event log found.")} Run <code>.venv/bin/python tools/seed_console_data.py</code>.</span>`}
    </section>

    <section><h3 class="section-title">SHACL validation gate (seed data)</h3>
      <div class="panel">
        ${validation.conforms ? badge("ok", `CONFORMS · 0 violations`) : badge("error", `${validation.violation_count} VIOLATIONS`)}
        ${validation.violations
          .map(
            (v) =>
              `<div class="prop" style="margin-top:6px"><span class="mono">${esc(v.focus_node)}</span> — <code>${esc(v.path)}</code>: ${esc(v.message)}</div>`
          )
          .join("")}
      </div>
    </section>

    <section><h3 class="section-title">Competency queries (${cq.total})</h3>
      ${
        cq.failed === 0
          ? badge("ok", `ALL ${cq.total} PASS`)
          : badge("error", `${cq.failed} FAILED: ${cq.failed_ids.join(", ")}`)
      }
      <table class="data" style="margin-top:10px">
        <thead><tr><th>ID</th><th>Group</th><th>Query</th><th>Status</th></tr></thead>
        <tbody>${"" /* filled below */}</tbody>
      </table>
    </section>`;

  // fetch individual spec details for the table rows
  const rowsHtml = [];
  for (const query of cq.queries) {
    rowsHtml.push(
      `<tr><td class="mono">${esc(query.id)}</td><td>${esc(query.group)}</td><td class="mono">${esc(query.query_file)}</td><td>${query.passed ? badge("ok", "PASS") : badge("error", `FAIL: ${esc(query.error ?? "")}`)}</td></tr>`
    );
  }
  view.querySelector("tbody:last-of-type")?.replaceChildren?.(); // no-op guard
  const tables = view.querySelectorAll("table.data");
  tables[tables.length - 1].querySelector("tbody").innerHTML = rowsHtml.join("");
}
