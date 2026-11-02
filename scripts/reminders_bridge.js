// reminders_bridge.js — JXA bridge between engram-tasks (Python) and Apple Reminders.
//
// Invocation:   osascript -l JavaScript scripts/reminders_bridge.js <op>
//   * request  : one JSON object on STDIN
//   * response : one JSON object on STDOUT  ->  {"ok":true,"result":{…}}
//                                          or  {"ok":false,"error":"…","code":"tcc|not-found|internal"}
//   * one osascript process handles ONE batch (op) then exits.
//
// The Python side (scripts/_lib/reminders.py :: bridge()) owns process-level failure
// classification (TCC / timeout / garbage / nonzero-exit); this script only classifies
// errors it can actually catch (TCC surfaces here as code:"tcc" when the Apple Event throws
// -1743). Partial success is legal for `create` / `read` / `complete` / `delete` (per-item
// ok/error, missing lists).
//
// ────────────────────────────────────────────────────────────────────────────────────────
// EMPIRICAL FINDINGS — pinned live on macOS 26.4.1 (build 25E253), Reminders signed-in,
// 733-reminder corpus (11 lists; only ~60 incomplete — lists retain full COMPLETED history:
// the largest lists held ~200 completed items each). Re-verify with
// `_lib/reminders.py :: doctor(live=True)` on a new macOS build; the doctor report re-pins each.
//
//  (1) byId app-wide  — App.reminders.byId('x-apple-reminder://<UUID>') RESOLVES a reminder
//      regardless of which list it now lives in (so a dragged/moved item still reads = accepted).
//      BUT it is O(corpus) and iCloud-variance-prone: ~40-100 s per single call (measured 99 s once).
//      => `read` must NEVER loop byId per id. It builds ONE index by iterating lists in bulk,
//         and only falls back to app-wide byId for the FEW ids not found under a `lists` hint.
//         (complete/delete DO resolve per id via byId — keep those batches small.)
//  (2) id format      — 'x-apple-reminder://<UUID>' (full URI, opaque; used verbatim as reminder_id).
//  (3) container      — reminder.container().name() returns the reminder's LIST name (used by read
//      only in the byId fallback path; the bulk index knows the list from iteration for free).
//  (4) alldayDueDate  — the property EXISTS and is readable/settable (see doctor live smoke). We set
//      due dates as ALL-DAY (alldayDueDate) so the reminder has no misleading clock time. Constant
//      USE_ALLDAY_DUE below; create() degrades to dueDate @ 09:00 local if the setter ever throws.
//  (5) flagged        — EXISTS, readable & settable via JXA (pinned by the live smoke).
//  (6) recently-deleted — a DELETED reminder is absent from every scriptable list AND unresolvable by
//      byId (the system "Recently Deleted" container is NOT exposed to AppleScript). So delete ⇒ read
//      returns it as MISSING (reconcile: missing ⇒ dismissed). Pinned by the live smoke.
//  (7) performance (MEASURED, 733-reminder corpus, 61 incomplete) — every bulk property read is
//      ~32 ms/item AND each property accessed through a whose()-filter RE-RUNS the whole-list scan
//      (verified: 1 prop on a 174-item list = 5.8 s, 6 props = 38 s; per-ITEM access on materialised
//      whose-matches was 141 s for 2 items — so bulk-props-on-the-whose-specifier, as below, is the
//      LEAST-bad shape). Consequences:
//        * dump_incomplete ≈ 165 s (7 property scans over the full completed-history corpus) — the
//          Python caller passes timeout>=240; it is a background (sync/nightly) op, never interactive.
//        * a SCOPED read (with a `lists` hint over the small engram lists) ≈ 1 s — the normal path.
//        * an UNSCOPED read / app-wide byId ≈ 40-100 s.
//      Lists retain full COMPLETED history (~200 items on the largest lists) which is the whole
//      cost. `whose({completed:false})` WORKS (not broken — merely slow); a naive per-item property
//      loop is what appears to hang. NOTE: EventKit (ObjC bridge) would fetch incomplete reminders
//      natively (no completed-history scan) but its async access callback did not fire from osascript
//      in this environment and it returns a different id scheme — deliberately not used in v1.
//  (8) native move  — App.move(reminder, {to: list}) WORKS (pinned live 2026-07-13): the reminder
//      relocates with its id STABLE and every rw property intact. Both endpoints must be
//      object-resolvable (top-level) lists — see (9). App.delete(list) also works (removes a list).
//  (9) sidebar GROUPS — NOT scriptable at all (no group class in the sdef; groups invisible to the
//      dictionary). A list nested in a group is READ-visible only in app-wide BULK scans
//      (App.reminders.id()/container.name()/…): the list OBJECT is unreachable by every path —
//      App.lists enumeration misses it, App.lists.byName throws, App.reminders[i] index access
//      throws, and byId(member).container() throws ("Can't get object."). byId(member) itself DOES
//      resolve for per-reminder property access (name/completed readable → complete/delete of
//      grouped items still work). Consequences: NOTHING can be created in or moved into a grouped
//      list via JXA; every list engram ROUTES INTO must stay top-level (ungrouped). opEnsureLists
//      carries a guard so a grouped route target fails loudly instead of minting a duplicate list.
//      (EventKit sees lists FLAT, so a compiled EventKit helper is the escape hatch if grouped
//      write targets are ever required.)
// (10) DUPLICATE-LIST INCIDENT (diagnosed 2026-07-13 via opDupCensus, which partitions by
//      container ID — names are ambiguous between duplicates): before the (9) guard existed,
//      the owner grouping their original area lists into sidebar Groups made them invisible to
//      ensure_lists,
//      which then minted same-named TOP-LEVEL duplicates and pushed there ever since. The
//      group-nested ORIGINALS hold the owner's hand-made items, pre-grouping engram pushes, and the
//      completed history; the top-level copies are 100% engram-created. Remediation:
//      a one-off merge over opMergePull (a byId-resolved reminder moved into a
//      list addressed by CONTAINER ID). A group-nested list cannot even be deleted by script —
//      emptied originals are removed manually in the sidebar.
// ────────────────────────────────────────────────────────────────────────────────────────

