# engram — store conventions

engram is a private markdown context library, **NOT** a vector/RAG/embedding store.
Retrieval is file-based: read `INDEX.md`, then open the relevant domain dir and the notes inside it.
Loaded context = this file + curated notes; everything else is read on demand. How the machinery
fits together: `docs/ARCHITECTURE.md`. Why the rules below exist: `docs/LESSONS.md`.

## Retrieval (index-then-drill)

Don't load everything: `INDEX.md` → the domain's `_index.md` (a large domain's is a map: area →
period → note; follow its links, don't reconstruct paths) → the note. Cross-cutting questions:
`dossiers/`, `digests/` and `glossary.md` first. Content search and frontmatter-wide scans: grep and
`_manifest/index.jsonl`. Full procedure: the `engram` skill (`_global-skills/engram/SKILL.md`).

- `dossiers/` and `digests/` are synthesized artifacts, not domain notes (valid wikilink targets and
  reachability hubs; not linted or indexed as notes).
- `_manifest/co-occurrence.md` is a weak signal: consider, never assert.

## Layout

Top-level **domain dirs** hold the notes (`INDEX.md` maps them; the list's single owner is
`_manifest/constants/domains.json`). Underscored dirs are infrastructure:

- `_sources/` — gitignored raw connector pulls and run state (never committed).
- `_archive/` — superseded or stale notes (moved here, **never deleted**).
- `_manifest/` — schema, registry (`people.md`), constants, ledgers and generated indices.
- `_<source>-pending/` — a source's distiller prompts (Claude Code Workflow `.mjs` files).

## Maintenance rules

- **One owner per decision — never re-hardcode.** The domain list and tag vocab live in
  `_manifest/constants/{domains,tag_vocab}.json`; frontmatter parsing, provenance-ref extraction and
  the series `norm()` live in `scripts/_lib/`. Import them; never paste a local copy.
- **Sync-pipeline guards live in Python, never in a distiller `.mjs`** (the Workflow runtime has no
  `import()` or filesystem). Re-run `wave_prepare.py` + `resolve_wave.py` fresh immediately before
  launching a distiller. Flow: `_manifest/SYNC.md`.
- **`bash scripts/check.sh` is the gate**: green before every commit. Tests: `make verify`. check.sh
  regenerates derived views; commit its output, then re-run for the green. A SUSPICIOUS warning on a
  modified note body: investigate before committing. Instruments stay out of check.sh: `/engram-audit`,
  `/engram-eval`, `check_ref_integrity.py`, `check_distiller_prompts.py` (check.sh must not need node).
- **`verified:` frontmatter is human-only**: set only by the owner's explicit action, by hand-editing
  frontmatter; never written or altered by a distiller, a redistill script or any automated write path.
- **The frontmatter-preserve gate is hard.** A distilled-source note modified in place may change only
  its body, `updated:` and append-only `participants:`; other sources keep the SUSPICIOUS halt; a
  modified dossier keeps its identity fields frozen (`related`/`supersedes` mutable). Frozen-field
  corrections go only through the override-ledger writers `scripts/edit_note_frontmatter.py` and
  answer-driven `scripts/clarify_writeback.py`; dossier edits may use `scripts/reconcile_dossier.py`.
  Never let a distiller, a sync or any other automated path call a ledger writer. Prove any gate change
  both ways (passes with the ledger, still fails under `--no-overrides`).
- **Source-verify before correcting a flagged note.** Re-fetch the source; never grade a note against a
  rendered summary of it.
- **Registry changes** (`_manifest/people.md`): regenerate the Granola KNOWN NAMES block
  (`python3 scripts/granola_glossary.py`) and paste it into your Granola template.
- **Fidelity corrections feed the next sync** via append-only `_manifest/corrections/<source>.jsonl`
  (`audit_to_corrections.py` / `log_correction.py`). Hardening a correction into a distiller's FIDELITY
  RULES block stays a manual, reviewed edit.
- `digests/action-items.md` and `digests/decisions.md` are on demand (`scripts/gen_digests.py`),
  gitignored. `digests/open-loops.md` is hand/agent-curated.
- **Owner identity and routing are configuration, never code**: the people.md `self` row (names and
  every address the owner sends from), `_manifest/constants/mail_routing.json` (PHI-screen sender
  domains, clinic task senders, header routing) and `_manifest/constants/reminders.json` (task lists).
  Never hard-code a person, address, institution or list name in a script or distiller.
- **The Outlook PHI hold is sticky**: `_sources/outlook-phi-excluded.jsonl` owns "ever held"; release
  only through `_sources/outlook-released.txt`, with the owner. Hold queues under `_sources/` are never
  staged, and never copied into a brief or an external tool's input.
- **engram-tasks (the Reminders bridge) is an action, never a gate**: never wire it into check.sh; a
  bridge failure never blocks a sync; a deleted reminder is dismissed forever (never re-pushed).
- `scripts/review_server.py` binds 127.0.0.1 only and has no auth: never expose it.

## Every note

- Has YAML frontmatter conforming to `_manifest/schema.md` (`title`, `date`, `tags`, `domain`,
  `source`, `provenance`, `participants`, `status`).
- Has **provenance**: a stable source ref (`granola:<id>`, `gmail:thread:<id>`, `obsidian:<path>`,
  `manual:<…>`, …), never a raw dump of the source.
- Lives in **exactly one** top-level domain dir. Ambiguous source/domain → `unsorted/` + tag
  `flagged`. **Never fabricate** a domain or a fact to avoid flagging.

## Distillation vs verbatim

- **Distilled** sources (meeting transcripts, email threads, chat logs) are compressed into atomic notes
  (decisions, commitments, key discussion, references).
- **Verbatim** sources (hand-authored notes imported from another tool) keep their content
  **untouched**; only structure, metadata and links are normalized. `provenance.method` records which.
- Distiller prompts carry FIDELITY RULES and SLICE GUARD blocks (Granola also a SECONDARY-ACCOUNT
  GUARD) that are **never removed** when editing them. After editing a prompt run
  `python3 scripts/check_distiller_prompts.py`. Never pool fabrication rates across distillers.
- The Granola transcript-verify pass after `granola_new.mjs` is mandatory and ends with
  `granola_verify.py check`: body-only fixes to NEW notes, each logged with its transcript quote;
  committed notes get propose mode only.

## File operations

- **Never** rename or move a file without updating inbound `[[wikilinks]]` in the same step.
- **Never** delete a note — move it to `_archive/`. Never delete a consumed wave contract
  (`_sources/*.contract.consumed.json`).
- **Never** commit raw source dumps (mbox, email bodies, chat exports, transcripts).

## Sensitive data — no clinical patient PHI

The store is single-user, so there is no access-control notion of "confidential". The one sensitivity
rule: **no clinical patient PHI in the store.**

- **Exclude at ingest.** Identifiable data about clinical patients (name/initials/MRN/DOB tied to a
  diagnosis, treatment or encounter) is never written. PHI-bearing source material is referenced by
  catalog link only, and a catalog entry that is itself identifying is excluded too.
- **De-identified material is allowed** when identifiers are stripped on the way in and verified.
- **Verify, don't just trust.** `phi_scan_store.py` runs in `make verify`; scan periodically.
- Out of scope: incidental personal health of family or colleagues is ordinary personal context.
- **Don't auto-merge sources.** Flag cross-source overlaps for review rather than merging them.
- **Secrets never go in notes.** If you need a secret store in the repo, use an encrypted dir
  (e.g. git-crypt) and back its key up outside the repo.

## Provenance & truth

- Do not fabricate. Unknown source or domain → `unsorted/` + tag `flagged`.
- People resolve through `_manifest/people.md` (its `self` row is the owner). Entities not in the
  registry are added with `verify: true` and surfaced to the owner (`/clarify`), never asserted.
