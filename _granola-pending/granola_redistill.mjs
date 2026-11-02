export const meta = {
  name: 'granola-redistill',
  description: 'Re-distill Granola notes from the regenerated (richer) summaries, in place, preserving curated frontmatter',
  phases: [
    { title: 'Setup', detail: 'count the work-list' },
    { title: 'Redistill', detail: 'one worker agent per 10-meeting slice (Granola connector)' },
  ],
}

const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode)
// and atomically claimed BEFORE this distiller launched (a backfill wave here — mode/allow_history
// enforced by resolve_wave). The Workflow runtime is pure JS (no fs, no dynamic loader), so the
// guard lives in Python now; a Setup agent reads the consumed contract and hands back wave + count.
const SOURCE = 'granola-redistill'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
const DONE = ROOT + '/_sources/granola-redistill-done.d'
const BATCH = A.batch || 10   // get_meetings accepts <=10 ids per call

const CONTRACT_SCHEMA = {
  type: 'object',
  properties: {
    wave: { type: 'string' }, expected_count: { type: 'integer' },
    source: { type: 'string' }, mode: { type: 'string' },
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
          status: { type: 'string', enum: ['done', 'error'] },
          fp: { type: ['string', 'null'] },
          participants_before: { type: ['integer', 'null'] },
          participants_after: { type: ['integer', 'null'] },
          note: { type: ['string', 'null'] },
        },
        required: ['id', 'status'],
      },
    },
  },
  required: ['meetings'],
}

function prompt(start, end) {
  return `You RE-DISTILL Granola meeting notes in the owner's engram store from the REGENERATED
(improved, far richer) Granola summaries. Conventions: ${ROOT}/CLAUDE.md, ${ROOT}/_manifest/schema.md.
Read the people registry once: ${ROOT}/_manifest/people.md (resolve attendees to canonical names).

LOAD THE GRANOLA TOOL FIRST: call ToolSearch with query
  "select:mcp__claude_ai_Granola__get_meetings"
then you can call mcp__claude_ai_Granola__get_meetings({meeting_ids:[...]}) (max 10 ids/call).

YOUR WORK-LIST: run \`sed -n '${start},${end}p' ${WAVE}\` — each line is JSON {id, fp, title, date}.
Collect the ids (<=10) and call get_meetings ONCE with all of them. Each returned meeting has a
<summary> (the new template: metadata header, ATTENDEES with orgs, DISCUSSION, DECISIONS, OPEN
QUESTIONS/CARRYOVER, CONTEXT & RELATIONSHIP INTEL, VERBATIM, NEXT STEPS) and <private_notes>.

For EACH meeting, update its note IN PLACE at ${ROOT}/<fp>:
1. READ the existing note file ${ROOT}/<fp>. Split YAML frontmatter from body.
2. PRESERVE the frontmatter EXACTLY — keep title, date, aliases, tags, domain, subpath, type,
   source, provenance (keep ref/ingested/method), status, AND ESPECIALLY related, supersedes,
   superseded_by (these are CURATED cross-links from the synthesis pass — NEVER blank them, NEVER
   write related: []). The ONLY frontmatter changes you make:
   - replace participants: with the FULL attendee roster from the new summary's ATTENDEES section
     (the old notes collapsed large rosters to ~2 — fix that), resolved to canonical names via
     people.md (humans only; omit the meeting bot / note-creator-only artifacts; include everyone
     named as an attendee). Anyone not in people.md: include their display name anyway.
   - add a top-level line  updated: 2026-06-30
3. FULLY REWRITE the BODY from the new summary — do NOT keep or lightly-edit the old body, and
   NEVER conclude "the body already matches, just stamp updated:". The OLD notes were distilled
   from a WEAKER template and SYSTEMATICALLY OMIT explicit Decisions, Open-questions, and the
   owner+due-date on action items — your rewrite MUST add every one the new summary contains.
   Produce the COMPLETE engram section set the new summary supports (DISTILLED, not verbatim):
   ## Summary            2-4 sentences: what the meeting was + why it matters
   ## Decisions          EVERY item from the summary's DECISIONS section (the old note usually has
                         no Decisions section at all — this is the biggest gain; do not drop it)
   ## Action items       from NEXT STEPS — one bullet per task as "- <task> — <owner> (due <date>)"
                         (or (due "<spoken phrase>") per rule b);
                         include BOTH items owed by the owner and owed by others, WITH owners and due dates
                         (the old notes dropped owners/dates — restore them). Omit only if truly none.
   ## Key discussion     condense DISCUSSION (all its subsections) to the substantive points
   ## Open questions     EVERY item from OPEN QUESTIONS / CARRYOVER (old notes usually lack this)
   ## References         docs/links/papers/datasets named (omit if none)
   Fold a few high-signal CONTEXT & RELATIONSHIP INTEL items into Key discussion. Use <private_notes>
   as extra signal. Don't copy VERBATIM quotes wholesale. EXPECTATION: because the new template is
   richer, your rewritten note is normally NOTICEABLY longer than the original (especially gaining a
   Decisions and an Open-questions section + action-item owners/dates). If your output isn't richer
   than the existing body, you have under-distilled — re-read the new summary and capture it fully.
FIDELITY RULES (audited — top defect class; do NOT break them):
   a. Assert ONLY facts present in THIS meeting's summary/notes. Never import specifics from outside
      knowledge — names, org/place names, numbers, dates. If not in the source, OMIT it. You MAY
      resolve attendees to canonical people.md names; invent no other specifics.
   b. Dates: use ONLY dates the source states. NEVER compute a calendar date yourself — not from
      cadence and not from a relative phrase. An absolute due date goes in as (due YYYY-MM-DD); a
      SPOKEN RELATIVE due phrase ("next Wednesday", "by Friday", "end of the month") is copied
      VERBATIM inside double quotes: (due "next Wednesday") — never converted to a date or given a
      weekday of your own (a deterministic script resolves it against the meeting date). No due
      given means write the action without one.
   c. OWNER attribution is the measured worst defect, and it is worst in a two-person 1:1 (only two
      candidate owners, and the note is written from the owner's side, so an unattributed task silently
      collapses onto the owner): an action item's OWNER is whoever the source assigns it to — do NOT
      default tasks to the owner (the host); a mentee's task stays the MENTEE's, and the host proposing a
      method or giving advice does not make the task theirs. Attribute by CONTENT (who reports having
      RUN/SENT/BUILT it, who is being told to do X). If the source names NO owner, write the item
      WITHOUT an owner ("— owner not specified in source") — an unowned task is accurate, a task
      defaulted to the owner is a fabrication. Never flip who said/owns/did a thing, in EITHER direction.
   d. Never state a plan/question as resolved unless the source shows it being resolved; never
      convert someone EXPLAINING a process into an action item or an open question.
   e. A hedged gloss ("likely X") is still a claim — only include it if the source supports it.
4. PHI RULE (CLAUDE.md): exclude identifiable clinical-patient PHI; de-identify any incidental
   clinical patient mention. Business, work and family content is normal — keep it.
5. Write the updated file to ${ROOT}/<fp> with the **Write tool** (full frontmatter + new body).
   Then write a done-marker with the Write tool to ${DONE}/<id> containing just: ok
   Only report status:"done" after BOTH writes succeed. NEVER report done for a file you didn't write.

Process meetings ONE AT A TIME; finish writing each file before the next. If a Write fails, retry once.

Return via StructuredOutput with its 'meetings' argument set to the ARRAY itself (never { meetings: [...] } nested inside it): [ { id, status:"done"|"error", fp, participants_before, participants_after,
note } ] — one entry per id.`
}

