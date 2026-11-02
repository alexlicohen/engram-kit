---
name: engram-capture
description: >-
  Write a durable fact BACK into the owner's engram second brain from ANY session — a decision made
  mid-work, a new commitment/open-loop, a new person, or a correction to a stale fact — so it isn't lost
  when the session ends (connectors only capture email/meetings/messages, not what happens inside a Claude
  session). Use when the owner says "/engram-capture", "save this to engram", "remember this in engram", "add
  this decision/commitment to my second brain", or when a genuinely durable fact emerges that won't reach
  engram any other way. Writes locally + commits (never pushes); refuses clinical-patient PHI.
---

# engram-capture — write-back to the second brain

The counterpart to `/engram` (recall). It appends ONE note per capture to the store at
`$ENGRAM_ROOT` (`$E`) via the guarded helper `scripts/engram_capture.py`. Capture only
DURABLE facts (a decision, a commitment/open-loop, a new person + role, or a correction) — not chatter.
Don't fabricate; if you're unsure of a field, ask the owner or route to `unsorted`.

```bash
E=$ENGRAM_ROOT
```

## How to capture

1. **Draft the note content** (per `$E/_manifest/schema.md` + `$E/AGENTS.md`): a concise `title`, the right
   `domain` (research · work · ventures · ventures · ventures · finance · personal · health · clinical
   · infra — pick one; if genuinely ambiguous use `unsorted`), `tags`, `type`
   (`event` for a dated point-in-time fact, `evergreen` for living state), `participants`, and a short body
   with a `## Summary` and the relevant `## Decisions` / `## Action items` sections. Resolve names via
   `$E/_manifest/people.md` — if a participant isn't in the registry yet, that's fine, capture proceeds
   anyway; registering them (with `verify: true`) is `/clarify`'s job, not this skill's.

   **Tags are hard-validated** against the controlled vocabulary in `$E/_manifest/schema.md`
   (§Controlled tag vocabulary) / `$E/_manifest/constants/tag_vocab.json` — an off-vocab tag makes the
   helper below **reject the write entirely (nothing saved)**, not just warn. Pick tags from that list.

2. **Determine `<session id>`** — the provenance pointer for this capture. It's the basename (minus
   `.jsonl`) of your own transcript file. Two ways to get it without hunting for a file listing:
   - Your scratchpad directory path (if you have one) embeds it directly: `.../<project-slug>/<SESSION-ID>/scratchpad`.
   - Otherwise, the newest `*.jsonl` in `~/.claude/projects/<project-slug>/` (slug = cwd path with
     `/` → `-`) is your live transcript; its basename is the session id.

3. **Call the helper** (it owns provenance, the PHI guard, subpath, verify, and the local commit):
   ```bash
   python3 $E/scripts/engram_capture.py --session "<session id>" \
     --domain <dom> --title "<title>" --tags "a,b" --type event \
     --kind decision|commitment|person|correction \
     [--participants "X,Y"] [--owner <name>] [--due <YYYY-MM-DD>] [--supersedes <slug>] \
     [--date <YYYY-MM-DD>] [--flagged] \
     --body-file <path-to-body.md>
   ```
   - Provenance is minted as `claude-chat:<session-id>:c<n>` (auto-incremented per session).
   - `--kind commitment` also appends the item to `digests/open-loops.md`.
   - `--kind correction --supersedes <slug>`: writes a NEW note that `supersedes:` the stale one — it
     NEVER edits the original (the next dossier reconcile merges it). Use this for "fact X is now wrong".
   - `--date <YYYY-MM-DD>`: back-dates the fact to when it actually happened/was decided (default: today
     — only override this if the decision is being captured after the fact).
   - `--flagged`: forces routing to `unsorted/` + the `flagged` tag — use this for the "genuinely
     ambiguous domain" case in step 1, instead of just picking `--domain unsorted` by hand.
   - The helper runs `check.sh` (regenerate + lint) and commits locally `capture: <title>` — **no push**.
     The capture rides the engram session's next `/engram-sync` or `/wrap-up` push; a wrap-up
     in another session leaves it committed and lists it as unpushed.

4. **If the helper REFUSES on PHI** (fail-closed): it detected a possible clinical-patient-PHI pattern.
   Surface it to the owner verbatim. Only re-run with `--phi-reviewed` if the owner confirms it is NOT
   clinical-patient PHI (i.e. they are not the person's treating physician) — the same rule as ingest. Never
   force PHI into the store.

## Guardrails
- **Local only, never push.** A foreign session must not push to the owner's repo (unless the owner explicitly says
  push); the helper commits locally.
- **One durable fact per capture.** Multiple facts → multiple calls (each gets its own `:c<n>`).
- **Don't auto-merge.** A captured fact will often be re-ingested later via Gmail/Granola — that's fine;
  the store's overlap-flag / dossier-reconcile handles dedup. Do not hunt down and edit prior notes.
- **No clinical-patient PHI**, ever (the helper enforces it). Business-sensitive/COI content is ordinary
  here — but if this capture originated from an external deliverable, use judgment about what to record.
