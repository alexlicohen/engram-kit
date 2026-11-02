#!/usr/bin/env python3
"""Deterministic bookends for the Granola TRANSCRIPT-VERIFY step (`_granola-pending/granola_verify.mjs`).

Why: the Granola distiller writes notes from the Granola AI SUMMARY, and nearly every Granola
fidelity defect (owner/recipient reversal, speaker misattribution, direction reversal,
proposal-as-decision, computed dates) is inherited from that summary. The verify workflow re-checks
each note against the meeting TRANSCRIPT and edits only the note BODY. The Workflow runtime is pure
JS (no fs), so the guards around it live here, in Python (AGENTS.md, "Sync-pipeline guards live in
PYTHON"):

  prepare  — PRE-launch. Build/validate the list file (one {"id","path"} per line) and SNAPSHOT each
             note to <results>/<id>.before.md. The snapshot is taken HERE, not by the verifier agent,
             so the pre-edit original is fixed before any model touches the note (audit trail and
             rollback source; `check` compares against it). Refuses a note whose provenance.ref is
             not `granola:<uuid>` or whose `-g<id8>` filename suffix disagrees with the ref (the
             mis-keyed-ref tell). Idempotent/resumable: an existing snapshot is never overwritten.
  check    — POST-run. For every list entry: the per-note result <results>/<id>.json exists and
             parses; the frontmatter block is BYTE-IDENTICAL to the snapshot (verifiers may edit
             only the body — this is the only gate that covers it, since in a sync the notes are
             still untracked and check.sh's frontmatter-preserve gate cannot see them); a body that
             changed has logged changes and a body that did not change has none. Exit 1 on any.

PROPOSE mode (committed notes; added 2026-09-27). On committed / hand-corrected notes the auto-edit
can revert a transcript-verified prior fix, rewrite a correct ISO date back to the spoken phrase and
demote a confirmed decision (2026-09-27 live test), so `prepare` refuses committed notes in edit
mode. `prepare --propose` instead selects COMMITTED, clean granola notes, writes each line with
"mode": "propose" (the workflow reads the mode from the LIST FILE, never from Workflow args) and
writes <results>/<id>.history.md (+ .history.json): every commit after the note's creation that
changed its BODY, with the lines it removed/added — the prior fixes the verifier must not revert.
The verifier then writes PROPOSALS to its result file and edits nothing:
  check    — propose lists: the note is BYTE-IDENTICAL to its snapshot (any edit fails) and every
             proposal is well-formed; WARNs a `current` not found exactly once in the body.
  review   — render every proposal into ONE numbered markdown file for the owner
             (_sources/granola-verify-proposals-<date>.md) + a machine index (.json, same stem).
  apply    — `apply --ids 1,4,7`: apply the chosen proposals to the note BODIES by exact-substring
             replacement (refused if `current` is not found verbatim in the body, or occurs more than
             once); never touches frontmatter; all-or-nothing; records applied numbers so none is
             applied twice. The orchestrator then runs check.sh (committed notes -> its
             frontmatter-preserve gate sees ordinary body edits).

Usage:
    python3 scripts/granola_verify.py prepare --untracked          # the sync wave's new granola notes
    python3 scripts/granola_verify.py prepare --notes a.md b.md    # explicit NEW (uncommitted) notes
    python3 scripts/granola_verify.py prepare --list L --results D # validate + snapshot an existing list
    python3 scripts/granola_verify.py check [--list L --results D]
    python3 scripts/granola_verify.py prepare --propose --since 2026-08-28 [--until D] [--exclude ID ...]
    python3 scripts/granola_verify.py prepare --propose --notes a.md b.md
    python3 scripts/granola_verify.py check --propose
    python3 scripts/granola_verify.py review [--date YYYY-MM-DD]
    python3 scripts/granola_verify.py apply --ids 1,4,7 [--proposals _sources/granola-verify-proposals-<date>.json]
Defaults: edit mode --list _sources/granola-verify.jsonl --results _sources/granola-verify.d; propose
mode (and review) --list _sources/granola-verify-propose.jsonl --results _sources/granola-verify-propose.d
(all gitignored). Paths in the list may be repo-relative or absolute.
"""
import argparse
import datetime
import difflib
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
ROOT = os.path.dirname(_HERE)

from _lib.notes import split_frontmatter, get_field  # noqa: E402
from _lib.provenance import parse_ref  # noqa: E402

