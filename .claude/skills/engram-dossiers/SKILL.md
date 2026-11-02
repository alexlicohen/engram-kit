---
name: engram-dossiers
description: >-
  Reconcile drifted dossiers — fold notes ingested since each dossier's coverage date into the
  cross-cutting one-pagers (dossiers/*.md), via the guarded writer. Use when the owner says
  "/engram-dossiers", "reconcile the dossiers", "dossier freshness pass", when `/engram-brief` or a
  sync reports drifted dossiers, or after any sync that adds many notes to a dossier's domain.
  Deterministic detection (dossier_drift.py), LLM synthesis, guarded write
  (reconcile_dossier.py: validate → write → lint → rollback). Never wire into check.sh.
---

# engram-dossiers — dossier freshness pass

Dossiers are **evergreen syntheses** (`type: evergreen`): they hold *current truth* across many
event notes and decay every sync. This skill is the recurring reconcile, formalizing what was
once an ad-hoc manual pass. Run only inside the store repo with a clean tree.

## Flow

1. **Detect (deterministic):** `python3 scripts/dossier_drift.py --json` → `{drifted, clean_count,
   total}`; each drifted entry lists the dossier, its coverage date, and the newer-unlinked notes
   (association tiers: SLUG_DOSSIER match, else domain match — see the script docstring).
2. **Prioritize.** Reconcile the dossiers the owner will actually pull from next (active grants,
   a live project, open deals); a 1-2-note drift on a dormant dossier can wait. State the chosen subset.
3. **Per dossier (fan out ONE agent per dossier, ≤4 at a time; judgment work — don't over-shard):**
   - Read the dossier + EVERY unlinked note from the drift report (read the notes, not summaries —
     an audit agent's summary can be wrong; facts come from the notes, never from model memory).
   - Rewrite the BODY: fold new facts into the existing structure, update stale state (prefer
     marking superseded over deleting), keep curated prose/links, add `[[wikilinks]]` for every
     newly folded note (that's what clears the drift), refresh any "Current as of:" line.
   - The agent returns the draft body; it does NOT write the dossier.
4. **Write (guarded, orchestrator):** save each draft to a scratch file, then
   `python3 scripts/reconcile_dossier.py --path dossiers/<name>.md --new-body-file <scratch> \
     [--new-related "a,b,c"]`
   Frozen identity fields (title/domain/type/provenance.ref) are gate-checked and the write rolls
   back on lint failure; `related`/`supersedes` ARE mutable here (updating them is the job).
   `generated:` is stamped automatically. The script does NOT commit.
5. **Verify + commit:** review each diff (a reconcile must never silently drop curated content —
   check deletions are deliberate supersessions), then ONE `bash scripts/check.sh` (never per-agent;
   concurrent regens race), then commit. Re-run `dossier_drift.py` and report before/after counts.

## Guardrails

- **Instrument-adjacent, never a gate** — drift is advisory; check.sh must not depend on this.
- **Facts are source-anchored:** every new claim folded in must cite/link the note it came from.
  On conflict between a new note and dossier prose, the newer note usually wins — but check the
  note isn't itself flagged/stale (git-check before "fixing"; a re-audit can revert a verified fix).
- **Dossier edits are the SANCTIONED in-place rewrite** for `dossiers/` — but only through
  `reconcile_dossier.py` (or hand-edit + the dossier preserve gate). Never let a distiller or
  automated sync path write a dossier.
- Agents are read-only + draft-return; the ORCHESTRATOR owns writes, check.sh, and the commit.
- ONE writer at a time — don't run during a sync/distill wave.
- Push only when the owner asks (or per the session's standing instruction).
