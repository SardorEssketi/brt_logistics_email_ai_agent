// Small vanilla JS for the Driver Outreach UI.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FSA = "showOpenFilePicker" in window;

// ---------- IndexedDB: remember the original file handle per campaign ----------
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("outreach", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("handles");
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function idbPut(key, val) {
  try { const db = await idb(); const tx = db.transaction("handles", "readwrite"); tx.objectStore("handles").put(val, key);
    await new Promise((r) => (tx.oncomplete = r)); } catch (e) { console.warn(e); }
}
async function idbGet(key) {
  try { const db = await idb(); const tx = db.transaction("handles", "readonly"); const req = tx.objectStore("handles").get(key);
    return await new Promise((r) => { req.onsuccess = () => r(req.result); req.onerror = () => r(null); }); } catch (e) { return null; }
}
// Buttons with data-busy="…" show that text and lock while their form submits (stops double clicks on slow steps).
document.addEventListener("submit", (ev) => {
  if (ev.defaultPrevented) return;
  const btn = ev.submitter || ev.target.querySelector("button[data-busy]");
  if (btn && btn.dataset.busy) { setTimeout(() => { btn.disabled = true; btn.textContent = btn.dataset.busy; }, 0); }
});

const PICKER_TYPES =[{ description: "Excel or CSV", accept: {
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
  "application/vnd.ms-excel.sheet.macroEnabled.12": [".xlsm"], "text/csv": [".csv"] } }];

// ---------- Step 1: new campaign ----------
function initNewCampaign() {
  const form = $("#new-form");
  let handle = null, file = null;
  if (FSA) { $("#fsa-box").hidden = false; $("#plain-file").hidden = true; }
  $("#pick-file")?.addEventListener("click", async () => {
    try {
      [handle] = await window.showOpenFilePicker({ types: PICKER_TYPES, multiple: false });
      const perm = await handle.requestPermission({ mode: "readwrite" });
      file = await handle.getFile();
      $("#picked").textContent = `Selected: ${file.name}` + (perm === "granted" ? "" : " (write permission not given — you'll get a download instead)");
    } catch (e) { if (e.name !== "AbortError") $("#new-error").textContent = "Couldn't open the file: " + e.message; }
  });
  form.addEventListener("submit", async (ev) => {
    if (!file) return; // normal submit (Google Sheet or plain file input)
    ev.preventDefault();
    const fd = new FormData(form);
    fd.set("file", file, file.name);
    $("#next-btn").disabled = true;
    const r = await fetch(form.action, { method: "POST", body: fd, headers: { Accept: "application/json" } });
    const j = await r.json().catch(() => ({ error: "Unexpected server response." }));
    if (!r.ok) { $("#new-error").textContent = j.error; $("#next-btn").disabled = false; return; }
    await idbPut("campaign-" + j.id, handle);
    location.href = j.redirect;
  });
}

// ---------- Step 3: compose with live checker ----------
function initCompose() {
  const subj = $("#subject"), body = $("#body");
  let last = body, timer = null;
  [subj, body].forEach((el) => el.addEventListener("focus", () => (last = el)));
  document.querySelectorAll("[data-insert]").forEach((el) => el.addEventListener("click", () => {
    const t = el.dataset.insert, s = last.selectionStart ?? last.value.length, e = last.selectionEnd ?? s;
    last.value = last.value.slice(0, s) + t + last.value.slice(e);
    last.focus(); last.selectionStart = last.selectionEnd = s + t.length; run();
  }));
  async function run() {
    const r = await fetch("/api/check", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: subj.value, body: body.value }) });
    if (!r.ok) return;
    const j = await r.json();
    $("#check-out").innerHTML = j.blocks.map((b) => `<div class="alert red">${esc(b)}</div>`).join("") +
      j.warnings.map((w) => `<div class="alert amber">${esc(w)}</div>`).join("") +
      (!j.blocks.length && !j.warnings.length ? '<div class="alert green">Looks good.</div>' : "");
    $("#word-count").textContent = `${j.words} words`;
  }
  [subj, body].forEach((el) => el.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 400); }));
  run();
}

