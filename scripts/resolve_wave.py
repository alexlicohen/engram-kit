#!/usr/bin/env python3
"""Validate + CONSUME a distiller's wave contract BEFORE the Workflow launches it.

    python3 scripts/resolve_wave.py --source SRC [--root R]

This is the Python port of a retired JavaScript helper (`resolveWave`). The guard moved here
because engram distillers run under the Workflow runtime, which is PURE JS: no dynamic loader
(`import()` is unavailable) and no Node/filesystem APIs (no node:fs / node:crypto / node:path).
resolveWave used node:fs + node:crypto, so a distiller's dynamic `await import(...)` of it failed at
launch ("import() is not available in workflow scripts") and blocked the ENTIRE pipeline. The guard
now runs in Python, invoked by the ORCHESTRATOR immediately before
`Workflow({scriptPath:<distiller>})`. The distiller then reads the already-consumed contract with
its own Read tool (see any of the *_distill.mjs Setup phases).

Behavior is IDENTICAL to (and no weaker than) the old resolveWave. It validates
`_sources/<S>.contract.json`:
  - mode ∈ {incremental, backfill}
  - contract.source == <S>                    (right contract wired to this distiller)
  - "wave" is a non-empty string; the wave file exists
  - mode==backfill REQUIRES allow_history == true   (re-distilling history is on-disk-authorized,
    never a Workflow arg — un-threaded args were the root of Bug 1)
  - mode==incremental REFUSES a wave whose basename is a known FULL-BACKFILL default (Bug 1)
  - the wave's non-empty line count == expected_count
  - the wave's sha256 == contract.sha256
Any mismatch → exit 1 with a clear message (FAIL-CLOSED; nothing is consumed).

On success the contract is ONE-SHOT: atomically renamed `_sources/<S>.contract.json` →
`_sources/<S>.contract.consumed.json` (the atomic claim), then the validated wave path is printed
(root-relative + absolute). A re-invoke finds no live contract and exits 1 — a double launch cannot
re-distill the same wave. `scripts/sync_verify.py` reads the consumed contract afterward.

CORRECTIONS BRIEF (added 2026-09-05). Immediately after consuming, this ALSO regenerates the
source's fidelity-audit corrections brief (`corrections_brief.write_brief`) and records it in the
consumed contract as:

    "corrections_brief": {"path": "_sources/<S>.corrections-brief.txt",
                          "sha256": "<hex of the file>", "lines": <non-empty line count>}
                       or null   (wave sources with no ledger: gmail-update, gmail-secondary, drive, ...)

It lives here, not as a separate SYNC.md step, because the brief is interpolated verbatim into the
distiller prompt and the two were separately-ordered manual commands: a forgotten/stale brief
injected the PREVIOUS wave's corrections (or none) with nothing anywhere to notice. Consuming the
contract and pinning the brief are now the same pre-launch act, and the sha256 lets each distiller's
Setup agent verify the file ON DISK is the one this step wrote — the same file-read→interpolate
contract already proven for the wave itself. WAVE_SOURCE_LEDGER maps wave source → ledger source
(granola-new→granola); an unmapped wave source records null, never a fabricated empty brief.

Single owner: BACKFILL_WAVES comes from `_lib.constants`; `count_nonempty`/`sha256_file` are
imported from `wave_prepare`/`_lib.hashing` — the SAME functions that WROTE expected_count/sha256,
so the writer and the validator can never drift; the brief's content rules stay in
`corrections_brief.py` (whose `VALID_SOURCES` come from `log_correction.py`).
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib + wave_prepare
from _lib.constants import BACKFILL_WAVES              # noqa: E402  (single owner of the backfill list)
from _lib.hashing import sha256_file                    # noqa: E402  (single owner of the hash, same as sync_verify.py/wave_prepare.py)
from wave_prepare import count_nonempty                # noqa: E402  (single owner of the nonempty-line count)
from corrections_brief import write_brief, brief_path  # noqa: E402  (single owner of the brief's content)

# Wave source -> corrections-ledger source. A wave source is a PIPELINE name (granola-new,
# imsg-reclass, gmail-update); a ledger source is a CONNECTOR in log_correction.VALID_SOURCES. Only
# the mappings below have a distiller whose prompt carries FIDELITY RULES to reinforce; every other
# wave source (gmail-update, gmail-secondary, drive, granola-redistill, imsg-reclass, ...) records
# `corrections_brief: null` — deliberately NOT an empty brief, so a Setup agent can tell "this
# pipeline has no corrections loop" from "this source has no OPEN corrections right now".
WAVE_SOURCE_LEDGER = {
    "granola-new": "granola",
    "gmail": "gmail",
    "imessage": "imessage",
    "whatsapp": "whatsapp",
    "outlook": "outlook",
}

# Sources whose distiller checks a per-item done-marker before (re-)processing a thread/chunk, so
# re-pinning + relaunching the SAME wave after an interruption safely skips already-completed
# items: imessage/whatsapp (stamp_markers.py) and granola-new (_sources/granola-new-done.d).
# Every other source's distiller (gmail/outlook/granola-redistill/imsg-reclass/..., a drive one if you add it)
# has NO such check — re-pinning after a partial run reprocesses EVERYTHING in the wave from
# scratch, including whatever the interrupted run already completed.
# imsg-reclass is deliberately NOT in this set (a 2026-07-10 code-review-caught bug: it was
# wrongly included here, printing a false "safe to resume" message) — verified via
# _imessage-pending/imsg_reclassify.mjs, which has zero references to stamp_markers.py or any
# done.d directory; its only per-item effect is an implicit mv-based side effect, not a marker a
# resumed run could check before reprocessing.
HAS_DONE_MARKERS = {"imessage", "whatsapp", "granola-new"}

VALID_MODES = ("incremental", "backfill")


def fail(msg):
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(1)


def regen_corrections_brief(source, root):
    """Regenerate the fidelity-audit corrections brief for the LEDGER source behind this wave source
    and return the record to pin in the consumed contract, or None when the wave source has no
    ledger (WAVE_SOURCE_LEDGER). Runs AFTER the contract is consumed so a refused wave never rewrites
    a brief a previous, still-running launch pinned."""
    ledger_source = WAVE_SOURCE_LEDGER.get(source)
    if ledger_source is None:
        return None
    outp, text = write_brief(ledger_source, root)
    return {
        "path": os.path.relpath(outp, root),
        "sha256": sha256_file(outp),
        "lines": sum(1 for ln in text.split("\n") if ln.strip()),
    }


def resolve_wave(source, root):
    """Validate + atomically CONSUME the contract for `source`, then regenerate + pin that source's
    corrections brief. Returns (wave_abs, expected_count, mode, brief) on success (brief = the
    corrections_brief record or None); exits 1 on ANY mismatch (fail-closed). Mirrors
    the retired JS resolveWave, plus the brief step."""
    src_dir = os.path.join(root, "_sources")
    contract_path = os.path.join(src_dir, "%s.contract.json" % source)
    consumed_path = os.path.join(src_dir, "%s.contract.consumed.json" % source)

    if not os.path.exists(contract_path):
        extra = ""
        if os.path.exists(consumed_path):
            if source in HAS_DONE_MARKERS:
                resume_note = ("re-pinning + relaunching is safe to resume an interrupted run — "
                                "done-markers make it skip already-completed items")
            else:
                resume_note = (f"{source}'s distiller has NO per-item done-marker check — "
                                f"re-pinning + relaunching after an interruption REPROCESSES THE "
                                f"WHOLE WAVE FROM SCRATCH, including whatever the interrupted run "
                                f"already completed (risk: the same thread can end up with two "
                                f"different notes if the LLM's choices differ between passes). "
                                f"Only re-pin if you're SURE nothing from this wave was already "
                                f"written, or reconcile manually first")
            extra = (" NOTE: a CONSUMED contract exists (%s) — a previous launch already claimed "
                     "this wave. Re-pin with wave_prepare.py ONLY if you intend to re-run it (%s)."
                     % (consumed_path, resume_note))
        fail("wave contract MISSING: %s — run `python3 scripts/wave_prepare.py --source %s --mode "
             "incremental|backfill --wave <path>` before launching the %s distiller (refusing to "
             "run on an un-pinned wave).%s" % (contract_path, source, source, extra))

    try:
        with open(contract_path, encoding="utf-8") as f:
            contract = json.load(f)
    except (OSError, ValueError) as e:
        fail("wave contract UNREADABLE (%s): %s" % (contract_path, e))

    c_source = contract.get("source")
    mode = contract.get("mode")
    wave = contract.get("wave")
    expected_count = contract.get("expected_count")
    sha256 = contract.get("sha256")

    if mode not in VALID_MODES:
        fail("wave contract %s: mode must be \"incremental\" or \"backfill\", got %r"
             % (contract_path, mode))
    if mode == "backfill" and contract.get("allow_history") is not True:
        fail("wave contract %s: mode=backfill but allow_history is not true — a backfill wave "
             "RE-DISTILLS EXISTING NOTES IN PLACE and must be explicitly authorized when the "
             "contract is written: python3 scripts/wave_prepare.py --source %s --mode backfill "
             "--wave <path> --allow-history. Refusing to run history silently (guard-audit "
             "finding 1)." % (contract_path, source))
    if c_source != source:
        fail("wave contract %s: source field %r != requested %r (wrong contract wired to this "
             "distiller)." % (contract_path, c_source, source))
    if not isinstance(wave, str) or not wave:
        fail("wave contract %s: missing/invalid \"wave\" field" % contract_path)

    wave_path = wave if os.path.isabs(wave) else os.path.join(root, wave)
    wave_path = os.path.abspath(wave_path)
    if not os.path.exists(wave_path):
        fail("wave file MISSING: %s (declared by contract %s)" % (wave_path, contract_path))

    wave_base = os.path.basename(wave_path)
    if mode == "incremental" and wave_base in BACKFILL_WAVES:
        fail("wave contract %s: mode=incremental but wave basename \"%s\" is a known FULL-BACKFILL "
             "default — refusing to re-distill history as if it were a delta (Bug 1)."
             % (contract_path, wave_base))

    actual_count = count_nonempty(wave_path)
    if actual_count != expected_count:
        fail("wave LINE-COUNT mismatch for \"%s\": %s has %d non-empty JSONL lines, contract %s "
             "expects %s — the wave changed since the contract was written; regenerate it with "
             "scripts/wave_prepare.py." % (source, wave_path, actual_count, contract_path,
                                           expected_count))

    digest = sha256_file(wave_path)
    if digest != sha256:
        fail("wave SHA256 mismatch for \"%s\": %s digest %s != contract %s (%s) — the wave content "
             "changed; regenerate the contract with scripts/wave_prepare.py."
             % (source, wave_path, digest, sha256, contract_path))

    # ALL validation passed — CONSUME the contract (one-shot atomic claim). The atomic rename is the
    # claim: a re-invoke finds no live contract (MISSING above); a concurrent double-launch races
    # the rename and exactly one wins (the loser gets ENOENT). sync_verify reads the consumed copy.
    try:
        os.rename(contract_path, consumed_path)
    except OSError as e:
        fail("wave contract CLAIM failed for \"%s\": rename %s -> %s (%s) — most likely a "
             "concurrent launch already consumed this contract; refusing to double-run the wave."
             % (source, contract_path, consumed_path, e))

    # Pin the corrections brief INTO the consumed contract, in this same pre-launch step (see the
    # module docstring). A malformed corrections ledger raises out of write_brief -> the launch stops
    # here rather than handing the distiller a brief nobody can vouch for.
    try:
        brief = regen_corrections_brief(source, root)
    except (OSError, ValueError) as e:
        fail("corrections BRIEF regeneration failed for \"%s\" (%s) — the contract is already "
             "CONSUMED (%s), so fix the ledger (python3 scripts/corrections_lint.py) and re-pin the "
             "wave with wave_prepare.py before launching." % (source, e, consumed_path))
    contract["corrections_brief"] = brief
    tmp = consumed_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(contract, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, consumed_path)
    except OSError as e:
        fail("consumed contract UPDATE failed for \"%s\": %s (%s) — the distiller's Setup agent "
             "verifies the corrections brief against this field, so refusing to launch without it."
             % (source, consumed_path, e))

    return wave_path, expected_count, mode, brief


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Validate + consume a distiller wave contract (orchestrator pre-launch step).",
        epilog="""\
