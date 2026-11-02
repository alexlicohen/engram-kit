---
name: engram-audit
description: >-
  Run the engram FIDELITY audit — sample distilled notes, re-fetch each note's ORIGINAL source
  (Gmail thread / Granola transcript / local iMessage chunk), and adversarially check the note
  against its source for FABRICATION or DISTORTION (not staleness — that's /engram-eval /
  dossier_drift). Use when the owner says "/engram-audit", "audit fidelity", "check the notes against
  their sources", "did the distiller hallucinate", or after a big distillation/sync change. It is an
  INSTRUMENT (LLM-cost, non-deterministic, connector-dependent) — never a gate; do not wire into check.sh.
---

# engram fidelity audit

Measures **distillation fidelity**: does note N faithfully represent its source S? The store's other
instruments cover retrieval (`/engram-eval`), freshness (`dossier_drift.py`), cross-note
CONTRADICTIONS for entity ownership/interest percentages (`scripts/fact_consistency_scan.py`,
deterministic + zero LLM cost — run it before/alongside an audit and feed any flagged conflicts
into your batch, since a contradiction is a fidelity-adjacent defect this tool won't otherwise
catch on its own), and structure/PHI (`check.sh`) — none of those check whether a distilled fact
is *true to its source*. This one does, by re-fetching the source and comparing. It is also the
**remediation tool**: each run surfaces the genuinely-wrong notes to fix (correct-but-unsourced
context imports are left alone).

## Method (index-then-fan-out)

1. **Sample** — `python3 scripts/audit_sample.py --n <N> --scope <all|high-stakes> [--out sample.jsonl]`.
   Deterministic (salted sha256 over path; same args ⇒ same sample). Eligibility has ONE owner,
   `audit_sample.SOURCE_RULES` (each row documents how the source is reached, or why it is
   excluded): gmail (secondary mailbox excluded — unreachable), granola and **outlook** by default,
   iMessage/WhatsApp via **`--include-chatdb`** (`--include-imessage` still works as a deprecated
   alias and has the same effect — it always covered both). The sampler RESOLVES a chat-DB note's
   ref to its local `_sources/imessage|whatsapp/<chat>__c<nnn>.txt` chunk, and an outlook note's to
   `_sources/outlook-threads/<thrid>.txt`, emitting that path as `fetch`, so a subagent CAN audit
   those sources now (it no longer has to map the ref itself — that was the old hand-verify-only
   limitation). Notes whose local file no longer exists — incremental-sync chunks are not
   retained — are DROPPED with a stderr WARN rather than emitted with a null source. Obsidian is
   verbatim (excluded). **`emitted=0` is never silent**: if the selectors matched real notes but
   eligibility dropped them all (or nothing resolved), the sampler prints a loud WARN naming the
   excluded sources and exits non-zero — do not read that as a clean audit. `--sources granola,imessage` restricts to one distiller's output (each
   distiller has its own FIDELITY RULES block, so a per-distiller rate is what actually drives a
   prompt fix). Pass `--exclude prior.jsonl` to skip already-audited notes across runs.
   `--scope high-stakes` restricts to finance/medical/legal/decision notes.
2. **Split** into per-agent batch files of **≤7 notes** (`sample.jsonl` → `batches/batch_NN.jsonl`).
3. **Fan out**, ONE agent per batch, each reading the shared instruction file (below) + its batch.
   **RATE LIMIT: launch ≤~4-5 agents concurrently** (7 Gmail fetches × many agents trips the
   server-side throttle — a 15-wide launch failed ~40% of batches). Run in small waves; re-run any
   batch that returns a rate-limit error (same batch file).
4. **Aggregate** — collect the JSON verdicts; tally PASS / MINOR / FABRICATION; list every
   FABRICATION + the flagged MINORs with a wrong specific.
5. **Report** — write `_manifest/audit-reports/<date>.md` (verdict counts, defect rate, the fix-list,
   and the dominant patterns). Diff against the prior report if one exists.
   Then feed the FABRICATIONs into the corrections loop so the NEXT sync's distiller prompts carry
   them as adversarial reminders (Feature C): save the step-4 structured JSON verdicts to a file and
   run `python3 scripts/audit_to_corrections.py --in <verdicts.json> --audit-report
   _manifest/audit-reports/<date>.md`. It appends one `open` entry per FABRICATION to
   `_manifest/corrections/{gmail,granola,imessage,outlook,whatsapp}.jsonl` (idempotent — dedupes on a
   deterministic id; a FABRICATION on a source outside `log_correction.VALID_SOURCES` is skipped). Hardening a correction into a distiller's
   FIDELITY RULES block, and flipping its ledger `status` to `applied`, stays a manual human step.
6. **Fix** — correct the FABRICATIONS (source-verify each before editing — an agent's summary can be
   wrong; re-fetch and read the exact source). Deliberate note-body edits trip check.sh's SUSPICIOUS
   tell by design. Ask the owner to adjudicate any "correct-but-unsourced" specific you can't confirm.
   **If you fan out parallel fixer agents, do NOT have each run check.sh** — it regenerates the shared
   index tree (deletes+recreates every `_index.md`), so concurrent runs race and crash. Fixers edit
   only; the orchestrator runs check.sh ONCE after all edits land.

## Auditor instruction template (give each fan-out agent, verbatim)

