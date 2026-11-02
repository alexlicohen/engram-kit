# engram — index

Map of the store (live tallies: [`_manifest/STATUS.md`](./_manifest/STATUS.md), generated).
Retrieval is **index-then-drill**: read this file, open the relevant domain's `_index.md`, then the
note. Content search is `grep` over notes + `_manifest/index.jsonl` — no embeddings, by design.

## Domains

| Domain | Scope |
|---|---|
| [`research/`](./research/) | Studies, analyses, grants, papers, collaborators |
| [`work/`](./work/) | The day job — teams, programs, committees, operations |
| [`ventures/`](./ventures/) | Companies you run, advise or invest in operationally |
| [`finance/`](./finance/) | Personal finance — tax, banking, insurance, budgets |
| [`personal/`](./personal/) | Family, friends, travel, school, social life |
| [`health/`](./health/) | Your own health and fitness (never clinical patient data) |
| [`infra/`](./infra/) | Computing, tooling, and this store's own meta-notes |
| [`literature/`](./literature/) | Reading notes, one per paper or book |
| [`unsorted/`](./unsorted/) | Fallback for ambiguous items (always tagged `flagged`) |

Edit this table together with `_manifest/constants/domains.json` — that file is the single owner of
the domain list; every script reads it from there.

## Reference

[`glossary.md`](./glossary.md) — acronyms & recurring terms ·
[`dossiers/`](./dossiers/) — cross-cutting one-pagers ·
[`digests/open-loops.md`](./digests/open-loops.md) — curated open commitments ·
[`_manifest/people.md`](./_manifest/people.md) — canonical people/entity registry