Also regenerates this source's fidelity-audit corrections brief and RECORDS it in the consumed
contract (_sources/<S>.contract.consumed.json) as:

  "corrections_brief": {"path": "_sources/<L>.corrections-brief.txt",
                        "sha256": "<sha256 of that file>",
                        "lines": <number of non-empty lines>}

or null for a wave source with no corrections ledger. <L> is the LEDGER source for this wave source
(granola-new->granola, gmail->gmail, imessage->imessage, whatsapp->whatsapp, outlook->outlook;
gmail-update / gmail-secondary / drive / granola-redistill / imsg-reclass -> null). The distiller's Setup
agent re-hashes the file on disk and must refuse to launch on a mismatch — the brief is
interpolated verbatim into the prompt, so a stale one silently injects the previous wave's
corrections.""")
    ap.add_argument("--source", required=True)
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    wave_abs, expected_count, mode, brief = resolve_wave(args.source, root)
    rel = os.path.relpath(wave_abs, root)
    # Line 1 is human/orchestrator-facing; the wave path is printed both root-relative and absolute
    # so the caller can hand the distiller whichever it needs.
    print("RESOLVED mode=%s expected=%d source=%s" % (mode, expected_count, args.source))
    print("wave_rel=%s" % rel)
    print("wave_abs=%s" % wave_abs)
    if brief is None:
        print("corrections_brief=none (source %s has no corrections ledger)" % args.source)
    else:
        print("corrections_brief=%s lines=%d sha256=%s"
              % (brief["path"], brief["lines"], brief["sha256"]))


if __name__ == "__main__":
    main()
