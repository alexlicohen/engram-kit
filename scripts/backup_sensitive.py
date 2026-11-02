#!/usr/bin/env python3
"""Snapshot the LOCAL-ONLY sensitive/exclusion state to timestamped tarballs before any
reclassify/distill pass touches it — a sensitive queue under `_sources/` got clobbered once by a
re-classify pass — AND refresh the git-crypt-encrypted vault copies of the exclusion ledgers so they
survive machine loss.

THE VAULT IS OPTIONAL AND MUST BE ENCRYPTED. Vault copies are written only when this repo encrypts
`_vault/` with git-crypt (a `.gitattributes` line `_vault/** filter=git-crypt diff=git-crypt` — see
README › Secrets). Without it, the vault step is SKIPPED with a notice and only the local tarballs
are written: a plaintext `_vault/` would put exclusion ledgers into ordinary git history.

    python3 scripts/backup_sensitive.py [--root R]

Two backups, different failure modes:
  1. Timestamped tarball -> ~/.engram-backups/ (same machine): guards against an in-place clobber.
  2. Ledger copy -> _vault/**/ (git-crypt -> the remote, encrypted): guards against MACHINE LOSS.
     The live ledgers are gitignored/local-only, so without the vault copy a fresh clone has no
     record of what to exclude and a re-sync re-ingests it.

Three queues, each backed up INDEPENDENTLY — an absent or empty one never skips the others:
  * iMessage — `_sources/imsg-sensitive/` -> `~/.engram-backups/imsg-sensitive-<ts>.tgz`; the
    top-level `*.jsonl` ledgers -> `_vault/imsg-sensitive/` (NOT `confirmed/`, which is local-only
    by design). `excluded_chat_guids()` reads the union of live + vault, so this copy is a live DR
    fallback, not just an archive.
  * WhatsApp — `_sources/whatsapp-sensitive/` the same way. It had no backup at
    all: the same durability gap the Outlook one below closes.
  * Outlook PHI — `_sources/outlook-phi-excluded.jsonl` (which threads must
    NEVER be distilled) + `_sources/outlook-released.txt` (the false-positive releases) ->
    `~/.engram-backups/outlook-sensitive-<ts>.tgz` and `_vault/outlook-sensitive/
    {phi-excluded.jsonl,released.txt}`. STICKY-ONCE-HELD made the ledger STATE rather than a pure
    derivative of the current scan: a lost row silently RELEASES the hold it carried.
    **The ledger is PROJECTED before it leaves `_sources/` — see LEDGER_VAULT_FIELDS.** Its raw rows
    carry `subject` and `participants`, and for the bare-name/MRN holds the subject IS the patient's
    name (and sometimes their MRN), so the raw file must never reach git, encrypted or not. The
    vault copy keeps only the opaque/enum fields a restore needs. Unlike the iMessage side, nothing
    reads it automatically — restoring is a manual copy back into `_sources/` before the first
    post-clone sync (see SYNC.md, Outlook step 4).

Vault copies are UNION-MERGED, never overwritten: a regressed/truncated live ledger must not shrink
the vault and drop an exclusion. An exclusion, once recorded, is append-only in the vault.

Copy-only, never deletes/modifies the source. Idempotent. Plain CLI (NOT the Workflow sandbox), so
datetime.now(timezone.utc) is fine here.

Usage: run BEFORE the iMessage reclassify/distill step, and in the Outlook pass once
`outlook_worklist.py` has rewritten the ledger (see SYNC.md).
"""
import datetime
import glob
import json
import os
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.expanduser("~/.engram-backups")

# Whole-directory sensitive queues: tarred entire, top-level *.jsonl ledgers vaulted.
CHAT_QUEUES = ("imsg-sensitive", "whatsapp-sensitive")

OUTLOOK_LEDGER = os.path.join("_sources", "outlook-phi-excluded.jsonl")
OUTLOOK_RELEASED = os.path.join("_sources", "outlook-released.txt")

# --- the PHI projection: the ONE owner of what may leave `_sources/` for the vault ----------------
# A CLOSED ALLOWLIST, deliberately not a denylist: a field added to the ledger later defaults to
# EXCLUDED, so a future identifier cannot leak into git by omission. Every field here is an opaque
# id, an enum or a boolean.
#
# Why each one earns its place (verified against `outlook_worklist.py`, which is the only reader of
# a restored ledger — `prior_ledger()` + the sticky/carry-forward block):
#   thrid          — IS the "ever held" set. `prior_ledger` keys on it and fail-closed exits
#                    without it; `ever_held = set(prior) | ...` uses nothing else.
#   reason         — read as the last-resort reason fallback for a sticky re-hold.
#   first_reason   — read to preserve the ORIGINAL reason across a carry-forward.
#   level          — enum (high|medium|subject|sticky) the carry-forward path preserves.
#   sticky,
#   carried_forward— the hold-state booleans; `grep '"sticky": true'` is how the sticky rows are
#                    counted.
# Nothing else in a ledger row is read to make a decision, so nothing else needs to survive.
#
# DELIBERATELY EXCLUDED — do not add these back:
#   subject        — for a `bare-name-subject` or `mrn-subject` hold this is literally the patient's
#                    name (and sometimes their MRN). This is the field the whole projection exists
#                    for.
#   participants   — patient/family/clinic addresses.
#   domain, n_msgs, strong, phi_hold_subject, phi_hold_reason — descriptive only; a restore does not
#                    read them, so they buy nothing and widen the surface.
LEDGER_VAULT_FIELDS = ("thrid", "reason", "first_reason", "level", "sticky", "carried_forward")