// ---------- Step 4: review / test send ----------
function initReview(cid) {
  const f = $("#test-form");
  f?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = f.querySelector("button"); btn.disabled = true;
    $("#test-out").textContent = "Sending…"; $("#test-out").className = "";
    const r = await fetch(`/api/campaigns/${cid}/test-send`, { method: "POST", body: new FormData(f) });
    const j = await r.json().catch(() => ({ ok: false, message: "Unexpected server response." }));
    $("#test-out").textContent = j.message; $("#test-out").className = j.ok ? "ok-text" : "bad-text";
    btn.disabled = false;
  });
}

// ---------- Settings ----------
function initSettings() {
  document.querySelectorAll("[data-test-account]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.testAccount, out = $("#test-" + id);
    out.textContent = "Testing…"; b.disabled = true;
    const r = await fetch(`/api/accounts/${id}/test`, { method: "POST" });
    const j = await r.json().catch(() => ({ ok: false, message: "Unexpected server response." }));
    out.textContent = j.message; out.className = "test-result " + (j.ok ? "ok-text" : "bad-text"); b.disabled = false;
  }));
  const warn = $(".cap-warning");
  const caps = document.querySelectorAll("input.cap");
  const risky = Number(warn?.dataset.risky || 100);
  const check = () => { if (warn) warn.hidden = ![...caps].some((i) => Number(i.value) > risky); };
  caps.forEach((i) => i.addEventListener("input", check)); check();
}
if (document.querySelector("[data-test-account], input.cap")) initSettings();

document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; }
  catch (e) { b.textContent = "Select and copy it"; }
  setTimeout(() => (b.textContent = "Copy"), 2000);
}));

