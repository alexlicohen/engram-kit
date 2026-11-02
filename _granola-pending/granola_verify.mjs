export const meta = {
  name: 'granola-verify',
  description: 'Verify Granola notes against the meeting TRANSCRIPT: fix contradicted claims in the note BODY (edit mode, new notes) or write PROPOSALS only (propose mode, committed notes)',
  phases: [
    { title: 'Setup', detail: 'read the verify list + resume state' },
    { title: 'Verify', detail: 'one deep verifier per <=3 notes (Granola transcript via connector)' },
  ],
}

// WHY: granola_new.mjs distills from the Granola AI SUMMARY, and nearly every Granola fidelity defect
// is inherited from it — wrong action-item OWNER or RECIPIENT, speaker MISATTRIBUTION, DIRECTION
// reversal, proposal written as decision, computed dates (audits 2026-09-27, corrections/granola.jsonl).
// The transcript decides every one. This workflow re-checks each listed note against its transcript
// and edits ONLY the note body. It is a runbook step (_manifest/SYNC.md § Granola (new meetings),
// step 6: after granola_new.mjs, BEFORE sync_verify/check.sh/commit), never called from granola_new.
//
// TWO MODES, ONE PROMPT (verifyPrompt is the single owner of the verification rules; the modes differ
// only in what the verifier does with a CONTRADICTED claim):
//   edit    — NEW (uncommitted) notes: fix the body in place and log each change. `prepare` refuses
//             committed notes here: on committed/hand-corrected notes the auto-edit reverted verified
//             prior fixes, rewrote correct ISO dates to spoken phrases and demoted decisions (2026-09-27).
//   propose — COMMITTED notes (`prepare --propose`): edit NOTHING; write proposals
//             {kind,current,proposed,transcript_quote,prior_fix_touched} to the result file, knowing
//             the note's prior-fix history (<results>/<id>.history.md, written by prepare). The owner picks
//             from `granola_verify.py review`; `apply --ids` makes the edits; then check.sh.
// The MODE is read from the LIST FILE (every line of a propose list carries "mode":"propose"), never
// from Workflow args — only `prepare` decides it. Setup's history_exists cross-checks it: a history
// file next to an edit-mode entry, or a propose entry without one, fails closed.
//
// Contract (the Workflow runtime is pure JS — no fs/import; agents do all file I/O):
//   PRE  `python3 scripts/granola_verify.py prepare ...` writes the list file (one
//        {"id":"<granola uuid>","path":"<repo-relative or absolute note path>"} per line) and snapshots
//        every note to <results>/<id>.before.md BEFORE any model touches it.
//   RUN  this workflow. The Setup agent reads the LIST FILE (the list never travels in args); each
//        verifier writes <results>/<id>.json after editing. A note that already has a result file is
//        skipped, so a re-launch resumes. Absolute paths are accepted (scratch copies outside the store).
//   POST `python3 scripts/granola_verify.py check ...` — result per note, frontmatter byte-identical
//        to the snapshot, every body edit logged (propose: the note byte-identical to the snapshot,
//        proposals well-formed). Exit 1 on any violation.
// args (all optional): { root, list, results, batch (<=3), concurrency (<=4), agentType }. A propose run
// passes list/results for the propose files (_sources/granola-verify-propose.{jsonl,d}); the mode itself
// still comes from the list's content.

const A = (typeof args !== 'undefined' && args) ? args : {}
const ROOT = A.root || '$ENGRAM_ROOT'
const abs = (p) => (p.startsWith('/') ? p : ROOT + '/' + p)
const LIST = abs(A.list || '_sources/granola-verify.jsonl')
const RESULTS = abs(A.results || '_sources/granola-verify.d')
const PEOPLE = ROOT + '/_manifest/people.md'
const GLOSSARY = ROOT + '/_granola-pending/granola-glossary.generated.txt'
const BATCH = Math.min(Math.max(parseInt(A.batch, 10) || 3, 1), 3)
// <=4 concurrent: wider fan-outs trip the Granola connector rate limit (measured).
const CONC = Math.min(Math.max(parseInt(A.concurrency, 10) || 4, 1), 4)
// Worker subagent type: needs MCP access (the Granola connector). 'general-purpose' is Claude Code's
// stock subagent (all tools); pass args.agentType to use your own subagent (e.g. a stronger model).
const AGENT_TYPE = A.agentType || 'general-purpose'
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