'use strict';
ObjC.import('Foundation');

// Pinned behaviour constants (see EMPIRICAL FINDINGS). create() still self-heals via try/catch.
var USE_ALLDAY_DUE = true;          // finding (4): set due as an all-day date, not a timed one.
var TOKEN_PREFIX_DEFAULT = 'engram-task: ';

var App = Application('Reminders');
App.includeStandardAdditions = false;

// List-name cache (App.lists.name() is a cheap ~1 s bulk call; fetch once per process).
var _listNames = null;
function listNames() {
  if (_listNames === null) _listNames = App.lists.name();
  return _listNames;
}
function listByName(name) {
  var names = listNames();
  for (var i = 0; i < names.length; i++) if (names[i] === name) return App.lists[i];
  return null;
}

// ── stdin / stdout ────────────────────────────────────────────────────────────────────────
function readStdin() {
  var fh = $.NSFileHandle.fileHandleWithStandardInput;
  var data = fh.readDataToEndOfFile;
  if (!data || data.length === 0) return '';
  return ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding));
}
function okResp(result) { return JSON.stringify({ ok: true, result: result }); }
function errResp(msg, code) { return JSON.stringify({ ok: false, error: String(msg), code: code || 'internal' }); }
function errString(e) { return (e && e.message) ? e.message : String(e); }
function classifyError(e) {
  var m = errString(e);
  if (m.indexOf('-1743') >= 0 || /not authoriz|not allowed|not permitted/i.test(m)) return 'tcc';
  return 'internal';
}

// ── date helpers ───────────────────────────────────────────────────────────────────────────
// Parse "YYYY-MM-DD" to a LOCAL Date. Using components (never new Date("YYYY-MM-DD"), which is
// UTC-midnight and shifts the calendar day west of GMT). Noon-local for all-day (shift-proof),
// 09:00-local for the timed fallback.
function _ymd(due) { var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due || ''); return m ? [+m[1], +m[2], +m[3]] : null; }
function dueAllDay(due) { var p = _ymd(due); return p ? new Date(p[0], p[1] - 1, p[2], 12, 0, 0, 0) : null; }
function dueTimed(due) { var p = _ymd(due); return p ? new Date(p[0], p[1] - 1, p[2], 9, 0, 0, 0) : null; }
function dateToYMD(d) {
  if (!d) return null;
  var y = d.getFullYear(), mo = ('0' + (d.getMonth() + 1)).slice(-2), da = ('0' + d.getDate()).slice(-2);
  return y + '-' + mo + '-' + da;
}
function firstLine(s) { if (!s) return ''; var i = s.indexOf('\n'); return (i < 0 ? s : s.slice(0, i)).trim(); }

// Bulk property fetch on a specifier, degrading to per-element on failure.
function bulkProp(spec, prop) {
  try { return spec[prop](); }
  catch (e) {
    var arr = spec(); // materialise
    return arr.map(function (x) { try { return x[prop](); } catch (e2) { return null; } });
  }
}
// Due of a reminder object: prefer all-day, fall back to timed.
function readDue(r) {
  var d = null;
  try { d = r.alldayDueDate(); } catch (e) {}
  if (!d) { try { d = r.dueDate(); } catch (e2) {} }
  return dateToYMD(d);
}
// Merge two bulk due arrays (all-day preferred) into Y-M-D strings.
function mergeDueBulk(allday, timed) {
  var out = [];
  for (var i = 0; i < allday.length; i++) out.push(dateToYMD(allday[i] || (timed ? timed[i] : null)));
  return out;
}

// ── app-wide byId (SLOW ~21 s; fallback only) ───────────────────────────────────────────────
function resolveAppWide(id) {
  try { var r = App.reminders.byId(id); r.name(); return r; } catch (e) {}
  // secondary fallback: per-list id filter (kept in case byId ever regresses on a future build)
  try {
    var names = listNames();
    for (var i = 0; i < names.length; i++) {
      var matches = App.lists[i].reminders.whose({ id: id })();
      if (matches && matches.length > 0) return matches[0];
    }
  } catch (e2) {}
  return null;
}

