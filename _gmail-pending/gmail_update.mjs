export const meta = {
  name: 'gmail-update',
  description: 'Redistill GROWN Gmail threads in place (new messages appended since last distill), preserving curated frontmatter',
  phases: [
    { title: 'Setup', detail: 'read the consumed wave contract' },
    { title: 'Update', detail: 'one worker agent per line-slice; get_thread FRESH, rewrite body in place' },
  ],
}

const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
const PEOPLE = A.peopleFile || (ROOT + '/_manifest/people.md')
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode) and
// atomically claimed BEFORE this distiller launched (a BACKFILL wave — it rewrites existing notes in
// place — so mode/allow_history are enforced by resolve_wave). The Workflow runtime is pure JS (no fs,
// no dynamic loader), so the guard lives in Python; a Setup agent reads the consumed contract.
const SOURCE = 'gmail-update'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
const DONE = ROOT + '/_sources/gmail-update-done.d'
const BATCH = A.batch || 8

const CONTRACT_SCHEMA = {
  type: 'object',
  properties: {
    wave: { type: 'string' }, expected_count: { type: 'integer' },
    source: { type: 'string' }, mode: { type: 'string' },
    today: { type: ['string', 'null'] },
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
          status: { type: 'string', enum: ['done', 'error'] },
          fp: { type: ['string', 'null'] },
          note: { type: ['string', 'null'] },
        },
        required: ['thrid', 'status'],
      },
    },
  },
  required: ['threads'],
}

function prompt(start, end, today) {
  return `You RE-DISTILL Gmail thread notes IN PLACE in the owner's engram store because each thread
GREW: new messages were appended AFTER it was first distilled, and that new content is missing from
the note. Conventions: ${ROOT}/CLAUDE.md, ${ROOT}/_manifest/schema.md.
Read the people registry once: ${PEOPLE} (resolve participants to canonical names).

LOAD THE GMAIL TOOL FIRST: call ToolSearch with query
  "select:mcp__claude_ai_Gmail__get_thread"
then call mcp__claude_ai_Gmail__get_thread({threadId:"<thrid>", messageFormat:"FULL_CONTENT"}).
The thrid is the connector HEX id from your work-list line — pass it verbatim as threadId.

YOUR WORK-LIST: run \`sed -n '${start},${end}p' ${WAVE}\` — each line is JSON
{thrid, key, fp, title, date}. Process EACH line (one note per thread), ONE AT A TIME.

For EACH thread, update its EXISTING note IN PLACE at ${ROOT}/<fp>:
1. READ the existing note file ${ROOT}/<fp> with the Read tool. Split YAML frontmatter from body.
2. PRESERVE the frontmatter EXACTLY — keep title, date, aliases, tags, domain, subpath, type,
   source, participants, status, AND ESPECIALLY related, supersedes, superseded_by (CURATED
   cross-links from the synthesis pass — NEVER blank them, NEVER write related: []), AND the ENTIRE
   provenance block (ref UNCHANGED — it stays gmail:thread:<same id>; keep ingested + method). The
   ONLY frontmatter edits you make:
   - add or update ONE top-level line  updated: ${today}   (if an \`updated:\` line already exists,
     replace its value with ${today}; otherwise add it, e.g. right after the \`status:\` line)
   - you MAY extend participants ONLY to ADD a human newly appearing in the grown messages
     (resolved via people.md); never REMOVE an existing participant.
   Do NOT rename the file, do NOT create a new note, do NOT change the filename or the provenance ref.
3. get_thread({threadId:"<thrid>"}) to fetch the FULL CURRENT thread (all messages, including the new
   ones). FULLY REWRITE the BODY from the complete current thread so the new messages are captured —
   do NOT merely append. Produce the engram section set (DISTILLED atomic facts, never verbatim; never
   fabricate — if unsure, omit):
   ## Summary            2-4 sentences: what the thread is + why it matters (reflect the new activity)
   ## Decisions          # omit if none
   ## Action items       # omit if none; "- <task> — <owner> (due <date>)"
   ## Key discussion     the substantive points, distilled (fold in the grown messages)
   ## Open questions     # omit if none
   ## References         # omit if none; links, doc names, attachment filenames mentioned
   This store is single-user: COI/business/financial/family content is NORMAL — keep it. The ONLY
   exclusion is identifiable clinical-patient PHI: never write patient identifiers or case detail;
   de-identify any incidental clinical mention. If the grown thread turned fundamentally into an
   identifiable-patient matter, do NOT rewrite it with PHI — return status:"error" (note the reason)
   and leave the existing note untouched.
4. Write the updated file back to the SAME ${ROOT}/<fp> with the **Write tool** (full preserved
   frontmatter + new body). Then write a done-marker with the Write tool to ${DONE}/<key> containing
   just: ok   — use the "key" field from your work-list line VERBATIM as the marker filename (do not
   recompute it). Only report status:"done" after BOTH writes succeed; NEVER report done for a file
   you did not write. If a Write fails, retry once.

Return via StructuredOutput with its 'threads' argument set to the ARRAY itself (never { threads: [...] } nested inside it): [ { thrid, status:"done"|"error", fp, note } ] — one entry per thrid line.`
}

