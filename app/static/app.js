// Small vanilla JS for the Wayflix Trucking Email Sender UI.
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

// ---------- Step 3: compose with live checker + live preview ----------
function initCompose(cid) {
  const subj = $("#subject"), body = $("#body"), form = $("#compose-form");
  let last = body, timer = null, dirty = false;
  [subj, body].forEach((el) => el.addEventListener("focus", () => (last = el)));
  document.querySelectorAll("[data-insert]").forEach((el) => el.addEventListener("click", () => {
    const t = el.dataset.insert, s = last.selectionStart ?? last.value.length, e = last.selectionEnd ?? s;
    last.value = last.value.slice(0, s) + t + last.value.slice(e);
    last.focus(); last.selectionStart = last.selectionEnd = s + t.length; dirty = true; run();
  }));
  async function run() {
    const payload = JSON.stringify({ subject: subj.value, body: body.value });
    const headers = { "Content-Type": "application/json" };
    const [rc, rp] = await Promise.all([
      fetch("/api/check", { method: "POST", headers, body: payload }),
      fetch(`/api/campaigns/${cid}/preview`, { method: "POST", headers, body: payload }),
    ]).catch(() => [null, null]);
    if (rc && rc.ok) {
      const j = await rc.json();
      $("#check-out").innerHTML = j.blocks.map((b) => `<div class="alert red">${esc(b)}</div>`).join("") +
        j.warnings.map((w) => `<div class="alert amber">${esc(w)}</div>`).join("") +
        (!j.blocks.length && !j.warnings.length ? '<div class="alert green">Looks good.</div>' : "");
      $("#word-count").textContent = `${j.words} words`;
    }
    if (rp && rp.ok) {
      const p = await rp.json();
      $("#pv-from").textContent = p.from;
      $("#pv-subject").textContent = p.subject || "(no subject yet)";
      $("#pv-text").textContent = p.text;
      $("#pv-who").textContent = p.row ? `— as ${p.name || p.to} (row ${p.row}) gets it` : "";
    }
  }
  [subj, body].forEach((el) => el.addEventListener("input", () => { dirty = true; clearTimeout(timer); timer = setTimeout(run, 400); }));
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  run();
}

