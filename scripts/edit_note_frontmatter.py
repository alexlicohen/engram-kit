#!/usr/bin/env python3
"""Guarded, rollback-safe writer for a DELIBERATE frozen-frontmatter-field change on a note.

The problem it solves: the redistill preserve gate (`verify_frontmatter_preserved.py`, wired into
check.sh) hard-fails ANY change to a frozen identity field — title, date, provenance.ref, domain,
etc. — on a note from a distilled connector source (gmail/granola/outlook/imessage/whatsapp). That is
correct for catching an accidental in-place re-distill, but it left NO sanctioned path to make a
CORRECT such change by hand: a fidelity audit that finds a note's TITLE is factually wrong, or a
chunk-DB note whose `provenance.ref` points at the wrong chunk (see check_ref_integrity.py), cannot
be fixed without tripping the gate. This is the guarded path for exactly those.

    python3 scripts/edit_note_frontmatter.py --path <note.md> \
        --set title="Corrected title" [--set provenance.ref="imessage:...:c12"] \
        --reason "2026-07-20 audit: title names the wrong study" [--root <path>]

RE-DOMAINING (added 2026-08-04) — `--set domain=<other>` MOVES the file too:

    python3 scripts/edit_note_frontmatter.py --path unsorted/2025-07-31-foo-im4821.md \
        --set domain=research [--set tags="[research, correspondence]"] \
        --reason "2026-08-04 triage: this is a lab-collaboration thread, not unsorted"

`domain:` is frozen, AND lint_store requires `domain:` to equal the note's top-level directory — so
for a distilled-source note (gmail/granola/outlook/imessage/whatsapp) the field change and the file
move are ONE operation, not two: setting the field alone fails lint, and moving the file alone
(`clarify_writeback.move_note(..., new_domain=X)`) is hard-refused by the frozen-field move gate
(review finding H4, 2026-08-04). This is the ONLY sanctioned way to re-domain such a note. The move
itself is delegated to `clarify_writeback.plan_move_note`/`move_note` — the single owner of "move a
note through a validate -> write -> gate -> lint -> rollback transaction" — with this writer
supplying the destination content and the ledger entries it minted for the frozen fields it changed.
The note SLUG is unchanged by construction (destination basename must match), so inbound
`[[wikilinks]]` are untouched; renaming a note is `move_note`'s own job, not this one's.

HUMAN-DRIVEN ONLY. No distiller, sync/redistill pass, or other automated path may call this module
or its CLI — every invocation records a standing excuse for a frozen-field change and must be a
deliberate, reviewed human act (CLAUDE.md). It is one of exactly TWO sanctioned ledger writers
(with clarify_writeback.py); do not add a third without an explicit decision from the store's owner.

Shape mirrors the guarded-writer precedent (reconcile_dossier.py / engram_capture.py): assemble the
new note IN MEMORY -> gate (refuse, before any write, if it would change ANYTHING other than the
named fields + `updated:`) -> record each change in the sanctioned-override ledger -> write ->
lint_store.py -> ROLL BACK note AND ledger on lint failure.

Why the override ledger (`_manifest/frontmatter-overrides.jsonl`): the change would otherwise trip
the redistill gate on the very next check.sh, which cannot tell a sanctioned edit from an accidental
one. Each ledger entry `{path, field, kind, old, new, head_sha, date, reason}` names the exact
sanctioned transition (old = HEAD value, new = the value written); the gate excuses a frozen-field
violation iff a matching entry exists AND that entry is still UNCOMMITTED. Commit the note and its
ledger line together: after that commit the note matches HEAD (nothing to excuse) and the entry is
inert forever — a permanent audit record of who changed a frozen field, to what, and why. Committing
the ledger without the note leaves the gate red until the note follows.

Guardrails:
- `verified:` is NEVER settable here — it is a human-only epistemic signal (CLAUDE.md), off every
  automated write path by construction.
- The target note must be CLEAN at HEAD (no uncommitted changes). A dirty note is refused: the
  ledger's `old` must be HEAD's value for the gate match to be exact, and mixing a hand-edit with
  this change hides what actually happened. Commit or stash first.
- Does NOT commit (reconcile_dossier precedent): review the working-tree change, run check.sh, commit
  yourself. check.sh's gate re-verifies the same fields against the committed diff + the ledger.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.constants import DOMAINS                              # noqa: E402  (single owner)
from _lib.notes import split_frontmatter                        # noqa: E402
from _lib.frontmatter_diff import (PROFILES, _get, OVERRIDES_LEDGER, head_blob_sha,  # noqa: E402
                                   git_show_head as _git_show_head, override_entry,
                                   serialize_overrides, UNEXCUSABLE_FIELDS,
                                   diff_frontmatter)

# The frozen identity fields a redistill-source note carries — the universe of what this writer may
# touch, MINUS `verified` (human-only). Both halves come from single owners so neither can drift: the
# redistill profile, and UNEXCUSABLE_FIELDS (the same set the gate hard-denies any ledger excuse for
# — this refusal and that hard-deny are two ends of one rule). `updated:` is always allowed to move
# (it is `mutable` there); `body` is never touched here.
_SETTABLE = (PROFILES["redistill"]["preserve"] - UNEXCUSABLE_FIELDS)


def _die(msg, code=2):
    print(f"edit_note_frontmatter: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _set_scalar(head, key, value):
    """Replace a top-level `key: <...>` line (quote the value if it was quoted or needs it)."""
    pat = re.compile(rf'^{re.escape(key)}:.*$', re.M)
    if not pat.search(head):
        _die(f"field {key!r} not present in frontmatter — refusing to invent it")
    return pat.sub(lambda _m: f"{key}: {value}", head, count=1)


def _set_nested(head, parent, child, value):
    """Replace an indented `child: <...>` line inside the `parent:` block (e.g. provenance.ref)."""
    lines = head.split("\n")
    in_block = False
    for i, ln in enumerate(lines):
        if re.match(rf'^{re.escape(parent)}:\s*$', ln):
            in_block = True
            continue
        if in_block:
            if ln[:1] and not ln[:1].isspace():          # dedent to a new top-level key => block ended
                break
            m = re.match(rf'^(\s+){re.escape(child)}:\s*(.*)$', ln)
            if m:
                lines[i] = f"{m.group(1)}{child}: {value}"
                return "\n".join(lines)
    _die(f"nested field {parent}.{child!r} not found in frontmatter — refusing to invent it")


def _quote_like(old_raw, new_val):
    """Mirror the existing quoting of a scalar: if the old line quoted the value, quote the new one."""
    return f'"{new_val}"' if old_raw.strip().startswith('"') else new_val


def _apply(head, field, new_val):
    """Set `field` (top-level or dotted provenance.child) to new_val, preserving quote style."""
    if "." in field:
        parent, child = field.split(".", 1)
        old_raw = re.search(rf'^\s+{re.escape(child)}:\s*(.*)$', head, re.M)
        val = _quote_like(old_raw.group(1) if old_raw else "", new_val)
        return _set_nested(head, parent, child, val)
    old_raw = re.search(rf'^{re.escape(field)}:\s*(.*)$', head, re.M)
    val = _quote_like(old_raw.group(1) if old_raw else "", new_val)
    return _set_scalar(head, field, val)


def run_lint(root):
    r = subprocess.run([sys.executable, os.path.join(_SCRIPTS, "lint_store.py")],
                       cwd=root, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def _parse_sets(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            _die(f"--set expects field=value, got {p!r}")
        field, value = p.split("=", 1)
        field = field.strip()
        if field not in _SETTABLE:
            _die(f"field {field!r} is not settable. Allowed: {', '.join(sorted(_SETTABLE))} "
                 f"(note: `verified` is human-only and never settable here)")
        if "\n" in value or "\r" in value:
            _die(f"--set value for {field!r} contains a newline — refusing (a frontmatter field is "
                 f"one line; a newline would splice in a rogue line the surgical gate can't see)")
        out[field] = value
    return out


def _move_destination(root, relpath, new_domain, to=None):
    """The path a `--set domain=<new_domain>` re-stamp must move `relpath` to, or None when the note
    is already in the right directory (a pure field fix, no move).

    Refuses anything that is not a plain domain-to-domain relocation of the SAME note: a slug change
    is a RENAME, which re-points every inbound `[[wikilink]]` and is `clarify_writeback.move_note`'s
    job, not this writer's. Keeping the basename is what makes "a re-domain never touches inbound
    links" a structural property rather than a happy accident."""
    if new_domain not in DOMAINS:
        _die(f"domain {new_domain!r} is not a store domain. Known: {', '.join(DOMAINS)}")
    top = relpath.split("/", 1)[0]
    if top not in DOMAINS:
        _die(f"{relpath} is not a domain note (top-level dir {top!r}) — this writer only re-domains "
             f"notes that live in a domain directory")
    base = os.path.basename(relpath)
    dst = to or os.path.join(new_domain, base)
    dst = dst.replace(os.sep, "/")
    if to:
        if dst.split("/", 1)[0] != new_domain:
            _die(f"--to {to!r} is not inside the new domain directory {new_domain}/")
        if os.path.basename(dst) != base:
            _die(f"--to {to!r} changes the note SLUG ({base} -> {os.path.basename(dst)}). This writer "
                 f"re-domains a note; renaming one is clarify_writeback.move_note's job (it rewrites "
                 f"every inbound [[wikilink]] in the same step)")
    if dst == relpath:
        return None                                  # already in the right dir — field fix only
    if os.path.exists(os.path.join(root, dst)):
        _die(f"destination already exists: {dst}")
    return dst


