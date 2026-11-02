# engram-kit architecture

engram is a single-owner, file-based knowledge store that LLM agents (Claude Code) and the owner
read and maintain together. Plain markdown + YAML frontmatter + grep, versioned in git. There is
deliberately no database, no embeddings and no daemon: the store is just files, every derived view is
regenerated and committed, and one gate (`scripts/check.sh`) decides whether a commit is allowed.

```
 sources (Granola, Gmail, …)              the store (git)                         consumers
 ─────────────────────                    ─────────────────────────────           ─────────────────
 connector → worklist → wave ─┐           <domain>/<note>.md   (notes)            engram skill (grep +
                              │ contract  _manifest/schema.md  (contract)          index-then-drill)
 wave_prepare → resolve_wave ─┤──────►    _manifest/people.md  (registry)         MCP server (read-only)
                              │ distill   _manifest/index.jsonl (derived)         engram_brief (due/drift)
 Workflow(<source>.mjs) ──────┘           INDEX.md, <domain>/_index.md (derived)  /clarify interview
 sync_verify → check.sh → bump_watermark  dossiers/, digests/  (synthesized)      fidelity audit / eval
```

## 1. Retrieval: index-then-drill

An agent never loads a whole domain. It reads `INDEX.md` (the domain map), then the domain's
`_index.md`, then the note. For a large domain `regen.py` writes `_index.md` as a map — areas
(`subpath`) → periods (year) → notes — each sized to a single read. Cross-cutting questions start at
`dossiers/` (curated one-pagers), `digests/` (open loops, decisions, action items) and `glossary.md`.

`grep` is the engine. `_manifest/index.jsonl` holds one JSON record per note (path, title, date,
domain, subpath, tags, status, source, ref, participants, related), so frontmatter-wide questions are
a `jq` over one file. The `engram` skill (`_global-skills/engram/SKILL.md`) packages the recipes: entity
brief, open loops on a topic, what changed since a date, source of a claim, plus a "delegate for
breadth" pattern (send a broad question to one fresh sub-agent and keep only its answer).

Why not RAG: the primary consumer is an agent with grep and a file reader. Index-then-drill gives it
exact, citable paths, costs nothing to keep fresh, and versions with the notes.

## 2. Schema and provenance

