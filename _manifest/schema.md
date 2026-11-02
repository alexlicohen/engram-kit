# engram — schema canon

Source of truth for note frontmatter, the controlled tag vocabulary, file naming, and the
ingestion manifest. Distillation agents and future sessions must conform to this file.

## Frontmatter schema (every note)

```yaml
---
title: "Human-readable title"
date: 2026-06-15            # ISO 8601; the event/thread date, NOT the ingest date
aliases: []                 # alternate names/titles to aid retrieval
tags: [canonical, tags]     # only from the controlled vocab below
domain: work                # exactly one top-level domain dir name, or "unsorted"
subpath: null               # optional sub-cluster, e.g. "grants"/"papers" (research) or "travel" (personal)
type: event                 # event (default) | evergreen — see "Event vs evergreen" below
source: granola             # granola | gmail | obsidian | claude-chat | manual | gdrive | pubmed | imessage | whatsapp | outlook | other
provenance:                 # REQUIRED on every note
  ref: "granola:<meeting-id>"   # stable source pointer; also gmail:thread:<id>, outlook:thread:<id>,
                                # obsidian:<path>, imessage:<guid>:<chunk_id>,
                                # whatsapp:<jid>:<chunk_id> (chat-DB sources),
                                # research:<slug> for infra/hand-authored notes (source: manual),
                                # claude-chat:<session-id>:c<n> for /engram-capture write-back
  ingested: 2026-06-28          # ISO date the note was created
  method: distilled             # distilled | verbatim | captured  (verbatim=Obsidian; captured=/engram-capture)
participants: []            # people present (resolve via people.md)
status: done                # pending | distilled | flagged | done | verbatim
verified: null              # ISO date the owner personally confirmed the facts; else null/absent. Human-set ONLY
related: []
supersedes: []
superseded_by: []
updated: 2026-06-28         # ISO; OPTIONAL, evergreen notes only — last time current-state was refreshed
---
```

### `verified` vs `status`

`status` tracks a note's **pipeline stage** (`pending → distilled → done`) — where it sits in the
ingestion machinery. `verified` is an **epistemic** signal: an ISO date recording that the owner personally
confirmed the note's facts are correct. The two are independent — a note can be `status: done` (fully
distilled and committed) yet unverified (no human has checked it against reality). `verified` is set
**only by explicit human action**; a distiller, redistill script, or any automated agent write path
must never write it. Absent/`null` = not yet human-verified.

### Event vs evergreen (`type`)

- **`event`** (default): a dated, point-in-time record — a meeting, an email thread, a doc. Immutable
  once written; it is the ledger. All distilled source notes are `event`.
- **`evergreen`**: a living **state note** — a topic/entity/project page that synthesizes across many
  event notes and holds *current truth* (e.g. an estate-structure note, a venture dossier, a person's
  profile). Updated in place; carries `updated:`. Dossiers and synthesis notes are `evergreen`.

`supersedes` / `superseded_by` link the timeline: when a later note (or a state refresh) overrides an
earlier fact, link them both ways so the history is reconstructable without deleting anything. Stale =
wrong, especially for finance/legal/estate — prefer marking superseded over silently leaving both.

> **Retired fields (2026-06-28):** `confidential` and `relationship` were removed. engram is a single-user store with no access-control notion of "confidential." The only sensitivity rule is: no clinical patient PHI in the store (see CLAUDE.md).

> **Added fields (2026-07-12):** `verified` — an optional ISO date recording that the owner personally confirmed the note's facts. An epistemic signal distinct from `status` (pipeline stage); see "`verified` vs `status`" above. Set only by explicit human action — never by a distiller, redistill script, or any automated agent write path.

## Controlled tag vocabulary

New tags are added **only** via the synonym→canonical map below (prevents tag sprawl).

- **Domain tags** (mirror top-level dirs): `research` `work` `ventures` `finance` `personal`
  `health` `infra` `literature` — plus research subclusters `grants` `papers` `lab-ops`, and personal
  subcluster `travel`.
- **Type tags:** `meeting` `decision` `commitment` `action-item` `1on1` `committee`
  `interview` `review` `reference` `correspondence`
- **Work / research topic tags:** `lab-meeting` `onboarding` `hiring` `protocol` `dataset` `analysis`
- **Ventures topic tags:** `product` `fundraise` `legal` `contract` `nda`
- **Finance topic tags:** `tax` `insurance` `banking` `budget` `bills`
- **Personal topic tags:** `family` `household` `school` `social` `fitness` `life-event`
- **Control tags:** `flagged` `overlap` `unsorted` `needs-review` `empty-source` `source-limited`

The machine-readable list is `_manifest/constants/tag_vocab.json` (the one owner — lint reads it);
edit that file, then this list, together.
