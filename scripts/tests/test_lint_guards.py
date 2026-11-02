#!/usr/bin/env python3
"""Guard tests for the store linter (scripts/lint_store.py) and the build_index slug-collision
backstop (scripts/build_index.py) — the two nets that catch a dup-ingest.

Covered:
  * dup-ref lint ERRORs when one provenance ref appears in >1 note (the wrong-scope/re-distill
    artifact);
  * the iMessage EXEMPTION holds — a shared `imessage:` ref across notes is BY DESIGN, not an
    error (chunk-granular ref, multi-note-per-chunk);
  * fail-closed (finding 5): a note with a missing / unparseable provenance ref is an ERROR, not
    a silently-skipped note;
  * build_index emits a slug-collision WARN when two notes share a basename (they'd mask each
    other in the graph) — and none when basenames are unique.

Hermetic: each case builds a synthetic store in a temp dir and runs the REAL scripts against it
(lint via cwd; build_index via a driver that re-chdirs after the module's import-time chdir). The
real store and real _sources are never touched. Run: python3 -m pytest this_file.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
PY = sys.executable
LINT_STORE = os.path.join(_SCRIPTS, "lint_store.py")

NOTE = """---
title: "{title}"
date: 2026-07-01
tags: {tags}
domain: {domain}
source: {source}
provenance:
  method: distilled
{ref_block}status: done
---

{body}
"""


def write_note(store, relpath, ref, domain="personal", source="granola",
               tags="[personal]", body="Body."):
    """Write a store note. ref=None omits the provenance ref line entirely (finding-5 case)."""
    fp = os.path.join(store, relpath)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    ref_block = f'  ref: "{ref}"\n' if ref is not None else ""
    with open(fp, "w", encoding="utf-8") as f:
        f.write(NOTE.format(title=os.path.basename(relpath), tags=tags, domain=domain,
                            source=source, ref_block=ref_block, body=body))
    return fp


def run_lint(store):
    return subprocess.run([PY, LINT_STORE], cwd=store, capture_output=True, text=True)


def run_build_index(store):
    """Run the real build_index.main() against `store`. build_index chdirs to the real repo at
    import time, so re-chdir to the synthetic store afterward, then invoke main()."""
    driver = (
        "import os, sys\n"
        f"sys.path.insert(0, {json.dumps(_SCRIPTS)})\n"
        "import build_index\n"                       # import-time os.chdir(real repo)
        f"os.chdir({json.dumps(store)})\n"           # point at the synthetic store
        "build_index.main()\n"
    )
    return subprocess.run([PY, "-c", driver], capture_output=True, text=True)


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestDupRefLint(unittest.TestCase):
    def test_duplicate_ref_errors(self):
        with tempfile.TemporaryDirectory() as store:
            write_note(store, "personal/note-a.md", "granola:dup-ref-1")
            write_note(store, "personal/note-b.md", "granola:dup-ref-1")
            r = run_lint(store)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("duplicate provenance ref 'granola:dup-ref-1'", r.stdout, out(r))

    def test_imessage_ref_exemption_holds(self):
        """A shared imessage: chunk ref across two notes is NOT a dup (multi-note-per-chunk)."""
        with tempfile.TemporaryDirectory() as store:
            ref = "imessage:CHAT-GUID-123:12_c000"
            write_note(store, "personal/im-a.md", ref, source="imessage")
            write_note(store, "personal/im-b.md", ref, source="imessage")
            r = run_lint(store)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertNotIn("duplicate provenance ref", r.stdout, out(r))
            self.assertIn("0 errors", r.stdout, out(r))

    def test_missing_ref_fails_closed(self):
        """finding 5: a note whose provenance ref is absent/unparseable is an ERROR."""
        with tempfile.TemporaryDirectory() as store:
            write_note(store, "personal/no-ref.md", ref=None)
            r = run_lint(store)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("missing/unparseable provenance ref", r.stdout, out(r))

    def test_href_lookalike_fails_closed(self):
        """A note carrying `href:` (not `ref:`) has no real provenance ref -> ERROR, never a
        silent pass that escapes dup detection."""
        with tempfile.TemporaryDirectory() as store:
            fp = os.path.join(store, "personal", "href-note.md")
            os.makedirs(os.path.dirname(fp))
            with open(fp, "w", encoding="utf-8") as f:
                f.write('---\ntitle: "x"\ndate: 2026-07-01\ntags: [personal]\n'
                        'domain: personal\nsource: granola\nprovenance:\n'
                        '  method: distilled\n  href: "gmail:thread:AAA"\n'
                        'status: done\n---\n\nBody.\n')
            r = run_lint(store)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("missing/unparseable provenance ref", r.stdout, out(r))


class TestSlugCollisionWarn(unittest.TestCase):
    def test_slug_collision_warns(self):
        with tempfile.TemporaryDirectory() as store:
            os.makedirs(os.path.join(store, "_manifest"))
            write_note(store, "personal/one/meeting.md", "granola:aaa")
            write_note(store, "personal/two/meeting.md", "granola:bbb")
            write_note(store, "personal/unique.md", "granola:ccc")
            r = run_build_index(store)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("WARN slug collision 'meeting'", r.stdout, out(r))

    def test_no_collision_no_warn(self):
        with tempfile.TemporaryDirectory() as store:
            os.makedirs(os.path.join(store, "_manifest"))
            write_note(store, "personal/note-a.md", "granola:aaa")
            write_note(store, "personal/note-b.md", "granola:bbb")
            r = run_build_index(store)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertNotIn("WARN slug collision", r.stdout, out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
