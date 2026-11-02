// engram review UI (Feature E, second half). Vanilla JS, no build step, no external deps — served
// by scripts/review_server.py over loopback. Two views:
//   * Clarify   — churn the /clarify queue with keyboard shortcuts; every answer goes through the
//                 server's /api/answer, which is a thin wrapper over clarify_writeback.apply().
//   * Sensitive — release / keep / exclude the local-only iMessage/WhatsApp hold notes.
//
// Evidence-next-to-judgment: the left pane is the work list; the right pane shows the FULL evidence
// (question + structured evidence + candidates, or the held note's entire text) beside the action
// controls, so a decision is never made blind. A propagating clarify edit (>1 file) requires a
// second confirmation showing the exact file list (dry-run first). A 409 shows the current server
// state and forces a re-decide instead of clobbering.

const $ = (sel, el = document) => el.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const esc = (s) => (s == null ? "" : String(s));

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  return { status: r.status, ok: r.ok, body: data || {} };
}

const state = {
  view: "clarify",
  clarify: { items: [], sel: 0, status: "open", msg: null },
  sensitive: { source: "imsg", items: [], sel: 0, msg: null },
};

// ---------------------------------------------------------------- shared shell
function setView(v) {
  state.view = v;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  if (v === "clarify") loadClarify(); else loadSensitive();
}

function renderToolbar() {
  const tb = $("#toolbar");
  tb.innerHTML = "";
  if (state.view === "clarify") {
    const sel = el("select");
    ["open", "answered", "deferred", "skipped", "all"].forEach((s) => {
      const o = el("option", null, s); o.value = s; if (s === state.clarify.status) o.selected = true; sel.appendChild(o);
    });
    sel.onchange = () => { state.clarify.status = sel.value; loadClarify(); };
    const refresh = el("button", null, "↻ rebuild queue");
    refresh.onclick = doRefresh;
    tb.appendChild(el("span", null, `${state.clarify.items.length} items`));
    tb.appendChild(sel);
    tb.appendChild(refresh);
  } else {
    const sel = el("select");
    [["imsg", "iMessage"], ["whatsapp", "WhatsApp"]].forEach(([v, label]) => {
      const o = el("option", null, label); o.value = v; if (v === state.sensitive.source) o.selected = true; sel.appendChild(o);
    });
    sel.onchange = () => { state.sensitive.source = sel.value; loadSensitive(); };
    tb.appendChild(el("span", null, `${state.sensitive.items.length} held`));
    tb.appendChild(sel);
  }
}

// ---------------------------------------------------------------- clarify view
async function loadClarify() {
  const { body } = await api("GET", `/api/queue?status=${encodeURIComponent(state.clarify.status)}`);
  state.clarify.items = body.items || [];
  if (state.clarify.sel >= state.clarify.items.length) state.clarify.sel = 0;
  renderToolbar();
  renderClarifyList();
  renderClarifyDetail();
}

async function doRefresh() {
  const { body } = await api("POST", "/api/refresh", {});
  state.clarify.msg = body.rebuilt ? { kind: "ok", text: "queue rebuilt" } : { kind: "warn", text: body.summary || "refresh skipped" };
  await loadClarify();
}

// Group items by `entity` (matches how /clarify batches a person's items together).
function groupByEntity(items) {
  const groups = new Map();
  items.forEach((it, i) => {
    const k = it.entity || "(unassigned)";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(i);
  });
  return groups;
}

function renderClarifyList() {
  const list = $("#list");
  list.innerHTML = "";
  const items = state.clarify.items;
  if (!items.length) { list.appendChild(el("div", "empty", "No items for this status.")); return; }
  const groups = groupByEntity(items);
  for (const [entity, idxs] of groups) {
    list.appendChild(el("div", "group-hdr", entity));
    idxs.forEach((i) => {
      const it = items[i];
      const row = el("div", "row" + (i === state.clarify.sel ? " sel" : "") + (it.status !== "open" ? " done" : ""));
      const t = el("div", "r-title", it.question || it.id);
      const meta = el("div", "r-meta");
      meta.appendChild(el("span", "cat", it.category));
      meta.appendChild(document.createTextNode("  " + it.status));
      row.appendChild(t); row.appendChild(meta);
      row.onclick = () => { state.clarify.sel = i; renderClarifyList(); renderClarifyDetail(); };
      list.appendChild(row);
    });
  }
  const s = list.querySelector(".row.sel");
  if (s) s.scrollIntoView({ block: "nearest" });
}

