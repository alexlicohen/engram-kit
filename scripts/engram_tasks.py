#!/usr/bin/env python3
"""engram-tasks — the two-way Apple Reminders task bridge CLI. Deterministic, cron-safe CLI over
the committed ledger `_manifest/task-queue.jsonl`. Which Reminders lists a task lands in is
CONFIGURATION: `_manifest/constants/reminders.json` (see apply_config below).

  Phase 1 (parse + ledger):     extract · status
  Phase 3 (push / reconcile):   push · reconcile · sync · doctor
  Phase 4 (completion suggest):  suggest-complete · complete

    python3 scripts/engram_tasks.py extract [--only FILE | --since DATE | --full | --seed] [--dry-run] [--root PATH]
    python3 scripts/engram_tasks.py status  [--root PATH]
    python3 scripts/engram_tasks.py push    [--dry-run] [--limit N] [--policy on|off] [--adjudicated ID=DEC …] [--route ID=LIST …] [--route-override LIST] [--dump-cache FILE]
    python3 scripts/engram_tasks.py reconcile [--force]
    python3 scripts/engram_tasks.py sync    [--force]
    python3 scripts/engram_tasks.py doctor  [--live]
    python3 scripts/engram_tasks.py suggest-complete (--only FILE | --add-json FILE) [--emit] [--dump-cache FILE]
    python3 scripts/engram_tasks.py complete --ids sid1,sid2 [--reject sid3,…]

Push policy: EXTRACTION IS UNCHANGED — the ledger keeps every item — but only the
rows `push_policy()` approves reach Reminders: `mine` rows with a due date or a real priority. `waiting_on` rows (other people's tasks) and undated/un-prioritized "project state"
lines get status `held` — not `open` (never retried as noise), not `dismissed` (the owner never saw
them). Held rows are re-checked on every push. `push --policy off` restores the old behaviour.

Row statuses: open · held · pushed · needs_review · skipped · done · dismissed · error.
`needs_review` (a live near-dup the deterministic push refused to create) is NOT terminal — it is
resolved only by `push --adjudicated <id>=create|skip|adopt:<rid>`; `skip` writes the terminal
`skipped` + `adjudicated_at` (a counter-only skip was the 2026-09-06 stuck-needs_review bug).

Single-owner reuse: note iteration/frontmatter via _lib.notes; provenance ref via
_lib.provenance; the grammar/due-resolver/owner-classifier/open-loops-parser/similarity all live
in _lib.actions (do NOT paste a local copy here); the JXA bridge in _lib.reminders (bridge()).

Ledger files (all under `_manifest/`, committed):
  task-queue.jsonl       — current state, one row per task id (rewritten atomically each run)
  task-log.jsonl         — append-only transition log
  task-suggestions.jsonl — completion suggestions (rewritten-state, one row per sid)
  tasks-state.json       — extraction watermark (`last_extract_note_date`) + `seeded` flag

Exit codes: 0 ok · 1 warnings · 2 usage · 3 bridge failure (BridgeError; ZERO ledger transitions)
· 4 abort-without-writes (corrupt ledger OR reconcile mass-dismissal valve).
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.notes import iter_notes, split_frontmatter, get_field, note_slug, section  # noqa: E402
from _lib.provenance import parse_ref                                                # noqa: E402
from _lib.actions import (parse_action_line, task_id, self_aliases, owner_class,      # noqa: E402
                          DOMAIN_BY_SECTION,                                       # noqa: E402
                           parse_openloop_line, is_resolved_marker, normalize_text,
                           resolved_action_text, title_ratio, NEAR_DUP_RATIO)
from _lib.reminders import bridge, BridgeError, doctor, T_DUMP, DEFAULT_TIMEOUT       # noqa: E402
from _lib.constants import DOMAINS                                                    # noqa: E402

QUEUE_REL = os.path.join("_manifest", "task-queue.jsonl")
LOG_REL = os.path.join("_manifest", "task-log.jsonl")
SUGGEST_REL = os.path.join("_manifest", "task-suggestions.jsonl")
STATE_REL = os.path.join("_manifest", "tasks-state.json")
OPENLOOPS_REL = os.path.join("digests", "open-loops.md")

# ── Routing — the ONE owner of domain→list. Never re-hardcode this table anywhere else. The
#    values come from `_manifest/constants/reminders.json` (apply_config); the defaults below
#    are what a store without that file gets. ───────────────────────────────────────────────────
#
#   inbox_list       fallback list for domains with no owned list (infra/unsorted/unknown)
#   waiting_list     fallback for waiting_on rows whose domain has no owned list
#   waiting_suffix   a waiting_on row routes to its domain's "<Owned><suffix>" sibling, so each
#                    topic's owned + waiting lists pair in the sidebar (triage by TOPIC, not by
#                    owner-class). Derived, never hand-listed.
#   domain_to_list   domain -> owned list (several domains may share one list)
#   entity_rules     [{"list", "pattern"}]: finance "mine" tasks that clearly belong to one of the
#                    owner's entities (an LLC, a trust) route straight to that entity's list. First
#                    match wins; conservative patterns only — anything ambiguous falls through to
#                    the general finance list (in-session /engram-sync can re-route via --route).
#                    Applied ONLY to owner_class == "mine" (waiting_on rows are entity-blind).
#   never_write      lists engram must NEVER create a reminder in (e.g. a billing or hours LOG
#                    that lives in Reminders: a to-do there would corrupt the record). Enforced by
#                    _assert_writable() at create time.
#   extra_lists      hand-managed lists engram never routes to but where tracked items get dragged
#                    by hand: keeping them in the bulk-read scope avoids a slow per-item app-wide
#                    lookup at reconcile time.
TASKS_CONFIG_REL = os.path.join("_manifest", "constants", "reminders.json")
DEFAULT_TASKS_CONFIG = {
    "inbox_list": "Engram Inbox",
    "waiting_list": "Waiting On",
    "waiting_suffix": " — Waiting On",
    "domain_to_list": {"research": "Research", "work": "Work", "ventures": "Ventures",
                       "finance": "Financial", "personal": "Personal", "health": "Personal"},
    "entity_rules": [],
    "never_write": [],
    "extra_lists": [],
}


def apply_config(cfg):
    """Install a routing config (module globals; tests install their own). Unknown keys or a
    domain not in _manifest/constants/domains.json fail loudly — a typo must never silently route
    a whole domain to the inbox."""
    global INBOX_LIST, WAITING_LIST, WAITING_SUFFIX, DOMAIN_TO_LIST, DOMAIN_TO_WAITING_LIST
    global WAITING_LISTS, ENTITY_RULES, NEVER_WRITE, ENGRAM_LISTS
    c = dict(DEFAULT_TASKS_CONFIG)
    unknown = set(cfg) - set(c) - {"_comment"}
    if unknown:
        raise ValueError("reminders config: unknown key(s) {}".format(sorted(unknown)))
    c.update({k: v for k, v in cfg.items() if k != "_comment"})
    bad = sorted(set(c["domain_to_list"]) - set(DOMAINS))
    if bad:
        raise ValueError("reminders config: domain_to_list names unknown domain(s) {}".format(bad))
    INBOX_LIST = c["inbox_list"]
    WAITING_LIST = c["waiting_list"]
    WAITING_SUFFIX = c["waiting_suffix"]
    DOMAIN_TO_LIST = dict(c["domain_to_list"])
    # Derived, never hand-edited: the waiting sibling of each owned list (dict dedupes shared lists).
    DOMAIN_TO_WAITING_LIST = {dom: lst + WAITING_SUFFIX for dom, lst in DOMAIN_TO_LIST.items()}
    WAITING_LISTS = sorted(set(DOMAIN_TO_WAITING_LIST.values()))
    ENTITY_RULES = [(r["list"], re.compile(r["pattern"], re.I)) for r in c["entity_rules"]]
    NEVER_WRITE = frozenset(c["never_write"])
    # The engram-routable lists — the scope hint for find_by_token (push step 1) and read (reconcile).
    scope = [INBOX_LIST, WAITING_LIST, *WAITING_LISTS, *DOMAIN_TO_LIST.values(),
             *(lst for lst, _ in ENTITY_RULES), *c["extra_lists"]]
    ENGRAM_LISTS = list(dict.fromkeys(scope))


def load_tasks_config(root=os.path.dirname(_SCRIPTS)):
    path = os.path.join(root, TASKS_CONFIG_REL)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


apply_config(load_tasks_config())


def _ref_kind(ref):
    """Single owner of ref→source_kind (granola/gmail/outlook/imessage/whatsapp/openloops/note —
    'note' is the fallback ref minted for notes without provenance). Used by BOTH the extract-time
    skip rules and triage-report, so a rule copied from a report line always matches."""
    return (ref or "?").split(":", 1)[0]


# Extraction-precision rules learned from the owner's triage (delete=dismissed, unflag=confirmed —
# see `triage-report`). A candidate action line matching ANY rule is skipped at extract time
# (never enters the ledger) — INCLUDING the --seed path (source_kind "openloops"). Every present
# key must match: source_kind (see _ref_kind), domain, owner_class, text_re (search, case-insens).
# Add rules ONLY from observed dismissal patterns, each with a date + rationale comment + a test.
# Schema is VALIDATED at extract time (_validate_skip_rules): unknown keys / no criteria / bad
# regex ⇒ exit 2 before any scan (a typo'd rule must never vacuously match everything).
EXTRACT_SKIP_RULES = [
    # (from `triage-report` over the owner's dismissals): staff/logistics record-KEEPING
    # notation assigned to someone other than the owner — pure bookkeeping into a passive store
    # ("Note in calendar that the owner is away March 3-17" / "Note the … replacement in the
    # config sheet"). Nothing is delivered back to the owner and there is no next step for them, so
    # a "waiting on X" reminder is noise. Scoped to owner_class "waiting_on" so a notation the owner
    # assigns THEMSELF is never dropped; verb-anchored (^note/add/log/record/mark) + a passive-store
    # target so it cannot hit real waiting-on items — verified against the KEPT set, e.g.
    # "Add meeting minutes … to Dropbox" (wrong target) and "Send help-desk triage
    # spreadsheet" (wrong verb) both survive.
    {"name": "calendar-logistics-notation",
     "owner_class": "waiting_on",
     "text_re": r"^\s*(note|add|log|record|mark)\b.*\b(calendar|config\s+sheet|config\s+spreadsheet|the\s+tracker|the\s+system)\b"},
]


_SKIP_RULE_KEYS = {"name", "source_kind", "domain", "owner_class", "text_re"}


def _validate_skip_rules(rules):
    """Fail-loud schema check. A rule with only mistyped keys would otherwise have ZERO active
    criteria and match every candidate — silently suppressing all extraction while the watermark
    advances past the skipped notes."""
    errs = []
    for i, ru in enumerate(rules):
        unknown = set(ru) - _SKIP_RULE_KEYS
        if unknown:
            errs.append("skip-rule {} ({}): unknown key(s) {}".format(
                i, ru.get("name", "unnamed"), sorted(unknown)))
        if not (set(ru) & (_SKIP_RULE_KEYS - {"name"})):
            errs.append("skip-rule {} ({}): no criteria".format(i, ru.get("name", "unnamed")))
        if ru.get("text_re"):
            try:
                re.compile(ru["text_re"])
            except re.error as e:
                errs.append("skip-rule {} ({}): bad text_re: {}".format(
                    i, ru.get("name", "unnamed"), e))
    return errs


def _skip_rule_hit(rule, *, source_kind, domain, oclass, text):
    if rule.get("source_kind") and rule["source_kind"] != source_kind:
        return False
    if rule.get("domain") and rule["domain"] != domain:
        return False
    if rule.get("owner_class") and rule["owner_class"] != oclass:
        return False
    if rule.get("text_re") and not re.search(rule["text_re"], text, re.I):
        return False
    return True


TOKEN_PREFIX = "engram-task: "       # re-link key: first notes line, matched by find_by_token
WAITING_NUDGE_DAYS = 7               # a due-less waiting-on row gets a synthetic +7d follow-up nudge


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def load_queue(root):
    """Return (rows: list[dict] in file order, by_id: dict[id -> row]). Exits 4 on a corrupt
    (unparseable JSON) line — prints the offending line number, never rewrites anything."""
    path = os.path.join(root, QUEUE_REL)
    rows = []
    by_id = {}
    if not os.path.exists(path):
        return rows, by_id
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"CORRUPT LEDGER: {QUEUE_REL} line {i}: {e}", file=sys.stderr)
                print(f"  offending line: {line!r}", file=sys.stderr)
                sys.exit(4)
            rid = row.get("id")
            rows.append(row)
            if rid:
                by_id[rid] = row
    return rows, by_id


def write_queue_atomic(root, rows):
    path = os.path.join(root, QUEUE_REL)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_log(root, entries):
    if not entries:
        return
    path = os.path.join(root, LOG_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def load_state(root):
    path = os.path.join(root, STATE_REL)
    if not os.path.exists(path):
        return {"last_extract_note_date": None, "seeded": False}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_state(root, state):
    path = os.path.join(root, STATE_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def load_suggestions(root):
    """Return (rows, by_sid) for task-suggestions.jsonl. Rewritten-state (one row per sid, like
    task-queue). A malformed line is SKIPPED (not fatal) — the LLM may hand-append records."""
    path = os.path.join(root, SUGGEST_REL)
    rows, by_sid = [], {}
    if not os.path.exists(path):
        return rows, by_sid
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = row.get("sid")
            rows.append(row)
            if sid:
                by_sid[sid] = row
    return rows, by_sid


def write_suggestions_atomic(root, rows):
    path = os.path.join(root, SUGGEST_REL)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _new_row(*, tid, ref, slug, note_path, line, text, owner, oclass, unowned,
             due, due_raw, source, domain=None, due_approx=False):
    now = _now_iso()
    row = {
        "id": tid, "ref": ref, "slug": slug, "note_path": note_path,
        "domain": domain or _domain_of(note_path),
        "line": line, "text": text,
        "owner": owner, "owner_class": oclass, "unowned": unowned,
        "due": due, "due_raw": due_raw, "due_auto": None, "priority": None,
        "status": "open", "list": None, "reminder_id": None, "source": source,
        "adopted": False,
        "created": now, "pushed_at": None, "resolved_at": None, "last_seen": None,
    }
    if due_approx:
        row["due_approx"] = True  # `due` approximates a vague phrase in due_raw ("next week")
    return row


def _domain_of(note_path):
    return note_path.split(os.sep, 1)[0] if note_path else "?"


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def _select_notes(root, mode, only_file, since_date):
    if mode == "only":
        with open(only_file, encoding="utf-8") as f:
            paths = [ln.strip() for ln in f if ln.strip()]
        return [p for p in paths if os.path.exists(os.path.join(root, p))]
    if mode == "full":
        return list(iter_notes())
    if mode == "since":
        out = []
        for p in iter_notes():
            with open(p, encoding="utf-8", errors="replace") as f:
                head, _ = split_frontmatter(f.read())
            if head is None:
                continue
            d = _parse_date(get_field(head, "date"))
            if d and d >= since_date:
                out.append(p)
        return out
    return []


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def cmd_extract(args, root):
    modes = [m for m in ("only", "since", "full", "seed") if getattr(args, m)]
    if len(modes) != 1:
        print("extract: pass exactly one of --only / --since / --full / --seed", file=sys.stderr)
        return 2

    mode = modes[0]
    since_date = None
    if mode == "since":
        since_date = _parse_date(args.since)
        if since_date is None:
            print(f"extract: --since expects YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2

    rule_errs = _validate_skip_rules(EXTRACT_SKIP_RULES)
    if rule_errs:
        for e in rule_errs:
            print("extract: {}".format(e), file=sys.stderr)
        print("extract: refusing to run with malformed EXTRACT_SKIP_RULES (no scan, no "
              "watermark change)", file=sys.stderr)
        return 2

    rows, by_id = load_queue(root)  # exits 4 internally on corrupt ledger
    aliases = self_aliases(root)

    new_rows = []
    log_entries = []
    skipped_by_rule = collections.Counter()
    warnings = 0
    max_note_date = None
    scanned = 0

    if mode == "seed":
        ol_path = os.path.join(root, OPENLOOPS_REL)
        if os.path.exists(ol_path):
            cur_domain = "unsorted"   # open-loops `##` heading -> area domain (routes seeds)
            with open(ol_path, encoding="utf-8") as f:
                for raw in f:
                    if raw.startswith("## "):
                        cur_domain = DOMAIN_BY_SECTION.get(raw[3:].strip(), "unsorted")
                        continue
                    parsed = parse_openloop_line(raw)
                    if parsed is None:
                        continue
                    scanned += 1
                    if is_resolved_marker(parsed["line"]) or is_resolved_marker(parsed["status"]):
                        continue
                    if len(normalize_text(parsed["text"])) < 8:
                        continue
                    ref = "openloops:" + (parsed["slug"] or "unfiled")
                    tid = task_id(ref, parsed["text"])
                    if tid in by_id:
                        continue
                    oclass = owner_class(parsed["owner"], aliases)
                    hit = next((ru for ru in EXTRACT_SKIP_RULES
                                if _skip_rule_hit(ru, source_kind=_ref_kind(ref),
                                                  domain=cur_domain, oclass=oclass,
                                                  text=parsed["text"])), None)
                    if hit is not None:
                        skipped_by_rule[hit.get("name", "unnamed")] += 1
                        continue
                    row = _new_row(tid=tid, ref=ref, slug=parsed["slug"],
                                    note_path=OPENLOOPS_REL, line=parsed["line"],
                                    text=parsed["text"], owner=parsed["owner"], oclass=oclass,
                                    unowned=parsed["unowned"], due=parsed["due"],
                                    due_raw=parsed["due_raw"], source="open-loops",
                                    domain=cur_domain)
                    new_rows.append(row)
                    log_entries.append({"ts": row["created"], "id": tid, "event": "extracted",
                                        "source": "open-loops"})
    else:
        note_paths = _select_notes(root, mode, getattr(args, "only", None), since_date)
        for p in note_paths:
            scanned += 1
            try:
                with open(os.path.join(root, p) if not os.path.isabs(p) else p,
                          encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError as e:
                print(f"WARN: could not read {p}: {e}", file=sys.stderr)
                warnings += 1
                continue
            head, body = split_frontmatter(txt)
            if head is None:
                print(f"WARN: {p} has no valid frontmatter — skipped", file=sys.stderr)
                warnings += 1
                continue
            note_date = _parse_date(get_field(head, "date"))
            if note_date and (max_note_date is None or note_date > max_note_date):
                max_note_date = note_date
            ref = parse_ref(head) or ("note:" + p)
            slug = note_slug(p)
            for line in section(body, "Action items"):
                parsed = parse_action_line(line, note_date)
                if parsed is None:
                    continue
                tid = task_id(ref, parsed["text"])
                if tid in by_id:
                    continue
                oclass = owner_class(parsed["owner"], aliases)
                hit = next((ru for ru in EXTRACT_SKIP_RULES
                            if _skip_rule_hit(ru, source_kind=_ref_kind(ref),
                                              domain=_domain_of(p), oclass=oclass,
                                              text=parsed["text"])), None)
                if hit is not None:
                    skipped_by_rule[hit.get("name", "unnamed")] += 1
                    continue
                row = _new_row(tid=tid, ref=ref, slug=slug, note_path=p,
                                line=parsed["line"], text=parsed["text"], owner=parsed["owner"],
                                oclass=oclass, unowned=parsed["unowned"], due=parsed["due"],
                                due_raw=parsed["due_raw"], source="note",
                                due_approx=parsed.get("due_approx", False))
                new_rows.append(row)
                log_entries.append({"ts": row["created"], "id": tid, "event": "extracted",
                                    "source": "note", "note_path": p})
                by_id[tid] = row  # guard against dup lines within this same run

    if args.dry_run:
        _print_report(mode, scanned, new_rows, warnings, skipped_by_rule)
        return 1 if warnings else 0

    if new_rows:
        write_queue_atomic(root, rows + new_rows)
        append_log(root, log_entries)

    state = load_state(root)
    if mode == "seed":
        state["seeded"] = True
    if max_note_date is not None:
        prev = state.get("last_extract_note_date")
        prev_d = _parse_date(prev) if prev else None
        if prev_d is None or max_note_date > prev_d:
            state["last_extract_note_date"] = max_note_date.isoformat()
    write_state(root, state)

    skipnote = ""
    if skipped_by_rule:
        skipnote = " · skipped by rule: " + ", ".join(
            f"{k}={v}" for k, v in sorted(skipped_by_rule.items()))
    print(f"extract ({mode}): scanned {scanned}, {len(new_rows)} new task(s), "
          f"{len(rows)} already in ledger, {warnings} warning(s).{skipnote}")
    return 1 if warnings else 0


def _print_report(mode, scanned, new_rows, warnings, skipped_by_rule=None):
    print(f"=== engram_tasks extract --dry-run (mode={mode}) ===")
    print(f"scanned: {scanned}")
    print(f"would create: {len(new_rows)} new task(s)  ({warnings} warning(s))")
    if skipped_by_rule:
        # the preview MUST show what a rule would eat — an over-broad rule is otherwise
        # indistinguishable from "no new tasks" in exactly the mode meant to catch it
        print("skipped by rule: " + ", ".join(
            f"{k}={v}" for k, v in sorted(skipped_by_rule.items())))

    by_domain = collections.Counter(r.get("domain") or _domain_of(r["note_path"]) for r in new_rows)
    by_oclass = collections.Counter(r["owner_class"] for r in new_rows)

    print("\nBy domain:")
    for dom, n in sorted(by_domain.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {dom}: {n}")

    print("\nBy owner_class:")
    for oc, n in sorted(by_oclass.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {oc}: {n}")

    print("\nSample rows:")
    for row in new_rows[:15]:
        due = row["due"] or (row["due_raw"] and f"~{row['due_raw']}") or "—"
        print(f"  [{row.get('domain') or _domain_of(row['note_path'])}] due={due} owner_class={row['owner_class']} "
              f":: {row['text'][:90]!r}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args, root):
    rows, _ = load_queue(root)  # exits 4 internally on corrupt ledger
    state = load_state(root)
    today = datetime.date.today()

    by_status = collections.Counter(r.get("status") for r in rows)
    by_oclass = collections.Counter(r.get("owner_class") for r in rows)
    by_list = collections.Counter(r.get("list") for r in rows)
    overdue = 0
    for r in rows:
        d = _parse_date(r.get("due"))
        if d and d < today and r.get("status") not in ("done", "dismissed", "skipped"):
            overdue += 1

    print(f"total: {len(rows)}")
    print("by status:")
    for k, n in sorted(by_status.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {k}: {n}")
    held = [r for r in rows if r.get("status") == "held"]
    if held:
        hr = collections.Counter(_hold_reason(r) for r in held)
        print("held (in the ledger, never pushed — push_policy): {} · {}".format(
            len(held), " · ".join("{} {}".format(k, n) for k, n in sorted(hr.items()))))
    print("by owner_class:")
    for k, n in sorted(by_oclass.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {k}: {n}")
    print("by list:")
    for k, n in sorted(by_list.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {k}: {n}")
    print(f"overdue: {overdue}")
    print(f"last_extract_note_date: {state.get('last_extract_note_date')}")
    print(f"seeded: {state.get('seeded', False)}")
    return 0


# ---------------------------------------------------------------------------
# Reminder mapping (routing table, priority, title truncation, notes contract)
# ---------------------------------------------------------------------------

def route_list(row, override=None, route_map=None):
    """The list a row is pushed into — the ONLY place routing is decided. Precedence:
    per-task `--route task:<id>=<List>` (the in-session judgment layer) → test-only
    --route-override → waiting_on → per-domain "<Owned> — Waiting On" (fallback: Waiting On)
    → finance entity rules (mine only) → domain table → Engram Inbox."""
    if route_map and row.get("id") in route_map:
        return route_map[row["id"]]
    if override:
        return override
    dom = row.get("domain") or _domain_of(row.get("note_path") or "")
    if row.get("owner_class") == "waiting_on":
        return DOMAIN_TO_WAITING_LIST.get(dom, WAITING_LIST)
    if dom == "finance":
        hay = "{} {}".format(row.get("text") or "", row.get("slug") or "")
        for lst, rx in ENTITY_RULES:
            if rx.search(hay):
                return lst
    return DOMAIN_TO_LIST.get(dom, INBOX_LIST)


def _assert_writable(list_name):
    """Guard: automation must never CREATE in a never-write list. route_list() can only return a
    never-write name via a bogus --route-override, so this is a belt-and-suspenders assertion."""
    if list_name in NEVER_WRITE:
        raise ValueError("refusing to create in never-write list {!r}".format(list_name))


def compute_priority(due_iso, today):
    """overdue/≤3d → 1 (high), ≤14d → 5 (medium), else 0 (none). `due_iso` is the EFFECTIVE due
    (source due, or the +7d nudge for a due-less waiting-on row)."""
    d = _parse_date(due_iso)
    if d is None:
        return 0
    delta = (d - today).days
    if delta <= 3:
        return 1
    if delta <= 14:
        return 5
    return 0


def truncate_title(text, limit=120):
    """Task text → reminder title, truncated at a word boundary within `limit` chars (an ellipsis
    is appended when truncation happened; it does not count toward the limit)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip()
    if not cut:                       # a single > limit-char token: hard cut
        cut = text[:limit]
    return cut + "…"