def project_ledger_row(row):
    """Project one PHI-ledger row down to LEDGER_VAULT_FIELDS. Absent fields stay absent (no None
    padding), so the projection of an already-projected row is itself — which also means a vault
    file written by an older version is STRIPPED on the next run, not grandfathered in."""
    return {k: row[k] for k in LEDGER_VAULT_FIELDS if k in row}


def union_merge(live_path, vault_path):
    """Union of the vault copy and the live file into vault_path, PRIOR LINES FIRST so the vault can
    only ever grow (a truncated/regressed live file must not drop a recorded exclusion). Writes only
    when the result differs from what is already there. Returns (n_merged, n_live)."""
    live = [ln for ln in open(live_path, encoding="utf-8").read().splitlines() if ln.strip()]
    prior = ([ln for ln in open(vault_path, encoding="utf-8").read().splitlines() if ln.strip()]
             if os.path.exists(vault_path) else [])
    merged, seen = [], set()
    for ln in prior + live:                       # prior first: vault lines are never dropped
        if ln not in seen:
            seen.add(ln); merged.append(ln)
    if merged != prior:
        with open(vault_path, "w", encoding="utf-8") as out:
            out.write("\n".join(merged) + "\n")
    return len(merged), len(live)


def _read_ledger(path, label):
    """Parse a PHI-ledger JSONL. FAIL-CLOSED on a malformed line, matching
    `outlook_worklist.prior_ledger`: silently skipping a row here would write a vault copy that is
    missing a hold, which is the exact failure the vault copy exists to prevent."""
    rows = []
    for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            sys.exit(f"FAIL (fail-closed PHI): {label} {path}:{lineno} is not valid JSON ({e}). "
                     f"Refusing to write a vault copy that would silently omit a hold.")
        if not isinstance(row, dict) or not row.get("thrid"):
            sys.exit(f"FAIL (fail-closed PHI): {label} {path}:{lineno} has no `thrid`. Refusing to "
                     f"write a vault copy that would silently omit a hold.")
        rows.append(row)
    return rows


def merge_projected_ledger(live_path, vault_path):
    """Union the vault's projected ledger with the live one, KEYED ON `thrid`, not on the line.
    The projection is not line-stable — a row gains `sticky`/`carried_forward` as it ages and its
    `reason` can be re-derived — so line dedup would accumulate one copy per state of the same
    thread. A thrid already in the vault is NEVER dropped; for a thrid in both, live fields win but
    vault-only fields survive, so a merge can only ever ADD information.
    Returns (n_merged, n_live)."""
    live = {r["thrid"]: project_ledger_row(r) for r in _read_ledger(live_path, "live ledger")}
    prior = ({r["thrid"]: project_ledger_row(r) for r in _read_ledger(vault_path, "vault ledger")}
             if os.path.exists(vault_path) else {})

    merged = {}
    for t in sorted(set(prior) | set(live)):
        row = dict(prior.get(t) or {})
        row.update(live.get(t) or {})
        row["thrid"] = t
        merged[t] = row

    text = "".join(json.dumps(merged[t], ensure_ascii=False) + "\n" for t in sorted(merged))
    old = open(vault_path, encoding="utf-8").read() if os.path.exists(vault_path) else None
    if text != old:
        with open(vault_path, "w", encoding="utf-8") as out:
            out.write(text)
    return len(merged), len(live)


