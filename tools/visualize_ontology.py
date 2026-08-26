"""Ontology visualization generator.

Reads every ontology module under ``ontology/`` and emits:

* ``ontology-model.json``  - machine-readable model (classes + properties)
* ``ontology.html``        - self-contained interactive D3 collapsible tree
* ``ontology.mmd``         - Mermaid diagram for docs/GitHub embedding

The HTML references D3 from a CDN, so it stays a single small artifact;
open it in any browser. Regenerate after ontology changes:

    make visualize-ontology
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

from ontology_utils import (
    attach_properties,
    describe_classes,
    describe_properties,
    dump_json,
    find_module_files,
    local_name,
)
from rdflib import Graph

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_model(module_paths: list[Path]) -> dict:
    """Load all modules into one graph and extract the full class/property model."""
    graph = Graph()
    for path in module_paths:
        graph.parse(path.as_posix(), format="turtle")

    classes = describe_classes(graph)
    properties = describe_properties(graph)
    attach_properties(classes, properties)

    roots = [iri for iri, desc in classes.items() if not desc["parents"]]
    return {
        "modules": [p.name for p in module_paths],
        "roots": sorted(roots),
        "classes": dict(sorted(classes.items())),
        "properties": properties,
    }


def build_tree(model: dict) -> list[dict]:
    """Build nested tree nodes rooted at top-level classes.

    A class is a root when it has no parents or all its parents live outside
    the loaded modules (dangling external references).
    """
    classes = model["classes"]
    children_of: dict[str, list[str]] = {}
    for iri, desc in classes.items():
        for parent in desc["parents"]:
            children_of.setdefault(parent, []).append(iri)

    roots = [
        iri
        for iri in classes
        if not desc_parents(classes, iri)
        or not all(parent in classes for parent in classes[iri]["parents"])
    ]

    def node(iri: str) -> dict:
        return {
            "name": local_name(iri),
            "iri": iri,
            "label": classes[iri]["label"],
            "children": [node(child) for child in sorted(children_of.get(iri, []))],
        }

    return [node(iri) for iri in sorted(roots)]


def desc_parents(classes: dict[str, dict], iri: str) -> list[str]:
    """Return direct parents of a class, tolerating unknown IRIs."""
    return classes.get(iri, {}).get("parents", [])


def render_mermaid(model: dict) -> str:
    """Render the model as a Mermaid flowchart (class edges solid, props dashed)."""
    classes = model["classes"]
    lines = ["graph TD"]
    for iri in sorted(classes):
        lines.append(f"  {html.escape(local_name(iri))}[{local_name(iri)}]")
    for iri, desc in sorted(classes.items()):
        child = local_name(iri)
        for parent in desc["parents"]:
            if parent in classes:
                lines.append(f"  {local_name(parent)} -->|subClassOf| {child}")
    for prop in model["properties"]:
        domain = prop["domain"]
        rng = prop["range"]
        if domain in classes:
            range_name = (
                local_name(rng)
                if rng in classes
                else f"{prop['kind']}: {local_name(rng) or 'literal'}"
            )
            arrow = "---" if prop["kind"] == "object" else "-.-"
            lines.append(
                f"  {local_name(domain)} {arrow}|{local_name(prop['iri'])}| "
                f"{html.escape(range_name)}"
            )
    return "\n".join(lines) + "\n"


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ontology Visualization</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; display: flex; }}
  #tree {{ flex: 1; overflow: auto; }}
  #panel {{ width: 340px; padding: 16px; border-left: 1px solid #ccc;
            max-height: 100vh; overflow: auto; box-sizing: border-box; }}
  .node circle {{ fill: #4a7fb5; stroke: #fff; stroke-width: 1.5px; }}
  .node text {{ font-size: 12px; }}
  .link {{ fill: none; stroke: #999; stroke-opacity: 0.5; stroke-width: 1.2px; }}
  .prop {{ margin-bottom: 6px; font-size: 13px; }}
  .prop code {{ background: #f0f3f6; padding: 1px 5px; border-radius: 3px; }}
  h3 {{ border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
</style>
</head>
<body>
<div id="tree"></div>
<div id="panel"><h3>Class details</h3><div id="details"><em>Select a node…</em></div></div>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const TREE = __TREE_DATA__;
const MODEL = __MODEL_DATA__;
const width = window.innerWidth - 360, height = window.innerHeight;

const svg = d3.select("#tree").append("svg")
  .attr("width", width).attr("height", height)
  .call(d3.zoom().on("zoom", (e) => g.attr("transform", e.transform)));
const g = svg.append("g");

const root = d3.hierarchy(TREE);
root.x0 = 0; root.y0 = 0;
const tree = d3.tree().nodeSize([22, 190]);

function update(src) {
  const nodes = root.descendants(), links = root.links();
  tree(root);
  nodes.forEach((d) => { d.y = d.depth * 190; });

  const node = g.selectAll("g.node").data(nodes, (d) => d.id || (d.id = ++i));
  const enter = node.enter().append("g").attr("class", "node")
    .attr("transform", `translate(${src.y0},${src.x0})`).on("click", click);
  enter.append("circle").attr("r", 5);
  enter.append("text").attr("dy", "0.32em").attr("x", (d) => d.children ? -9 : 9)
    .attr("text-anchor", (d) => d.children ? "end" : "start").text((d) => d.data.name);
  const u = node.merge(enter).transition().duration(250)
    .attr("transform", (d) => `translate(${d.y},${d.x})`);
  u.select("circle").attr("r", 5);
  const link = g.selectAll("path.link").data(links, (d) => d.target.id);
  link.enter().insert("path", "g").attr("class", "link")
    .merge(link).transition().duration(250)
    .attr("d", (d) => `M${d.source.y},${d.source.x}C${(d.source.y+d.target.y)/2},${d.source.x} ${(d.source.y+d.target.y)/2},${d.target.x} ${d.target.y},${d.target.x}`);
  node.exit().transition().duration(250).attr("transform", `translate(${src.y},${src.x})`).remove();
}

let i = 0;
function click(event, d) {
  if (d.children) { d._children = d.children; d.children = null; }
  else { d.children = d._children; d._children = null; }
  showDetails(d.data); update(d);
}
function showDetails(data) {
  const cls = MODEL.classes[data.iri] || {properties: [], comment: ""};
  let out = `<h3>${data.name}</h3><p>${cls.comment || ""}</p>`;
  for (const p of cls.properties || []) {
    out += `<div class="prop"><code>${p.name}</code> -> ${p.kind} : ${p.range.split("#").pop()}</div>`;
  }
  document.getElementById("details").innerHTML = out;
}
root.descendants().forEach((d, depth) => { d.id = ++i; if (depth >= 2) { d._children = d.children; d.children = null; } });
update(root);
</script>
</body>
</html>
"""


def render_html(model: dict, tree: list[dict]) -> str:
    """Render the self-contained interactive HTML artifact."""
    return _HTML_TEMPLATE.replace("__TREE_DATA__", dump_json(tree)).replace(
        "__MODEL_DATA__", dump_json(model)
    )


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="build", help="Output directory (default: build/)")
    args = parser.parse_args()

    module_paths = find_module_files()
    if not module_paths:
        print("No ontology modules found under ontology/.")
        return 1

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    model = build_model(module_paths)
    tree = build_tree(model)

    (out_dir / "ontology-model.json").write_text(dump_json(model), encoding="utf-8")
    (out_dir / "ontology.mmd").write_text(render_mermaid(model), encoding="utf-8")
    html_path = out_dir / "ontology.html"
    html_path.write_text(render_html(model, tree), encoding="utf-8")

    print(f"Modules: {len(model['modules'])}")
    print(f"Classes: {len(model['classes'])}, properties: {len(model['properties'])}")
    print(
        f"Wrote: {out_dir / 'ontology.html'}, {out_dir / 'ontology.mmd'}, "
        f"{out_dir / 'ontology-model.json'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
