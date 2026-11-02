#!/usr/bin/env python3
"""Guard tests for scripts/check_distiller_prompts.py — the static (no-live-Workflow-launch) syntax
check for distiller prompt-builder template literals.

Hermetic by design: uses synthetic .mjs fixtures written to tmp_path, NEVER the real
`_*-pending/*.mjs` distillers (those are exercised manually via the script's own CLI, not by this
suite — the point of a hermetic test is that it doesn't break when Avery edits a real distiller's
prose). Two fixtures:

  * a GOOD file — well-formed `function prompt(...)` with balanced backticks and a few
    `${UPPER_CASE}` interpolations, mirroring the real distillers' shape.
  * a BROKEN file — an unbalanced backtick inside the prompt prose (the exact bug class commit
    2c204adb shipped), which terminates the template literal early and makes the file fail to parse.

Skips (not fails) if `node` isn't on PATH, matching the script's own SKIP-on-missing-node contract.
Run: python3 -m pytest scripts/tests/test_check_distiller_prompts.py -q
"""
import os
import shutil
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)

import check_distiller_prompts as C  # noqa: E402

GOOD_MJS = """\
export const meta = { name: 'fake-distill' }

const ROOT = '/fake/root'
const WAVE = '/fake/wave.jsonl'

function prompt(start, end, today) {
  return `You distill THINGS into notes. Conventions live at ${ROOT}/CLAUDE.md.
Work-list: run \\`sed -n '${start},${end}p' ${WAVE}\\` to get your slice.
Today is ${today}. Write notes with the Write tool, never bash/heredoc.
Return JSON: { threads: [] }`
}

// ---- run ----
const rendered = prompt(1, 2, '2026-01-01')
"""

# The unbalanced backtick class of bug: a stray ` inside the prose (not escaped) closes the
# template literal early, leaving a syntax error later in the function (an unterminated string /
# stray token), exactly like commit 2c204adb.
BROKEN_MJS = """\
export const meta = { name: 'fake-distill-broken' }

const ROOT = '/fake/root'
const WAVE = '/fake/wave.jsonl'

function prompt(start, end, today) {
  return `You distill THINGS into notes. Use the `Write` tool, never bash/heredoc.
Work-list: run sed -n '${start},${end}p' ${WAVE} to get your slice.
Today is ${today}.
Return JSON: { threads: [] }`
}

const rendered = prompt(1, 2, '2026-01-01')
"""


# Feature C shape: the prompt folds an optional const in via `${CONST ? `…` : ''}`. Under the
# empty-string stub the ternary ALWAYS takes the '' branch, so the inner (nested-backtick) literal
# never executes — which is why check_one renders twice, the second time with a truthy sentinel.
BRANCHED_MJS = """\
export const meta = { name: 'fake-distill-branched' }

const ROOT = '/fake/root'
const CORRECTIONS = ''

function prompt(start, end, today) {
  return `You distill THINGS into notes. Conventions live at ${ROOT}/CLAUDE.md.
${CORRECTIONS ? `\\nPast mistakes to avoid:\\n${CORRECTIONS}\\n` : ''}
Return JSON: { threads: [] }`
}

const rendered = prompt(1, 2, '2026-01-01')
"""

# Same fold, but the truthy branch breaks at RUNTIME (a string has no .map). It parses fine, and
# renders fine under the empty stub — only the sentinel pass reaches it. This is the blind spot the
# second stub mode exists to close.
BRANCH_BROKEN_MJS = BRANCHED_MJS.replace(
    "`\\nPast mistakes to avoid:", "`\\n${CORRECTIONS.map(x => x)}Past mistakes to avoid:")


