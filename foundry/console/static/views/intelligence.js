/** Analyst-facing entity profile, temporal relations, provenance and identity review. */
import { esc, get, post, badge } from "../api.js";

const PROVENANCE_TOKEN_KEY = "foundry.provenance.token";
const PAGE_SIZE = 25;

function pageNote(page) {
  if (!page) return "";
  const shown = Math.min(page.offset + PAGE_SIZE, page.total);
  return `<p class="view-sub">${shown} of ${page.total}${page.has_more ? " · more results available" : ""}${page.truncated ? " · scan reached its safety limit" : ""}</p>`;
}

function valueLabel(object) {
  if (!object) return "—";
  if (object.kind === "literal") return object.value ?? "";
  return object.value ?? "";
}

function localDateTimeValue(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function renderEntityResults(container, entities) {
  if (!entities.length) {
    container.innerHTML = '<p class="empty">No matching entities. Search by a recorded name, alias, or canonical ID.</p>';
    return;
  }
  container.innerHTML = `
    <div class="intelligence-results">
      ${entities.map((entity, index) => `
        <button class="intelligence-result" type="button" data-result-index="${index}">
          <strong>${esc(entity.name)}</strong>
          <span>${esc(entity.entity_type)} · ${esc(entity.entity_id)}</span>
          ${entity.identity_status === "merged" ? badge("warn", "merged") : ""}
        </button>`).join("")}
    </div>`;
}

function renderIdentifiers(data) {
  const aliases = data.aliases.length
    ? data.aliases.map((alias) => `<span class="intelligence-chip">${esc(alias)}</span>`).join("")
    : '<span class="empty">No aliases recorded.</span>';
  const externalIds = data.external_ids.length
    ? `<table class="data"><thead><tr><th>Namespace</th><th>Identifier</th><th>Policy</th></tr></thead><tbody>
      ${data.external_ids.flatMap((item) => item.values.map((value) => `
        <tr><td>${esc(item.namespace)}</td><td class="mono">${esc(value)}</td>
        <td>${item.unique_authoritative ? badge("ok", "authoritative unique") : badge("warn", "candidate only")}</td></tr>`)).join("")}
      </tbody></table>`
    : '<p class="empty">No external identifiers recorded.</p>';
  return `<div class="intelligence-identifiers"><div><h4>Aliases</h4><div class="intelligence-chips">${aliases}</div></div><div><h4>External IDs</h4>${externalIds}</div></div>`;
}

function renderRelations(data) {
  if (data.depth > 1) {
    if (!data.paths.length) return '<p class="empty">No relationships found for this time.</p>';
    return `<ol class="timeline">${data.paths.map((path) => `
      <li><strong>${path.hops ?? path.assertions.length} hop path</strong>
        <div class="mono">${path.nodes.map(esc).join(" → ")}</div>
        <div>${path.assertions.map((assertion) => esc(assertion.predicate_iri)).join(" · ")}</div>
      </li>`).join("")}</ol>${pageNote(data)}`;
  }
  if (!data.relations.length) return '<p class="empty">No active relationships found for this time.</p>';
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Relationship</th><th>Target</th><th>Valid from</th><th>Evidence</th></tr></thead><tbody>
    ${data.relations.map((relation) => `<tr>
      <td class="mono">${esc(relation.predicate_iri)}</td>
      <td>${relation.target ? `${esc(relation.target.name)}<div class="mono">${esc(relation.target.entity_id)}</div>` : `<span class="mono">${esc(valueLabel(relation.object))}</span>`}</td>
      <td>${esc(relation.valid_from)}</td>
      <td><button type="button" class="link-button" data-evidence-id="${esc(relation.assertion_id)}">Open evidence</button></td>
    </tr>`).join("")}
    </tbody></table></div>${pageNote(data)}`;
}

function renderTimeline(data) {
  if (!data.assertions.length) return '<p class="empty">No assertion history recorded.</p>';
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Status</th><th>Valid time</th><th>Recorded</th><th>Claim</th><th>Evidence</th></tr></thead><tbody>
    ${data.assertions.map((assertion) => `<tr>
      <td>${badge(assertion.status === "asserted" ? "ok" : "warn", assertion.status)}</td>
      <td>${esc(assertion.valid_from)}${assertion.valid_until ? `<div>to ${esc(assertion.valid_until)}</div>` : "<div>open end</div>"}</td>
      <td>${esc(assertion.recorded_at || "unknown")}</td>
      <td><div class="mono">${esc(assertion.predicate_iri)}</div>${esc(valueLabel(assertion.object))}</td>
      <td><button type="button" class="link-button" data-evidence-id="${esc(assertion.assertion_id)}">Open</button></td>
    </tr>`).join("")}
    </tbody></table></div>${pageNote(data)}`;
}

function renderConflicts(data) {
  if (!data.conflicts.length) return '<p class="empty">No active conflicting assertions found.</p>';
  return `${data.conflicts.map((conflict) => `
    <article class="conflict-card">
      <div class="row"><strong class="mono">${esc(conflict.predicate_iri)}</strong>${badge("warn", `${conflict.review_status} (derived)`)}</div>
      ${conflict.alternatives.map((alternative) => `
        <div class="conflict-alternative"><strong>${esc(valueLabel(alternative.object))}</strong>
          <ul>${alternative.assertions.map((assertion) => `<li>
            ${esc(assertion.assertion_id)} · ${esc(assertion.recorded_at || "recorded time unknown")}
            · <button type="button" class="link-button" data-evidence-id="${esc(assertion.assertion_id)}">evidence</button>
          </li>`).join("")}</ul>
        </div>`).join("")}
    </article>`).join("")}${pageNote(data)}`;
}

function renderEvidence(data) {
  const items = data.evidence.length
    ? data.evidence.map((item) => `
      <li><strong>${esc(item.source_id || "Source hidden")}</strong>
        ${item.locator ? `<div>Locator: ${esc(item.locator)}</div>` : ""}
        ${item.span ? `<div>Text span: <code>${esc(JSON.stringify(item.span))}</code></div>` : ""}
        ${item.span?.exact_text ? `<blockquote>${esc(item.span.exact_text)}</blockquote>` : ""}
      </li>`).join("")
    : '<li class="empty">No evidence is visible under the current provenance scope.</li>';
  return `<div class="panel evidence-detail">
    <div class="row"><strong>${esc(data.assertion_id)}</strong>${badge(data.redacted_evidence_count ? "warn" : "info", data.redacted_evidence_count ? `${data.redacted_evidence_count} evidence item(s) hidden` : "evidence")}</div>
    <p><span class="mono">${esc(data.predicate_iri)}</span> · ${esc(valueLabel(data.object))}</p>
    <p>Valid: ${esc(data.valid_from)}${data.valid_until ? ` to ${esc(data.valid_until)}` : " onward"} · Recorded: ${esc(data.recorded_at || "unknown")}</p>
    <ul>${items}</ul>
  </div>`;
}

export async function renderIntelligence(view, params) {
  view.innerHTML = `
    <section class="intelligence-search panel">
      <div><h3>Entity Intelligence Explorer</h3><p class="view-sub">Search canonical entities, then inspect relationships, history, evidence, conflicts, and identity decisions.</p></div>
      <form id="entity-search" class="row"><input name="q" data-role="search" type="search" minlength="1" maxlength="500" placeholder="Name, alias, or canonical ID" required><button class="primary">Search</button></form>
      <div id="entity-results"></div>
    </section>
    <section id="entity-workspace"><p class="empty">Choose an entity to open its 360° profile.</p></section>`;

  const resultsEl = view.querySelector("#entity-results");
  const workspace = view.querySelector("#entity-workspace");
  const searchForm = view.querySelector("#entity-search");
  const searchInput = searchForm.elements.q;

  async function search(query) {
    if (!query.trim()) return;
    searchInput.value = query;
    resultsEl.innerHTML = '<p class="view-sub">Searching…</p>';
    try {
      const result = await get(`/api/v1/entities?q=${encodeURIComponent(query)}&limit=${PAGE_SIZE}`);
      renderEntityResults(resultsEl, result.entities);
      resultsEl.querySelectorAll("[data-result-index]").forEach((button) => {
        button.addEventListener("click", () => {
          const entity = result.entities[Number(button.dataset.resultIndex)];
          if (entity) openEntity(entity.entity_id);
        }, { once: true });
      });
    } catch (error) {
      resultsEl.innerHTML = `<p class="error-text">${esc(error.message)}</p>`;
    }
  }

  async function openEvidence(assertionId) {
    const token = sessionStorage.getItem(PROVENANCE_TOKEN_KEY) || "";
    const panel = view.querySelector("#selected-evidence");
    if (!panel) return;
    panel.innerHTML = '<p class="view-sub">Loading evidence…</p>';
    try {
      const data = await get(`/api/v1/assertions/${encodeURIComponent(assertionId)}/evidence`, token);
      panel.innerHTML = renderEvidence(data);
    } catch (error) {
      panel.innerHTML = `<p class="error-text">${esc(error.message)}</p>`;
    }
  }

  async function evaluateCandidate(form) {
    const values = Object.fromEntries(new FormData(form).entries());
    const token = sessionStorage.getItem(PROVENANCE_TOKEN_KEY) || "";
    const resultEl = form.parentElement.querySelector(".identity-result");
    resultEl.innerHTML = '<p class="view-sub">Evaluating…</p>';
    try {
      const payload = Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
      const result = await post("/api/v1/identity/candidates", payload, token);
      resultEl.innerHTML = `
        <div class="row">${badge(result.decision === "MATCH" ? "ok" : result.decision === "CONFLICT" ? "error" : "warn", result.decision)}
          ${result.same_entity_evidence_required ? badge("warn", "human review required") : ""}</div>
        <p>${esc(result.reason || "No additional explanation")}</p>
        ${result.canonical_id ? `<p>Canonical ID: <code>${esc(result.canonical_id)}</code></p>` : ""}
        ${result.candidates?.length ? `<h4>Candidate entities (${result.candidate_total})</h4><ul>${result.candidates.map((item) => `<li>${esc(item.name || item.entity_id)} <code>${esc(item.entity_id)}</code></li>`).join("")}</ul>${result.candidates_truncated ? '<p class="view-sub">Showing the first page; refine the incoming record to narrow candidates.</p>' : ""}` : ""}
        ${result.cannot_link_evidence ? `<h4>Cannot-link evidence</h4><pre>${esc(JSON.stringify(result.cannot_link_evidence, null, 2))}</pre>` : ""}
        ${result.cannot_link_evidence_redacted ? '<p class="view-sub">Cannot-link evidence is hidden by its source scope. Add an authorized provenance token to inspect it.</p>' : ""}
        <p class="view-sub">${esc(result.method)}${result.candidate_method ? ` · ${esc(result.candidate_method)}` : ""} · candidate score ${esc(result.candidate_score ?? "—")} (not probability) · no state written</p>`;
    } catch (error) {
      resultEl.innerHTML = `<p class="error-text">${esc(error.message)}</p>`;
    }
  }

  async function openEntity(entityId) {
    workspace.innerHTML = '<p class="view-sub">Loading entity profile…</p>';
    const atControl = `<input id="relations-at" type="datetime-local" value="${localDateTimeValue(new Date())}">`;
    try {
      const [profile, identifiers, relations, timeline, conflicts] = await Promise.all([
        get(`/api/v1/entities/${encodeURIComponent(entityId)}`),
        get(`/api/v1/entities/${encodeURIComponent(entityId)}/identifiers`),
        get(`/api/v1/entities/${encodeURIComponent(entityId)}/relations?limit=${PAGE_SIZE}`),
        get(`/api/v1/entities/${encodeURIComponent(entityId)}/timeline?limit=${PAGE_SIZE}`),
        get(`/api/v1/entities/${encodeURIComponent(entityId)}/conflicts?limit=${PAGE_SIZE}`),
      ]);
      const entity = profile.entity;
      workspace.innerHTML = `
        <header class="entity-hero panel">
          <div><p class="section-title">${esc(entity.entity_type)} · Entity 360°</p><h2>${esc(entity.name)}</h2>
            <code>${esc(entity.entity_id)}</code><div class="row">${badge(entity.identity_status === "merged" ? "warn" : "ok", entity.identity_status)}${entity.merged_into ? `<span>Redirected to <code>${esc(entity.merged_into)}</code></span>` : ""}</div>
            <p class="view-sub">First seen ${esc(entity.first_seen)} · Last update ${esc(entity.last_event_time)}</p></div>
          <label class="provenance-control">Provenance access token (optional)
            <input id="provenance-token" type="password" autocomplete="off" placeholder="Scoped evidence token">
            <small>Token stays in this browser session and is used for source-linked evidence.</small>
          </label>
        </header>
        <section class="panel"><h3 class="section-title">Canonical identity & identifiers</h3>${renderIdentifiers(identifiers)}</section>
        <section class="panel"><div class="row section-heading"><h3 class="section-title">Relationships</h3><label>Valid at ${atControl}</label></div><div id="relations-panel">${renderRelations(relations)}</div></section>
        <section class="panel"><h3 class="section-title">Timeline</h3><p class="view-sub">Recorded time shows when the system learned a claim; valid time shows when it applies in the world.</p>${renderTimeline(timeline)}</section>
        <section class="panel"><h3 class="section-title">Conflicts</h3>${renderConflicts(conflicts)}</section>
        <section class="panel"><h3 class="section-title">Evidence</h3><div id="selected-evidence"><p class="empty">Open evidence from a relationship, timeline item, or conflict.</p></div></section>
        <section class="panel"><h3 class="section-title">Identity decision review</h3>
          <div class="identity-forms">
            <form class="candidate-form"><h4>Review incoming source record</h4>
              <label>Name <input name="name" maxlength="500" placeholder="Example: Vessel Alpha"></label>
              <label>Entity type <input name="entity_type" maxlength="100" value="${esc(entity.entity_type)}"></label>
              <label>Identifier namespace <input name="external_namespace" maxlength="200" placeholder="e.g. ais-mmsi"></label>
              <label>External ID <input name="external_id" maxlength="500"></label>
              <label>Source ID <input name="source_id" maxlength="200"></label>
              <label>Source record key <input name="source_record_id" maxlength="500"></label>
              <button class="primary">Evaluate candidate</button><div class="identity-result"></div>
            </form>
            <form class="pair-form"><h4>Inspect a possible merge</h4>
              <label>Entity A <input name="entity_id" value="${esc(entity.entity_id)}" required></label>
              <label>Entity B <input name="candidate_entity_id" placeholder="Canonical entity ID" required></label>
              <button class="primary">Review pair</button><div class="identity-result"></div>
              <p class="view-sub">Only an existing merge, a type/cannot-link conflict, or a human-review recommendation is returned. This check does not merge entities.</p>
            </form>
          </div>
        </section>`;

      const tokenInput = workspace.querySelector("#provenance-token");
      tokenInput.value = sessionStorage.getItem(PROVENANCE_TOKEN_KEY) || "";
      tokenInput.addEventListener("change", () => {
        if (tokenInput.value) sessionStorage.setItem(PROVENANCE_TOKEN_KEY, tokenInput.value);
        else sessionStorage.removeItem(PROVENANCE_TOKEN_KEY);
        workspace.querySelector("#selected-evidence").innerHTML =
          '<p class="empty">Token updated. Open an evidence item to retrieve its source details.</p>';
      });

      workspace.querySelectorAll("[data-evidence-id]").forEach((button) => {
        button.addEventListener("click", () => openEvidence(button.dataset.evidenceId));
      });
      workspace.querySelectorAll(".candidate-form, .pair-form").forEach((form) => {
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          evaluateCandidate(form);
        });
      });
      workspace.querySelector("#relations-at").addEventListener("change", async (event) => {
        if (!event.target.value) return;
        const instant = new Date(event.target.value).toISOString();
        const panel = workspace.querySelector("#relations-panel");
        panel.innerHTML = '<p class="view-sub">Loading relationships…</p>';
        try {
          const updated = await get(
            `/api/v1/entities/${encodeURIComponent(entityId)}/relations?at=${encodeURIComponent(instant)}&limit=${PAGE_SIZE}`,
          );
          panel.innerHTML = renderRelations(updated);
          panel.querySelectorAll("[data-evidence-id]").forEach((button) => {
            button.addEventListener("click", () => openEvidence(button.dataset.evidenceId));
          });
        } catch (error) {
          panel.innerHTML = `<p class="error-text">${esc(error.message)}</p>`;
        }
      });
    } catch (error) {
      workspace.innerHTML = `<p class="error-text">${esc(error.message)}</p>`;
    }
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    search(searchInput.value);
  });

  const initialQuery = params.get("q");
  if (initialQuery) await search(initialQuery);
}