def build_notes(row, due_auto=None):
    """The reminder notes-field contract, COMPACT (2026-07-07, the owner: metadata was eating phone
    screen space). Line 1 is ALWAYS `engram-task: <id>` followed by ` · <slug>` — the bridge
    matchers take the first whitespace-delimited token after the prefix, so the id stays the
    machine key. Line 2 (only when there is something to say): owner (waiting-on) / unresolved
    due phrasing / nudge label. ref + original line live in the LEDGER, not here."""
    first = "{}{}".format(TOKEN_PREFIX, row["id"])
    if row.get("slug"):
        first += " · {}".format(row["slug"])
    ctx = []
    if row.get("owner_class") == "waiting_on" and row.get("owner"):
        ctx.append("waiting on {}".format(row["owner"]))
    if not row.get("due") and row.get("due_raw"):
        ctx.append("due said: {}".format(row["due_raw"]))
    elif row.get("due_approx") and row.get("due_raw"):
        ctx.append("due said: {} (date approximate)".format(row["due_raw"]))
    if due_auto:
        ctx.append("+{}d nudge (no deadline in source)".format(WAITING_NUDGE_DAYS))
    return first + ("\n" + " · ".join(ctx) if ctx else "")


def _effective_due(row, today):
    """(effective_due_iso, due_auto_iso_or_None). A due-less waiting-on row gets a synthetic
    +Nd nudge (the ONLY permitted non-source date, and labeled as such); it is returned as
    due_auto and NEVER written to the ledger `due` field (source-due vs nudge stay distinguishable)."""
    if row.get("due"):
        return row["due"], None
    if row.get("owner_class") == "waiting_on":
        nudge = (today + datetime.timedelta(days=WAITING_NUDGE_DAYS)).isoformat()
        return nudge, nudge
    return None, None


