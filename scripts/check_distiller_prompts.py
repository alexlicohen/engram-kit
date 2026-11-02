#!/usr/bin/env python3
"""Static syntax check for distiller prompt-builder functions — catches the backtick-terminates-
the-template-literal class of bug WITHOUT a live Workflow launch.

Why this exists: the distiller `.mjs` files (`_*-pending/*.mjs`) build their agent prompt as one
giant JS template literal. Editing the prose inside one has twice shipped broken (commit 2c204adb
blocked a whole sync) because a stray backtick in the prose silently terminates the literal early.
`node --check` is USELESS on these files — they use top-level `return`/`await`, which is illegal
outside a function, so it always fails regardless of the prompt body. Until now the only real test
was a live Workflow launch (slow, costs agent calls, and only exercises the ONE distiller you happen
to run).

What it does, per distiller file — TWO stages:
  STAGE 1 (authoritative): a WHOLE-FILE syntax check. Strip the leading `export ` from each line
     (illegal inside a function) and wrap the ENTIRE file body in `async function __w(){ … }` (so its
     top-level `return`/`await` become legal), then `node --check` it. This catches a stray backtick
     or unbalanced `${` ANYWHERE in the file — the Setup-agent prompt literal, a `throw()` message,
     any hand-edited region — not just inside the prompt() function. An earlier version parsed only
     the isolated prompt() function and so gave a FALSE PASS when the break was in another literal;
     since the backtick-terminates-the-literal bug is exactly what this tool exists to catch, that
     was the wrong scope. `node --check` only parses (no execution), so wrapping is side-effect-free.
  STAGE 2 (best-effort bonus): isolate the prompt-builder function, stub its `${UPPER_CASE}` consts,
     and RENDER it (execute with dummy args) to confirm the prompt string actually builds. Run TWICE,
     under TWO stub modes, because a prompt may branch on a stubbed const:
       * "empty"    — every `${UPPER}` stubs to ''  (falsy)
       * "sentinel" — every `${UPPER}` stubs to 'X' (truthy)
     The Feature C fold `${CORRECTIONS ? `…${CORRECTIONS}…` : ''}` is the reason: under the empty
     stub the ternary always takes the '' branch, so the INNER template literal — its own nested
     backticks and `${}` — is never executed and a runtime break inside it renders as a pass. The
     sentinel pass drives every such branch the other way.
     A stage-2 failure when stage 1 passed is NOT a hard fail — the file is syntactically valid (the
     property we care about); the brace-matcher is just a naive scan that can't always isolate the
     function. But a DISAGREEMENT between the two modes (one renders, the other doesn't) IS a hard
     fail: extraction problems hit both modes equally, so an asymmetry is a real branch bug.
  A file FAILS iff stage 1 fails, or the two stage-2 modes disagree. Backtick parity is reported only
  as a weak secondary hint.

This is an ON-DEMAND INSTRUMENT, not a gate: it is NOT wired into scripts/check.sh, because check.sh
must run on a machine without `node` installed. Run it by hand after editing a distiller's prompt
prose, or whenever you want a fast static check across all of them.

Usage:
    python3 scripts/check_distiller_prompts.py
    python3 scripts/check_distiller_prompts.py --glob '_*-pending/*.mjs'   # override the glob
    python3 scripts/check_distiller_prompts.py -v                          # print rendered length detail
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DEFAULT_GLOB = "_*-pending/*.mjs"

# Matches a top-level `function <name>(` where name is exactly "prompt" or ends in "...Prompt"
# (distillers name it `prompt`, `distillPrompt`, `remediatePrompt`, etc).
_PROMPT_FN_RE = re.compile(r"^function\s+(\w*[Pp]rompt)\s*\(", re.MULTILINE)

# ${UPPER_CASE} interpolations — the module-level consts a prompt function closes over.
_INTERP_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Leading `export ` (illegal inside a function) — stripped before wrapping the file for the parse.
_EXPORT_RE = re.compile(r"(?m)^export\s+")


def whole_file_check(src, node_bin):
    """STAGE 1 — syntax-check the WHOLE file. Wrap it in an async function (legalizes its top-level
    return/await) with `export ` stripped, then `node --check` (parse only, no execution). Returns
    (ok, stderr). This is what actually catches a stray backtick anywhere in the file."""
    wrapped = "async function __wrapper__(){\n" + _EXPORT_RE.sub("", src) + "\n}\n"
    fd, tmp_path = tempfile.mkstemp(suffix=".mjs", prefix="check_distiller_whole_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(wrapped)
        proc = subprocess.run([node_bin, "--check", tmp_path],
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "node --check timed out"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return proc.returncode == 0, proc.stderr.strip()


def find_distillers(pattern=DEFAULT_GLOB, root=_ROOT):
    """Glob for distiller .mjs files and keep only those defining a prompt-builder function."""
    paths = sorted(glob.glob(os.path.join(root, pattern)))
    out = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        if _PROMPT_FN_RE.search(src):
            out.append(p)
    return out


def extract_prompt_fn(src):
    """Brace-match the first top-level prompt-builder function out of `src`.

    Returns (fn_name, fn_source) on success, or (None, None) if extraction failed (e.g. no match,
    or brace-matching never closed — a real possibility since this is a naive scan that doesn't
    understand strings/template literals, so an unbalanced brace INSIDE one can throw it off; we
    catch that as a clean failure rather than raising).
    """
    m = _PROMPT_FN_RE.search(src)
    if not m:
        return None, None
    fn_name = m.group(1)
    i = m.start()
    try:
        j = src.index("{", i)
    except ValueError:
        return None, None
    depth = 0
    k = j
    n = len(src)
    while k < n:
        ch = src[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return fn_name, src[i:k + 1]
        k += 1
    return None, None


def backtick_parity_ok(fn_src):
    """Cheap secondary signal: an even count of literal ` chars. Not authoritative (an escaped
    backtick \\` throws this off), but worth surfacing alongside the real (node-executed) check."""
    return fn_src.count("`") % 2 == 0


# The two stage-2 stub values. '' leaves every `${UPPER} ? … : ''` fold on its falsy branch; 'X'
# forces the truthy branch so the inner (nested-backtick) template literal is actually executed.
STUB_MODES = (("empty", ""), ("sentinel", "X"))


def build_harness(fn_name, fn_src, stub=""):
    """Stub every ${UPPER_CASE} interpolation the function closes over, then call it and print the
    rendered length. Args are positional and few (start/end/today at most) — string/number stubs are
    enough to exercise the template literal without needing real distiller state. `stub` is the value
    every stubbed const takes: '' (falsy) or a sentinel like 'X' (truthy) — see STUB_MODES."""
    names = sorted(set(_INTERP_RE.findall(fn_src)))
    stubs = "\n".join(f"const {name} = {stub!r};" for name in names)
    # The function body may reference its own params (start, end, today) via ${...} too — those
    # aren't ALL-CAPS so _INTERP_RE won't catch them; they come from the call args below instead.
    call_args = "1, 2, '2026-01-01'"
    harness = (
        "// AUTO-GENERATED by check_distiller_prompts.py — do not edit.\n"
        f"{stubs}\n\n"
        f"{fn_src}\n\n"
        f"const __rendered = {fn_name}({call_args});\n"
        "if (typeof __rendered !== 'string' || __rendered.length === 0) {\n"
        "  console.error('rendered prompt is empty or not a string');\n"
        "  process.exit(1);\n"
        "}\n"
        "process.stdout.write(String(__rendered.length));\n"
    )
    return harness


def _render_prompt_fn(fn_name, fn_src, node_bin, stub=""):
    """STAGE 2 — stub the ${UPPER} consts with `stub`, execute the isolated prompt fn, return
    (ok, message)."""
    harness = build_harness(fn_name, fn_src, stub)
    fd, tmp_path = tempfile.mkstemp(suffix=".mjs", prefix="check_distiller_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run([node_bin, tmp_path], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "node timed out"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if proc.returncode != 0:
        return False, (proc.stderr.strip() or "(no stderr)").splitlines()[-1]
    rendered_len = proc.stdout.strip()
    if not rendered_len or not rendered_len.isdigit() or int(rendered_len) == 0:
        return False, "rendered empty"
    return True, f"{rendered_len} chars"


def check_one(path, node_bin, verbose=False):
    """Returns (ok, message) for one distiller file. `ok` is STAGE 1 (whole-file syntax) — the
    authoritative check — AND stage-2 agreement between the two stub modes; a stage-2 failure in
    BOTH modes stays a best-effort note (the brace-matcher cannot always isolate the function)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        return False, f"could not read file — {e}"

    parity_note = "" if src.count("`") % 2 == 0 else " [WARN: odd backtick count]"

    # STAGE 1 — authoritative. A stray backtick ANYWHERE in the file fails here.
    ok, stderr = whole_file_check(src, node_bin)
    if not ok:
        return False, f"whole-file node --check failed{parity_note}\n{_indent(stderr or '(no stderr)')}"

    # STAGE 2 — render the isolated prompt builder under BOTH stub modes (see STUB_MODES). Falsy
    # stubs never enter a `${CONST ? `…` : ''}` fold, so the sentinel pass is what actually executes
    # the inner template literal of every such branch.
    fn_name, fn_src = extract_prompt_fn(src)
    if fn_name is None:
        return True, f"parses OK (render skipped: no isolable prompt-builder function){parity_note}"
    results = [(label, *_render_prompt_fn(fn_name, fn_src, node_bin, stub))
               for label, stub in STUB_MODES]
    detail = "; ".join(f"{label}-stub {msg}" for label, ok, msg in results)
    n_ok = sum(1 for _, r_ok, _ in results if r_ok)
    if n_ok == 0:                       # both modes failed -> an extraction artifact, not a branch bug
        return True, f"parses OK (render skipped: {detail}){parity_note}"
    if n_ok < len(results):             # one mode renders, the other doesn't -> a real branch bug
        return False, (f"render DISAGREES between stub modes — a conditional branch breaks under one "
                       f"of them: {detail}{parity_note}")
    return True, f"parses OK; rendered {detail}{parity_note}"


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in text.splitlines())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--glob", default=DEFAULT_GLOB,
                         help=f"glob (relative to repo root) for distiller files (default: {DEFAULT_GLOB})")
    parser.add_argument("-v", "--verbose", action="store_true", help="print extra detail per file")
    args = parser.parse_args(argv)

    node_bin = shutil.which("node")
    if not node_bin:
        print("SKIP: `node` not found on PATH — this check requires node, but check.sh does not "
              "depend on it, so this is not a failure.")
        return 0

    distillers = find_distillers(args.glob, root=_ROOT)
    if not distillers:
        print(f"No distiller files matched glob '{args.glob}' under {_ROOT} — nothing to check.")
        return 0

    n_fail = 0
    for path in distillers:
        rel = os.path.relpath(path, _ROOT)
        ok, msg = check_one(path, node_bin, verbose=args.verbose)
        if ok:
            print(f"OK   {rel} — {msg}")
        else:
            n_fail += 1
            print(f"FAIL {rel} — {msg}")

    n = len(distillers)
    print(f"\n{n - n_fail}/{n} distiller prompt(s) OK" + (f", {n_fail} FAILED" if n_fail else ""))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