DEFAULT_LIST = "_sources/granola-verify.jsonl"
DEFAULT_RESULTS = "_sources/granola-verify.d"
PROPOSE_LIST = "_sources/granola-verify-propose.jsonl"
PROPOSE_RESULTS = "_sources/granola-verify-propose.d"
PROPOSALS_STEM = "_sources/granola-verify-proposals-"
MODES = ("edit", "propose")
KINDS = ("owner", "recipient", "attribution", "direction", "date", "decision", "name", "other")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUFFIX_RE = re.compile(r"-g([0-9a-f]{8})\.md$")
INGESTED_RE = re.compile(r'^\s+ingested:\s*"?(\d{4}-\d{2}-\d{2})', re.M)
ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _abs(root, p):
    return p if os.path.isabs(p) else os.path.join(root, p)


def _read(fp):
    with open(fp, encoding="utf-8") as f:
        return f.read()


def _git(root, *a, check=True):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=check)


def note_id(fp):
    """granola uuid from the note's provenance.ref; raises ValueError on anything else."""
    txt = _read(fp)
    head, _ = split_frontmatter(txt)
    if head is None:
        raise ValueError(f"{fp}: no frontmatter")
    if get_field(head, "source") != "granola":
        raise ValueError(f"{fp}: source is not granola")
    ref = parse_ref(head) or ""
    if not ref.startswith("granola:") or not UUID_RE.match(ref[len("granola:"):]):
        raise ValueError(f"{fp}: provenance.ref {ref!r} is not granola:<uuid>")
    gid = ref[len("granola:"):]
    m = SUFFIX_RE.search(fp)
    if m and m.group(1) != gid[:8]:
        raise ValueError(f"{fp}: filename suffix g{m.group(1)} != ref {gid[:8]} — mis-keyed "
                         f"provenance.ref? fix the ref before verifying")
    return gid


def untracked_granola_notes(root):
    """Repo-relative paths of untracked/added .md notes whose source is granola (a sync wave's new
    notes, before commit)."""
    out = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    paths = []
    for line in out.splitlines():
        code, p = line[:2], line[3:].strip().strip('"')
        if code not in ("??", "A ", "AM") or not p.endswith(".md") or p.startswith("_"):
            continue
        if os.path.basename(p) == "_index.md":
            continue
        try:
            head, _ = split_frontmatter(_read(os.path.join(root, p)))
        except OSError:
            continue
        if head is not None and get_field(head, "source") == "granola":
            paths.append(p)
    return sorted(paths)


def committed_granola_notes(root, since, until=None):
    """Repo-relative paths of TRACKED granola notes (domain dirs; not dossiers/_infra/_index.md)
    whose provenance.ingested date is in [since, until] (inclusive; until=None -> open)."""
    out = _git(root, "ls-files", "-z", "--", "*.md").stdout
    paths = []
    for p in out.split("\0"):
        if not p or p.startswith(("_", "dossiers/", "digests/", "profile/")) \
                or os.path.basename(p) == "_index.md":
            continue
        try:
            head, _ = split_frontmatter(_read(os.path.join(root, p)))
        except OSError:
            continue
        if head is None or get_field(head, "source") != "granola":
            continue
        m = INGESTED_RE.search(head)
        if not m or m.group(1) < since or (until and m.group(1) > until):
            continue
        paths.append(p)
    return sorted(paths)