# ---------------------------------------------------------------------------
# push — dedup (find_by_token repair + dump adopt/near-review) then batch create
# ---------------------------------------------------------------------------

def _parse_adjudicated(values):
    """Parse repeatable `--adjudicated ID=DECISION` tokens (also comma-joined) into
    {task_id: decision}, decision ∈ {"create", "skip", ("adopt", reminder_id)}. Returns
    (map, errors)."""
    out, errs = {}, []
    for chunk in values or []:
        for tok in chunk.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if "=" not in tok:
                errs.append("adjudicated token missing '=': {!r}".format(tok))
                continue
            tid, dec = tok.split("=", 1)
            tid, dec = tid.strip(), dec.strip()
            if dec in ("create", "skip"):
                out[tid] = dec
            elif dec.startswith("adopt:"):
                out[tid] = ("adopt", dec[len("adopt:"):].strip())
            else:
                errs.append("adjudicated decision must be create|skip|adopt:<reminder_id>: {!r}".format(tok))
    return out, errs


def _parse_routes(values):
    """Parse repeatable `--route ID=LIST` tokens (comma-joinable) into {task_id: list_name} —
    the in-session judgment layer: /engram-sync may send any task to ANY list except the
    never-write set (validated here so a typo fails fast, before any bridge call)."""
    out, errs = {}, []
    for chunk in values or []:
        for tok in chunk.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if "=" not in tok:
                errs.append("route token missing '=': {!r}".format(tok))
                continue
            tid, lst = tok.split("=", 1)
            tid, lst = tid.strip(), lst.strip()
            if not lst:
                errs.append("route target empty: {!r}".format(tok))
            elif lst in NEVER_WRITE:
                errs.append("route target {!r} is never-write".format(lst))
            else:
                out[tid] = lst
    return out, errs


