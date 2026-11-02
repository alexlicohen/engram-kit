export const meta = {
  name: 'granola-new',
  description: 'Distill NEW Granola meetings (incremental sync) into fresh engram notes via the connector',
  phases: [
    { title: 'Setup', detail: 'count the work-list' },
    { title: 'Distill', detail: 'one worker agent per 10-meeting slice (Granola connector)' },
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
// consumed contract (wave lines: {id, title, date}) and hands back the pinned wave path + count.
const SOURCE = 'granola-new'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
const DONE = ROOT + '/_sources/granola-new-done.d'
const PEOPLE = ROOT + '/_manifest/people.md'
// Feature C (fidelity corrections loop): the corrections ledger/brief use the source key "granola"
// (NOT "granola-new"); the Setup agent reads this pre-launch brief (scripts/corrections_brief.py
// --source granola) and prompt() folds any bullets in as extra adversarial reminders. May be empty.
const CORR_SOURCE = 'granola'
const CORRECTIONS_BRIEF = ROOT + '/_sources/' + CORR_SOURCE + '.corrections-brief.txt'
// Controlled tag vocabulary. SINGLE OWNER: _manifest/constants/tag_vocab.json (CLAUDE.md, "one
// owner per decision"). The Setup agent reads it at run time and hands the array back as
// `tag_vocab`; the prompt's allowed-tag list is built from THAT. Never paste a copy of the vocab
// into this file -- a hardcoded list silently drifts from the owner, which is exactly how the
// distillers started emitting off-vocab tags.
const TAG_VOCAB_FILE = ROOT + '/_manifest/constants/tag_vocab.json'
// SECONDARY-ACCOUNT GUARD config. If your Granola account lives on a secondary address (e.g. a
// company you run), put that company's name here so the guard below can neutralise the account
// tag Granola stamps on every meeting. '' = no secondary account (the guard still routes by content).
const SECONDARY_ACCOUNT = ''
const BATCH = A.batch || 10

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
    meetings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          status: { type: 'string', enum: ['done', 'dropped'] },
          note: { type: ['string', 'null'] },
          domain: { type: ['string', 'null'] },
          reason: { type: ['string', 'null'] },
          new_people: { type: 'array', items: { type: 'object',
            properties: { name: { type: 'string' }, email: { type: ['string', 'null'] } },
            required: ['name'] } },
        },
        required: ['id', 'status'],
      },
    },
  },
  required: ['meetings'],
}