phase('Setup')
const cc = await agent(
  `Read the JSON file at ${CONTRACT} with the Read tool. It is a wave contract that ` +
  `scripts/resolve_wave.py already validated (line-count + sha256 + mode) and consumed. Return ` +
  `EXACTLY its fields as JSON: {"wave": <the "wave" string, verbatim>, "expected_count": <the ` +
  `"expected_count" integer>, "source": <the "source" string>, "mode": <the "mode" string>}. Copy ` +
  `the values verbatim; do not recompute or invent. If the file is missing or unreadable, return ` +
  `{"wave": "", "expected_count": -1, "source": "MISSING", "mode": ""}.`,
  { schema: CONTRACT_SCHEMA, label: 'contract', phase: 'Setup' })
if (!cc || cc.source !== SOURCE || !Number.isInteger(cc.expected_count) || cc.expected_count < 0 || !cc.wave) {
  throw new Error(`${SOURCE} distiller: no valid CONSUMED wave contract at ${CONTRACT} — ` +
    `scripts/resolve_wave.py must validate + consume the contract BEFORE this distiller launches ` +
    `(fail-closed; the pure-JS Workflow runtime cannot read files, so the guard runs in the ` +
    `orchestrator). Got: ${JSON.stringify(cc)}`)
}
const WAVE = cc.wave.startsWith('/') ? cc.wave : ROOT + '/' + cc.wave
const count = cc.expected_count
log(`re-distill work-list: ${count} meetings, batch ${BATCH} (wave ${WAVE})`)

const nbatches = Math.ceil(count / BATCH)
const ranges = []
for (let b = 0; b < nbatches; b++) ranges.push([b * BATCH + 1, Math.min((b + 1) * BATCH, count)])

phase('Redistill')
const results = await parallel(ranges.map(([s, e], bi) => () =>
  agent(prompt(s, e), {
    label: `redistill:b${bi + 1}/${nbatches} (L${s}-${e})`,
    phase: 'Redistill',
    schema: RESULT_SCHEMA,
    agentType: AGENT_TYPE,
  }).then(r => (r && r.meetings) || [])
))

const all = results.filter(Boolean).flat()
const done = all.filter(m => m.status === 'done')
log(`re-distilled: ${done.length}/${count} notes (${all.length - done.length} errors)`)
return { total: count, done: done.length, errors: all.length - done.length, meetings: all }