// ════════════════════════════════════════ OPS ══════════════════════════════════════════════

// ensure_lists {names:[…]} -> {lists:{name:id}, grouped:[…]}  (creates any missing list)
//   GROUP GUARD (2026-07-13, pinned live): a list nested inside a sidebar GROUP is invisible to
//   App.lists AND unreachable as an object by ANY means (byName, index, byId(member).container()
//   all throw "Can't get object.") — but its reminders still appear in app-wide BULK scans. So a
//   wanted name missing from enumeration gets one app-wide container-name census (~55 s, only in
//   this rare case) before creation: if the name exists as some reminder's container, the list is
//   group-nested — creating would silently mint a DUPLICATE top-level list and scatter items
//   across the two. We refuse, return it under `grouped`, and the caller's create then fails
//   per-item with list-not-found (visible, retryable — never silent corruption). Residual hole:
//   a grouped list with ZERO reminders (incl. completed) is undetectable and would still be
//   duplicated; that is unfixable from JXA.
function opEnsureLists(payload) {
  var want = payload.names || [];
  var haveNames = listNames();
  var haveIds = App.lists.id();
  var map = {};
  for (var i = 0; i < haveNames.length; i++) map[haveNames[i]] = haveIds[i];
  var result = {};
  var grouped = [];
  var hiddenContainers = null;   // lazily-built app-wide container-name set
  for (var j = 0; j < want.length; j++) {
    var nm = want[j];
    if (Object.prototype.hasOwnProperty.call(map, nm)) { result[nm] = map[nm]; continue; }
    if (hiddenContainers === null) {
      hiddenContainers = {};
      try {
        var conts = App.reminders.container.name();
        for (var c = 0; c < conts.length; c++) hiddenContainers[conts[c]] = true;
      } catch (e) { /* census failed: fall through to legacy create-on-missing behaviour */ }
    }
    if (hiddenContainers[nm]) { grouped.push(nm); continue; }
    var lst = App.List({ name: nm });
    App.lists.push(lst);
    var id = lst.id();
    result[nm] = id;
    map[nm] = id;
    _listNames = null; // invalidate cache: a new list now exists
  }
  return { lists: result, grouped: grouped };
}

// create {list, items:[{task_id,title,notes,due,priority,flagged?}]}
//        -> {created:[{task_id,reminder_id,ok}], errors:[{task_id,error}]}
function opCreate(payload) {
  var listName = payload.list;
  var items = payload.items || [];
  var lst = listByName(listName);
  if (!lst) {
    return {
      created: [],
      errors: items.map(function (it) { return { task_id: it.task_id, error: 'list-not-found:' + listName }; })
    };
  }
  // PERF (2026-07-07, measured): property sets / id() on a just-pushed reminder
  // each re-resolve against the container (~3s+ on history-heavy lists) => ~13s
  // per item. So: pass EVERY property to the constructor (push = one Apple
  // Event), never touch `r` afterwards, and read ids back in ONE vectorized
  // body()+id() scan per list, matched via the `engram-task: <id>` notes token.
  var created = [], errors = [], pushed = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    try {
      var props = { name: String(it.title == null ? '' : it.title), body: String(it.notes == null ? '' : it.notes) };
      if (it.due) {
        try { if (USE_ALLDAY_DUE) props.alldayDueDate = dueAllDay(it.due); else props.dueDate = dueTimed(it.due); }
        catch (ed) { props.dueDate = dueTimed(it.due); }
      }
      if (typeof it.priority === 'number') props.priority = it.priority;
      if (it.flagged != null) props.flagged = !!it.flagged;
      lst.reminders.push(App.Reminder(props));
      pushed.push(it);
    } catch (e) {
      errors.push({ task_id: it.task_id, error: errString(e) });
    }
  }
  var idByToken = {};
  if (pushed.length) {
    try {
      var bodies = lst.reminders.body();
      var ids = lst.reminders.id();
      for (var j = 0; j < bodies.length; j++) {
        if (bodies[j] == null) continue;
        var line = String(bodies[j]).split('\n')[0];
        if (line.indexOf('engram-task: ') === 0) idByToken[line.slice('engram-task: '.length).split(/\s/)[0]] = ids[j];
      }
    } catch (eb) {}
  }
  for (var k = 0; k < pushed.length; k++) {
    var rid = idByToken[pushed[k].task_id];
    if (rid) created.push({ task_id: pushed[k].task_id, reminder_id: rid, ok: true });
    else errors.push({ task_id: pushed[k].task_id, error: 'created-but-id-not-read-back (next push repair-adopts it)' });
  }
  return { created: created, errors: errors };
}

