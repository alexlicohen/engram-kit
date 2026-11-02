#!/usr/bin/env python3
"""Shared frontmatter-diff primitive for the in-place note-rewrite guard (Feature B, plan
generic-bubbling-eagle.md). ONE OWNER of "did an in-place rewrite change a field it promised to
preserve?" — imported by scripts/verify_frontmatter_preserved.py (the check.sh redistill/dossier
gate) and scripts/reconcile_dossier.py (the guarded dossier writer). Do NOT paste a local copy of
this logic or of PROFILES.

    from _lib.frontmatter_diff import diff_frontmatter, PROFILES

Reuses _lib.notes.get_field for scalar frontmatter access (does NOT reimplement frontmatter
parsing). Nested provenance.<child> keys, which get_field's `^key:` anchor cannot reach (they are
indented under `provenance:`), are read by a small block-scoped getter here.
"""
import json
import os
import re
import subprocess

from _lib.notes import get_field

# The sanctioned-override ledger: DELIBERATE frozen-field changes made via a guarded writer
# (edit_note_frontmatter.py, clarify_writeback.py), each
# `{path, field, kind, old, new, head_sha, date, reason}`. The check.sh redistill/dossier gate
# consults it to excuse a frozen-field violation — the only way a by-hand title/provenance.ref fix,
# or a people-dedup participants rename, passes the gate. Append-only, committed, one owner (loaded
# by verify_frontmatter_preserved.py through `load_overrides`).
#
# LIVENESS — what the gate actually guarantees (corrected 2026-08-02; the previous claim was FALSE):
# an entry excuses a violation ONLY while the ledger line itself is UNCOMMITTED, i.e. in the window
# between the guarded edit and the commit that lands it. `load_overrides` subtracts every entry
# already present in `HEAD:_manifest/frontmatter-overrides.jsonl`, so a committed line is inert —
# permanently, and independently of what the note's content later becomes.
#
# The earlier design relied on `head_sha` alone for that inertness and claimed a transition "can't
# re-arm if HEAD later returns to `old`". It can: a git blob sha is a pure CONTENT hash, so restoring
# the pre-fix blob (`git checkout <pre-fix> -- research/n1.md`, a revert, a bad merge) makes
# `HEAD:<path>` equal the recorded sha again and re-arms the committed entry — silently
# re-authorizing the same frozen-field change with no guarded writer and no new ledger line. With
# ~112 committed lines that was 112 latent re-authorizations, growing monotonically. Dropping
# committed entries removes the whole class.
#
# `head_sha` still binds each entry to the note's HEAD blob at record time (== the PRE-edit content,
# since the writer requires a clean note), and the gate still re-checks it. Its job is narrower than
# advertised: it is an EXPIRY/precision check (the entry stops matching the moment the note's HEAD
# moves), NOT an authenticity one — `git rev-parse HEAD:<path>` is a one-command public lookup, so
# anyone hand-appending a line can name it. Authenticity comes from the line being new, reviewed
# (check.sh prints every uncommitted line — see `ledger_audit`) and unmodifiable after the fact.
# Legacy entries with no head_sha (pre-2026-07-20) never match — they are all committed + inert.
OVERRIDES_LEDGER = os.path.join("_manifest", "frontmatter-overrides.jsonl")

# Fields NO ledger entry may ever excuse, whatever it says. `verified:` is the human-only epistemic
# signal (CLAUDE.md): no automated writer may set it — edit_note_frontmatter.py refuses it outright —
# so a ledger line naming it can only be forged or mistaken. Hard-denied in check_path.
UNEXCUSABLE_FIELDS = frozenset({"verified"})


