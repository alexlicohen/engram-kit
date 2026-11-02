#!/usr/bin/env python3
"""Guarded, rollback-safe writer for dossier reconciliation (Feature B, plan
generic-bubbling-eagle.md) — the FIRST script to ever exist for dossier writes (today dossier
reconciliation is 100% ad-hoc Read+Edit by an agent). Shape mirrors the guarded-writer precedent
scripts/engram_capture.py: assemble the full new note IN MEMORY -> gate (refuse, before any write,
if a frozen identity field drifted) -> write -> lint_store.py -> ROLL BACK to the original content
on lint failure.

    python3 scripts/reconcile_dossier.py --path dossiers/<name>.md --new-body-file <scratch.md> \
        [--new-related "a,b,c"] [--new-supersedes "x,y"] [--root <path>]

--new-body-file is either the reconciled BODY only, or a fully-assembled draft (frontmatter+body):
if it begins with a `---` frontmatter block, that draft's frontmatter is validated against the
existing dossier (so a domain/title/type/provenance.ref tamper is caught and REFUSED before touching
disk); otherwise the frozen frontmatter is copied verbatim from the existing dossier and only the
body is replaced. --new-related/--new-supersedes override those (mutable) cross-link fields.
`generated:` is stamped to today when present.

Available tool, not a mandatory replacement for manual reconciliation (plan's call): it writes to
the working tree and does NOT commit — review the change and commit it yourself. When check.sh next
runs, the dossier preserve gate re-verifies the same frozen fields on the committed diff.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.notes import split_frontmatter                        # noqa: E402
from _lib.frontmatter_diff import diff_frontmatter, PROFILES     # noqa: E402


def _die(msg, code=2):
    print(f"reconcile_dossier: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _set_line(head, key, value):
    """Replace `key: <...>` in the frontmatter block, or append it if absent. `head` is the block
    up to (not including) the closing `---`, as split_frontmatter returns it."""
    pat = re.compile(rf'^{re.escape(key)}:.*$', re.M)
    line = f"{key}: {value}"
    if pat.search(head):
        return pat.sub(lambda _m: line, head, count=1)
    return head.rstrip("\n") + "\n" + line


def _fmt_list(items):
    return "[" + ", ".join(items) + "]"


def _parse_csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def run_lint(root):
    r = subprocess.run([sys.executable, os.path.join(_SCRIPTS, "lint_store.py")],
                       cwd=root, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def main(argv=None):
    ap = argparse.ArgumentParser(description="Guarded, rollback-safe dossier reconciliation writer.")
    ap.add_argument("--path", required=True, help="existing dossier, e.g. dossiers/example-project.md")
    ap.add_argument("--new-body-file", required=True,
                    help="reconciled body (or a full frontmatter+body draft)")
    ap.add_argument("--new-related")
    ap.add_argument("--new-supersedes")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS))
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    today = datetime.date.today().isoformat()

    # --- resolve + read the existing dossier ---
    dossier_path = a.path if os.path.isabs(a.path) else os.path.join(root, a.path)
    if not os.path.isfile(dossier_path):
        _die(f"--path not found: {dossier_path}")
    old_text = open(dossier_path, encoding="utf-8").read()
    old_head, _old_body = split_frontmatter(old_text)
    if old_head is None:
        _die(f"existing dossier has no parseable frontmatter: {dossier_path}")

    # --- read the reconciled content (body-only OR a full frontmatter+body draft) ---
    if not os.path.isfile(a.new_body_file):
        _die(f"--new-body-file not found: {a.new_body_file}")
    draft = open(a.new_body_file, encoding="utf-8").read()
    if not draft.strip():
        _die("--new-body-file is empty")
    draft_head, draft_body = split_frontmatter(draft)
    if draft_head is not None:                       # full-assembled draft mode
        new_head, new_body = draft_head, draft_body
    else:                                            # body-only mode: keep the old frozen frontmatter
        new_head = old_head
        new_body = "\n---\n\n" + draft.strip("\n") + "\n"

    # --- apply the allowed (mutable) overrides + stamp generated: ---
    if re.search(r'^generated:.*$', new_head, re.M):
        new_head = _set_line(new_head, "generated", today)
    if a.new_related is not None:
        new_head = _set_line(new_head, "related", _fmt_list(_parse_csv(a.new_related)))
    if a.new_supersedes is not None:
        new_head = _set_line(new_head, "supersedes", _fmt_list(_parse_csv(a.new_supersedes)))

    # --- GATE: refuse (before ANY write) if a FROZEN identity field drifted from the old dossier ---
    viols = diff_frontmatter(old_head, new_head, PROFILES["dossier"]["preserve"],
                             PROFILES["dossier"]["mutable"])
    if viols:
        print("reconcile_dossier: REFUSED — a frozen dossier identity field would change "
              "(nothing was written):", file=sys.stderr)
        for v in viols:
            print(f"    {v['field']}: {v['old']!r} -> {v['new']!r}", file=sys.stderr)
        sys.exit(3)

    new_text = new_head + new_body
    if not new_text.endswith("\n"):
        new_text += "\n"

    # --- write, then verify via lint_store; ROLL BACK to the ORIGINAL content on lint failure.
    # The file already EXISTS, so rollback = rewrite the saved original (NOT os.remove, which the
    # engram_capture precedent uses only because it CREATES a brand-new note). ---
    open(dossier_path, "w", encoding="utf-8").write(new_text)
    ok, lint_out = run_lint(root)
    if not ok:
        open(dossier_path, "w", encoding="utf-8").write(old_text)
        _die(f"lint_store rejected the reconciled dossier (rolled back to the original):\n{lint_out}", 6)

    print("reconcile_dossier: OK")
    print(f"  dossier: {os.path.relpath(dossier_path, root)}")
    print(f"  lint:    clean")
    print(f"  commit:  not committed — review the working-tree change and commit it yourself")
    return 0


if __name__ == "__main__":
    sys.exit(main())