def _print_entries(entries, note_line):
    print("edit_note_frontmatter: OK")
    print(note_line)
    for e in entries:
        print(f"  {e['field']}: {e['old']!r} -> {e['new']!r}")
    print(f"  ledger: +{len(entries)} sanctioned-override entr{'y' if len(entries) == 1 else 'ies'} "
          f"in {OVERRIDES_LEDGER}")
    print("  commit: not committed — run check.sh (the gate will now PASS via the ledger) and commit")


def _do_move(root, src_rel, dst_rel, new_text, entries, changed):
    """Execute a re-domain: move the note AND record its sanctioned-override entries in ONE guarded
    transaction, delegating to clarify_writeback (the single owner of the note-move transaction).

    Why this cannot be done here as "set the field, then move the file": `move_note` writes the
    ledger BEFORE running the in-transaction frozen-field gate, and rolls BOTH back together, so a
    refused move leaves neither a half-moved note nor an orphan ledger entry. Re-implementing that
    ordering locally would be a second owner of the same transaction — the exact drift CLAUDE.md's
    one-owner rule exists to prevent. `content=` hands over the frontmatter this writer already
    assembled and gated; `overrides=` hands over the entries it minted from the gate's own diff."""
    import clarify_writeback as cw                      # deferred: the in-place path never needs it
    try:
        res = cw.move_note(root, src_rel, dst_rel, verify=True, content=new_text, overrides=entries)
    except cw.WritebackError as e:
        _die(f"{e}", 6)
    _print_entries(entries, f"  moved:  {src_rel} -> {dst_rel}")
    for msg in res.get("messages", []):
        print(f"  {msg}")
    print(f"  links:  unchanged — the note slug is identical, so no inbound [[wikilink]] moved")
    if "domain" in changed:
        print(f"  note:   `domain:` and the file's directory are now consistent again "
              f"(lint_store requires domain == top-level dir)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Guarded writer for a deliberate frozen-frontmatter "
                                             "change; records it in the sanctioned-override ledger. "
                                             "`--set domain=X` MOVES the note into X/ as part of the "
                                             "same guarded transaction.")
    ap.add_argument("--path", required=True, help="note to edit, e.g. research/....md")
    ap.add_argument("--set", dest="sets", action="append", required=True, metavar="FIELD=VALUE",
                    help="a frozen field to change (repeatable), e.g. --set title=\"...\"")
    ap.add_argument("--reason", required=True, help="why this frozen field is being changed (logged)")
    ap.add_argument("--to", help="explicit destination path for a `--set domain=` move (default "
                                 "<domain>/<same basename>); must keep the note's slug")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS))
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    today = datetime.date.today().isoformat()
    sets = _parse_sets(a.sets)

    relpath = os.path.relpath(a.path if os.path.isabs(a.path) else os.path.join(root, a.path), root)
    abspath = os.path.join(root, relpath)
    if not os.path.isfile(abspath):
        _die(f"--path not found: {abspath}")

    # Precondition: note must be CLEAN at HEAD, so the ledger's `old` is exactly HEAD's value.
    head_txt = _git_show_head(root, relpath)
    if head_txt is None:
        _die(f"{relpath} is not tracked at HEAD — commit it before a guarded frozen-field edit")
    cur_txt = open(abspath, encoding="utf-8").read()
    if cur_txt != head_txt:
        _die(f"{relpath} has uncommitted changes — commit or stash them first (the override ledger's "
             f"`old` must be HEAD's value for the gate match to be exact)")

    old_head, body = split_frontmatter(cur_txt)
    if old_head is None:
        _die(f"{relpath} has no parseable frontmatter")

    # The note is clean (checked above), so its HEAD blob IS the pre-edit content. Bind every ledger
    # entry to this sha; the gate excuses the change only while HEAD:<path> still equals it (i.e.
    # before commit), then it goes inert. Same value for all fields of this one note.
    head_sha = head_blob_sha(root, relpath)

    # Build the new head by applying ONLY the named fields, then stamp updated:.
    new_head = old_head
    changed = {}                                     # field -> old value (skips no-ops)
    for field, new_val in sets.items():
        old_val = _get(old_head, field)
        if old_val == new_val:
            print(f"edit_note_frontmatter: WARN {field} already {new_val!r} — no-op", file=sys.stderr)
            continue
        new_head = _apply(new_head, field, new_val)
        changed[field] = old_val
    if not changed:
        _die("nothing to change (all --set values already current)")
    if re.search(r'^updated:.*$', new_head, re.M):
        new_head = _set_scalar(new_head, "updated", today)

    # Where the note ENDS UP. `domain:` is frozen AND lint requires it to equal the top-level dir, so
    # a domain change is inseparable from the file move — see the module docstring.
    if a.to and "domain" not in changed:
        _die("--to is only meaningful together with a `--set domain=<domain>` that actually changes "
             "the domain")
    dst_rel = _move_destination(root, relpath, sets["domain"], a.to) if "domain" in changed else None
    ledger_path = dst_rel or relpath          # the path the GATE will key its override lookup on

    # The ledger entries are derived from `diff_frontmatter` — the GATE'S OWN comparator, run under
    # the same profile check.sh applies — rather than from the --set strings, so `field`/`old`/`new`/
    # `kind` are byte-identical to the violation records the gate will build. A hand-derived tuple
    # that is even slightly off (a quoted value, a list compared as text rather than as a set)
    # excuses nothing and leaves check.sh red. This is the same construction `clarify_writeback`'s
    # `plan_participant_overrides` / `plan_move_overrides` use.
    #
    # `head_sha` binds every entry to the note's PRE-edit HEAD blob (the note is clean, checked
    # above). For a MOVE that is `HEAD:<src>` — the destination has no HEAD blob at all, and the
    # gate resolves the same source blob through its own rename pairing.
    prof = PROFILES["redistill"]
    viols = diff_frontmatter(old_head, new_head, prof["preserve"], prof["mutable"])

    # GATE (before any write): every FROZEN field except the ones we set must be unchanged. We assert
    # a SURGICAL change — nothing leaked. `updated:` is NOT checked here: it is mutable in the redistill
    # profile and we deliberately stamp it (a prior fix wrongly froze it, refusing any note whose
    # `updated:` predated today). The body is checked separately below.
    leaked = [v for v in viols if v["field"] not in sets]
    if leaked:
        _die("internal gate: " + "; ".join(
            f"field {v['field']!r} would change but was not in --set "
            f"({v['old']!r} -> {v['new']!r})" for v in leaked) + " — refusing")
    entries = [override_entry(ledger_path, v["field"], v["old"], v["new"], head_sha, today,
                              a.reason, kind=v["kind"]) for v in viols]
    if not entries:
        _die("the named field(s) changed no FROZEN value the gate would flag (e.g. a tags reorder, "
             "which the gate compares as a set) — there is nothing to sanction, so nothing is written")

    _, new_body = split_frontmatter(new_head + body)
    if new_body != body:
        _die("internal gate: body would change — refusing (this writer touches frontmatter only)")

    new_text = new_head + body
    if not new_text.endswith("\n"):
        new_text += "\n"

    if dst_rel:
        return _do_move(root, relpath, dst_rel, new_text, entries, changed)

    # Append the override entries, remembering the ledger's prior state to roll back precisely —
    # its size if it existed, or its ABSENCE (so a rollback restores "no file", not a 0-byte file).
    ledger = os.path.join(root, OVERRIDES_LEDGER)
    ledger_existed = os.path.isfile(ledger)
    prev_size = os.path.getsize(ledger) if ledger_existed else 0
    # `_manifest/` always exists in the real store, but a store that has never recorded an override
    # has no ledger DIR in a fresh checkout — creating it here (and removing it again on rollback)
    # keeps "the first sanctioned edit" from dying with a FileNotFoundError traceback.
    made_dir = not os.path.isdir(os.path.dirname(ledger))
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(serialize_overrides(entries))

    # Write the note, verify via lint_store; ROLL BACK note AND ledger on failure.
    open(abspath, "w", encoding="utf-8").write(new_text)
    ok, lint_out = run_lint(root)
    if not ok:
        open(abspath, "w", encoding="utf-8").write(cur_txt)
        if ledger_existed:
            with open(ledger, "r+", encoding="utf-8") as fh:
                fh.truncate(prev_size)
        else:
            os.remove(ledger)
            if made_dir:
                os.rmdir(os.path.dirname(ledger))
        _die(f"lint_store rejected the edit (rolled back note + ledger):\n{lint_out}", 6)

    _print_entries(entries, f"  note:   {relpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