def vault_enabled(root):
    """True iff `_vault/` is git-crypt-encrypted in this repo (a .gitattributes rule for it naming
    filter=git-crypt). The vault copies ride git to the remote, so they are written ONLY then."""
    try:
        with open(os.path.join(root, ".gitattributes"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    return any(l.strip().startswith("_vault") and "filter=git-crypt" in l for l in lines)


def _vault_skipped(what):
    print(f"vault copy SKIPPED for {what}: _vault/ is not git-crypt-encrypted in this repo "
          f"(local tarball only; see README › Secrets)")


def backup_chat_queue(root, ts, queue):
    """One whole-directory sensitive queue (`imsg-sensitive`, `whatsapp-sensitive`): tar the whole
    directory, vault the top-level `*.jsonl` ledgers only — the `confirmed/` holding area stays
    local-only by design."""
    src = os.path.join(root, "_sources", queue)
    if not os.path.isdir(src) or not os.listdir(src):
        print(f"nothing to back up: {src} is absent or empty")
        return

    dest = os.path.join(BACKUP_DIR, f"{queue}-{ts}.tgz")
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src, arcname=queue)

    size = os.path.getsize(dest)
    print(f"backed up {src} -> {dest} ({size:,} bytes)")

    # These ride git-crypt to the remote — so only when the vault IS encrypted.
    if not vault_enabled(root):
        _vault_skipped(queue)
        return
    vault = os.path.join(root, "_vault", queue)
    os.makedirs(vault, exist_ok=True)
    ledgers = sorted(glob.glob(os.path.join(src, "*.jsonl")))
    for f in ledgers:
        n_merged, n_live = union_merge(f, os.path.join(vault, os.path.basename(f)))
        if n_merged > n_live:
            print(f"  NOTE {os.path.basename(f)}: vault keeps {n_merged - n_live} line(s) "
                  f"the live ledger no longer has (union — never shrinks)")
    if ledgers:
        print(f"refreshed vault copy: {len(ledgers)} ledger(s) -> {vault} "
              f"(git-crypt-encrypted, union-merged; commit to persist off-machine)")


def backup_outlook(root, ts):
    """The Outlook PHI-exclusion state. Independent of the chat queues on purpose: it is written by
    a different pass (`outlook_worklist.py`), and STICKY-ONCE-HELD made it real state — losing
    `outlook-phi-excluded.jsonl` does not just lose an archive, it silently releases every hold the
    file carried, because the rebuild would re-derive from the CURRENT scan only.

    The vault copy is written FIRST and the tarball is taken FROM THE VAULT, so the only version of
    this ledger that ever leaves `_sources/` is the projected one (LEDGER_VAULT_FIELDS)."""
    ledger = os.path.join(root, OUTLOOK_LEDGER)
    released = os.path.join(root, OUTLOOK_RELEASED)
    if not os.path.isfile(ledger) and not os.path.isfile(released):
        print(f"nothing to back up: no {OUTLOOK_LEDGER} / {OUTLOOK_RELEASED}")
        return

    # No encrypted vault: project into a throwaway dir instead, so the tarball still holds only the
    # projection (the raw ledger never leaves `_sources/` either way).
    use_vault = vault_enabled(root)
    tmpdir = None if use_vault else tempfile.mkdtemp(prefix="engram-outlook-projection-")
    vault = (os.path.join(root, "_vault", "outlook-sensitive") if use_vault
             else os.path.join(tmpdir, "outlook-sensitive"))
    os.makedirs(vault, exist_ok=True)
    vaulted = []

    if os.path.isfile(ledger):
        dst = os.path.join(vault, "phi-excluded.jsonl")
        n_merged, n_live = merge_projected_ledger(ledger, dst)
        extra = (f"  (vault keeps {n_merged - n_live} thrid(s) the live ledger no longer has)"
                 if n_merged > n_live else "")
        print(f"  phi-excluded.jsonl: {n_live:,} live row(s) -> {n_merged:,} thrid(s) in vault, "
              f"projected to {'/'.join(LEDGER_VAULT_FIELDS)}{extra}")
        vaulted.append(dst)

    if os.path.isfile(released):
        dst = os.path.join(vault, "released.txt")
        n_merged, n_live = union_merge(released, dst)          # thrids + comments only
        extra = (f"  (vault keeps {n_merged - n_live} line(s) the live file no longer has)"
                 if n_merged > n_live else "")
        print(f"  released.txt: {n_live:,} live line(s) -> {n_merged:,} in vault{extra}")
        vaulted.append(dst)

    if use_vault:
        print(f"refreshed vault copy: {len(vaulted)} file(s) -> {vault} "
              f"(git-crypt-encrypted, union-merged; commit to persist off-machine)")
    else:
        _vault_skipped("outlook-sensitive")

    dest = os.path.join(BACKUP_DIR, f"outlook-sensitive-{ts}.tgz")
    try:
        with tarfile.open(dest, "w:gz") as tf:
            for path in vaulted:
                tf.add(path, arcname=os.path.join("outlook-sensitive", os.path.basename(path)))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"backed up {len(vaulted)} projected outlook PHI file(s) -> {dest} "
          f"({os.path.getsize(dest):,} bytes)")


def main():
    root = ROOT
    if "--root" in sys.argv:
        root = os.path.abspath(sys.argv[sys.argv.index("--root") + 1])

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for queue in CHAT_QUEUES:
        backup_chat_queue(root, ts, queue)
    backup_outlook(root, ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
