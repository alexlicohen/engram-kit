# engram — incremental sync (connector-based top-up)

How to pull NEW source items since the last ingest without re-running a full backfill. State (one
watermark per source) lives in `_manifest/sync-state.json`. Sources shipped: **Granola** meetings (the
exemplar), **Gmail** (primary account via the connector; a secondary account via mbox), **Outlook** work
mail, **iMessage** and **WhatsApp**; plus the **Apple Reminders** task bridge that runs after a sync.
Every source runs the same contract-guarded pipeline; "Adding a source" at the end says what a new one
needs. Run only the sources you use.

> **Post-sync fidelity spot-check:** after every sync, audit the just-written notes against their
> sources (the `/engram-audit` mechanism, scoped via `audit_sample.py --only`). That routine is what
> proves the distiller FIDELITY RULES hold on new notes. Steps: the `/engram-sync` skill's
> "After any sync".

**Why this is a Claude-Code-driven process, not a cron daemon:** MCP connectors (Granola, Gmail, …) are
callable only by Claude (the orchestrator or a workflow agent), not by plain shell scripts, and they may
be absent in headless runs. So "sync" = ask Claude to run it. The scripts and distillers below do the
heavy lifting; Claude does the connector discovery and the watermark bump.

## The universal flow (every source `<S>`)

```
worklist          -> emit the delta wave (one JSON line per source item)
wave_prepare.py --source <S> --mode incremental --wave <wave>    # writes _sources/<S>.contract.json
resolve_wave.py --source <S>      # validates count + sha256 + mode, atomically CONSUMES the contract,
                                  # regenerates the corrections brief and records its sha256
Workflow(<distiller>.mjs)         # a Setup agent reads the CONSUMED contract — never Workflow args
sync_verify.py --source <S> [--dropped N]   # on-disk note delta == expected - dropped; mode-aware
bash scripts/check.sh             # regenerate derived views + lint + gates
bump_watermark.py --source <S>    # from the max note date actually written — never today()
commit
```

- **The contract is one-shot.** `resolve_wave.py` renames `<S>.contract.json` →
  `<S>.contract.consumed.json` before the distiller runs, so a retry, double launch or resumed session
  finds no live contract and aborts. To resume an interrupted run, re-run `wave_prepare.py` on the same
  wave: the distiller's done-markers make the re-run skip completed items.
- **`sync_verify.py` is mode-aware.** `incremental` ⇒ every matched note must be git-ADDED (a modified
  note means history was re-distilled). `backfill` ⇒ modified is expected; count-only assertion.
- **Backfill is gated.** `wave_prepare.py --mode backfill` needs `--allow-history`, recorded on disk in
  the contract; `resolve_wave.py` refuses a backfill contract without it.
- Never put filesystem access or guards in a distiller `.mjs` (the Workflow runtime has none). Guards
  live in Python.

## Prerequisites per source

| Source | Needs | Watermark key (`sync-state.json`) | Regime |
|---|---|---|---|
| Granola | the Claude **Granola** connector | `granola` | day |
| Gmail (primary) | the Claude **Gmail** connector, signed in to that account | `gmail_personal` | day |
| Gmail (secondary) | a Google Takeout **mbox** of the second account (the connector reaches one account) | `gmail_secondary` | day |
| Outlook (work) | a manual **`.eml` export** into `_sources/outlook/`, or an Entra/Azure **app registration** for Microsoft Graph (`ENGRAM_GRAPH_CLIENT_ID`, optional `ENGRAM_GRAPH_TENANT`) | `outlook` | day (backlog) |
| iMessage | macOS **Full Disk Access** for the app running the sync (terminal/editor) — `~/Library/Messages/chat.db` | `imessage` | timestamp |
| WhatsApp | the native Mac WhatsApp app (its `ChatStorage.sqlite`; Full Disk Access as above) | `whatsapp` | timestamp |
| Reminders (tasks) | macOS **Automation** permission for Reminders (`engram_tasks.py doctor`) | — | — |

Connectors are used **inside Workflows** by the distiller's worker agents (Gmail `get_thread`, Granola
`get_meetings`), so they must be available to subagents in your Claude Code setup. The distillers launch
workers as Claude Code's stock `general-purpose` subagent (each distiller's `AGENT_TYPE` constant,
documented in its header). To pin tools or a model, define your own subagent with the connector tools
and pass its name as `agentType` in the workflow args.

Per-source configuration (all committed, all owner-specific, all shipped empty or generic):

