export const meta = {
  name: 'imsg-reclassify',
  description: 'Re-judge over-flagged iMessage sensitive holds: release ordinary context to the store, keep only genuinely-private',
  phases: [{ title: 'Setup' }, { title: 'Reclassify' }],
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
const SOURCE = 'imsg-reclass'
const CONTRACT = ROOT + '/_sources/' + SOURCE + '.contract.consumed.json'
const CONFIRMED = ROOT + '/_sources/imsg-sensitive/confirmed'
const BATCH = A.batch || 16

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
  properties: { items: { type: 'array', items: { type: 'object', properties: {
    fp: { type: 'string' }, decision: { type: 'string', enum: ['release', 'keep'] },
    domain: { type: ['string', 'null'] }, reason: { type: ['string', 'null'] },
  }, required: ['fp', 'decision'] } } },
  required: ['items'],
}

function prompt(start, end) {
  return `You RE-JUDGE iMessage notes that an over-aggressive pass quarantined as "sensitive". engram's
actual rule (${ROOT}/CLAUDE.md): the store is single-user, so the ONLY truly-restricted thing is
identifiable clinical-PATIENT PHI. Family/personal content — including a family member's
hospitalization, surgery, illness, recovery, or pregnancy, and normal family money (gifts/loans/help)
— is ORDINARY PERSONAL CONTEXT and BELONGS IN THE STORE. The previous pass wrongly quarantined most of it.

WORK-LIST: \`sed -n '${start},${end}p' ${WAVE}\` — each line {fp} is a held note at ${ROOT}/<fp>.
For EACH, read the note and decide:

RELEASE (it belongs in the store) — the DEFAULT for almost everything, including:
  - any family member's medical event/condition/recovery/pregnancy mentioned as life context
  - normal family financial matters (gifts, loans, who's funding what, account logistics among family)
  - ordinary relationship/logistics/trip/home content
  -> read its \`domain:\` frontmatter; move the file with the Bash tool:
     \`mv "${ROOT}/<fp>" "${ROOT}/<domain>/<basename>"\`  (if the target exists, use <basename minus .md>-2.md)
     Then set \`related: []\` in the moved note IF its related: list contains any [[link]] (the original pass
     sometimes fabricated links) — use the Edit tool to replace the related: line with \`related: []\`.

KEEP held (genuinely private — a MINORITY) ONLY for:
  - mental-health / therapy / psychiatric content about a specific person
  - intimate / sexual / private-relationship-conflict content (e.g. someone considering divorce)
  - active legal disputes / litigation / privileged matters / allegations (e.g. a lawsuit, sexual-assault claim)
  - a confidential THIRD-PARTY matter (someone else's private job search, affair, private diagnosis they'd want withheld)
  -> the sensitive area is precious (it was clobbered once) — REFUSE TO OVERWRITE an existing file there.
     With the Bash tool, run: \`test -e "${CONFIRMED}/<basename>" && { echo "REFUSE: ${CONFIRMED}/<basename> already exists — not overwriting" >&2; false; } || mv "${ROOT}/<fp>" "${CONFIRMED}/<basename>"\`
     If that refuses (nonzero exit), do NOT rename with a suffix and do NOT retry — leave \`${ROOT}/<fp>\`
     where it is, and report this fp's decision as "keep" with reason "conflict: target already exists in confirmed/ — needs manual resolution".

PHI: if a note actually contains identifiable clinical-PATIENT data (e.g. if the owner is a clinician: a
patient's name/MRN/DOB+dx) -> it must NOT go in the store; move to ${CONFIRMED}/ and note reason "phi".

Return via StructuredOutput with its 'items' argument set to the ARRAY itself (never { items: [...] } nested inside it): [ { fp, decision:"release"|"keep", domain, reason } ] — one per fp. Only report
after the mv (and any Edit) actually succeeded.`
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
log(`reclassify ${count} held notes, batch ${BATCH} (wave ${WAVE})`)
const nb = Math.ceil(count / BATCH)
const ranges = []
for (let b = 0; b < nb; b++) ranges.push([b * BATCH + 1, Math.min((b + 1) * BATCH, count)])

phase('Reclassify')
const res = await parallel(ranges.map(([s, e], bi) => () =>
  agent(prompt(s, e), { label: `reclass:b${bi + 1}/${nb}`, phase: 'Reclassify',
    schema: RESULT_SCHEMA, agentType: AGENT_TYPE }).then(r => (r && r.items) || [])
))
const all = res.filter(Boolean).flat()
const rel = all.filter(i => i.decision === 'release').length
const keep = all.filter(i => i.decision === 'keep').length
log(`released ${rel} to store, kept ${keep} genuinely-sensitive`)
return { total: count, released: rel, kept: keep, items: all }
