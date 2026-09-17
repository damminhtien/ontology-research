/** Admin view: token-gated write operations over the production log. */
import { esc, post } from "../api.js";

const TOKEN_KEY = "foundry.admin.token";

export async function renderAdmin(viewEl) {
  viewEl.innerHTML = `
    <p class="view-sub">Mọi thao tác ghi cần admin token (<code>$FOUNDRY_CONSOLE_TOKEN</code>) và
    được audit trên event log (payload <code>actor</code>).</p>
    <div class="card">
      <h3>Admin token</h3>
      <input id="admin-token" type="password" placeholder="bearer token"
             value="${esc(sessionStorage.getItem(TOKEN_KEY) ?? "")}" style="width:60%">
      <button id="admin-save">Lưu</button>
      <span id="admin-status"></span>
    </div>
    <div class="card">
      <h3>Review queue (<code>ResolutionReviewQueued</code>)</h3>
      <button id="admin-review">Tải danh sách</button>
      <div id="review-list"></div>
    </div>
    <div class="card">
      <h3>Merge (under-merge repair)</h3>
      <input id="merge-survivor" placeholder="survivor urn:world:entity:…" style="width:45%">
      <input id="merge-duplicate" placeholder="duplicate urn:world:entity:…" style="width:45%">
      <input id="merge-reason" placeholder="lý do" style="width:60%">
      <button id="admin-merge">Merge</button>
    </div>
    <div class="card">
      <h3>Split (undo merge)</h3>
      <input id="split-survivor" placeholder="survivor" style="width:45%">
      <input id="split-duplicate" placeholder="duplicate" style="width:45%">
      <button id="admin-split">Split</button>
    </div>
    <div class="card">
      <h3>Backfill external-id bindings</h3>
      <button id="admin-backfill">Chạy backfill</button>
    </div>
    <pre id="admin-result"></pre>`;

  const token = () => document.getElementById("admin-token").value.trim();
  const result = (text) => {
    document.getElementById("admin-result").textContent = text;
  };

  document.getElementById("admin-save").onclick = () => {
    sessionStorage.setItem(TOKEN_KEY, token());
    document.getElementById("admin-status").textContent = "đã lưu (session)";
  };

  const guard = async (fn) => {
    try {
      result(JSON.stringify(await fn(), null, 2));
    } catch (err) {
      result(`ERROR: ${err.message}`);
    }
  };

  document.getElementById("admin-review").onclick = () =>
    guard(async () => {
      const data = await post("/api/admin/review", {}, token());
      return data;
    });

  document.getElementById("admin-merge").onclick = () =>
    guard(() =>
      post(
        "/api/admin/merge",
        {
          survivor_id: document.getElementById("merge-survivor").value.trim(),
          duplicate_id: document.getElementById("merge-duplicate").value.trim(),
          reason: document.getElementById("merge-reason").value.trim(),
          actor: "console-admin",
        },
        token(),
      ),
    );

  document.getElementById("admin-split").onclick = () =>
    guard(() =>
      post(
        "/api/admin/split",
        {
          survivor_id: document.getElementById("split-survivor").value.trim(),
          duplicate_id: document.getElementById("split-duplicate").value.trim(),
          reason: "undo via console",
          actor: "console-admin",
        },
        token(),
      ),
    );

  document.getElementById("admin-backfill").onclick = () =>
    guard(() => post("/api/admin/backfill-external-ids", {}, token()));
}