// read {ids:[…], lists?:[…]} -> {found:[{id,completed,list,flagged}], missing:[…]}
//   ONE app-wide UNFILTERED bulk pass (id + completed + flagged + container.name), then a lookup.
//   This is the ONLY correct shape (measured 2026-07-08):
//     * App.lists does NOT enumerate lists nested inside a GROUP — a per-list scan silently misses
//       every grouped list, and its items would read as `missing` ⇒ wrongly dismissed. App-wide
//       `App.reminders` sees grouped lists.
//     * byId is ~22 s/item even warm (no amortization) — a per-id fallback is fatal at any scale.
//     * whose()-filters re-scan the whole corpus per property; an UNFILTERED bulk read is cheaper.
//   Cost is FIXED (~115 s over ~830 reminders incl. completed history) regardless of how many items
//   changed — so a 40-item triage session costs the same as a 0-item nightly. `lists` is ignored
//   (kept for signature compat). A deleted reminder is absent app-wide (Recently Deleted is not
//   scriptable) ⇒ missing ⇒ dismissed. completed=true ⇒ done. present+incomplete ⇒ still-open
//   (its container.name is the CURRENT list, so moves and unflags are observed).
function opRead(payload) {
  var ids = payload.ids || [];
  var all = App.reminders;
  var aids = all.id();
  var acomp = bulkProp(all, 'completed');
  var aflag = bulkProp(all, 'flagged');
  var aconts = [];
  try { aconts = all.container.name(); } catch (e) { aconts = aids.map(function () { return null; }); }
  var index = {};
  for (var j = 0; j < aids.length; j++) {
    // flagged is TRI-STATE: a degraded per-element read yields null, which must reach Python as
    // null (confirm requires an OBSERVED false) — never coerced to false, or one bad read would
    // mass-confirm the ledger.
    index[aids[j]] = { completed: !!acomp[j],
                       flagged: aflag[j] == null ? null : !!aflag[j],
                       list: aconts[j] || null };
  }
  var found = [], missing = [];
  for (var k = 0; k < ids.length; k++) {
    var it = index[ids[k]];
    if (it) found.push({ id: ids[k], completed: it.completed, list: it.list, flagged: it.flagged });
    else missing.push(ids[k]); // absent app-wide ⇒ deleted
  }
  return { found: found, missing: missing };
}

// dump_incomplete {} -> {items:[{id,title,list,due,notes_head,flagged,priority}]} across ALL lists.
function opDumpIncomplete() {
  var out = [];
  var names = listNames();
  for (var i = 0; i < names.length; i++) {
    var spec = App.lists[i].reminders.whose({ completed: false });
    var ids = spec.id();
    if (!ids.length) continue;
    var titles = bulkProp(spec, 'name');
    var bodies = bulkProp(spec, 'body');
    var dues = mergeDueBulk(bulkProp(spec, 'alldayDueDate'), bulkProp(spec, 'dueDate'));
    var flags = bulkProp(spec, 'flagged');
    var prios = bulkProp(spec, 'priority');
    for (var j = 0; j < ids.length; j++) {
      out.push({
        id: ids[j], title: titles[j], list: names[i], due: dues[j],
        notes_head: firstLine(bodies[j]), flagged: !!flags[j], priority: prios[j]
      });
    }
  }
  return { items: out };
}

// find_by_token {lists:[…], prefix:"engram-task: "} -> {matches:[{task_id,reminder_id,completed,list}]}
//   scans the named lists' INCOMPLETE reminders' notes first line for the token (repair sweep:
//   adopt orphans from a crashed prior push, which are incomplete). Named lists only ⇒ bounded cost.
function opFindByToken(payload) {
  var names = payload.lists || [];
  var prefix = payload.prefix || TOKEN_PREFIX_DEFAULT;
  var matches = [];
  for (var i = 0; i < names.length; i++) {
    var lst = listByName(names[i]);
    if (!lst) continue;
    var spec = lst.reminders.whose({ completed: false });
    var ids = spec.id();
    if (!ids.length) continue;
    var bodies = bulkProp(spec, 'body');
    for (var j = 0; j < ids.length; j++) {
      var head = firstLine(bodies[j]);
      if (head && head.indexOf(prefix) === 0) {
        matches.push({ task_id: head.slice(prefix.length).trim().split(/\s/)[0], reminder_id: ids[j], completed: false, list: names[i] });
      }
    }
  }
  return { matches: matches };
}

// read_full {lists:[…]} -> {items:[{id,title,notes,due_allday,due_timed,flagged,priority,
//                                    completed,list}], missing_lists:[…]}
//   FULL-field scoped read over the NAMED lists only (bulk props; ~1-2s per small list). Used by
//   the waiting-list migration for before/after field-for-field verification. Unlike
//   dump_incomplete this returns the whole notes body and keeps the allday-vs-timed due
//   distinction (dates as "YYYY-MM-DD"). A named list that does not resolve (e.g. nested in a
//   sidebar GROUP — invisible to App.lists, see opRead notes) lands in missing_lists.
function opReadFull(payload) {
  var names = payload.lists || [];
  var out = [], missing = [];
  for (var i = 0; i < names.length; i++) {
    var lst = listByName(names[i]);
    if (!lst) { missing.push(names[i]); continue; }
    var spec = lst.reminders;
    var ids = spec.id();
    if (!ids.length) continue;
    var titles = bulkProp(spec, 'name');
    var bodies = bulkProp(spec, 'body');
    var alldays = bulkProp(spec, 'alldayDueDate');
    var timeds = bulkProp(spec, 'dueDate');
    var flags = bulkProp(spec, 'flagged');
    var prios = bulkProp(spec, 'priority');
    var comps = bulkProp(spec, 'completed');
    for (var j = 0; j < ids.length; j++) {
      out.push({
        id: ids[j], title: titles[j], notes: bodies[j] == null ? '' : String(bodies[j]),
        due_allday: dateToYMD(alldays[j]), due_timed: timeds[j] ? String(timeds[j]) : null,
        flagged: flags[j] == null ? null : !!flags[j], priority: prios[j],
        completed: !!comps[j], list: names[i]
      });
    }
  }
  return { items: out, missing_lists: missing };
}