function renderClarifyDetail() {
  const d = $("#detail");
  d.innerHTML = "";
  const it = state.clarify.items[state.clarify.sel];
  if (!it) { d.appendChild(el("div", "empty", "Select an item.")); return; }

  d.appendChild(Object.assign(el("div", "q"), { textContent: it.question || it.id }));
  const sub = el("div", "sub");
  sub.appendChild(el("span", "pill", it.category));
  sub.appendChild(document.createTextNode("  " + it.id + "  ·  status: " + it.status));
  d.appendChild(sub);

  // evidence card
  const ev = it.evidence || {};
  if (Object.keys(ev).length) {
    const card = el("div", "card"); card.appendChild(el("h3", null, "Evidence"));
    const dl = el("dl", "evidence");
    for (const [k, v] of Object.entries(ev)) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, typeof v === "object" ? JSON.stringify(v) : String(v)));
    }
    card.appendChild(dl); d.appendChild(card);
  }
  if (it.writeback && it.writeback.note) {
    const wb = el("div", "card"); wb.appendChild(el("h3", null, "Write-back effect"));
    wb.appendChild(el("div", null, it.writeback.note)); d.appendChild(wb);
  }

  // judgment card: candidate buttons + free text
  const jc = el("div", "card"); jc.appendChild(el("h3", null, "Answer"));
  const cands = el("div", "cands");
  (it.candidates || []).forEach((c, i) => {
    const b = el("button", "cand");
    b.appendChild(el("span", "num", String(i + 1)));
    b.appendChild(el("span", null, c));
    b.onclick = () => submitAnswer(it, c);
    cands.appendChild(b);
  });
  jc.appendChild(cands);

  const ft = el("div", "freetext");
  const input = el("input"); input.id = "freetext"; input.placeholder = "…or type a free-text answer (e.g. \"move to research\", \"corrected: <role>\") then Enter";
  input.onkeydown = (e) => { if (e.key === "Enter" && input.value.trim()) { e.preventDefault(); submitAnswer(it, input.value.trim()); } };
  const go = el("button", "primary", "submit");
  go.onclick = () => { if (input.value.trim()) submitAnswer(it, input.value.trim()); };
  ft.appendChild(input); ft.appendChild(go);
  jc.appendChild(ft);
  d.appendChild(jc);

  // pending confirm slot + message slot
  d.appendChild(el("div", null)).id = "confirmSlot";
  if (state.clarify.msg) {
    const m = el("div", "msg " + state.clarify.msg.kind, state.clarify.msg.text);
    d.appendChild(m);
  }
  d.appendChild(el("div", "hint",
    "Keys: j / ↓ next · k / ↑ prev · 1-9 pick a candidate · e free-text · Enter confirm. "
    + "A propagating edit (>1 file) asks you to confirm the file list first."));
}

// Submit flow: dry-run first; if it would touch >1 file, show the file list and require a second
// click; otherwise apply directly. Always send the last-seen hash so a stale queue -> safe 409.
async function submitAnswer(it, answer) {
  state.clarify.msg = null;
  const dry = await api("POST", "/api/dry-run", { id: it.id, answer });
  if (dry.status === 422) { state.clarify.msg = { kind: "err", text: dry.body.error }; return renderClarifyDetail(); }
  if (!dry.ok) { state.clarify.msg = { kind: "err", text: dry.body.error || ("dry-run failed " + dry.status) }; return renderClarifyDetail(); }
  const files = dry.body.files || [];
  if (files.length > 1) {
    return showConfirm(it, answer, dry.body);
  }
  return doApply(it, answer);
}

