---
name: engram-eval
description: >-
  Run the engram retrieval eval — fan out cold agents over a fixed question bank through the real
  index-then-drill + grep path, each self-grading WELL/PARTIAL/THIN and reporting any stale/superseding
  facts, then write a dated report and diff it against the previous run. Use when the owner says "/engram-eval",
  "run the retrieval eval", "grade engram retrieval", "how well does engram answer", or before/after a
  major store change to measure it. It is an INSTRUMENT (LLM-cost, non-deterministic) — never a gate; do
  not wire it into check.sh.
---

# engram-eval — retrieval quality instrument

Productizes the manual eval that every major engram investment decision has been made off (synthesis vs
coverage, the tier-2 reorg, the dossier sweeps). It measures whether the store *answers* through its real
retrieval path — not whether files exist. Run it on demand, from inside the engram repo.

## Run it

1. **Get the question bank:** `python3 scripts/eval_report.py --list` (the questions in
   `_manifest/eval-questions.jsonl`; the kit ships a small example bank about its seed notes. Replace it
   with questions about your own store that span your domains, people, commitments, a temporal "what
   changed", and a correctness-reconciliation probe).

2. **Fan out — one COLD agent per question, in parallel** (cold = fresh context, no prior knowledge; that's
   the whole point). Each agent gets this prompt (substitute the question):
   > You are a COLD retrieval test of the engram store at `$ENGRAM_ROOT`. No prior context —
   > find everything through the store's own path: read `INDEX.md` → the domain's `_index.md` (a large
   > domain is a map: `### Areas` → a subpath's `_index.md` → per-year leaf) → the note; grep is the engine
   > (`grep`, and `jq` over `_manifest/index.jsonl`); consult `dossiers/`, `digests/`, `_manifest/people.md`.
   > QUESTION: "<question>". Answer as fully as the store allows, citing note paths.
   > Then SELF-GRADE: **WELL** (answered fully, ≤6 hops) · **PARTIAL** (partial/scattered) · **THIN** (couldn't).
   > Then report FRESHNESS/CORRECTNESS: does any note contain a fact NEWER than or CONTRADICTING a dossier
   > you used? List each stale fact → the note path that supersedes it. Read-only.
   > Return: ANSWER, GRADE, HOPS (int), NOTES (paths used), STALE (list of "fact -> note", empty if none).

3. **Collect** each agent's result into a `results.jsonl` (one object per question):
   `{"id":"<qid>","grade":"WELL|PARTIAL|THIN","hops":<int>,"notes":["…md",…],"stale":["… -> …",…],"answer":"one-liner"}`

4. **Write the report + diff:** `python3 scripts/eval_report.py --write results.jsonl`
   → `_manifest/eval-reports/<date>.{md,jsonl}` + prints the grade delta vs the previous run
   (regressed / improved) and any staleness flags.

## Act on the output

- **Any REGRESSED question** (WELL→PARTIAL/THIN vs last run) — a real retrieval regression; investigate.
- **Any staleness flag** — a dossier drifted; reconcile it (`/engram-dossiers`; `dossier_drift.py` is the
  deterministic complement). Fix the note if a fact is wrong (e.g. a corrected street address).
- **Any THIN** — a coverage or tooling gap. NOTE the tripwire: a THIN/PARTIAL caused by a **lexical**
  miss (a synonym or a multi-hop question the grep path cannot bridge, as opposed to a stale fact) is the
  signal to add a BM25-style index; until one is observed, grep is enough (failure-observed, not
  note-count-based).

## Notes

- On-demand only. one cold agent per question is real spend — run it before/after a big change or when steering, not
  routinely. The reports accumulate in `_manifest/eval-reports/` so quality is tracked run-over-run.
- Extend the bank by editing `_manifest/eval-questions.jsonl` (id/domain/question/expect_hint/check); keep
  ids stable so the diff stays meaningful across runs.