const LIST_SCHEMA = {
  type: 'object',
  properties: {
    list_found: { type: 'boolean' },
    list_lines: { type: 'integer' },   // non-empty lines in the list file
    entries: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          path: { type: 'string' },          // verbatim from the list line
          mode: { type: 'string' },          // verbatim "mode" field, "edit" when the line has none
          note_exists: { type: 'boolean' },
          snapshot_exists: { type: 'boolean' },
          history_exists: { type: 'boolean' },
          result_exists: { type: 'boolean' },
        },
        required: ['id', 'path', 'mode', 'note_exists', 'snapshot_exists', 'history_exists', 'result_exists'],
      },
    },
  },
  required: ['list_found', 'list_lines', 'entries'],
}
const KINDS = ['owner', 'recipient', 'attribution', 'direction', 'date', 'decision', 'name', 'other']
const CHANGE = {
  type: 'object',
  properties: {
    kind: { type: 'string', enum: KINDS },
    before: { type: 'string' },
    after: { type: 'string' },
    transcript_quote: { type: 'string' },
  },
  required: ['kind', 'before', 'after', 'transcript_quote'],
}
const PROPOSAL = {
  type: 'object',
  properties: {
    kind: { type: 'string', enum: KINDS },
    current: { type: 'string' },
    proposed: { type: 'string' },
    transcript_quote: { type: 'string' },
    prior_fix_touched: { type: 'boolean' },
  },
  required: ['kind', 'current', 'proposed', 'transcript_quote', 'prior_fix_touched'],
}
// The only schema difference between the modes: `changes` (edit) vs `proposals` (propose).
function resultSchema(mode) {
  const key = mode === 'propose' ? 'proposals' : 'changes'
  return {
    type: 'object',
    properties: {
      notes: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string' },
            path: { type: 'string' },
            status: { type: 'string', enum: ['verified', 'error'] },
            claims_checked: { type: 'integer' },
            [key]: { type: 'array', items: mode === 'propose' ? PROPOSAL : CHANGE },
            unverifiable: { type: 'array', items: { type: 'string' } },
            error: { type: ['string', 'null'] },
          },
          required: ['id', 'path', 'status', key, 'unverifiable'],
        },
      },
    },
    required: ['notes'],
  }
}

