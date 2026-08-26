/** Hash router for the console SPA. */
import { get } from "./api.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderExplorer } from "./views/explorer.js";
import { renderVersions } from "./views/versions.js";
import { renderImpact } from "./views/impact.js";
import { renderMonitor } from "./views/monitor.js";

const routes = {
  dashboard: { title: "Dashboard", render: renderDashboard },
  explorer: { title: "Ontology Explorer", render: renderExplorer },
  versions: { title: "Versions & Releases", render: renderVersions },
  impact: { title: "Impact Analysis", render: renderImpact },
  monitor: { title: "Data Monitor", render: renderMonitor },
};

const viewEl = document.getElementById("view");

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [view, queryString] = raw.split("?");
  return {
    view: routes[view] ? view : "dashboard",
    params: new URLSearchParams(queryString ?? ""),
  };
}

async function route() {
  const { view, params } = parseHash();
  document.querySelectorAll("#nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === view);
  });
  viewEl.innerHTML = `<h2 class="view-title">${routes[view].title}</h2><p class="view-sub">loading…</p>`;
  try {
    await routes[view].render(viewEl, params);
  } catch (err) {
    viewEl.innerHTML = `<p class="error-text">Failed to load view: ${err.message}</p>`;
  }
  location.hash = `#/${view}${params.toString() ? `?${params}` : ""}`;
}

async function refreshHealthBadge() {
  const badgeEl = document.getElementById("health-badge");
  try {
    const data = await get("/api/overview");
    const problems = [
      data.version_check.passed ? null : "versions",
      data.dag_check.passed ? null : "dag",
      data.cq.failed > 0 ? `${data.cq.failed} cq` : null,
    ].filter(Boolean);
    badgeEl.className = `badge ${problems.length ? "error" : "ok"}`;
    badgeEl.textContent = problems.length
      ? `issues: ${problems.join(", ")}`
      : "all checks passed";
  } catch (err) {
    badgeEl.className = "badge warn";
    badgeEl.textContent = err.message;
  }
}

window.addEventListener("hashchange", () => {
  route();
  refreshHealthBadge();
});
route();
refreshHealthBadge();
setInterval(refreshHealthBadge, 30000);

document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.target.tagName === "INPUT") return;
  const searchBox = document.querySelector('input[data-role="search"]');
  if (searchBox) {
    event.preventDefault();
    searchBox.focus();
  }
});
