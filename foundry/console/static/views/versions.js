/** Versions view: releases, pending release preview, diff between versions. */
import { get, esc, badge, severityBadge } from "../api.js";

export async function renderVersions(view) {
  const data = await get("/api/releases");
  const moduleNames = Object.keys(data.modules).map((iri) => {
    const parts = iri.split("/");
    return parts[parts.length - 1];
  });
  if (!moduleNames.length) {
    view.innerHTML = '<p class="empty">No releases recorded yet.</p>';
    return;
  }

  view.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      ${moduleNames.map((m, i) => `<button class="${i === 0 ? "primary" : ""}" data-module="${esc(m)}">${esc(m)}</button>`).join(" ")}
    </div>
    <div id="module-body"></div>`;

  const buttons = view.querySelectorAll("button[data-module]");
  buttons.forEach((btn) =>
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.remove("primary"));
      btn.classList.add("primary");
      renderModule(view.querySelector("#module-body"), btn.dataset.module);
    })
  );
  renderModule(view.querySelector("#module-body"), moduleNames[0]);
}

async function renderModule(container, moduleName) {
  container.innerHTML = `<p class="view-sub">loading ${esc(moduleName)}…</p>`;
  const [releasesData, pending] = await Promise.all([
    get("/api/releases"),
    get(`/api/releases/${encodeURIComponent(moduleName)}/pending`),
  ]);
  const entries = releasesData.modules.find(
    (_iri, idx) => false
  ) ?? Object.values(releasesData.modules).flat().filter((r) => r.module_iri.endsWith(`/${moduleName}`));

  const versions = entries.map((e) => e.version);

  let html = "";

  html += `<section><h3 class="section-title">Pending release</h3><div class="panel" id="pending">${renderPending(pending)}</div></section>`;

  html += `<section><h3 class="section-title">Diff between released versions</h3>
    <div class="row">
      <select id="from">${versions.map((v) => `<option>${v}</option>`).join("")}</select>
      <span>→</span>
      <select id="to">${versions.map((v) => `<option>${v}</option>`).join("")}</select>
      <button class="primary" id="diff-btn">Show diff</button>
    </div>
    <div id="diff-result" style="margin-top:10px"></div>
  </section>`;

  html += `<section><h3 class="section-title">Release history</h3><ul class="timeline">
    ${entries
      .map(
        (entry) => `<li>
          <strong class="mono">${esc(entry.version)}</strong> ${severityBadge(entry.severity)}
          <span class="when mono">${esc(entry.date)} · commit ${esc(entry.commit ?? "-")}</span>
          ${
            entry.changes.length
              ? `<ul style="margin:6px 0 0">${entry.changes
                  .map(
                    (c) => `<li>${severityBadge(c.severity)} <span class="mono">${esc(c.name)}</span>: ${esc(c.detail)}</li>`
                  )
                  .join("")}</ul>`
              : "<div class='empty'>Initial baseline release.</div>"
          }
          ${entry.migration ? `<div class="panel" style="margin-top:8px"><strong>Migration:</strong> ${esc(entry.migration)}</div>` : ""}
        </li>`
      )
      .join("")}
  </ul></section>`;

  container.innerHTML = html;

  container.querySelector("#diff-btn").addEventListener("click", () => {
    const fromV = container.querySelector("#from").value;
    const toV = container.querySelector("#to").value;
    showDiff(container.querySelector("#diff-result"), moduleName, fromV, toV);
  });
}

function renderPending(pending) {
  if (!pending.has_baseline) {
    return `<span class="empty">${esc(pending.message)}</span>`;
  }
  if (!pending.changes.length) {
    return `${badge("ok", "CLEAN")} <span>No changes since ${esc(pending.latest_version)} — nothing to release.</span>`;
  }
  const rows = pending.changes
    .map(
      (change) =>
        `<tr><td>${severityBadge(change.severity)}</td><td class="mono">${esc(change.name)}</td><td>${esc(change.detail)}</td></tr>`
    )
    .join("");
  const statusBadge = badge(
    pending.status === "error" ? "error" : pending.status === "warn" ? "warn" : "ok",
    `version check: ${pending.status}`
  );
  return `
    <p>${statusBadge} declared <code>${esc(pending.declared_version)}</code>,
       suggested <code>${esc(pending.suggested_version ?? "—")}</code>
       (${esc(pending.status_message)})</p>
    <table class="data"><thead><tr><th>Severity</th><th>Term</th><th>Change</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p style="margin-top:10px"><button onclick="navigator.clipboard.writeText('.venv/bin/python tools/manage_ontology.py release ${esc(pending.module)}')">Copy CLI command</button></p>`;
}

async function showDiff(el, moduleName, fromVersion, toVersion) {
  try {
    const data = await get(
      `/api/releases/${encodeURIComponent(moduleName)}/diff?from_version=${encodeURIComponent(fromVersion)}&to_version=${encodeURIComponent(toVersion)}`
    );
    el.innerHTML =
      data.suggested_bump === "NONE"
        ? '<span class="empty">No semantic differences between these versions.</span>'
        : `<p>Suggested bump: ${severityBadge(data.suggested_bump)}</p>
           <table class="data"><thead><tr><th>Severity</th><th>Term</th><th>Change</th></tr></thead>
           <tbody>${data.changes
             .map((c) => `<tr><td>${severityBadge(c.severity)}</td><td class="mono">${esc(c.name)}</td><td>${esc(c.detail)}</td></tr>`)
             .join("")}</tbody></table>`;
  } catch (err) {
    el.innerHTML = `<p class="error-text">${esc(err.message)}</p>`;
  }
}