def load_list(fp):
    entries = []
    with open(fp, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as ex:
                raise ValueError(f"{fp}:{n}: not JSON ({ex})")
            if not isinstance(e, dict) or not isinstance(e.get("id"), str) \
                    or not isinstance(e.get("path"), str):
                raise ValueError(f"{fp}:{n}: need {{\"id\": str, \"path\": str}}")
            if e.get("mode", "edit") not in MODES:
                raise ValueError(f"{fp}:{n}: mode {e.get('mode')!r} not in {MODES}")
            entries.append(e)
    return entries


def list_mode(entries, fp="list"):
    """The single mode of a list ('edit' when no line carries one). Mixed modes are refused."""
    modes = {e.get("mode", "edit") for e in entries}
    if len(modes) > 1:
        raise ValueError(f"{fp}: mixed modes {sorted(modes)} — one list, one mode")
    return modes.pop() if modes else "edit"


def _repo_rel(root, fp):
    """Path relative to the repo, or None when fp lies outside it."""
    rel = os.path.relpath(os.path.realpath(fp), os.path.realpath(root))
    return None if rel.startswith("..") else rel


def _is_committed(root, fp):
    """True if fp is inside the repo and present in HEAD (a committed note). Paths outside the repo
    (e.g. a scratch test set) are never 'committed'."""
    rel = _repo_rel(root, fp)
    if rel is None:
        return False
    return _git(root, "cat-file", "-e", f"HEAD:{rel}", check=False).returncode == 0


def _is_dirty(root, fp):
    rel = _repo_rel(root, fp)
    return _git(root, "diff", "--quiet", "HEAD", "--", rel, check=False).returncode != 0


# ------------------------------------------------------------------ prior-fix history (propose)

def _body_lines(txt):
    _, body = split_frontmatter(txt)
    return (body if body is not None else txt).splitlines()


def note_history(root, rel):
    """Commits that changed the note's BODY after the commit that created it (followed across
    renames). Frontmatter-only commits (related:/updated: regen) are skipped. Returns
    {"created": {sha,date,subject}|None, "fixes": [{sha,date,subject,removed:[..],added:[..]}]}
    oldest first; removed/added are body lines (a body diff, not a raw git diff)."""
    out = _git(root, "log", "--follow", "--format=%x00%H%x09%ad%x09%s", "--date=short",
               "--name-only", "--", rel).stdout
    commits = []
    for blk in out.split("\0")[1:]:
        lines = [x for x in blk.splitlines() if x.strip()]
        sha, date, subject = (lines[0].split("\t", 2) + ["", ""])[:3]
        commits.append({"sha": sha, "date": date, "subject": subject,
                        "path": lines[-1] if len(lines) > 1 else rel})
    commits.reverse()
    hist = {"created": None, "fixes": []}
    prev = None
    for c in commits:
        r = _git(root, "show", f"{c['sha']}:{c['path']}", check=False)
        if r.returncode != 0:
            prev = None
            continue
        cur = _body_lines(r.stdout)
        meta = {"sha": c["sha"], "date": c["date"], "subject": c["subject"]}
        if prev is None:
            if hist["created"] is None:
                hist["created"] = meta
        else:
            removed, added = [], []
            for d in difflib.ndiff(prev, cur):
                if d.startswith("- ") and d[2:].strip():
                    removed.append(d[2:])
                elif d.startswith("+ ") and d[2:].strip():
                    added.append(d[2:])
            if removed or added:
                hist["fixes"].append(dict(meta, removed=removed, added=added))
        prev = cur
    return hist


def render_history(rel, hist):
    lines = [f"# Prior body changes to {rel}", ""]
    c = hist.get("created")
    lines.append(f"Created: {c['sha'][:8]} {c['date']} {c['subject']}" if c else "Created: (unknown)")
    if not hist["fixes"]:
        lines += ["", "No later commit changed the body — no prior fix to protect."]
    for f in hist["fixes"]:
        lines += ["", f"## {f['sha'][:8]} {f['date']} — {f['subject']}"]
        lines += [f"- REMOVED: {x}" for x in f["removed"]]
        lines += [f"+ ADDED:   {x}" for x in f["added"]]
    lines += ["", "Every ADDED line is text a later commit wrote on purpose (most are transcript-verified "
              "corrections). Do not propose reverting it to a REMOVED line; propose changing it only when "
              "the transcript PLAINLY contradicts it, and then set prior_fix_touched: true."]
    return "\n".join(lines) + "\n"


def fix_added_lines(hist):
    return [x.strip() for f in hist.get("fixes", []) for x in f.get("added", []) if len(x.strip()) >= 8]


def touches_prior_fix(current, added):
    cur = (current or "").strip()
    if not cur:
        return False
    for line in added:
        if cur in line or line in cur:
            return True
    return False


# ------------------------------------------------------------------ prepare

def prepare(root, list_path, results, notes=None, untracked=False, propose=False, since=None,
            until=None, exclude=(), skip_results=(DEFAULT_RESULTS,)):
    list_fp, res_dir = _abs(root, list_path), _abs(root, results)
    mode = "propose" if propose else "edit"
    if untracked:
        if propose:
            raise ValueError("--untracked is edit mode (new notes); propose takes --since or --notes")
        notes = untracked_granola_notes(root)
    if since is not None:
        if not propose:
            raise ValueError("--since selects COMMITTED notes, so it requires --propose")
        notes = committed_granola_notes(root, since, until)
    if notes is not None:
        entries = [{"id": None, "path": p} for p in notes]
    else:
        entries = load_list(list_fp)
        if list_mode(entries, list_fp) != mode:
            raise ValueError(f"{list_fp}: list mode is {list_mode(entries, list_fp)!r}, "
                             f"not {mode!r} — pass/omit --propose to match")
    excl = [x.lower() for x in exclude]
    errors, seen, kept, skipped = [], {}, [], []
    for e in entries:
        fp = _abs(root, e["path"])
        try:
            gid = note_id(fp)
        except (OSError, ValueError) as ex:
            errors.append(str(ex))
            continue
        if e["id"] is not None and e["id"] != gid:
            errors.append(f"{e['path']}: list id {e['id']} != note ref granola:{gid}")
            continue
        if any(gid.startswith(x) for x in excl):
            skipped.append(f"excluded: {e['path']}")
            continue
        if since is not None:   # a selection, not an explicit ask: skip already-verified notes
            prior = [d for d in skip_results if os.path.exists(os.path.join(_abs(root, d), gid + ".json"))]
            if prior:
                skipped.append(f"already has a result in {prior[0]}: {e['path']}")
                continue
        if gid in seen:
            errors.append(f"duplicate meeting id {gid}: {seen[gid]} and {e['path']}")
            continue
        committed = _is_committed(root, fp)
        if not propose and committed:
            errors.append(f"{e['path']}: already committed — the verifier auto-edits only NEW (uncommitted) "
                          "notes; on committed notes it can revert a verified fix (2026-09-27 test), so it is "
                          "refused. Use `prepare --propose` (proposals only, reviewed by the owner)")
            continue
        if propose and not committed:
            errors.append(f"{e['path']}: not committed — propose mode is for COMMITTED notes (it reads their "
                          "git history of prior fixes); a new note goes through edit mode (`prepare --untracked`)")
            continue
        if propose and _is_dirty(root, fp):
            errors.append(f"{e['path']}: has uncommitted changes — commit or finish them first (the snapshot "
                          "and the prior-fix history must describe the same text)")
            continue
        seen[gid] = e["path"]
        e["id"] = gid
        if propose:
            e["mode"] = "propose"
        kept.append(e)
    for m in skipped:
        print(f"skip {m}")
    if errors:
        for m in errors:
            print(f"ERROR {m}")
        return 1
    entries = kept
    os.makedirs(res_dir, exist_ok=True)
    pending = done = 0
    for e in entries:
        snap = os.path.join(res_dir, e["id"] + ".before.md")
        if os.path.exists(os.path.join(res_dir, e["id"] + ".json")):
            done += 1
            print(f"already {'proposed' if propose else 'verified'} (move {e['id']}.json + .before.md aside "
                  f"to redo): {e['path']}")
            continue
        pending += 1
        if not os.path.exists(snap):   # never overwrite: an existing one is the pre-edit original
            with open(snap, "w", encoding="utf-8") as f:
                f.write(_read(_abs(root, e["path"])))
        if propose:
            hj = os.path.join(res_dir, e["id"] + ".history.json")
            if not os.path.exists(hj):   # same rule as the snapshot: fixed at first prepare
                rel = _repo_rel(root, _abs(root, e["path"]))
                hist = note_history(root, rel)
                with open(hj, "w", encoding="utf-8") as f:
                    json.dump(hist, f, indent=1)
                with open(os.path.join(res_dir, e["id"] + ".history.md"), "w", encoding="utf-8") as f:
                    f.write(render_history(rel, hist))
    if notes is not None:
        os.makedirs(os.path.dirname(list_fp), exist_ok=True)
        with open(list_fp, "w", encoding="utf-8") as f:
            for e in entries:
                line = {"id": e["id"], "path": e["path"]}
                if propose:
                    line["mode"] = "propose"
                f.write(json.dumps(line) + "\n")
    print(f"granola-verify list ({mode}): {list_fp} — {len(entries)} note(s), {pending} pending, "
          f"{done} already done, {len(skipped)} skipped; snapshots in {res_dir}")
    return 0


# ------------------------------------------------------------------ check

def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def body_text(txt):
    """(prefix, rest): prefix = frontmatter + closing '---' line, rest = the note body proper. A
    `current` is matched ONLY in rest, so a replacement can never reach the frontmatter."""
    head, body = split_frontmatter(txt)
    if head is None:
        raise ValueError("no frontmatter")
    nl = body.find("\n", 1)
    cut = len(head) + (len(body) if nl < 0 else nl + 1)
    return txt[:cut], txt[cut:]


def iso_to_spoken(p):
    """A date proposal that drops every ISO date without writing another one — the harmless-noise
    class the propose prompt suppresses; flagged in check/review as a backstop."""
    return p.get("kind") == "date" and bool(ISO_RE.search(p.get("current", ""))) \
        and not ISO_RE.search(p.get("proposed", "")) and bool(_norm(p.get("proposed")))


def _load_proposals(res_fp):
    res = json.loads(_read(res_fp))
    props = res["proposals"]
    unver = res.get("unverifiable", [])
    assert isinstance(props, list) and isinstance(unver, list), "proposals/unverifiable not lists"
    for i, p in enumerate(props, 1):
        assert isinstance(p, dict), f"proposal {i} not an object"
        for k in ("current", "proposed", "transcript_quote"):
            assert isinstance(p.get(k), str), f"proposal {i}: {k} not a string"
        assert p.get("kind") in KINDS, f"proposal {i}: kind {p.get('kind')!r}"
        assert isinstance(p.get("prior_fix_touched"), bool), f"proposal {i}: prior_fix_touched not bool"
        assert p["current"].strip(), f"proposal {i}: empty current"
        assert p["current"] != p["proposed"], f"proposal {i}: current == proposed"
    return res, props, unver


def check(root, list_path, results):
    list_fp, res_dir = _abs(root, list_path), _abs(root, results)
    entries = load_list(list_fp)
    if list_mode(entries, list_fp) == "propose":
        return check_propose(root, list_fp, res_dir, entries)
    fails, warns, n_changes, n_unver = [], [], 0, 0
    for e in entries:
        gid, fp = e["id"], _abs(root, e["path"])
        snap = os.path.join(res_dir, gid + ".before.md")
        res_fp = os.path.join(res_dir, gid + ".json")
        if not os.path.exists(snap):
            fails.append(f"{e['path']}: no snapshot {snap} (run prepare first)")
            continue
        if not os.path.exists(res_fp):
            fails.append(f"{e['path']}: no result {res_fp} (not verified / run incomplete)")
            continue
        try:
            res = json.loads(_read(res_fp))
            changes = res["changes"]
            unver = res.get("unverifiable", [])
            assert isinstance(changes, list) and isinstance(unver, list)
        except (ValueError, KeyError, AssertionError) as ex:
            fails.append(f"{res_fp}: malformed result ({ex!r})")
            continue
        n_changes += len(changes)
        n_unver += len(unver)
        before_head, before_body = split_frontmatter(_read(snap))
        now_head, now_body = split_frontmatter(_read(fp))
        if now_head is None or now_head != before_head:
            fails.append(f"{e['path']}: FRONTMATTER CHANGED by the verifier (body-only contract) — "
                         f"restore it from {snap}")
            continue
        body_changed = _norm(before_body) != _norm(now_body)
        if body_changed and not changes:
            fails.append(f"{e['path']}: body edited but the result logs NO changes (unlogged edit)")
        if changes and not body_changed:
            fails.append(f"{e['path']}: result logs {len(changes)} change(s) but the body is unchanged")
        for c in changes:
            after = _norm(c.get("after"))
            if after and after not in _norm(now_body):
                warns.append(f"{e['path']}: logged 'after' text not found verbatim in the note: {after[:80]!r}")
            if not _norm(c.get("transcript_quote")):
                warns.append(f"{e['path']}: a change carries no transcript_quote")
    for m in warns:
        print(f"WARN {m}")
    for m in fails:
        print(f"FAIL {m}")
    print(f"granola-verify check: {len(entries)} note(s), {n_changes} change(s), "
          f"{n_unver} unverifiable claim(s), {len(fails)} failure(s), {len(warns)} warning(s)")
    return 1 if fails else 0


def check_propose(root, list_fp, res_dir, entries):
    """Propose-mode gate: the verifier must have edited NOTHING (note byte-identical to its
    snapshot) and every result must be a well-formed proposal set."""
    fails, warns, n_props, n_unver, n_err = [], [], 0, 0, 0
    for e in entries:
        gid, fp = e["id"], _abs(root, e["path"])
        snap = os.path.join(res_dir, gid + ".before.md")
        res_fp = os.path.join(res_dir, gid + ".json")
        if not os.path.exists(snap):
            fails.append(f"{e['path']}: no snapshot {snap} (run prepare --propose first)")
            continue
        if _read(fp) != _read(snap):
            fails.append(f"{e['path']}: NOTE CHANGED during a propose run (propose mode edits nothing) — "
                         f"restore it from {snap}")
        if not os.path.exists(res_fp):
            fails.append(f"{e['path']}: no result {res_fp} (run incomplete)")
            continue
        try:
            res, props, unver = _load_proposals(res_fp)
        except (ValueError, KeyError, AssertionError) as ex:
            fails.append(f"{res_fp}: malformed propose result ({ex})")
            continue
        if res.get("status") == "error":
            n_err += 1
            warns.append(f"{e['path']}: verifier error — {res.get('error')}")
        n_props += len(props)
        n_unver += len(unver)
        _, rest = body_text(_read(snap))
        for i, p in enumerate(props, 1):
            k = rest.count(p["current"])
            if k != 1:
                warns.append(f"{e['path']} #{i}: `current` occurs {k}x in the body (apply will refuse): "
                             f"{p['current'][:70]!r}")
            if iso_to_spoken(p):
                warns.append(f"{e['path']} #{i}: ISO date -> spoken phrase (suppressed noise class; review)")
            if not _norm(p["transcript_quote"]):
                warns.append(f"{e['path']} #{i}: no transcript_quote")
    for m in warns:
        print(f"WARN {m}")
    for m in fails:
        print(f"FAIL {m}")
    print(f"granola-verify check (propose): {len(entries)} note(s), {n_props} proposal(s), {n_unver} "
          f"unverifiable claim(s), {n_err} verifier error(s), {len(fails)} failure(s), {len(warns)} warning(s)")
    return 1 if fails else 0


# ------------------------------------------------------------------ review

def _md_quote(s):
    return (s or "").replace("\n", " / ").strip()


def review(root, list_path, results, out_md):
    """Number every proposal (list order, then result order) into one markdown file for the owner plus a
    machine index (.json, same stem) that `apply` reads — the numbers are fixed by that index."""
    list_fp, res_dir = _abs(root, list_path), _abs(root, results)
    entries = load_list(list_fp)
    if list_mode(entries, list_fp) != "propose":
        raise ValueError(f"{list_fp}: not a propose list")
    out_md = _abs(root, out_md)
    out_json = os.path.splitext(out_md)[0] + ".json"
    if os.path.exists(out_json + ".applied.jsonl"):
        raise ValueError(f"{out_json} already has applied proposals — renumbering would re-point them; "
                         "write the review under another --out")
    index, md, missing, n_notes = [], [], [], 0
    md_head = [f"# Granola transcript-verify proposals ({os.path.basename(out_md)})", ""]
    for e in entries:
        res_fp = os.path.join(res_dir, e["id"] + ".json")
        if not os.path.exists(res_fp):
            missing.append(e["path"])
            continue
        res, props, unver = _load_proposals(res_fp)
        hj = os.path.join(res_dir, e["id"] + ".history.json")
        added = fix_added_lines(json.loads(_read(hj))) if os.path.exists(hj) else []
        try:
            _, rest = body_text(_read(_abs(root, e["path"])))
        except (OSError, ValueError):
            rest = ""
        if not props and not unver and res.get("status") != "error":
            continue
        n_notes += 1
        md += [f"## {e['path']}", ""]
        if res.get("status") == "error":
            md += [f"**Verifier error:** {res.get('error')}", ""]
        for p in props:
            n = len(index) + 1
            det = touches_prior_fix(p["current"], added)
            flags = []
            if p["prior_fix_touched"] or det:
                flags.append("PRIOR FIX" + ("" if p["prior_fix_touched"] == det else
                                            (" (history match only)" if det else " (verifier only)")))
            if iso_to_spoken(p):
                flags.append("ISO→spoken")
            k = rest.count(p["current"])
            if k != 1:
                flags.append("NOT FOUND" if k == 0 else f"AMBIGUOUS ×{k}")
            md += [f"**{n}.** `{p['kind']}`" + (f" — ⚑ {', '.join(flags)}" if flags else ""),
                   f"- current: {_md_quote(p['current'])}",
                   f"- proposed: {_md_quote(p['proposed']) or '(delete)'}",
                   f"- transcript: {_md_quote(p['transcript_quote'])}", ""]
            index.append({"n": n, "id": e["id"], "path": e["path"], "kind": p["kind"],
                          "current": p["current"], "proposed": p["proposed"],
                          "transcript_quote": p["transcript_quote"],
                          "prior_fix_touched": p["prior_fix_touched"], "prior_fix_history_match": det})
        if unver:
            md += ["<details><summary>unverifiable (" + str(len(unver)) + ")</summary>", ""]
            md += [f"- {_md_quote(u)}" for u in unver] + ["", "</details>", ""]
    md_head += [f"{len(index)} proposal(s) across {n_notes} note(s); {len(entries)} note(s) in "
                f"{os.path.relpath(list_fp, root) if _repo_rel(root, list_fp) else list_fp}"
                + (f"; {len(missing)} without a result" if missing else "") + ".",
                "", "Apply the ones you accept: `python3 scripts/granola_verify.py apply --ids 1,4,7 "
                f"--proposals {os.path.relpath(out_json, root) if _repo_rel(root, out_json) else out_json}` "
                "then `bash scripts/check.sh`.",
                "Flags: PRIOR FIX = the line was written by a later commit (likely a verified correction); "
                "ISO→spoken = replaces a written date with the spoken phrase; NOT FOUND/AMBIGUOUS = apply "
                "will refuse.", ""]
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md_head + md))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"list": list_path, "results": results, "proposals": index}, f, indent=1)
    for p in missing:
        print(f"WARN no result for {p}")
    print(f"granola-verify review: {len(index)} proposal(s), {n_notes} note(s) -> {out_md} (+ {out_json})")
    return 0


