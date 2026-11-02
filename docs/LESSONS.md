# Lessons from running an LLM-maintained knowledge store

Engineering lessons from the private store this kit was extracted from (several thousand notes, six
ingestion sources, months of syncs and audits). Each one cost something to learn; most are now
encoded as a gate, a test or a prompt block in this kit.

## Measuring fidelity

- **The raw fabrication rate is not the true rate.** First-pass auditor verdicts carried false
  positives anywhere from 3% to 67% depending on source and auditor prompt. Always run an adversarial
  *refute* pass over every FABRICATION candidate before you fix or report anything, and never
  bulk-apply auditor fixes.
- **Rates are per distiller; never pool them.** Measured on the original store: meeting transcripts
  ~3%, email ~11%, chat ~13% of audited notes with a fabrication before hardening; a work-email
  back-catalog under 2% but 3 of 16 on its first fresh waves (open questions written up as decisions).
  A pooled number hides the source that needs work.
- **A hardened prompt is not a proven one.** Every sync gets a spot-check of its new notes; that is
  the only routine audit, and it is what catches a regression. One targeted fidelity rule took a
  distiller's fresh-wave rate from roughly 20% to 1 in 28 — and only the paired before/after sample
  showed it.
- **The LLM summary is not ground truth; the transcript decides.** Most meeting-note defects were
  inherited from the meeting tool's own AI summary (wrong owner or recipient, speaker swaps, a proposal
  recorded as a decision, computed dates). A verifier that reads the transcript caught 22 of 24 known
  defects with 0 harmful edits in 99 changes.
- **Two-person meetings are the highest-risk shape for owner reversal.** With only two candidates and
  the note written from the owner's side, an unattributed task collapses onto the owner. An unowned item
  must stay UNOWNED.
- **Registry gaps masquerade as fabrications.** A "wrong name" is often a person the registry does not
  know or knows under another spelling. Check the registry first; if the registry is wrong, fix the
  registry, not the note.
- **A re-audit can revert a verified fix, and a prior fix can itself be the defect.** Read the note's
  history (`git log --follow -p`) before re-editing a note someone already corrected.
- **Tally from disk, not from a workflow's summary.** Per-item result files are the record; a
  workflow's self-reported totals under-counted.

## Guards and gates

- **Guards fail narrow in the dimension they name and wide in the one they don't.** Three separate
  misses had the same shape: a check that verified the field it was written for and was blind to
  another operation producing the same trigger (an in-place edit was gated while a *move* that
  re-stamped the same frozen fields passed untouched). For every guard ask: does it verify the
  INTENDED change, and what else produces its trigger condition?
- **Green tests prove nothing until a test fails on the pre-fix code.** Write the regression test
  first, watch it fail on the old code, then fix. `scripts/mutate_check.py` automates the question
  "would this suite notice?".
- **A green gate over a blind spot is worse than no gate**, because it is read as coverage. When a gate
  changes, prove it both ways: it passes the sanctioned change and still fails the unsanctioned one
  (`--no-overrides`).
- **Exceptions need a ledger, a writer and an expiry.** Frozen-field corrections go through exactly two
  guarded writers into an append-only ledger whose entries are live only until the commit that lands
  them. An exemption you can widen by editing a list will be widened.
- **Fail closed.** A scanner that cannot build its denylist, a contract whose sha does not match, a
  render that looks truncated: each refuses and exits non-zero rather than degrading to "clean".
- **Put guards where the runtime can enforce them.** The distiller runtime had no filesystem, so a
  guard written there was decoration; all guards moved to Python steps around it.
- **Don't spec a fix by analogy to another writer.** Two writers that look alike had different
  contracts; check the live data each one touches.

## Pipelines

- **Make every long job resumable from disk.** Done-ness is derived from on-disk notes and markers, never
  from an agent's "done" report, so an interrupted run resumes and a re-run duplicates nothing.
- **Pin inputs by content, not by argument.** A wave file named in a launch argument silently failed to
  arrive and the distiller fell back to a default full-history wave. A one-shot on-disk contract (count +
  sha256 + mode, consumed atomically) closed it.
- **Watermarks come from what was written, never from today().** A watermark bumped to the run date skips
  anything that arrived during the run.
- **Order matters around commits.** Compute "what grew" and bump watermarks from the pre-commit state;
  recomputing after the commit sees the just-written notes as growth.
- **One writer at a time.** Concurrent distillers across *different* sources are fine when verification
  is scoped by ref prefix; two writers on one source are not.
- **Same-subject items processed by one worker can cross-contaminate.** Keep unrelated items in separate
  workers, and look for a provenance ref copied from a neighbouring record (a filename suffix that
  disagrees with the ref is the tell).

## Store design

- **Optimise for agent legibility.** The main reader is an agent with grep. Index-then-drill, stable
  refs and a one-file JSONL index beat semantic search for this workload and cost nothing to keep fresh.
- **One owner per decision.** The domain list, tag vocabulary, frontmatter parser and ref extraction each
  live in one place and are imported everywhere. Every pasted copy eventually drifted.
- **Commit the derived views and gate on their freshness.** A clone is self-describing, and a diff shows
  exactly what a change did to the indices.
- **Weight by attention, not by note count.** Note volume tracks source chattiness, not importance; any
  aggregate analysis needs an explicit weighting.
- **Never auto-merge across sources.** A meeting and an email about the same matter are flagged as an
  overlap for review; silent merges destroy provenance.
- **Exclude sensitive data at ingest, then audit anyway.** Clinical-patient data is screened out before
  distillation and the whole store is scanned in CI. Scan file and catalog names too, not only note
  bodies: an identifier in a filename is as much a leak as one in the text.
- **Keep instruments out of the gate.** Audits and evals are expensive and non-deterministic; a gate
  must be cheap, deterministic and offline.
