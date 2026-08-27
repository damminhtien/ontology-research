/** Projection view: read-model health + benchmark latency chart. */
import { get, esc, badge } from "../api.js";

function renderLatencyChart(container, data) {
  const margin = { top: 20, right: 30, bottom: 60, left: 50 };
  const width = 640 - margin.left - margin.right;
  const height = 320 - margin.top - margin.bottom;

  container.innerHTML = "";
  const svg = d3
    .select(container)
    .append("svg")
    .attr("width", width + margin.left + margin.right)
    .attr("height", height + margin.top + margin.bottom)
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  const x = d3.scaleBand().domain(data.map((d) => d.label)).range([0, width]).padding(0.25);
  const yMax = Math.max(d3.max(data, (d) => d.p95), d3.max(data, (d) => d.slo)) * 1.15;
  const y = d3.scaleLinear().domain([0, yMax]).nice().range([height, 0]);

  svg
    .append("g")
    .attr("transform", `translate(0,${height})`)
    .call(d3.axisBottom(x))
    .selectAll("text")
    .attr("transform", "rotate(-25)")
    .style("text-anchor", "end");

  svg.append("g").call(d3.axisLeft(y).ticks(5));
  svg.append("text").attr("x", -height / 2).attr("y", -margin.left + 12)
    .attr("transform", "rotate(-90)")
    .style("text-anchor", "middle")
    .style("font-size", "12px")
    .style("fill", "#66788c")
    .text("latency (ms)");

  // SLO reference lines
  svg
    .selectAll(".slo-line")
    .data(data)
    .enter()
    .append("line")
    .attr("class", "slo-line")
    .attr("x1", (d) => x(d.label))
    .attr("x2", (d) => x(d.label) + x.bandwidth())
    .attr("y1", (d) => y(d.slo))
    .attr("y2", (d) => y(d.slo))
    .attr("stroke", "#c62828")
    .attr("stroke-width", 2)
    .attr("stroke-dasharray", "4 2");

  svg
    .selectAll(".bar")
    .data(data)
    .enter()
    .append("rect")
    .attr("class", "bar")
    .attr("x", (d) => x(d.label))
    .attr("y", (d) => y(d.p95))
    .attr("width", x.bandwidth())
    .attr("height", (d) => height - y(d.p95))
    .attr("fill", (d) => (d.p95 <= d.slo ? "#1a7f37" : "#c62828"));

  svg
    .selectAll(".label")
    .data(data)
    .enter()
    .append("text")
    .attr("x", (d) => x(d.label) + x.bandwidth() / 2)
    .attr("y", (d) => y(d.p95) - 5)
    .attr("text-anchor", "middle")
    .style("font-size", "11px")
    .style("fill", "#1c2733")
    .text((d) => `${d.p95.toFixed(3)} ms`);
}

export async function renderProjection(view) {
  const [stats, benchmark] = await Promise.allSettled([
    get("/api/projection"),
    get("/api/projection/benchmark").catch(() => null),
  ]);

  const proj = stats.status === "fulfilled" ? stats.value : null;
  const bench = benchmark.status === "fulfilled" ? benchmark.value : null;

  const statsHtml = proj
    ? `
      <div class="cards">
        <div class="card"><h4>Entities</h4><div class="value">${proj.entities}</div></div>
        <div class="card"><h4>With location</h4><div class="value">${proj.with_location}</div></div>
        <div class="card"><h4>Distinct locations</h4><div class="value">${proj.locations}</div></div>
        <div class="card"><h4>Projection lag</h4><div class="value">${proj.lag_seconds === null ? "—" : `${proj.lag_seconds.toFixed(2)}s`}</div><div class="sub">SLO &lt; ${proj.slo_lag_seconds}s · ${proj.within_slo === false ? badge("error", "OVER") : badge("ok", "OK")}</div></div>
      </div>
      <p class="view-sub">Last event: ${esc(proj.last_event_time ?? "none")}</p>`
    : `<span class="empty">Projection stats unavailable.</span>`;

  let benchHtml = "";
  if (bench && bench.query_latency) {
    const latency = bench.query_latency;
    const rows = Object.entries(latency)
      .map(([name, m]) => {
        const slo = bench.slo_comparison[name];
        return `<tr>
          <td class="mono">${esc(name)}</td>
          <td>${m.p50_ms.toFixed(4)}</td>
          <td>${m.p95_ms.toFixed(4)}</td>
          <td>${m.p99_ms.toFixed(4)}</td>
          <td>${slo.slo_ms}</td>
          <td>${slo.within_slo ? badge("ok", "PASS") : badge("error", "FAIL")}</td>
        </tr>`;
      })
      .join("");

    benchHtml = `
      <section><h3 class="section-title">Latency benchmark (p95 vs SLO)</h3>
        <div id="latency-chart" style="margin-bottom:12px"></div>
        <table class="data">
          <thead><tr><th>Query</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>SLO ms</th><th>Status</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        <p class="view-sub">Scale: ${bench.scale.entities} entities · ${bench.scale.total_events} events · ${bench.projection.events_per_s.toLocaleString()} events/s projected</p>
      </section>`;
  } else {
    benchHtml = `
      <section><h3 class="section-title">Latency benchmark</h3>
        <div class="panel">
          <span class="empty">No benchmark report found. Run <code>make benchmark</code> to populate build/benchmark-report.json.</span>
        </div>
      </section>`;
  }

  view.innerHTML = `
    <section><h3 class="section-title">Read-model projection</h3>${statsHtml}</section>
    ${benchHtml}`;

  if (bench && bench.query_latency) {
    const chartData = Object.entries(bench.query_latency).map(([name, m]) => ({
      label: name.replace(/_ms$/, "").replace(/_/g, " "),
      p95: m.p95_ms,
      slo: bench.slo_comparison[name].slo_ms,
    }));
    renderLatencyChart(document.getElementById("latency-chart"), chartData);
  }
}