// migrate_move {id, from, to, token} -> {ok, method:"move"|"clone"|"already", reminder_id, list}
//   Move ONE reminder between two NAMED (top-level, enumerable) lists — the waiting-list
//   migration primitive. Deliberately single-item: the Python caller checkpoints the ledger after
//   every item, so a crash can never lose or double an item (house rule: resumable jobs derive
//   state from disk). Avoids app-wide byId entirely (~40-100s/item) — everything is list-scoped.
//   Steps:
//     1. already in `to`  -> {method:"already"} (idempotent resume after a crashed prior run).
//     2. native App.move(obj, {to:…}) variants, VERIFIED by re-scanning both lists (a no-throw
//        move that did not actually relocate the item is treated as a failure, not trusted).
//     3. clone+delete fallback: copy every rw-scriptable property (name/body/alldayDueDate/
//        dueDate/remindMeDate/priority/flagged), create in `to`, read the new id back via the
//        `token` notes first line, THEN delete the source (create-before-delete: a crash between
//        the two leaves a duplicate the next run heals — see step 1/dedup — never a loss).
//        NOTE the clone path cannot carry non-scriptable state (subtasks, attachments,
//        recurrence, creation date) — the caller surfaces method:"clone" so that is reviewable.
function opMigrateMove(payload) {
  var id = payload.id, fromName = payload.from, toName = payload.to, token = payload.token || '';
  var fromList = listByName(fromName);
  var toList = listByName(toName);
  if (!toList) return { ok: false, error: 'list-not-found:' + toName };
  var toIds = toList.reminders.id();
  if (toIds.indexOf(id) >= 0) return { ok: true, method: 'already', reminder_id: id, list: toName };
  // token match in target (clone from a crashed prior run minted a NEW id): adopt it, then make
  // sure the source copy is gone.
  var tokMatch = null;
  if (token) {
    var toBodies = bulkProp(toList.reminders, 'body');
    for (var t = 0; t < toBodies.length; t++) {
      if (toBodies[t] != null && String(toBodies[t]).split('\n')[0].indexOf(token) === 0) { tokMatch = toIds[t]; break; }
    }
  }
  if (!fromList) {
    if (tokMatch) return { ok: true, method: 'already', reminder_id: tokMatch, list: toName };
    return { ok: false, error: 'list-not-found:' + fromName };
  }
  var fromIds = fromList.reminders.id();
  var idx = fromIds.indexOf(id);
  if (tokMatch) {
    if (idx >= 0) { try { App.delete(fromList.reminders[idx]); } catch (e) { return { ok: false, error: 'dup-cleanup-failed: ' + errString(e) }; } }
    return { ok: true, method: 'already', reminder_id: tokMatch, list: toName };
  }
  if (idx < 0) return { ok: false, error: 'not-in-from-or-target' };
  var obj = fromList.reminders[idx];

  // (2) native move variants, each verified by membership re-scan
  var variants = [
    function () { App.move(obj, { to: toList }); },
    function () { App.move(obj, { to: toList.reminders }); }
  ];
  for (var v = 0; v < variants.length; v++) {
    try { variants[v](); } catch (e) { continue; }
    var nowTo = toList.reminders.id();
    if (nowTo.indexOf(id) >= 0) return { ok: true, method: 'move', reminder_id: id, list: toName };
    // re-resolve in case the failed attempt reindexed anything (it should not have)
    fromIds = fromList.reminders.id();
    idx = fromIds.indexOf(id);
    if (idx < 0) return { ok: false, error: 'move-variant-' + v + '-lost-item' };
    obj = fromList.reminders[idx];
  }

  // (3) clone + delete (create-before-delete)
  var snap = {};
  try {
    snap.name = obj.name(); snap.body = obj.body();
    try { snap.allday = obj.alldayDueDate(); } catch (e1) { snap.allday = null; }
    try { snap.timed = obj.dueDate(); } catch (e2) { snap.timed = null; }
    try { snap.remind = obj.remindMeDate(); } catch (e3) { snap.remind = null; }
    snap.priority = obj.priority(); snap.flagged = obj.flagged(); snap.completed = obj.completed();
  } catch (e) { return { ok: false, error: 'clone-read-failed: ' + errString(e) }; }
  if (token && String(snap.body || '').split('\n')[0].indexOf(token) !== 0) {
    return { ok: false, error: 'token-mismatch-refusing-clone' };  // wrong item — never clone it
  }
  try {
    var props = { name: String(snap.name == null ? '' : snap.name), body: String(snap.body == null ? '' : snap.body) };
    if (snap.allday) props.alldayDueDate = snap.allday;
    else if (snap.timed) props.dueDate = snap.timed;
    if (snap.remind) props.remindMeDate = snap.remind;
    if (typeof snap.priority === 'number') props.priority = snap.priority;
    if (snap.flagged != null) props.flagged = !!snap.flagged;
    if (snap.completed) props.completed = true;
    toList.reminders.push(App.Reminder(props));
  } catch (e) { return { ok: false, error: 'clone-create-failed: ' + errString(e) }; }
  var newId = null;
  var bodies2 = bulkProp(toList.reminders, 'body');
  var ids2 = toList.reminders.id();
  for (var k = 0; k < bodies2.length; k++) {
    if (bodies2[k] != null && String(bodies2[k]).split('\n')[0].indexOf(token || String(snap.body).split('\n')[0]) === 0) newId = ids2[k];
  }
  if (!newId) return { ok: false, error: 'clone-created-but-not-found (source NOT deleted)' };
  // delete the source only now that the clone is verified present
  fromIds = fromList.reminders.id();
  idx = fromIds.indexOf(id);
  if (idx >= 0) {
    try { App.delete(fromList.reminders[idx]); }
    catch (e) { return { ok: false, error: 'clone-ok-source-delete-failed (duplicate present; rerun heals): ' + errString(e), reminder_id: newId, list: toName }; }
  }
  return { ok: true, method: 'clone', reminder_id: newId, list: toName };
}

