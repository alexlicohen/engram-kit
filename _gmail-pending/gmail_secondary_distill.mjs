export const meta = {
  name: 'gmail-secondary-distill',
  description: 'Distill extracted secondary-account email threads into engram atomic notes (batched, resumable)',
  phases: [
    { title: 'Setup', detail: 'count the work-list' },
    { title: 'Distill', detail: 'one worker agent per line-slice of threads' },
  ],
}

// Robust to `args` not threading through a re-launch: fixed secondary paths are defaults.
const A = (typeof args !== 'undefined' && args) ? args : {}
// Worker subagent type. 'general-purpose' is Claude Code's stock subagent (all tools, incl. the
// MCP connectors). To pin tools or a model, define your own subagent and pass args.agentType.
const AGENT_TYPE = A.agentType || 'general-purpose'
const ROOT = A.root || '$ENGRAM_ROOT'
const THREADS = A.threadsDir || (ROOT + '/_sources/gmail-secondary-threads')
const PEOPLE = A.peopleFile || (ROOT + '/_manifest/people.md')
// Wave pinned by a CONSUMED contract that scripts/resolve_wave.py validated (count+sha256+mode)
// and atomically claimed BEFORE this distiller launched. The Workflow runtime is pure JS (no fs,
// no dynamic loader), so the wave-contract guard lives in Python now; a Setup agent reads the
// consumed contract with its Read tool and hands back the pinned wave path + expected count.
const SOURCE = 'gmail-secondary'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
const DROPS = ROOT + '/_sources/gmail-secondary-drops.d'
const BATCH = A.batch || 12
// What the secondary mailbox is (configure for yours): a one-line description the distiller reads,
// and the domain it defaults to when a thread is genuinely ambiguous. The canonical case is a
// dedicated account for a company you run; then most threads are that company's business.
const ACCOUNT_DESC = A.accountDesc || "a dedicated account for a company the owner runs (its business of record: legal/IP, finance/banking, fundraising, vendors, board, advisors)"
const PRIOR_DOMAIN = A.priorDomain || 'ventures'
const TAG_VOCAB_FILE = ROOT + '/_manifest/constants/tag_vocab.json'


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
  return `You distill EMAIL THREADS into engram atomic notes. engram is the owner's private
markdown knowledge base. Conventions: ${ROOT}/CLAUDE.md and ${ROOT}/_manifest/schema.md.
First, read the people registry once: ${ROOT}/_manifest/people.md (resolve participants to canonical names).

SOURCE MAILBOX: these threads come from the owner's SECONDARY mail account: ${ACCOUNT_DESC}.
Some threads are personal or other matters that happened to use this address.

YOUR WORK-LIST: run \`sed -n '${start},${end}p' ${WAVE}\` — that prints your thread records, one JSON
object per line, each with a "thrid" field. Process EACH line (one note per thread). For a thread,
read its extracted text at ${THREADS}/<thrid>.txt (header with subject/labels/participants/dates,
then messages oldest->newest with quoted reply chains), then decide KEEP or DROP.

DROP (status:"dropped", set reason) when the thread has no durable content:
  - pure logistics/scheduling with no lasting info, one-word replies ("thanks","got it"),
    receipts/confirmations, automated mail with a trivial human reply, calendar invites only,
    bank/cap-table/e-sign NOTICES with no decision (statement-ready, signature-completed).
  - reason "phi" if the thread is fundamentally about an IDENTIFIABLE clinical patient
    (name/initials+MRN/DOB tied to diagnosis/treatment). Do NOT write such a note.
  Dropped threads: write NO note. INSTEAD write a durable drop marker with the Write tool to
  ${DROPS}/<thrid>.json containing exactly
  {"thrid":"<thrid>","reason":"<short reason>"} — this records the drop so it is never re-processed.

KEEP (status:"done"): write ONE markdown note. Compress to atomic facts — DISTILLED, not verbatim.
Never fabricate; if unsure, omit. This store is single-user: COI/business/financial/family content
is NORMAL — keep it. The ONLY exclusion is identifiable clinical-patient PHI: never write patient
identifiers or case detail; de-identify any incidental clinical mention.
SECRETS HYGIENE: never write an SSN, full bank account/routing number, password, API key, or a
one-time verification code into a note — those belong in the encrypted _vault, not the store. For a
filing that contains them (e.g. a Corporate Transparency Act beneficial-ownership filing, a tax
form), record that the filing happened and its purpose/parties, and OMIT the identifier values and
any personal DOB/SSN of the owners. A thread that is ONLY an OTP/verification code or password
reset has no durable content -> DROP it.

NOTE FORMAT (exact frontmatter, then body):
---
title: "Concise human title (no Re:/Fw:)"
date: YYYY-MM-DD            # the thread's most representative date (decision/last substantive msg)
aliases: [...]              # 1-3 alternate titles / key entities for retrieval (person, deal/entity
                           #   name, doc name); [] only if nothing obvious
tags: [...]                 # ONLY from the controlled vocabulary file (see CONTROLLED TAGS)
domain: <one domain>        # exactly one of the dirs below
subpath: null               # or grants|papers|lab-ops (research) or travel (personal)
type: event                 # ALWAYS "event" for these (dated email threads)
source: gmail
provenance:
  ref: "gmail:thread:<thrid>"
  ingested: ${today}
  method: distilled
  note: "gmail-secondary-account mailbox"
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
  Route by CONTENT, not the sender domain alone. A company the owner runs or advises -> ventures.
  Personal real-estate/LLC/trust/tax/investment -> finance. Studies/grants/papers -> research.
  Family/school/social/travel/home -> personal.
  PRIOR: when a thread is genuinely ambiguous, default to **${PRIOR_DOMAIN}** (this mailbox's main
  purpose; NOT unsorted); use unsorted only if a thread clearly fits no domain (then add tag "flagged").
  DUAL-DOMAIN: file under the PRIMARY domain (where the decision/outcome lands), and add the tag
  "overlap" plus name the second domain in the body. Do not duplicate the note.

CONTROLLED TAGS — read ${TAG_VOCAB_FILE} ONCE with the Read tool: its "tags" array is the COMPLETE
allowed vocabulary (its single owner). A tag is valid ONLY if it appears there, spelled exactly. Never
coin a tag, a singular/plural variant or a category label ('email', 'misc' are NOT tags); if nothing
fits, use just the domain tag + 'correspondence'. Never add a word to the vocabulary.
  Always include "correspondence" (these are emails) plus the domain tag.

LINKING RULE: leave related, supersedes, superseded_by ALL as [] — do NOT add wikilinks. All
cross-linking and supersession is computed later by the synthesis/backlinks pass.

FILE NAMING: slug = lowercase kebab of the title, concise. To avoid collisions with the personal
Gmail pass (which used \`-<thrid_last6>\`) AND across parallel workers, the filename MUST end with
\`-sec\` + the last 8 chars of the thrid:
\`${ROOT}/<domain>/<YYYY-MM-DD>-<slug>-sec<thrid_last8>.md\` (e.g. 2026-01-15-vendor-nda-sec0a1b2c3d.md).
Create the file with the Write tool; do not print its contents.

RELIABILITY (CRITICAL): create every KEEP note with the **Write tool** — NEVER bash/cat/heredoc/
printf. The Write tool result confirms the file landed; only report status:"done" for a thread once
its file is actually written. NEVER report "done" for a note you did not write. Process the threads
one at a time and finish writing EACH file before moving on. If a Write fails, retry it once. At the
end, the count of "done" entries you return MUST equal the number of files you actually created.

PARTICIPANTS: map emails/names to canonical names via people.md. Any human sender/recipient NOT in
people.md -> include their display name in the note AND report them in new_people (name + email) so
the registry can be updated. Do not invent relationships.

Return via StructuredOutput with its 'threads' argument set to the ARRAY itself (never { threads: [...] } nested inside it): [ { thrid, status, note: "<relpath under root or null>", domain,
title, reason: "<drop reason or null>", new_people: [{name,email}] } ] — one entry per thrid line.`
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
  `args.today). If the file is missing or unreadable, return ` +
  `{"wave": "", "expected_count": -1, "source": "MISSING", "mode": "", "today": null}.`,
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

const all = results.filter(Boolean).flat()
const done = all.filter(t => t.status === 'done')
const dropped = all.filter(t => t.status === 'dropped')
log(`distilled: ${done.length} notes, ${dropped.length} dropped, ${all.length}/${count} accounted`)
return { total: count, accounted: all.length, done: done.length, dropped: dropped.length, threads: all }
