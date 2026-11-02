---
title: "Store conventions — quickstart"
date: 2026-01-10
aliases: []
tags: [infra, reference]
domain: infra
subpath: null
type: evergreen
source: manual
provenance:
  ref: "research:store-conventions-quickstart"
  ingested: 2026-01-10
  method: captured
participants: []
status: done
related: []
supersedes: []
superseded_by: []
updated: 2026-01-10
---

# Store conventions — quickstart

An evergreen note (`type: evergreen`) about the store itself. The binding rules live in
`AGENTS.md`; the design rationale in `docs/ARCHITECTURE.md`.

- Every note: schema frontmatter (`_manifest/schema.md`), a stable `provenance.ref`, one domain.
- Retrieval is index-then-drill: `INDEX.md` → `<domain>/_index.md` → the note.
- `bash scripts/check.sh` must be green before every commit.