function showConfirm(it, answer, dryBody) {
  renderClarifyDetail();
  const slot = $("#confirmSlot");
  const box = el("div", "confirm-box");
  box.appendChild(el("div", null, `This answer will change ${dryBody.files.length} files:`));
  const ul = el("ul");
  dryBody.files.forEach((f) => ul.appendChild(el("li", null, f)));
  box.appendChild(ul);
  if (dryBody.edit) box.appendChild(el("div", null, "→ " + dryBody.edit));
  (dryBody.messages || []).forEach((m) => box.appendChild(el("div", "msg warn", m)));
  const confirm = el("button", "primary", `Confirm — apply "${answer}"`);
  confirm.onclick = () => doApply(it, answer);
  const cancel = el("button", null, "cancel");
  cancel.onclick = () => { renderClarifyDetail(); };
  const acts = el("div"); acts.style.marginTop = "8px"; acts.style.display = "flex"; acts.style.gap = "8px";
  acts.appendChild(confirm); acts.appendChild(cancel);
  box.appendChild(acts);
  slot.appendChild(box);
}

async function doApply(it, answer) {
  const res = await api("POST", "/api/answer", { id: it.id, answer, old: it._hash });
  if (res.status === 409) {
    state.clarify.msg = { kind: "warn", text: "Conflict — the queue changed since you loaded it. Reloading; re-decide." };
    await loadClarify();
    return;
  }
  if (res.status === 422 || res.status === 500) {
    state.clarify.msg = { kind: "err", text: res.body.error || ("apply failed " + res.status) };
    return renderClarifyDetail();
  }
  state.clarify.msg = { kind: "ok", text: "✓ " + (res.body.edit || "applied") };
  await loadClarify();
}

// ---------------------------------------------------------------- sensitive view
async function loadSensitive() {
  const { body } = await api("GET", `/api/sensitive-queue?source=${encodeURIComponent(state.sensitive.source)}`);
  state.sensitive.items = body.items || [];
  if (state.sensitive.sel >= state.sensitive.items.length) state.sensitive.sel = 0;
  renderToolbar();
  renderSensitiveList();
  renderSensitiveDetail();
}

function renderSensitiveList() {
  const list = $("#list");
  list.innerHTML = "";
  const items = state.sensitive.items;
  if (!items.length) { list.appendChild(el("div", "empty", "No held items in this queue.")); return; }
  list.appendChild(el("div", "group-hdr", "Held for review — release / keep / exclude"));
  items.forEach((it, i) => {
    const row = el("div", "row" + (i === state.sensitive.sel ? " sel" : ""));
    row.appendChild(el("div", "r-title", it.title || it.fp.split("/").pop()));
    const meta = el("div", "r-meta");
    meta.appendChild(document.createTextNode(it.date || ""));
    if (!it.present) { meta.appendChild(document.createTextNode("  ")); meta.appendChild(el("span", "cat", "file missing")); }
    row.appendChild(meta);
    row.onclick = () => { state.sensitive.sel = i; renderSensitiveList(); renderSensitiveDetail(); };
    list.appendChild(row);
  });
  const s = list.querySelector(".row.sel");
  if (s) s.scrollIntoView({ block: "nearest" });
}