# ------------------------------------------------------------------ apply

def _line_span(text, i, j):
    """(start, end_incl_newline) if text[i:j] is exactly one or more whole lines, else None."""
    if (i == 0 or text[i - 1] == "\n") and (j == len(text) or text[j] == "\n"):
        return i, (j + 1 if j < len(text) else j)
    return None


def _section_of(text, pos):
    heads = [m for m in re.finditer(r"(?m)^## +(.+?)[ \t]*$", text) if m.start() <= pos]
    return heads[-1].group(1) if heads else None


def _add_open_question(text, line):
    m = re.search(r"(?m)^## +Open questions[ \t]*$", text)
    if m:
        nxt = re.search(r"(?m)^## ", text[m.end():])
        end = m.end() + nxt.start() if nxt else len(text)
        sect = text[m.end():end].rstrip("\n")
        return text[:m.end()] + sect + "\n" + line + "\n" + ("\n" if nxt else "") + text[end:]
    ref = re.search(r"(?m)^## +References[ \t]*$", text)
    block = "## Open questions\n" + line + "\n"
    if ref:
        return text[:ref.start()] + block + "\n" + text[ref.start():]
    return text.rstrip("\n") + "\n\n" + block


def _drop_empty_decisions(text):
    m = re.search(r"(?m)^## +Decisions[ \t]*\n", text)
    if not m:
        return text
    nxt = re.search(r"(?m)^## ", text[m.end():])
    end = m.end() + nxt.start() if nxt else len(text)
    if text[m.end():end].strip():
        return text
    return text[:m.start()] + text[end:]


