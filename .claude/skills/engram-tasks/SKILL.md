---
name: engram-tasks
description: >-
  Operate the two-way Apple Reminders task bridge: push action items distilled into engram
  (meetings/email/chat) into the owner's Reminders area lists, and reconcile completions and deletions
  back into the committed task ledger. Use when the owner says "/engram-tasks", "push my tasks",
  "reconcile reminders", "task status", "what did I complete", or as the post-sync tasks pass inside
  `/engram-sync`. Deterministic Python core (cron-safe); the LLM steps (near-dup adjudication,
  completion-suggestion review) run only in a Claude session. An ACTION, not a gate — never wire it
  into check.sh.
---

# engram-tasks — engram ⇄ Apple Reminders, two-way

Distilled notes carry `## Action items` (`- <task> — <owner> (due <date>)`). This bridge pushes them
into Reminders (cross-device, the owner's real triage surface) and reads completions and deletions
back. macOS only: it drives Reminders through JXA (`scripts/reminders_bridge.js`, called by
`scripts/_lib/reminders.py`).

**PHI:** clinical-patient PHI is excluded from the store at ingest, so task text derived from notes is
safe to sync to iCloud Reminders. No new PHI surface.

## Semantics (what the owner's actions mean)

| In Reminders, the owner… | The next reconcile records… |
|---|---|
| completes a reminder | ledger `done` |
| deletes a reminder | ledger `dismissed` — **never re-pushed** |
| moves it to another list | accepted (tracked wherever it lives; logged as a `moved` event) |
| unflags it | `confirmed` — the kept-it signal for `triage-report` |
| edits due date/title | kept in Reminders only (not synced back) |

## Push policy

Extraction keeps EVERY item in the ledger (`/engram-brief` and the audit trail depend on it); only what
reaches Reminders is gated, by `push_policy()` in `scripts/engram_tasks.py` (the single owner):
- `owner_class: waiting_on` (someone else's task) → `held`, never pushed;
- `owner_class: mine` → pushed only with a `due` or a non-zero `priority`; undated, unprioritized
  "project state" lines → `held`.

`held` is not terminal: every push re-evaluates held rows. `push --policy off` pushes everything
eligible. Owner = the people.md `self` row (`_lib.actions.self_aliases`).

## Routing — configuration, `_manifest/constants/reminders.json`

`domain_to_list` maps each domain to an owned list; a waiting-on row goes to that list's
`"<Owned> — Waiting On"` sibling (derived, never hand-listed), so each topic's owned and waiting lists
pair in the sidebar. Domains with no owned list fall back to `inbox_list` (mine) or `waiting_list`
(waiting on). `entity_rules` send finance tasks that clearly belong to one entity (an LLC, a trust) to
that entity's list; `never_write` names lists engram must never create a reminder in (e.g. an hours or
billing log kept in Reminders); `extra_lists` widens the reconcile read scope to hand-managed lists.
Lists are created on demand. `apply_config` validates the file at import (unknown key or domain → error).

**Every list engram routes INTO must stay top-level in the Reminders sidebar, never inside a Group**:
Groups are not scriptable and a group-nested list is unreachable as an object, so nothing can be
created in or moved into it (`reminders_bridge.js` finding 9). `ensure_lists` refuses to mint a
duplicate of a group-hidden name (the row lands as `status=error` instead of silently forking a list).

**Signals:** created flagged (= untriaged) · all-day due when the source states one · a due-less
waiting-on item gets due = push date + 7d, labelled `due-auto:` in its notes (the only permitted
non-source date) · priority: overdue/≤3d high, ≤14d medium.

## Run it

```bash
python3 scripts/engram_tasks.py doctor [--live]           # TCC + feature probe (+ live smoke in a list "Engram Test")
python3 scripts/engram_tasks.py status                    # counts, overdue, watermark (read-only)
python3 scripts/engram_tasks.py sync                      # reconcile → extract --since watermark → push
python3 scripts/engram_tasks.py extract --only /tmp/synced.txt   # the /engram-sync tasks pass
python3 scripts/engram_tasks.py extract --seed            # one-time: seed from digests/open-loops.md
python3 scripts/engram_tasks.py push [--dry-run]          # policy gate, live dedup, create
python3 scripts/engram_tasks.py reconcile [--force]       # completions/deletions → ledger
python3 scripts/engram_tasks.py suggest-complete --only F # deterministic completion hints
python3 scripts/engram_tasks.py complete --ids sid…       # apply confirmed suggestions
python3 scripts/engram_tasks.py routing-report            # the owner's re-filings vs predictions
python3 scripts/engram_tasks.py triage-report             # confirm/dismiss rates by facet
python3 scripts/reminders_sweep.py --help                 # bulk retire/complete (ledger-first)
```

Exit codes: 0 ok · 1 warnings · 2 usage · 3 bridge failure (**zero ledger transitions**) · 4 abort with
no writes (corrupt ledger, or the mass-dismissal valve: >50% and ≥5 pushed rows read missing, the
signed-out-iCloud signature; `--force` after checking).

**First run:** `doctor` interactively — macOS asks for Automation permission (Terminal/your editor →
Reminders). Expect `dump_incomplete` to take minutes on a large list history (it scans completed items
too); scoped reads take about a second.

## In-session (LLM) steps during `/engram-sync`

- Near-dups held as `needs_review` → `push --adjudicated 'task:<id>=create|skip|adopt:<reminder_id>'`.
- Judgment routing → `push --route 'task:<id>=<List>'` (any list except a `never_write` one).
- **Routing learning loop:** reconcile logs every hand re-filing as `moved`; `routing-report` groups
  them; fold recurring patterns into `entity_rules` / `domain_to_list` (+ a test), then
  `routing-report --rebaseline`.
- **Triage learning loop:** unflag ⇒ `confirmed`, delete ⇒ `dismissed`; `triage-report` shows accept /
  reject rates by source/domain/owner-class/due. Fold a MECHANICAL dismissal pattern into
  `EXTRACT_SKIP_RULES` (+ a test), and test any candidate skip regex against the KEPT set first (a
  phrase shared by kept and dismissed items is not a skip signal). Recurring semantic over-extraction
  from one source → a reviewed distiller-prompt refinement.
- Never auto-complete: review `suggest-complete` output with the owner, then `complete --ids/--reject`.

## Ledger (committed, `_manifest/`)

`task-queue.jsonl` (one row per task id; open → pushed → done | dismissed, plus held / needs_review /
skipped / error; an id in the ledger is never re-extracted) · `task-log.jsonl` (append-only
transitions) · `task-suggestions.jsonl` · `tasks-state.json` (extract watermark + seeded flag).
Machine-local runtime in gitignored `_sources/tasks/`. Task ids hash the ref + due-stripped text, so a
note fix that only changes a task's owner or due does not re-extract it — edit the row by hand.

## Nightly job (optional, launchd)

`scripts/tasks_nightly.sh` runs the deterministic core daily: reconcile → extract → push, a watermark
staleness report, and a LOCAL commit of the ledger files only when nothing else is dirty and check.sh is
green (anything unexpected → `_sources/tasks/NIGHTLY-WARN.txt`, no commit; it never pushes).
The template `scripts/launchd/com.example.engram-tasks.plist` documents the install: copy it to
`~/Library/LaunchAgents/<your label>.plist`, replace the label, the `/Users/you` paths and
`ENGRAM_ROOT`, build the applet wrapper named in it (an `.app` is the identity macOS can grant
Automation to; bare zsh under launchd cannot be granted), then
`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<label>.plist`. The launchd context needs its own
Automation grant (System Settings → Privacy & Security → Automation) — interactive grants do not carry
over. **`ENGRAM_NIGHTLY_CLAUDE` is 0 (off) by default**; set it to 1 only to also attempt a headless
`claude -p "/engram-sync"` with an explicit tool allowlist (denials are logged, never silently skipped).

## Guardrails

- ACTION, not a gate: never wire into check.sh; a bridge failure during a sync is noted, never blocking.
- Never edits notes, digests or `open-loops.md`.
- One Reminders writer at a time (the nightly lock covers launchd; don't run two pushes at once).
- Never set properties on a just-pushed reminder object; creates are batched (per-item re-resolution
  is ~10s+ each).