def push_policy(row):
    """"push" | "held" — the ONE owner of "does this row reach Reminders?" (2026-08-30, the owner,
    after 474 pushed / 33 acted on). _push_eligible consults it; nothing else may re-decide.

      owner_class waiting_on             → held   (other people's tasks — 224 of the 474; they
                                                   are tracked, not the owner's to-dos)
      mine + a due OR a real priority    → push
      mine, undated + un-prioritized     → held   (the "project state" lines that made the
                                                   Reminders lists unreadable)

    EXTRACTION IS UNCHANGED: every item still lands in the ledger (`/engram-brief` and the audit
    trail depend on it). This gates only what is CREATED in Reminders. `held` is not terminal —
    every push re-evaluates held rows, so one that gains a due/priority is pushed then.

    CAVEAT (extract has no in-place update path): `task_id` = sha(ref + normalized text) and
    parse_action_line strips the `(due …)` parenthetical from that text, so a re-distilled line
    that only ADDS a due hashes to the SAME id and extract skips it ("already in ledger"). A held
    row therefore gains a due today only from a hand edit to the ledger (or a future
    thread-update path), never from a re-distill. Do not assume re-distillation revives holds.

    A row stamped `swept_at` (removed from Reminders by scripts/reminders_sweep.py at the owner's
    direction) stays held regardless of due/priority — the owner struck it, so re-creating it is wrong.
    Clear `swept_at` by hand to let policy re-evaluate it.

    `push --policy off` restores the pre-2026-08-30 behaviour (push every eligible row)."""
    if row.get("swept_at"):
        return "held"
    if row.get("owner_class") == "waiting_on":
        return "held"
    if row.get("due"):
        return "push"
    if row.get("priority"):        # non-null AND non-zero (0 == "none", see compute_priority)
        return "push"
    return "held"


def _hold_reason(row):
    """Label for an ALREADY-held row (log/report text only — never a second policy decision)."""
    return "waiting_on" if row.get("owner_class") == "waiting_on" else "no-due/no-priority"


def _push_eligible(rows, adjmap, limit, policy_on=True):
    """(to_push, to_hold) for this run. Candidates: status in (open, error, held) — `held` is
    re-checked every push so a row that later gains a due/priority is picked up — PLUS any row
    named in --adjudicated regardless of status (so a needs_review/skipped row can be resolved or
    revived). Each
    candidate is split by push_policy(); the only bypasses are `--policy off` and an explicit
    --adjudicated id (a human/in-session decision, and the only way to `adopt:` a live reminder
    for an already-pushed waiting_on row). --limit caps the rows actually PUSHED — holds are
    free (no bridge calls)."""
    base = [r for r in rows if r.get("status") in ("open", "error", "held")]
    seen = {r["id"] for r in base}
    base += [r for r in rows if r["id"] in adjmap and r["id"] not in seen]
    to_push, to_hold = [], []
    for r in base:
        if not policy_on or r["id"] in adjmap or push_policy(r) == "push":
            to_push.append(r)
        else:
            to_hold.append(r)
    if limit is not None:
        to_push = to_push[:limit]
    return to_push, to_hold