def apply_one(rest, p):
    """Apply one proposal to the note body `rest`; returns the new body or raises ValueError. The
    match must be verbatim and unique. A `decision` proposal whose `current` is a whole line under
    `## Decisions` is MOVED: removed there, `proposed` appended under `## Open questions` (created
    before `## References`, or at the end); an emptied Decisions section is dropped. An empty
    `proposed` on a whole line deletes the line."""
    cur, new = p["current"], p["proposed"]
    k = rest.count(cur)
    if k == 0:
        raise ValueError("`current` not found verbatim in the body")
    if k > 1:
        raise ValueError(f"`current` occurs {k} times in the body — ambiguous")
    i = rest.index(cur)
    j = i + len(cur)
    span = _line_span(rest, i, j)
    if p.get("kind") == "decision" and span and _norm(new) \
            and (_section_of(rest, i) or "").lower() == "decisions":
        out = rest[:span[0]] + rest[span[1]:]
        return _add_open_question(_drop_empty_decisions(out), new.strip("\n"))
    if not new and span:
        return rest[:span[0]] + rest[span[1]:]
    return rest[:i] + new + rest[j:]


def apply(root, proposals_path, ids):
    idx_fp = _abs(root, proposals_path)
    index = json.loads(_read(idx_fp))["proposals"]
    by_n = {p["n"]: p for p in index}
    applied_fp = idx_fp + ".applied.jsonl"
    done = set()
    if os.path.exists(applied_fp):
        done = {json.loads(x)["n"] for x in _read(applied_fp).splitlines() if x.strip()}
    errors = []
    for n in ids:
        if n not in by_n:
            errors.append(f"#{n}: no such proposal in {idx_fp}")
        elif n in done:
            errors.append(f"#{n}: already applied (see {applied_fp})")
    if len(set(ids)) != len(ids):
        errors.append("duplicate ids")
    texts = {}
    if not errors:
        for n in sorted(ids):
            p = by_n[n]
            fp = _abs(root, p["path"])
            if fp not in texts:
                texts[fp] = body_text(_read(fp))
            prefix, rest = texts[fp]
            try:
                texts[fp] = (prefix, apply_one(rest, p))
            except ValueError as ex:
                errors.append(f"#{n} {p['path']}: {ex}: {p['current'][:80]!r}")
    if errors:
        for m in errors:
            print(f"REFUSED {m}")
        print("granola-verify apply: nothing written (all-or-nothing) — drop the refused ids and re-run")
        return 1
    for fp, (prefix, rest) in texts.items():
        old = _read(fp)
        new = prefix + rest
        assert split_frontmatter(new)[0] == split_frontmatter(old)[0], "frontmatter would change"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(new)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(applied_fp, "a", encoding="utf-8") as f:
        for n in sorted(ids):
            f.write(json.dumps({"n": n, "id": by_n[n]["id"], "path": by_n[n]["path"], "applied_at": stamp}) + "\n")
    print(f"granola-verify apply: {len(ids)} proposal(s) applied to {len(texts)} note(s). "
          "Next: bash scripts/check.sh (the orchestrator runs it), then commit.")
    return 0