- `_manifest/people.md` `self` row — your name, spoken variants **and every e-mail address you send
  from**. Every mail source's "sent by the owner" test reads it (`_lib.actions.self_emails`).
- `_manifest/constants/mail_routing.json` — PHI-screen sender domains (the clinical institutions you
  work at or hear from), clinic task senders whose mail is a deterministic drop, and the ordered
  header-only domain rules. See `scripts/_lib/mail_routing.py`.
- `_manifest/constants/reminders.json` — which Reminders list each domain's tasks land in.
- Environment only, never committed: `ENGRAM_GRAPH_CLIENT_ID` / `ENGRAM_GRAPH_TENANT` (Graph),
  `ENGRAM_OUTLOOK_ACCOUNT` (legacy local Outlook cache export).

## Granola (new meetings)

1. Read `sync-state.json` → `granola.watermark_date`.
2. Claude calls the Granola connector's meeting list for `<watermark>..<today>`.
3. Drop any meeting whose `granola:<id>` note already exists:
   `grep -rl "granola:<id>" --include='*.md' . | grep -v _archive` → if hit, skip.
4. Write the remaining `{id,title,date}` lines to `_sources/granola-new.jsonl`.
5. `python3 scripts/wave_prepare.py --source granola-new --mode incremental --wave _sources/granola-new.jsonl`
   → `python3 scripts/resolve_wave.py --source granola-new` →
   `Workflow({scriptPath:"_granola-pending/granola_new.mjs"})`. Fresh notes are domain-routed, carry a
   `-g<id8>` filename suffix and the full roster. Resumable via `_sources/granola-new-done.d/`.
   (`python3 scripts/corrections_brief.py --source granola` prints the corrections brief by hand; it is
   optional, since `resolve_wave.py` regenerates it and pins its sha256 in the consumed contract.)
6. **Transcript-verify the wave's new notes.** The distiller reads the Granola AI SUMMARY, which is the
   source of most Granola defects (owner/recipient, speaker, direction, proposal-as-decision, computed
   dates). The transcript decides:
   `python3 scripts/granola_verify.py prepare --untracked` (one `{id,path}` per new granola note +
   snapshots; refuses already-committed notes) →
   `Workflow({scriptPath:"_granola-pending/granola_verify.mjs"})` (one verifier per ≤3 notes, ≤4
   concurrent; BODY-only edits, each logged with its transcript quote) →
   `python3 scripts/granola_verify.py check` (exit 1 if a result is missing, frontmatter changed, or an
   edit is unlogged). Review the `unverifiable` lines. Then
   `python3 scripts/sync_verify.py --source granola-new --dropped <#dropped>`, `bash scripts/check.sh`,
   harvest `new_people` into `_manifest/people.md` (`verify: true`),
   `python3 scripts/bump_watermark.py --source granola-new`, commit.

### Transcript-verify on COMMITTED notes — propose only

Edit mode refuses committed notes (it could revert a verified hand fix). For back-catalog notes, on a
clean tree:
`python3 scripts/granola_verify.py prepare --propose --since <YYYY-MM-DD>` →
`Workflow({scriptPath:"_granola-pending/granola_verify.mjs", args:{list:"_sources/granola-verify-propose.jsonl", results:"_sources/granola-verify-propose.d"}})`
→ `python3 scripts/granola_verify.py check --propose` (fails if any note changed) →
`python3 scripts/granola_verify.py review` (a numbered proposals file for the owner) → the owner picks →
`python3 scripts/granola_verify.py apply --ids 1,4,7` (exact-substring, body-only, all-or-nothing) →
`bash scripts/check.sh` → commit.

### Re-distill existing Granola notes (when summaries are regenerated)

`python3 scripts/granola_redistill_worklist.py` → `python3 scripts/wave_prepare.py --source granola-redistill
--mode backfill --wave _sources/granola-redistill.jsonl --allow-history` →
`python3 scripts/resolve_wave.py --source granola-redistill` →
`Workflow({scriptPath:"_granola-pending/granola_redistill.mjs"})` →
`python3 scripts/sync_verify.py --source granola-redistill` (in-place rewrites show as modified notes).
Bodies are rewritten from the current summaries; curated frontmatter (`related`, `supersedes`, `domain`,
`aliases`) is preserved.

### Speech-to-text glossary

`python3 scripts/granola_glossary.py` regenerates the KNOWN NAMES block of
`_granola-pending/granola-template-v2.md` from `_manifest/people.md` + `index.jsonl` (`--check` exits 1
when stale). Paste the block into your Granola template after any registry change.

