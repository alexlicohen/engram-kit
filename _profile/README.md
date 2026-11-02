# engram-profile — the attention instrument

`python3 scripts/profile_metrics.py` writes a deterministic metrics pack (markdown + JSON) to
`_profile/metrics/`. It reads only generated artifacts — `_manifest/index.jsonl`, `digests/decisions.md`
and `digests/action-items.md` (generate them first with `scripts/gen_digests.py`),
`digests/open-loops.md`, `_manifest/backlinks.md`, `dossiers/` — never note bodies, no network, no LLM.
It is an INSTRUMENT, never a check.sh gate. Its output describes the owner: keep it out of any shared
copy of the store.

## Why it exists

Note volume is not attention. A domain fed by a chatty connector (statements, notifications) can hold a
third of the notes and little of the owner's engagement. The pack puts six estimators side by side as
SHARES per domain, so the divergence between where the notes ARE and where the attention WENT is visible.
**Any aggregate claim about the store weights by E1–E5, never raw counts (E0) alone.**

## Estimators (M2, the flagship table)

| Estimator | Proxy for | Numerator |
|---|---|---|
| E0 raw-count | note volume | every note |
| E1 sync-time | calendared hours (~45 min per meeting) | `source: granola` or a `meeting` tag |
| E2 decision-density | active cognitive engagement | a note listed in `decisions.md` ∪ `action-items.md` |
| E3 hand-authored | deliberate, self-initiated attention | `source` ∈ {obsidian, manual, claude-chat} |
| E4 human-conversation | relational investment | `source` ∈ {imessage, whatsapp}, or a gmail thread the owner plausibly COMPOSED |
| E5 curation | what the synthesis layer links | a note with curated inbound links (`backlinks.md`) |

Composed-gmail heuristic (E4, frontmatter only): `source: gmail`, 1–2 non-owner participants, a
`decision` or `meeting` tag, and a title that is not transactional (statement, receipt, invoice,
newsletter, alert, payment, order…). Its precision is unmeasured until you calibrate it against a hand
sample; report that error bar with any gmail-derived claim.

## The rest of the pack

- **M1** — count matrices: domain × year, source × year, source × domain.
- **M3** — people dynamics from `participants:` (distinct non-owner correspondents, recurrence).
- **M4** — temporal fingerprints, incl. tag first/last appearance (era-boundary candidates).
- **M5 / bias ledger** — each source's coverage window and source × era counts, plus the
  claim-permission rules every temporal or share claim must cite: cross-era volume comparisons only
  within one source that spans both eras; meeting-time claims only inside the meeting-recorder era; a
  note explosion that starts where a source's coverage starts is connector onboarding, not life change.
- **M6** — path-only worklists for stated priorities and declines (grep hits, not interpretations).

Eras, the meeting length and the heuristics are module-level constants in `scripts/profile_metrics.py`
(re-cut them there). Owner names come from the people.md `self` row; domains from `domains.json`.