def cmd_push(args, root, runner=None):
    """Push eligible rows into Reminders with live dedup. See module docstring for the flags.

    Eligibility is push_policy()'s call (open/error/held candidates → pushed vs `held`); rows it
    holds never reach the bridge, and a fully-held run makes ZERO bridge calls. `--policy off`
    restores the pre-2026-08-30 push-everything behaviour.

    --dry-run is BRIDGE-FREE: it previews routing/priority/title for eligible rows and writes
    nothing (no dedup lookup). A real push runs: (1) find_by_token repair sweep, (2) dump_incomplete
    exact-adopt / near→needs_review, (3) batch create per list. On any BridgeError → exit 3 with
    zero BRIDGE-DEPENDENT ledger transitions (the single atomic ledger write happens only after the
    create batch returns; a mid-batch failure is recovered idempotently by the next run's
    find_by_token sweep). The one exception is an adjudicated `skip`, which depends on no bridge
    result and is recorded even then — the message says exactly what was and was not written."""
    rows, by_id = load_queue(root)  # exits 4 internally on corrupt ledger
    adjmap, adj_errs = _parse_adjudicated(getattr(args, "adjudicated", None))
    route_map, route_errs = _parse_routes(getattr(args, "route", None))
    for e in adj_errs + route_errs:
        print("push: {}".format(e), file=sys.stderr)
    if adj_errs or route_errs:
        return 2
    override = getattr(args, "route_override", None)
    policy_on = getattr(args, "policy", "on") != "off"
    today = datetime.date.today()
    to_push, to_hold = _push_eligible(rows, adjmap, getattr(args, "limit", None),
                                      policy_on=policy_on)

    if getattr(args, "dry_run", False):
        _push_dry_run(to_push, to_hold, override, today, route_map, policy_on)
        return 0

    if not to_push and not to_hold:
        print("push: no open/error/held rows to push.")
        return 0

    # decision[id] = ("repair"|"adopted", reminder_id, list) | ("needs_review",) | ("create",)
    #              | ("skip",)  → terminal status `skipped` on the row (never a counter-only no-op)
    decision = {}
    create_results = {}   # id -> ("ok", reminder_id) | ("error", detail)
    create_ctx = {}       # id -> (list, due_auto)

    # (0) adjudication takes precedence over automatic dedup for the named ids. Decided BEFORE
    # the bridge gate below, because a `skip` needs no bridge call to be resolved.
    for r in to_push:
        dec = adjmap.get(r["id"])
        if dec == "skip":
            decision[r["id"]] = ("skip",)
        elif dec == "create":
            decision[r["id"]] = ("create",)
        elif isinstance(dec, tuple) and dec[0] == "adopt":
            decision[r["id"]] = ("adopted", dec[1], None)

    # A fully-held push makes no bridge call at all — and neither does a run whose every eligible
    # row was adjudicated `skip`: that decision is already made, so the only work left is the
    # ledger transition. Gating on this (not on `to_push`) is what lets a skip be RECORDED while
    # the JXA bridge is unreachable/timing out; any other decision still needs a live dedup pass.
    needs_bridge = [r for r in to_push if decision.get(r["id"], ("create",))[0] != "skip"]
    if needs_bridge:
        try:
            # (1) find_by_token repair sweep — adopt orphans from a crashed prior push.
            fbt = bridge("find_by_token", {"lists": ENGRAM_LISTS, "prefix": TOKEN_PREFIX}, runner=runner)
            elig_ids = {r["id"] for r in to_push}
            for m in fbt.get("matches", []):
                tid = m.get("task_id")
                if tid in elig_ids and tid not in decision:
                    decision[tid] = ("repair", m.get("reminder_id"), m.get("list"))

            # (2) dump_incomplete → exact-title adopt, else near-dup hold.
            dump = bridge("dump_incomplete", {}, runner=runner, timeout=T_DUMP)
            incomplete = dump.get("items", [])
            cache_path = getattr(args, "dump_cache", None)
            if cache_path:
                _write_dump_cache(cache_path, incomplete)
            by_id_item = {it["id"]: it for it in incomplete}
            norm_index = {}
            for it in incomplete:
                norm_index.setdefault(normalize_text(it.get("title", "")), it)

            for r in to_push:
                if r["id"] in decision:
                    continue
                nt = normalize_text(r["text"])
                exact = norm_index.get(nt)
                if exact:
                    decision[r["id"]] = ("adopted", exact["id"], exact.get("list"))
                    continue
                near = next((it for it in incomplete
                             if title_ratio(r["text"], it.get("title", "")) >= NEAR_DUP_RATIO), None)
                if near:
                    decision[r["id"]] = ("needs_review",)

            # fill an adjudicated adopt's list from the live dump when we can.
            for r in to_push:
                d = decision.get(r["id"])
                if d and d[0] == "adopted" and d[2] is None and d[1] in by_id_item:
                    decision[r["id"]] = ("adopted", d[1], by_id_item[d[1]].get("list"))

            # (3) batch create per list for everything not adopted / held / skipped.
            to_create = [r for r in to_push if decision.get(r["id"], ("create",))[0] == "create"]
            by_list = collections.OrderedDict()
            for r in to_create:
                by_list.setdefault(route_list(r, override, route_map), []).append(r)
            for lst_name in by_list:
                _assert_writable(lst_name)   # refuse before we create anything, not mid-batch
            if by_list:
                ens = bridge("ensure_lists", {"names": list(by_list.keys())}, runner=runner)
                for nm in ens.get("grouped", []) if isinstance(ens, dict) else []:
                    # pinned 2026-07-13: a group-nested list is unreachable as an object — nothing can
                    # be created in it. ensure_lists refused to mint a duplicate; the create below will
                    # fail per-item with list-not-found (status=error, retryable). Tell the owner the fix.
                    print("push: WARN target list {!r} is nested in a sidebar Group — unreachable for "
                          "scripted writes. Un-group it (drag to top level) and re-push; its {} row(s) "
                          "will land as status=error until then.".format(nm, len(by_list.get(nm, []))),
                          file=sys.stderr)
            for lst_name, group in by_list.items():
                items = []
                for r in group:
                    eff_due, due_auto = _effective_due(r, today)
                    create_ctx[r["id"]] = (lst_name, due_auto)
                    items.append({
                        "task_id": r["id"], "title": truncate_title(r["text"]),
                        "notes": build_notes(r, due_auto=due_auto), "due": eff_due,
                        "priority": compute_priority(eff_due, today), "flagged": True,
                    })
                # Chunked creates: Apple Events cost ~1-3s/item, so one big batch can
                # blow the 120s default (observed: 109-item seed). Small chunks with a
                # count-scaled timeout keep each osascript call bounded; a mid-run
                # failure loses at most one chunk, and the next push's find_by_token
                # repair sweep adopts any strays the killed chunk already created.
                CHUNK = 20
                for i in range(0, len(items), CHUNK):
                    chunk = items[i:i + CHUNK]
                    res = bridge("create", {"list": lst_name, "items": chunk}, runner=runner,
                                 timeout=max(DEFAULT_TIMEOUT, 10 * len(chunk) + 30))
                    for c in res.get("created", []):
                        create_results[c["task_id"]] = ("ok", c["reminder_id"])
                    for e in res.get("errors", []):
                        create_results[e["task_id"]] = ("error", e.get("error", ""))
        except BridgeError as e:
            # All-or-nothing, precisely scoped: every transition that DEPENDS on a bridge result
            # is abandoned. An adjudicated `skip` depends on none (it is the owner's decision, already
            # made), so it is still recorded — otherwise a dead bridge makes a needs_review row
            # unresolvable, which is exactly how the 2026-09-06 pair got stuck. Automated callers
            # (cmd_sync, tasks_nightly) never pass --adjudicated, so for them this is byte-identical
            # to the old behaviour: 0 recorded, same message, same exit 3.
            n = _apply_skips_only(rows, decision, root)
            print("push: bridge failure ({}: {}) — {}".format(
                e.code, e.detail,
                "recorded {} adjudicated skip(s); no other ledger transitions".format(n) if n
                else "no ledger transitions"), file=sys.stderr)
            return 3

    # ── single atomic ledger write (only reached when every bridge op returned) ──
    now = _now_iso()
    log_entries = []
    counts = collections.Counter()
    for r in to_push:
        d = decision.get(r["id"])
        if d and d[0] in ("repair", "adopted"):
            r["reminder_id"] = d[1]
            r["list"] = d[2]
            r["routed_list"] = route_list(r, override, route_map)   # prediction, for routing-report
            r["status"] = "pushed"
            r["adopted"] = True
            # flag_owned = did ENGRAM set this reminder's flag? REPAIR rebinds a reminder engram
            # itself created flagged (recovered from a crashed push) ⇒ True: the owner unflagging it IS
            # a confirm. DEDUP-adopt binds to the owner's OWN pre-existing (unflagged) reminder ⇒ False:
            # its unflagged state is not a triage signal. This is the correct confirm gate — the
            # blunt `adopted` flag wrongly excluded repair rows (63 of the backfill).
            r["flag_owned"] = (d[0] == "repair")
            r["pushed_at"] = now
            log_entries.append({"ts": now, "id": r["id"], "event": "repair" if d[0] == "repair" else "adopted",
                                "reminder_id": d[1], "list": d[2]})
            counts[d[0]] += 1
        elif d and d[0] == "needs_review":
            r["status"] = "needs_review"
            log_entries.append({"ts": now, "id": r["id"], "event": "needs_review"})
            counts["needs_review"] += 1
        elif d and d[0] == "skip":
            # A skip is TERMINAL: the owner/the LLM judged the row a duplicate of something already
            # live in Reminders, so it must never be re-offered. Before 2026-09-06 this branch
            # only bumped a counter — the row kept status `needs_review` forever and every brief
            # (and every later push) re-surfaced it. Idempotent: a re-run of the same adjudication
            # leaves the ledger byte-identical and logs nothing (the `still_held` pattern).
            if r.get("status") == "skipped":
                counts["still_skipped"] += 1
            else:
                r["status"] = "skipped"
                r["adjudicated_at"] = now
                log_entries.append({"ts": now, "id": r["id"], "event": "skipped"})
                counts["skip"] += 1
        else:
            outcome = create_results.get(r["id"])
            lst_name, due_auto = create_ctx.get(r["id"], (route_list(r, override, route_map), None))
            if outcome and outcome[0] == "ok":
                r["reminder_id"] = outcome[1]
                r["list"] = lst_name
                r["routed_list"] = lst_name   # prediction == placement at create time
                r["status"] = "pushed"
                r["adopted"] = False
                r["flag_owned"] = True        # engram created it flagged ⇒ unflag = confirm
                r["pushed_at"] = now
                r["due_auto"] = due_auto
                r["priority"] = compute_priority(due_auto or r.get("due"), today)
                log_entries.append({"ts": now, "id": r["id"], "event": "pushed",
                                    "reminder_id": outcome[1], "list": lst_name})
                counts["pushed"] += 1
            else:
                r["status"] = "error"
                detail = outcome[1] if outcome else "no create result returned"
                log_entries.append({"ts": now, "id": r["id"], "event": "error", "detail": detail})
                counts["error"] += 1

    # policy holds: they stay in the LEDGER (nothing is dropped) and out of Reminders. Only a
    # real transition is logged, so a steady-state push leaves a byte-identical ledger and an
    # unchanged log (no nightly churn).
    for r in to_hold:
        if r.get("status") == "held":
            counts["still_held"] += 1
            continue
        r["status"] = "held"
        log_entries.append({"ts": now, "id": r["id"], "event": "held",
                            "reason": _hold_reason(r)})
        counts["held"] += 1

    write_queue_atomic(root, rows)
    append_log(root, log_entries)
    print("push: pushed {} · adopted {} · repaired {} · needs_review {} · error {} · "
          "skipped {} (+{} already skipped) · held {} (+{} already held)".format(
              counts["pushed"], counts["adopted"], counts["repair"], counts["needs_review"],
              counts["error"], counts["skip"], counts["still_skipped"],
              counts["held"], counts["still_held"]))
    return 1 if counts["error"] else 0


def _apply_skips_only(rows, decision, root):
    """Persist ONLY the adjudicated-skip transitions (used on the bridge-failure path). Returns
    how many rows actually changed; writes nothing when that is 0, so a failed automated push
    still leaves a byte-identical ledger."""
    now = _now_iso()
    log_entries = []
    for r in rows:
        d = decision.get(r["id"])
        if d and d[0] == "skip" and r.get("status") != "skipped":
            r["status"] = "skipped"
            r["adjudicated_at"] = now
            log_entries.append({"ts": now, "id": r["id"], "event": "skipped"})
    if log_entries:
        write_queue_atomic(root, rows)
        append_log(root, log_entries)
    return len(log_entries)