def head_blob_sha(root, relpath):
    """The git blob sha of `relpath` at HEAD, or '' if untracked. Single owner — the writer records
    it, the gate re-checks it."""
    r = subprocess.run(["git", "rev-parse", f"HEAD:{relpath}"], cwd=root,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def git_show_head(root, relpath):
    """The committed (HEAD) content of `relpath`, or None if it is not tracked at HEAD. Single owner
    of "what did this file look like at HEAD" for the preserve-gate family — the gate
    (verify_frontmatter_preserved.py) and both guarded writers (edit_note_frontmatter.py,
    clarify_writeback.py) must read the SAME baseline the gate diffs against."""
    r = subprocess.run(["git", "show", f"HEAD:{relpath}"], cwd=root, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def override_key(path, field, old, new, kind, head_sha):
    """The EXACT tuple the gate matches a violation against. SINGLE OWNER of the match shape, used by
    both `load_overrides` (ledger side) and verify_frontmatter_preserved.check_path (violation side)
    so the two cannot drift.

    `kind` is part of the key (added 2026-08-02): diff_frontmatter emits "changed" (a frozen field
    drifted) and "removed" (an append-only field dropped members) with the SAME (field, old, new)
    payload shape, so leaving kind out let a `removed` excuse — the only kind clarify_writeback mints
    — cover a `changed` violation on the same values.

    str() coerces so a hand-recorded list/dict value can't raise an unhashable TypeError out of the
    whole gate — it just fails to match (fail-closed)."""
    return (str(path), str(field), str(old), str(new), str(kind), str(head_sha))


def override_entry(path, field, old, new, head_sha, date, reason, *, kind):
    """One sanctioned-override ledger record, carrying exactly the fields `override_key` matches on
    plus the human audit fields. SINGLE OWNER of the record shape — every guarded writer that records
    a sanctioned frozen-field transition builds its entries here rather than hand-assembling a dict,
    so the writer side and the gate side cannot drift apart. `old` MUST be the value the gate will
    read from `HEAD:<path>` and `new` the value it will read back from the working tree, and `kind`
    the diff_frontmatter kind the gate will report ("changed" for a frozen scalar/list field,
    "removed" for a dropped append-only member) — or the entry silently fails to excuse anything.
    `kind` is keyword-only and has no default ON PURPOSE: a writer must state which violation it is
    sanctioning, never inherit one."""
    return {"path": path, "field": field, "kind": kind, "old": old, "new": new,
            "head_sha": head_sha, "date": date, "reason": reason}


def serialize_overrides(entries):
    """Render override records as appendable JSONL text (one line each, trailing newline). SINGLE
    OWNER of the on-disk ledger line format, shared by edit_note_frontmatter.py (append-then-truncate
    rollback) and clarify_writeback.py (whole-file transactional write)."""
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)


def _ledger_keys(text):
    """JSONL ledger text -> set of `override_key` tuples. A line missing `head_sha`/`kind` records ""
    (legacy — never matches a real sha/kind). Malformed lines are SKIPPED (fail-closed: a bad line
    must not weaken the gate, nor crash it)."""
    out = set()
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            out.add(override_key(r["path"], r["field"], r["old"], r["new"],
                                 r.get("kind", ""), r.get("head_sha", "")))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue
    return out


def _ledger_lines(text):
    """Non-blank raw lines of a ledger text ('' / None -> [])."""
    return [ln for ln in (text or "").split("\n") if ln.strip()]


def _split_committed(root):
    """(committed_lines, pending_lines) of the working-tree ledger — HEAD's lines are its PREFIX
    (append-only; `ledger_audit` is what fails the gate when they are not)."""
    path = os.path.join(root, OVERRIDES_LEDGER)
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    work = _ledger_lines(text)
    n_head = len(_ledger_lines(git_show_head(root, OVERRIDES_LEDGER)))
    return work[:n_head], work[n_head:]


def merge_overrides(root, entries):
    """The FULL new ledger text with `entries` recorded — COLLAPSING each into an existing
    UNCOMMITTED entry for the same (path, field, kind, head_sha) instead of appending a stale
    intermediate beside it. SINGLE OWNER of "record a sanctioned override on disk".

    WHY (L4, 2026-08-04): /clarify applies items one at a time and commits nothing between them, so
    two merges routinely land on one note before either is committed — participants A->B, then
    B->C. Both entries are computed against HEAD, so the SECOND already records the whole A->C
    transition and the first names a value the note never ends at. Inert once committed, but during
    the uncommitted window a stale A->B is a LIVE standing excuse for a state the store never
    reached: a concurrent unrelated edit landing exactly on B would have passed the gate on the
    strength of it. Five such entries shipped in the 2026-08-01 dedup batch. After collapsing there
    is exactly ONE entry per (path, field) — the transition that actually happened.

    Only the UNCOMMITTED tail is rewritten; the committed prefix is copied through byte-for-byte
    (the ledger is append-only and `ledger_audit` fails on any change to it). `head_sha` is part of
    the collapse key on purpose: two entries bound to DIFFERENT HEAD blobs describe transitions from
    different baselines and must not be fused. A collapse whose endpoints are equal (A->B->A)
    records nothing at all — the note is back at HEAD's value, so no violation is left to excuse.
    """
    committed, pending = _split_committed(root)
    parsed = []
    for ln in pending:
        try:
            obj = json.loads(ln)
            parsed.append(obj if isinstance(obj, dict) else None)
        except json.JSONDecodeError:
            parsed.append(None)              # an unparseable pending line is carried through as-is
    out = list(pending)

    def _same_slot(a, b):
        return all(str(a.get(k, "")) == str(b.get(k, ""))
                   for k in ("path", "field", "kind", "head_sha"))

    for e in entries:
        idx = next((i for i in range(len(out) - 1, -1, -1)
                    if parsed[i] and _same_slot(parsed[i], e)), None)
        if idx is None:
            out.append(json.dumps(e, ensure_ascii=False))
            parsed.append(e)
            continue
        prev = parsed[idx]
        merged = dict(e)
        merged["old"] = prev.get("old", e["old"])          # the START of the chain, not the middle
        merged["reason"] = " | ".join(x for x in (prev.get("reason", ""), e.get("reason", "")) if x)
        if merged["old"] == merged["new"]:
            del out[idx]                                   # round trip — nothing left to excuse
            del parsed[idx]
            continue
        out[idx] = json.dumps(merged, ensure_ascii=False)
        parsed[idx] = merged
    return "".join(ln + "\n" for ln in committed + out)


def load_overrides(root):
    """The LIVE sanctioned-override entries: every working-tree ledger key MINUS every key already
    committed at HEAD. Missing/empty file => empty set.

    Subtracting HEAD is what makes an entry go permanently inert once committed (see the
    OVERRIDES_LEDGER note above). The `head_sha` binding alone did NOT achieve that — blob shas are
    content hashes, so restoring a note's pre-fix content re-armed its long-committed entry. Liveness
    now means "this ledger line is not committed yet", which is exactly the window the design always
    claimed: between the guarded edit and its commit.

    Consequence to know when operating the store: committing the LEDGER without the note it excuses
    turns the gate red until the note is committed too. That is correct — the two belong in one
    commit, and check.sh's ledger REVIEW block (see `ledger_audit`) shows the pending lines."""
    path = os.path.join(root, OVERRIDES_LEDGER)
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        live = _ledger_keys(fh.read())
    live -= _ledger_keys(git_show_head(root, OVERRIDES_LEDGER))
    return live


def ledger_audit(root):
    """Audit the override ledger's OWN diff against HEAD. Returns (ok, new_lines, problems).

    Nothing else looks at it: check.sh's `pre_mod_notes`/`notes_changed` greps both exclude
    `_manifest/`, so a committed ledger line could be rewritten in place with no signal anywhere.
    The ledger is APPEND-ONLY by design, so HEAD's lines must be an exact PREFIX of the working
    tree's; a modified or removed committed line is a hard failure (`ok` False). `new_lines` are the
    appended lines, which check.sh prints as a mandatory REVIEW block — every one of them is a
    live frozen-field excuse until it is committed."""
    head_lines = _ledger_lines(git_show_head(root, OVERRIDES_LEDGER))
    path = os.path.join(root, OVERRIDES_LEDGER)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            work_lines = _ledger_lines(fh.read())
    else:
        work_lines = []

    problems = []
    if len(work_lines) < len(head_lines):
        problems.append(f"{len(head_lines) - len(work_lines)} committed ledger line(s) were REMOVED "
                        f"(HEAD has {len(head_lines)}, the working tree has {len(work_lines)}) — the "
                        f"ledger is append-only")
    for i in range(min(len(work_lines), len(head_lines))):
        if work_lines[i] != head_lines[i]:
            problems.append(f"committed ledger line {i + 1} was MODIFIED in place:\n"
                            f"      HEAD: {head_lines[i]}\n"
                            f"      NOW:  {work_lines[i]}")
    return (not problems), work_lines[len(head_lines):], problems

# Fields whose frontmatter value is an inline YAML list `[a, b, c]`. For PRESERVE fields these are
# compared as SETS (order-independent — reordering tags/aliases is not corruption); the
# superset-checked mutable field (participants) is compared for dropped members.
_LIST_FIELDS = {"aliases", "tags", "related", "supersedes", "superseded_by", "participants"}

# The ONLY mutable field that is checked, and only for REMOVED members (append-only): a redistill
# may ADD a participant (a human newly appearing in grown messages) but must never DROP one.
# Everything else in a profile's `mutable` set (body, updated, generated, and — dossier profile —
# related/supersedes/superseded_by) is freely mutable and not checked here.
_SUPERSET_FIELDS = {"participants"}

# Named preserve/mutable profiles — the single source of truth for what each in-place rewrite flow
# is allowed to touch. Verified against the live distiller prompts on 2026-07-12
# (_gmail-pending/gmail_update.mjs, _granola-pending/granola_redistill.mjs).
PROFILES = {
    # gmail_update.mjs + granola_redistill.mjs: they rewrite the BODY and stamp `updated:`, and may
    # only APPEND participants. Everything curated — identity, the provenance ref/method, the
    # synthesis-pass cross-links, and the human-only `verified` signal (Feature A) — stays frozen.
    "redistill": {
        "preserve": {"title", "date", "aliases", "tags", "domain", "subpath", "type",
                     "source", "status", "provenance.ref", "provenance.ingested",
                     "provenance.method", "related", "supersedes", "superseded_by", "verified"},
        "mutable": {"body", "updated", "participants"},
    },
    # Dossier reconciliation: rewriting the body AND the related/supersedes/superseded_by cross-links
    # IS the job, so those are mutable; the note's identity (title/domain/subpath/type/source and a
    # provenance ref if one is present) stays frozen.
    "dossier": {
        "preserve": {"title", "domain", "subpath", "type", "source", "provenance.ref"},
        "mutable": {"body", "updated", "generated", "related", "supersedes", "superseded_by"},
    },
}

# The HEAD `source:` values that route a modified note through the `redistill` profile above — the
# distilled connector sources, i.e. the ones that HAVE an in-place machine rewrite flow (gmail_update
# / granola_redistill / the fidelity-audit body-fix passes). Everything else (obsidian = verbatim,
# gdrive/manual/claude-chat) keeps check.sh's blunt SUSPICIOUS halt.
#
# SINGLE OWNER (2026-08-02). It lives beside PROFILES because it IS the redistill profile's scope —
# adding a sixth distilled source is one edit here, and both consumers follow. It used to be
# hardcoded in three places (check.sh's `case` list, clarify_writeback.REDISTILL_SOURCES, and prose
# in edit_note_frontmatter.py that conceded it was hand-synced); missing one copy when adding a
# source silently reopens the seam this gate closed. check.sh no longer classifies notes itself at
# all — it calls `verify_frontmatter_preserved.py --modified notes` (which routes via `--classify`), reading this set AND reading
# `source:` through the frontmatter-scoped getter (its old `grep -m1 '^source:'` was whole-file, so a
# body line `source: gmail` could route a hand-edited note into the precise gate instead of the halt).
REDISTILL_SOURCES = frozenset({"gmail", "granola", "outlook", "imessage", "whatsapp"})


def _get(head, field):
    """Frontmatter value for `field`. Dotted keys (provenance.ref / provenance.method) are read from
    the indented child line inside the named parent block; everything else via the shared get_field
    scalar getter. Returns '' when absent (matching get_field)."""
    if head is None:
        return ""
    if "." in field:
        parent, child = field.split(".", 1)
        return _get_nested(head, parent, child)
    return get_field(head, field)


def _get_nested(head, parent, child):
    """Value of an indented `child:` line inside the `parent:` block (e.g. provenance.ref)."""
    in_block = False
    for ln in head.split("\n"):
        if re.match(rf'^{re.escape(parent)}:\s*$', ln):
            in_block = True
            continue
        if in_block:
            if ln[:1] and not ln[:1].isspace():   # dedent to a new top-level key => block ended
                break
            m = re.match(rf'^\s+{re.escape(child)}:\s*(.*)$', ln)
            if m:
                return m.group(1).strip().strip('"')
    return ""


def parse_inline_list(val):
    """Tokenize an inline YAML list `[a, "b c", 'd']` into its RAW members — whitespace-stripped but
    QUOTING PRESERVED (`['a', '"b c"', "'d'"]`). A non-bracketed scalar becomes a single-element list
    (or [] when empty). engram list frontmatter is uniformly inline; member names never contain a
    literal comma, so a comma split is safe.

    SINGLE OWNER of "split an inline frontmatter list into members". `_parse_list` (the diff's
    set-comparison view) and clarify_writeback._rewrite_participants (which must re-render the list
    with each member's original quoting intact) are both built on it — they used to tokenize
    separately, and the rewriter's copy was quote-blind, so it silently skipped every note whose
    `participants:` members are quoted (~128 notes)."""
    val = (val or "").strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [x.strip() for x in inner.split(",") if x.strip()]
    return [val] if val else []


def unquote_member(token):
    """A raw inline-list member -> its bare value (`'"Samuel Testcase"'` -> `'Samuel Testcase'`)."""
    return token.strip().strip('"').strip("'")


def render_inline_list(members):
    """Raw members -> the inline YAML list text engram frontmatter uses (`[a, "b c"]`)."""
    return "[" + ", ".join(members) + "]"


def _parse_list(val):
    """Inline YAML list -> stripped, UNQUOTED members (the set-comparison view used by the diff)."""
    return [unquote_member(x) for x in parse_inline_list(val)]


def _base_field(field):
    """Bare field name for list/scalar classification (provenance.ref -> ref)."""
    return field.split(".")[-1]


def diff_frontmatter(old_head, new_head, preserve_fields, mutable_fields):
    """Return a list of violation records (dicts) describing every way `new_head` broke the
    preserve/mutable contract relative to `old_head`.

    * preserve_fields: ANY change is a violation. List-valued fields (tags, related, aliases, ...)
      are compared as SETS (order-independent); scalars by exact (quote-stripped) value.
    * mutable_fields: only `participants` is checked, and only for REMOVED members (append-only);
      every other mutable field is unchecked (body/updated/generated/related/... may change freely
      per profile).

    Each record: {"field", "old", "new", "kind"} where kind is "changed" (a frozen field drifted)
    or "removed" (an append-only field dropped members; also carries "removed": [dropped])."""
    violations = []
    for field in sorted(preserve_fields):
        old_v, new_v = _get(old_head, field), _get(new_head, field)
        if _base_field(field) in _LIST_FIELDS:
            if set(_parse_list(old_v)) != set(_parse_list(new_v)):
                violations.append({"field": field, "old": old_v, "new": new_v, "kind": "changed"})
        elif old_v != new_v:
            violations.append({"field": field, "old": old_v, "new": new_v, "kind": "changed"})
    for field in sorted(set(mutable_fields) & _SUPERSET_FIELDS):
        old_v, new_v = _get(old_head, field), _get(new_head, field)
        removed = sorted(set(_parse_list(old_v)) - set(_parse_list(new_v)))
        if removed:
            violations.append({"field": field, "old": old_v, "new": new_v,
                               "kind": "removed", "removed": removed})
    return violations
