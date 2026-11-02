export const meta = {
  name: 'outlook-distill',
  description: 'Distill extracted Outlook (work-email) threads into engram atomic notes — PHI fail-closed',
  phases: [
    { title: 'Setup', detail: 'read the consumed wave contract' },
    { title: 'Distill', detail: 'one worker agent per line-slice of threads' },
  ],
}

// Robust to `args` not threading through a re-launch: fixed project paths are defaults.
const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
const THREADS = A.threadsDir || (ROOT + '/_sources/outlook-threads')
const PEOPLE = A.peopleFile || (ROOT + '/_manifest/people.md')
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode) and
// atomically claimed BEFORE this distiller launched. The Workflow runtime is pure JS (no fs, no
// dynamic loader), so the guard lives in Python; a Setup agent reads the consumed contract.
const SOURCE = 'outlook'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
// Feature C (fidelity corrections loop): the Setup agent also reads this pre-launch brief (open
// audit corrections for this source, rendered by scripts/corrections_brief.py --source outlook) and
// distillPrompt() folds any bullets in as extra adversarial reminders. May be absent/empty.
const CORR_SOURCE = 'outlook'
const CORRECTIONS_BRIEF = ROOT + '/_sources/' + CORR_SOURCE + '.corrections-brief.txt'
// Controlled tag vocabulary. SINGLE OWNER: _manifest/constants/tag_vocab.json (CLAUDE.md, "one
// owner per decision"). The Setup agent reads it at run time and hands the array back as
// `tag_vocab`; the prompt's allowed-tag list is built from THAT. Never paste a copy of the vocab
// into this file -- a hardcoded list silently drifts from the owner, which is exactly how the
// distillers started emitting off-vocab tags.
const TAG_VOCAB_FILE = ROOT + '/_manifest/constants/tag_vocab.json'
// Whether this work mailbox is at a clinical institution (patients). true = the PHI block below is
// stated at full strength (the canonical case: a hospital-employed clinician-researcher). The PHI
// rule binds either way; this only sets how hard the prompt leans on it.
const CLINICAL_EMPLOYER = A.clinicalEmployer !== undefined ? !!A.clinicalEmployer : true
const BATCH = A.batch || 12

const CONTRACT_SCHEMA = {
  type: 'object',
  properties: {
    wave: { type: 'string' }, expected_count: { type: 'integer' },
    source: { type: 'string' }, mode: { type: 'string' },
    today: { type: ['string', 'null'] },
    corrections: { type: ['string', 'null'] },   // Feature C: past-mistake reminders (may be empty)
    corrections_status: { type: 'string', enum: ['ok', 'none', 'error'] },  // Feature C integrity verdict
    corrections_lines: { type: ['integer', 'null'] },  // contract corrections_brief.lines, copied verbatim
    corrections_error: { type: ['string', 'null'] },   // why the integrity check failed (status 'error')
    tag_vocab: { type: 'array', items: { type: 'string' } },  // _manifest/constants/tag_vocab.json .tags, verbatim
  },
  required: ['wave', 'expected_count', 'source'],
}

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    threads: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          thrid: { type: 'string' },
          status: { type: 'string', enum: ['done', 'dropped'] },
          note: { type: ['string', 'null'] },
          domain: { type: ['string', 'null'] },
          title: { type: ['string', 'null'] },
          reason: { type: ['string', 'null'] },
          new_people: {
            type: 'array',
            items: {
              type: 'object',
              properties: { name: { type: 'string' }, email: { type: ['string', 'null'] } },
              required: ['name'],
            },
          },
        },
        required: ['thrid', 'status'],
      },
    },
  },
  required: ['threads'],
}