function verifyPrompt(itemsText, count, mode) {
  // ONE prompt, two output modes. Both mode blocks are built on every call (so a render exercises
  // both template literals); `mode` only selects which one is interpolated.
  const P = mode === 'propose'
  const EDIT_TASK = `check the note's claims against the transcript and fix, in the note BODY only, every claim the
transcript CONTRADICTS.`
  const PROPOSE_TASK = `check the note's claims against the transcript and, for every claim the transcript CONTRADICTS,
write a PROPOSAL (the exact current text and the corrected text). PROPOSE MODE: these notes are COMMITTED
and many were already hand-corrected against the transcript — you edit NOTHING; the owner reviews each proposal.`
  const EDIT_HISTORY = `- A committed note (tracked in git) may already carry a transcript-verified fix: before changing a line,
  run \`git -C ${ROOT} log --follow -p -- <path>\` (skip for untracked/absolute scratch paths); do not
  revert a prior fix unless the transcript plainly contradicts it.`
  const PROPOSE_HISTORY = `- PRIOR FIXES: read ${RESULTS}/<id>.history.md for each note (written before this run from
  \`git log --follow\`): every commit after the note's creation that changed its body, with the lines it
  REMOVED and ADDED. ADDED lines are deliberate later corrections, most of them already checked against
  this transcript. Never propose reverting one to a REMOVED line; propose changing one only when the
  transcript PLAINLY contradicts it, and set "prior_fix_touched": true on any proposal whose "current"
  overlaps an ADDED line (false otherwise).
- DATES (narrows the Date recipe above) — do NOT propose replacing a correct ISO date with the spoken phrase. Propose a date change ONLY
  when the written date is WRONG: the transcript states a different date, the spoken phrase cannot mean
  that calendar day (you may work out the day from the meeting date to CHECK a written date — never to
  write a new one), or no date/deadline was said at all (then propose the text without the date).
- DECISIONS (narrows the Decision recipe above) — do NOT propose demoting a ## Decisions line merely because nobody said "agreed". Propose it
  only when the transcript shows it was NOT agreed: someone objects or defers, it is left explicitly open
  ("let's think about it", "we'll decide next time"), or it is only floated. The transcript_quote must be
  the words that show it was not agreed. No explicit agreement but no dissent either = UNVERIFIABLE.
- WRITING A PROPOSAL: "current" = text copied EXACTLY from the note body (same characters, spacing and
  punctuation) that occurs ONCE in the body — include the whole line or enough surrounding words to make
  it unique; never span the frontmatter. "proposed" = the text that should replace it, following the
  recipes above. A decision demotion: "current" = the ENTIRE ## Decisions bullet line (from "- " to the
  end of the line), "proposed" = the ENTIRE new line "- Proposed: ..." — the apply step moves it to
  ## Open questions. Deleting a whole line: "current" = the whole line, "proposed" = "". One proposal per
  contradicted claim.`
  const EDIT_LIMITS = `- NEVER edit the frontmatter (everything from the first "---" line through the closing "---" line) — not
  participants, title, tags, anything. If a frontmatter field is wrong, list it under unverifiable as
  "FRONTMATTER: <field> <problem>" instead. A post-run check compares the frontmatter byte-for-byte
  against ${RESULTS}/<id>.before.md (the pre-edit snapshot — never modify it).
- Touch no file except your own notes and your own result files. No check.sh, no git writes, no commits.`
  const PROPOSE_LIMITS = `- NEVER modify a note — no Edit/Write on any note, not the body, not the frontmatter. A post-run check
  compares every note byte-for-byte against ${RESULTS}/<id>.before.md (never modify it) and fails the run
  on any change. A wrong frontmatter field goes under unverifiable as "FRONTMATTER: <field> <problem>".
- Write ONLY your own result files. No check.sh, no git writes, no commits.`
  const EDIT_RESULT = `RESULT FILE (after all edits to that note): write ${RESULTS}/<id>.json with the Write tool:
{"id": "<id>", "path": "<path as given>", "status": "verified"|"error", "claims_checked": <int>,
 "changes": [{"kind": "owner|recipient|attribution|direction|date|decision|name|other",
   "before": "<exact old text>", "after": "<exact new text, verbatim as it now stands in the note; "" if deleted>",
   "transcript_quote": "<the transcript words that decide it, with channel/speaker label>"}],
 "unverifiable": ["<claim — why>"], "error": null|"<reason>"}
Every body edit must appear in "changes"; an unlogged edit fails the post-run check.`
  const PROPOSE_RESULT = `RESULT FILE (one per note): write ${RESULTS}/<id>.json with the Write tool:
{"id": "<id>", "path": "<path as given>", "mode": "propose", "status": "verified"|"error", "claims_checked": <int>,
 "proposals": [{"kind": "owner|recipient|attribution|direction|date|decision|name|other",
   "current": "<exact text now in the note body>", "proposed": "<replacement text; "" to delete>",
   "transcript_quote": "<the transcript words that decide it, with channel/speaker label>",
   "prior_fix_touched": true|false}],
 "unverifiable": ["<claim — why>"], "error": null|"<reason>"}
An empty "proposals" list is a normal result for a note the transcript supports.`

  return `You are an engram TRANSCRIPT VERIFIER for Granola meeting notes. engram = the owner's private
markdown KB (${ROOT}). Each note below was distilled from the Granola AI SUMMARY, which is NOT ground
truth: audits found it credits the wrong person (owner, recipient, speaker), reverses who did what to
whom, writes proposals as decisions, and computes dates nobody said. The TRANSCRIPT decides. Your job:
${P ? PROPOSE_TASK : EDIT_TASK}

YOUR NOTES (${count}; one JSON object per line — process ONLY these):
${itemsText}

SETUP
- Load the tools: ToolSearch query "select:mcp__claude_ai_Granola__get_meeting_transcript,mcp__claude_ai_Granola__get_meetings".
- Read the people registry ${PEOPLE} and the spelling glossary ${GLOSSARY} (context-scoped spellings +
  AMBIGUOUS SPOKEN FORMS). They fix SPELLINGS of names actually spoken; they are never a source of facts.
- Call mcp__claude_ai_Granola__get_meetings({meeting_ids:[<all your ids>]}) ONCE — use ONLY each
  meeting's <private_notes> (the owner's own typed notes: authoritative for spellings and for their own
  commitments). Ignore its <summary>: that is the thing being checked.
- For EACH note: Read the note (its path is absolute or relative to ${ROOT}), then call
  mcp__claude_ai_Granola__get_meeting_transcript({meeting_id:<id>}). If the result is saved to a file,
  read ALL of it (Read in chunks / grep) — verifiers that stop early call supported facts unsupported.
  An empty or unavailable transcript: make NO ${P ? 'proposals' : 'edits'}; return status "error" with the reason.

READING THE TRANSCRIPT (diarization is unreliable — judge by content and turn-taking)
- On a call, the "Microphone" channel is the owner; everyone else shares "System audio", and System-audio
  speaker labels drift: one utterance is split across labels and a label often switches a fragment
  late. An acknowledgement on System audio is never the owner's.
- In an IN-PERSON meeting (a room or committee table; several voices on the Microphone channel, little
  or no System audio), the Microphone carries EVERYONE — it does not mean the owner is speaking.
- Attribute by content: who was explaining, who is addressed by name, who says "I'll…", who reports
  having done the work, who is being asked to do X. Never read an owner or a pairing off label
  boundaries alone. If the transcript does not establish who, the claim is UNVERIFIABLE, not wrong.

CHECK EVERY ONE OF THESE CLAIMS in the note (count them as claims_checked):
1. Every ## Action items line: its OWNER (the "— <owner>" segment), any RECIPIENT/counterparty in the
   task text (send X TO whom, FROM whom — direction), and its due date.
2. Every attribution anywhere in the body: "<person> said / proposed / suggested / raised / flagged /
   reported / agreed / decided / asked / pushed back / was concerned", and who did what to whom.
3. Every ## Decisions line: explicitly AGREED in the meeting, or only proposed/floated?
4. Every date in the body (due dates, event dates, "on 2026-..").
5. Every person name: is that person (and that spelling/surname) actually spoken, typed in
   <private_notes>, or a correct people.md/glossary resolution of a spoken form in this meeting's context?
Classify each: SUPPORTED, CONTRADICTED (the transcript says otherwise), or UNVERIFIABLE (the
transcript is silent, inaudible, or ambiguous). ${P ? 'Propose changes' : 'Edit'} ONLY for CONTRADICTED claims; list every
UNVERIFIABLE one (one line each: the claim + why) and leave its text alone.

FIDELITY RULES (the distiller's own, applied to your corrections — do NOT break them):
1. Assert ONLY facts present in THIS meeting's transcript/typed notes. Never import specifics from outside
   knowledge — names/surnames, org/place names, numbers, dates. If not in the source, OMIT it. You
   MAY resolve attendees to canonical people.md names; invent no other specifics.
2. Dates: use ONLY dates the source states. NEVER compute/guess a calendar date yourself — not
   from cadence ("next weekly" -> a calendar date) and not from a relative phrase. When the source
   gives only a SPOKEN RELATIVE due phrase ("next Wednesday", "by Friday"), copy that phrase VERBATIM
   inside double quotes as the due: (due "next Wednesday"). If no due is given, write the action without one.
3. Preserve attribution + exact figures/labels. An action item's OWNER is whoever the transcript assigns
   it to — do NOT default tasks to the owner (the host), MOST of all in a two-person 1:1, where an
   unattributed task silently collapses onto the owner. A task belongs to a person only if they say they will
   ("I'll…", "I can do that") or are explicitly asked and agree; when the owner says "I'll…" (or accepts an
   explicit ask) it IS theirs. If the source names NO owner, the item has NO owner — an unowned task is
   accurate, a task defaulted to someone is a fabrication. Never flip who said/owns a thing.
4. A hedged gloss ("likely X") is still a claim — only keep it if the source supports it.
5. STATUS WORDS: do not upgrade status. "should", "plan to", "proposed", "I'd envision" stay tentative;
   "done/confirmed/decided" only when someone says it is.

HOW TO FIX A CONTRADICTED CLAIM (the corrected text: minimal in-place replacement, keeping the note's format —
${P ? 'written as a proposal, never applied' : 'applied with the Edit tool'})
- Owner -> the owner the transcript establishes; if it establishes none, delete the " — <owner>" segment
  (keep the task text and any source-stated due).
- Recipient/direction -> the sender/recipient the transcript establishes; else drop the wrong party.
- Attribution -> the right person, or "it was suggested/said" or "[speaker unclear]" when the transcript
  cannot tell who.
- Date -> the date or phrase as spoken (rule 2), or remove it if nothing was said.
- Decision not explicitly agreed -> remove it from ## Decisions and add it under ## Open questions as
  "- Proposed: <what was proposed, by whom only if the transcript establishes it>" (create
  "## Open questions" right before "## References", or at the end, if the note has none).
- Name -> the canonical people.md name when the spoken form is a close match AND the meeting context
  matches that name's glossary group; otherwise the name as heard + " [sp?]" (drop an unspoken surname).
- A replacement may state ONLY what fixes the checked claim — add no new facts, bullets or context, and
  never "improve" a SUPPORTED or UNVERIFIABLE claim.
${P ? PROPOSE_HISTORY : EDIT_HISTORY}

HARD LIMITS
${P ? PROPOSE_LIMITS : EDIT_LIMITS}
- No clinical-patient PHI in anything you write.

${P ? PROPOSE_RESULT : EDIT_RESULT}
Then return the same objects as { notes: [ ... ] } — one per note. Return through the structured output;
do not reply ESCALATE — record doubt as "unverifiable".`
}

