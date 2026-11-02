#!/usr/bin/env python3
"""Compute the PENDING Gmail distillation work-list (idempotent / resumable).

pending = <keep or wave list>  MINUS  threads already distilled on disk
          (a note exists whose provenance ref is gmail:thread:<thrid>)
          MINUS  PHI-hold threads (optional; from the phi report, level high|medium)
          MINUS  previously-dropped threads  PLUS the RE-OPENED ones (see below)

Because "done" is derived from the notes themselves, an interrupted run is resumed
simply by regenerating the work-list — no workflow-internal state needed.

AUTOMATIC UN-DROP. A drop is durable (`_sources/gmail-drops.d/<thrid>.json`)
and used to be PERMANENT — so a thread dropped as an offering blast / logistics noise that later
GREW because **the owner themself replied** was excluded forever: `gmail_worklist` skipped it as
`prev-dropped`, and `gmail_update_worklist` could not route it either (no note exists to update).
The case that found this: an investment-offering blast was dropped, and days later the owner
replied into it with a real decision that the store then never saw.

This script is the single owner of the pending decision, so the un-drop lives HERE (no second
script). A dropped thread in the wave is RE-OPENED as a NEW note iff ALL of:
  1. a durable drop marker exists for its canonical key, and
  2. its rendered thread (`_sources/gmail-threads/<thrid>.txt`) has a message TIMESTAMPED AFTER
     the drop (drop time = the marker's own `dropped_at`, else the marker file's mtime — markers
     are local-only, `_sources/` is gitignored, so mtime faithfully records when it was written), and
  3. at least one of those post-drop messages is FROM the owner (sender in `gmail_select.SELF`).
Growth WITHOUT an owner reply is not a reason (a second newsletter blast is still a blast): those
stay dropped and are printed as `grown-but-still-dropped` so the operator can see them.
The marker is never deleted — it is MOVED to `gmail-drops.d/reopened/<stem>.<YYYYMMDDTHHMMSS>.json`
with `reopened_at` / `reopened_reason` appended, which also makes the un-drop idempotent (a second
run finds no marker, so it re-opens nothing).

Usage:
  python3 scripts/gmail_worklist.py <in-list.jsonl> <out-pending.jsonl> \
        [--phi-report _sources/gmail-phi-report.jsonl] [--drops _sources/gmail-drops.jsonl] \
        [--root <repo-root>]
"""
import sys, os, json, re, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.provenance import gmail_thread_key
from _lib.notes import gmail_done_map
from gmail_select import SELF as SELF_ADDRS   # single owner of "the owner's own addresses" — do NOT copy

# scripts/gmail_worklist.py -> repo root is one level up. Deterministic (not cwd-dependent).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def done_thrids(root):
    """Canonical INT keys of every gmail thread already distilled on disk. Thin wrapper over the
    single-owner scan _lib.notes.gmail_done_map (key -> path); see there for the base-agnostic
    (hex==decimal) rationale that makes a re-surfacing historical thread read as already-done."""
    return set(gmail_done_map(root).keys())


def exclusion_keys(wave_dir, drops_path=None, phi_path=None):
    """Canonical INT keys to EXCLUDE from a gmail worklist: PHI-hold threads (phi report, level
    high|medium) + previously-dropped threads (`--drops` file + durable `<wave_dir>/gmail-drops.d/`
    markers). SINGLE OWNER shared by gmail_worklist (new-note wave) and gmail_update_worklist
    (grown-thread update wave) so the two can never drift on what "excluded" means. Returns
    (drops, phi_hold)."""
    phi_hold, drops = set(), set()
    if phi_path:
        for line in open(phi_path, encoding='utf-8'):
            r = json.loads(line)
            if r.get('phi') and r.get('level') in ('high', 'medium'):
                k = gmail_thread_key(r['thrid'])
                if k is not None:
                    phi_hold.add(k)
    if drops_path and os.path.exists(drops_path):
        for line in open(drops_path, encoding='utf-8'):
            k = gmail_thread_key(json.loads(line)['thrid'])
            if k is not None:
                drops.add(k)
    # durable per-thread drop markers (distillers write _sources/gmail-drops.d/<thrid>.json).
    # Filename stem IS the thrid, so drops survive interruption like done-notes do (no race).
    drops.update(drop_markers(wave_dir))
    return drops, phi_hold