function distillPrompt(start, end, today) {
  return `You distill EMAIL THREADS from the owner's WORK (Outlook) mailbox into engram atomic notes. engram is the owner's private markdown knowledge base. Conventions:
${ROOT}/CLAUDE.md and ${ROOT}/_manifest/schema.md.
First, read the people registry once: ${PEOPLE} (resolve participants to canonical names).

SLICE GUARD: your slice is EXACTLY lines ${start}-${end} of the wave = ${end - start + 1} record(s). Process ONLY those lines — other workers own the rest of the file, and a line processed outside your slice produces duplicate notes/markers (this has happened: one worker read a whole 45-line wave). Confirm the sed output has ${end - start + 1} lines before starting; never cat/read the whole wave file.
YOUR WORK-LIST: run \`sed -n '${start},${end}p' ${WAVE}\` — that prints your thread records, one JSON
object per line, each with a "thrid" field. Process EACH line (one note per thread). For a thread,
read its extracted text at ${THREADS}/<thrid>.txt (header with subject/labels/participants/dates,
then the messages as "--- MSG n ---" blocks in CHRONOLOGICAL order — MSG 1 is the OLDEST, the LAST
block is the NEWEST — while INSIDE any one block the sender's new text is on top and the quoted
history sits below it; see FIDELITY RULE 6), then decide KEEP or DROP.

★★★ PHI IS FAIL-CLOSED${CLINICAL_EMPLOYER ? ' — THIS IS WORK EMAIL FROM A CLINICAL INSTITUTION' : ''} ★★★
engram's ONE hard rule: NO identifiable clinical-patient PHI in the store.${CLINICAL_EMPLOYER ? ' Much of this corpus may be clinical.' : ''}
When in doubt, DROP — a lost admin note is cheap; a leaked patient identifier is not.
DROP with status:"dropped", reason:"phi", and WRITE NO NOTE when a thread is fundamentally about an
IDENTIFIABLE clinical patient. Write a CONTENT-FREE drop marker with the Write tool to
${ROOT}/_sources/outlook-drops.d/<thrid>.json containing EXACTLY {"thrid":"<thrid>","reason":"phi"} —
the thrid + the literal word "phi" ONLY, NEVER any patient name/MRN/detail (this durably skips the
thread on re-sync so patient content is not re-processed; the marker itself carries no PHI). Cases:
  - an Epic / in-basket / MyChart forward or a "msg <name>" / "staff message" / result / refill /
    prior-auth notification about a patient;
  - a patient NAME as (or in) the subject with clinical context (e.g. a nurse/coordinator emailing
    the owner about a named patient), even if the body is short;
  - any message carrying an MRN, DOB tied to care, a census/sign-out/weekend-service roster, a
    consult/patient list, a clinical note, or case detail (diagnosis/treatment/imaging/EEG findings)
    for an identifiable individual.
If a KEEP-worthy administrative/research thread contains an INCIDENTAL clinical mention, DE-IDENTIFY
it: never write a patient name/initials/MRN/DOB or case specifics — generalize ("a patient", "a
family"). The owner's OWN kids / colleagues' ordinary life events are NOT patient PHI — keep as normal
context.

ALSO DROP (status:"dropped", set reason; these are non-durable) — write a durable drop marker with
the Write tool to ${ROOT}/_sources/outlook-drops.d/<thrid>.json containing exactly
{"thrid":"<thrid>","reason":"<short reason>"}:
  - pure logistics/scheduling with no lasting info, one-word replies ("thanks","got it","will do"),
    receipts/confirmations, automated mail with a trivial human reply, calendar invites only,
    newsletters/digests/announcements/mass-list blasts, IT/facilities/HR admin noise.

KEEP (status:"done"): write ONE markdown note. Compress to atomic facts — DISTILLED, not verbatim.
Never fabricate; if unsure, omit. This store is single-user: COI/business/financial/venture/faculty
content is NORMAL — keep it. Substantive work to KEEP: grants (applications/biosketches/study
section/IRB/COI), research/lab coordination, multi-site project operations, committee/faculty
decisions, mentorship, collaborations, manuscripts/peer review — MINUS any patient PHI (de-identified
per above).

FIDELITY RULES (audited — the top defect class; do NOT break them):
1. Assert ONLY facts present in THIS source OR resolvable from people.md. You MAY resolve a
   first-name mention to its canonical people.md name and MAY use that person's people.md-registered
   title/affiliation. But NEVER invent a specific that is in NEITHER the source NOR people.md — no
   invented ACRONYM EXPANSION (e.g. writing out a committee's initials as a full name the source
   never gives and the registry does not define), no invented org full-name, address, grant number, or date. If a
   specific is not in the source and not a people.md fact, OMIT it (a vaguer true note beats a precise
   wrong one). And NEVER MERGE separately-stated facts into a false attribution — e.g. if the source
   says "reviewed by the partner-site team" and, in a separate sentence, "thanks to Ann, Pat, Lou",
   do NOT imply Ann/Pat/Lou ARE the partner-site team. Invent no other specifics.
2. Dates: use ONLY dates explicitly written in the source. Never compute or guess one.
3. Attribution is load-bearing: preserve WHO said/decided each thing and the EXACT label/category of
   any value. Do not flip a speaker or merge two distinct categories. In lab meetings, do NOT credit
   a mentee's task/finding to the owner/the PI (owner reversal is a known defect).
4. Do NOT over-anchor a whole note to one detail unless the source makes it the subject.
5. PARTICIPANT ISOLATION (a measured defect: two sibling notes had byte-identical, wrong participant
   lists, both including a person absent from either real source): derive \`participants:\`
   ONLY from the headers/body of the CURRENT thread you are distilling. Never reuse, copy, or infer a
   participant list from a similarly-named or related thread (e.g. a recurring meeting's prior/next
   recap) — even if the two threads look alike or share most attendees, each note's participants must
   come from ITS OWN source text, checked independently every time.
6. Do NOT state a question/decision as RESOLVED unless the source shows it actually being answered.
   If a message asks a question and the reply addresses something else (or doesn't reply at all),
   record the question as OPEN (an "Open questions" section or explicit "not resolved in this thread"
   framing) — never infer or assume the asker's preferred/likely answer as the outcome.
   AND THE REVERSE (measured: 2 of 7 sampled notes — FALSE-UNRESOLVED): the newest text in
   the thread often ANSWERS or DISCHARGES a request made earlier ("Can you send your biosketch?" ...
   newest text: "See attached"). Before writing an action item or open question, check the newest text
   for its resolution — an already-discharged ask written as open leaks a phantom to-do into the task
   bridge. Both directions of resolution status must match the source.
   **THIS FILE HAS TWO ORDERING AXES AND THEY RUN OPPOSITE WAYS — GET BOTH RIGHT (mixing them up is
   how BOTH directions above go wrong).** Check the MSG timestamps; do not assume reading order.
   (a) ACROSS the "--- MSG n ---" blocks the file is CHRONOLOGICAL: MSG 1 is the OLDEST and the LAST
       block is the NEWEST, so the LAST block is the current state — the outcome. Read it before you
       write anything as open, pending, awaited, or unresolved.
   (b) INSIDE one block the order is INVERTED: the sender's new text is at the TOP, and everything
       below the "From:/Sent:/To:/Subject:" separator lines is QUOTED HISTORY, not the outcome. A
       one-message thread is therefore entirely top-newest.
   So the newest text in the file is the TOP of the LAST "--- MSG n ---" block. Never record as open,
   pending, or awaited anything that text already answers, delivers, or closes; and never take a
   QUOTED earlier message as the current state — a quoted line is always EARLIER than the block
   quoting it, even though it sits lower on the page. Before writing "confirmed", "agreed",
   "approved", "moved", or "decided", verify that a message with a LATER timestamp actually answers
   the earlier one. (Measured defects: a 09:15Z quoted line was read as answering a 09:40Z question,
   inventing a confirmation that never happened; and the newest text delivered a full recount, counts
   and all, while the note still wrote that recount up as an open action item because only the quoted
   earlier email had been read.)
   Also: NEVER expand an acronym the source does not expand (write "the XRF", not "XRF (extended
   reporting form)") — check people.md/store context only for PEOPLE, not for coined expansions.
   And populate participants: with ALL human To/Cc parties of the thread (resolved via people.md),
   not just the people the body narrates — under-population was found on 5 of 7 sampled notes.
   This is one instance of SPEECH-ACT INVERSION — the TOP measured defect class on this source (half
   of the fabrications in one back-catalog audit). Never collapse the DIRECTION of an exchange: a REQUEST is
   not a grant ("can the report slip a week?" is NOT a moved deadline), a WANT is not a decision
   ("reviewers want X added to the checklist" is NOT "X was added"), a QUESTION is not a
   confirmation, the person who ASKS for a deliverable is NOT its owner (they are the requester), the
   person keeping someone in the loop is not the one holding the role, and an accepted suggestion must
   not be recorded as rejected or vice versa.
7. Action-item RELEVANCE — an item owned by someone else belongs in ## Action items ONLY if the owner
   needs its outcome delivered to them or it blocks their own next step. A task purely between other people (never
   routing through the owner), or internal admin/logistics bookkeeping — a billing-appeal process, calendar
   notation, "note X in the system/sheet" — needing no follow-up from the owner, is NOT the owner's action item:
   put it under ## Key discussion if it matters, otherwise omit. This is about WHETHER TO LIST, not
   attribution: it does NOT override rule 3 (a listed item keeps its true owner). When genuinely
   unclear, KEEP it — a stray reminder is far cheaper than silently dropping something the owner needed.
8. FRESH-WAVE CLASSES (measured: ~20% of fresh-wave notes carried one of these, vs ~2% on the
   back-catalog — do NOT repeat them):
   (a) RELATIVE TIME binds to the SENT timestamp of the message it is written in, never to the note
       date or another message: "yesterday" in a 6/14 email is 6/13; "the draft is due Thursday" sent on
       a Tuesday is the COMING Thursday, not a past one. When unsure, keep the source's own words
       ("the Thursday after this exchange") instead of a date (rule 2).
   (b) STAY INSIDE THIS THREAD: never import a date, figure, or fact from another note in the store or
       from a related thread, even one this thread mentions. If the thread does not say it, omit it.
   (c) PROPOSALS ARE NOT DECISIONS: text framed as "first thoughts", "proposal", "for discussion",
       "draft", "this is only a straw man", or "once this is discussed and decided" goes under
       Key discussion (or a "Proposed:" line), never under ## Decisions — unless a LATER message in the
       thread records it being agreed.
   (d) FUTURE OR CONDITIONAL IS NOT DONE: "once I hear back", "will send", "plan to", "should"
       mean it has NOT happened. Write it as done only if a later message shows it done. Likewise, a
       range the source leaves open ("a 2-week vs 3-4 week turnaround ... either could work") stays open:
       never pick one value for it.
   (e) A NAME THE THREAD USES FOR AN ORG, PLATFORM, OR PRODUCT stays that name even when typed in
       lowercase: if the thread is about Acme vs. Summit, "the summit interface" means Summit's. Never
       turn a proper noun into a common adjective.
WRITE SCOPE: create ONLY the one new note file for your thread (or the drop marker). NEVER edit,
rewrite, or append to any existing file in the store — not to add a back-link, not to fix another
note. A sync that touches an existing note fails verification and is reverted.
${CORRECTIONS ? `\nPast mistakes to avoid on THIS source (from fidelity audits — REAL defects found in earlier notes from this same connector; do NOT repeat them):\n${CORRECTIONS}\n` : ''}
NOTE FORMAT (exact frontmatter, then body):
---
title: "Concise human title (no Re:/Fw:)"
date: YYYY-MM-DD            # the thread's most representative date (decision/last substantive msg)
aliases: [...]              # 1-3 alternate titles / key entities for retrieval; [] if nothing obvious
tags: [...]                 # ONLY from the CONTROLLED TAGS list below — no other word
domain: <one domain>        # exactly one of the dirs below; ambiguous -> unsorted
subpath: null               # or grants|papers|lab-ops (research) or travel (personal)
type: event                 # ALWAYS "event" for these (dated email threads)
source: outlook
provenance:
  ref: "outlook:thread:<thrid>"
  ingested: ${today}
  method: distilled
participants: [Canonical Name, ...]   # humans only; resolve via people.md; omit automated senders
status: done
related: []                 # ALWAYS [] — see LINKING RULE
supersedes: []              # ALWAYS [] — the synthesis pass sets supersession
superseded_by: []           # ALWAYS []
---

## Summary
<2-4 sentence what-this-thread-is-and-why-it-matters>

## Decisions            # omit section if none
- ...
## Action items         # omit if none; "- <task> — <owner> (due <date>)"
- ...
## Key discussion       # the substantive points, distilled
- ...
## Open questions       # omit if none
- ...
## References           # omit if none; links, doc names, attachment filenames mentioned

DOMAINS (pick one, write file to ${ROOT}/<domain>/<YYYY-MM-DD>-<slug>.md; the list's single owner is
  ${ROOT}/_manifest/constants/domains.json): research (subpath grants|papers|lab-ops), work, ventures,
  finance, personal (subpath travel), health, infra, literature, unsorted.
  Route by CONTENT, not the sender domain alone (almost everything here shares one employer domain).
  Grants/papers/lab/academic -> research. The day job's operations, multi-site projects, and
  department/faculty matters with NO patient PHI (division updates, scheduling, teaching, service
  logistics) -> work. A company the owner runs or advises -> ventures. Real-estate/LLC/trust/tax/
  investment -> finance. Family/school/social/travel -> personal. Store/tooling infrastructure ->
  infra. Genuinely unclear -> unsorted (add tag "flagged").
  DUAL-DOMAIN: file under the PRIMARY domain, add the tag "overlap", name the second domain in the
  body. Do not duplicate the note.

CONTROLLED TAGS — the COMPLETE allowed vocabulary is the space-separated list below, read for THIS
RUN from ${TAG_VOCAB_FILE} (its single owner). A tag is valid ONLY if it appears in this list,
spelled exactly as written (all lowercase, hyphenated). Choose every tag from it and emit nothing
else — no invented word, no singular/plural or spacing variant, no category label ('finance',
'email', 'misc', 'ops' are NOT tags):
  ${TAG_VOCAB}
NEAREST-MATCH RULE: if the concept you want to tag has no word in that list, do NOT coin one. Use the
closest tag that IS in the list, or emit no tag for that concept at all — it still lives in the
note's title and body, which is where it is searchable. ('neuroimaging', 'operations', 'trial-ops',
'funding', 'rent', 'cash-flow', 'insurance' and 'appointment' are real tags earlier runs invented;
each one is a lint failure and is invisible to every tag-based query.) A missing tag always beats a
made-up one. Never add a word to the vocabulary — that is the owner's call, made in tag_vocab.json.
Always include "correspondence" (these are emails) plus the domain tag.

LINKING RULE: leave related, supersedes, superseded_by ALL as [] — do NOT add wikilinks. All
cross-linking is computed later by the synthesis/backlinks pass. Guessed links break the store.

FILE NAMING: slug = lowercase kebab of the title, concise. To avoid collisions across parallel
workers, the filename MUST end with EXACTLY the last 6 characters of the thrid (the thrid is a 12-hex
string; take its final 6 chars, NEVER omit them):
\`${ROOT}/<domain>/<YYYY-MM-DD>-<slug>-<thrid_last6>.md\` (e.g. thrid 0a1b2c3d4e5f -> suffix
3d4e5f -> 2026-06-25-division-updates-3d4e5f.md). Create the file with the Write tool; do not print it.

RELIABILITY (CRITICAL): create every KEEP note with the **Write tool** — NEVER bash/cat/heredoc/
printf. Only report status:"done" for a thread once its file is actually written. NEVER report "done"
for a note you did not write. Process threads one at a time; finish writing EACH file before moving
on. If a Write fails, retry once. The count of "done" entries you return MUST equal the number of
files you actually created.

PARTICIPANTS: map emails/names to canonical names via people.md. Any human sender/recipient NOT in
people.md -> include their display name in the note AND report them in new_people (name + email). Do
NOT report a patient as a person. Do not invent relationships.

Return via StructuredOutput with its 'threads' argument set to the ARRAY itself (never { threads: [...] } nested inside it): [ { thrid, status, note: "<relpath under root or null>", domain, title,
reason: "<drop reason or null>", new_people: [{name,email}] } ] — one entry per thrid line.`
}