## Gmail — primary account (Claude Gmail connector)

Keep scope: "everything I replied to" — a thread with at least one message from one of your own
addresses (or labelled Sent), minus Spam/Trash. The distiller still drops non-durable threads.

1. Read `gmail_personal.watermark_date`.
2. Claude calls the Gmail connector's `search_threads(query:"after:<YYYY/MM/DD> -in:chats")`,
   paginating; drop no-reply/notification/list/receipt/calendar-only threads, keep substantive human
   threads. Record EVERY keep candidate in `_sources/gmail-wave.jsonl` (`{thrid, subject, domain, date}`,
   domain from `gmail_select.provisional_domain`), including ones that already have a drop marker: the
   un-drop check needs their current render.
3. For each candidate fetch `get_thread(<id>, METADATA_ONLY)` and pipe the JSON through
   `python3 scripts/gmail_render.py <resp.json>` → `_sources/gmail-threads/<thrid>.txt`. Renders are
   **metadata for the Python steps only** (dates, senders); the distiller never takes a fact from them.
   `gmail_render.py` refuses (exit 2, old render kept) a thread that is not a verbatim connector dump
   (missing `historyId`, `date` ≠ `internalDate`) or is truncated.
4. **Capture the grown-thread set NOW**: `python3 scripts/gmail_update_worklist.py` (see below) —
   before the new notes are committed.
5. `python3 scripts/gmail_worklist.py _sources/gmail-wave.jsonl _sources/gmail-pending.jsonl
   [--phi-report …] [--drops …]` — pending = wave − already distilled (base-agnostic thread id:
   connector hex == mbox decimal) − drops − PHI holds. It **auto-un-drops** a dropped thread you replied
   to after the drop (marker archived to `gmail-drops.d/reopened/`, never deleted); growth without your
   reply stays dropped (`grown-but-still-dropped`).
6. `python3 scripts/wave_prepare.py --source gmail --mode incremental --wave _sources/gmail-pending.jsonl`
   → `python3 scripts/resolve_wave.py --source gmail` →
   `Workflow({scriptPath:"_gmail-pending/gmail_distill.mjs", args:{today:"<YYYY-MM-DD>"}})`. Workers call
   `get_thread(FULL_CONTENT)` fresh and distill from that response; a failed fetch returns
   `status:"error"` (no note, no marker, still pending — re-run the worklist, never hand-distill from the
   render). Drops write `_sources/gmail-drops.d/<thrid>.json` with `dropped_at`.
7. `python3 scripts/sync_verify.py --source gmail --dropped <#dropped>` →
   `python3 scripts/gmail_reconcile.py` (vocab/link/dup-thread sweep; `--dry-run` first) →
   `bash scripts/check.sh` → `python3 scripts/bump_watermark.py --source gmail` → commit.

### Gmail — grown threads (redistill in place)

An incremental search returns a thread only when it has post-watermark activity, so a wave thread that
is already in the store GREW. `python3 scripts/gmail_update_worklist.py` (same wave) emits
`_sources/gmail-update.jsonl` (`{thrid, key, fp, title, date}`), judging growth against the note's own
`date:`/`updated:` (never the watermark, which the main sync has already moved), and re-emits a thread
whose done-marker is stale (`stale-marker`). After the new-note commit, on a clean tree:
`wave_prepare.py --source gmail-update --mode backfill --allow-history --wave _sources/gmail-update.jsonl`
→ `resolve_wave.py --source gmail-update` → `Workflow({scriptPath:"_gmail-pending/gmail_update.mjs",
args:{today:"<YYYY-MM-DD>"}})` (body rewritten from a fresh `get_thread`, frontmatter preserved except
`updated:` and appended participants) → `sync_verify.py --source gmail-update` (modified notes
expected) → `check.sh` (SUSPICIOUS on exactly these notes is by design: confirm the set) → commit. No
watermark bump.

## Gmail — secondary account (mbox / Takeout)

For a second mailbox the connector cannot reach (e.g. a dedicated account for a company you run). Keep
scope: every thread with at least one human message, replied to or not; fully automated threads drop.
Configure `ACCOUNT_DESC` / `PRIOR_DOMAIN` in `_gmail-pending/gmail_secondary_distill.mjs` (or pass
`args.accountDesc` / `args.priorDomain`).

