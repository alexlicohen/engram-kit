#!/usr/bin/env python3
"""reminders_sweep — bulk Reminders mutation, ledger-first (the owner).

ONE owner of the "mutate hundreds of reminders at once" pattern. Two modes; everything except the
input parser, the ledger transition and the bridge op is shared:

  --mode sweep    (default)  RETIRE pushed rows the owner struck: ledger `pushed` -> `held` (+`swept_at`),
                             then DELETE the reminder. Input: a hand-edited sweep markdown file
                             (`## <List>` headers, `- [<id8>] (<age>, <owner>) <text>` lines); every
                             remaining line is a target, a pushed row NOT named is untouched.
  --mode complete (stale audit)  CLOSE rows that are already done: ledger `pushed` ->
                             `done` (+`resolved_at`), then mark the reminder COMPLETED (not deleted
                             — completion is what the owner sees in his own Reminders view, and what
                             reconcile/triage-report learn from). Input: a JSON array of
                             `{"id": "task:…", "reminder_id": "x-apple-reminder://…"}` pairs; the
                             `reminder_id` is CROSS-CHECKED against the ledger row and a mismatch
                             refuses the whole run.

    python3 scripts/reminders_sweep.py --file FILE [--mode sweep|complete] [--dry-run]
    python3 scripts/reminders_sweep.py --file FILE [--mode …] --apply [--batch N] [--phase a|b|both]

Two phases, ledger FIRST (a bridge stall must never leave Reminders mutated and the ledger stale):

  PHASE A (no bridge calls) — the ledger transition, plus one `{"event": <mode event>, "reason":
      <mode reason>}` line per row in task-log.jsonl (same shape cmd_push/cmd_reconcile write).
      `reminder_id` is KEPT in both modes. Keeping it is safe: cmd_reconcile only looks at
      `status == "pushed"` rows, so neither a held row whose reminder is gone nor a done row whose
      reminder is completed is ever re-read there. Idempotent — a row already in the target status
      is skipped and logs nothing.

  PHASE B (bridge) — the Reminders mutation, resumable and idempotent because remaining work is
      derived from LIVE Reminders state, never from a memo: each `<op>_in_list` call re-reads the
      list's ids and reports the targeted ids that are no longer there as `not_in_list`. Re-running
      after a timeout simply finds fewer targets. Batches are <=25 ids; a per-batch BridgeError is
      logged and the run continues (`bridge_failures` in the summary).

Why the two `*_in_list` bridge ops: the existing `delete`/`complete` resolve every id app-wide via
byId (~40-100 s PER id — 339 ids would be hours) and `read` deliberately ignores its `lists` hint
(one app-wide pass). `delete_in_list {list, ids}` / `complete_in_list {list, ids}` do ONE
list-scoped bulk id() read, then mutate by index in DESCENDING order (a higher index never shifts a
lower one), re-verifying `obj.id()` immediately before each mutation — so a reminder whose id is
not in the target set can never be touched. Every other bridge op is untouched.

Single-owner reuse: ledger load/write/append + the `_now_iso` stamp come from engram_tasks; the
bridge runner + BridgeError come from _lib.reminders. Nothing is copied.

Summary: `_manifest/tasks-sweep.json` / `_manifest/tasks-stale-audit.json`
(rewritten after every list — a stall leaves an accurate checkpoint). Commits nothing.

Exit codes: 0 ok · 2 unresolved/ambiguous input (nothing written) · 1 phase-B bridge failures.
"""
import argparse
import collections
import json
import os
import re
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from engram_tasks import (load_queue, write_queue_atomic, append_log, _now_iso,  # noqa: E402
                          LOG_REL)
from _lib.reminders import bridge, BridgeError                                   # noqa: E402

SWEEP_REASON = "sweep"
SUMMARY_REL = os.path.join("_manifest", "tasks-sweep.json")
STALE_AUDIT_REASON = "stale-audit"
STALE_AUDIT_SUMMARY_REL = os.path.join("_manifest", "tasks-stale-audit.json")
MAX_BATCH = 25
# A list-scoped id() read plus <=25 index mutations; well under the app-wide byId cost, but
# Reminders is iCloud-variance-prone, so give a batch real headroom before calling it a timeout.
BATCH_TIMEOUT = 240

LINE_RE = re.compile(r"^-\s*\[([^\]]+)\]\s*(.*)$")
HEADER_RE = re.compile(r"^##\s+(.*\S)\s*$")
# The sweep line truncates the row text; match on a prefix long enough to be unique but short
# enough to survive the truncation (observed width ~110 chars).
TEXT_PROBE = 40


