#!/usr/bin/env python3
"""Content-integrity gate for the in-place note-rewrite flows (Feature B, plan
generic-bubbling-eagle.md). For each note path, diff the COMMITTED version (`git show HEAD:<path>`)
against the WORKING-TREE version using a named preserve/mutable profile and report any frozen-field
violation. Exits 1 if ANY path violates, 0 if all clean (or no paths given).

Reuses _lib.notes.split_frontmatter + _lib.frontmatter_diff (the single owner of the diff logic).

    python3 scripts/verify_frontmatter_preserved.py [--profile redistill|dossier] \
        [--root <path>] [--changed] <path> [<path> ...]
    python3 scripts/verify_frontmatter_preserved.py --classify <path> [<path> ...]
    python3 scripts/verify_frontmatter_preserved.py --modified notes|dossiers
    python3 scripts/verify_frontmatter_preserved.py --audit-ledger
    python3 scripts/verify_frontmatter_preserved.py --moved

  --profile       redistill (default; gmail_update.mjs / granola_redistill.mjs) or dossier.
  --changed       also auto-derive modified *.md under domain dirs via `git diff --name-only HEAD`.
  --moved         gate every MOVED/RENAMED note against its ORIGINAL at HEAD. A move has no HEAD
                  blob at its new path, so the path-keyed check below saw nothing and check.sh's
                  `^(.M|M.)` grep did not either — the same re-stamp that is hard-refused in place
                  walked straight through (H4).
  --modified      the whole pre-modified-file gate for check.sh, end to end and NUL-safe: enumerate
                  what is already dirty, route each note through `classify`, run the field gate, and
                  report. `notes` = domain notes (redistill gate + the SUSPICIOUS list);
                  `dossiers` = modified dossiers under the `dossier` profile. Exit code is a
                  BITMASK (see RC_FIELD / RC_SUSPICIOUS) so check.sh can pick its message without
                  ever handling a path.
  --classify      print `redistill <path>` / `other <path>` per path — which gate check.sh applies,
                  decided from the note's HEAD `source:`. Single owner of that routing.
  --audit-ledger  print the sanctioned-override ledger's uncommitted lines as a REVIEW block and
                  FAIL if a committed line was modified/removed (the ledger is append-only).

check.sh drives the gate through --modified / --moved and reacts to the exit code; it passes no
path list at all, because it cannot do so safely (see `modified_paths`). --changed and an explicit
path list are for standalone use and for the tests.
"""
import argparse
import os
import subprocess
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.notes import split_frontmatter, get_field              # noqa: E402
from _lib.constants import DOMAINS                               # noqa: E402
from _lib.frontmatter_diff import (diff_frontmatter, PROFILES,                    # noqa: E402
                                   load_overrides as _load_overrides, head_blob_sha,
                                   git_show_head as _git_show_head, override_key,
                                   ledger_audit, OVERRIDES_LEDGER, REDISTILL_SOURCES,
                                   UNEXCUSABLE_FIELDS, _get as _fm_get)

# --modified exit-code BITS. Deliberately not 1/2: a Python traceback exits 1 and an argparse usage
# error exits 2, so bits there would let a CRASHED gate masquerade as a clean finding-report and get
# check.sh to print a specific, wrong explanation. With 4/8 any other non-zero code means "the gate
# itself failed", which check.sh reports as such — fail-closed either way, but honestly labelled.
RC_FIELD = 4          # a FROZEN-field violation was reported (redistill for notes, dossier profile)
RC_SUSPICIOUS = 8     # pre-modified notes that are NOT from a distilled source (the blunt halt)


def _changed_paths(root):
    r = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "*.md"],
                       cwd=root, capture_output=True, text=True)
    out = []
    for p in (r.stdout.splitlines() if r.returncode == 0 else []):
        p = p.strip()
        if p and p.split("/", 1)[0] in DOMAINS:
            out.append(p)
    return out


