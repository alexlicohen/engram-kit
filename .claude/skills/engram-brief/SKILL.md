---
name: engram-brief
description: >-
  Show the owner a pull-based deadline & maintenance brief from engram — the time-decaying knowledge the
  second brain holds but nothing else volunteers: near-term & overdue open loops (with days-remaining
  and owner), drifted dossiers, sync-watermark staleness, and a hygiene tail (unsorted / deferred-
  clarify / unpushed commits). Use when the owner says "/engram-brief", "what's due", "what needs my
  attention", "engram brief", "what am I forgetting", or naturally at the top of `/wrap-up` or
  `/engram-sync`. READ-ONLY, deterministic, <1s. No daemon, no schedule, no writes.
---

# engram-brief — the second brain reminds, not just recalls

engram already *holds* deadline-bearing knowledge (open loops with owner/due/status, watermark
staleness, dossier drift) — much of it minted by `/engram-capture` and reaching engram through no
connector. This surfaces it on demand. It is the digests layer made time-aware; it is **not**
ambient/spaced resurfacing (that stays deferred) and **not** scheduled — the owner (or a `/wrap-up`)
invokes it.

## Run it

```bash
python3 $ENGRAM_ROOT/scripts/engram_brief.py            # next 21 days
python3 $ENGRAM_ROOT/scripts/engram_brief.py --days 7   # tighter window
python3 $ENGRAM_ROOT/scripts/engram_brief.py --today 2026-07-10   # what-if
```

The composer is deterministic (no LLM, every line cited to its note/loop). It reads
`digests/open-loops.md` (due-dated, unresolved loops), `dossier_drift.py` (drifted dossiers),
`_manifest/sync-state.json` (per-source days-behind), and counts unsorted / deferred-clarify /
unpushed-commits. Print its output verbatim, then optionally offer to act on the top items.

## After showing it

- **Overdue loops** are the digest's view — some may be stale (done but not marked resolved in
  `open-loops.md`). A large overdue count is itself a signal that `open-loops.md` wants a reconcile.
  Offer to check/resolve specific ones against their source note; do NOT bulk-mark them resolved.
- **Drifted dossiers / stale watermarks** → offer `/engram-sync` (if watermarks lag) then a dossier
  reconcile; the brief is the trigger, the fix is those flows.
- **Never writes.** Any follow-up edit (resolving a loop, reconciling a dossier, a capture) goes
  through its own guarded path (`/engram-capture`, a dossier edit, `/engram-sync`) with the owner's OK.

## Notes

- Human-invoked only. If the owner ever wants it on a cadence (e.g. a Monday brief), their harness
  `schedule` / `/loop` can wrap this same command unchanged — build nothing new for that.
- Pairs with `/engram` (recall) and `/engram-capture` (write-back): recall answers what you ask,
  capture saves what you decide, brief tells you what's coming due.