// ---- run ----
phase('Setup')
const cc = await agent(
  `Read the JSON file at ${CONTRACT} with the Read tool. It is a wave contract that ` +
  `scripts/resolve_wave.py already validated (line-count + sha256 + mode) and consumed. Return ` +
  `EXACTLY its fields as JSON: {"wave": <the "wave" string, verbatim>, "expected_count": <the ` +
  `"expected_count" integer>, "source": <the "source" string>, "mode": <the "mode" string>}. Copy ` +
  `the values verbatim; do not recompute or invent. ALSO run \`date +%Y-%m-%d\` with the Bash tool ` +
  `and add its output as "today" (a fallback used only if the workflow was launched without ` +
  `args.today). ALSO run the CORRECTIONS-BRIEF INTEGRITY CHECK (Feature C). Read the ` +
  `contract's "corrections_brief" field: it is either null/absent (no brief is pinned for this ` +
  `wave) or an object {"path": <path, relative to ${ROOT} unless it starts with "/">, "sha256": ` +
  `<hex digest of the file>, "lines": <integer>}. CASE A — null or absent: return ` +
  `"corrections_status": "none" and OMIT both "corrections" and "corrections_lines"; do NOT read ` +
  `any brief file and do NOT emit an empty value. CASE B — an object: (1) resolve its path and ` +
  `confirm it is exactly ${CORRECTIONS_BRIEF}; (2) Read that file; (3) with the Bash tool compute ` +
  `its digest \`shasum -a 256 <path>\` AND its NON-EMPTY line count \`awk 'NF' <path> | wc -l\` ` +
  `(lines holding at least one non-whitespace char — the brief has NO trailing newline, so a bare ` +
  `wc -l under-counts by one); (4) if the file is missing/unreadable, or the resolved path is not ` +
  `${CORRECTIONS_BRIEF}, or the digest differs from "sha256", or the count differs from "lines", ` +
  `return "corrections_status": "error" plus "corrections_error": "<what mismatched, naming both ` +
  `the pinned and the computed value>" and OMIT "corrections" — never fall back to using the file ` +
  `anyway; (5) on a full match return "corrections_status": "ok", "corrections_lines": <the ` +
  `contract's "lines" integer, copied verbatim>, and "corrections": <the file's FULL contents, ` +
  `verbatim — never summarized, truncated, reordered or reformatted; the workflow re-counts the ` +
  `lines of what you return and aborts if they disagree>. "corrections_status" is MANDATORY in every case. ` +
  `ALSO read the CONTROLLED TAG VOCABULARY at ${TAG_VOCAB_FILE} with the Read tool. It is a JSON ` +
  `object with a "tags" key whose value is the array of every allowed tag. Return that array ` +
  `VERBATIM as "tag_vocab": every element, in file order, spelled exactly as the file spells it — ` +
  `never add, drop, reorder, re-case, translate or summarize a tag, and never supply the list from ` +
  `memory or from schema.md. That file is the SINGLE OWNER of the vocabulary and the distiller ` +
  `prompt's allowed-tag list is built from what you return, so a paraphrase here corrupts every ` +
  `note this run writes. If the file is missing or unreadable, return "tag_vocab": [] — the ` +
  `workflow aborts, fail-closed. ` +
  `If the CONTRACT file is missing or unreadable, return {"wave": "", ` +
  `"expected_count": -1, "source": "MISSING", "mode": "", "today": null, ` +
  `"corrections_status": "error", "corrections_error": "consumed contract missing or unreadable"}.`,
  { schema: CONTRACT_SCHEMA, label: 'contract', phase: 'Setup' })
