#!/bin/zsh
# engram-tasks nightly runner — invoked by launchd (com.example.engram-tasks).
# Deterministic core: reconcile Reminders -> ledger, push pending tasks, report
# sync-watermark staleness, commit the ledger files locally (never push).
# Optional (ENGRAM_NIGHTLY_CLAUDE=1): attempt a headless /engram-sync first.
# Design contract (see .claude/skills/engram-tasks/SKILL.md): resumable, one
# writer at a time, never leaves the repo auto-committed in a red state.
set -uo pipefail

# The store: $ENGRAM_ROOT if set (the plist sets it), else the repo this script lives in.
ROOT="${ENGRAM_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="$ROOT/_sources/tasks"
WARN="$STATE/NIGHTLY-WARN.txt"
LEDGER_PATHS=("_manifest/task-queue.jsonl" "_manifest/task-log.jsonl" "_manifest/tasks-state.json" "_manifest/task-suggestions.jsonl")
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Own the log streams (launchd's StandardOut/ErrPath fds proved unreliable for
# child-process output — self-redirect instead; launchd paths stay as fallback).
exec >> "$HOME/Library/Logs/engram-tasks.log" 2>&1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

mkdir -p "$STATE"

# 1. last-success guard: RunAtLoad + StartCalendarInterval both fire; only one
#    run per ~day is wanted. Skip if the last success is under 20h old.
if [[ -f "$STATE/last-success" ]]; then
  now=$(date +%s); last=$(stat -f %m "$STATE/last-success")
  if (( now - last < 72000 )); then
    log "guard: last success $(( (now - last) / 3600 ))h ago (<20h) — skipping"
    exit 0
  fi
fi

# 2. one-writer lock (mkdir is atomic); a dead holder's lock is reclaimed once.
if ! mkdir "$STATE/lock.d" 2>/dev/null; then
  holder=$(cat "$STATE/lock.d/pid" 2>/dev/null || true)
  if [[ -n "${holder:-}" ]] && kill -0 "$holder" 2>/dev/null; then
    log "lock held by live pid $holder — exiting"
    exit 0
  fi
  log "reclaiming stale lock (pid ${holder:-unknown} dead)"
  rm -rf "$STATE/lock.d"
  mkdir "$STATE/lock.d" 2>/dev/null || { log "lock race lost — exiting"; exit 0; }
fi
echo $$ > "$STATE/lock.d/pid"
trap 'rm -rf "$STATE/lock.d"' EXIT

# 3. network wait, up to 5 min. Offline is a degrade, not a failure: the local
#    Reminders store works offline (device completions just arrive later).
net=0
for _ in {1..30}; do
  if ping -c 1 -t 2 1.1.1.1 >/dev/null 2>&1; then net=1; break; fi
  sleep 10
done
[[ $net -eq 0 ]] && log "no network after 5 min — proceeding offline (degraded)"

cd "$ROOT"

# 4. optional headless full sync (staged rollout: default OFF; flip the env var
#    in the plist only after a week of clean deterministic-core logs).
if [[ "${ENGRAM_NIGHTLY_CLAUDE:-0}" == "1" && $net -eq 1 ]]; then
  if [[ -z "$(git status --porcelain)" ]]; then
    log "headless /engram-sync attempt (30 min cap)"
    # dontAsk + an explicit allowlist: a tool not listed is denied, and the denial is
    # reported in the JSON result (permission_denials) instead of vanishing. List derived
    # from the tools/commands an interactive sync and its workflow agents used; extend it from
    # the denials the first real runs report. Deliberately absent: git push, rm, connector write
    # tools, SendMessage.
    CTMP="/private/tmp/claude-$(id -u)"   # Claude Code's per-user scratch root
    SYNC_TOOLS=(
      Read Glob Grep ToolSearch Skill Agent Workflow StructuredOutput
      "Edit(/$ROOT/**)" "Write(/$ROOT/**)"
      "Write(/$CTMP/**)" "Write(//tmp/**)"
      mcp__claude_ai_Gmail__search_threads mcp__claude_ai_Gmail__get_thread
      mcp__claude_ai_Granola__list_meetings mcp__claude_ai_Granola__get_meetings
      mcp__claude_ai_Granola__get_meeting_transcript
      "Bash(python3 scripts/*)" "Bash(/usr/bin/python3 scripts/*)"
      "Bash(python3 $CTMP/*)" "Bash(bash scripts/check.sh*)"
      "Bash(git status*)" "Bash(git diff*)" "Bash(git log*)" "Bash(git show*)"
      "Bash(git rev-parse*)" "Bash(git add *)" "Bash(git commit *)"
      "Bash(grep *)" "Bash(/usr/bin/grep *)" "Bash(head *)" "Bash(tail *)" "Bash(wc *)"
      "Bash(ls*)" "Bash(cat *)" "Bash(jq *)" "Bash(sort*)" "Bash(uniq*)" "Bash(cut *)"
      "Bash(sed -n *)" "Bash(echo *)"
    )
    RESULT="$STATE/nightly-claude.json"
    : > "$RESULT"
    claude -p "/engram-sync" --permission-mode dontAsk --allowedTools "${SYNC_TOOLS[@]}" \
      --output-format json </dev/null >"$RESULT" 2>>"$STATE/nightly-claude.log" &
    cpid=$!
    ( sleep 1800; kill -0 $cpid 2>/dev/null && { kill $cpid; echo "watchdog: killed headless sync at 30 min"; } ) &
    wpid=$!
    wait $cpid; crc=$?
    kill $wpid 2>/dev/null || true
    # Judge the run by its JSON result, not only the exit code. Prints one line; exit 1 = failure.
    if summary=$(/usr/bin/python3 - "$RESULT" <<'PYRES'
import json, sys
try:
    r = json.loads(open(sys.argv[1]).read())
except Exception as e:
    print(f"no parseable JSON result ({type(e).__name__}) - killed or crashed"); sys.exit(1)
den = r.get("permission_denials") or []
names = sorted({d.get("tool_name", "?") for d in den if isinstance(d, dict)})
line = (f"subtype={r.get('subtype')} is_error={r.get('is_error')} turns={r.get('num_turns')} "
        f"cost_usd={r.get('total_cost_usd')} denials={len(den)}{' ' + ','.join(names) if names else ''}")
print(line)
sys.exit(1 if r.get("is_error") or r.get("subtype") != "success" else 0)
PYRES
    ); then
      log "headless sync finished (rc=$crc): $summary"
      [[ "$summary" == *"denials=0"* ]] || echo "engram-tasks nightly $(date '+%Y-%m-%d %H:%M:%S'): headless sync hit permission denials (allowlist gap): $summary" >> "$WARN"
    else
      log "headless sync FAILED (rc=$crc): $summary"
      echo "engram-tasks nightly $(date '+%Y-%m-%d %H:%M:%S'): headless sync failed: $summary (result: $RESULT)" >> "$WARN"
    fi
    # A half-run must never be auto-committed: red check.sh below skips the commit.
  else
    log "repo dirty before headless sync — skipping the attempt"
  fi
fi

# 4.5 bridge canary: one cheap Apple Event with a short timeout, logged with timing.
#     Fast (<5s) = context healthy; slow/timeout = TCC-attribution or cold-app trouble in THIS
#     context (diagnostic only — the core still runs and fails safe on its own).
/usr/bin/python3 - <<'PYCANARY' 2>&1
import subprocess, time
t = time.time()
try:
    r = subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Reminders" to count lists'],
                       capture_output=True, text=True, timeout=60)
    print(f"canary: rc={r.returncode} {time.time()-t:.1f}s lists={r.stdout.strip()} "
          f"err={r.stderr.strip()[:120]}")