// ---------- Progress ----------
function initProgress(cid, sourceType, originalName) {
  let handle = null, lastSavedTotal = -1, lastTotal = 0, saving = false;
  const show = (id, on) => { const el = $(id); if (el) el.hidden = !on; };

  async function poll() {
    let j;
    try { const r = await fetch(`/api/campaigns/${cid}/stats`); if (r.status === 401) { location.href = "/login"; return; } j = await r.json(); }
    catch (e) { return; }
    const st = $("#st-status"); st.textContent = j.status; st.className = "status " + j.status;
    for (const k of ["sent", "remaining", "replies", "bounces", "unsubs", "errors"]) $("#c-" + k).textContent = j[k];
    const total = j.sent + j.remaining + j.errors, pct = total ? Math.round((j.sent / total) * 100) : 0;
    $("#road-fill").style.width = pct + "%"; $("#road-bar").setAttribute("aria-valuenow", pct);
    $("#road-text").textContent = total ? `${j.sent} of ${total} sent (${pct}%)` : "Nobody to send to.";
    const reason = $("#st-reason"); reason.hidden = !j.reason; reason.textContent = j.reason || "";
    $("#st-window").textContent = j.status === "running" ? j.window : "";
    $("#st-estimate").textContent = j.estimate ? `Estimated finish: ${j.estimate}` : "";
    if (!j.estimate && j.status === "done") $("#st-estimate").textContent = "Finished";
    $("#st-red").innerHTML = j.red.map((m) => `<div class="alert red">${esc(m)}</div>`).join("");
    show("#f-pause", j.status === "running"); show("#f-resume", j.status === "paused");
    show("#f-stop", j.status === "running" || j.status === "paused");
    const accState = (a) => a.state === "ok"
      ? (a.next_in > 0 && j.status === "running" ? `<span class="status running">Next in ${Math.ceil(a.next_in / 60)} min</span>` : `<span class="status running">Ready</span>`)
      : a.state === "paused" ? `<span class="status paused">Resting</span><span class="status-note">until ${esc(a.paused_until)}</span>`
      : a.state === "no_name" ? `<span class="status stopped">Blocked</span><span class="status-note">No sender name. <a href="/settings#accounts">Add one</a></span>`
      : `<span class="status stopped">Stopped</span><span class="status-note">${esc(a.disabled_reason || "")}</span>`;
    $("#acc-table tbody").innerHTML = j.accounts.map((a) => `<tr><td>${esc(a.email)}</td><td class="n">${a.sent}</td>
      <td class="n">${a.sent_24h} / ${a.cap}</td><td class="n">${a.replies}</td><td class="n">${a.bounces}</td><td class="n">${a.unsubs}</td>
      <td class="n">${a.errors}</td><td>${accState(a)}</td></tr>`).join("");
    const m = j.marks; lastTotal = m.total;
    $("#marks-info").textContent = `${m.total - m.pending - m.unmatched.length} note(s) written` +
      (m.pending ? `, ${m.pending} waiting to be written` : "") + (m.last_error ? ` — last problem: ${m.last_error} (will retry)` : "");
    $("#unmatched").innerHTML = m.unmatched.length ? `<div class="alert amber"><b>${m.unmatched.length} note(s) couldn't be written</b>
      (row not found — the email was changed or the row deleted):<ul>${m.unmatched.map((u) =>
      `<li>Row ${u.row_num}, ${esc(u.email || "(no email)")}: ${esc(u.note)}</li>`).join("")}</ul></div>` : "";
    $("#st-alerts").innerHTML = j.alerts.length ? j.alerts.map((a) => `<div class="alert ${a.level === "error" ? "red" : "amber"}"><span>${esc(a.message)}</span></div>`).join("") : "None.";
  }

  async function saveMarks(interactive) {
    if (saving || !handle) return;
    saving = true;
    const out = $("#save-out");
    try {
      let perm = await handle.queryPermission({ mode: "readwrite" });
      if (perm !== "granted" && interactive) perm = await handle.requestPermission({ mode: "readwrite" });
      if (perm !== "granted") { if (interactive) throw Object.assign(new Error("permission"), { name: "NotAllowedError" }); return; }
      const file = await handle.getFile();
      const fd = new FormData(); fd.append("file", file, file.name);
      const r = await fetch(`/api/campaigns/${cid}/mark-file`, { method: "POST", body: fd });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || "Server error"); }
      const blob = await r.blob();
      const w = await handle.createWritable(); await w.write(blob); await w.close();
      lastSavedTotal = lastTotal;
      out.className = "ok-text"; out.textContent = `Saved into "${file.name}" at ${new Date().toLocaleTimeString()}.`;
    } catch (e) {
      out.className = "bad-text";
      const why = e.name === "NotAllowedError" ? "Permission to write the file was not given."
        : e.name === "NotFoundError" ? "The original file was moved, renamed or deleted."
        : `Couldn't write the file. It may be open in Excel or read-only: close it in Excel and click "Save notes" again. (${e.message})`;
      out.textContent = why + ' You can always use "Download marked copy" instead.';
    } finally { saving = false; }
  }

  async function setupFile() {
    if (sourceType === "gsheet") return;
    const btn = $("#save-file");
    if (!FSA) { $("#save-out").textContent = "Your browser can't save into the original file (use Chrome or Edge). Use \"Download marked copy\"."; return; }
    handle = await idbGet("campaign-" + cid);
    btn.hidden = false;
    if (!handle) btn.textContent = "Choose the original file to save notes into…";
    btn.addEventListener("click", async () => {
      if (!handle) {
        try { [handle] = await window.showOpenFilePicker({ types: PICKER_TYPES }); } catch (e) { return; }
        if ((await handle.getFile()).name !== originalName &&
            !confirm(`This file is not named "${originalName}". Save the marks into it anyway?`)) { handle = null; return; }
        await idbPut("campaign-" + cid, handle); btn.textContent = "Save notes into the Excel file";
      }
      saveMarks(true);
    });
    setInterval(() => { if (handle && lastTotal !== lastSavedTotal) saveMarks(false); }, 3 * 60 * 1000);
  }

  poll(); setInterval(poll, 5000); setupFile();
}