# ---------------------------------------------------------------- un-drop (re-open) machinery

DROPS_DIRNAME = 'gmail-drops.d'
REOPENED_DIRNAME = 'reopened'          # archive subdir; NOT *.json, so listdir/glob scans skip it
THREADS_DIRNAME = 'gmail-threads'
# `--- MSG 3 | 2026-09-05T22:06:34Z | From: owner@example.com | To: someone@... ---`
MSG_RE = re.compile(r'^--- MSG \d+ \| ([^|]*) \| From: (.*?) \| To: ', re.M)
ADDR_RE = re.compile(r'<([^>]+)>')


def drop_markers(wave_dir):
    """{canonical gmail_thread_key -> marker path} for every durable drop marker under
    `<wave_dir>/gmail-drops.d/*.json`. SINGLE OWNER of that listdir — `exclusion_keys` derives its
    drop key SET from this, the un-drop pass needs the PATH, so neither scans the dir itself.
    The `reopened/` archive subdir is not `*.json` and is therefore skipped (a re-opened thread must
    NOT read as dropped again). On the (impossible-by-construction) hex+decimal double marker for one
    thread, the lexicographically-first stem wins — deterministic via sorted()."""
    out = {}
    ddir = os.path.join(wave_dir, DROPS_DIRNAME)
    if not os.path.isdir(ddir):
        return out
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith('.json'):
            continue
        k = gmail_thread_key(fn[:-5])
        if k is not None:
            out.setdefault(k, os.path.join(ddir, fn))
    return out


def _as_utc(dt):
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt.astimezone(
        datetime.timezone.utc)


def parse_ts(raw):
    """Aware-UTC datetime from a rendered-thread / marker timestamp, or None if unparseable.
    Accepts ISO-8601 with a trailing Z (the connector renderer's form), with an offset, or naive
    (assumed UTC). A None here is CONSERVATIVE: an unreadable date can never trigger an un-drop."""
    if not raw:
        return None
    t = raw.strip()
    if t.endswith('Z'):
        t = t[:-1] + '+00:00'
    try:
        return _as_utc(datetime.datetime.fromisoformat(t))
    except ValueError:
        return None


def dropped_at(marker_path):
    """(aware-UTC drop time, source) for a marker: its own `dropped_at` field when present (written
    by the distiller), else the marker file's mtime. `_sources/` is gitignored,
    so markers are never re-materialized by a checkout and their mtime IS when the distiller wrote
    them — a sound fallback for markers written before the field existed."""
    try:
        rec = json.load(open(marker_path, encoding='utf-8'))
    except (OSError, ValueError):
        rec = {}
    ts = parse_ts(rec.get('dropped_at')) if isinstance(rec, dict) else None
    if ts is not None:
        return ts, 'marker'
    # tz-AWARE from the epoch: datetime.fromtimestamp(ts) alone yields NAIVE LOCAL time, and
    # stamping that as UTC would shift the drop time by the local offset (4 h in EDT) — earlier
    # than reality, i.e. biased toward FALSE un-drops. Pass the tz to fromtimestamp instead.
    return datetime.datetime.fromtimestamp(os.path.getmtime(marker_path),
                                           datetime.timezone.utc), 'mtime'


