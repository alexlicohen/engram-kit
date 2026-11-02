---
name: clarify
description: >-
  Interview the owner about the parts of engram the assistant is least confident about, then write
  their answers back into the store. Use when the owner says "/clarify", "clarify mode", "ask me about
  what you're unsure of", "interview me", or wants to resolve verify/tentative flags. Drives a
  conversational, resumable Q&A over a ranked queue of low-confidence items (people dedup/roles/
  promotion, domain/thin-source notes) and applies each
  confirmed answer atomically — updating people.md / note frontmatter / the manifest and clearing
  the underlying flag.
---

# Clarify — the confidence interview

engram constantly *creates* low-confidence flags during ingest (`verify: true`, tentative roles,
`third-party-sensitive`, `needs-domain-confirm`, thin-source). This skill is the mechanism that
*retires* them: it asks the owner about the highest-value uncertainties, one entity at a time, and
writes the answers back. It is **read-curated, never fabricating** — if the owner doesn't know, the
item is deferred, not guessed (per CLAUDE.md "Provenance & truth").

## Files

- `scripts/build_clarify_queue.py` — regenerates the ranked queue from current store signals.
- `_manifest/clarify-queue.jsonl` — one question per line: `id, category, priority, entity,
  question, candidates, evidence, writeback, blocked_on, status` (+ `answer` once answered).
  `status` ∈ `open | answered | skipped | deferred`. Sorted by priority desc.
- `_manifest/clarify-log.jsonl` — append-only audit: every answer and the edit it produced.

## Run loop

1. **Refresh the queue** so it reflects the latest store:
   `python3 scripts/build_clarify_queue.py --summary`
   (Idempotent — it preserves any prior `answered/skipped/deferred` status by stable `id`.)
