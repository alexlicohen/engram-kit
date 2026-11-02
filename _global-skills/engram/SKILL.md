---
name: engram
description: >-
  Retrieve the owner's personal & professional context from their private "engram" second-brain store (people, work and research, ventures, finance, family/personal, commitments, decisions, provenance). Use from ANY session whenever a task needs facts about the owner's life or work that aren't in the current project/repo — e.g. drafting an email or grant that needs a collaborator's role or a prior decision, "who is X", "what do we know about X", "what's the status of / what's open on Y", "what changed since <date>", or reconciling a fact against its source. Triggers: "/engram", "check engram", "recall from engram", "what does engram say about…". READ-ONLY from outside the store (to save a fact: engram-capture).
---

# engram — recall

engram is the owner's private markdown **second brain** at `$ENGRAM_ROOT` (call it
`$E`). It is **NOT** a vector/RAG/embedding store — retrieval is file-based: **grep + index-then-drill**.
Notes across life-areas, each with YAML frontmatter (title/date/tags/domain/source/provenance/
participants/status) and a stable `provenance.ref` back to its source (gmail · outlook · obsidian ·
imessage · granola · whatsapp · gdrive · claude-chat). From any session **other** than the engram repo
itself, treat it **READ-ONLY** — never edit or commit the store from elsewhere.

```bash
E="${ENGRAM_ROOT:-$HOME/engram}"
```

## How retrieval works (index-then-drill, 3 tiers)

Don't load whole domains. Navigate:
1. `$E/INDEX.md` → pick the **domain** (the list is `_manifest/constants/domains.json`; the seed set is
   research · work · ventures · finance · personal · health · infra · literature · unsorted — the last
   is the flagged/ambiguous catch-all, rarely a recall target).
2. that domain's `_index.md`. For a **large** domain it's a compact **map**: an `### Areas` section links
   each subpath's own `_index.md` (e.g. `work/projects/_index.md`, `personal/family/_index.md`),
   and a large area's index is itself a `### By period` map linking per-year leaves (e.g.
   `work/projects/2026/_index.md`). Follow area → year → note. Every `_index.md` is sized to a
   single read.
3. open the note. For cross-cutting questions start with **`$E/dossiers/`** (curated one-pager syntheses),
   **`$E/digests/`** (open-loops, decisions, action-items), `$E/_manifest/people.md`, `$E/glossary.md`.
- **grep is the engine.** The exhaustive per-note scan surface is **`$E/_manifest/index.jsonl`** (one JSON
  record per note: `path,title,date,domain,subpath,tags,status,source,ref,participants,related`) — `jq` it.

## Recipes (use these — they answer most questions without reading much)

**Entity brief — a person or org ("who is X / what do we know about X"):**
```bash
grep -i "X" $E/_manifest/people.md                 # canonical name, role, aliases (the registry)
ls $E/dossiers/ | grep -i "X"                       # a one-pager? (one per project, org or long-running matter)
jq -r 'select((.participants[]?|test("X";"i")) or (.title|test("X";"i")))
       | "\(.date) [\(.domain)] \(.title)  ·  \(.path)"' $E/_manifest/index.jsonl | sort -r | head -20
```

**Open loops / commitments on a topic or person Y ("what's open on Y"):**
```bash
grep -i "Y" $E/digests/open-loops.md                # curated open commitments, by domain, with owner/due/status
```

**What's due / what needs attention (broader than one topic — "what's on my plate", "what am I forgetting"):**
```bash
python3 $E/scripts/engram_brief.py --days 14         # near-term + overdue open loops (days-remaining, owner),
                                                      # drifted dossiers, sync-watermark staleness, hygiene tail
```
Deterministic, <1s, read-only — reuse this instead of hand-grepping `open-loops.md` for anything broader
than a single topic/person.

**What changed since a date D ("what's new / recent on Z"):**
```bash
jq -r --arg d "2026-06-01" 'select(.date >= $d) | "\(.date) [\(.domain)] \(.title)  ·  \(.path)"' \
   $E/_manifest/index.jsonl | sort -r | head -40         # NEW notes since D
grep -rl "^updated: 2026-0[67]" $E/*/**/*.md 2>/dev/null # notes REFRESHED in place (thread-updates etc.)
```

**Status of a specific matter ("state of the Example Fund II capital call"):**
```bash
grep -irl "Example Fund II" $E --include="*.md" | grep -v _archive   # find the note(s)
# then read the newest hit + the dossier that covers that matter
```

**Find / verify the source of a claim:** every note has `provenance.ref` (`gmail:thread:<id>`,
`outlook:thread:<id>`, `granola:<id>`, `imessage:<guid>:<chunk_id>`, `whatsapp:<jid>:<chunk_id>`,
`obsidian:<path>`, `claude-chat:<session-id>:c<n>`). Only **gmail** and **granola** have a live connector
to re-fetch from: `get_thread(<id>)` for gmail (the thread id is base-agnostic — hex or decimal),
`get_meeting_transcript` for granola. **outlook, imessage, and whatsapp have NO connector** — outlook is
a manual/local-cache `.eml` export, imessage/whatsapp are local chat-DB extracts — there is no live
re-fetch path for these. To verify one of those, cross-check the note against its dossier / other notes
on the same matter, or (if you have engram-repo access) re-run that source's export pipeline. Do this
when a fact is high-stakes or looks stale.

## Delegate for breadth (keep the caller's context clean)

For a broad or multi-hop question, **don't** read many engram files into the current session — delegate
to ONE fresh sub-agent/worker if your client has one (Claude Code: `Agent(general-purpose)`) and keep only its
conclusion; if it has none, do the index-then-drill read yourself and keep only the conclusion. Prompt:
```
"Answer <question> from the owner's engram store at $ENGRAM_ROOT using
index-then-drill + grep (procedure & recipes: the engram skill's SKILL.md). Return a concise answer,
the note paths you used, and a freshness stamp. Read-only — do not edit anything."
```
This is the proven pattern (the original store's retrieval evals scored 20/20 WELL this way) and it keeps
whole-domain dumps out of your working context.

## Freshness

For anything recency-sensitive, read `$E/_manifest/sync-state.json` — the per-source watermarks (six
sources: `granola` / `gmail_personal` / `gmail_secondary` / `imessage` / `whatsapp` / `outlook`) are the
last dates synced. Stamp your answer "current through <watermark>". If the gap matters, tell the owner the
store may be behind and that an incremental sync (`$E/_manifest/SYNC.md`) would refresh it before they
rely on it.

## Guardrails

- **Read-only** from any non-engram session. Never write to or commit the store from elsewhere. (Capturing
  NEW knowledge back into engram is a separate, Claude-Code-only flow — `/engram-capture`, one fact per
  call — not this skill.)
- The store holds **no clinical-patient PHI** by design. It DOES hold ordinary business-sensitive / COI
  content (ventures, finances, comp, family) — that's normal *here*. When such a fact feeds an
  **external deliverable** (an outgoing email, a grant, a shared doc), whether to include it is a
  per-deliverable judgment — don't leak COI/financial/family detail into something that leaves the owner's hands.
- Don't fabricate. If engram doesn't answer, say so — never invent a fact or a source. Names resolve through
  `people.md`; unknowns are flagged there, not asserted.