def _write(tmp_path, name, content):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestExtractPromptFn(unittest.TestCase):
    def test_extracts_named_prompt_fn(self):
        fn_name, fn_src = C.extract_prompt_fn(GOOD_MJS)
        self.assertEqual(fn_name, "prompt")
        self.assertTrue(fn_src.startswith("function prompt("))
        self.assertTrue(fn_src.rstrip().endswith("}"))

    def test_extracts_distillprompt_variant(self):
        src = GOOD_MJS.replace("function prompt(", "function distillPrompt(").replace(
            "prompt(1, 2", "distillPrompt(1, 2")
        fn_name, fn_src = C.extract_prompt_fn(src)
        self.assertEqual(fn_name, "distillPrompt")

    def test_no_prompt_fn_returns_none(self):
        fn_name, fn_src = C.extract_prompt_fn("const x = 1;\n")
        self.assertIsNone(fn_name)
        self.assertIsNone(fn_src)


class TestFindDistillers(unittest.TestCase):
    def test_finds_only_files_with_prompt_fn(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "_fake-pending"))
            _write(d, "_fake-pending/has_prompt.mjs", GOOD_MJS)
            with open(os.path.join(d, "_fake-pending", "no_prompt.mjs"), "w") as f:
                f.write("const x = 1;\n")
            found = C.find_distillers("_*-pending/*.mjs", root=d)
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].endswith("has_prompt.mjs"))


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class TestCheckOneLive(unittest.TestCase):
    """Exercises the real node subprocess path — the part that actually catches the backtick bug."""

    def test_good_file_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "good.mjs", GOOD_MJS)
            ok, msg = C.check_one(path, shutil.which("node"))
            self.assertTrue(ok, msg)
            self.assertIn("rendered", msg)

    def test_broken_file_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "broken.mjs", BROKEN_MJS)
            ok, msg = C.check_one(path, shutil.which("node"))
            self.assertFalse(ok)

    def test_both_stub_modes_are_rendered_and_reported(self):
        """The sentinel pass must actually take the truthy branch — so it renders MORE than the
        empty pass on a file carrying a `${CONST ? `…` : ''}` fold."""
        import re
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "branched.mjs", BRANCHED_MJS)
            ok, msg = C.check_one(path, shutil.which("node"))
            self.assertTrue(ok, msg)
            self.assertIn("empty-stub", msg)
            self.assertIn("sentinel-stub", msg)
            empty, sentinel = (int(n) for n in re.findall(r"stub (\d+) chars", msg))
            self.assertGreater(sentinel, empty, msg)

    def test_break_only_in_the_truthy_branch_fails(self):
        """The blind spot the second pass closes: valid syntax, renders under the empty stub, and
        breaks only once the fold's inner template literal is actually executed."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "branch_broken.mjs", BRANCH_BROKEN_MJS)
            ok, msg = C.check_one(path, shutil.which("node"))
            self.assertFalse(ok, msg)
            self.assertIn("DISAGREES", msg)

    def test_main_exits_nonzero_on_broken_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "_fake-pending"))
            _write(d, "_fake-pending/broken.mjs", BROKEN_MJS)
            cwd = os.getcwd()
            old_root = C._ROOT
            try:
                C._ROOT = d
                rc = C.main(["--glob", "_fake-pending/*.mjs"])
            finally:
                C._ROOT = old_root
                os.chdir(cwd)
            self.assertEqual(rc, 1)

    def test_main_exits_zero_on_good_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "_fake-pending"))
            _write(d, "_fake-pending/good.mjs", GOOD_MJS)
            old_root = C._ROOT
            try:
                C._ROOT = d
                rc = C.main(["--glob", "_fake-pending/*.mjs"])
            finally:
                C._ROOT = old_root
            self.assertEqual(rc, 0)


class TestBacktickParity(unittest.TestCase):
    def test_even_backticks_ok(self):
        self.assertTrue(C.backtick_parity_ok("`a` `b`"))

    def test_odd_backticks_warns(self):
        self.assertFalse(C.backtick_parity_ok("`a` `b"))


if __name__ == "__main__":
    unittest.main()