# ---------------------------------------------------------------------------
# modes — the ONLY place the two jobs differ
# ---------------------------------------------------------------------------

class Mode(object):
    """One bulk-mutation job. `status`/`stamp`/`event` describe phase A, `op`/`op_key` phase B.

    `resolve_statuses` is what phase-A-already-ran resumption needs: the target status must be
    resolvable too, or a re-run of a half-finished job cannot find its own rows.
    `repush_risk` records whether the target status is in _push_eligible's candidate set
    (open/error/held) — i.e. whether the next `engram_tasks push` could re-create what we removed.
    `done_key`/`status` also name the summary-JSON keys, so each artifact reads in its own verb.
    """

    def __init__(self, name, reason, summary_rel, op, op_key, verb,
                 status, stamp, event, resolve_statuses, done_key, repush_risk):
        self.name, self.reason, self.summary_rel = name, reason, summary_rel
        self.op, self.op_key, self.verb = op, op_key, verb
        self.status, self.stamp, self.event = status, stamp, event
        self.resolve_statuses = resolve_statuses
        self.done_key, self.repush_risk = done_key, repush_risk

    # summary-JSON key names (kept mode-specific so the artifact is self-describing)
    @property
    def k_reminder_ids(self):
        return self.done_key + "_reminder_ids"

    @property
    def k_task_ids(self):
        return self.done_key + "_task_ids"

    @property
    def k_status_ids(self):
        return self.status + "_task_ids"

    @property
    def k_already_status_ids(self):
        return "already_" + self.status + "_task_ids"


MODE_SWEEP = Mode(
    name="sweep", reason=SWEEP_REASON, summary_rel=SUMMARY_REL,
    op="delete_in_list", op_key="deleted", verb="delete",
    status="held", stamp="swept_at", event="held",
    resolve_statuses=("pushed", "held"), done_key="deleted",
    repush_risk=True,          # `held` IS a push candidate — push_policy re-evaluates it every run
)
MODE_COMPLETE = Mode(
    name="complete", reason=STALE_AUDIT_REASON, summary_rel=STALE_AUDIT_SUMMARY_REL,
    op="complete_in_list", op_key="completed", verb="complete",
    status="done", stamp="resolved_at", event="done",
    resolve_statuses=("pushed", "done"), done_key="completed",
    repush_risk=False,         # `done` is not in _push_eligible's (open/error/held) candidate set
)
MODES = {m.name: m for m in (MODE_SWEEP, MODE_COMPLETE)}


# ---------------------------------------------------------------------------
# input parsing + resolution
# ---------------------------------------------------------------------------

def parse_sweep_file(path):
    """[{line_no, id8, list, rest}] for every `- [<id8>] …` line, tagged with its `## <List>`."""
    out = []
    current = None
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            h = HEADER_RE.match(line)
            if h:
                current = h.group(1)
                continue
            m = LINE_RE.match(line)
            if m:
                out.append({"line_no": n, "id8": m.group(1).strip(),
                            "list": current, "rest": m.group(2)})
    return out