`_manifest/schema.md` is the note contract; `scripts/lint_store.py` enforces it (`make lint`). Every
note has `title`, `date`, `tags`, `domain`, `source`, `provenance`, `participants`, `status`, and lives
in exactly one top-level domain dir (the list's single owner: `_manifest/constants/domains.json`; the
tag vocabulary's: `_manifest/constants/tag_vocab.json`).

`provenance.ref` is a stable pointer back to the source (`granola:<meeting-id>`,
`gmail:thread:<id>`, `obsidian:<path>`, `manual:<…>`), never a dump of it. Refs are unique per note
(lint fails on a duplicate), which is what makes dedup, re-fetch-and-audit and grown-thread detection
possible. `provenance.method` records whether the note was **distilled** (compressed by an LLM) or
**verbatim** (hand-authored text kept untouched).

`_manifest/people.md` is the people/entity registry. Distillers resolve participants through it; an
unknown person is added with `verify: true` and surfaced to the owner rather than asserted. Its `self`
row is the owner: every script that needs to know whose store this is reads that row
(`scripts/_lib/actions.self_aliases`) — nothing hard-codes a name or e-mail.

## 3. The gate: `make verify` + `scripts/check.sh`

- `make verify` = `lint_store.py` → `phi_scan_store.py --summary` → `pytest scripts/tests/`, fail-fast.
- `scripts/check.sh` = lint → corrections-ledger lint → the frontmatter-preserve gate over notes already
  modified before the run → the move gate → regenerate every derived view (`build_index.py`,
  `build_cooccurrence.py`, `regen.py` for `INDEX.md`/`_index.md`/`STATUS.md` tallies, `gen_backlinks.py`
  for `related:` and dossier backlinks, `build_clarify_queue.py`) → **STALE** check: if regeneration
  changed a tracked file, the derived views were not committed and the gate fails.

Derived views are committed so that a fresh clone is self-describing and a diff shows exactly what a
change did to the indices. CI (`.github/workflows/verify.yml`) runs the same two commands.

Instruments are kept out of the gate on purpose: the fidelity audit and retrieval eval are LLM-cost and
non-deterministic; `check_ref_integrity.py` and `check_distiller_prompts.py` (needs node) are
informational. A gate must be cheap, deterministic and runnable offline.

## 4. Sync pipeline (contract-guarded waves)

Runbook: `_manifest/SYNC.md`; checklist: the `/engram-sync` skill. Every source follows the same five
stages:

1. **Worklist** — connector discovery since the source's watermark (`_manifest/sync-state.json`), minus
   items that already have a note (by provenance ref) → a wave file, one JSON line per item.
2. **Contract** — `wave_prepare.py` writes `_sources/<S>.contract.json` (count, sha256, mode).
   `resolve_wave.py` validates it and atomically renames it to `*.contract.consumed.json` (one-shot: a
   retry or double launch finds no live contract and aborts). Backfill mode needs an explicit on-disk
   `--allow-history` token. It also renders the source's open corrections into a brief and pins the
   brief's sha256 in the consumed contract.
3. **Distill** — a Claude Code Workflow script (`_<source>-pending/<source>_new.mjs`). Its Setup agent
   reads the consumed contract (never Workflow args, which once silently failed to thread and caused a
   history re-distill), workers write notes, done-markers make it resumable. All guards live in Python
   because the Workflow runtime has no filesystem or `import()`.
4. **Verify** — `sync_verify.py`: the on-disk note delta must equal `expected - dropped`; in
   incremental mode every matched note must be newly ADDED (a modified note = history was rewritten).
   For Granola, a transcript-verify pass (`granola_verify.py` + `granola_verify.mjs`) then corrects the
   new notes against the transcript, body only, each edit logged with its quote.
5. **Commit** — `check.sh`, `bump_watermark.py` (from the max date actually written, never today()),
   commit. One writer at a time; the committed watermark is the handoff between syncs.

### Sources

Each source is a worklist (Python, deterministic, resumable from on-disk state) feeding the same
contract → distill → verify → commit stages. They differ only in how items are found and what "done"
means. Runbook per source, prerequisites and traps: `_manifest/SYNC.md`.

| Source | Discovery | Done = | Provenance ref | Distiller |
|---|---|---|---|---|
| Granola | connector, since watermark | a `granola:<id>` note | `granola:<id>` | `granola_new.mjs` + `granola_verify.mjs` |
| Gmail (primary) | connector `search_threads`; `gmail_render.py` metadata renders | a note (hex == decimal id) or a drop marker | `gmail:thread:<id>` | `gmail_distill.mjs`; grown threads `gmail_update.mjs` |
| Gmail (secondary) | Takeout mbox: `mbox_index` → `gmail_secondary_select` → `mbox_extract` | as above; `-sec<id8>` filename suffix | `gmail:thread:<id>` | `gmail_secondary_distill.mjs` |
| Outlook | `.eml` export / Graph → `outlook_extract` → `outlook_select` (threading, PHI holds) | a note or a drop marker; PHI ledger subtracted | `outlook:thread:<thrid>` | `outlook_distill.mjs` |
| iMessage | `chat.db` → `imsg_select` → `imsg_extract` (chunks) | a done-marker per chunk (`stamp_markers.py`) | `imessage:<guid>:<chunk>` | `imsg_distill.mjs` |
| WhatsApp | `ChatStorage.sqlite` → `whatsapp_extract` | as iMessage | `whatsapp:<jid>:<chunk>` | `whatsapp_distill.mjs` |

Two mechanisms recur. **Drops are durable and auditable**: a dropped item gets a marker, never
silence, and Gmail re-opens a dropped thread the owner later replied to (the marker is archived, not
deleted). **Owner identity and routing are configuration**: every "sent by the owner" test reads the
people.md `self` row (`_lib.actions.self_emails`), and header-only routing, PHI-screen sender domains and
clinic task senders live in `_manifest/constants/mail_routing.json` (`_lib/mail_routing.py`). The
distiller sets the authoritative domain; selectors only pre-sort.

## 5. Frontmatter-preserve gate and the override ledger

A distilled note modified in place (a re-distill, a fidelity fix) may change only its body, `updated:`
and append-only `participants:`; everything that identifies it (title, date, tags, domain, subpath,
source, status, provenance, related, supersedes, `verified`) is frozen
(`scripts/_lib/frontmatter_diff.py` profiles, enforced by `verify_frontmatter_preserved.py`, exit 1
naming the field). Moves are gated too: a note moved to another domain is compared against its
pre-move blob. Other sources (verbatim imports, manual notes) get a blunt SUSPICIOUS halt instead.

A correction that genuinely needs a frozen field changed goes through one of two sanctioned writers:
`edit_note_frontmatter.py` (`--set field=value --reason …`) or answer-driven `clarify_writeback.py`.
Each records the transition in the append-only `_manifest/frontmatter-overrides.jsonl`. An entry is
live only while that ledger line is uncommitted and the note's HEAD blob still matches the recorded
sha — exactly the window between the guarded edit and the commit that lands both. `verified:` is never
settable by any writer: it records that the owner personally confirmed a note, by hand.
Dossiers (`dossiers/*.md`) are reconciled through `reconcile_dossier.py`
(validate → write → lint → rollback), with identity fields frozen and `related`/`supersedes` mutable.

## 6. PHI exclusion

The one sensitivity rule: no identifiable clinical-patient data in the store. `scripts/phi_scan.py` is
a deterministic heuristic (identifier patterns × clinical vocabulary → a level) used three ways: at
ingest (a source's select/worklist step holds flagged items out), at write-back (`engram_capture.py`
refuses a flagged note unless the owner explicitly overrides), and as a store-wide audit in
`make verify` (`phi_scan_store.py`, WARN on flags, non-zero only on an internal error — fail-closed).
Held items stay local and are released only by the owner.

**Work mail is the high-risk source.** `outlook_select.py` adds deterministic holds the general
heuristic misses (roster/hand-off subjects, EHR senders and subjects, MRN-shaped numbers, a bare
lowercase person-name subject from a clinical sender) and a non-PHI drop rule for configured clinic
task senders. `outlook_worklist.py` keeps a **sticky** exclusion ledger: once held, a thread stays held
even if it grows and its scanner score drops, until the owner releases it. Chat distillers write highly
sensitive items to a local hold queue instead of the store; `review_server.py` (127.0.0.1 only, no
auth) is the review surface. `backup_sensitive.py` keeps the exclusion ledgers durable — local tarballs,
plus an identifier-free projection in a git-crypt `_vault/` when one is configured.

## 7. Fidelity: audit → corrections ledger → next wave

LLM distillation fabricates at a measurable, source-specific rate. The loop that keeps it honest:

1. **Spot-check every sync** (`/engram-audit`, `audit_sample.py --only <new notes>`): auditors re-fetch
   each note's original source and grade it adversarially (FABRICATION / DISTORTION / MINOR / OK).
2. **Refute pass**: every FABRICATION candidate gets a second adversarial reader before anything changes;
   raw auditor verdicts carry real false positives.
3. **Fix body-only**, source-verified, and log the defect:
   `audit_to_corrections.py` / `log_correction.py` append to `_manifest/corrections/<source>.jsonl`
   (closed defect taxonomy, deterministic ids, `corrections_lint.py` in check.sh).
4. **Feed forward**: `resolve_wave.py` renders the source's open corrections into the next wave's brief,
   which the distiller's Setup agent folds in as extra adversarial reminders. Promoting a recurring
   correction into the distiller's FIDELITY RULES block is a manual, reviewed edit;
   `check_distiller_prompts.py` renders every prompt to prove it still parses and keeps its guard blocks.

Per-distiller rates are never pooled: sources fail differently.

## 8. Agent tooling

- **Skills** (`.claude/skills/`, `_global-skills/`): `engram` (recall), `engram-capture` (write one
  durable fact back from any session, with the PHI guard), `engram-distill-session`, `/engram-sync`,
  `/engram-audit`, `/engram-eval` (retrieval eval over a fixed question bank), `/engram-brief`
  (deterministic due/drift brief, `engram_brief.py`), `/engram-dossiers` (`dossier_drift.py` →
  synthesis → `reconcile_dossier.py`), `/clarify` (an interview over `build_clarify_queue.py`'s ranked
  low-confidence items, applied by `clarify_writeback.py`).
- **MCP server** (`scripts/engram_mcp.py`): local stdio, read-only recall for Claude Desktop. It serves
  git-tracked files only (so gitignored `_sources/` never leaks), excludes `_vault/`, realpath-contains
  every path to the repo, and has no write tools. Wire it in `claude_desktop_config.json` as
  `uv run --python 3.12 --with mcp <repo>/scripts/engram_mcp.py`.
- **Task bridge** (`scripts/engram_tasks.py`, `/engram-tasks`): action-item lines are parsed by the
  shared grammar in `_lib/actions.py` into a committed ledger (`_manifest/task-queue.jsonl`), pushed to
  Apple Reminders through a JXA bridge, and reconciled back (complete = done, delete = dismissed forever,
  unflag = confirmed). Push policy holds other people's and undated items; list routing is
  `_manifest/constants/reminders.json`. Optional nightly launchd job (`scripts/tasks_nightly.sh`, plist
  template in `scripts/launchd/`). An action, never a gate.
- **Profile instrument** (`scripts/profile_metrics.py`): deterministic attention metrics (six
  estimators per domain, so note volume is never read as attention) over `index.jsonl` and the
  digests. Definitions: `_profile/README.md`. Outputs describe the owner; keep them out of any shared copy.
- **Instruments**: `audit_sample.py`, `eval_report.py`, `fact_consistency_scan.py` (two notes stating
  different values for the same fact), `mutate_check.py` (does a test fail on a planted bug?),
  `check_ref_integrity.py`.

## Renaming this kit's defaults

The seed domains (`research work ventures finance personal health infra literature unsorted`) are an
example. To change them: edit `_manifest/constants/domains.json`, rename the dirs, update `INDEX.md`,
the domain → group map in `scripts/granola_glossary.py` (`DOMAIN_GROUP`, asserted against the list),
the open-loops headings map in `scripts/_lib/actions.py`, the domains named in
`_manifest/constants/mail_routing.json` and `reminders.json`, and the domain-routing prose in each
distiller (`_*-pending/*.mjs`); then run `make verify && bash scripts/check.sh`.
