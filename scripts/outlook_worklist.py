#!/usr/bin/env python3
"""Compute the PENDING Outlook distillation work-list (idempotent / resumable) AND write the
durable PHI-exclusion ledger.

  pending = KEEP threads
            − already-distilled (a live note carries provenance ref `outlook:thread:<thrid>`)
            − PHI-HELD  (phi report level high|medium  OR  the KEEP record's phi_hold_subject
                         OR  EVER held before — see STICKY below)
            − previously-dropped (a `_sources/outlook-drops.d/<thrid>.json` marker exists)
            − deterministically drop-ruled (the KEEP record's `drop_rule`, e.g.
              `clinical-task-nurse`) — this step writes the drop marker

"Done" is derived from the notes themselves (like every engram worklist), so an interrupted run is
resumed just by regenerating the work-list — no workflow-internal state.

PHI ledger (fail-closed; the owner 2026-07-06): every HELD thread is written to
`_sources/outlook-phi-excluded.jsonl` — the durable record of which threads must NEVER be
distilled. That file (+ `_sources/outlook-released.txt`) IS the review surface the owner walks to release
false-positives. FAIL-CLOSED: if `--phi-report` is set but the file is absent (the phi_scan step was
skipped), this aborts rather than proceeding with the scanner layer silently dropped.

STICKY-ONCE-HELD. **The ledger is the ONE owner of "this thread was ever held."**
It used to be rebuilt from scratch out of the CURRENT scan every run, so a hold could evaporate:
a thread about a person's appointments was phi-high, then GREW 2->4 msgs; the extra administrative text diluted the scanner's density score to `low`, the rebuilt
ledger lost the row, and the thread re-entered the pending wave. The scanner level is a property of
the CURRENT text and is therefore not a safe carrier of a past decision.
So: the prior ledger is READ before it is rewritten, every prior thrid is held again regardless of
what the current scan says, and a row that no longer recomputes is CARRIED FORWARD (`sticky: true`)
instead of disappearing. The set is seeded from durable `phi*`-reason drop markers too, so a hold
recorded by the distiller is not lost either. The ONLY exit is `_sources/outlook-released.txt`:
a released thrid is dropped from the ledger and force-included, exactly as before.
Corollary: this file is now STATE, not a pure derivative — losing it loses the "ever held" set (it
would silently re-derive from the current scan only), and like the iMessage/WhatsApp exclusion
ledgers it lives under the gitignored `_sources/`, so nothing in git holds it. `python3
scripts/backup_sensitive.py` is what makes it durable: it union-merges this file (keyed on `thrid`)
into `_vault/outlook-sensitive/phi-excluded.jsonl` + `released.txt`, which git-crypt encrypts into
the repo, and tars that vault copy to `~/.engram-backups/outlook-sensitive-<UTC-ts>.tgz` —
COMMIT the vault files. Run it after this script, in the Outlook pass (SYNC.md step 4).

The vault copy is a PROJECTION, not a copy: only `thrid`/`reason`/`first_reason`/`level`/`sticky`/
`carried_forward` leave `_sources/` (`backup_sensitive.LEDGER_VAULT_FIELDS` is the single owner of
that list, a CLOSED allowlist so a field added to a row here defaults to excluded). The rows this
script writes carry `subject` + `participants`, and for a `bare-name-subject`/`mrn-subject` hold the
subject IS the patient's name — that must not enter git, encrypted or not. The projection keeps
exactly what a restore reads: `prior_ledger` needs `thrid` (it is the whole "ever held" set and the
only required field), and the sticky block below reads only `reason` and `first_reason` off a prior
row. If you ever make the sticky logic depend on a further prior-row field, add THAT field to the
allowlist — never `subject` or `participants`.

Restoring on a fresh clone is manual: `git-crypt unlock`, then copy
`_vault/outlook-sensitive/phi-excluded.jsonl` back to `_sources/` BEFORE the first sync — unlike
`excluded_chat_guids()` on the iMessage side, nothing here reads the vault copy on its own. The
restored ledger holds every thrid but no subjects, which is all the gate needs; the human-readable
review surface stays the local file.

    python3 scripts/outlook_worklist.py [--keep _sources/outlook-keep.jsonl]
        [--phi-report _sources/outlook-phi-report.jsonl] [--out _sources/outlook-run.jsonl]
        [--ledger _sources/outlook-phi-excluded.jsonl]
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS                       # noqa: E402
from _lib.provenance import parse_ref, thread_id         # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEME = "outlook:thread:"


def done_thrids(root):
    """Every outlook thrid already distilled on disk — a LIVE note whose provenance ref is
    `outlook:thread:<thrid>`. Keyed off the ref inside the note (not the filename), matching
    gmail_secondary_worklist: the distiller can emit a garbled filename suffix, but the ref is authoritative."""
    done = set()
    for dom in DOMAINS:
        for fp in glob.glob(os.path.join(root, dom, "**", "*.md"), recursive=True):
            if os.path.basename(fp) == "_index.md":
                continue
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(1500)
            except OSError:
                continue
            tid = thread_id(parse_ref(head), scheme=SCHEME)
            if tid:
                done.add(tid)
    return done


def phi_held(phi_path):
    """thrid -> {level, strong} for every thread the scanner flagged at level high|medium.

    FAIL-CLOSED (a code-review finding): if `phi_path` is given but the file does not exist,
    ABORT — proceeding would silently drop the scanner layer (which catches content-dense patient
    narratives the deterministic subject/Epic/MRN holds miss), leaving only the distiller as backstop
    for a pipeline that advertises fail-closed PHI. Run `phi_scan.py` before this worklist."""
    if phi_path and not os.path.exists(phi_path):
        sys.exit(f"FAIL (fail-closed PHI): --phi-report {phi_path} does not exist — run "
                 f"`python3 scripts/phi_scan.py _sources/outlook-keep.jsonl _sources/outlook-threads "
                 f"{phi_path}` FIRST. Refusing to compute a pending wave without the PHI scanner "
                 f"layer (it would silently omit content-dense patient threads from the hold set).")
    held = {}
    if phi_path:
        for line in open(phi_path, encoding="utf-8"):
            r = json.loads(line)
            if r.get("phi") and r.get("level") in ("high", "medium"):
                held[r["thrid"]] = {"level": r["level"], "strong": r.get("strong", []),
                                    "subject": r.get("subject")}
    return held


DROPS_D = os.path.join("_sources", "outlook-drops.d")


def drop_markers(root):
    """thrid -> marker dict (possibly {}) for every `_sources/outlook-drops.d/<thrid>.json`."""
    d = os.path.join(root, DROPS_D)
    out = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                rec = {}
            out[fn[:-5]] = rec if isinstance(rec, dict) else {}
    return out


def write_drop_marker(root, thrid, reason, note=None):
    """Write the content-free `_sources/outlook-drops.d/<thrid>.json` marker for a deterministic
    drop. NEVER overwrites an existing marker — a hand-written one (the owner's note, or the distiller's
    own reason) is the more informative record and must survive a re-run. Returns True if written."""
    d = os.path.join(root, DROPS_D)
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f"{thrid}.json")
    if os.path.exists(fp):
        return False
    rec = {"thrid": thrid, "reason": reason}
    if note:
        rec["note"] = note
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def prior_ledger(path):
    """thrid -> the row as last written. The ONE owner of "this thread was EVER held" (see the
    STICKY note at the top). Read BEFORE the ledger is rewritten.

    FAIL-CLOSED on a malformed line rather than skipping it. Skipping is the failure this whole rule
    exists to prevent, just one level down: an unreadable row is a hold silently deleted, and the
    thread walks straight back into the next wave. Since STICKY made this file STATE, a line can only
    be malformed by hand-editing or disk corruption (the rewrite below is atomic, so a crash can no
    longer truncate it) — both of which a human should see, not a wave."""
    out = {}
    if path and os.path.exists(path):
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as e:
                sys.exit(f"FAIL (fail-closed PHI): {path}:{lineno} is not valid JSON ({e}). This "
                         f"file is the ONE owner of the 'ever held' set — refusing to run, because "
                         f"skipping the line would silently release whatever thread it held. Repair "
                         f"the line (git-less: the row's `thrid` is the only required field) and "
                         f"re-run.")
            if not isinstance(row, dict) or not row.get("thrid"):
                sys.exit(f"FAIL (fail-closed PHI): {path}:{lineno} has no `thrid` — refusing to "
                         f"run rather than silently dropping a hold.")
            out[row["thrid"]] = row
    return out


def write_ledger(path, rows):
    """Rewrite the PHI-exclusion ledger ATOMICALLY (tmp file in the same dir + os.replace).

    STICKY turned this file from a pure derivative into STATE: a crash part-way through a plain
    truncate-and-write would leave a SHORT ledger, and every hold below the cut would be gone for
    good — exactly the leak this rule closes, re-introduced by the write path."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as lg:
        for row in rows:
            lg.write(json.dumps(row, ensure_ascii=False) + "\n")
        lg.flush()
        os.fsync(lg.fileno())
    os.replace(tmp, path)


