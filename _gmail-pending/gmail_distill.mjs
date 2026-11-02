export const meta = {
  name: 'gmail-distill',
  description: 'Distill extracted Gmail threads into engram atomic notes (batched, resumable)',
  phases: [
    { title: 'Setup', detail: 'count the work-list' },
    { title: 'Distill', detail: 'one worker agent per line-slice; get_thread FRESH, distill from the connector' },
  ],
}

// Robust to `args` not threading through a re-launch: fixed project paths are defaults.
const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
// Local renders: METADATA ONLY for the workers (headers). Facts come from a FRESH connector
// get_thread each worker makes itself — renders re-typed by an agent were found paraphrased
// (16 of 22 in one measured sync). Same pattern as gmail_update.mjs.
const THREADS = A.threadsDir || (ROOT + '/_sources/gmail-threads')
const PEOPLE = A.peopleFile || (ROOT + '/_manifest/people.md')
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode)
// and atomically claimed BEFORE this distiller launched. The Workflow runtime is pure JS (no fs,
// no dynamic loader), so the wave-contract guard lives in Python now; a Setup agent reads the
// consumed contract with its Read tool and hands back the pinned wave path + expected count.
const SOURCE = 'gmail'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
// Feature C (fidelity corrections loop): the Setup agent also reads this pre-launch brief (open
// audit corrections for this source, rendered by scripts/corrections_brief.py --source gmail) and
// distillPrompt() folds any bullets in as extra adversarial reminders. May be absent/empty.
const CORR_SOURCE = 'gmail'
const CORRECTIONS_BRIEF = ROOT + '/_sources/' + CORR_SOURCE + '.corrections-brief.txt'
// Controlled tag vocabulary. SINGLE OWNER: _manifest/constants/tag_vocab.json (CLAUDE.md, "one
// owner per decision"). The Setup agent reads it at run time and hands the array back as
// `tag_vocab`; the prompt's allowed-tag list is built from THAT. Never paste a copy of the vocab
// into this file -- a hardcoded list silently drifts from the owner, which is exactly how the
// distillers started emitting off-vocab tags.
const TAG_VOCAB_FILE = ROOT + '/_manifest/constants/tag_vocab.json'
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
          // 'error' = get_thread failed for this thread: no note, no drop marker (stays pending)
          status: { type: 'string', enum: ['done', 'dropped', 'error'] },
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
  return `You distill EMAIL THREADS into engram atomic notes. engram is the owner's private
markdown knowledge base. Conventions: ${ROOT}/CLAUDE.md and ${ROOT}/_manifest/schema.md.
First, read the people registry once: ${ROOT}/_manifest/people.md (resolve participants to canonical names).

SLICE GUARD: your slice is EXACTLY lines ${start}-${end} of the wave = ${end - start + 1} record(s). Process ONLY those lines — other workers own the rest of the file, and a line processed outside your slice produces duplicate notes/markers (this has happened: one worker read a whole 45-line wave). Confirm the sed output has ${end - start + 1} lines before starting; never cat/read the whole wave file.
LOAD THE GMAIL TOOL FIRST: call ToolSearch with query
  "select:mcp__claude_ai_Gmail__get_thread"
then call mcp__claude_ai_Gmail__get_thread({threadId:"<thrid>", messageFormat:"FULL_CONTENT"}).
The thrid is the connector HEX id from your work-list line — pass it verbatim as threadId.

YOUR WORK-LIST: run \`sed -n '${start},${end}p' ${WAVE}\` — that prints your thread records, one JSON
object per line, each with a "thrid" field. Process EACH line (one note per thread), ONE AT A TIME.
For a thread:
1. get_thread({threadId:"<thrid>", messageFormat:"FULL_CONTENT"}) to fetch the FULL CURRENT thread
   (all messages, oldest->newest, with quoted reply chains). THAT CONNECTOR RESPONSE IS THE SOURCE:
   every fact, name, date, figure and quotation in the note must come from it. If the result is too
   large to come back inline and is saved to a file instead, Read that file — it is still the
   connector response.
2. The local render at ${THREADS}/<thrid>.txt is METADATA ONLY. You MAY read its header
   (subject/labels/participants/date range), but NEVER take a fact from its message text: renders
   were produced by an earlier agent re-typing the connector output and are known to be condensed
   or paraphrased. Where the render and the connector response disagree, the connector wins.
3. If get_thread FAILS for a thread (tool error, not found, no messages, or the tool cannot be
   loaded), return status:"error" for that thrid with reason "<what failed>". Write NO note and NO
   drop marker for it, and do NOT fall back to the local render — the thread stays pending and is
   retried on the next run.
4. Otherwise decide KEEP or DROP from the connector response.

DROP (status:"dropped", set reason) when the thread has no durable content:
  - pure logistics/scheduling with no lasting info, one-word replies ("thanks","got it"),
    receipts/confirmations, automated mail with a trivial human reply, calendar invites only.
  - reason "phi" if the thread is fundamentally about an IDENTIFIABLE clinical patient
    (name/initials+MRN/DOB tied to diagnosis/treatment). Do NOT write such a note.
  Dropped threads: write NO note. INSTEAD write a durable drop marker with the Write tool to
  ${ROOT}/_sources/gmail-drops.d/<thrid>.json containing exactly
  {"thrid":"<thrid>","reason":"<short reason>","dropped_at":"<UTC now>"} — this records the drop so
  it is never re-processed. Get <UTC now> ONCE for your whole chunk with Bash: date -u +%Y-%m-%dT%H:%M:%SZ
  and reuse that string in every marker you write. The dropped_at timestamp is load-bearing:
  scripts/gmail_worklist.py RE-OPENS a dropped thread that later grows because the owner themself replies
  to it, and it compares the new messages against dropped_at (falling back to the marker file mtime
  when the field is missing). Do not omit it and do not back-date it.

KEEP (status:"done"): write ONE markdown note. Compress to atomic facts — DISTILLED, not verbatim.
Never fabricate; if unsure, omit. This store is single-user: COI/business/financial/family content
is NORMAL — keep it. The ONLY exclusion is identifiable clinical-patient PHI: never write patient
identifiers or case detail; de-identify any incidental clinical mention.

FIDELITY RULES (audited — these are the top defect class; do NOT break them):
1. Assert ONLY facts present in THIS source. Never import a specific from outside knowledge —
   property addresses, people's surnames, company/carrier/fund names, account numbers, dates. If a
   specific is not in the source, OMIT it (a vaguer true note beats a precise wrong one). You MAY
   resolve a participant to their canonical people.md name; invent no other specifics.
   **THE #1 MEASURED DEFECT ON THIS SOURCE IS GAP-FILLING.** A full audit of one domain's email
   notes found a ~10% fabrication rate, and nearly every case was the same behavior: the thread left
   something unstated, and the note supplied a plausible-sounding specific to make the story read
   smoothly. An incomplete note is CORRECT; a smooth one that adds detail is WRONG. When the source
   is silent, SAY NOTHING or say "not stated in this thread".
   Illustrative examples of what you must not do (each mirrors a measured defect class):
   - invented a REASON: wrote that a meeting moved "due to a room conflict" when the organizer
     gave no reason at all (no room is mentioned anywhere in the thread);
   - invented a MECHANISM: wrote that a request was routed "through the vendor's support portal"
     when the actual chain was a plain forwarded email and no system is named anywhere;
   - invented CONTEXT/color: wrote that a volunteer quit "over a scheduling conflict", that the broken
     projector was "in room 204", and that the organizer "was out sick" — the source says only that
     the volunteer had "a lot going on", "the projector upstairs is dead", and "I'm back now";
   - invented FINANCIAL detail: wrote that a club budget was "nearly exhausted" with a spending
     history, when the message says only that the treasurer was "watching the budget closely".
2. Dates: use ONLY dates explicitly written in the source. Never compute or guess one — do not turn
   "Monday"/"next week"/"the 23rd" into a calendar date, and never invent a date's meaning
   ("first day of orientation"). Invented dates are a measured defect class. Also do NOT fuse two
   different events onto one date — e.g. the source says the RENOVATION starts March 3 and the LEASE
   begins June 1, and the note writes "their June 1 renovation", conflating lease start with the renovation. Each date belongs to the event the source attaches it to.
3. Attribution is load-bearing: preserve WHO said/decided each thing and the EXACT label/category of
   any value (e.g. "Preferred Return" vs "Return of Capital"; on- vs off-days of a schedule). Do not
   flip a speaker or merge two distinct categories.
4. Do NOT over-anchor a whole note to one detail (a single address/person/deal) unless the source
   makes it the subject.
5. Action-item RELEVANCE — include an item owned by someone else in ## Action items ONLY when the owner
   needs its outcome delivered to them or it blocks their own next step. A commitment strictly between other people
   (that never routes through the owner), or pure administrative/logistics bookkeeping — a billing/appeals
   process, calendar notation, "note X in the system/sheet" — requiring no follow-up from the owner, is NOT
   the owner's action item: record it under ## Key discussion if it matters, otherwise omit. This is about
   WHETHER TO LIST, not attribution: it does NOT soften rule 3 — an item you do list keeps its true
   owner. When genuinely ambiguous, KEEP it (an occasional needless line costs far less than silently
   dropping something the owner needed to see).
6. PERSON IDENTITY — use the person the SOURCE names, never a more familiar substitute. Person-swaps
   are a measured defect class, and they lean one way: the note replaces a less-prominent person with
   a better-known one from the same circle. E.g. a gym membership is attributed to the owner's
   housemate when the email is addressed to, and names, the owner's cousin, and the housemate appears
   nowhere in the thread — even though people.md registers both. Before naming a family member, colleague, or counterparty,
   confirm THAT NAME is in this thread. Resolving a name to its canonical people.md form is fine;
   swapping in a different person is a fabrication about the owner's family or money.
7. FIGURES — never complete a pattern, symmetry, or arithmetic the source did not state. Invented
   money amounts, percentages, accounts and dates are the largest measured defect class. E.g. the
   email says two club members could each contribute "the same amount", and the note records two
   different figures, inventing an asymmetry the source explicitly denies.
   Do not derive a second number from a first, do not total, do not pro-rate, and do not infer a
   figure from an attachment you cannot read. Quote the source's numbers or omit them. This is
   financial content: a missing figure is recoverable, a wrong one is not.
8. NEVER STATE A QUESTION OR PROPOSAL AS RESOLVED unless the source shows it actually answered (a
   measured defect class). Email threads routinely END on an unanswered question, and the last message
   resolves nothing by itself. Do not promote a request into a grant, a proposal into a decision, or
   an acknowledgement into an approval — e.g. a reply of "Received, thank you." written up as the
   request having been "accepted". Check whether a LATER message actually answers the earlier
   one; if none does, record it under "## Open questions" or say it is unresolved in this thread.
   Never infer the asker's preferred or likely outcome.
${CORRECTIONS ? `\nPast mistakes to avoid on THIS source (from fidelity audits — REAL defects found in earlier notes from this same connector; do NOT repeat them):\n${CORRECTIONS}\n` : ''}
NOTE FORMAT (exact frontmatter, then body):
---
title: "Concise human title (no Re:/Fw:)"
date: YYYY-MM-DD            # the thread's most representative date (decision/last substantive msg)
aliases: [...]              # 1-3 alternate titles / key entities for retrieval (property address,
                           #   person, deal/entity name); [] only if nothing obvious
tags: [...]                 # ONLY from the CONTROLLED TAGS list below — no other word
domain: <one domain>        # exactly one of the dirs below; ambiguous -> unsorted
subpath: null               # or grants|papers|lab-ops (research) or travel (personal)
type: event                 # ALWAYS "event" for these (dated email threads)
source: gmail
provenance:
  ref: "gmail:thread:<thrid>"
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
  Route by CONTENT, not the sender domain alone. Real-estate/LLC/trust/tax/investment -> finance.
  Studies/grants/papers/academic -> research. The day job's operations -> work. A company the owner
  runs or advises -> ventures. Family/school/social/travel/home -> personal. Genuinely unclear -> unsorted (add tag "flagged").
  DUAL-DOMAIN: file under the PRIMARY domain (where the decision/outcome lands), and add the tag
  "overlap" plus name the second domain in the body. Do not duplicate the note.

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
cross-linking and supersession is computed later by the synthesis/backlinks pass (which knows every
note's final filename). Guessed links break the store, so emit none.

FILE NAMING: slug = lowercase kebab of the title, concise. To avoid collisions across parallel
workers, the filename MUST end with EXACTLY the last 6 characters of the FULL thrid — count them:
the final 6 chars of the 16-hex thrid, NOT the last 4, and NEVER omit the suffix:
\`${ROOT}/<domain>/<YYYY-MM-DD>-<slug>-<thrid_last6>.md\` (e.g. thrid 18abcdef00123abc -> suffix
123abc -> 2026-07-01-grant-revisions-123abc.md). Create the file with the Write tool; do not print it.

RELIABILITY (CRITICAL): create every KEEP note with the **Write tool** — NEVER bash/cat/heredoc/
printf. The Write tool result confirms the file landed; only report status:"done" for a thread once
its file is actually written. NEVER report "done" for a note you did not write. Process the threads
one at a time and finish writing EACH file before moving on; do not batch-skip. If a Write fails,
retry it once. At the end, the count of "done" entries you return MUST equal the number of files you
actually created.

PARTICIPANTS: map emails/names to canonical names via people.md. Any human sender/recipient NOT in
people.md -> include their display name in the note AND report them in new_people (name + email) so
the registry can be updated. Do not invent relationships.

Return via StructuredOutput with its 'threads' argument set to the ARRAY itself (never { threads: [...] } nested inside it): [ { thrid, status: "done"|"dropped"|"error", note: "<relpath under root or
null>", domain, title, reason: "<drop reason, get_thread failure, or null>", new_people: [{name,email}] } ]
— one entry per thrid line.`
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
// else the first 'dropped', else the first record seen ('error' ranks last — a duplicate that DID
// write a note or marker must not be masked by one whose fetch failed). Counts below are of UNIQUE
// threads so `accounted` matches the wave.
const RANK = { done: 2, dropped: 1 }
const byThrid = new Map()
for (const t of allRaw) {
  const prev = byThrid.get(t.thrid)
  if (!prev || (RANK[t.status] || 0) > (RANK[prev.status] || 0)) byThrid.set(t.thrid, t)
}
const all = [...byThrid.values()]
if (allRaw.length !== all.length) log(`WARN: ${allRaw.length - all.length} duplicate thread record(s) collapsed — a worker over-read its slice`)
const done = all.filter(t => t.status === 'done')
const dropped = all.filter(t => t.status === 'dropped')
const errors = all.filter(t => t.status === 'error')
log(`distilled: ${done.length} notes, ${dropped.length} dropped, ${errors.length} get_thread error(s), ${all.length}/${count} accounted`)
if (errors.length) log(`WARN: get_thread failed — no note, no drop marker (still pending; re-run the worklist): ` +
  errors.map(t => `${t.thrid} (${t.reason || 'no reason given'})`).join('; '))
return { total: count, accounted: all.length, done: done.length, dropped: dropped.length, errors: errors.length, threads: all }