def thread_messages(wave_dir, thrid):
    """[(aware-UTC datetime|None, sender-address-lowercased)] for the rendered thread
    `<wave_dir>/gmail-threads/<thrid>.txt`, oldest->newest. [] when the render is absent.
    Scans line-by-line: a rendered thread carries full message bodies (6 kB each) and only the
    `--- MSG n | ... ---` header lines matter here."""
    fp = os.path.join(wave_dir, THREADS_DIRNAME, f'{thrid}.txt')
    msgs = []
    try:
        with open(fp, encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line.startswith('--- MSG '):
                    continue
                m = MSG_RE.match(line)
                if m:
                    msgs.append((parse_ts(m.group(1)), sender_addr(m.group(2))))
    except OSError:
        return []
    return msgs


def sender_addr(raw):
    """Bare lowercased address from a rendered `From:` value — `a@b` or `Name <a@b>`."""
    s = (raw or '').strip()
    m = ADDR_RE.search(s)
    return (m.group(1) if m else s).strip().lower()


def classify_dropped(wave_dir, thrid, marker_path):
    """Verdict for ONE dropped thread that reappeared in the wave. Returns
    (verdict, drop_time, drop_src, owner_ts, owner_from, n_post) where verdict is:
      'reopen'  — grew after the drop AND >=1 post-drop message is from the owner  -> becomes pending
      'grown'   — grew after the drop, but no post-drop message is from the owner  -> stays dropped
      'stale'   — no message after the drop                                   -> stays dropped
      'norender'— no rendered thread, so growth is unknowable                 -> stays dropped"""
    drop_time, drop_src = dropped_at(marker_path)
    msgs = thread_messages(wave_dir, thrid)
    if not msgs:
        return 'norender', drop_time, drop_src, None, None, 0
    post = [(ts, frm) for ts, frm in msgs if ts is not None and ts > drop_time]
    if not post:
        return 'stale', drop_time, drop_src, None, None, 0
    mine = [(ts, frm) for ts, frm in post if frm in SELF_ADDRS]
    if not mine:
        return 'grown', drop_time, drop_src, None, None, len(post)
    return 'reopen', drop_time, drop_src, mine[-1][0], mine[-1][1], len(post)


def archive_marker(marker_path, reason, extra):
    """MOVE a drop marker into `gmail-drops.d/reopened/<stem>.<YYYYMMDDTHHMMSS>.json`, appending
    `reopened_at` + `reopened_reason` (+ the evidence in `extra`) to the original record. NEVER a
    delete: the original `reason` (why it was dropped) is preserved verbatim, so the archive is a
    full audit trail of drop -> re-open. Writes the archive FIRST and unlinks after, so a crash
    leaves the thread dropped (conservative) rather than losing the marker. Returns the new path."""
    ddir = os.path.dirname(marker_path)
    rdir = os.path.join(ddir, REOPENED_DIRNAME)
    os.makedirs(rdir, exist_ok=True)
    try:
        rec = json.load(open(marker_path, encoding='utf-8'))
        if not isinstance(rec, dict):
            rec = {'marker': rec}
    except (OSError, ValueError):
        rec = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    rec.update(extra)
    rec['reopened_at'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    rec['reopened_reason'] = reason
    stem = os.path.basename(marker_path)[:-5]
    base = f"{stem}.{now.strftime('%Y%m%dT%H%M%S')}"
    dest, n = os.path.join(rdir, base + '.json'), 1
    while os.path.exists(dest):          # same-second re-open of the same stem
        dest = os.path.join(rdir, f'{base}.{n}.json')
        n += 1
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    os.remove(marker_path)
    return dest


def undrop_pass(wave_dir, records, drops):
    """Re-open every dropped wave thread the owner replied to after the drop. MUTATES `drops` (removes
    the re-opened keys) and MOVES their markers. Returns (reopened, grown, unreadable) report rows."""
    markers = drop_markers(wave_dir)
    reopened, grown, unreadable = [], [], []
    for r in records:
        t = gmail_thread_key(r.get('thrid'))
        if t is None or t not in drops or t not in markers:
            continue
        verdict, dt, src, ats, afrom, npost = classify_dropped(wave_dir, r['thrid'], markers[t])
        row = {'thrid': r['thrid'], 'subject': r.get('subject') or '', 'drop_time': dt,
               'drop_src': src, 'owner_ts': ats, 'owner_from': afrom, 'n_post': npost}
        if verdict == 'reopen':
            row['archived'] = archive_marker(
                markers[t],
                f"thread grew after the drop and the owner ({afrom}) replied "
                f"{ats.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                {'dropped_at': dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'dropped_at_source': src,
                 'owner_reply_at': ats.strftime('%Y-%m-%dT%H:%M:%SZ'), 'owner_reply_from': afrom,
                 'reopened_by': 'scripts/gmail_worklist.py'})
            drops.discard(t)
            reopened.append(row)
        elif verdict == 'grown':
            grown.append(row)
        elif verdict == 'norender':
            unreadable.append(row)
    return reopened, grown, unreadable


def main():
    in_path, out_path = sys.argv[1], sys.argv[2]
    phi_path = (sys.argv[sys.argv.index('--phi-report') + 1]
                if '--phi-report' in sys.argv else None)
    drops_path = (sys.argv[sys.argv.index('--drops') + 1]
                  if '--drops' in sys.argv else None)
    root = (os.path.abspath(sys.argv[sys.argv.index('--root') + 1])
            if '--root' in sys.argv else ROOT)
    wave_dir = os.path.dirname(os.path.abspath(in_path))
    drops, phi_hold = exclusion_keys(wave_dir, drops_path, phi_path)

    records = [json.loads(ln) for ln in open(in_path, encoding='utf-8') if ln.strip()]
    # RE-OPEN before the exclusion loop: undrop_pass removes the re-opened keys from `drops`, so
    # those threads fall through as ordinary pending NEW notes below.
    reopened, grown, unreadable = undrop_pass(wave_dir, records, drops)
    for row in reopened:
        print(f"UN-DROPPED {row['thrid']} ({row['subject']}) — the owner replied "
              f"{row['owner_ts'].strftime('%Y-%m-%d')} after drop "
              f"{row['drop_time'].strftime('%Y-%m-%d')}"
              f"{' (drop time from marker mtime)' if row['drop_src'] == 'mtime' else ''}")
        print(f"    marker archived -> {os.path.relpath(row['archived'], wave_dir)}")
    for row in grown:
        print(f"grown-but-still-dropped {row['thrid']} ({row['subject']}) — {row['n_post']} new "
              f"msg since drop {row['drop_time'].strftime('%Y-%m-%d')}, none from the owner")
    for row in unreadable:
        print(f"dropped-no-render {row['thrid']} ({row['subject']}) — no "
              f"{THREADS_DIRNAME}/{row['thrid']}.txt; growth unknowable, left dropped")

    done = done_thrids(root)
    total = held = already = dropped = written = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        for r in records:
            total += 1
            t = gmail_thread_key(r['thrid'])   # canonical INT (base-agnostic); None if unparseable
            if t is not None and t in done:
                already += 1
                continue
            if t is not None and t in drops:
                dropped += 1
                continue
            if t is not None and t in phi_hold:
                held += 1
                continue
            out.write(json.dumps(r, ensure_ascii=False) + '\n')
            written += 1
    print(f"in={total:,}  already-done={already:,}  prev-dropped={dropped:,}  "
          f"un-dropped={len(reopened):,}  grown-but-still-dropped={len(grown):,}  "
          f"phi-held={held:,}  -> pending(new)={written:,}")
    print(f"pending list -> {out_path}")
    # The NEW-note wave still EXCLUDES already-done threads (no behavior change). But in an
    # INCREMENTAL wave, search_threads only returns a thread with post-watermark activity, so an
    # already-distilled thread reappearing here = a GROWN thread (new messages since last distill).
    # gmail_worklist does NOT own the grown set — it just flags it; gmail_update_worklist is the
    # single source of truth and routes those to the redistill-in-place (thread-update) flow.
    if already:
        print(f"note: {already:,} thread(s) are already-done; in an incremental wave those reappear "
              f"only with post-watermark activity = GROWN threads. Run "
              f"`python3 scripts/gmail_update_worklist.py` to redistill them IN PLACE (grown set is "
              f"owned there, not here).")


if __name__ == '__main__':
    main()