# `phi`, `phi-high`, `phi-manual`, ... — anchored so it cannot swallow an unrelated reason that
# merely starts with those letters (a live marker reads "phishing/scam email").
PHI_REASON_RX = re.compile(r"^phi(-|$)", re.I)


def phi_marker_thrids(markers):
    """thrids whose drop marker records a PHI reason. These are durable evidence of a past hold, so
    they seed the sticky set — otherwise a hold the distiller made survives only as long as nobody
    rebuilds the ledger from a diluted scan."""
    return {t for t, rec in markers.items()
            if PHI_REASON_RX.match(str(rec.get("reason") or ""))}


def released_thrids(path):
    """Thrids the owner has explicitly RELEASED from the PHI hold after review (durable + idempotent, so
    a re-run keeps releasing them). One thrid per line; '#' starts a comment. These are force-included
    in the pending wave and excluded from the ledger — the "release-the-false-positives" step of the
    exclude+review posture. True patient records are never listed here."""
    out = set()
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line)
    return out


def main():
    ap = argparse.ArgumentParser(description="Outlook pending work-list + PHI-exclusion ledger.")
    ap.add_argument("--keep", default=os.path.join(ROOT, "_sources", "outlook-keep.jsonl"))
    ap.add_argument("--phi-report", default=os.path.join(ROOT, "_sources", "outlook-phi-report.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "_sources", "outlook-run.jsonl"))
    ap.add_argument("--ledger", default=os.path.join(ROOT, "_sources", "outlook-phi-excluded.jsonl"))
    ap.add_argument("--released", default=os.path.join(ROOT, "_sources", "outlook-released.txt"),
                    help="thrids the owner reviewed + RELEASED from the PHI hold (force-include)")
    ap.add_argument("--root", default=ROOT,
                    help="store root (notes + _sources); overridable for tests")
    args = ap.parse_args()

    root = args.root
    held = phi_held(args.phi_report)
    released = released_thrids(args.released)
    done = done_thrids(root)
    markers = drop_markers(root)

    # --- the sticky set: EVERY thread ever held, minus the ones the owner released ---------------------
    prior = prior_ledger(args.ledger)
    ever_held = (set(prior) | phi_marker_thrids(markers)) - released

    total = already = dropped = n_hold_scan = n_hold_subj = written = n_released = 0
    n_sticky = n_drop_rule = n_marker_written = 0
    warnings = []
    ledger_rows = []
    with open(args.out, "w", encoding="utf-8") as out:
        for line in open(args.keep, encoding="utf-8"):
            r = json.loads(line)
            t = r["thrid"]
            total += 1
            hs = held.get(t)
            subj_hold = bool(r.get("phi_hold_subject"))
            sticky = t in ever_held
            drop_rule = r.get("drop_rule")
            if t in done:
                already += 1
                if (hs or subj_hold or sticky or drop_rule) and t not in released:
                    # a note exists for a thread the rules now say must never be distilled.
                    warnings.append(f"  {t}  distilled-but-now-{drop_rule or 'held'}: "
                                    f"{(r.get('subject') or '')[:60]!r}")
                continue
            if t in markers:
                dropped += 1
                continue
            # A deterministic drop RULE outranks the PHI hold: it is the owner's "never enters engram"
            # disposition, and routing 70+ nurse threads through the PHI ledger would bury the
            # release-review surface that ledger exists to be. Both outcomes are never-distilled.
            # `released` outranks the drop RULE in turn: outlook-released.txt is documented as the
            # force-include switch, and without this the release would defeat itself after one run
            # — the rule would write a drop marker, and the marker check above (which release does
            # NOT override, since a marker is the distiller's separate not-durable verdict) would
            # drop the thread forever. No live thrid hits this today; it is a latent trap.
            if drop_rule and t not in released:
                dropped += 1
                n_drop_rule += 1
                if write_drop_marker(root, t, drop_rule):
                    n_marker_written += 1
                continue
            # PHI hold: scanner flag (high|medium) OR the deterministic select-layer hold
            # (roster / Epic-sender / Epic-message / MRN / bare-name subject) OR EVER-HELD (sticky).
            # An owner-RELEASED thrid overrides all three.
            if (hs or subj_hold or sticky) and t not in released:
                # reason prefers the deterministic select-layer reason (more specific) when present,
                # then the current scanner level, then whatever the hold was recorded as last time.
                reason = ((r.get("phi_hold_reason") if subj_hold else None)
                          or ("phi-%s" % hs["level"] if hs else None)
                          or (prior.get(t, {}).get("reason") or "sticky"))
                row = {
                    "thrid": t, "reason": reason,
                    "level": hs["level"] if hs else "subject" if subj_hold else "sticky",
                    "strong": hs["strong"] if hs else [],
                    "phi_hold_subject": subj_hold,
                    "phi_hold_reason": r.get("phi_hold_reason"),
                    "domain": r.get("domain"), "subject": r.get("subject"),
                    "n_msgs": r.get("n_msgs"), "participants": r.get("participants", [])[:8],
                }
                if sticky:
                    row["first_reason"] = prior.get(t, {}).get("first_reason") or \
                        prior.get(t, {}).get("reason") or reason
                    if not (hs or subj_hold):
                        # held ONLY because it was held before — the class that was leaking.
                        # Flagged narrowly so `grep '"sticky": true'` finds exactly those rows
                        # (and the carried-forward ones) rather than every row in the ledger.
                        row["sticky"] = True
                ledger_rows.append(row)
                if not (hs or subj_hold):
                    n_sticky += 1          # held ONLY because it was held before (level diluted)
                elif subj_hold:
                    n_hold_subj += 1
                else:
                    n_hold_scan += 1
                continue
            if t in released and (hs or subj_hold or sticky):
                n_released += 1   # was PHI-held, force-included by the owner's review
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            written += 1

    # --- carry forward every prior hold the current pass did not re-emit -------------------------
    # (the thread is now done/dropped, fell out of KEEP, or became bulk). Dropping the row here is
    # what made the hold evaporate in the first place — the ledger must not forget.
    seen = {row["thrid"] for row in ledger_rows}
    n_carried = 0
    for t in sorted(ever_held - seen):
        row = dict(prior.get(t) or {"thrid": t,
                                    "reason": markers.get(t, {}).get("reason") or "phi",
                                    "level": "sticky"})
        row["thrid"] = t
        row["sticky"] = True
        row.setdefault("first_reason", row.get("reason"))
        row["carried_forward"] = True
        ledger_rows.append(row)
        n_carried += 1

    write_ledger(args.ledger, sorted(ledger_rows, key=lambda x: (x.get("reason") != "phi-high",
                                                                 str(x.get("reason")), x["thrid"])))

    n_held = n_hold_scan + n_hold_subj + n_sticky
    print(f"KEEP={total:,}  already-done={already:,}  prev-dropped={dropped:,} "
          f"(drop_rule={n_drop_rule:,}, markers written={n_marker_written:,})  "
          f"PHI-HELD={n_held:,} (scanner={n_hold_scan:,}, subject-hold={n_hold_subj:,}, "
          f"sticky-only={n_sticky:,})  released={n_released:,}  -> pending={written:,}")
    print(f"pending -> {os.path.relpath(args.out, root)}")
    print(f"PHI-exclusion ledger ({len(ledger_rows):,} threads, NEVER distilled; "
          f"{n_carried:,} carried forward) -> {os.path.relpath(args.ledger, root)}")
    if warnings:
        print(f"WARN: {len(warnings):,} thread(s) have a distilled note but would now be "
              f"held/dropped — review:")
        for w in warnings[:20]:
            print(w)


if __name__ == "__main__":
    main()