phase('Setup')
const ls = await agent(
  `Read the file ${LIST} with the Read tool. It is a JSON-lines list: each non-empty line is one object ` +
  `{"id": "<granola meeting uuid>", "path": "<note path>"} and may also carry "mode". Return "list_found": ` +
  `true, "list_lines": <the number of NON-EMPTY lines>, and "entries": one object per non-empty line, in ` +
  `file order, with "id" and "path" copied VERBATIM from that line (never resolve, normalize, reorder, dedupe ` +
  `or drop a line) and "mode" = that line's "mode" value copied verbatim, or "edit" when the line has no ` +
  `"mode" key. For each entry also check with the Bash tool (\`test -f\`), where P = the path if it starts ` +
  `with "/" else ${ROOT}/<path>: "note_exists" = P is a file; "snapshot_exists" = ${RESULTS}/<id>.before.md ` +
  `is a file; "history_exists" = ${RESULTS}/<id>.history.md is a file; "result_exists" = ${RESULTS}/<id>.json ` +
  `is a file. Do not read or modify any note. If the list file ` +
  `is missing or unreadable return {"list_found": false, "list_lines": 0, "entries": []}.`,
  { schema: LIST_SCHEMA, label: 'list', phase: 'Setup' })

if (!ls || !ls.list_found) {
  throw new Error(`granola-verify: no list file at ${LIST} — run \`python3 scripts/granola_verify.py prepare\` ` +
    `first (it writes the list and the pre-edit snapshots). Got: ${JSON.stringify(ls)}`)
}
const entries = ls.entries || []
if (!Number.isInteger(ls.list_lines) || entries.length !== ls.list_lines) {
  throw new Error(`granola-verify: Setup returned ${entries.length} entries for a list of ${ls.list_lines} ` +
    `non-empty line(s) — the list was not handed back verbatim; fail-closed.`)
}
const bad = []
const ids = new Set()
// MODE comes from the list file (written by `prepare`), never from args. One list, one mode; the
// history files `prepare --propose` writes cross-check it (fail-closed on any disagreement).
const modes = [...new Set(entries.map(e => e.mode))]
if (modes.length > 1 || (modes.length && !['edit', 'propose'].includes(modes[0]))) {
  throw new Error(`granola-verify: list ${LIST} has mode(s) ${JSON.stringify(modes)} — need exactly one of ` +
    `"edit"/"propose"; fail-closed.`)
}
const MODE = modes[0] || 'edit'
for (const e of entries) {
  if (MODE === 'propose' && !e.result_exists && !e.history_exists) bad.push(`propose entry ${e.id} has no prior-fix history ${e.id}.history.md`)
  if (MODE === 'edit' && e.history_exists) bad.push(`edit-mode entry ${e.id} has a propose history file — mode mismatch`)
}
for (const e of entries) {
  if (!UUID_RE.test(e.id)) bad.push(`bad id ${JSON.stringify(e.id)}`)
  else if (ids.has(e.id)) bad.push(`duplicate id ${e.id}`)
  ids.add(e.id)
  if (!e.path) bad.push(`empty path for ${e.id}`)
  else if (!e.note_exists) bad.push(`note missing: ${e.path}`)
  else if (!e.result_exists && !e.snapshot_exists) bad.push(`no pre-edit snapshot for ${e.id} (${e.path})`)
}
if (bad.length) {
  throw new Error(`granola-verify: list ${LIST} failed validation — ${bad.join('; ')}. Re-run ` +
    `\`python3 scripts/granola_verify.py prepare\` (it validates refs and writes the snapshots); fail-closed.`)
}
const pending = entries.filter(e => !e.result_exists)
log(`verify list (${MODE} mode): ${entries.length} note(s), ${entries.length - pending.length} already done (skipped), ` +
  `${pending.length} pending; batch ${BATCH}, <=${CONC} concurrent, agentType ${AGENT_TYPE}; results ${RESULTS}`)