// dup_census {names:[…]} -> {enumerated:[{name,id}], reminders:[{id,title,completed,
//                             container_name, container_id}], container_id_ok: bool}
//   Read-only census for the duplicate-list merge (2026-07-13): same-named list pairs (one
//   group-nested, one top-level) are indistinguishable by NAME, so this partitions by container
//   ID. `enumerated` = the object-resolvable (top-level) lists among `names`; `reminders` = every
//   reminder app-wide whose container NAME is in `names`, with its container id when the bulk
//   container.id() read works (container_id_ok records that; the Python caller falls back to
//   set-difference against the enumerated lists' own id scans when it does not).
function opDupCensus(payload) {
  var wanted = {};
  (payload.names || []).forEach(function (n) { wanted[n] = true; });
  var enumerated = [];
  var eNames = listNames();
  var eIds = App.lists.id();
  for (var i = 0; i < eNames.length; i++) {
    if (wanted[eNames[i]]) enumerated.push({ name: eNames[i], id: eIds[i] });
  }
  var all = App.reminders;
  var aids = all.id();
  var atitles = bulkProp(all, 'name');
  var acomp = bulkProp(all, 'completed');
  var cnames = [];
  try { cnames = all.container.name(); } catch (e) { cnames = aids.map(function () { return null; }); }
  var cids = null;
  try { cids = all.container.id(); } catch (e2) { cids = null; }
  var out = [];
  for (var j = 0; j < aids.length; j++) {
    if (!wanted[cnames[j]]) continue;
    out.push({ id: aids[j], title: atitles[j] == null ? null : String(atitles[j]),
               completed: !!acomp[j], container_name: cnames[j],
               container_id: cids ? cids[j] : null });
  }
  return { enumerated: enumerated, reminders: out, container_id_ok: !!cids };
}

// merge_pull {id, to_list_id} -> {ok, method:"move"|"already", before:{…}, reminder_id, list}
//   Pull ONE reminder OUT of a group-nested duplicate list into the automation (top-level) copy,
//   addressed by DESTINATION LIST ID (names are ambiguous between same-named duplicates). The
//   reminder is resolved app-wide via byId — per-reminder property access works for grouped items
//   even though their CONTAINER object is unreachable (finding 9). Returns the full
//   before-snapshot read pre-move so the Python caller can field-verify after. Deliberately NO
//   clone fallback: these are the owner's HAND-made items (subtasks/attachments plausible) and a clone
//   would silently drop non-scriptable state — if App.move throws we fail loudly; the fallback is
//   manual un-grouping, never a lossy copy.
function opMergePull(payload) {
  var id = payload.id, toListId = payload.to_list_id;
  var eIds = App.lists.id();
  var idx = eIds.indexOf(toListId);
  if (idx < 0) return { ok: false, error: 'to-list-id-not-resolvable:' + toListId };
  var toList = App.lists[idx];
  var toName = toList.name();
  var toIds = toList.reminders.id();
  if (toIds.indexOf(id) >= 0) return { ok: true, method: 'already', reminder_id: id, list: toName };
  var r;
  try { r = App.reminders.byId(id); r.name(); }
  catch (e) { return { ok: false, error: 'byid-unresolvable: ' + errString(e) }; }
  var before = {};
  try {
    before.title = r.name(); before.notes = r.body() == null ? '' : String(r.body());
    try { before.due_allday = dateToYMD(r.alldayDueDate()); } catch (e1) { before.due_allday = null; }
    try { var td = r.dueDate(); before.due_timed = td ? String(td) : null; } catch (e2) { before.due_timed = null; }
    before.priority = r.priority(); before.flagged = r.flagged(); before.completed = r.completed();
  } catch (e) { return { ok: false, error: 'before-read-failed: ' + errString(e) }; }
  try { App.move(r, { to: toList }); }
  catch (e) { return { ok: false, error: 'move-threw: ' + errString(e), before: before }; }
  var nowTo = toList.reminders.id();
  if (nowTo.indexOf(id) < 0) {
    return { ok: false, error: 'move-no-throw-but-not-in-target (id may have morphed — inspect)',
             before: before };
  }
  return { ok: true, method: 'move', before: before, reminder_id: id, list: toName };
}

