/** Dashboard view: aggregated health + modules + releases. */
import { get, esc, badge } from "../api.js";

export async function renderDashboard(view) {
  const data = await get("/api/overview");
  const worstStability = Math.min(
    ...(data.stability.modules.length
      ? data.stability.modules.map((m) => m.stability)
      : [1])
  );

  const cards = `
    <div class="cards">
      <div class="card"><h4>Version check</h4><div class="value">${badge(data.version_check.passed ? "ok" : "error", data.version_check.passed ? "PASS" : "FAIL")}</div></div>
      <div class="card"><h4>Dependency DAG</h4><div class="value">${badge(data.dag_check.passed ? "ok" : "error", data.dag_check.passed ? "OK" : "CYCLE")}</div></div>
      <div class="card"><h4>CQ regression</h4><div class="value">${data.cq.failed === 0 ? badge("ok", `${data.cq.total} PASS`) : badge("error", `${data.cq.failed} FAIL`)}</div><div class="sub">${esc((data.cq.failed_ids || []).join(", "))}</div></div>
      <div class="card"><h4>Worst stability</h4><div class="value mono">${worstStability}</div></div>
      <div class="card"><h4>Projection</h4><div class="value" id="proj-value">…</div><div class="sub" id="proj-sub">loading</div></div>
      <div class="card"><h4>Events logged</h4><div class="value">${data.events.exists ? data.events.total : "—"}</div><div class="sub">${data.events.exists ? esc(Object.keys(data.events.by_type).join(", ")) : "no log yet"}</div></div>
    </div>`;

  const moduleRows = data.modules
    .map(
      (m) => `<tr>
        <td class="mono">${m.name}</td>
        <td class="mono">${m.version}</td>
        <td>${m.classes} / ${m.properties}</td>
        <td>${badge(m.status, m.status)}</td>
        <td class="mono">${m.latest_version ?? "—"}</td>
        <td class="mono">${m.last_release ?? "—"}</td>
      </tr>`
    )
    .join("");

  const stabilityRows = data.stability.modules
    .map(
      (s) => `<tr>
        <td class="mono">${s.module}</td>
        <td><span class="meter ${s.ok ? "" : "low"}"><div style="width:${Math.round(s.stability * 100)}%"></div></span> ${s.stability}</td>
        <td>${s.breaking}/${s.releases}</td>
        <td>${badge(s.ok ? "ok" : "error", s.ok ? `>= ${s.threshold}` : `< ${s.threshold}`)}</td>
      </tr>`
    )
    .join("");

  view.innerHTML = `
    ${cards}
    <section><h3 class="section-title">Modules</h3>
      <table class="data">
        <thead><tr><th>Module</th><th>Version</th><th>Classes / Props</th><th>Version status</th><th>Last released</th><th>Last release date</th></tr></thead>
        <tbody>${moduleRows}</tbody>
      </table>
    </section>
    <section><h3 class="section-title">Semantic stability (roadmap KPI #4)</h3>
      <table class="data">
        <thead><tr><th>Module</th><th>Stability</th><th>Breaking / Releases</th><th>Threshold</th></tr></thead>
        <tbody>${stabilityRows}</tbody>
      </table>
    </section>`;

  // Populate the projection card asynchronously; it is allowed to fail soft.
  try {
    const proj = await get("/api/projection");
    const valueEl = document.getElementById("proj-value");
    const subEl = document.getElementById("proj-sub");
    if (!proj.exists) {
      valueEl.textContent = "—";
      subEl.textContent = "no event log yet";
    } else {
      const ok = proj.within_slo !== false;
      valueEl.innerHTML = badge(ok ? "ok" : "error", `${proj.entities}`);
      subEl.textContent = proj.within_slo === null
        ? `${proj.with_location} with location`
        : `lag ${proj.lag_seconds.toFixed(2)}s · ${proj.with_location} located`;
    }
  } catch (_err) {
    // leave placeholder; health badge will show API issues
  }
}