function renderSensitiveDetail() {
  const d = $("#detail");
  d.innerHTML = "";
  const it = state.sensitive.items[state.sensitive.sel];
  if (!it) { d.appendChild(el("div", "empty", "Select a held item.")); return; }

  d.appendChild(Object.assign(el("div", "q"), { textContent: it.title || it.fp.split("/").pop() }));
  const sub = el("div", "sub");
  sub.appendChild(document.createTextNode(it.date + "  ·  "));
  sub.appendChild(el("span", "pill", it.fp));
  d.appendChild(sub);

  const card = el("div", "card"); card.appendChild(el("h3", null, "Held note (full text)"));
  if (it.present) {
    card.appendChild(Object.assign(el("pre", "note"), { textContent: it.content }));
  } else {
    card.appendChild(el("div", "msg warn",
      "This held draft is not present locally (held notes are local-only / gitignored). "
      + "Re-run the iMessage ingest to regenerate it before deciding."));
  }
  d.appendChild(card);

  const jc = el("div", "card"); jc.appendChild(el("h3", null, "Decision"));
  const reason = el("input", "reason");
  reason.placeholder = "reason (required for exclude) — e.g. \"PHI: the owner is treating physician\"";
  const acts = el("div"); acts.style.display = "flex"; acts.style.gap = "8px"; acts.style.flexWrap = "wrap";

  const release = el("button", "good", "Release → store");
  release.disabled = !it.present;
  release.onclick = () => confirmSensitive(it, "release", null,
    "Release this note into the real store (crosses from never-committed to a tracked file). Continue?");

  const keep = el("button", null, "Keep held");
  keep.onclick = () => sensitiveDecision(it, "keep", null);

  const exclude = el("button", "danger", "Exclude (permanent)");
  exclude.disabled = !it.present;
  exclude.onclick = () => {
    if (!reason.value.trim()) { setSensMsg("err", "Exclude requires a reason."); return; }
    confirmSensitive(it, "exclude", reason.value.trim(),
      "Permanently exclude this source from re-ingest and relocate the draft to excluded/ (not deleted). Continue?");
  };

  acts.appendChild(release); acts.appendChild(keep); acts.appendChild(exclude);
  jc.appendChild(reason); jc.appendChild(acts);
  d.appendChild(jc);

  d.appendChild(el("div", null)).id = "sensConfirmSlot";
  if (state.sensitive.msg) d.appendChild(el("div", "msg " + state.sensitive.msg.kind, state.sensitive.msg.text));
}

function setSensMsg(kind, text) { state.sensitive.msg = { kind, text }; renderSensitiveDetail(); }

function confirmSensitive(it, decision, reason, prompt) {
  renderSensitiveDetail();
  const slot = $("#sensConfirmSlot");
  const box = el("div", "confirm-box");
  box.appendChild(el("div", null, prompt));
  const acts = el("div"); acts.style.marginTop = "8px"; acts.style.display = "flex"; acts.style.gap = "8px";
  const go = el("button", "primary", "Confirm " + decision);
  go.onclick = () => sensitiveDecision(it, decision, reason);
  const cancel = el("button", null, "cancel");
  cancel.onclick = () => renderSensitiveDetail();
  acts.appendChild(go); acts.appendChild(cancel);
  box.appendChild(acts);
  slot.appendChild(box);
}

async function sensitiveDecision(it, decision, reason) {
  const payload = { fp: it.fp, decision };
  if (reason) payload.reason = reason;
  const res = await api("POST", "/api/sensitive-decision", payload);
  if (!res.ok) { setSensMsg("err", res.body.error || ("failed " + res.status)); return; }
  state.sensitive.msg = { kind: "ok", text: "✓ " + (res.body.message || decision) };
  await loadSensitive();
}

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  const typing = e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (state.view === "clarify") {
    const items = state.clarify.items;
    if (typing) {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); if (state.clarify.sel < items.length - 1) { state.clarify.sel++; renderClarifyList(); renderClarifyDetail(); } }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); if (state.clarify.sel > 0) { state.clarify.sel--; renderClarifyList(); renderClarifyDetail(); } }
    else if (e.key === "e") { const ft = $("#freetext"); if (ft) { e.preventDefault(); ft.focus(); } }
    else if (/^[1-9]$/.test(e.key)) {
      const it = items[state.clarify.sel];
      if (it && it.candidates && it.candidates[+e.key - 1]) { e.preventDefault(); submitAnswer(it, it.candidates[+e.key - 1]); }
    } else if (e.key === "Enter") {
      const it = items[state.clarify.sel];
      if (it && it.candidates && it.candidates[0]) { e.preventDefault(); submitAnswer(it, it.candidates[0]); }
    }
  } else {
    const items = state.sensitive.items;
    if (typing) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); if (state.sensitive.sel < items.length - 1) { state.sensitive.sel++; renderSensitiveList(); renderSensitiveDetail(); } }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); if (state.sensitive.sel > 0) { state.sensitive.sel--; renderSensitiveList(); renderSensitiveDetail(); } }
  }
});

document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => setView(t.dataset.view); });

// boot
setView("clarify");