except subprocess.TimeoutExpired:
    print(f"canary: TIMEOUT at {time.time()-t:.0f}s — Apple Events parked in this context")
PYCANARY

# 5. deterministic core: reconcile -> extract --since watermark -> push.
core_rc=0
/usr/bin/python3 scripts/engram_tasks.py sync || core_rc=$?
if (( core_rc == 1 )); then
  log "engram_tasks sync completed with warnings (rc=1) — see err log"
elif (( core_rc != 0 )); then
  log "engram_tasks sync exited $core_rc (3=bridge failure, 4=mass-dismissal valve)"
  {
    echo "engram-tasks nightly $(date '+%Y-%m-%d %H:%M:%S'): engram_tasks.py sync exited $core_rc"
    echo "3 = bridge failure (no ledger transitions) · 4 = mass-dismissal valve or corrupt ledger."
    echo "Run '/usr/bin/python3 scripts/engram_tasks.py doctor' interactively, then re-run 'sync'."
  } > "$WARN"
fi

# 6. watermark staleness report (the degrade signal when connectors were absent).
/usr/bin/python3 - <<'PY' || true
import json, datetime, pathlib
p = pathlib.Path("_manifest/sync-state.json")
if p.exists():
    today = datetime.date.today()
    for src, meta in json.loads(p.read_text()).items():
        if not isinstance(meta, dict):
            continue
        wm = str(meta.get("watermark_date", ""))[:10]
        try:
            age = (today - datetime.date.fromisoformat(wm)).days
        except ValueError:
            continue
        if age >= 3:
            print(f"stale: {src} watermark {wm} ({age}d behind) — run /engram-sync")
PY

# 7. guarded local commit: only the ledger files may be dirty, and check.sh must
#    be green. Anything else -> leave working tree as-is + WARN (never a blind commit).
dirty=$(git status --porcelain)
if [[ -z "$dirty" ]]; then
  log "no ledger changes to commit"
else
  unexpected=$(git status --porcelain | awk '{print $2}' | grep -v -x -F -f <(printf '%s\n' "${LEDGER_PATHS[@]}") || true)
  if [[ -n "$unexpected" ]]; then
    log "non-ledger changes present — skipping commit:"
    echo "$unexpected" | while read -r f; do log "  $f"; done
    echo "engram-tasks nightly $(date '+%Y-%m-%d %H:%M:%S'): commit skipped, non-ledger changes present: $unexpected" >> "$WARN"
  elif bash scripts/check.sh >>"$STATE/nightly-check.log" 2>&1; then
    # Stage only ledger files that EXIST — a missing pathspec (e.g. task-suggestions.jsonl before
    # the first suggestion) makes `git add` abort atomically and stage NOTHING, so the commit would
    # then find an empty index and fail ("commit failed" with real changes stranded).
    for f in "${LEDGER_PATHS[@]}"; do [[ -f "$f" ]] && git add "$f"; done
    counts=$(/usr/bin/python3 scripts/engram_tasks.py status 2>/dev/null | head -1 || echo "")
    git commit -m "tasks: nightly reconcile+push${counts:+ (${counts})}" >/dev/null \
      && log "committed ledger changes (local only)" \
      || log "commit failed"
  else
    log "check.sh RED — commit skipped (see $STATE/nightly-check.log)"
    echo "engram-tasks nightly $(date '+%Y-%m-%d %H:%M:%S'): check.sh red, ledger changes left uncommitted" >> "$WARN"
  fi
fi

# 8. success marker (the <20h guard anchor). Core exit 3/4 still counts as a run
#    (the WARN file carries the signal); a crash before this line means the next
#    launchd fire retries — every step above is idempotent.
touch "$STATE/last-success"
log "nightly run complete (core_rc=$core_rc)"
exit 0
