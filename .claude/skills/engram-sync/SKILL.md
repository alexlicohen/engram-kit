---
name: engram-sync
description: >-
  Run an incremental sync of the engram store — pull new items from each configured source (Granola
  meetings, Gmail via the connector, a secondary mailbox from an mbox export, Outlook work mail from an
  .eml export or Microsoft Graph, iMessage, WhatsApp) since the last watermark, distill them through the
  contract-guarded pipeline, verify, commit, then run the Reminders tasks pass. Use when the owner says
  "/engram-sync", "sync engram", "top up engram", "pull new meetings/emails/messages into engram", or
  after a stretch where connectors have new data. Claude-driven (connectors
  aren't cron-safe); ONE writer at a time. Authoritative detail lives in `_manifest/SYNC.md`.
---

# engram-sync — incremental top-up

Codifies the `_manifest/SYNC.md` runbook. Run only **inside** `$ENGRAM_ROOT` with a clean tree.
`SYNC.md` is the source of truth for connector queries and keep/drop rules; this skill is the
orchestration checklist so a sync is habitual and never done from memory.

## Before you start
- **ONE writer.** Only one sync at a time; check no other session is distilling into this store.
- Read `_manifest/sync-state.json`: the per-source watermarks are where you resume from. Sync only the
  sources that have a key there.
- Verify live which connectors this session has (Gmail, Granola); never assume. A missing connector
  skips that source (note it), it does not fail the sync.
- Chat sources need Full Disk Access for this terminal; Outlook needs fresh `.eml` files in
  `_sources/outlook/` (or a working Graph login). Prerequisites: `SYNC.md` › Prerequisites per source.

## The universal flow (every source)
Guards are Python (the Workflow runtime is pure JS). For each source `<S>`:
```
worklist        → emit the delta wave
wave_prepare.py --source <S> --mode incremental --wave <wave>   # (in-place passes: --mode backfill --allow-history)
resolve_wave.py --source <S>            # validates count+sha256+mode, atomically CONSUMES the contract
Workflow(<distiller>.mjs)               # a Setup agent reads the CONSUMED contract
sync_verify.py --source <S>             # incremental ⇒ all matched notes ADDED; backfill ⇒ modified expected
bump_watermark.py --source <S>          # from disk (max date written) — NEVER hand-set to today()
```
Re-run `wave_prepare.py` + `resolve_wave.py` fresh immediately before every distiller launch.

## Granola — new meetings
`SYNC.md` §"Granola (new meetings)": list meetings since the watermark → minus meetings already having a
`granola:<id>` note → wave → `granola_new.mjs` → **transcript-verify** (before `sync_verify`/check.sh/commit):
`scripts/granola_verify.py prepare --untracked` → `Workflow(_granola-pending/granola_verify.mjs)` →
`scripts/granola_verify.py check`. The distiller reads the Granola AI summary; the verifier fixes
owner/recipient/speaker/direction/decision/date claims the transcript contradicts, body only.

## Gmail — primary account
`SYNC.md` §Gmail: `search_threads(after:<wm>)` → wave of keep candidates → `get_thread(METADATA_ONLY)` →
`gmail_render.py` (metadata only) → **`gmail_update_worklist.py` now** (grown set, before any commit) →
`gmail_worklist.py` (dedup, auto-un-drop, PHI holds) → `gmail_distill.mjs` (workers fetch FULL_CONTENT
themselves) → `sync_verify` → `gmail_reconcile.py` → check.sh → watermark → commit. Then the grown-thread
pass: `gmail_update.mjs` as a backfill wave (`--allow-history`), no watermark bump.

## Gmail — secondary account (mbox)
`mbox_index.py` → `gmail_secondary_select.py` → `mbox_extract.py` → `gmail_secondary_worklist.py` →
copy the run file to a dated delta → `gmail_secondary_distill.mjs` → `sync_verify --source
gmail-secondary`. No grown-thread path.

## Outlook — work mail
`.eml` export (or `outlook_graph_export.py`) → `outlook_extract.py` → `outlook_select.py` → `phi_scan.py`
→ `outlook_worklist.py` (sticky PHI ledger; review it, release via `_sources/outlook-released.txt`) →
`backup_sensitive.py` → `outlook_distill.mjs` → `sync_verify --dropped <#markers>` → `phi_scan_store.py`
→ watermark → commit. PHI is fail-closed: never hand-release a hold without the owner.

## iMessage / WhatsApp
`imsg_select.py --since <wm>` → `imsg_extract.py --since <wm>` → `imsg_worklist.py` →
`backup_sensitive.py` → `imsg_distill.mjs` → `stamp_markers.py` → `sync_verify --dropped <#>`; WhatsApp:
`whatsapp_extract.py --since <wm>` (never without `--since` on an incremental) → `whatsapp_worklist.py`
→ `whatsapp_distill.mjs` → `stamp_markers.py --source whatsapp` (never bare). Review the sensitive queues
with the owner (`review_server.py`, loopback-only) before committing.

## After any sync
1. `python3 scripts/dossier_drift.py` — surfaces dossiers the new notes made stale.
2. `bash scripts/check.sh` — MUST end green. New notes = ADDED is fine; an in-place pass trips
   SUSPICIOUS by design — confirm it is only the intended notes.
3. **Post-sync fidelity spot-check** (the `/engram-audit` mechanism, scoped to just-written notes), after
   check.sh (it needs a fresh `index.jsonl`):
   - `git status --porcelain | awk '/^\?\?|^A /{print $2}' | grep '\.md$' | grep -vE '_index\.md|_manifest/|dossiers/|digests/' > /tmp/synced.txt`
     (or a `git diff --diff-filter=A <pre-sync-sha>..HEAD` listing after the commit).
   - `python3 scripts/audit_sample.py --only /tmp/synced.txt --n 15 --out /tmp/sync_audit.jsonl`.
   - One auditor per ≤5-note batch (the `/engram-audit` auditor template), ≤4 concurrent.
   - Run an adversarial **refute** pass over every FABRICATION candidate before fixing anything.
     Confirmed defects: surface to the owner with the note + source quote, fix body-only, then feed them
     to the corrections ledger: `python3 scripts/audit_to_corrections.py --in <verdicts.json> --date <YYYY-MM-DD>`.
     `resolve_wave.py` folds open corrections into the next wave's brief automatically.
4. Confirm the watermark moved in `sync-state.json`; commit. Push only when the owner asks. If drift
   was flagged, run **`/engram-dossiers`**.
5. **Tasks pass** (`/engram-tasks`): `engram_tasks.py reconcile` → `extract --only /tmp/synced.txt` →
   `push`; commit the ledger files. A bridge failure is noted, never blocking.

## Guardrails
- Distiller "done" self-reports are NOT trusted — done-ness is derived from disk (notes + markers), so an
  interrupted run just resumes. Check `total` vs the expected delta and `git status` after every wave.
- No clinical-patient PHI enters the store (`phi_scan` runs in check.sh).
- Touching history instead of the delta is the tell for a wave-threading bug — `sync_verify` catches it.
- `bump_watermark.py` before the sync commit; the trap list is `SYNC.md` › Traps — read it once per sync.