if (!cc || cc.source !== SOURCE || !Number.isInteger(cc.expected_count) || cc.expected_count < 0 || !cc.wave) {
  throw new Error(`${SOURCE} distiller: no valid CONSUMED wave contract at ${CONTRACT} — ` +
    `scripts/resolve_wave.py must validate + consume the contract BEFORE this distiller launches ` +
    `(fail-closed; the pure-JS Workflow runtime cannot read files, so the guard runs in the ` +
    `orchestrator). Got: ${JSON.stringify(cc)}`)
}
const WAVE = cc.wave.startsWith('/') ? cc.wave : ROOT + '/' + cc.wave
const count = cc.expected_count
const isISO = (d) => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d)
const TODAY = isISO(A.today) ? A.today : (isISO(cc.today) ? cc.today : null)
if (!TODAY) {
  throw new Error(`${SOURCE} distiller: no valid today date for ingested: (args.today=${JSON.stringify(A.today)}, contract-agent today=${JSON.stringify(cc.today)}) — relaunch with args:{today:"YYYY-MM-DD"}.`)
}
log(`work-list: ${count} threads, batch ${BATCH} (wave ${WAVE})`)
// Feature C: past fidelity-audit corrections for this source, read by the Setup agent from the
// pre-launch brief; distillPrompt() folds them in below (empty string when there are none).
const CORRECTIONS = (typeof cc.corrections === 'string') ? cc.corrections.trim() : ''
// Feature C INTEGRITY (fail-closed). resolve_wave.py pins the brief inside the consumed contract as
// corrections_brief:{path,sha256,lines}; the Setup agent (which HAS tools) verified the file against
// that pin — the body is pure JS and cannot hash a file. What the body CAN do, and does here, is
// re-derive the line count from the text the agent actually handed back, so a Setup agent that
// verified the real file but returned a summarized/truncated/absent brief is still caught BEFORE any
// distill batch launches. "lines" = NON-EMPTY lines — the same definition corrections_brief.py uses
// ([ln for ln in text.split("\n") if ln.strip()]), which is why a bare wc -l does not match.
const CORR_LINES = CORRECTIONS ? CORRECTIONS.split('\n').filter(l => l.trim() !== '').length : 0
const CORR_STATUS = cc.corrections_error ? 'error' : cc.corrections_status
if (CORR_STATUS === 'error') {
  throw new Error(`${SOURCE} distiller: corrections-brief INTEGRITY CHECK FAILED — ` +
    `${cc.corrections_error || '(no detail returned)'}. The consumed contract at ${CONTRACT} pins ` +
    `${CORRECTIONS_BRIEF} by sha256 + non-empty line count; re-run scripts/corrections_brief.py ` +
    `--source ${CORR_SOURCE}, then scripts/wave_prepare.py + scripts/resolve_wave.py, before ` +
    `relaunching (fail-closed: an unpinned or stale brief feeds the distiller the wrong ` +
    `past-mistake reminders).`)
} else if (CORR_STATUS === 'ok') {
  if (!Number.isInteger(cc.corrections_lines)) {
    throw new Error(`${SOURCE} distiller: corrections_status "ok" but corrections_lines is ` +
      `${JSON.stringify(cc.corrections_lines)} (not an integer) — the contract's pinned line count ` +
      `was not returned, so the brief is unverifiable; fail-closed.`)
  }
  if (CORR_LINES !== cc.corrections_lines) {
    throw new Error(`${SOURCE} distiller: corrections-brief LINE-COUNT MISMATCH — the consumed ` +
      `contract pins ${cc.corrections_lines} non-empty line(s) for ${CORRECTIONS_BRIEF}, but the ` +
      `Setup agent returned ${CORR_LINES}. The brief was not handed back verbatim; fail-closed.`)
  }
  log(`corrections brief: ${CORR_LINES} open item(s) folded into the distiller prompt ` +
    `(sha256 + line count verified against the consumed contract)`)
} else if (CORR_STATUS === 'none') {
  if (CORRECTIONS) {
    throw new Error(`${SOURCE} distiller: the consumed contract pins NO corrections brief ` +
      `(corrections_brief: null) yet the Setup agent returned ${CORR_LINES} line(s) of corrections ` +
      `— unpinned prompt input; fail-closed.`)
  }
} else {
  throw new Error(`${SOURCE} distiller: Setup agent returned corrections_status ` +
    `${JSON.stringify(cc.corrections_status)} — the Feature C integrity check did not run, so the ` +
    `brief is unverified; fail-closed (expected "ok", "none" or "error").`)
}