// ---------- Step 4: review / test send ----------
function initReview(cid) {
  const cards = [...document.querySelectorAll("[data-preview]")];
  let shown = 0;
  $("#next-preview")?.addEventListener("click", () => {
    cards[shown].hidden = true; shown = (shown + 1) % cards.length; cards[shown].hidden = false;
  });
  const f = $("#test-form");
  f?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = f.querySelector("button"); btn.disabled = true; const label = btn.textContent; btn.textContent = "Sending…";
    $("#test-out").textContent = ""; $("#test-out").className = "";
    const r = await fetch(`/api/campaigns/${cid}/test-send`, { method: "POST", body: new FormData(f) });
    const j = await r.json().catch(() => ({ ok: false, message: "Unexpected server response." }));
    $("#test-out").textContent = j.message; $("#test-out").className = j.ok ? "alert green" : "alert red";
    btn.disabled = false; btn.textContent = label;
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

  const nf = (n) => Number(n).toLocaleString("en-US");
  let lastStatus = null;
  async function poll() {
    let j;
    try { const r = await fetch(`/api/campaigns/${cid}/stats`); if (r.status === 401) { location.href = "/login"; return; } j = await r.json(); }
    catch (e) { return; }
    // A status change (e.g. auto-pause) reloads the page so the buttons and messages are all current.
    if (lastStatus && lastStatus !== j.status) { location.reload(); return; }
    lastStatus = j.status;
    const st = $("#st-status"); st.textContent = j.status_label; st.className = "status " + j.status;
    const b = j.banner;
    $("#st-banner").className = "st-banner " + b.tone;
    $("#st-head").textContent = b.title === j.status_label ? "" : b.title;
    $("#st-text").textContent = b.text;
    $("#st-window").textContent = j.status === "running" && j.next_email ? j.next_email : "";
    for (const k of ["sent", "remaining", "replies", "bounces", "unsubs", "errors"]) $("#c-" + k).textContent = nf(j[k]);
    const total = j.sent + j.remaining + j.errors, pct = total ? Math.round((j.sent / total) * 100) : 0;
    $("#road-fill").style.width = pct + "%"; $("#road-bar").setAttribute("aria-valuenow", pct);
    $("#road-text").textContent = total ? `${nf(j.sent)} of ${nf(total)} sent (${pct}%)` : "Nobody to send to.";
    $("#st-estimate").textContent = j.estimate ? `Expected to finish around ${j.estimate}` : "";
    $("#st-warnings").innerHTML = j.warnings.map((m) => `<div class="alert amber">${esc(m)}</div>`).join("");
    $("#st-accprob").hidden = !j.account_problem;
    $("#st-accprob-text").textContent = j.account_problem ? `Not every account can send: ${j.account_problem}.` : "";
    show("#f-pause", j.status === "running"); show("#f-resume", j.status === "paused");
    show("#f-stop-wrap", j.status === "running" || j.status === "paused");
    const tone = (s) => (s === "ok" ? "running" : s === "paused" ? "paused" : "stopped");
    const accNote = (a) => a.state === "ok"
      ? (a.next_in > 0 && j.status === "running" ? `Next email in ${Math.ceil(a.next_in / 60)} min` : "")
      : a.state === "paused" ? `Until ${esc(a.paused_until)}`
      : a.state === "no_name" ? `<a href="/accounts#acc-${a.id}">Add a sender name</a>`
      : `${esc(a.disabled_reason || "")} — <a href="/accounts#acc-${a.id}">Fix</a>`;
    $("#acc-table tbody").innerHTML = j.accounts.map((a) => `<tr>
      <td data-label="Account"><b>${esc(a.name || a.email)}</b><br><span class="muted small-text">${esc(a.email)}</span></td>
      <td data-label="State" class="state-cell"><span class="status ${tone(a.state)}">${esc(a.state_label)}</span><span class="status-note">${accNote(a)}</span></td>
      <td class="n" data-label="Sent here">${nf(a.sent)}</td><td class="n" data-label="Last 24 h">${a.sent_24h} of ${a.cap}</td>
      <td class="n hide-sm" data-label="Replies">${a.replies}</td><td class="n hide-sm" data-label="Bounced">${a.bounces}</td>
      <td class="n hide-sm" data-label="Unsubscribed">${a.unsubs}</td><td class="n hide-sm" data-label="Failed">${a.errors}</td></tr>`).join("");
    const m = j.marks; lastTotal = m.total;
    const written = m.total - m.pending - m.unmatched.length;
    $("#marks-info").textContent = !m.total ? "No results yet." :
      `${nf(written)} of ${nf(m.total)} results written` + (sourceType === "gsheet" ? " into the sheet" : " on the server") +
      (m.pending ? ` · ${nf(m.pending)} waiting` : "") + (m.last_error ? ` — last problem: ${m.last_error} (it retries by itself)` : ".");
    $("#unmatched").innerHTML = m.unmatched.length ? `<div class="alert amber"><div><b>${m.unmatched.length} result(s) couldn't be written</b>
      because the row was not found (the email was changed or the row deleted). Add them by hand:<ul>${m.unmatched.map((u) =>
      `<li>Row ${u.row_num}, ${esc(u.email || "(no email)")}: ${esc(u.note)}</li>`).join("")}</ul></div></div>` : "";
    $("#act-count").textContent = j.alerts.length ? `(${j.alerts.length})` : "";
    $("#st-alerts").innerHTML = j.alerts.length ? j.alerts.map((a) =>
      `<li class="${a.level === "error" ? "red" : "amber"}">${esc(a.message)}</li>`).join("") : '<li class="muted">Nothing yet.</li>';
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
        : `Couldn't write the file. It may be open in Excel or read-only: close it in Excel and click "Save into my original file" again. (${e.message})`;
      out.textContent = why + ' You can always use "Download the list with results" instead.';
    } finally { saving = false; }
  }

  async function setupFile() {
    if (sourceType === "gsheet") return;
    const btn = $("#save-file");
    if (!FSA) return;   // other browsers: the download button is the way
    handle = await idbGet("campaign-" + cid);
    btn.hidden = false;
    if (!handle) btn.textContent = "Save into my original file…";
    btn.addEventListener("click", async () => {
      if (!handle) {
        try { [handle] = await window.showOpenFilePicker({ types: PICKER_TYPES }); } catch (e) { return; }
        if ((await handle.getFile()).name !== originalName &&
            !confirm(`This file is not named "${originalName}". Save the marks into it anyway?`)) { handle = null; return; }
        await idbPut("campaign-" + cid, handle); btn.textContent = "Save into my original file";
      }
      saveMarks(true);
    });
    setInterval(() => { if (handle && lastTotal !== lastSavedTotal) saveMarks(false); }, 3 * 60 * 1000);
  }

  poll(); setInterval(poll, 5000); setupFile();
}
