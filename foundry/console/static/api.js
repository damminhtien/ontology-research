/** Fetch helpers for the console API. */

export async function get(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? ""));
  }
  return res.json();
}

export function esc(text) {
  const el = document.createElement("span");
  el.textContent = text ?? "";
  return el.innerHTML;
}

export function badge(status, label) {
  const cls = { ok: "ok", warn: "warn", error: "error", info: "info" }[status] ?? "info";
  return `<span class="badge ${cls}">${esc(label ?? status)}</span>`;
}

export function severityBadge(severity) {
  const map = { NONE: ["info", "NONE"], PATCH: ["ok", "PATCH"], MINOR: ["warn", "MINOR"], MAJOR: ["error", "MAJOR"] };
  const [cls, label] = map[severity] ?? ["info", severity];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}
