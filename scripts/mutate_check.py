#!/usr/bin/env python3
"""Mutation harness — prove the guards have teeth.

A green test suite is only meaningful if the tests actually FAIL when the thing they guard is
broken. For each (guard file, mutation, test) triple below this script:

  1. runs the guard's test on the pristine file  -> expects GREEN (sanity: the test passes now);
  2. reintroduces the exact bug the guard prevents (a one-token byte edit);
  3. runs the guard's test again                  -> expects RED (the test catches the bug);
  4. ALWAYS restores the file (try/finally, even on exception/interrupt) and verifies it is
     byte-identical to the original.

Exit 0 iff every triple was GREEN-pristine, RED-mutated, and restored byte-identical, and
`git status` is clean for the touched files afterward. Never commits.

    python3 scripts/mutate_check.py

The tests drive the guard files in FRESH subprocesses (lint_store.py via cwd; resolve_wave.py via a
subprocess invocation), so a mutation is seen immediately with no module-cache staleness.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

# Each triple isolates ONE guard: `old` must be present exactly once in `file`; `new` reintroduces
# the bug; `test` is the specific node-id that must go from GREEN (pristine) to RED (mutated).
MUTATIONS = [
    {
        "name": "dup-ref lint threshold (>1 duplicate -> ERROR)",
        "file": REPO / "scripts" / "lint_store.py",
        "old": 'if len(paths) > 1 and not ref.startswith("imessage:"):',
        "new": 'if len(paths) > 2 and not ref.startswith("imessage:"):',
        "test": "scripts/tests/test_lint_guards.py::TestDupRefLint::test_duplicate_ref_errors",
    },
    {
        "name": "wave-contract sha256 check (content-pin)",
        "file": REPO / "scripts" / "resolve_wave.py",
        "old": "    if digest != sha256:",
        "new": "    if False:",
        "test": "scripts/tests/test_wave_integrity.py::TestWaveIntegrity::test_sha256_mismatch_throws",
    },
    {
        "name": "wave-contract line-count check",
        "file": REPO / "scripts" / "resolve_wave.py",
        "old": "    if actual_count != expected_count:",
        "new": "    if False:",
        "test": "scripts/tests/test_wave_integrity.py::TestWaveIntegrity::test_count_mismatch_throws",
    },
]


def run_test(node_id):
    """Return True if the test PASSES (green), False if it FAILS (red)."""
    r = subprocess.run([PY, "-m", "pytest", node_id, "-q", "--no-header"],
                       cwd=REPO, capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    return r.returncode == 0, last


def check_one(m):
    path = Path(m["file"])
    original = path.read_bytes()
    rel = path.relative_to(REPO)
    print(f"\n[{m['name']}]  {rel}")
    print(f"  test: {m['test']}")

    ok = True
    try:
        # 1) pristine -> GREEN
        green, line = run_test(m["test"])
        print(f"  pristine : {'GREEN' if green else 'RED'}   ({line})")
        if not green:
            print("  !! expected GREEN on the pristine file — aborting this mutation")
            return False

        # 2) reintroduce the bug (exact, unique replacement)
        text = original.decode("utf-8")
        n = text.count(m["old"])
        if n != 1:
            print(f"  !! anchor found {n} times (need exactly 1) — refusing to mutate")
            return False
        path.write_bytes(text.replace(m["old"], m["new"], 1).encode("utf-8"))

        # 3) mutated -> RED
        green, line = run_test(m["test"])
        print(f"  mutated  : {'GREEN' if green else 'RED'}   ({line})")
        if green:
            print("  !! mutation SURVIVED — the guard's test did NOT catch the bug (no teeth)")
            ok = False
        else:
            print("  mutated  : bug reintroduced -> guard test failed, as required")
    finally:
        # 4) ALWAYS restore, verify byte-identical
        path.write_bytes(original)
        identical = path.read_bytes() == original
        print(f"  restored : {'byte-identical' if identical else 'MISMATCH (!!)'}")
        ok = ok and identical
    return ok


def git_status(files):
    """(porcelain-status-string, returncode) for the given files. Compared start-vs-end so the
    check means 'mutate_check introduced NO net change', independent of any pre-existing
    uncommitted edits (e.g. an edit already in progress)."""
    rels = [str(Path(f).relative_to(REPO)) for f in files]
    r = subprocess.run(["git", "status", "--porcelain", "--", *rels],
                       cwd=REPO, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def main():
    touched = sorted({m["file"] for m in MUTATIONS})
    before, rc0 = git_status(touched)

    results = []
    try:
        for m in MUTATIONS:
            results.append(check_one(m))
    except KeyboardInterrupt:
        print("\ninterrupted — files were restored by the per-mutation finally block")

    after, rc1 = git_status(touched)
    print("\n--- post-run git status for touched guard files (net change from start) ---")
    if rc0 != 0 or rc1 != 0:
        print("  (git unavailable — skipping net-change check)")
        clean = True
    elif before != after:
        print("  DIRTY: mutate_check left a net change!\n"
              f"  before:\n{before or '    (none)'}\n  after:\n{after or '    (none)'}")
        clean = False
    else:
        print("  clean — no net change (files restored exactly as found; any pre-existing "
              "uncommitted edit is unchanged)")
        clean = True

    passed = sum(1 for x in results if x)
    total = len(MUTATIONS)
    all_ok = passed == total and len(results) == total and clean
    print(f"\nmutate_check: {passed}/{total} guards proven to have teeth; "
          f"git {'clean' if clean else 'DIRTY'} -> {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