function prompt(start, end, today) {
  return `You distill NEW Granola meetings into FRESH engram atomic notes (incremental sync).
engram = the owner's private markdown KB. Conventions: ${ROOT}/CLAUDE.md, ${ROOT}/_manifest/schema.md.
Read the people registry once: ${PEOPLE} (resolve attendees to canonical names; its 'self' row is the owner).

LOAD THE GRANOLA TOOL FIRST: ToolSearch query "select:mcp__claude_ai_Granola__get_meetings",
then call mcp__claude_ai_Granola__get_meetings({meeting_ids:[...]}) (max 10/call).

SLICE GUARD: your slice is EXACTLY lines ${start}-${end} of the wave = ${end - start + 1} record(s). Process ONLY those lines — other workers own the rest of the file, and a line processed outside your slice produces duplicate notes/markers (this happened 2026-09-09: one worker read all 45 lines). Confirm the sed output has ${end - start + 1} lines before starting; never cat/read the whole wave file.
WORK-LIST: \`sed -n '${start},${end}p' ${WAVE}\` — each line {id, title, date}. Collect ids, call
get_meetings ONCE. Each meeting has a <summary> (rich template: metadata header, ATTENDEES w/ orgs,
DISCUSSION, DECISIONS, OPEN QUESTIONS, CONTEXT, VERBATIM, NEXT STEPS) and <private_notes>.

FIDELITY RULES (audited — top defect class; do NOT break them):
1. Assert ONLY facts present in THIS meeting's summary/notes. Never import specifics from outside
   knowledge — names/surnames, org/place names, numbers, dates. If not in the source, OMIT it. You
   MAY resolve attendees to canonical people.md names; invent no other specifics.
2. Dates: use ONLY dates the source states. NEVER compute/guess a calendar date yourself — not
   from cadence ("next weekly" -> a calendar date) and not from a relative phrase. When the source
   gives an absolute due date, write it as (due YYYY-MM-DD). When it gives only a SPOKEN RELATIVE
   due phrase ("next Wednesday", "by Friday", "end of the month", "in two weeks"), copy that phrase
   VERBATIM inside double quotes as the due: (due "next Wednesday") — do NOT convert it to a date
   and do NOT add a weekday or date of your own; a deterministic script resolves it against the
   meeting date. If no due is given, write the action without one.
3. Preserve attribution + exact figures/labels. CRITICAL in EVERY meeting, and MOST of all in a
   two-person 1:1 (there are only two candidate owners and the note is written from the owner's side, so
   an unattributed task silently collapses onto the owner — a measured defect): an action
   item's OWNER is whoever the transcript assigns it to — do NOT default tasks to the owner (the host);
   a mentee's task ("you install the GPUs", "you write it up") stays the MENTEE's, and the host's
   methods advice is not the mentee's work. Diarization is often garbled — attribute by CONTENT
   (who did the analysis, who reports having RUN it, who is being told to do X), not by guessing, and
   never flip who said/owns a thing. If the source names NO owner ("that needs to be added in"), write
   the item WITHOUT an owner — an unowned task is accurate, a task defaulted to the owner is a fabrication.
4. A hedged gloss ("likely X") is still a claim — only add it if the source supports it; else omit.
5. Action-item RELEVANCE (is it the owner's loop?): put an item in ## Action items ONLY if the owner needs its
   outcome delivered to them or it blocks their own next step. A task owned by someone else that runs
   purely between other people (one mentee waiting on another) or is internal admin/logistics
   bookkeeping — "note X in the config sheet", calendar notation, a billing-appeal process — with no
   deliverable back to the owner is NOT an owner action item: leave it in ## Key discussion if it matters,
   else drop it. This governs INCLUSION only and does NOT override rule 3 — any item you DO list keeps
   its true owner (never re-attribute it to the owner to justify keeping it). When it is genuinely unclear
   whether the owner needs it, KEEP it (a stray reminder costs far less than dropping something they needed).
${CORRECTIONS ? `\nPast mistakes to avoid on THIS source (from fidelity audits — REAL defects found in earlier Granola notes; do NOT repeat them):\n${CORRECTIONS}\n` : ''}
For EACH meeting decide KEEP or DROP:
DROP (status:"dropped", set reason) only if there is genuinely no durable content (empty/cancelled/
test meeting). Write a done-marker to ${DONE}/<id> containing: dropped
  - reason "phi" if fundamentally about an IDENTIFIABLE clinical patient — write NO note.

KEEP (status:"done"): write ONE fresh markdown note. Match the EXISTING granola-note schema exactly
(read one for reference, e.g. an existing file under research/). Frontmatter:
---
title: "<concise human title; you may use the meeting title cleaned up>"
date: <YYYY-MM-DD from the meeting>
aliases: [...]            # key entities/people/projects for retrieval; [] if none obvious
tags: [...]               # ONLY from the CONTROLLED TAGS list below — no other word
domain: <one domain>      # route by CONTENT (see list); ambiguous -> unsorted + tag "flagged"
subpath: null             # or grants|papers|lab-ops (research) or travel (personal)
source: granola
provenance:
  ref: "granola:<id>"
  ingested: ${today}
  method: distilled
participants: [Canonical Name, ...]   # FULL roster from the ATTENDEES section, resolved via people.md
status: done
related: []               # ALWAYS [] — the backlinks/synthesis pass sets links
---
Then body sections (DISTILLED): ## Summary / ## Decisions / ## Action items ("- <task> — <owner>
(due <date>)", or (due "<spoken phrase>") per rule 2) / ## Key discussion / ## Open questions / ## References — include every Decision,
Open-question, and dated action item the summary contains; omit a section only if truly empty.

DOMAINS (route by content; the list's single owner is ${ROOT}/_manifest/constants/domains.json):
research (subpath grants|papers|lab-ops), work, ventures, finance, personal (subpath travel), health,
infra, literature, unsorted. Studies/grants/papers -> research. The day job's operations -> work. A
company the owner runs or advises -> ventures.
SECONDARY-ACCOUNT GUARD: ${SECONDARY_ACCOUNT ? "the owner's Granola account is attached to their " + SECONDARY_ACCOUNT + " address, so a meeting's calendar/account metadata may carry a " + SECONDARY_ACCOUNT + " association even when the meeting is NOT about " + SECONDARY_ACCOUNT + ". Route to 'ventures' (with the ventures tag) ONLY when the meeting is substantively about " + SECONDARY_ACCOUNT + "." : 'no secondary account is configured.'} Classify by CONTENT + PARTICIPANTS, NEVER by an account or calendar
tag: a mentee 1:1 or research discussion is 'research', a work call is 'work', etc. — whatever calendar it
lands on. Never stamp a domain or tag onto a meeting only because of the account it was recorded on.

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
Always include "meeting" plus the domain tag.

FILE NAME: ${ROOT}/<domain>/<YYYY-MM-DD>-<slug>-g<first 8 chars of the id>.md  (collision-safe).
Create with the **Write tool** (never cat/heredoc). Then write the done-marker ${DONE}/<id>
containing: ok . Only report status after BOTH writes succeed.

DO NOT read, edit, or modify ANY EXISTING note (e.g. a prior meeting in the same recurring series).
Write ONLY your new note + its done-marker. Series prev/next \`related:\` links are added LATER by
gen_backlinks (that is why the new note's related: is []); back-linking a prior note here breaks the
incremental-only-CREATE invariant and makes sync_verify FAIL the whole wave.

PHI: exclude identifiable clinical-patient PHI; de-identify incidental mentions. Report any human
attendee NOT in people.md in new_people (name+email if known). Do not invent relationships.

Return via StructuredOutput with its 'meetings' argument set to the ARRAY itself (never { meetings: [...] } nested inside it): [ { id, status, note, domain, reason, new_people } ] — one per id.`
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
log(`new-meeting work-list: ${count}, batch ${BATCH} (wave ${WAVE})`)
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
const results = await parallel(ranges.map(([s, e], bi) => () =>
  agent(prompt(s, e, TODAY), { label: `granola-new:b${bi + 1}/${nbatches}`, phase: 'Distill',
    schema: RESULT_SCHEMA, agentType: AGENT_TYPE }).then(r => (r && r.meetings) || [])
))
const all = results.filter(Boolean).flat()
const done = all.filter(m => m.status === 'done')
log(`distilled ${done.length} new notes, ${all.length - done.length} dropped`)
return { total: count, done: done.length, dropped: all.length - done.length, meetings: all }