// ---- run ----
phase('Setup')
const cc = await agent(
  `Read the JSON file at ${CONTRACT} with the Read tool. It is a wave contract that ` +
  `scripts/resolve_wave.py already validated (line-count + sha256 + mode) and consumed. Return ` +
  `EXACTLY its fields as JSON: {"wave": <the "wave" string, verbatim>, "expected_count": <the ` +
  `"expected_count" integer>, "source": <the "source" string>, "mode": <the "mode" string>}. ALSO ` +
  `run \`date +%Y-%m-%d\` with the Bash tool and add its output as "today" (a fallback used only if ` +
  `the workflow was launched without args.today). Copy the contract values verbatim; do not ` +
  `recompute them. If the file is missing or unreadable, return {"wave": "", "expected_count": -1, ` +
  `"source": "MISSING", "mode": "", "today": null}.`,
  { schema: CONTRACT_SCHEMA, label: 'contract', phase: 'Setup' })
if (!cc || cc.source !== SOURCE || !Number.isInteger(cc.expected_count) || cc.expected_count < 0 || !cc.wave) {
  throw new Error(`${SOURCE} distiller: no valid CONSUMED wave contract at ${CONTRACT} — ` +
    `scripts/resolve_wave.py must validate + consume the contract BEFORE this distiller launches ` +
    `(fail-closed; the pure-JS Workflow runtime cannot read files, so the guard runs in the ` +
    `orchestrator). Got: ${JSON.stringify(cc)}`)
}
const WAVE = cc.wave.startsWith('/') ? cc.wave : ROOT + '/' + cc.wave
const count = cc.expected_count
// updated: date. args.today is the intended source; cc.today (from the Setup agent) is the fallback
// because Workflow args do not always thread reliably (Bug 1) — and there is no safe hardcoded date.
const isISO = (d) => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d)
const TODAY = isISO(A.today) ? A.today : (isISO(cc.today) ? cc.today : null)
if (!TODAY) {
  throw new Error(`${SOURCE} distiller: no valid today date (args.today=${JSON.stringify(A.today)}, ` +
    `contract-agent today=${JSON.stringify(cc.today)}) — needed to stamp updated:. Relaunch with ` +
    `args:{today:"YYYY-MM-DD"}.`)
}
log(`update work-list: ${count} grown thread(s), batch ${BATCH}, updated:${TODAY} (wave ${WAVE})`)

const nbatches = Math.ceil(count / BATCH)
const ranges = []
for (let b = 0; b < nbatches; b++) ranges.push([b * BATCH + 1, Math.min((b + 1) * BATCH, count)])

phase('Update')
const results = await parallel(ranges.map(([s, e], bi) => () =>
  agent(prompt(s, e, TODAY), {
    label: `update:b${bi + 1}/${nbatches} (L${s}-${e})`,
    phase: 'Update',
    schema: RESULT_SCHEMA,
    agentType: AGENT_TYPE,
  }).then(r => (r && r.threads) || [])
))

const all = results.filter(Boolean).flat()
const done = all.filter(t => t.status === 'done')
log(`redistilled in place: ${done.length}/${count} notes (${all.length - done.length} errors)`)
return { total: count, done: done.length, errors: all.length - done.length, threads: all }