if (!pending.length) return { mode: MODE, total: entries.length, verified: 0, errors: 0, changes: 0, notes: [] }

const batches = []
for (let i = 0; i < pending.length; i += BATCH) batches.push(pending.slice(i, i + BATCH))

phase('Verify')
// Bounded worker pool: CONC workers pull batches in order (the runtime's own cap is wider than the
// connector tolerates).
const queue = batches.map((b, i) => ({ b, i }))
const out = []
await parallel(Array.from({ length: Math.min(CONC, batches.length) }, () => async () => {
  while (queue.length) {
    const { b, i } = queue.shift()
    const text = b.map(e => JSON.stringify({ id: e.id, path: e.path })).join('\n')
    const r = await agent(verifyPrompt(text, b.length, MODE), {
      label: `granola-verify:${MODE}:b${i + 1}/${batches.length}`, phase: 'Verify',
      schema: resultSchema(MODE), agentType: AGENT_TYPE })
    const got = (r && r.notes) || []
    for (const e of b) {
      const n = got.find(x => x.id === e.id)
      out.push(n || { id: e.id, path: e.path, status: 'error', [MODE === 'propose' ? 'proposals' : 'changes']: [], unverifiable: [],
        error: r ? 'verifier returned no entry for this note' : 'verifier agent died' })
    }
  }
}))

const ok = out.filter(n => n.status === 'verified')
const KEY = MODE === 'propose' ? 'proposals' : 'changes'
const nChanges = ok.reduce((s, n) => s + (n[KEY] || []).length, 0)
const errs = out.filter(n => n.status !== 'verified')
log(`verified ${ok.length}/${out.length} note(s): ${nChanges} ${MODE === 'propose' ? 'proposal(s), no edits' : 'body change(s)'}; ` +
  `${errs.length} error(s)` + (errs.length ? ` — ${errs.map(n => `${n.path}: ${n.error}`).join('; ')}` : '') +
  `. Next: python3 scripts/granola_verify.py check --list ${LIST} --results ${RESULTS}` +
  (MODE === 'propose' ? ` && python3 scripts/granola_verify.py review --list ${LIST} --results ${RESULTS}` : ''))
return { mode: MODE, total: entries.length, verified: ok.length, errors: errs.length, [KEY]: nChanges, notes: out }