def parse_pairs_file(path):
    """JSON `[{id, reminder_id}, …]` -> the same entry shape parse_sweep_file emits.

    The id is FULL (not a truncated prefix) so resolution is exact; `list` is unknown here and is
    taken from the ledger row instead. `reminder_id` rides along so resolve_entries can cross-check
    it against the row — the pair file is generated OUTSIDE this script, and a stale pairing would
    otherwise mutate the wrong reminder."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("pairs file must be a JSON array of {id, reminder_id} objects")
    out = []
    for n, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError("pairs file item {} is not a {{id, reminder_id}} object".format(n))
        out.append({"line_no": n, "id8": item["id"], "list": None, "rest": "",
                    "reminder_id": item.get("reminder_id")})
    return out


def resolve_entries(entries, rows, statuses=("pushed", "held")):
    """(resolved, problems). `resolved` is [(entry, row)] in file order.

    An id8 prefix is NOT unique in this ledger (44 of the 339 collide across two rows), so
    resolution narrows in three deterministic steps and REFUSES anything still ambiguous:
      1. rows whose id starts with the prefix, with a status in `statuses` (the source status OR
         the mode's target status — a resumed run must still resolve rows phase A already flipped);
      2. of those, rows whose `list` equals the sweep file's `## <List>` header;
      3. of those, rows whose `text` prefix appears in the (truncated) sweep line.
    Two sweep lines resolving to the SAME row is also a problem — a silently dropped mutation.
    A `reminder_id` carried on the entry must equal the row's — a mismatch is refused, never
    silently preferred either way."""
    problems = []
    resolved = []
    claimed = {}
    for e in entries:
        cands = [r for r in rows
                 if r.get("id", "").startswith(e["id8"])
                 and r.get("status") in statuses]
        if len(cands) > 1 and e["list"]:
            narrowed = [r for r in cands if r.get("list") == e["list"]]
            if narrowed:
                cands = narrowed
        if len(cands) > 1:
            probe = [r for r in cands
                     if (r.get("text") or "")[:TEXT_PROBE]
                     and (r.get("text") or "")[:TEXT_PROBE] in e["rest"]]
            if probe:
                cands = probe
        if not cands:
            problems.append("line {}: [{}] under '{}' matched NO {} ledger row"
                            .format(e["line_no"], e["id8"], e["list"], "/".join(statuses)))
            continue
        if len(cands) > 1:
            problems.append("line {}: [{}] under '{}' is AMBIGUOUS -> {}"
                            .format(e["line_no"], e["id8"], e["list"],
                                    ", ".join(sorted(r["id"] for r in cands))))
            continue
        row = cands[0]
        if e.get("reminder_id") and row.get("reminder_id") != e["reminder_id"]:
            problems.append("line {}: [{}] reminder_id MISMATCH — input {} vs ledger {}"
                            .format(e["line_no"], e["id8"], e["reminder_id"],
                                    row.get("reminder_id")))
            continue
        if row["id"] in claimed:
            problems.append("line {}: [{}] resolves to {} which line {} already claimed"
                            .format(e["line_no"], e["id8"], row["id"], claimed[row["id"]]))
            continue
        claimed[row["id"]] = e["line_no"]
        resolved.append((e, row))
    return resolved, problems


# ---------------------------------------------------------------------------
# summary file
# ---------------------------------------------------------------------------

def load_summary(root, mode=MODE_SWEEP):
    """The existing summary, or {}. Seeding from it is what makes a SEPARATE `--phase b` run (or a
    resumed one) additive instead of destructive: without it a phase-b invocation would rewrite the
    file with an empty phase-A record and an empty done-set to resume from."""
    path = os.path.join(root, mode.summary_rel)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def marked_ids(root, mode=MODE_SWEEP):
    """Ids this job's phase A transitioned, read from the append-only task-log — the authoritative
    record, independent of which invocation ran phase A."""
    path = os.path.join(root, LOG_REL)
    out = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == mode.event and e.get("reason") == mode.reason and e.get("id"):
                out.add(e["id"])
    return out


def write_summary(root, summary, mode=MODE_SWEEP):
    path = os.path.join(root, mode.summary_rel)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# PHASE A — ledger
# ---------------------------------------------------------------------------

def phase_a(root, rows, targets, mode=MODE_SWEEP):
    """Flip each target to the mode's status (reminder_id kept) and log it. Idempotent.

    Returns {"marked": [...ids flipped now...], "already": [...]}"""
    now = _now_iso()
    log_entries = []
    marked, already = [], []
    for r in targets:
        if r.get("status") == mode.status:
            already.append(r["id"])
            continue
        r["status"] = mode.status
        r[mode.stamp] = now
        log_entries.append({"ts": now, "id": r["id"], "event": mode.event,
                            "reason": mode.reason})
        marked.append(r["id"])
    if log_entries:
        write_queue_atomic(root, rows)
        append_log(root, log_entries)
    return {"marked": marked, "already": already}


# ---------------------------------------------------------------------------
# PHASE B — the Reminders mutation
# ---------------------------------------------------------------------------

def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def mutate_batch(list_name, ids, mode=MODE_SWEEP, runner=None, timeout=BATCH_TIMEOUT):
    """One `<op>_in_list` call. Returns (done, not_in_list, errors) — never raises for a per-item
    failure; a BridgeError propagates to the caller, which logs and continues.

    `already_completed` (complete mode only) counts as DONE: the reminder is already in the target
    state, so leaving it pending would strand it in `not_found_task_ids`. It is also recorded
    separately by the caller so the summary never claims a change it did not make."""
    res = bridge(mode.op, {"list": list_name, "ids": list(ids)},
                 runner=runner, timeout=timeout) or {}
    return (list(res.get(mode.op_key, [])),
            list(res.get("not_in_list", [])),
            list(res.get("errors", [])),
            list(res.get("already_completed", [])))


def phase_b(root, targets, summary, mode=MODE_SWEEP, batch=MAX_BATCH, runner=None, verbose=True):
    """Mutate every target's reminder, list by list, then re-try leftovers in the other targeted
    lists (a reminder the owner dragged elsewhere is otherwise silently left behind).

    Remaining work is derived from LIVE state on every call: `not_in_list` already covers
    "handled by an earlier run", so a re-run after a stall is safe and converges."""
    summary.setdefault(mode.k_reminder_ids, [])
    summary.setdefault(mode.k_task_ids, [])
    summary.setdefault("already_done_reminder_ids", [])
    by_list = collections.OrderedDict()
    rid_of = {}
    for r in targets:
        rid = r.get("reminder_id")
        if not rid:
            summary["no_reminder_id"].append(r["id"])
            continue
        rid_of[rid] = r["id"]
        by_list.setdefault(r.get("list") or "", []).append(rid)

    done = set(summary[mode.k_reminder_ids])
    already = set(summary["already_done_reminder_ids"])
    pending = {rid for rid in rid_of if rid not in done}

    def run_pass(list_name, rids, label):
        got_any = False
        for k, chunk in enumerate(_chunks(rids, batch), start=1):
            try:
                d, nil, errs, alr = mutate_batch(list_name, chunk, mode=mode, runner=runner)
            except BridgeError as e:
                msg = "{} batch {}: {}: {}".format(list_name, k, e.code, e.detail)
                summary["bridge_failures"].append(msg)
                if verbose:
                    print("  ! {}  (logged; continuing — re-run to finish)".format(msg))
                continue
            for rid in list(d) + list(alr):
                done.add(rid)
                pending.discard(rid)
            already.update(alr)
            # `not_in_list` is deliberately NOT treated as done: it means "not in THIS list",
            # which is either already-handled or moved by hand. Only the rescan pass can tell
            # them apart, and an id that survives every pass ends up in not_found_task_ids.
            for er in errs:
                summary["item_errors"].append({"list": list_name, **er})
            got_any = got_any or bool(d)
            if verbose:
                print("  {} [{}] batch {}: {} {} · already {} · not-in-list {} · errors {}".format(
                    list_name, label, k, mode.op_key, len(d), len(alr), len(nil), len(errs)))
            summary[mode.k_reminder_ids] = sorted(done)
            summary["already_done_reminder_ids"] = sorted(already)
            write_summary(root, summary, mode)
        return got_any

    for list_name, rids in by_list.items():
        todo = [rid for rid in rids if rid in pending]
        if not todo:
            continue
        if verbose:
            print("list {!r}: {} to {}".format(list_name, len(todo), mode.verb))
        run_pass(list_name, todo, "home")

    # leftovers: still pending after their own list's pass ⇒ either already gone, or moved by hand.
    leftovers = sorted(pending - done)
    if leftovers:
        others = [ln for ln in by_list if ln]
        if verbose:
            print("rescanning {} leftover id(s) across {} other list(s)".format(
                len(leftovers), len(others)))
        for list_name in others:
            still = [rid for rid in leftovers if rid in pending]
            if not still:
                break
            run_pass(list_name, still, "rescan")

    summary[mode.k_reminder_ids] = sorted(done)
    summary["already_done_reminder_ids"] = sorted(already)
    summary["not_found_task_ids"] = sorted(rid_of[rid] for rid in rid_of if rid not in done)
    summary[mode.k_task_ids] = sorted(rid_of[rid] for rid in done if rid in rid_of)
    write_summary(root, summary, mode)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None, runner=None):
    ap = argparse.ArgumentParser(description="Bulk Apple Reminders mutation (see module docstring).")
    ap.add_argument("--file", required=True,
                    help="sweep markdown file (--mode sweep) or {id, reminder_id} JSON array "
                         "(--mode complete)")
    ap.add_argument("--mode", choices=sorted(MODES), default="sweep")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS) if os.path.basename(_SCRIPTS) == "scripts"
                    else ".", help="store root (default: the repo containing this script)")
    ap.add_argument("--apply", action="store_true", help="actually write (default is --dry-run)")
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--phase", choices=["a", "b", "both"], default="both")
    ap.add_argument("--batch", type=int, default=MAX_BATCH)
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    batch = max(1, min(args.batch, MAX_BATCH))
    mode = MODES[args.mode]

    try:
        entries = (parse_pairs_file(args.file) if mode is MODE_COMPLETE
                   else parse_sweep_file(args.file))
    except ValueError as e:
        print("input file rejected: {}".format(e), file=sys.stderr)
        return 2
    rows, _ = load_queue(root)
    resolved, problems = resolve_entries(entries, rows, statuses=mode.resolve_statuses)

    print("=== reminders_sweep --mode {} ===".format(mode.name))
    print("input: {}  ({} item line(s))".format(args.file, len(entries)))
    if problems:
        print("\nUNRESOLVED / AMBIGUOUS — nothing written:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 2

    targets = [r for _, r in resolved]
    per_list = collections.Counter(r.get("list") or "(none)" for r in targets)
    st = collections.Counter(r.get("status") for r in targets)
    print("resolved {} row(s) — status: {}".format(
        len(targets), " · ".join("{} {}".format(k, v) for k, v in sorted(st.items()))))
    for name, n in sorted(per_list.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  {:<28} {}".format(name, n))
    missing_rid = [r["id"] for r in targets if not r.get("reminder_id")]
    if missing_rid:
        print("  ({} row(s) carry no reminder_id — ledger-only)".format(len(missing_rid)))

    # Re-push warning: push_policy() re-evaluates `held` rows on every push, so a held row that
    # still carries a due/priority is recreated in Reminders by the next `push`. Report, never
    # silently work around it (push_policy is the ONE owner of that decision). Not applicable to
    # `--mode complete`: a `done` row is not a push candidate at all (see Mode.repush_risk).
    repushable = []
    if mode.repush_risk:
        from engram_tasks import push_policy
        repushable = [r for r in targets if push_policy(r) == "push"]
    if repushable:
        print("\n⚠ {} swept row(s) would be RE-PUSHED by the next `engram_tasks push` "
              "(push_policy says 'push' — they have a due or a priority):".format(len(repushable)))
        for r in repushable:
            print("    {}  due={} prio={}  {!r}".format(
                r["id"], r.get("due"), r.get("priority"), (r.get("text") or "")[:60]))

    if not args.apply:
        print("\n--dry-run: nothing written. Re-run with --apply.")
        return 0

    # Seed from any existing summary so `--phase a` then `--phase b` (or a resumed phase B) is
    # ADDITIVE: phase B resumes from the done-set, and phase A's record survives.
    prior = load_summary(root, mode)
    summary = {
        "mode": mode.name,
        "reason": mode.reason,
        "input_file": os.path.abspath(args.file),
        "generated_at": _now_iso(),
        "resolved_task_ids": sorted(r["id"] for r in targets),
        mode.k_status_ids: [],
        mode.k_already_status_ids: prior.get(mode.k_already_status_ids, []),
        mode.k_reminder_ids: prior.get(mode.k_reminder_ids, []),
        mode.k_task_ids: prior.get(mode.k_task_ids, []),
        "already_done_reminder_ids": prior.get("already_done_reminder_ids", []),
        "not_found_task_ids": prior.get("not_found_task_ids", []),
        "no_reminder_id": [],
        "bridge_failures": prior.get("bridge_failures", []),
        "item_errors": prior.get("item_errors", []),
        "repush_risk_task_ids": sorted(r["id"] for r in repushable),
    }

    if args.phase in ("a", "both"):
        a = phase_a(root, rows, targets, mode)
        summary[mode.k_already_status_ids] = sorted(
            set(summary[mode.k_already_status_ids]) | set(a["already"]))
        write_summary(root, summary, mode)
        print("\nPHASE A: {} row(s) pushed->{} (+{} already {}). reminder_id kept.".format(
            len(a["marked"]), mode.status, len(a["already"]), mode.status))

    # Always sourced from the task-log, never from this invocation — so a phase-b-only run reports
    # the same transitioned set as a --phase both run.
    target_ids = {r["id"] for r in targets}
    summary[mode.k_status_ids] = sorted(marked_ids(root, mode) & target_ids)
    write_summary(root, summary, mode)

    if args.phase in ("b", "both"):
        print("\nPHASE B: {} in Reminders (batch<={} per call)".format(mode.verb, batch))
        phase_b(root, targets, summary, mode=mode, batch=batch, runner=runner)
        print("\nPHASE B done: {} {} · already-in-state {} · not-found-in-Reminders {} · "
              "bridge failures {} · item errors {}".format(
                  mode.op_key, len(summary[mode.k_reminder_ids]),
                  len(summary["already_done_reminder_ids"]),
                  len(summary["not_found_task_ids"]),
                  len(summary["bridge_failures"]),
                  len(summary["item_errors"])))

    print("summary: {}".format(os.path.join(root, mode.summary_rel)))
    return 1 if (summary["bridge_failures"] or summary["item_errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