def _ids(s):
    try:
        return [int(x) for x in s.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--ids wants comma-separated integers, got {s!r}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    today = datetime.date.today().isoformat()
    for name in ("prepare", "check", "review", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=ROOT)
        if name == "apply":
            p.add_argument("--ids", type=_ids, required=True, help="comma-separated proposal numbers")
            p.add_argument("--proposals", default=f"{PROPOSALS_STEM}{today}.json",
                           help="the review index (.json) the numbers refer to")
            continue
        p.add_argument("--list")
        p.add_argument("--results")
        if name in ("prepare", "check"):
            p.add_argument("--propose", action="store_true",
                           help="PROPOSE mode (committed notes; proposals only, no edits)")
        if name == "review":
            p.add_argument("--date", default=today)
            p.add_argument("--out", help="markdown path (default _sources/granola-verify-proposals-<date>.md)")
        if name == "prepare":
            g = p.add_mutually_exclusive_group()
            g.add_argument("--untracked", action="store_true",
                           help="list every untracked granola note (a sync wave's new notes)")
            g.add_argument("--notes", nargs="+", help="explicit note paths (repo-relative or absolute)")
            g.add_argument("--since", help="propose: committed granola notes ingested on/after YYYY-MM-DD")
            p.add_argument("--until", help="with --since: ingested on/before YYYY-MM-DD")
            p.add_argument("--exclude", nargs="+", default=[],
                           help="meeting ids (or id prefixes) to leave out")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "apply":
            return apply(a.root, a.proposals, a.ids)
        propose = a.cmd == "review" or a.propose
        lst = a.list or (PROPOSE_LIST if propose else DEFAULT_LIST)
        res = a.results or (PROPOSE_RESULTS if propose else DEFAULT_RESULTS)
        if a.cmd == "prepare":
            if a.until and not a.since:
                raise ValueError("--until needs --since")
            return prepare(a.root, lst, res, notes=a.notes, untracked=a.untracked, propose=a.propose,
                           since=a.since, until=a.until, exclude=a.exclude)
        if a.cmd == "review":
            return review(a.root, lst, res, a.out or f"{PROPOSALS_STEM}{a.date}.md")
        if a.propose and list_mode(load_list(_abs(a.root, lst)), lst) != "propose":
            raise ValueError(f"{lst}: --propose given but the list is not a propose list")
        return check(a.root, lst, res)
    except (OSError, ValueError) as ex:
        print(f"ERROR {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
