#!/usr/bin/env python3
"""Write a wave CONTRACT that pins the exact wave a distiller is allowed to run.

    python3 scripts/wave_prepare.py --source SRC --mode incremental|backfill --wave PATH [--allow-history]

This is the orchestrator/worklist step that runs immediately BEFORE launching a distiller.
It computes the wave's non-empty line count + sha256 and writes
`_sources/<source>.contract.json` atomically (tmp file + os.replace). The distiller then
resolves its wave via `scripts/resolve_wave.py`, which re-verifies count+sha and REFUSES to
run on a mismatch — so an un-threaded `args.waveFile` can no longer silently fall back to a
full-backfill wave (engram Bug 1).

The contract is ONE-SHOT: resolve_wave.py atomically renames it to
`_sources/<source>.contract.consumed.json` when a validated run starts, so a stale contract can
never drive a second, note-overwriting re-distill (guard-audit finding 1). Re-run this script to
re-pin when resuming an interrupted run.

`--mode backfill` re-distills EXISTING notes in place (history), so it additionally requires the
explicit `--allow-history` flag; the authorization is recorded IN the contract
(`allow_history: true`) — deliberately on-disk, NOT a Workflow arg, because args do not thread
reliably through the Workflow harness (the root of Bug 1). resolve_wave.py refuses a backfill
contract without it.

Contract shape (matches scripts/resolve_wave.py):
    {source, mode, wave, expected_count, sha256, created[, allow_history]}

`wave` is stored root-relative when the file is under <root>, else absolute — resolve_wave.py
resolves it the same way. Count semantics MUST match resolve_wave.py: a whitespace-only line is
"empty" and is not counted.
"""
import argparse
import datetime
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib
# Single owner (finding 4): the known full-backfill wave basenames live in
# _manifest/constants/backfill_waves.json and are shared with the Python owner _lib/constants.py. resolve_wave.py refuses such a wave under --mode incremental at run time; this
# script flags the same mistake earlier, before the distiller is ever launched.
from _lib.constants import BACKFILL_WAVES
from _lib.hashing import sha256_file  # noqa: E402  (single owner, also used by sync_verify.py/resolve_wave.py)


def count_nonempty(path):
    """Count non-empty JSONL lines (a line that is only whitespace does not count)."""
    n = 0
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".contract-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# The full set of valid sync_verify.SOURCES keys. Previously hand-copied here (a 2026-07-10
# code-review finding — "one owner per decision" violation, only a test-time check, not an
# import-time guard) because sync_verify.py used to import sha256_file FROM wave_prepare.py,
# making the reverse import circular. That circularity is now broken (sha256_file moved to the
# shared _lib.hashing, imported by wave_prepare.py/sync_verify.py/resolve_wave.py alike), so this
# imports the real thing directly — zero drift risk, not just a caught one.
from sync_verify import SOURCES as KNOWN_SOURCES  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Write a distiller wave contract.")
    ap.add_argument("--source", required=True, choices=sorted(KNOWN_SOURCES),
                    help="contract source name")
    ap.add_argument("--mode", required=True, choices=["incremental", "backfill"])
    ap.add_argument("--wave", required=True, help="path to the wave .jsonl (abs or relative to --root)")
    ap.add_argument("--allow-history", action="store_true", dest="allow_history",
                    help="REQUIRED with --mode backfill: explicitly authorize re-distilling "
                         "existing notes in place (recorded in the contract; resolve_wave.py refuses "
                         "a backfill contract without it)")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()

    if args.mode == "backfill" and not args.allow_history:
        sys.exit("FAIL: --mode backfill RE-DISTILLS EXISTING NOTES IN PLACE (history, not a delta). "
                 "If that is really the intent (e.g. granola-redistill, gmail-remediate2), pass "
                 "--allow-history explicitly. Refusing to write an unauthorized backfill contract "
                 "(guard-audit finding 1).")
    if args.mode == "incremental" and args.allow_history:
        sys.exit("FAIL: --allow-history only applies to --mode backfill — an incremental wave must "
                 "only CREATE new notes and never needs history authorization.")

    root = os.path.abspath(args.root)
    wave = args.wave if os.path.isabs(args.wave) else os.path.join(root, args.wave)
    wave = os.path.abspath(wave)
    if not os.path.exists(wave):
        sys.exit(f"FAIL: wave file not found: {wave}")

    base = os.path.basename(wave)
    if args.mode == "incremental" and base in BACKFILL_WAVES:
        sys.exit(f"FAIL: mode=incremental but wave '{base}' is a known full-backfill default — "
                 f"resolve_wave.py would refuse this. Point --wave at the delta, or use --mode backfill.")

    count = count_nonempty(wave)
    digest = sha256_file(wave)

    rel = os.path.relpath(wave, root)
    wave_field = wave if rel.startswith("..") else rel

    contract = {
        "source": args.source,
        "mode": args.mode,
        "wave": wave_field,
        "expected_count": count,
        "sha256": digest,
        "created": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if args.mode == "backfill":
        contract["allow_history"] = True   # the on-disk token resolve_wave.py requires for backfill
    out = os.path.join(root, "_sources", f"{args.source}.contract.json")
    atomic_write(out, json.dumps(contract, indent=2) + "\n")
    flags = " allow_history=true" if args.mode == "backfill" else ""
    print(f"wave contract: source={args.source} mode={args.mode}{flags} wave={wave_field} "
          f"count={count} sha256={digest[:12]}… -> {os.path.relpath(out, root)}")


if __name__ == "__main__":
    main()