2. **Load** `_manifest/clarify-queue.jsonl`, keep `status == "open"`, drop items whose
   `blocked_on` is unmet (e.g. "the team org chart" until the owner provides it — mention they're parked).
3. **Group by `entity`** and ask the highest-priority group first. Prefer **entity-grouped
   batches of 3–5 questions** (all of one person's identity + role + promotion together) over
   strict one-at-a-time — fewer turns, same context. Honor a count arg: `/clarify 10` ≈ ten items.
4. For each question **show the evidence** (`evidence` field: meeting counts, domains, note
   paths, the conflicting spellings) so the owner can answer from memory, and offer the `candidates`.
   Never lead toward an answer; present the options neutrally.
5. **On each answer, apply the write-back immediately** (recipes below), then:
   - append a record to `_manifest/clarify-log.jsonl`:
     `{"id":..., "answer":"<verbatim>", "edit":"<what changed>", "files":[...]}`
   - set that queue item's `status` to `answered` (or `skipped`/`deferred`) and store `answer`.
   - If the owner doesn't know → `deferred`. If they say it's noise/irrelevant → `skipped`.
6. **Stop** when the count/group is done or the owner says stop. Then **show a summary** of edits and
   commit per the convention (batch-review-then-commit): one commit, lint first.

## Write-back recipes (apply in the SAME step as clearing the flag)

Honor the CLAUDE.md file-ops rules on every edit: **never delete** (archive to `_archive/`);
**any rename/move updates inbound `[[wikilinks]]` and `participants:` in the same step**;
keep frontmatter valid (`_manifest/schema.md`).

- **people-dedup** — *"yes, same person"*: add the variant as an alias in the canonical row of
  `_manifest/people.md`, and rewrite that spelling to the canonical name in every note's
  `participants:` (grep the variant, edit each). *"no, distinct"*: treat as **people-promote**.
- **people-cluster** — a whole group of spelling variants (`evidence.members`, each with its note
  count) **none** of which is in `people.md`, so there is no row to alias onto — asked once instead
  of pairwise. Show every member with its note count, the example note paths, and any
  `evidence.cooccurrences` / `evidence.chained` warning (both mean *probably not one person*).
  *"canonical: `<Member>`"* (optionally `; role: <text>` and/or `; only: <Member>, <Member>` to
  merge just a subset — unlabelled trailing text is **refused**, never written in as a role):
  promote that spelling as a new **`Verify? = true`** row, record the others as its
  aliases, and rewrite `participants:`/`[[wikilinks]]` everywhere. *"not one person"* / *"skip"*:
  mark answered, change nothing. Apply via `clarify_writeback.py` — it does all of the above in one
  rollback-guarded transaction and records the sanctioned participants overrides. The promoted row
  comes back as a **people-verify** question next build (choosing a spelling ≠ confirming the
  person), which is where the real role gets filled in.
- **people-promote** — *"add"*: insert a row in the correct domain table of `people.md`
  (canonical name + relationship/role); remove it from the `verify`/discovered backlog.
  *"alias of X"*: do the dedup alias write-back instead. *"noise"*: just mark skipped.
- **people-verify** — a row already in `people.md` with `Verify? = yes` (auto-added from notes,
  unconfirmed). *"confirm"*: change that row's `Verify?` cell from `yes` to `—`. *"correct"*: fix
  the canonical name and/or role text, then set `Verify?` → `—` (if the name changed, rewrite the
  old spelling in every note's `participants:`). *"merge/remove"*: fold into the right canonical row
  as an alias (dedup write-back) or delete the row.
- **people-role** — replace the tentative role text in the `people.md` row with the owner's answer and
  drop the `tentative` / `(role TBD)` marker. (team-roster roles can be `blocked_on` an org chart —
  skip until provided.)
- **domain** — *"correct"*: clear the flag tag. *"move"*: relocate the file to the right domain
  dir, fix `domain:`/tags, and update inbound `[[wikilinks]]` in the same step.
- **thin-source** — *"keep"*: mark answered. *"enrich"*: add the detail the owner gives (note it as
  `source: manual` provenance addition). *"archive"*: move to `_archive/`.

## Hand-curated items (`dossier-conflict`)

Some questions no harvester in `build_clarify_queue.py` can produce — e.g. two ingested notes (or
a note and a dossier) disagree with each other and nothing short of asking the owner resolves it. These
live in an optional committed sidecar, `_manifest/clarify-manual.jsonl` (absent in a fresh store:
create it with the first such question; one JSON record per line, same
shape as a generated item: `id, category:"dossier-conflict", priority, entity, question,
candidates, evidence:{dossier, notes:[paths]}, status:"open"|"resolved", added`), which
`build_clarify_queue.py`'s `load_manual_items()` merges into every regenerated queue — so a
hand-added question survives `check.sh` wiping and regenerating `clarify-queue.jsonl`. Only `open`
entries are merged; a malformed line fails the build loudly rather than being dropped.

- **Answering one**: same as any other queue item (show evidence, ask, get the owner's answer) — but the
  write-back is **manual, never a scripted recipe**: edit the dossier body via
  `scripts/reconcile_dossier.py`, citing the owner's answer in the commit/log, then edit
  `_manifest/clarify-manual.jsonl` directly to flip that entry's `status` to `resolved` and add
  `resolved: <date>` + `answer: <verbatim>`. The entry then drops out of the next regen.
- **Adding one**: append a new record with a fresh, stable `id` (never reuse or duplicate one) and
  `status: "open"`; run `build_clarify_queue.py` to confirm it merges.

## After the session

- `python3 scripts/lint_store.py` → expect 0 errors (fix any broken wikilink from a rename).
- `python3 scripts/regen.py` if any note titles/domains changed (refreshes MOCs/digests).
- `python3 scripts/build_clarify_queue.py` once more so the queue reflects the cleared flags.
- Commit (one reviewable commit) and push. Report the cleared count + what's left open.

## Notes

- **Resumable across sessions** — status persists in the queue, so a later `/clarify` picks up
  exactly where this one stopped. Mirrors the ingestion manifest pattern.
- **Don't run it prematurely on people-promote / people-verify** if a bigger canonical source is
  about to land (a Drive ingest, a roster, an org chart) — those confirm/correct many
  promote/verify/role items in bulk, and the next regen drops the resolved ones. Lead with
  `people-dedup`, which is source-stable. (Per the 2026-06-30 plan: run the full `/clarify` sweep
  *after* the Drive ingest, when the queue is richest and Drive has auto-resolved a chunk.)