> You are an engram fidelity auditor (repo $ENGRAM_ROOT/). For EACH note in your batch
> ({path, source, fetch}): `Read` the note; re-fetch its source — gmail → ToolSearch
> `select:mcp__claude_ai_Gmail__get_thread` then `get_thread(threadId=<fetch>,
> messageFormat="FULL_CONTENT")`; granola → `select:mcp__claude_ai_Granola__get_meetings` then
> `get_meetings(meeting_ids=[<every fetch id in the batch>])` (ONE call, max 10) and grade against
> the `<summary>` + `<private_notes>` blocks — **these, not the transcript, are what the distiller
> actually read** (`_granola-pending/granola_new.mjs`), so grading against
> `get_meeting_transcript` over-flags; pull the transcript only as a tiebreaker on a genuinely
> ambiguous claim. NB a due date the SUMMARY itself computed ("Due: ~2026-03-09") is
> source-supported — not an inferred date. **BUT where the note and the summary CONFLICT, pull the
> transcript and let it decide: the summary is an LLM artifact with its own measured failure modes
> (see "The granola summary is not ground truth" below), and a prior fix pass may already have
> corrected the note against the transcript.** imessage/whatsapp → `fetch` is already a local chunk
> path; just `Read` it (`[YYYY-MM-DD HH:MM] <sender>: <text>`, oldest→newest, `Me` = the owner; one
> chunk legitimately yields several notes, so judge only the claims YOUR note makes).
> Compare every MATERIAL claim (participants,
> dates, amounts, names, addresses, decisions) to the source. **Also check sideways, not just
> back**: for any numeric or named fact tied to a specific entity (an LLC's ownership %, a person's
> employer/brokerage, an address) — quickly grep for that entity elsewhere in the store
> (`grep -rn "<entity>" --include="*.md" .` or check the relevant `dossiers/*.md`) and flag a
> mismatch as its own issue, same severity rules as below. This is a real, found defect class (an
> ownership-split mismatch: two notes giving the same holding different percentages) distinct from source-fidelity — it's two notes disagreeing with
> each other, not a note disagreeing with its own source. Be skeptical. Grade:
> **PASS** (all grounded) · **MINOR** (small imprecision, OR a specific that is correct but not in
> THIS source — the store legitimately imports true context; grade MINOR not FABRICATION unless it's
> actually wrong) · **FABRICATION** (invented or materially WRONG fact — wrong amount/date/category/
> attribution, or thread over-anchored to a wrong subject) · **TOOL_FAILURE** (couldn't reach the
> connector — say so; never guess a verdict). Return RAW JSON: `[{path, source, verdict,
> issues:[{severity, note_claim, source_says, kind}], one_line}]`.

## What "fidelity" defects look like (from the 2026-07-04 pilot)

The dominant defect class is **not** random hallucination — it's three systematic distiller
behaviors (now guarded against in the distiller prompts' FIDELITY RULES):
1. **Over-anchoring / external-context injection** — importing an address / surname / carrier / date
   not in the source. USUALLY correct (helpful) → MINOR; occasionally wrong → FABRICATION.
2. **Inferred/computed dates** — turning "Monday"/"next week" into a wrong calendar date.
3. **Speaker/category misattribution** — who said/decided it; "Preferred Return" vs "Return of
   Capital"; a schedule's on- vs off-days.

Random sample ≈ 1-in-5 material defect; **high-stakes financial notes are far cleaner (~3-5%)** —
the numbers are near-uniformly verbatim; the errors are the tail above.

## The granola summary is not ground truth (measured 2026-07-20)

Auditing granola produced a **67% false-positive rate** (4 of 6 flags refuted), and all four had one
cause: the Granola AI `<summary>` was wrong and the NOTE was right. Two measured summary failure
modes — treat them as priors:
- **Over-attribution to the note-taker.** The summary routinely credits the owner with things another
  attendee said, and once listed an action item under "Owed by me" that the transcript shows they never
  took on. It also attributed a quote about a hospital that is not even their employer to them.
- **ASR garbling of technical terms**, presented as settled fact. one gene symbol came back as a different gene symbol;
  gene names, acronyms and surnames are all exposed. The `[sp?]` markers only cover some of it.
Two of the four refuted flags would have **REVERTED a verified fix** from an earlier pass. So:
**before "fixing" a note, `git log --follow -p` it and check whether the exact line was already
corrected** — a re-audit that reverts prior work is worse than no audit.

## Auditor failure modes to warn each batch about (measured)

- **Stopping before the source's last message** and calling a supported fact unsupported (found in
  the finance pass's refutals).
- **Missing a specific inside a multi-line message body** — auditors scan the timestamped first line
  of each message and skip continuation lines. One refuted flag ("the chunk never mentions May") was
  wrong because the fact was on line 2 of a message.
- **Flagging a correct people.md resolution** as a fabrication. Registry gaps also masquerade as
  fabrications — check whether `people.md` is the defect before editing a note.

## Guardrails
- READ-ONLY re-fetch; the audit never writes to the store (fixes are a separate, deliberate step).
- A subagent's finding is a LEAD, not truth — source-verify before you "fix" a note. Run an
  adversarial REFUTE pass over every FABRICATION candidate before touching anything: measured
  false-positive rates across passes are 3%, 11%, 50% and 67%, so the verify step is not optional.
- Never let a rate-limited/errored batch silently drop — re-run it; a missing batch is not "0 defects."
- A defect rate is only actionable **per distiller** — `--sources <s>`. The 2026-07-20 pass found
  granola at 2.9% (baseline, left alone) and iMessage at 13.4% (hardened) in the same run; a pooled
  number would have justified the wrong edit on both.
- **Fixing what you measure needs a gate-passing path.** `check.sh`'s precise redistill gate covers
  gmail/granola/outlook/imessage/whatsapp; a body-only fix on those passes, and a title/date/ref
  change is a hard failure by design. If an audit's correct fix requires changing a FROZEN field,
  that is a deliberate act needing a guarded writer — do not work around the gate.