// complete {ids:[…]} -> {done:[…], missing:[…]}
function opComplete(payload) {
  var ids = payload.ids || [];
  var done = [], missing = [];
  for (var i = 0; i < ids.length; i++) {
    var r = resolveAppWide(ids[i]);
    if (!r) { missing.push(ids[i]); continue; }
    try { r.completed = true; done.push(ids[i]); } catch (e) { missing.push(ids[i]); }
  }
  return { done: done, missing: missing };
}

// delete {ids:[…]} -> {done:[…], missing:[…]}
function opDelete(payload) {
  var ids = payload.ids || [];
  var done = [], missing = [];
  for (var i = 0; i < ids.length; i++) {
    var r = resolveAppWide(ids[i]);
    if (!r) { missing.push(ids[i]); continue; }
    try { App.delete(r); done.push(ids[i]); } catch (e) { missing.push(ids[i]); }
  }
  return { done: done, missing: missing };
}

// delete_in_list {list, ids:[…]} -> {deleted:[…], not_in_list:[…], errors:[{id,error}],
//                                     list_missing: bool, scanned: n}
//   BULK scoped delete — the only shape that is affordable at sweep scale. `delete` above resolves
//   every id through resolveAppWide (byId, ~40-100 s PER id, finding 1); this does ONE list-scoped
//   bulk `id()` read (no whose() filter — finding 7 — so ~1 property scan of that list) and then
//   deletes by INDEX.
//   Two invariants:
//     * DESCENDING index order — App.delete() reindexes the list, and a higher index never shifts
//       a lower one, so the precomputed indices stay valid through the batch.
//     * id RE-VERIFIED immediately before every App.delete (obj.id() === the wanted id). A
//       reminder whose id is not in `ids` therefore cannot be deleted even if the list mutated
//       mid-batch; on a mismatch we re-read the list's ids and re-resolve that ONE id, and give up
//       on it (error: unresolved-in-list) rather than deleting a neighbour.
//   `not_in_list` = requested ids absent from this list (already deleted, or moved elsewhere) —
//   which is what makes the caller resumable: remaining work is re-derived from live state.
//   Lists are never created or deleted here.
function opDeleteInList(payload) {
  var listName = payload.list;
  var ids = payload.ids || [];
  var want = {};
  for (var w = 0; w < ids.length; w++) want[ids[w]] = true;
  var lst = listByName(listName);
  if (!lst) return { deleted: [], not_in_list: ids, errors: [], list_missing: true, scanned: 0 };

  var present = lst.reminders.id();
  var inList = {};
  var targets = [];
  for (var i = 0; i < present.length; i++) {
    if (want[present[i]] && !inList[present[i]]) {
      inList[present[i]] = true;
      targets.push({ idx: i, id: present[i] });
    }
  }
  targets.sort(function (a, b) { return b.idx - a.idx; });   // descending: delete-safe indices

  var deleted = [], errors = [];
  for (var t = 0; t < targets.length; t++) {
    var tgt = targets[t];
    var obj = null;
    try { obj = lst.reminders[tgt.idx]; } catch (e) { obj = null; }
    var actual = null;
    if (obj) { try { actual = obj.id(); } catch (e2) { actual = null; } }
    if (actual !== tgt.id) {
      obj = null;                                  // index drifted — re-resolve inside the list
      try {
        var cur = lst.reminders.id();
        var k = cur.indexOf(tgt.id);
        if (k >= 0) {
          var cand = lst.reminders[k];
          if (cand.id() === tgt.id) obj = cand;
        }
      } catch (e3) { obj = null; }
    }
    if (!obj) { errors.push({ id: tgt.id, error: 'unresolved-in-list' }); continue; }
    try { App.delete(obj); deleted.push(tgt.id); }
    catch (e4) { errors.push({ id: tgt.id, error: errString(e4) }); }
  }

  var notIn = [];
  for (var n = 0; n < ids.length; n++) if (!inList[ids[n]]) notIn.push(ids[n]);
  return { deleted: deleted, not_in_list: notIn, errors: errors,
           list_missing: false, scanned: present.length };
}

