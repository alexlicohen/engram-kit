@AGENTS.md

<!-- Claude Code only below. -->
Project skills live in `.claude/skills/`: `/clarify`, `/engram-audit`, `/engram-brief`,
`/engram-dossiers`, `/engram-eval`, `/engram-sync`, `/engram-tasks`. The three cross-project skills live in
`_global-skills/` (`engram` = recall, `engram-capture` = write-back, `engram-distill-session` = turn a
finished Claude Code session into notes); to use them from any session, symlink each into
`~/.claude/skills/<name>`. Verify discovery with a fresh `claude -p` from an unrelated directory —
an `ls` of the link proves nothing about discovery.

Read-only recall from Claude Desktop goes through the local stdio MCP server `scripts/engram_mcp.py`
(see `docs/ARCHITECTURE.md` › Agent tooling for its sandbox contract and wiring).