1. Export the account with Google Takeout (Mail, mbox) into `_sources/` (gitignored).
2. `python3 scripts/mbox_index.py <mbox> _sources/gmail-secondary-index.jsonl` (streams headers +
   byte offsets; memory-safe on multi-GB files) →
   `python3 scripts/gmail_secondary_select.py _sources/gmail-secondary-index.jsonl _sources/gmail-secondary-keep.jsonl`
   → `python3 scripts/mbox_extract.py <mbox> _sources/gmail-secondary-keep.jsonl _sources/gmail-secondary-threads`.
   (`mbox_triage.py` is a one-off sizing aid for a first backfill: keep/review/drop counts per domain.)
3. `python3 scripts/gmail_secondary_worklist.py` → `_sources/gmail-secondary-run.jsonl` = keep − done
   (by provenance ref) − drops − cross-account duplicates (a thread whose Message-IDs are ≥50% in the
   primary pass's kept threads; recorded as a `dup:personal-account` drop marker).
4. **Copy the run file to a dated delta** (`_sources/gmail-secondary-<date>.jsonl`):
   `wave_prepare.py --mode incremental` refuses the backfill basename `gmail-secondary-run.jsonl` by
   design. Then `wave_prepare.py --source gmail-secondary --mode incremental --wave <delta>` →
   `resolve_wave.py --source gmail-secondary` → `Workflow({scriptPath:"_gmail-pending/gmail_secondary_distill.mjs",
   args:{today:"<YYYY-MM-DD>"}})`. Notes end in `-sec<last 8 of thrid>.md`, which is how
   `sync_verify` tells them from primary-account notes that share the `gmail:thread:` scheme.
5. `sync_verify.py --source gmail-secondary --dropped <#>` → `check.sh` →
   `bump_watermark.py --source gmail-secondary` → commit.

Known gap: this pipeline has no grown-thread path — a done thread that grows is not redistilled.

## Outlook — work mailbox (`.eml` export or Microsoft Graph)

PHI is **fail-closed** here: every thread is scanned, holds are sticky, and the distiller carries a
strengthened PHI block (`CLINICAL_EMPLOYER` in `_outlook-pending/outlook_distill.mjs`).

**Step 0 — get `.eml` files into `_sources/outlook/`.** Any of:
- manual export ("Save as" `.eml`) of the messages since `outlook.watermark_date` — always works;
  overlap is harmless (messages dedupe by Message-ID, threads by conversation id);
- `python3 scripts/outlook_graph_auth.py login` once, then `python3 scripts/outlook_graph_export.py
  [--since …]` (device-code OAuth, token cached `0600` in your home dir, never in the repo). Needs
  `ENGRAM_GRAPH_CLIENT_ID` (a public-client app registration with delegated `Mail.Read`) and, for a
  single tenant, `ENGRAM_GRAPH_TENANT`. An employer's Conditional Access policy can block it; then use
  the manual export;
- `python3 scripts/outlook_local_export.py --account-email <you@work>` reads a **legacy** Outlook for Mac
  profile's local `.mime` cache. New Outlook does not write that cache: "no MimeFiles dir found" there
  is not a sync gap.

1. `python3 scripts/outlook_extract.py` → `_sources/outlook-index.jsonl` (one row per message; "sent"
   = From is one of your self-row addresses).
2. `python3 scripts/outlook_select.py` → threads by Exchange Thread-Index GUID, then
   References/In-Reply-To (never subject-only); stable 12-hex thrid; bulk dropped unless you wrote in
   the thread; provisional domain from `mail_routing.json`; PHI screen (`phi_risk` = a participant at a
   `phi_screen_sender_domains` domain) and deterministic holds (roster/hand-off subjects, EHR senders and
   subjects, an MRN-shaped number, a bare all-lowercase person-name subject from a clinical sender);
   the non-PHI `clinical-task-nurse` drop rule for `clinical_task_senders`. Renders
   `_sources/outlook-threads/<thrid>.txt`.
3. `python3 scripts/phi_scan.py _sources/outlook-keep.jsonl _sources/outlook-threads _sources/outlook-phi-report.jsonl`.
4. `python3 scripts/outlook_worklist.py` → `_sources/outlook-run.jsonl` = keep − distilled − held −
   dropped − drop-ruled, and rewrites the **PHI-exclusion ledger** `_sources/outlook-phi-excluded.jsonl`.
   The ledger is STATE: it is read before it is rewritten, so a held thread that grows and dilutes its
   scanner score stays held; `_sources/outlook-released.txt` (one thrid per line, `# why`) is the only
   exit; a malformed line aborts the run. Review the ledger: that is where false positives get released.
   Then `python3 scripts/backup_sensitive.py` (see below).
5. `wave_prepare.py --source outlook --mode incremental --wave _sources/outlook-run.jsonl` →
   `resolve_wave.py --source outlook` →
   `Workflow({scriptPath:"_outlook-pending/outlook_distill.mjs", args:{today:"<YYYY-MM-DD>"}})`.
6. `sync_verify.py --source outlook --dropped <#drop markers this wave>` → `check.sh` →
   `python3 scripts/phi_scan_store.py` on the new notes → `bump_watermark.py --source outlook` → commit.

## iMessage (`~/Library/Messages/chat.db`)

Local SQLite, read-only, no connector. Watermark is a TIMESTAMP (ISO-8601 UTC).
1. `python3 scripts/imsg_select.py --since <watermark>` → `_sources/imessage-incr-keep.jsonl` (chats with
   new messages, minus short-code / sender-ID robots, minus chats in the exclusion ledger).
2. `python3 scripts/imsg_extract.py --since <watermark>` → chunks in `_sources/imessage-incr/`,
   `_sources/imessage-incr-run.jsonl`, and `_sources/imessage.maxts` (the instant the next watermark
   comes from). Incremental chunk ids are namespaced by the run (`<chat>_incr<ts>_c<n>`).
3. `python3 scripts/imsg_worklist.py _sources/imessage-incr-run.jsonl` → `_sources/imessage-incr-pending.jsonl`
   (drops chunks that already have a done-marker).
4. `python3 scripts/backup_sensitive.py` BEFORE distilling (snapshots the sensitive queue).
5. `wave_prepare.py --source imessage --mode incremental --wave _sources/imessage-incr-pending.jsonl` →
   `resolve_wave.py --source imessage` → `Workflow({scriptPath:"_imessage-pending/imsg_distill.mjs",
   args:{today:"<YYYY-MM-DD>"}})`. Highly sensitive items (mental health, intimate, privileged, a
   third party's private matter) are written to `_sources/imsg-sensitive/`, not the store.
6. `python3 scripts/stamp_markers.py` (verifies each chunk's RESULT file → writes done-markers; bare =
   iMessage) → `sync_verify.py --source imessage --dropped <#chunks with no note>` → `check.sh` →
   review the sensitive queue (`python3 scripts/review_server.py`) → `bump_watermark.py --source imessage`
   → commit.

Over-held queue? `_imessage-pending/imsg_reclassify.mjs` (source `imsg-reclass`, backfill mode)
re-judges held notes and releases ordinary family/personal context back to the store.

## WhatsApp (native Mac app, `ChatStorage.sqlite`)

The iMessage twin; it reuses the same chunking, worklist and marker primitives.
1. `python3 scripts/whatsapp_extract.py --since <watermark>` → `_sources/whatsapp-incr/`,
   `_sources/whatsapp-incr-run.jsonl`, `_sources/whatsapp.maxts`. Incremental keeps every chat with ≥1
   new message. **Without `--since` it is a full backfill.**
2. `python3 scripts/whatsapp_worklist.py _sources/whatsapp-incr-run.jsonl` → `-pending.jsonl`.
3. `wave_prepare.py --source whatsapp --mode incremental --wave _sources/whatsapp-incr-pending.jsonl` →
   `resolve_wave.py --source whatsapp` → `Workflow({scriptPath:"_whatsapp-pending/whatsapp_distill.mjs",
   args:{today:"<YYYY-MM-DD>"}})`.
4. `python3 scripts/stamp_markers.py --source whatsapp` (**never bare**: bare stamps the iMessage dirs and
   the WhatsApp chunks re-distill into duplicates) → `sync_verify.py --source whatsapp` → `check.sh` →
   review `_sources/whatsapp-sensitive/` → `bump_watermark.py --source whatsapp` → commit.

## Sensitive queues, the review UI and the vault

- Hold queues (`_sources/imsg-sensitive/`, `_sources/whatsapp-sensitive/`, the Outlook PHI ledger) are
  local-only and never staged.
- `python3 scripts/review_server.py` serves a review UI for the iMessage/WhatsApp hold queues and the
  `/clarify` queue at `http://localhost:8765` (the Outlook PHI ledger is reviewed as a file). It binds **127.0.0.1 only** (no flag can change that), has **no
  authentication** (single-user, loopback threat model) and rejects foreign Host headers. Never put it
  behind a tunnel or proxy.
- `python3 scripts/backup_sensitive.py` tars each queue to `~/.engram-backups/` and, **only when
  `_vault/` is git-crypt-encrypted** (a `.gitattributes` rule `_vault/** filter=git-crypt diff=git-crypt`),
  union-merges the exclusion ledgers into `_vault/` so they survive machine loss. The Outlook ledger
  leaves `_sources/` only as an identifier-free projection (`LEDGER_VAULT_FIELDS`). Without git-crypt
  the vault step is skipped. Restoring the Outlook ledger on a new machine is a manual
  `cp _vault/outlook-sensitive/phi-excluded.jsonl _sources/outlook-phi-excluded.jsonl` before the first
  sync; the iMessage side reads the vault copy automatically.

## Post-sync tasks pass (Apple Reminders)

`python3 scripts/engram_tasks.py reconcile` → `extract --only <new-notes list>` → `push`. Ledger:
`_manifest/task-queue.jsonl` (+ `task-log.jsonl`, `task-suggestions.jsonl`, `tasks-state.json`), committed
with the wave. An ACTION, not a gate: a bridge failure (exit 3, zero ledger transitions) never blocks a
sync. Detail: the `/engram-tasks` skill.

## Traps (each one has bitten a real sync)

- **Order: watermark before commit.** `bump_watermark.py` reads the git-ADDED, uncommitted notes; run it
  before the sync commit. Already committed? Set the watermark from the max `date:` of that commit's
  added notes, never today().
- **Capture the Gmail grown set at wave time**, before committing new notes: run afterwards, a
  just-created note dated at the thread's start reads as "grown".
- **Same-day regrowth** can hide behind a fresh done-marker; check the render's `--- MSG n | <ts>`
  headers and move the marker aside by hand if a thread grew again the same day.
- **Renders are metadata.** An agent relaying a large tool result inline can retype it; a verbatim
  connector dump carries `historyId`/`labelIds`. `search_threads` shows only a thread's newest 5
  messages, and `resultCountEstimate` under-reports — paginate. A `get_thread(METADATA_ONLY)` that
  counts a draft can come back one message short of `messageCount`; the render is refused (fail-closed).
- **A worker can mis-key `provenance.ref`** from a neighbouring record: the filename's id suffix not
  matching the ref is the tell (lint warns on slug/ref mismatches).
- **Chat sources yield 0..n notes per chunk**, so `sync_verify`'s count needs `--dropped` and can
  still disagree when one chunk yields two notes; the load-bearing assertion is 0 modified / 0 deleted.
- **Several chat/backup scripts parse argv by hand and ignore `--help`** (`imsg_select.py`,
  `imsg_extract.py`, `whatsapp_extract.py`, `backup_sensitive.py`): `--help` runs a full pass. Read the
  docstring instead.
- **Full Disk Access** is per app (the terminal or editor running Claude Code); a missing grant shows as
  `sqlite3.DatabaseError: authorization denied`. No restart needed after granting.
- **Chat timestamps** are written as UTC: an evening message in a western timezone can land on the next
  day's date.
- **`.mime` / `.eml` file mtimes are not message dates**; export-state files can desync — move them aside
  and re-export (idempotent).
- **check.sh twice without committing** flags every new note SUSPICIOUS (the first run's backlink regen
  edited their `related:`): benign when the diff is only `related:` — commit, then re-run.
- **Task ids are text-keyed**: a note fix that only changes a task's owner does not re-extract it; skip
  the stale ledger row by hand.
- **Concurrent distillers across sources are safe** (`sync_verify` scopes by ref prefix); two
  distillers on the SAME source are not.

## After any sync

`bash scripts/check.sh` (must end green post-commit), bump the watermark, commit. ONE writer at a time.
Recommended: `python3 scripts/dossier_drift.py` (read-only; flags dossiers older than their newest
notes); when it flags drift, run `/engram-dossiers`.

## Adding a source

The shipped sources are the templates: a connector source like `gmail_distill.mjs`, a local-export
source like `outlook_*`, a chat database like `imsg_*` (chunked, with done-markers). A source is: a worklist that emits a wave, a distiller `.mjs` under `_<source>-pending/` whose Setup
agent reads `_sources/<source>.contract.consumed.json` (copy the pattern in `granola_new.mjs`), an entry in
the source lists of
`wave_prepare.py` / `resolve_wave.py` / `sync_verify.py` / `bump_watermark.py`, a watermark key in
`sync-state.json`, and a `log_correction.VALID_SOURCES` entry so audit findings feed its next wave.
Keep the distiller's FIDELITY RULES and SLICE GUARD blocks and run
`python3 scripts/check_distiller_prompts.py` after editing any prompt.
