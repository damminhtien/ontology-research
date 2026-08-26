/** Explorer view: D3 collapsible hierarchy + term detail panel. */
import { get, esc, badge } from "../api.js";

let selectedName = null;

export async function renderExplorer(view) {
  const [treeData, model] = await Promise.all([
    get("/api/ontology/tree"),
    get("/api/ontology/model"),
  ]);

  view.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <input data-role="search" type="search" placeholder='Search classes / properties ("operates")…' style="width:340px">
      <span id="search-results"></span>
    </div>
    <div class="grid-2">
      <div class="panel"><div id="tree"></div></div>
      <div class="panel"><h3 class="section-title">Term detail</h3><div id="details"><em>Select a node…</em></div></div>
    </div>`;

  const searchBox = view.querySelector('input[data-role="search"]');
  searchBox.addEventListener("input", () => {
    const q = searchBox.value.trim().toLowerCase();
    const hits = !q
      ? []
      : model.properties.filter((p) => p.iri.toLowerCase().includes(q)).map((p) => p.iri.split("#").pop())
          .concat(model.classes ? [] : []);
    const classHits = Object.keys(model.classes).filter(
      (iri) => iri.toLowerCase().split("#").pop().includes(q)
    );
    const all = [...new Set([...hits, ...classHits.map((i) => i.split("#").pop())])];
    document.getElementById("search-results").innerHTML = all
      .slice(0, 8)
      .map((n) => `<button data-term="${esc(n)}">${esc(n)}</button>`)
      .join(" ");
    document.querySelectorAll("#search-results button").forEach((btn) =>
      btn.addEventListener("click", () => showTerm(btn.dataset.term))
    );
  });

  const width = view.clientWidth - 40;
  const height = window.innerHeight - 120;
  const svg = d3
    .select(view.querySelector("#tree"))
    .append("svg")
    .attr("width", width)
    .attr("height", height)
    .call(d3.zoom().on("zoom", (event) => g.attr("transform", event.transform)));
  const g = svg.append("g");

  const root = d3.hierarchy(treeData.tree[0]);
  root.x0 = 0;
  root.y0 = 0;
  const treeLayout = d3.tree().nodeSize([24, 200]);

  function update(src) {
    treeLayout(root);
    root.descendants().forEach((d) => {
      d.y = d.depth * 200;
    });

    const nodes = g.selectAll("g.node").data(root.descendants(), (d) => d.id ?? (d.id = ++counter));
    const enterNodes = nodes
      .enter()
      .append("g")
      .attr("class", "node")
      .attr("transform", `translate(${src.y0},${src.x0})`)
      .on("click", (event, d) => {
        if (d.children) {
          d._children = d.children;
          d.children = null;
        } else {
          d.children = d._children;
          d._children = null;
        }
        showTerm(d.data.name);
        update(d);
      });
    enterNodes.append("circle").attr("r", 5);
    enterNodes.append("text").attr("dy", "0.32em").attr("x", (d) => (d.children || d._children ? -9 : 9))
      .attr("text-anchor", (d) => (d.children || d._children ? "end" : "start"))
      .text((d) => d.data.name);

    nodes.merge(enterNodes).transition().duration(220).attr("transform", (d) => `translate(${d.y},${d.x})`);

    const links = g.selectAll("path.link").data(root.links(), (d) => d.target.id);
    links
      .enter()
      .insert("path", "g")
      .attr("class", "link")
      .merge(links)
      .transition()
      .duration(220)
      .attr("d", (d) =>
        `M${d.source.y},${d.source.x}C${(d.source.y + d.target.y) / 2},${d.source.x} ${(d.source.y + d.target.y) / 2},${d.target.x} ${d.target.y},${d.target.x}`
      );
    nodes.exit().remove();
    links.exit().remove();
  }

  let counter = 0;
  root.descendants().forEach((d) => {
    d.id = ++counter;
    if (d.depth >= 1) {
      d._children = d.children;
      d.children = null;
    }
  });

  async function showTerm(name) {
    selectedName = name;
    try {
      const detail = await get(`/api/ontology/terms/${encodeURIComponent(name)}`);
      if (selectedName !== name) return;
      let html = `<h3 class="section-title">${esc(detail.name)} <code>${esc(detail.kind)}</code></h3>`;
      if (detail.comment) html += `<p style="font-size:13px">${esc(detail.comment)}</p>`;
      if (detail.parents?.length) {
        html += `<p class="mono">parents: ${detail.parents.map(esc).join(", ")}</p>`;
      }
      if (detail.properties?.length) {
        html += "<h4 class='section-title'>Properties</h4>";
        for (const prop of detail.properties) {
          html += `<div class="prop"><code>${esc(prop.name)}</code> → ${esc(prop.kind)} : ${esc(prop.range.split("#").pop() || "-")}</div>`;
        }
      }
      const br = detail.blast_radius;
      const score = Object.values(br).reduce((sum, files) => sum + files.length, 0);
      html += `<p>${badge(score > 0 ? "warn" : "ok", `blast radius BR=${score}`)}</p>`;
      document.getElementById("details").innerHTML = html;
    } catch (err) {
      document.getElementById("details").innerHTML = `<p class="error-text">${esc(err.message)}</p>`;
    }
  }

  // expand the first level and show the root by default
  root.children?.forEach((child) => {
    child.children = child._children;
    child._children = null;
  });
  update(root);
}
