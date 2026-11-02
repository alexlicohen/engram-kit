# engram-kit

A kit for building your own **engram**: a single-owner, file-based second brain that LLM agents
(Claude Code) and you read and maintain together. Plain markdown + YAML frontmatter + grep, versioned
in git — deliberately **no embeddings, no database, no daemon**. Retrieval is index-then-drill
(`INDEX.md` → domain `_index.md` → note), which agents navigate natively.

This is the machinery of a private store, published without any of its notes: the conventions, gates
and pipelines that made LLM-written notes trustworthy enough to rely on.

- **Schema + lint gate.** Every note carries typed frontmatter and a stable `provenance.ref`;
  `scripts/check.sh` regenerates all derived views and fails on drift, duplicate ingestion, broken
  wikilinks or off-schema notes. CI runs the same gate.
- **Contract-guarded distillation.** A resumable, one-shot-contract sync pipeline turns source items
  into atomic notes. Shipped sources: Granola meetings (with a transcript-verify pass after every wave),
  Gmail (connector; grown threads redistilled in place), a secondary mailbox from an mbox export,
  Outlook work mail (`.eml` export or Microsoft Graph), iMessage and WhatsApp. Distiller prompts carry
  FIDELITY RULES that measurably suppress fabrication.
- **Integrity gates.** Distilled notes get frozen identity frontmatter; sanctioned corrections go
  through two guarded writers and an append-only override ledger.
- **Fidelity loop.** Spot-check audits feed an append-only corrections ledger that the next wave's
  distiller reads.
- **PHI exclusion, fail-closed.** Clinical-patient data is screened at ingest (sticky holds for work
  mail, a review gate for sensitive chat items) and audited store-wide.
- **Task bridge.** Action items flow into Apple Reminders and completions flow back into a committed
  ledger (`/engram-tasks`), with an optional nightly launchd job.
- **Agent tooling.** Claude Code skills for recall, write-back, sync, audit, eval, dossiers and a
  clarify interview, plus a read-only MCP server for Claude Desktop.

Read `docs/ARCHITECTURE.md` for how the parts fit, `docs/LESSONS.md` for why they exist, and
`AGENTS.md` for the store conventions agents follow (Claude Code loads it through `CLAUDE.md`).

## Quick start

```bash
git clone <your copy of engram-kit> ~/engram && cd ~/engram
pip install pytest                      # the store scripts themselves are stdlib-only
make verify && bash scripts/check.sh    # both green on the seed store
```

## Make it yours, in this order

1. **Set the owner.** In `_manifest/people.md`, replace the fictional `self` row with your name, spoken
   variants and e-mail addresses. Every script that needs to know whose store this is reads that row.
   Replace the other fictional rows with real people as they appear (unknowns get `verify: true`).
2. **Name your domains.** Edit `_manifest/constants/domains.json`, rename the dirs, update `INDEX.md`
   (the full checklist is at the end of `docs/ARCHITECTURE.md`). Set your tag vocabulary in
   `_manifest/constants/tag_vocab.json` and mirror it in `_manifest/schema.md`.
3. **Replace the seed notes** (each domain has one or two fictional examples) and keep the gate green:
   `make verify && bash scripts/check.sh`, then commit the regenerated views. CI
   (`.github/workflows/verify.yml`) enforces the same on every push.
4. **Install the skills.** Project skills are in `.claude/skills/`; symlink the three in
   `_global-skills/` into `~/.claude/skills/` to recall from (`engram`) and write back to
   (`engram-capture`) the store from any Claude Code session. Point `ENGRAM_ROOT` at the store if it is
   not at `~/engram`.
5. **Connect your sources** (each optional): put every address you send mail from on the `self` row,
   fill `_manifest/constants/mail_routing.json` (PHI-screen domains, routing rules) and
   `_manifest/constants/reminders.json` (task lists), keep the `_manifest/sync-state.json` keys for the
   sources you use, and run `/engram-sync`. `_manifest/SYNC.md` has one section per source with its
   prerequisites (Claude connectors, an `.eml` export or an Azure app registration for Graph, Full Disk
   Access for `chat.db`, Apple Reminders), its stages and its traps; it also lists what a new source needs.
   For Granola, generate the speech-to-text glossary first (`python3 scripts/granola_glossary.py`).
6. **Audit what the LLM writes.** Run `/engram-audit` on the first batches from any distiller and
   measure its fabrication rate before you trust it.
7. **Desktop recall** (optional): wire `scripts/engram_mcp.py` into Claude Desktop (read-only).

## Requirements

Python 3.10+ and `pytest`. Node is needed only for `scripts/check_distiller_prompts.py`. The
distillation pipeline and skills assume [Claude Code](https://claude.com/claude-code) with its
Workflow tool and MCP connectors (Gmail, Granola) available to subagents; the distillers launch workers
as Claude Code's stock `general-purpose` subagent (one `AGENT_TYPE` constant per distiller); pass
`agentType` in the workflow args to run them as your own subagent instead.
iMessage, WhatsApp, the legacy Outlook cache export and the Reminders bridge are macOS-only. The store
itself is plain markdown and works with anything.

The regression suite (`scripts/tests/`, run by `make verify`) ships only tests whose fixtures are
synthetic. Tests the original private store built from its own data (registry rows, ledgers, notes,
messages) are not included, so the Granola, Gmail, task-bridge, clarify-queue and clarify write-back
paths have little or no test coverage here; add your own before you change them.

## Secrets

Never put credentials in notes. If you want secrets in the repo, use an encrypted directory
(e.g. [git-crypt](https://github.com/AGWA/git-crypt)) and back its key up outside the repo; this kit
ships without one. `scripts/backup_sensitive.py` writes off-machine copies of the PHI/sensitive
exclusion ledgers into `_vault/` only when `.gitattributes` encrypts it
(`_vault/** filter=git-crypt diff=git-crypt`); otherwise it keeps local tarballs only. Graph
credentials (`ENGRAM_GRAPH_CLIENT_ID`, `ENGRAM_GRAPH_TENANT`) live in your environment, and the OAuth
token cache in your home directory (`0600`), never in the repo.

## Local review UI

`python3 scripts/review_server.py` serves the `/clarify` queue and the sensitive chat hold queues at
`http://localhost:8765`. It binds 127.0.0.1 only and has no authentication by design (single-user,
loopback-only); never expose it through a tunnel or proxy.

## License

MIT — see `LICENSE`.
