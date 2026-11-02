export const meta = {
  name: 'imsg-distill',
  description: 'Distill iMessage conversation chunks into durable-event atomic notes (sensitive content review-gated)',
  phases: [
    { title: 'Setup', detail: 'count the work-list' },
    { title: 'Distill', detail: 'one worker agent per few chunks' },
  ],
}

const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode)
// and atomically claimed BEFORE this distiller launched. The Workflow runtime is pure JS (no fs,
// no dynamic loader), so the wave-contract guard lives in Python now; a Setup agent reads the
// consumed contract with its Read tool and hands back the pinned wave path + expected count.
// Done-markers are likewise stamped FROM CODE — but now by scripts/stamp_markers.py AFTER this
// workflow (pure JS can't stamp), off the per-chunk RESULT files each agent writes below.
const SOURCE = 'imessage'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
// Feature C (fidelity corrections loop): the Setup agent also reads this pre-launch brief (open
// audit corrections for this source, rendered by scripts/corrections_brief.py --source imessage)
// and prompt() folds any bullets in as extra adversarial reminders. May be absent/empty.
const CORR_SOURCE = 'imessage'
const CORRECTIONS_BRIEF = ROOT + '/_sources/' + CORR_SOURCE + '.corrections-brief.txt'
// Controlled tag vocabulary. SINGLE OWNER: _manifest/constants/tag_vocab.json (CLAUDE.md, "one
// owner per decision"). The Setup agent reads it at run time and hands the array back as
// `tag_vocab`; the prompt's allowed-tag list is built from THAT. Never paste a copy of the vocab
// into this file -- a hardcoded list silently drifts from the owner, which is exactly how the
// distillers started emitting off-vocab tags.
const TAG_VOCAB_FILE = ROOT + '/_manifest/constants/tag_vocab.json'
const PEOPLE = ROOT + '/_manifest/people.md'
const DONE = ROOT + '/_sources/imsg-done.d'
const RESULTS = ROOT + '/_sources/imsg-results.d'       // per-chunk RESULT files (stamp_markers.py reads them)
const SENS = ROOT + '/_sources/imsg-sensitive'          // gitignored holding (review before store)
const BATCH = A.batch || 4

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
    chunks: { type: 'array', items: { type: 'object', properties: {
      chunk_id: { type: 'string' },
      ref: { type: ['string', 'null'] },             // imessage:<guid>:<chunk_id> (verbatim) — audit record
      durable_found: { type: 'integer' },
      notes_written: { type: 'integer' },
      sensitive_held: { type: 'integer' },
      notes: { type: 'array', items: { type: 'string' } },       // EXACT store-note paths written
      sensitive: { type: 'array', items: { type: 'string' } },   // EXACT sensitive-held paths written
      new_people: { type: 'array', items: { type: 'object',
        properties: { name: { type: 'string' }, handle: { type: ['string', 'null'] } }, required: ['name'] } },
    }, required: ['chunk_id'] } },
  },
  required: ['chunks'],
}