def _push_dry_run(to_push, to_hold, override, today, route_map=None, policy_on=True):
    print("=== engram_tasks push --dry-run (bridge-free preview; no dedup lookup) ===")
    print("candidates (open/error/held): {} · would PUSH {} · would HOLD {}   [policy {}]".format(
        len(to_push) + len(to_hold), len(to_push), len(to_hold), "on" if policy_on else "off"))
    if to_hold:
        reasons = collections.Counter(_hold_reason(r) for r in to_hold)
        print("hold reasons: " + " · ".join(
            "{} {}".format(k, n) for k, n in sorted(reasons.items())))
    by_list = collections.Counter(route_list(r, override, route_map) for r in to_push)
    print("\nWould route to:")
    for lst, n in sorted(by_list.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  {}: {}".format(lst, n))
    print("\nSample:")
    for r in to_push[:15]:
        eff_due, due_auto = _effective_due(r, today)
        tag = " (+{}d nudge)".format(WAITING_NUDGE_DAYS) if due_auto else ""
        print("  -> {:<24} pri={} due={}{} :: {!r}".format(
            route_list(r, override, route_map), compute_priority(eff_due, today),
            eff_due or "—", tag, truncate_title(r["text"])[:80]))


def _write_dump_cache(path, items):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": _now_iso(), "items": items}, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_dump_cache(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("items", [])


# ---------------------------------------------------------------------------
# reconcile — read pushed rows, apply completed⇒done / missing⇒dismissed / moved⇒accepted
# ---------------------------------------------------------------------------

def cmd_reconcile(args, root, runner=None):
    rows, by_id = load_queue(root)  # exits 4 internally on corrupt ledger
    pushed = [r for r in rows if r.get("status") == "pushed" and r.get("reminder_id")]
    if not pushed:
        print("reconcile: no pushed rows to reconcile.")
        return 0
    ids = [r["reminder_id"] for r in pushed]
    # read is a single app-wide UNFILTERED pass (~115s over the whole corpus) — the only
    # grouped-list-safe shape: App.lists does not enumerate lists nested in a GROUP, so a
    # per-list scan would miss grouped lists and dismiss their items. Fixed cost regardless of
    # triage volume; no list hint needed (see reminders_bridge.js opRead).
    try:
        res = bridge("read", {"ids": ids}, runner=runner, timeout=T_DUMP)
    except BridgeError as e:
        print("reconcile: bridge failure ({}: {}) — no ledger transitions".format(e.code, e.detail),
              file=sys.stderr)
        return 3
    found = {f["id"]: f for f in res.get("found", [])}
    missing = set(res.get("missing", []))

    # Mass-dismissal valve: a signed-out-iCloud signature (most reminders vanish at once). Abort
    # with ZERO writes unless --force. Trips when > 50% of the pushed rows read this run are missing,
    # on a ledger of at least 5 pushed rows (the floor keeps a legitimately-deleted single item on a
    # tiny ledger from reading as a "mass" dismissal). NOTE: the plan prose says "≥5 missing"; the
    # brief's own test case (6 pushed / 4 missing ⇒ exit 4) only trips if the ≥5 floor is on the
    # pushed POPULATION, not the missing count — so the floor is on len(pushed).
    if not getattr(args, "force", False) and len(pushed) >= 5 and len(missing) > 0.5 * len(pushed):
        print("reconcile: MASS-DISMISSAL VALVE tripped — {}/{} pushed reminders missing (>50% of >=5 "
              "pushed). Refusing to dismiss (looks like signed-out iCloud). Re-run with --force to "
              "override.".format(len(missing), len(pushed)), file=sys.stderr)
        return 4

    now = _now_iso()
    log_entries = []
    counts = collections.Counter()
    for r in pushed:
        rid = r["reminder_id"]
        f = found.get(rid)
        if f is not None:
            if f.get("completed"):
                r["status"] = "done"
                r["resolved_at"] = now
                log_entries.append({"ts": now, "id": r["id"], "event": "done", "reminder_id": rid})
                counts["done"] += 1
            else:
                # NOTE: a still-open item with no transition writes NOTHING — so a no-op reconcile
                # produces a byte-identical ledger and the nightly has nothing to commit (no churn).
                # flag dropped => the owner triaged and KEPT it. We create reminders flagged and never
                # flag anything ourselves, so an OBSERVED unflag on a reminder whose flag WE own
                # (flag_owned: created + repair-adopted, NOT dedup-adopted) is a one-shot confirm.
                if (f.get("flagged") is False and not r.get("confirmed_at")
                        and r.get("flag_owned", True)):
                    r["confirmed_at"] = now
                    log_entries.append({"ts": now, "id": r["id"], "event": "confirmed"})
                    counts["confirmed"] += 1
                if f.get("list") and f["list"] != r.get("list"):
                    # a move anywhere is accepted — and LOGGED: the owner's manual re-filing is the
                    # training signal for routing (see `routing-report`).
                    log_entries.append({"ts": now, "id": r["id"], "event": "moved",
                                        "from": r.get("list"), "to": f["list"]})
                    counts["moved"] += 1
                    r["list"] = f["list"]
                counts["still_open"] += 1
        elif rid in missing:
            r["status"] = "dismissed"
            r["resolved_at"] = now
            log_entries.append({"ts": now, "id": r["id"], "event": "dismissed", "reminder_id": rid})
            counts["dismissed"] += 1

    write_queue_atomic(root, rows)
    append_log(root, log_entries)
    if counts["confirmed"]:
        print("reconcile: {} item(s) confirmed by unflagging.".format(counts["confirmed"]))
    if counts["moved"]:
        print("reconcile: {} item(s) re-filed by hand — run `routing-report` to review the "
              "corrections".format(counts["moved"]))
    print("reconcile: done {} · dismissed {} · still-open {} (of {} pushed).".format(
        counts["done"], counts["dismissed"], counts["still_open"], len(pushed)))
    return 0


# ---------------------------------------------------------------------------
# sync — reconcile → extract --since <watermark> → push (deterministic core)
# ---------------------------------------------------------------------------

def cmd_sync(args, root, runner=None):
    rc_recon = cmd_reconcile(args, root, runner=runner)
    if rc_recon in (3, 4):
        return rc_recon

    state = load_state(root)
    wm = state.get("last_extract_note_date")
    if wm:
        ex_args = argparse.Namespace(only=None, since=wm, full=False, seed=False, dry_run=False)
        cmd_extract(ex_args, root)
    else:
        print("sync: no extraction watermark yet — skipping extract (run `extract --full`/`--seed` first).")

    push_args = argparse.Namespace(dry_run=False, limit=None, adjudicated=None,
                                   route_override=getattr(args, "route_override", None),
                                   dump_cache=None)
    rc_push = cmd_push(push_args, root, runner=runner)
    return rc_push


# ---------------------------------------------------------------------------
# doctor — thin wrapper over _lib.reminders.doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args, root, runner=None):
    print(json.dumps(doctor(live=getattr(args, "live", False), runner=runner), indent=2))
    return 0


# ---------------------------------------------------------------------------
# suggest-complete — deterministic completion-suggestion substrate
# ---------------------------------------------------------------------------

def _source_kind(row):
    return _ref_kind(row.get("ref"))


def cmd_triage_report(args, root):
    """Extraction-precision signal from the owner's triage: delete ⇒ `dismissed` (false positive /
    unwanted), unflag ⇒ `confirmed_at` (real task), complete ⇒ done (strongest confirm). Shows
    accept/reject rates by facet + the dismissed texts, so recurring patterns can be folded into
    EXTRACT_SKIP_RULES (mechanical patterns) or a deliberate distiller-prompt refinement
    (semantic over-extraction). Untriaged (still flagged) rows are excluded from rates, and so
    are push_policy `held` rows (never pushed ⇒ no triage signal) — they get their own count."""
    rows, _ = load_queue(root)
    held_n = sum(1 for r in rows if r.get("status") == "held")
    scored = [r for r in rows if r.get("status") in ("pushed", "done", "dismissed")]
    if not scored:
        print("triage-report: nothing pushed yet.{}".format(
            " {} held by push_policy.".format(held_n) if held_n else ""))
        return 0
    def bucket(r):
        if r["status"] == "dismissed":
            return "dismissed"
        if r["status"] == "done" or r.get("confirmed_at"):
            return "confirmed"
        return "untriaged"
    by_bucket = collections.Counter(bucket(r) for r in scored)
    # `held` is its own bucket and is EXCLUDED from the rates below: the owner never saw those rows,
    # so they carry no confirm/dismiss signal (a held row is a push-policy decision, not triage).
    print("triage-report: confirmed {confirmed} · dismissed {dismissed} · untriaged {untriaged} "
          "· held {held} (not pushed — push_policy; excluded from the rates)".format(
              confirmed=by_bucket["confirmed"], dismissed=by_bucket["dismissed"],
              untriaged=by_bucket["untriaged"], held=held_n))
    triaged = [r for r in scored if bucket(r) != "untriaged"]
    if not triaged:
        return 0
    for facet, keyfn in (("source", _source_kind),
                         ("domain", lambda r: r.get("domain") or "?"),
                         ("owner_class", lambda r: r.get("owner_class") or "?"),
                         ("due", lambda r: "has-due" if r.get("due") else "no-due")):
        agg = collections.defaultdict(lambda: [0, 0])   # key -> [confirmed, dismissed]
        for r in triaged:
            agg[keyfn(r)][0 if bucket(r) == "confirmed" else 1] += 1
        parts = []
        for k, (c, d) in sorted(agg.items(), key=lambda kv: -sum(kv[1])):
            parts.append(f"{k} {c}✓/{d}✗")
        print(f"  by {facet}: " + " · ".join(parts))
    dismissed = [r for r in triaged if bucket(r) == "dismissed"]
    if dismissed:
        print("  dismissed texts (patterns → EXTRACT_SKIP_RULES or a distiller-prompt refinement):")
        for r in dismissed[:20]:
            print(f"     ✗ [{_source_kind(r)}/{r.get('domain','?')}/{r.get('owner_class','?')}] {r['text'][:80]}")
        if len(dismissed) > 20:
            print(f"     … and {len(dismissed) - 20} more")
    return 0


def cmd_routing_report(args, root):
    """Where the owner re-filed items by hand: rows whose current list differs from the list routing
    chose at push time (`routed_list`). This is the TRAINING SIGNAL — review the patterns
    in-session and fold recurring ones into entity_rules / domain_to_list (the single owner:
    _manifest/constants/reminders.json), so next time routing agrees with the owner. `--rebaseline` accepts the current placement as the new
    baseline once a pattern has been folded in (or deliberately ignored)."""
    rows, _ = load_queue(root)
    mism = [r for r in rows if r.get("routed_list") and r.get("list")
            and r["routed_list"] != r["list"] and r.get("status") in ("pushed", "done")]
    if not mism:
        print("routing-report: no hand re-filings observed — routing agrees with the owner.")
        return 0
    if getattr(args, "rebaseline", False):
        now = _now_iso()
        entries = []
        for r in mism:
            entries.append({"ts": now, "id": r["id"], "event": "rebaselined",
                            "from": r["routed_list"], "to": r["list"]})
            r["routed_list"] = r["list"]
        write_queue_atomic(root, rows)
        append_log(root, entries)
        print("routing-report: {} row(s) re-baselined.".format(len(mism)))
        return 0
    by_pair = collections.Counter((r["routed_list"], r["list"]) for r in mism)
    print("routing-report: {} hand re-filing(s) since push:".format(len(mism)))
    for (src, dst), n in by_pair.most_common():
        print("  {} -> {} ({}x)".format(src, dst, n))
        for r in mism:
            if (r["routed_list"], r["list"]) == (src, dst):
                print("     · [{}] {}".format(r.get("domain", "?"), r["text"][:80]))
    print("\nFold recurring patterns into entity_rules/domain_to_list (_manifest/constants/reminders.json) "
          "+ a test, then `routing-report --rebaseline`.")
    return 0


def _suggestion_sid(target_id, slug):
    return "sug:" + hashlib.sha256("{}\n{}".format(target_id, slug).encode("utf-8")).hexdigest()[:16]


def _bullet_lines(body):
    for raw in body.splitlines():
        s = raw.strip()
        if s[:1] in ("-", "*"):
            yield raw


def _match_target(text, rows, incomplete):
    """Match a resolution-marked line's text back to an open/pushed ledger row (preferred) or a
    live incomplete reminder. Exact normalized-title equality wins; else best fuzzy >= NEAR_DUP_RATIO.
    Returns ("task", task_id) | ("reminder", reminder_id) | None."""
    nt = normalize_text(text)
    if len(nt) < 8:
        return None
    open_rows = [r for r in rows if r.get("status") in ("open", "pushed")]
    for r in open_rows:
        if normalize_text(r["text"]) == nt:
            return ("task", r["id"])
    best, best_r = 0.0, None
    for r in open_rows:
        ratio = title_ratio(text, r["text"])
        if ratio > best:
            best, best_r = ratio, r
    if best >= NEAR_DUP_RATIO:
        return ("task", best_r["id"])
    for it in incomplete:
        if normalize_text(it.get("title", "")) == nt:
            return ("reminder", it["id"])
    best, best_it = 0.0, None
    for it in incomplete:
        ratio = title_ratio(text, it.get("title", ""))
        if ratio > best:
            best, best_it = ratio, it
    if best >= NEAR_DUP_RATIO:
        return ("reminder", best_it["id"])
    return None


def _read_only_notes(root, only_file):
    with open(only_file, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def cmd_suggest_complete(args, root, runner=None):
    """Deterministic completion-suggestion substrate. Scans --only notes for resolution-marked
    action lines matching an open/pushed ledger row or a live incomplete reminder, and/or ingests
    LLM-produced records via --add-json. Records go to task-suggestions.jsonl. A (target, slug)
    pair that already has ANY status is never re-suggested (rejected pairs never re-surface).

    --emit persists the deterministic scan (default = preview only). --add-json ALWAYS persists
    (it is the injection path). --dump-cache reuses a push's dump (avoids a second ~165s scan)."""
    only_file = getattr(args, "only", None)
    add_json = getattr(args, "add_json", None)
    if not only_file and not add_json:
        print("suggest-complete: pass --only FILE and/or --add-json FILE", file=sys.stderr)
        return 2

    rows, _ = load_queue(root)  # exits 4 internally on corrupt ledger
    sug_rows, by_sid = load_suggestions(root)
    existing_pairs = {(_pair_target(s), s.get("evidence", {}).get("slug")) for s in sug_rows}

    now = _now_iso()
    new_records = []   # (sid, record)
    warnings = 0

    # deterministic scan of the synced notes
    if only_file:
        incomplete = []
        cache = getattr(args, "dump_cache", None)
        if cache and os.path.exists(cache):
            incomplete = _read_dump_cache(cache)
        elif runner is not None or not _in_test_context():
            try:
                incomplete = bridge("dump_incomplete", {}, runner=runner, timeout=T_DUMP).get("items", [])
            except BridgeError as e:
                print("suggest-complete: dump_incomplete failed ({}) — matching ledger rows only".format(e.code),
                      file=sys.stderr)
        for rel in _read_only_notes(root, only_file):
            path = rel if os.path.isabs(rel) else os.path.join(root, rel)
            if not os.path.exists(path):
                continue
            try:
                head, body = split_frontmatter(open(path, encoding="utf-8", errors="replace").read())
            except OSError:
                warnings += 1
                continue
            if body is None:
                continue
            slug = note_slug(path)
            for line in _bullet_lines(body):
                if not is_resolved_marker(line):
                    continue
                text = resolved_action_text(line)
                target = _match_target(text, rows, incomplete)
                if target is None:
                    continue
                target_id = target[1]
                if (target_id, slug) in existing_pairs:
                    continue
                sid = _suggestion_sid(target_id, slug)
                rec = _mk_suggestion(sid, target, slug, line.strip(), now)
                new_records.append((sid, rec))
                existing_pairs.add((target_id, slug))

    # LLM-injected records (same schema, same dedup)
    if add_json:
        for rec_in in _load_add_json(add_json):
            target = _pair_target_from_record(rec_in)
            slug = (rec_in.get("evidence") or {}).get("slug")
            quote = (rec_in.get("evidence") or {}).get("quote", "")
            if not target or not slug:
                warnings += 1
                continue
            if (target[1], slug) in existing_pairs:
                continue
            sid = _suggestion_sid(target[1], slug)
            rec = _mk_suggestion(sid, target, slug, quote, now)
            new_records.append((sid, rec))
            existing_pairs.add((target[1], slug))

    persist = bool(getattr(args, "emit", False)) or bool(add_json)
    if not persist:
        print("suggest-complete (preview): {} new suggestion(s) (pass --emit to write).".format(len(new_records)))
        for sid, rec in new_records[:15]:
            print("  {} -> {} :: {!r}".format(sid, _pair_target(rec), rec["evidence"]["quote"][:80]))
        return 1 if warnings else 0

    if new_records:
        for sid, rec in new_records:
            sug_rows.append(rec)
        write_suggestions_atomic(root, sug_rows)
        append_log(root, [{"ts": now, "id": rec.get("task_id") or rec.get("reminder_id"),
                           "sid": sid, "event": "suggested"} for sid, rec in new_records])
    print("suggest-complete: emitted {} new suggestion(s), {} warning(s).".format(len(new_records), warnings))
    return 1 if warnings else 0


def _mk_suggestion(sid, target, slug, quote, ts):
    rec = {"sid": sid, "evidence": {"slug": slug, "quote": quote}, "status": "open", "ts": ts}
    if target[0] == "task":
        rec["task_id"] = target[1]
    else:
        rec["reminder_id"] = target[1]
    return rec


def _pair_target(sug):
    """The dedup/target id of a suggestion record: task_id preferred, else reminder_id."""
    return sug.get("task_id") or sug.get("reminder_id")


def _pair_target_from_record(rec):
    if rec.get("task_id"):
        return ("task", rec["task_id"])
    if rec.get("reminder_id"):
        return ("reminder", rec["reminder_id"])
    return None


def _load_add_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("suggestions", [])
    return data if isinstance(data, list) else []


def _in_test_context():
    """True under pytest (so suggest-complete --only without an injected runner or a --dump-cache
    matches ledger rows only, never spawning osascript in the hermetic suite)."""
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


# ---------------------------------------------------------------------------
# complete — bridge-complete confirmed suggestions; reject records the decision
# ---------------------------------------------------------------------------

def _split_ids(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def cmd_complete(args, root, runner=None):
    """Complete confirmed suggestions and/or reject others. For a confirmed sid: bridge-complete
    its reminder (small batches — each byId ~40s), mark the ledger row done and the suggestion
    confirmed. A suggestion for a hand-created reminder (no ledger row) is bridge-completed only.
    --reject records the suggestion as rejected (no bridge, no ledger change) so the pair never
    re-surfaces. On BridgeError → exit 3 with ZERO writes."""
    confirm_ids = _split_ids(getattr(args, "ids", None))
    reject_ids = _split_ids(getattr(args, "reject", None))
    if not confirm_ids and not reject_ids:
        print("complete: pass --ids sid,… and/or --reject sid,…", file=sys.stderr)
        return 2

    rows, by_id = load_queue(root)
    sug_rows, by_sid = load_suggestions(root)

    # gather the reminders to complete (dedup ids across sids)
    to_complete = []          # reminder_ids
    plan = []                 # (sid, sug, row_or_None, reminder_id_or_None)
    warnings = 0
    for sid in confirm_ids:
        sug = by_sid.get(sid)
        if sug is None:
            print("complete: unknown sid {!r} — skipped".format(sid), file=sys.stderr)
            warnings += 1
            continue
        row = by_id.get(sug["task_id"]) if sug.get("task_id") else None
        rid = (row.get("reminder_id") if row else None) or sug.get("reminder_id")
        if rid:
            to_complete.append(rid)
        plan.append((sid, sug, row, rid))

    if to_complete:
        uniq = list(dict.fromkeys(to_complete))
        timeout = max(DEFAULT_TIMEOUT, 45 * len(uniq) + 60)   # byId ~40s each
        try:
            bridge("complete", {"ids": uniq}, runner=runner, timeout=timeout)
        except BridgeError as e:
            print("complete: bridge failure ({}: {}) — no ledger transitions".format(e.code, e.detail),
                  file=sys.stderr)
            return 3

    now = _now_iso()
    log_entries = []
    done_rows = 0
    for sid, sug, row, rid in plan:
        sug["status"] = "confirmed"
        if row is not None:
            row["status"] = "done"
            row["resolved_at"] = now
            log_entries.append({"ts": now, "id": row["id"], "sid": sid, "event": "done"})
            done_rows += 1
        log_entries.append({"ts": now, "id": _pair_target(sug), "sid": sid, "event": "confirmed"})

    rejected = 0
    for sid in reject_ids:
        sug = by_sid.get(sid)
        if sug is None:
            print("complete: unknown sid {!r} — skipped".format(sid), file=sys.stderr)
            warnings += 1
            continue
        sug["status"] = "rejected"
        rejected += 1
        log_entries.append({"ts": now, "id": _pair_target(sug), "sid": sid, "event": "rejected"})

    write_suggestions_atomic(root, sug_rows)
    if done_rows:
        write_queue_atomic(root, rows)
    append_log(root, log_entries)
    print("complete: confirmed {} (rows done {}) · rejected {} · {} warning(s).".format(
        len(plan), done_rows, rejected, warnings))
    return 1 if warnings else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="engram-tasks — two-way Apple Reminders task bridge.")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS),
                    help="store root (defaults to the repo); tests point this at a temp store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="scan notes/open-loops into the task ledger")
    p_extract.add_argument("--only", metavar="FILE", help="newline-delimited list of note paths")
    p_extract.add_argument("--since", metavar="DATE", help="notes with date >= DATE (YYYY-MM-DD)")
    p_extract.add_argument("--full", action="store_true", help="scan every note")
    p_extract.add_argument("--seed", action="store_true", help="seed from digests/open-loops.md")
    p_extract.add_argument("--dry-run", action="store_true", help="report only, write nothing")

    sub.add_parser("status", help="read-only ledger counts")

    p_push = sub.add_parser("push", help="push open/error rows into Reminders (live dedup)")
    p_push.add_argument("--dry-run", action="store_true",
                        help="BRIDGE-FREE preview of routing/priority/title; writes nothing, no dedup lookup")
    p_push.add_argument("--limit", type=int, default=None, help="cap the number of rows actually pushed")
    p_push.add_argument("--policy", choices=("on", "off"), default="on",
                        help="push policy (default on, 2026-08-30): only `mine` rows with a due "
                             "or a priority reach Reminders; waiting_on and undated/"
                             "un-prioritized rows are ledger-`held`. `off` pushes everything "
                             "eligible (pre-policy behaviour). Single owner: push_policy()")
    p_push.add_argument("--adjudicated", action="append", metavar="ID=DEC", default=None,
                        help="apply a sync-session near-dup decision: 'task:…=create', "
                             "'task:…=skip', or 'task:…=adopt:<reminder_id>'. Repeatable / comma-joined. "
                             "Deterministic push only HOLDS near-dups (needs_review); the LLM decides here.")
    p_push.add_argument("--route", action="append", metavar="ID=LIST", default=None,
                        help="per-task list override, e.g. 'task:ab12=Family Trust' — the "
                             "in-session judgment layer; any list except the never-write set")
    p_push.add_argument("--route-override", metavar="LIST", default=None,
                        help="TEST-ONLY: force ALL routing to this single list (live-smoke against 'Engram Test')")
    p_push.add_argument("--dump-cache", metavar="FILE", default=None,
                        help="write the dump_incomplete result here so a same-run suggest-complete can reuse it")

    p_recon = sub.add_parser("reconcile", help="read pushed rows: completed⇒done, missing⇒dismissed, moved⇒accepted")
    p_recon.add_argument("--force", action="store_true", help="override the mass-dismissal valve (>50%% & >=5 missing)")

    sub.add_parser("triage-report", help="confirm/dismiss rates by facet (the extraction-precision signal)")
    p_rr = sub.add_parser("routing-report", help="hand re-filings vs routing predictions (the training signal)")
    p_rr.add_argument("--rebaseline", action="store_true",
                      help="accept current placements as the new baseline (after folding patterns into the rules)")
    p_sync = sub.add_parser("sync", help="reconcile → extract --since <watermark> → push (deterministic core)")
    p_sync.add_argument("--force", action="store_true", help="forwarded to reconcile's mass-dismissal valve")

    p_doctor = sub.add_parser("doctor", help="Reminders bridge health/feature report")
    p_doctor.add_argument("--live", action="store_true", help="run the create→read→complete→delete smoke in 'Engram Test'")

    p_sug = sub.add_parser("suggest-complete", help="emit completion suggestions from resolution-marked lines")
    p_sug.add_argument("--only", metavar="FILE", help="newline-delimited synced-note paths to scan")
    p_sug.add_argument("--emit", action="store_true", help="persist the deterministic scan (default: preview only)")
    p_sug.add_argument("--add-json", metavar="FILE", default=None,
                       help="ingest LLM-produced suggestion records (same schema/dedup); always persists")
    p_sug.add_argument("--dump-cache", metavar="FILE", default=None, help="reuse a push's cached dump_incomplete")

    p_comp = sub.add_parser("complete", help="complete confirmed suggestions / reject others")
    p_comp.add_argument("--ids", metavar="SID,…", default=None,
                        help="confirmed suggestion sids to complete (bridge complete is byId ~40s each — small batches)")
    p_comp.add_argument("--reject", metavar="SID,…", default=None, help="suggestion sids to record as rejected")

    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"--root {root!r} is not a directory", file=sys.stderr)
        return 2

    for attr in ("only", "dump_cache", "add_json"):  # resolve file paths before chdir(root) below
        if getattr(args, attr, None):
            setattr(args, attr, os.path.abspath(getattr(args, attr)))

    prev_cwd = os.getcwd()
    os.chdir(root)  # _lib.notes.iter_notes globs relative to cwd, matching regen.py's convention
    try:
        dispatch = {
            "extract": cmd_extract, "status": cmd_status, "push": cmd_push,
            "reconcile": cmd_reconcile, "sync": cmd_sync, "doctor": cmd_doctor,
            "routing-report": cmd_routing_report, "triage-report": cmd_triage_report,
            "suggest-complete": cmd_suggest_complete, "complete": cmd_complete,
        }
        fn = dispatch.get(args.cmd)
        if fn is not None:
            return fn(args, root)
    finally:
        os.chdir(prev_cwd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