def _status_entries(root, pathspec="*.md"):
    """`git status --porcelain -z -- <pathspec>` parsed into (xy, path, orig) triples.

    `-z` ON PURPOSE: the default output QUOTES any path carrying a non-ASCII character
    (core.quotePath) and renders a rename as `old -> new`, and store paths are person names with
    spaces and accents — both would mangle here. With -z a rename emits `XY <new>\\0<old>\\0`, so
    `orig` is read from the following token."""
    r = subprocess.run(["git", "status", "--porcelain", "-z", "--", pathspec],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    toks = r.stdout.split("\0")
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        xy, path = t[:2], t[3:]
        orig = None
        if "R" in xy or "C" in xy:                 # git already resolved the pair (staged rename)
            orig = toks[i] if i < len(toks) else None
            i += 1
        out.append((xy, path, orig))
    return out


# Path fragments that are NOT domain notes, and so are outside the pre-modified-note gate. Mirrors,
# fragment for fragment, the `grep -vE` check.sh carried in shell until 2026-08-04.
_NON_NOTE_FRAGMENTS = ("_manifest/", "digests/", "dossiers/", "profile/", "literature/", "_index.md")


def _is_domain_note(relpath):
    """True when `relpath` is a note the pre-modified gate covers — i.e. a file inside a top-level
    DOMAIN dir, not a derived/synthesized artifact, not infrastructure, not a root-level doc.

    Equivalent to check.sh's old filter, with its two positional rules fixed:
      * `_manifest/|digests/|_index\\.md|dossiers/|profile/|literature/` — derived or synthesized,
        gated elsewhere (dossiers get their own profile) or not at all;
      * `^.{3}[_.]`   — an underscored/dotted top-level dir (`_archive/`, `_sources/`, `.claude/`)
                        is infrastructure, never a note;
      * `^.{3}[^/]+$` — a root-level `*.md` (CLAUDE.md, INDEX.md, glossary.md) is a hand-maintained
                        doc; notes always live in a domain subdir.
    The last two counted characters into the porcelain LINE, so on a QUOTED path (`_archive/José.md`
    arrives as ` M "_archive/Jos\\303\\251.md"`) the character they tested was the opening `"` and
    neither rule fired. Here they read the real path, so an accented `_archive/` note is excluded for
    the reason it was always meant to be."""
    if any(frag in relpath for frag in _NON_NOTE_FRAGMENTS):
        return False
    if "/" not in relpath:
        return False
    return relpath[0] not in "_."


def modified_paths(root, pathspec="*.md", domain_notes_only=True):
    """Every tracked path with a pre-existing MODIFICATION (porcelain `XY` containing an `M`), read
    NUL-safely and returned as REAL paths. SINGLE OWNER of "what was already dirty before this run".

    check.sh used to compute this in shell and hand the result to `--classify` unquoted:

        pre_mod_notes=$(git status --porcelain -- '*.md' | grep -E '^(.M|M.)' | grep -vE ...)
        pre_mod_paths=$(printf '%s\\n' "$pre_mod_notes" | sed -E 's/^...//')
        classified=$(... --classify $pre_mod_paths)      # <- unquoted

    Both halves were wrong on the store's own filenames, and compounded. Plain `git status
    --porcelain` QUOTES any path containing a space or a non-ASCII byte (core.quotePath), and an
    unquoted `$var` word-splits, so ` M "research/José Ejémplo Muestra.md"` reached the gate as
    THREE arguments that name no file — `"research/Jos\\303\\251`, `Ej\\303\\251mplo`,
    `Muestra.md"`. Hundreds of tracked notes carry a space and the people registry's canonical
    spellings carry accents (`José Ejémplo Muestra`, `Solène Exemple`, `Anaïs
    Sample`), and `move_note` renames person notes TO those spellings — so this was the normal
    case, not an edge one. Every such note classified `other` (fail-closed: an unresolvable path is
    not a distilled source) and fell to the blunt SUSPICIOUS halt, so the per-field redistill gate
    never saw exactly the notes it exists for, and the operator was shown fragments of a filename
    instead of a note. It degraded the gate's precision rather than opening it — but only because
    the SUSPICIOUS branch is itself fatal.

    `_status_entries` reads `git status --porcelain -z`, which emits raw, unquoted bytes; it is the
    single owner of porcelain parsing here (`moved_pairs` already depended on it for the same
    reason). `M` in `xy` == check.sh's `^(.M|M.)`: modified in the index or in the worktree,
    excluding untracked (`??`) and plain adds (`A `)."""
    return sorted(path for xy, path, _orig in _status_entries(root, pathspec)
                  if "M" in xy and (not domain_notes_only or _is_domain_note(path)))


def _print_violations(out, header, viols):
    """The one renderer for a frozen-field violation block, shared by every mode."""
    print(f"FRONTMATTER-PRESERVE VIOLATION {header}:", file=out)
    for v in viols:
        if v["kind"] == "removed":
            print(f"    {v['field']}: members REMOVED {v['removed']} "
                  f"(old={v['old']!r} new={v['new']!r}) — append-only, may not drop members",
                  file=out)
        else:
            print(f"    {v['field']}: CHANGED old={v['old']!r} -> new={v['new']!r}", file=out)


def check_modified(root, overrides, target="notes", out=sys.stdout):
    """check.sh's pre-modified-file gate, end to end. Returns an RC_* bitmask (0 = clean).

    `notes`     — every pre-modified domain note, routed by `classify`: a distilled-connector note
                  gets the `redistill` field gate (RC_FIELD on a violation), everything else is
                  listed for check.sh's blunt SUSPICIOUS halt (RC_SUSPICIOUS). Identical routing to
                  the shell version; what changes is that the paths survive the trip.
    `dossiers`  — every modified dossier under the `dossier` profile (identity frozen, cross-links
                  mutable). No classification and no SUSPICIOUS branch: a dossier is deliberately
                  reconciled, and this gate is its only content-integrity check.

    Enumeration and checking happen in ONE call ON PURPOSE. check.sh must enumerate before it
    regenerates (gen_backlinks may add `related:` to a note, and `related` is FROZEN, so a
    generator's own edit would read as a redistill violation), and previously it enumerated pre-regen
    but ran the field gate post-regen — against a working tree the generators had since touched. That
    split could both invent a violation (a generator-added `related:` blamed on a redistill) and hide
    one (a redistill that BLANKED `related:` looks clean again once gen_backlinks re-adds the same
    links). Checking the same snapshot it enumerated removes both. Nothing is lost in the other
    direction: a generator-modified note is caught by check.sh's own STALE check, which fails the run
    regardless."""
    rc = 0
    if target == "dossiers":
        for p in modified_paths(root, "dossiers/*.md", domain_notes_only=False):
            viols = check_path(root, p, "dossier", overrides)
            if viols:
                rc |= RC_FIELD
                _print_violations(out, f"[dossier] {p}", viols)
        return rc

    redistill, other = [], []
    for p in modified_paths(root):
        (redistill if classify(root, p) == "redistill" else other).append(p)
    for p in redistill:
        viols = check_path(root, p, "redistill", overrides)
        if viols:
            rc |= RC_FIELD
            _print_violations(out, f"[redistill] {p}", viols)
    if other:
        rc |= RC_SUSPICIOUS
        print(f"SUSPICIOUS — {len(other)} pre-modified note "
              f"{'body is' if len(other) == 1 else 'bodies are'} not from a distilled connector "
              f"source:", file=out)
        for p in other:
            print(f"    {p}", file=out)
    return rc


def _identity_keys(root, relpath, text):
    """(slug, provenance.ref, body) for a note text — the keys `moved_pairs` joins on."""
    head, body = split_frontmatter(text) if text is not None else (None, None)
    return (os.path.splitext(os.path.basename(relpath))[0],
            _fm_get(head, "provenance.ref") if head is not None else "",
            body)


def moved_pairs(root):
    """[(src, dst)] for every note MOVED or RENAMED in the working tree relative to HEAD.

    This is the case `check_path`'s `git show HEAD:<path>` cannot see: at the NEW path there is no
    HEAD blob, so the frozen-field gate reported CLEAN for exactly the domain/tags/title re-stamp it
    hard-refuses when the same note is edited IN PLACE (H4, 2026-08-04). check.sh could not see it
    either — its `pre_mod_notes` grep matches `^(.M|M.)` only.

    A move reaches us in one of two shapes. STAGED, git has already paired it (`R  <new>\\0<old>`)
    and we take its answer. UNSTAGED — the shape everything here actually produces, since no writer
    stages anything — it is ` D <old>` plus `?? <new>`, which git will NOT pair for us: rename
    detection never considers untracked files, so `git diff --find-renames --name-status HEAD`
    reports the delete alone. Those are paired here, most specific key first, each source consumed
    at most once:

      1. the note SLUG      — a domain move keeps the basename;
      2. `provenance.ref`   — frozen under every profile, so a rename keeps it;
      3. the note BODY      — a pure rename does not touch it.

    A deleted note that pairs with nothing is just a deletion, which no gate covers by design (and
    a wrong pair fails CLOSED: the gate diffs against the wrong baseline and reports MORE, never
    fewer, violations). Chat-DB refs are chunk-granular and shared between notes, so key 2 could in
    principle mis-pair two iMessage notes renamed in the same uncommitted batch — key 1 resolves
    every same-basename move before key 2 is consulted, and the failure mode is noise, not a pass."""
    pairs, deleted, added = [], [], []
    for xy, path, orig in _status_entries(root):
        if orig:
            pairs.append((orig, path))
        elif xy[0] == "D" or xy[1] == "D":
            deleted.append(path)
        elif xy == "??" or xy[0] == "A":
            added.append(path)
    if deleted and added:
        srcs = [(p, _identity_keys(root, p, _git_show_head(root, p))) for p in deleted]
        dsts = []
        for p in added:
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    dsts.append((p, _identity_keys(root, p, fh.read())))
            except OSError:
                continue
        taken, matched = set(), set()
        for level in (0, 1, 2):                    # slug, then provenance.ref, then body
            for dst, dk in dsts:
                if dst in matched or not dk[level]:
                    continue
                hit = next((s for s, sk in srcs if s not in taken and sk[level] == dk[level]), None)
                if hit:
                    taken.add(hit)
                    matched.add(dst)
                    pairs.append((hit, dst))
    return [(s, d) for s, d in pairs if s != d]


def rename_source(root, relpath):
    """The ORIGINAL path of `relpath` when it is the destination of a working-tree move, else None."""
    return next((s for s, d in moved_pairs(root) if d == relpath), None)


def check_path(root, relpath, profile, overrides=frozenset(), baseline=None):
    """Violations for one path ([] = clean). Structural problems (HEAD version missing => new note,
    frontmatter lost/malformed) are handled explicitly: a new note has nothing to preserve against
    (skip); a note that LOST its frontmatter is itself a violation.

    `baseline` is the path whose HEAD blob is the comparison — the note's own path for an in-place
    rewrite, and the SOURCE path for a MOVED note (whose destination has no HEAD blob at all).
    Callers that already know the move pair (clarify_writeback's in-transaction gate) pass it;
    otherwise a missing `HEAD:<relpath>` falls back to `rename_source`, so a move can no longer walk
    past the gate the identical in-place edit is refused by.

    `overrides` is a set of `override_key` tuples from the sanctioned-override ledger — as returned
    by `load_overrides`, which yields ONLY entries not yet committed at HEAD. A frozen-field
    violation is excused only when its exact (path, field, kind, old, new) transition is recorded
    AND the entry's `head_sha` still equals the current `HEAD:<path>` blob. Both conditions are
    narrow on purpose: the ledger line must be new (a committed line is inert forever, so a later
    revert of the note cannot re-arm it) and it must name the note's current HEAD blob. This is the
    only way a title/provenance.ref fix or a people-dedup participants rename passes the gate.

    `verified` is HARD-DENIED: no ledger entry may excuse it, ever. It is the human-only epistemic
    signal — no sanctioned writer sets it (edit_note_frontmatter.py refuses it outright), so a
    ledger line naming it is by construction forged or mistaken. `head_sha` is an expiry check, not
    an authenticity one (`git rev-parse HEAD:<path>` is a public one-liner anyone can paste into a
    hand-appended line), so it must not be the only thing standing between a machine and a
    `verified:` stamp."""
    prof = PROFILES[profile]
    src = baseline or relpath
    old_txt = _git_show_head(root, src)
    if old_txt is None and baseline is None:
        src = rename_source(root, relpath) or relpath        # a MOVED note: diff vs its ORIGINAL
        old_txt = _git_show_head(root, src)
    if old_txt is None:
        return []                                   # untracked at HEAD (new note) — nothing to guard
    abspath = os.path.join(root, relpath)
    try:
        new_txt = open(abspath, encoding="utf-8", errors="replace").read()
    except OSError:
        return [{"field": "<file>", "old": "present at HEAD", "new": "unreadable",
                 "kind": "changed"}]
    old_head, _ = split_frontmatter(old_txt)
    new_head, _ = split_frontmatter(new_txt)
    if new_head is None:
        return [{"field": "<frontmatter>", "old": "valid", "new": "MISSING/malformed",
                 "kind": "changed"}]
    if old_head is None:
        return []                                   # old had no parseable frontmatter — nothing to diff
    viols = diff_frontmatter(old_head, new_head, prof["preserve"], prof["mutable"])
    if overrides and viols:
        # The blob the entry must name is the BASELINE's — the note's own for an in-place rewrite,
        # the move SOURCE's for a moved note (its destination has no HEAD blob, so keying on
        # `relpath` would make every move unexcusable and refuse even a ledger-recorded rename).
        cur_sha = head_blob_sha(root, src)          # one git call per flagged path
        viols = [v for v in viols
                 if v["field"] in UNEXCUSABLE_FIELDS       # never excusable, whatever the ledger says
                 or not (cur_sha and override_key(relpath, v["field"], v["old"], v["new"],
                                                  v["kind"], cur_sha) in overrides)]
    return viols


def classify(root, relpath):
    """Which content-integrity gate check.sh applies to a modified note: "redistill" when its HEAD
    `source:` is a distilled connector source (_lib.frontmatter_diff.REDISTILL_SOURCES), else
    "other" (check.sh's blunt SUSPICIOUS halt).

    SINGLE OWNER of that routing. check.sh used to carry its own `case gmail|granola|...` list AND
    its own `git show HEAD:<f> | grep -m1 '^source:'` — a whole-FILE grep, so a note with no
    `source:` in its frontmatter but a body line starting `source: gmail` classified as redistill and
    got the precise field gate instead of the halt (fail-open). Here the source is read from the
    frontmatter block only, via the same `get_field` clarify_writeback and the linter use.

    A MOVED note is classified by what it was at its ORIGINAL path (`rename_source`) — the note is
    the same note, and its HEAD `source:` lives at the path it came from.

    Classifies fail-CLOSED: anything not positively identified as a distilled source (untracked at
    HEAD and not a move destination, unparseable frontmatter, absent `source:`) is "other" — the
    stricter, stop-and-investigate branch."""
    head_txt = _git_show_head(root, relpath)
    if head_txt is None:
        src = rename_source(root, relpath)
        head_txt = _git_show_head(root, src) if src else None
    if head_txt is None:
        return "other"
    head, _ = split_frontmatter(head_txt)
    if head is None:
        return "other"
    return "redistill" if get_field(head, "source") in REDISTILL_SOURCES else "other"


def check_moves(root, overrides, out=sys.stdout):
    """Gate every MOVED/RENAMED note against its ORIGINAL at HEAD. Returns 0 (clean) or 1.

    Routing MIRRORS the in-place gate exactly, decided by `classify` on the note's source AT ITS
    ORIGINAL PATH: a distilled-connector note gets the `redistill` field gate; everything else gets
    nothing. That second half is deliberate — check.sh applies no FIELD gate to obsidian/gdrive/
    manual notes either, and its blunt SUSPICIOUS halt is for a pre-existing BODY modification, which
    a rename is not. `move_note` is the sanctioned tool for person-note renames (all of which are
    obsidian) and is used routinely; halting on those would be a regression, not a hardening."""
    bad = []
    for src, dst in sorted(moved_pairs(root)):
        # classify the SOURCE: it is the path that HAS a HEAD blob, so this is the same answer
        # `classify(root, dst)` gives through its rename fallback, without re-deriving the pair.
        if classify(root, src) != "redistill":
            continue
        viols = check_path(root, dst, "redistill", overrides, baseline=src)
        if viols:
            bad.append((src, dst, viols))
    for src, dst, viols in bad:
        _print_violations(out, f"[redistill/move] {src} -> {dst}", viols)
    return 1 if bad else 0


def audit_ledger(root, out=sys.stdout):
    """Print the sanctioned-override ledger's REVIEW block; return 0 (clean) or 1 (append-only
    violated). Every uncommitted line is a LIVE frozen-field excuse, so check.sh prints all of them
    unconditionally — nothing else in the gate ever showed them (`pre_mod_notes`/`notes_changed`
    both exclude `_manifest/`), which is why an in-place rewrite of a committed line was invisible."""
    ok, new_lines, problems = ledger_audit(root)
    if new_lines:
        n = len(new_lines)
        print(f"REVIEW {n} uncommitted sanctioned-override entr{'y' if n == 1 else 'ies'} in "
              f"{OVERRIDES_LEDGER} — each one EXCUSES a frozen-field change on the named note. Read "
              f"every line before committing; commit each WITH the note it excuses.", file=out)
        for ln in new_lines:
            print(f"  + {ln}", file=out)
    else:
        print(f"{OVERRIDES_LEDGER}: no uncommitted entries (a committed entry is inert — an entry "
              f"excuses a change only while it is uncommitted).", file=out)
    for p in problems:
        print(f"OVERRIDE-LEDGER ERROR: {p}", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify frozen frontmatter fields survived an "
                                             "in-place note rewrite.")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="redistill")
    ap.add_argument("--changed", action="store_true",
                    help="also auto-derive modified domain notes via `git diff --name-only HEAD`")
    ap.add_argument("--no-overrides", action="store_true",
                    help="ignore the sanctioned-override ledger (strict mode; for tests)")
    ap.add_argument("--classify", action="store_true",
                    help="print `redistill <path>` / `other <path>` per path instead of checking")
    ap.add_argument("--audit-ledger", action="store_true", dest="audit_ledger",
                    help="print the override ledger's uncommitted lines; fail if a committed line "
                         "was modified or removed (it is append-only)")
    ap.add_argument("--moved", action="store_true",
                    help="gate every MOVED/RENAMED note against its ORIGINAL at HEAD (the case a "
                         "path-keyed `git show HEAD:<path>` cannot see)")
    ap.add_argument("--modified", choices=("notes", "dossiers"),
                    help="enumerate the pre-modified files, route them, and run the field gate — "
                         "check.sh's whole in-place gate in one call, so no path list crosses a "
                         "shell boundary. Exit code is an RC_FIELD|RC_SUSPICIOUS bitmask.")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS))
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)

    if a.audit_ledger:
        return audit_ledger(root)

    if a.classify:
        for p in a.paths:
            rel = os.path.relpath(p if os.path.isabs(p) else os.path.join(root, p), root)
            print(f"{classify(root, rel)}\t{rel}")
        return 0

    # The sanctioned-override ledger excuses DELIBERATE frozen-field changes made via a guarded
    # writer. Loaded by default so check.sh's gate honors it; --no-overrides restores strict
    # behavior. Only entries not yet committed at HEAD are live (see load_overrides).
    overrides = frozenset() if a.no_overrides else _load_overrides(root)

    if a.moved:
        return check_moves(root, overrides)

    if a.modified:
        return check_modified(root, overrides, a.modified)

    paths = list(a.paths)
    if a.changed:
        paths += _changed_paths(root)
    rels = sorted({os.path.relpath(p if os.path.isabs(p) else os.path.join(root, p), root)
                   for p in paths})

    any_viol = False
    for rel in rels:
        viols = check_path(root, rel, a.profile, overrides)
        if not viols:
            continue
        any_viol = True
        _print_violations(sys.stdout, f"[{a.profile}] {rel}", viols)
    if any_viol:
        print("\nThese fields are FROZEN for in-place rewrites. A redistill (gmail/granola) may change\n"
              "ONLY the body + `updated:` and may only APPEND participants; a dossier reconcile may\n"
              "also rewrite related/supersedes cross-links — but NEVER a note's identity or provenance.\n"
              "A changed frozen field is the tell for a rewrite that dropped curated cross-links,\n"
              "re-anchored provenance, or re-titled/re-domained the note. Restore the frozen fields\n"
              "from HEAD before committing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