function prompt(start, end, today) {
  return `You distill APPLE iMESSAGE conversation chunks into engram atomic notes. engram is the owner's
private markdown KB. Conventions: ${ROOT}/CLAUDE.md, ${ROOT}/_manifest/schema.md. Read the
people registry once: ${PEOPLE} (resolve handles/names to canonical people).

SLICE GUARD: your slice is EXACTLY lines ${start}-${end} of the wave = ${end - start + 1} record(s). Process ONLY those lines — other workers own the rest of the file, and a line processed outside your slice produces duplicate notes/markers (this has happened: one worker read a whole 45-line wave). Confirm the sed output has ${end - start + 1} lines before starting; never cat/read the whole wave file.
WORK-LIST: \`sed -n '${start},${end}p' ${WAVE}\` — each line is JSON {chunk_id, chat_id, guid, is_group,
who, date_start, date_end, fp, n_msgs}. For EACH chunk, read its text at ${ROOT}/<fp> (header, then
lines "[YYYY-MM-DD HH:MM] <sender>: <text>", oldest->newest; "Me" = the owner).

GOAL — extract only DURABLE content as dated atomic notes. Most chunks are mostly chit-chat; it is
NORMAL for a chunk to yield 0 or 1 notes. KEEP (write a note) only for items with lasting value:
  - a plan / decision / commitment / agreement / scheduled thing that matters
  - a life event (birth, move, job, trip taken, health event, milestone, relationship change)
  - a durable fact (address, gift idea acted on, vendor hired, money sent/owed, account/booking)
  - meaningful relationship/family context worth remembering
DROP (write NO note for): greetings, reactions, "ok/lol/omw", logistics with no lasting info,
[attachment]-only exchanges, link-drops with no discussion, emoji.

ONE note per durable item (NOT one per chunk). A chunk can yield several notes or none.

FIDELITY RULES (audited — top defect class; do NOT break them):
A 68-note audit of this source measured a ~13% fabrication rate — about 4x the store baseline,
and the WORST of any source. Rules 3, 4 and 5 below carry the classes it actually found.
1. Assert ONLY facts present in THIS chunk. Never import a specific from outside knowledge —
   addresses, surnames, vendor/company names, amounts, dates. If not in the source, OMIT it. You MAY
   resolve a handle to their canonical people.md name; invent no other specifics.
   The generative behavior to resist is GAP-FILLING: the chat leaves something vague, and the note
   supplies a plausible specific so the story reads cleanly. A vaguer true note beats a precise wrong
   one; when the source is vague, SAY SO. A case of this shape — the source said only "the landlord
   says the lease thing is sorted" (followed by a bare "Edited", i.e. the sender was fixing a typo),
   and the note invented a specific contract term, an "early-termination clause", then titled the
   whole note after it. Texting is terse, elliptical and full of autocorrect; that is NOT license to reconstruct what
   someone meant. Write what was said and note that the specifics are not stated.
2. Dates: use ONLY timestamps/dates in the chunk. Never guess or compute one. Relative day-words are
   the trap — "tomorrow", "this weekend", "next Tuesday" must be read off the SENDING MESSAGE's own
   timestamp, and if that does not pin an exact calendar date, keep the source's own words instead of
   naming a day. E.g. a Friday message reading "Tomorrow am is the bake sale" written up as "the
   sale is on Sunday": the day after a Friday is SATURDAY, and "Sunday" appears nowhere
   in the chunk.
3. ATTRIBUTION AND DIRECTION — the #1 measured defect on this source. Three distinct ways to get
   it wrong, all measured (the examples below mirror them):
   (a) WHO SAID IT. A group chat interleaves several raw handles; a 1:1 still has "Me" (the owner) vs the
       contact. Attribute by the SENDING handle on that exact line, never by who seems most likely to
       have said it. E.g. a reminder sent by one group member's handle credited to another
       member's; a "Maybe the router just needs a restart" from one friend credited to the owner,
       and recast as the owner's own diagnosis.
   (b) DIRECTION. An action between two people has a direction and reversing it inverts the fact.
       E.g. "The portal is not letting Kim add ME as a co-organizer" becoming "the portal
       would not let Kim be added as a co-organizer". Re-read the sentence and
       check who is the actor and who is the object before you paraphrase it.
   (c) WHICH THING. A figure or attribute belongs to the object the source attaches it to. E.g.
       "My old bike cost 400 dollars new ... I gave that to my cousin" having its price and
       condition transplanted onto a DIFFERENT bike, the one that was lent to the owner.
   Also keep the EXACT category/label of any value or schedule (which days are on vs off), and never
   flip which of two results was the positive one. E.g. "the first scale said just over 20 kg,
   so I weighed it again and it is under" became "just over 20 kg on a re-weigh", turning an
   equivocal reading into a confirmed one.
4. NEVER STATE A PLAN, INTENT OR PROPOSAL AS DONE. Chats are full of things being arranged; the
   arranging is not the outcome, and the chunk usually just stops. Do not promote "we should",
   "I'll take care of it" or "can you book it" into an accomplished fact, and do not infer the
   likely outcome. E.g. "We booked the hall ... Can't wait for the party!" written up as the party
   having HAPPENED; "We can send the photos to the printer" written up as the photos having been
   SENT. If the chunk does not show it happening, say it was planned or proposed,
   or say the outcome is not in this chunk.
5. Do NOT over-anchor a whole thread to the FIRST detail named (e.g. an address); anchor to what the
   durable item is actually about. This governs the TITLE too: a title that asserts more than the
   body can support is the same defect (a title claiming a purchase was completed, a booking made).
${CORRECTIONS ? `\nPast mistakes to avoid on THIS source (from fidelity audits — REAL defects found in earlier iMessage notes; do NOT repeat them):\n${CORRECTIONS}\n` : ''}
NOTE FORMAT (frontmatter then body):
---
title: "<concise human title>"
date: <YYYY-MM-DD of the event/decision, from the message timestamps>
aliases: []
tags: [...]                # ONLY from the CONTROLLED TAGS list below — no other word
domain: <one domain>       # route by content: personal (default; subpath travel for trips), finance
                           #   (money/property), health, research, work, ventures, infra, literature;
                           #   ambiguous -> unsorted + tag "flagged" (list: _manifest/constants/domains.json)
subpath: null              # or travel|grants|papers|lab-ops
type: event
source: imessage
provenance:
  ref: "imessage:<guid>:<chunk_id>"   # VERBATIM guid AND full chunk_id from this work-list line
                                       # (e.g. imessage:any;-;+15550101234:90354_c000). NOT c<n> —
                                       # the full chunk_id makes the ref chunk-UNIQUE, which the
                                       # crash-safe resume relies on. All notes from ONE chunk share it.
  ingested: ${today}
  method: distilled
participants: [Canonical Name, ...]   # the owner + the contact(s), resolved via people.md
status: done
related: []
---
## Summary
<1-3 sentences: the durable thing + why it matters>
## Key points        # bullets of the durable detail (omit if the summary suffices)
- ...

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
Always include "correspondence" (these are chat messages) plus the domain tag.

SENSITIVE — review-gate (the owner's rule: ingest all, flag sensitive for review). If a durable item is
HIGHLY SENSITIVE — mental-health/therapy, intimate/relationship-private, legal-privileged, a private
medical condition (of the owner/family/anyone), or private financial detail about another person — do NOT
write it into the store. INSTEAD write the same note file to ${SENS}/<date>-<slug>-im<chat_id>-<h>.md
(same <h> collision-proof hash suffix as the store naming below) AND
append one line to ${SENS}/review.jsonl: {"fp":"<that path>","who":"<who>","date":"<date>",
"reason":"<why sensitive>","title":"<title>"}. The owner reviews these before any go into the store.
Ordinary family/personal content (a parent's hospitalization mentioned in passing, normal relationship
logistics) is NOT sensitive — keep it in the store normally.

PHI: exclude identifiable CLINICAL-PATIENT PHI (name/MRN/DOB+dx of a patient); de-identify incidental
clinical mentions. (If the owner is a clinician they may discuss patients — those are PHI, drop/de-identify.)

FILE NAMING (non-sensitive): ${ROOT}/<domain>/<YYYY-MM-DD>-<slug>-im<chat_id>-<h>.md , where <slug>
is a short kebab-case slug of the title and <h> is a per-note hash suffix that keeps filenames
collision-proof across chunks/notes: <h> = the FIRST 6 HEX CHARS of the SHA-256 of the exact string
"<guid>:<chunk_id>:<slug>" (guid and chunk_id from this chunk's work-list line; <slug> the same slug
used in the filename). Two notes from one chunk have different <slug> → different <h>; two notes from
different chunks never share <h>. If the resulting path already exists, append -2, -3, … Create every
file with the **Write tool** (never cat/heredoc).

DO NOT write any done-marker. Instead, after you finish EACH chunk, write a per-chunk RESULT file
with the Write tool to \`${RESULTS}/<chunk_id>.json\` (ONE file per chunk — never share a file between
chunks, so there is no write race) containing EXACTLY:
  {"chunk_id":"<chunk_id>","ref":"imessage:<guid>:<chunk_id>","notes":[<abs store-note paths>],
   "sensitive":[<abs sensitive-held paths>],"durable_found":<integer>}
(<chunk_id> and <guid> copied verbatim from this chunk's work-list line; every path EXACTLY as you
passed it to the Write tool). The orchestrator runs scripts/stamp_markers.py AFTER this workflow: it
verifies every declared path exists AND that notes.length + sensitive.length == durable_found, then
stamps the code-owned done-marker \`${DONE}/<chunk_id>\` — THAT (never your word) is what makes the run
crash-safe and resumable. A missing/mis-reported path or a count mismatch leaves the chunk PENDING
and it is cleanly reconciled + re-distilled next run, so write the RESULT file accurately and only
AFTER the notes are actually written. If a chunk yields nothing durable, still write its RESULT file
with empty arrays and "durable_found":0 (so it is marked done and never re-processed). Also RETURN the
same per-chunk data in your JSON (below) for the run log.

PARTICIPANTS: resolve handles (phone/email) to canonical names via people.md. A human contact NOT in
people.md -> use the best available name (or the handle) AND report them in new_people (name + handle).

HARD INVARIANT: every durable item you identify MUST be written — either a store note or (if sensitive)
a holding-area file. So for each chunk, notes_written + sensitive_held MUST EQUAL durable_found, AND
notes_written == notes.length, sensitive_held == sensitive.length (the arrays list the EXACT paths you
wrote — scripts/stamp_markers.py verifies each one exists before stamping the marker; a missing/
mis-reported path leaves the chunk PENDING and it is cleanly re-distilled next run, so report paths
accurately). Never
report durable_found greater than what you wrote. If on reflection an item is not worth keeping, it is
NOT durable — exclude it from the count. Do NOT find-then-drop: if you counted it, you write it.

Return via StructuredOutput with its 'chunks' argument set to the ARRAY itself (never { chunks: [...] } nested inside it): [ { chunk_id, ref, durable_found, notes_written, sensitive_held,
  notes:[<paths>], sensitive:[<paths>], new_people:[{name,handle}] } ].`
}

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
log(`chunks: ${count}, batch ${BATCH} (wave ${WAVE})`)
// Feature C: past fidelity-audit corrections for this source, read by the Setup agent from the
// pre-launch brief; prompt() folds them in below (empty string when there are none).
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
// Each agent writes a per-chunk RESULT file to _sources/imsg-results.d/<chunk_id>.json (NOT the
// done-marker). The Workflow runtime is pure JS (no fs), so done-markers are stamped AFTER this
// workflow by scripts/stamp_markers.py, which verifies the declared note paths + count and only
// then writes _sources/imsg-done.d/<chunk_id>. That preserves the finding-2 crash-safety (a marker
// means "the chunk's declared notes verifiably exist"), with the verification moved back to Python.
const results = await parallel(ranges.map(([s, e], bi) => () =>
  agent(prompt(s, e, TODAY), { label: `imsg:b${bi + 1}/${nbatches} (L${s}-${e})`, phase: 'Distill',
    schema: RESULT_SCHEMA, agentType: AGENT_TYPE }).then(r => (r && r.chunks) || [])
))
const all = results.filter(Boolean).flat()
const notes = all.reduce((a, c) => a + (c.notes_written || 0), 0)
const sens = all.reduce((a, c) => a + (c.sensitive_held || 0), 0)
log(`distilled ${notes} store notes, ${sens} sensitive held for review, ${all.length}/${count} chunks`)
log(`per-chunk RESULT files in ${RESULTS} — run scripts/stamp_markers.py next to stamp done-markers`)
return { total: count, chunks_done: all.length, notes, sensitive_held: sens, detail: all }