// Controlled tag vocabulary (fail-closed). The Workflow runtime is pure JS — it cannot read
// tag_vocab.json — so the Setup agent (which has tools) read it above; all this body may do is
// shape-check what came back and refuse to distill without it. Deliberately NO fallback list: a
// hardcoded copy would re-create the drift this contract exists to remove.
const TAG_VOCAB_LIST = Array.isArray(cc.tag_vocab) ? cc.tag_vocab : null
if (!TAG_VOCAB_LIST || TAG_VOCAB_LIST.length < 20 ||
    !TAG_VOCAB_LIST.every(t => typeof t === 'string' && /^[a-z0-9][a-z0-9-]*$/.test(t))) {
  throw new Error(`${SOURCE} distiller: the Setup agent returned no usable controlled tag ` +
    `vocabulary from ${TAG_VOCAB_FILE} — got ${JSON.stringify(cc.tag_vocab)}. That file is the ` +
    `single owner of the vocab and the prompt's allowed-tag list is built from it at run time; ` +
    `fail-closed rather than distill against a partial or invented list.`)
}
const TAG_VOCAB = TAG_VOCAB_LIST.join(' ')
log(`tag vocab: ${TAG_VOCAB_LIST.length} controlled tags read from ${TAG_VOCAB_FILE}`)

