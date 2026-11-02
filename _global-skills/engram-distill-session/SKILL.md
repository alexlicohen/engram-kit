---
name: engram-distill-session
description: >-
  At the END of a document-editing Claude session (an NIH grant, a manuscript — work whose
  deliverable is a `.docx`, not a git repo), distill that whole session's SETTLED decisions in bulk
  into the owner's engram second brain. These in-flow decisions reach engram through no connector
  (no git, no Granola, and email carries only the questions, not the resolutions) and mostly slip past
  one-at-a-time `/engram-capture`. Use when the owner says "/engram-distill-session", "distill this session
  into engram", "capture this session's decisions", "pull the decisions from this grant/paper session".
  NOT for coding/tooling sessions (git already captures those) — see Scope. Verifies every fact against
  the transcript, refuses clinical-patient PHI, writes locally + commits (never pushes).
---

# engram-distill-session — bulk end-of-session write-back (doc-editing sessions only)

The batch sibling of `/engram-capture`. Where `/engram-capture` saves ONE fact the moment you decide
it, this distills a WHOLE finished session's settled decisions in one pass — for the one session class
where that pays. It reuses the extractor + the guarded `engram_capture.py` write path.

```bash
E=$ENGRAM_ROOT
```

## Scope — when this pays, and when it does NOT

A 2026-07-02 pilot measured 3 session classes. Only ONE is worth distilling:

- ✅ **Grant / paper `.docx`-editing sessions** — decisions made in the editing flow (an aim
  restructured, a method scoped, a section cut with rationale, a new funding mechanism adopted) have
  **no git repo**, **no Granola meeting**, and the collaborator email holds only their *questions*, not
  the *resolutions*. Connector-invisible, and ~⅔ slip past one-at-a-time capture. **This is the gap.**
- ❌ **Coding / tooling sessions** — everything durable is already in **git commits + the repo**.
  Distilling them duplicates git at real fabrication risk. Skip; use `/engram-capture` for the odd milestone.
- ❌ **Sessions whose facts are read out of Granola/Gmail** — already reach engram via the connectors.

If the session isn't clearly a doc-editing session with real in-flow decisions, **stop and say so** —
don't distill for its own sake. There is deliberately no "distill all my sessions" mode (the pilot
rejected it: redundant + high fabrication).

## Workflow

**1. Locate the transcript + note its SESSION-ID (the provenance source).**
The slug is the project's cwd with every `/` replaced by `-`, under `~/.claude/projects/<slug>/`.
```bash
P=~/.claude/projects/-Users-<you>-projects-<project>     # e.g. -Users-you-projects-my-paper
python3 $E/scripts/extract_conv.py --list "$P"          # newest first; STUB = a bridge pointer, skip it
```
Pick the target (usually the newest real transcript — the session just finished). Its `.jsonl`
**basename is the session-id** used for provenance below. Then extract the conversation (tool-noise
stripped → a distillable ~15-50k-token text):
```bash
python3 $E/scripts/extract_conv.py "$P/<session-id>.jsonl" /tmp/conv.txt   # or: --latest "$P" /tmp/conv.txt
```

**2. Distill with ONE cold subagent.** Keep the raw transcript out of your own context — delegate.
Give the agent this brief verbatim (the filters are load-bearing — they are the failure modes the
pilot found):
> Read `/tmp/conv.txt` (a cleaned user/assistant transcript from a grant/paper editing session).
> Extract only the DURABLE facts that belong in a second brain: decisions actually made, commitments
> / open loops, new people + roles, corrections, key factual outcomes. For each: title · one-line
> fact · domain · kind (decision/commitment/person/correction) · the SPECIFICS to verify (numbers,
> dates, DOIs, dollar amounts, names). Apply these filters and state them:
> • **SETTLED-vs-PROPOSED** — record only what was actually decided/approved; NEVER the assistant's
>   "ready to apply on your go" drafts or suggestions as if adopted.
> • **DROP the mock-review** — an AI-simulated study-section critique / suggested citations are NOT
>   real outcomes; never record simulated scores or AI-proposed refs as facts.
> • **DROP tooling/debugging** mechanics.
> • **NOVELTY** — mark each "session-only" (reaches engram via no connector) vs "redundant" (git /
>   Granola / email already carry it); propose only session-only facts for capture.
> • **Flag any clinical-patient PHI** (identifiable patient + dx/treatment). Return compactly.

**3. VERIFY every candidate against the transcript — MANDATORY, non-negotiable.**
For each specific (dollar amount, deadline, DOI, FOA number, name), `grep` the transcript and confirm
it appears **verbatim** and is **settled** (not a proposal or a flagged-open item). Drop or correct
anything you can't confirm.
> This gate is why the skill exists rather than an auto-pipeline: on the first real run it caught a
> **hallucinated DOI** and an **unverified "decision"** in the distiller's own summary. An unguarded
> auto-distill would have written both into the store. Always check the source, not the summary.

**4. Review with the owner.** Present the verified candidates (title + one-line + novelty). He
approves / edits / drops. **Never auto-write** — human-in-loop is required.

**5. Capture each approved fact** via the guarded helper — provenance points at the **SOURCE session**,
not the current one, so each fact traces to the conversation it came from:
```bash
python3 $E/scripts/engram_capture.py --session "<SOURCE session-id>" \
  --domain <dom> --title "<title>" --tags "a,b" --type event \
  --kind decision|commitment|person|correction [--due <YYYY-MM-DD>] [--owner <name>] \
  [--participants "X,Y"] --body-file <body.md> [--no-commit]
```
Each fact gets `claude-chat:<source-session-id>:c<n>` (auto-incremented). A commitment with a deadline
(`--kind commitment --due`) also lands in `digests/open-loops.md`. Use `--no-commit` on each and do one
combined commit if capturing several, or let each commit itself. The helper runs `check.sh` + commits
locally — **never pushes** (the batch rides the next `/engram-sync` or `/wrap-up` push).

Tags are hard-validated against the controlled vocabulary (`$E/_manifest/schema.md` §Controlled tag
vocabulary) — an off-vocab tag rejects that capture entirely (nothing saved for it), so pick from the
list rather than inventing one, or a batch capture can silently lose an item.

## Guardrails
- **Verify against the source, always.** The distiller summary is not trustworthy for specifics.
- **Settled only.** Proposed edits, AI-simulated reviews, and suggested (unverified) citations are NOT facts.
- **Provenance = the source session-id**, and it must be a real doc-editing session (not the meta-session
  you're running this from).
- **No clinical-patient PHI** — the helper is fail-closed; if it refuses, surface it to the owner, only
  `--phi-reviewed` if they confirm it isn't patient PHI (they are not the person's treating physician).
- **Local commit, never push.** Human-in-loop; never auto-write; don't distill out-of-scope sessions.