// complete_in_list {list, ids:[…]} -> {completed:[…], already_completed:[…], not_in_list:[…],
//                                       errors:[{id,error}], list_missing: bool, scanned: n}
//   BULK scoped completion — the `complete` twin of delete_in_list, and the only affordable shape
//   at sweep scale: `complete` above resolves every id through resolveAppWide (byId, ~40-100 s PER
//   id, finding 1). This does ONE list-scoped bulk `id()` read plus ONE bulk `completed()` read (no
//   whose() filter — finding 7 — so 2 property scans of that list) and then sets `completed` by
//   INDEX, under delete_in_list's two invariants:
//     * DESCENDING index order. Completing does NOT remove an item from a list (lists retain full
//       completed history, finding 7), so indices are stable here — the order is kept anyway so the
//       two ops share one auditable pattern and a future Reminders build that DOES re-index (e.g.
//       "show completed" off) cannot silently corrupt the batch.
//     * id RE-VERIFIED immediately before every mutation (obj.id() === the wanted id). A reminder
//       whose id is not in `ids` therefore cannot be completed even if the list mutated mid-batch;
//       on a mismatch we re-read the list's ids and re-resolve that ONE id, and give up on it
//       (error: unresolved-in-list) rather than completing a neighbour.
//   `already_completed` is reported separately (setting completed=true on a completed reminder is a
//   no-op, but the caller's summary should not claim a change it did not make). `not_in_list` =
//   requested ids absent from this list (moved elsewhere, or deleted) — which is what makes the
//   caller resumable: remaining work is re-derived from live state on every call.
//   Lists are never created or deleted here.
function opCompleteInList(payload) {
  var listName = payload.list;
  var ids = payload.ids || [];
  var want = {};
  for (var w = 0; w < ids.length; w++) want[ids[w]] = true;
  var lst = listByName(listName);
  if (!lst) return { completed: [], already_completed: [], not_in_list: ids, errors: [],
                     list_missing: true, scanned: 0 };

  var present = lst.reminders.id();
  var doneFlags = bulkProp(lst.reminders, 'completed');
  var inList = {};
  var targets = [];
  for (var i = 0; i < present.length; i++) {
    if (want[present[i]] && !inList[present[i]]) {
      inList[present[i]] = true;
      targets.push({ idx: i, id: present[i], done: !!(doneFlags && doneFlags[i]) });
    }
  }
  targets.sort(function (a, b) { return b.idx - a.idx; });   // descending: mirrors delete_in_list

  var completed = [], already = [], errors = [];
  for (var t = 0; t < targets.length; t++) {
    var tgt = targets[t];
    if (tgt.done) { already.push(tgt.id); continue; }
    var obj = null;
    try { obj = lst.reminders[tgt.idx]; } catch (e) { obj = null; }
    var actual = null;
    if (obj) { try { actual = obj.id(); } catch (e2) { actual = null; } }
    if (actual !== tgt.id) {
      obj = null;                                  // index drifted — re-resolve inside the list
      try {
        var cur = lst.reminders.id();
        var k = cur.indexOf(tgt.id);
        if (k >= 0) {
          var cand = lst.reminders[k];
          if (cand.id() === tgt.id) obj = cand;
        }
      } catch (e3) { obj = null; }
    }
    if (!obj) { errors.push({ id: tgt.id, error: 'unresolved-in-list' }); continue; }
    try { obj.completed = true; completed.push(tgt.id); }
    catch (e4) { errors.push({ id: tgt.id, error: errString(e4) }); }
  }

  var notInC = [];
  for (var n2 = 0; n2 < ids.length; n2++) if (!inList[ids[n2]]) notInC.push(ids[n2]);
  return { completed: completed, already_completed: already, not_in_list: notInC, errors: errors,
           list_missing: false, scanned: present.length };
}

// ════════════════════════════════════════ DISPATCH ═════════════════════════════════════════
function run(argv) {
  var op = argv && argv.length ? argv[0] : '';
  var raw;
  try { raw = readStdin(); } catch (e) { return errResp('stdin-read: ' + errString(e), 'internal'); }
  var payload;
  try { payload = raw ? JSON.parse(raw) : {}; }
  catch (e) { return errResp('bad-request-json: ' + errString(e), 'internal'); }
  try {
    var result;
    switch (op) {
      case 'ensure_lists':    result = opEnsureLists(payload); break;
      case 'create':          result = opCreate(payload); break;
      case 'read':            result = opRead(payload); break;
      case 'read_full':       result = opReadFull(payload); break;
      case 'migrate_move':    result = opMigrateMove(payload); break;
      case 'dup_census':      result = opDupCensus(payload); break;
      case 'merge_pull':      result = opMergePull(payload); break;
      case 'dump_incomplete': result = opDumpIncomplete(payload); break;
      case 'find_by_token':   result = opFindByToken(payload); break;
      case 'complete':        result = opComplete(payload); break;
      case 'delete':          result = opDelete(payload); break;
      case 'delete_in_list':  result = opDeleteInList(payload); break;
      case 'complete_in_list': result = opCompleteInList(payload); break;
      default:                return errResp('unknown-op:' + op, 'internal');
    }
    return okResp(result);
  } catch (e) {
    return errResp(errString(e), classifyError(e));
  }
}