const nbatches = Math.ceil(count / BATCH)
const ranges = []
for (let b = 0; b < nbatches; b++) ranges.push([b * BATCH + 1, Math.min((b + 1) * BATCH, count)])

phase('Distill')
const results = await parallel(ranges.map(([s, e], bi) => () =>
  agent(distillPrompt(s, e, TODAY), {
    label: `distill:b${bi + 1}/${nbatches} (L${s}-${e})`,
    phase: 'Distill',
    schema: RESULT_SCHEMA,
    agentType: AGENT_TYPE,
  }).then(r => (r && r.threads) || [])
))

const allRaw = results.filter(Boolean).flat()
// Dedupe by thrid (a worker that over-reads its slice double-reports): keep the first 'done' record,
// else the first record seen. Counts below are of UNIQUE threads so `accounted` matches the wave.
const byThrid = new Map()
for (const t of allRaw) {
  const prev = byThrid.get(t.thrid)
  if (!prev || (prev.status !== 'done' && t.status === 'done')) byThrid.set(t.thrid, t)
}
const all = [...byThrid.values()]
if (allRaw.length !== all.length) log(`WARN: ${allRaw.length - all.length} duplicate thread record(s) collapsed — a worker over-read its slice`)
const done = all.filter(t => t.status === 'done')
const dropped = all.filter(t => t.status === 'dropped')
log(`distilled: ${done.length} notes, ${dropped.length} dropped, ${all.length}/${count} accounted`)
return { total: count, accounted: all.length, done: done.length, dropped: dropped.length, threads: all }
