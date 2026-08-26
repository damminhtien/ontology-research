/** Impact view: blast-radius lookup per term. */
import { get, esc, badge } from "../api.js";

export async function renderImpact(view) {
  const model = await get("/api/ontology/model");
  const names = [
    ...Object.keys(model.classes).map((iri) => iri.split("#").pop()),
    ...model.properties.map((p) => p.iri.split("#").pop()),
  ].sort();

  view.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <input id="term-input" list="term-list" placeholder="Term (e.g. Platform)…" style="width:300px">
      <datalist id="term-list">${names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist>
      <button class="primary" id="go">Analyze</button>
    </div>
    <div id="result"><p class="empty">Enter a term to see which modules, queries and application files reference it.</p></div>`;

  const input = view.querySelector("#term-input");
  const run = async () => {
    const term = input.value.trim();
    if (!term) return;
    el = view.querySelector("#result");
    el.innerHTML = '<p class="view-sub">analyzing…</p>';
    try {
      const data = await get(`/api/impact?term=${encodeURIComponent(term)}`);
      let html = `<div class="cards">
        <div class="card"><h4>Blast radius</h4><div class="value">${data.score}</div><div class="sub mono">${esc(data.term)}</div></div>`;
      for (const category of ["modules", "queries", "applications"]) {
        html += `<div class="card"><h4>${category}</h4><div class="value">${data[category].length}</div></div>`;
      }
      html += "</div>";
      for (const category of ["modules", "queries", "applications"]) {
        if (data[category].length) {
          html += `<h3 class="section-title">${category}</h3><ul class="timeline" style="list-style:none;padding:0">${data[category]
            .map((file) => `<li class="mono">${esc(file)}</li>`)
            .join("")}</ul>`;
        }
      }
      el.innerHTML = html;
    } catch (err) {
      el.innerHTML = `<p class="error-text">${esc(err.message)}</p>`;
    }
  };
  let el;
  view.querySelector("#go").addEventListener("click", run);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") run();
  });
}
