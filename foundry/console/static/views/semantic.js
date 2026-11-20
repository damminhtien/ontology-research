/** Natural-language interface over the profile's allowlisted CQ templates. */
import { esc, get, post, badge } from "../api.js";

const PROVENANCE_TOKEN_KEY = "foundry.provenance.token";

function cellValue(cell) {
  if (!cell) return "—";
  if (cell.kind === "iri") return `<code>${esc(cell.value)}</code>`;
  const language = cell.language ? ` @${esc(cell.language)}` : "";
  const datatype = cell.datatype ? ` <span class="muted mono">${esc(cell.datatype)}</span>` : "";
  return `${esc(cell.value)}${language}${datatype}`;
}

function renderResult(data) {
  if (data.status === "unsupported") {
    return `<div class="panel"><p>${badge("warn", "Unsupported query")}</p>
      <p>This profile only runs registered competency queries. Choose one of the examples below.</p></div>`;
  }
  if (data.status === "ambiguous") {
    return `<div class="panel"><p>${badge("warn", "Needs clarification")}</p>
      <p>Choose which question you meant:</p><ul>${data.candidates.map((item) => `<li>${esc(item.id)} · ${esc(item.label)}</li>`).join("")}</ul></div>`;
  }
  if (data.status === "needs_input") {
    return `<div class="panel"><p>${badge("warn", "Missing query details")}</p>
      <p>${esc(data.label)} needs: ${data.missing_slots.map(esc).join(", ")}.</p>
      <p>Include the canonical entity or assertion IRI${data.missing_slots.includes("timezone_aware_instant") ? " and an ISO date or timezone-aware timestamp" : ""}.</p></div>`;
  }

  const rows = data.rows.map((row) => `<tr>${row.map((cell) => `<td>${cellValue(cell)}</td>`).join("")}</tr>`).join("");
  const header = data.variables.map((name) => `<th>${esc(name)}</th>`).join("");
  return `<section class="panel">
    <div class="row">${badge("ok", `${data.template_id} · ${data.label}`)}${badge("info", `planner: ${data.planner}`)}</div>
    <p class="view-sub">${data.rows.length} result(s) · ${data.elapsed_ms} ms${data.has_more ? " · more results available" : ""}</p>
    ${data.redacted_source_count ? `<p class="view-sub">${esc(data.redacted_source_count)} source(s) hidden by provenance scope.</p>` : ""}
    ${data.rows.length ? `<div class="table-wrap"><table class="data"><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="empty">No rows matched the selected template.</p>'}
    <details><summary>Validated SPARQL template and typed bindings</summary>
      <pre>${esc(data.sparql)}</pre><pre>${esc(JSON.stringify(data.bindings, null, 2))}</pre></details>
  </section>`;
}

export async function renderSemanticQuery(view) {
  view.innerHTML = `
    <section class="panel semantic-query">
      <div><h3>Ask the Vietnam-SCS profile</h3>
        <p class="view-sub">Questions map to registered competency queries. The request cannot submit SPARQL.</p></div>
      <form id="semantic-query-form">
        <label for="semantic-question">Question</label>
        <textarea id="semantic-question" name="question" rows="3" maxlength="2000" required placeholder="Vị trí của https://data.example/entity/patrol-03 tại 2026-08-05T00:00:00Z ở đâu?"></textarea>
        <label>Provenance token (optional)
          <input name="provenance_token" type="password" autocomplete="off" placeholder="Allows source-scoped evidence results">
          <small>Kept in this browser session; it is sent as a bearer token, never in the query body.</small>
        </label>
        <div class="row"><label>Rows <input name="limit" type="number" min="1" max="100" value="25"></label><button class="primary">Run safe query</button></div>
      </form>
      <div id="semantic-examples" class="semantic-examples"></div>
    </section>
    <section id="semantic-result"><p class="empty">Choose an example or enter a question with canonical IDs.</p></section>`;

  const form = view.querySelector("#semantic-query-form");
  const result = view.querySelector("#semantic-result");
  const examples = view.querySelector("#semantic-examples");
  form.elements.provenance_token.value = sessionStorage.getItem(PROVENANCE_TOKEN_KEY) || "";
  form.elements.provenance_token.addEventListener("input", () => {
    const value = form.elements.provenance_token.value;
    if (value) sessionStorage.setItem(PROVENANCE_TOKEN_KEY, value);
    else sessionStorage.removeItem(PROVENANCE_TOKEN_KEY);
  });
  try {
    const catalog = await get("/api/v1/semantic/templates");
    examples.innerHTML = `<p class="view-sub">Profile ${esc(catalog.profile_id)}</p>
      <div class="semantic-example-list">${catalog.templates.map((template, index) => `
        <button type="button" data-template-index="${index}"><strong>${esc(template.id)} · ${esc(template.label)}</strong>
          <span>${esc(template.examples[0])}</span></button>`).join("")}</div>`;
    examples.querySelectorAll("[data-template-index]").forEach((button) => {
      button.addEventListener("click", () => {
        const template = catalog.templates[Number(button.dataset.templateIndex)];
        form.elements.question.value = template.examples[0];
      });
    });
  } catch (error) {
    examples.innerHTML = `<p class="error-text">Could not load query catalog: ${esc(error.message)}</p>`;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      question: form.elements.question.value,
      limit: Number(form.elements.limit.value),
    };
    result.innerHTML = '<p class="view-sub">Planning and running a registered query…</p>';
    try {
      const data = await post(
        "/api/v1/semantic/query",
        payload,
        form.elements.provenance_token.value,
      );
      result.innerHTML = renderResult(data);
    } catch (error) {
      result.innerHTML = `<div class="panel"><p class="error-text">${esc(error.message)}</p>
        <p class="view-sub">Configure <code>FOUNDRY_SEMANTIC_QUERY_DATA</code> with an RDF file or directory to enable this optional query path.</p></div>`;
    }
  });
}
