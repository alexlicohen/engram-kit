"""Single owner for the action-item extraction grammar: action-line parsing (both owner-last and
owner-first grammars), owner classification against the people.md self row, deterministic
due-date resolution (anchored to a note's `date:`, NEVER wall clock), task-id minting, text
normalization, and the digests/open-loops.md bullet grammar.

Any task-bridge you build on top of engram (a reminders/issue-tracker push, a reconcile pass)
imports these — do not fork a copy, or the two grammars drift and the same bullet mints two ids.

    from _lib.actions import (parse_action_line, resolve_due, normalize_text, task_id,
                               self_aliases, owner_class, parse_openloop_line,
                               is_resolved_marker)
"""
import datetime
import difflib
import hashlib
import re

# ---------------------------------------------------------------------------
# Due-paren extraction
# ---------------------------------------------------------------------------

_DUE_PAREN_RE = re.compile(r'\((?:due|by)\s+([^)]*)\)', re.I)
_BARE_DATE_PAREN_RE = re.compile(r'\((\d{4}-\d{2}-\d{2})\)')

_BULLET_RE = re.compile(r'^[-*]\s*(?:\[[ xX]\]\s*)?')

_MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

_WEEKDAYS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

_RESOLVED_RE = re.compile(r'resolved|\(done\)|status:\s*done|—\s*done', re.I)


def is_resolved_marker(text):
    """True if `text` (a raw line, or a status field) carries a resolution marker — the
    engram-tasks skip rule (case-insensitive 'resolved', '(done)', 'status: done', '— done')."""
    return bool(_RESOLVED_RE.search(text or ""))


def _extract_due(text):
    """Return (due_raw or None, text-with-the-due-paren-removed). Prefers the last
    `(due …)`/`(by …)` paren; falls back to a last bare `(YYYY-MM-DD)` paren. A quoted spoken
    phrase — (due "next Wednesday") — is returned without its quotes."""
    matches = list(_DUE_PAREN_RE.finditer(text))
    if matches:
        m = matches[-1]
        return _unquote(m.group(1)), text[:m.start()] + text[m.end():]
    matches = list(_BARE_DATE_PAREN_RE.finditer(text))
    if matches:
        m = matches[-1]
        return m.group(1).strip(), text[:m.start()] + text[m.end():]
    return None, text


def _name_shaped(seg):
    """Heuristic: is `seg` shaped like a person's name (owner segment), not a task sentence?
    <=5 words, <=40 chars, starts uppercase, no sentence-final punctuation."""
    seg = seg.strip()
    if not seg or len(seg) > 40:
        return False
    words = seg.split()
    if not words or len(words) > 5:
        return False
    if not seg[0].isupper():
        return False
    if seg[-1] in '.!?':
        return False
    return True


def _strip_to_remainder(line):
    """Bullet-strip a raw line, pull out its `(due …)` paren, collapse whitespace and drop a
    trailing period. Returns `(remainder, due_raw)` — `remainder` is None when nothing meaningful
    survives. Shared by parse_action_line and resolved_action_text so both apply IDENTICAL
    stripping (do not fork)."""
    raw = line.rstrip("\n")
    text = _BULLET_RE.sub('', raw.strip()).strip()
    if not text:
        return None, None
    due_raw, remainder = _extract_due(text)
    remainder = re.sub(r'\s{2,}', ' ', remainder).strip()
    remainder = remainder.rstrip('.').rstrip()
    if not remainder:
        return None, due_raw
    return remainder, due_raw


def _split_owner(remainder):
    """Split a due-stripped remainder into `(task_text, owner, unowned)` via the name-shaped-segment
    heuristic (owner may be the last OR the first ` — `-delimited segment)."""
    segments = re.split(r'\s+—\s+', remainder)
    owner = ""
    unowned = True
    task_text = remainder
    if len(segments) >= 2:
        if _name_shaped(segments[-1]):
            owner = segments[-1].strip()
            task_text = ' — '.join(segments[:-1]).strip()
            unowned = False
        elif _name_shaped(segments[0]):
            owner = segments[0].strip()
            task_text = ' — '.join(segments[1:]).strip()
            unowned = False
    return task_text, owner, unowned


def parse_action_line(line, note_date):
    """Parse one `## Action items` bullet (either grammar) into a dict, or None if the line
    should be skipped (resolution marker / too short / empty after stripping). `note_date` is a
    `datetime.date` (or None) — the anchor for due resolution, NEVER wall clock.

    Returns: {"line": <original, rstripped>, "text": <task text, due-paren removed>,
              "owner": <owner string, "" if none>, "unowned": bool,
              "due_raw": <raw due text or None — the spoken phrase, kept for audit>,
              "due": <resolved ISO date or None>,
              "due_approx": <True when `due` is an approximation, e.g. "next week" → Monday>}
    """
    raw = line.rstrip("\n")
    if is_resolved_marker(raw):
        return None
    remainder, due_raw = _strip_to_remainder(line)
    if remainder is None:
        return None

    task_text, owner, unowned = _split_owner(remainder)

    if len(normalize_text(task_text)) < 8:
        return None

    due, due_approx = resolve_due_detail(due_raw, note_date) if due_raw else (None, False)
    return {
        "line": raw,
        "text": task_text,
        "owner": owner,
        "unowned": unowned,
        "due_raw": due_raw,
        "due": due,
        "due_approx": due_approx,
    }


def resolved_action_text(line):
    """Task TEXT of a *resolution-marked* action bullet, with the resolution marker AND the
    owner/due scaffolding stripped — used by engram-tasks `suggest-complete` to match a
    resolution-marked source line back to the still-open task/reminder it resolves. Returns ''
    when nothing meaningful survives (mirrors parse_action_line's stripping but does NOT skip
    on the resolution marker, since that is exactly the signal we key off)."""
    remainder, _ = _strip_to_remainder(_RESOLVED_RE.sub('', line))
    if remainder is None:
        return ""
    task_text, _, _ = _split_owner(remainder)
    return task_text


# ---------------------------------------------------------------------------
# Due resolution — deterministic, anchored to note_date, bounded set. Never guesses: any form
# outside the bounded set (TBD, ASAP, "next meeting 7/7", "~2026-04-01", "October 2026", …)
# returns None while due_raw is preserved by the caller.
# ---------------------------------------------------------------------------

def _next_on_or_after(month, day, note_date):
    try:
        candidate = datetime.date(note_date.year, month, day)
    except ValueError:
        return None
    if candidate < note_date:
        try:
            candidate = datetime.date(note_date.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _last_day_of_month(year, month):
    if month == 12:
        return datetime.date(year, 12, 31)
    return datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)


def _week_monday(d):
    """Monday of `d`'s ISO (Mon–Sun) calendar week — the week unit every weekday rule uses."""
    return d - datetime.timedelta(days=d.weekday())


def _resolve_absolute(s, note_date):
    """The absolute-date forms (ISO / M/D[/YY[YY]] / Month D[, YYYY]). `s` is stripped, original
    case. Returns a `datetime.date` or None."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$', s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12):
            return None
        yr = m.group(3)
        try:
            if yr is None:
                return _next_on_or_after(month, day, note_date)
            y = int(yr)
            if y < 100:
                y += 2000
            return datetime.date(y, month, day)
        except ValueError:
            return None

    # "Month D" (year = next occurrence on/after note_date) or "Month D, YYYY" (explicit year)
    m = re.match(r'^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$', s)
    if m and m.group(1).lower() in _MONTHS:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        if m.group(3) is None:
            return _next_on_or_after(month, day, note_date)
        try:
            return datetime.date(int(m.group(3)), month, day)
        except ValueError:
            return None
    return None


_WEEKDAY_ABBR = {
    'mon': 0, 'tue': 1, 'tues': 1, 'wed': 2, 'weds': 2, 'thu': 3, 'thur': 3, 'thurs': 3,
    'fri': 4, 'sat': 5, 'sun': 6,
}
_WD_ALL = dict(_WEEKDAYS, **_WEEKDAY_ABBR)
_WD_ALT = '|'.join(sorted(_WD_ALL, key=len, reverse=True))

_NUM_WORDS = {
    'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
    'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
}

_QUOTES = '"\'\u201c\u201d\u2018\u2019'
_LEAD_RE = re.compile(r'^(?:by|on|due|until|till|til|no later than)\s+', re.I)


def _unquote(s):
    """Strip whitespace and surrounding straight/curly quotes from a due phrase — the distiller
    writes a spoken relative phrase as (due "next Wednesday"). Returns the whitespace-stripped
    original when stripping would leave nothing."""
    s0 = (s or "").strip()
    t = s0.strip(_QUOTES).strip()
    return t if t else s0


def _parse_weekday_phrase(low):
    """`(qualifier, weekday_int, week)` for "[this|coming|this coming|next] X [this|next week]" or
    "this|next week X" (lowercased, whitespace-collapsed); None if `low` is not weekday-shaped."""
    m = re.match(r'^(this coming|coming|this|next)?\s*(' + _WD_ALT + r')\.?'
                 r'(?:,?\s+(this|next) week)?$', low)
    if m:
        return m.group(1), _WD_ALL[m.group(2)], m.group(3)
    m = re.match(r'^(this|next) week,?\s+(' + _WD_ALT + r')\.?$', low)
    if m:
        return None, _WD_ALL[m.group(2)], m.group(1)
    return None


def resolve_due_detail(due_raw, note_date):
    """Resolve a due phrase to `(iso_date_or_None, approximate: bool)`, anchored to `note_date`
    (a `datetime.date` — the note's `date:`, i.e. the MEETING date; NEVER wall clock). The single
    owner of due-phrase resolution for engram-tasks. Bounded grammar — anything outside it returns
    (None, False) and the caller keeps the phrase in `due_raw`. NEVER guesses.

    Weeks are ISO calendar weeks (Monday–Sunday). "this week" = the anchor's week, "next week" =
    the week after it. Weekday conventions (X = the named weekday, A = the anchor):

      phrase                       resolves to                                        e.g. A = Thu 2026-09-24
      ---------------------------  -------------------------------------------------  -----------------------
      X / by X / on X              first X STRICTLY after A (1–7 days; same day→+7)   Fri→09-25, Wed→09-30,
                                                                                      Thu→10-01
      this X                       first X ON or after A (same day = A)               this Fri→09-25,
                                                                                      this Thu→09-24
      coming X / this coming X     first X strictly after A                           coming Fri→09-25
      next X                       X of NEXT calendar week, but ONLY when that equals next Wed→09-30,
                                   the first X strictly after A (i.e. X <= A's        next Thu→10-01,
                                   weekday); otherwise AMBIGUOUS ("next Friday" said  next Fri→None
                                   on a Thursday = tomorrow or +8?) → None
      X next week / next week X    X of next calendar week (phrase disambiguates)     Fri next week→10-02
      X this week                  X of A's week if X >= A's weekday, else None       Fri this week→09-25
      Weekday + date ("Wed Oct 13", "Friday, 10/2"): the date, only if its weekday matches; else None.

    Other forms: today / tonight / EOD / end of day / COB → A; tomorrow → A+1;
    EOW / end of (the|this) week → Friday of A's week (next Friday if A is Sat/Sun);
    end of next week → Friday of next week; next week → Monday of next week, APPROXIMATE;
    EOM / end of (the|this) month / month end → last day of A's month; end of next month → last
    day of the following month; in|within N days|weeks (N digits or a..twelve) → A+N / A+7N;
    ISO; M/D[/YY[YY]]; Month D[, YYYY] (no year → next occurrence on/after A).
    Leading by/on/due/until and surrounding quotes are ignored ((due "by Friday") == Friday).
    """
    if not due_raw or note_date is None:
        return None, False
    s = _unquote(due_raw).rstrip('.,;').strip()
    if not s:
        return None, False
    prev = None
    while prev != s:
        prev = s
        s = _LEAD_RE.sub('', s).strip()
        s = re.sub(r'^the\s+', '', s, flags=re.I).strip()

    d = _resolve_absolute(s, note_date)
    if d:
        return d.isoformat(), False

    low = re.sub(r'\s+', ' ', s.lower().rstrip('.')).strip()
    wd = note_date.weekday()

    # weekday + absolute date: "wed oct 13" / "friday, 10/2" — the date wins only if consistent
    m = re.match(r'^(' + _WD_ALT + r')\.?,?\s+(.+)$', low)
    if m:
        d = _resolve_absolute(m.group(2).strip(), note_date)
        if d:
            return (d.isoformat(), False) if d.weekday() == _WD_ALL[m.group(1)] else (None, False)

    if low in ('today', 'tonight', 'eod', 'end of day', 'end of today', 'cob',
               'close of business'):
        return note_date.isoformat(), False
    if low == 'tomorrow':
        return (note_date + datetime.timedelta(days=1)).isoformat(), False

    next_monday = _week_monday(note_date) + datetime.timedelta(days=7)

    wk = _parse_weekday_phrase(low)
    if wk is not None:
        qual, target, week = wk
        if week == 'next':
            if qual:
                return None, False
            return (next_monday + datetime.timedelta(days=target)).isoformat(), False
        if week == 'this':
            if qual or target < wd:
                return None, False
            return (note_date + datetime.timedelta(days=target - wd)).isoformat(), False
        strictly_after = note_date + datetime.timedelta(days=(target - wd - 1) % 7 + 1)
        if qual == 'this':
            return (note_date + datetime.timedelta(days=(target - wd) % 7)).isoformat(), False
        if qual == 'next' and target > wd:
            return None, False  # ambiguous: this week's X or next week's X
        return strictly_after.isoformat(), False  # bare / coming / this coming / unambiguous next

    if low in ('eow', 'end of week', 'end of this week', 'end of the week'):
        this_friday = note_date + datetime.timedelta(days=(4 - wd))
        if wd > 4:
            this_friday += datetime.timedelta(days=7)
        return this_friday.isoformat(), False
    if low == 'end of next week':
        return (next_monday + datetime.timedelta(days=4)).isoformat(), False
    if low == 'next week':
        return next_monday.isoformat(), True

    if low in ('eom', 'end of month', 'end of this month', 'end of the month', 'month end',
               'month-end'):
        return _last_day_of_month(note_date.year, note_date.month).isoformat(), False
    if low == 'end of next month':
        y, mo = (note_date.year + 1, 1) if note_date.month == 12 else (note_date.year,
                                                                     note_date.month + 1)
        return _last_day_of_month(y, mo).isoformat(), False

    m = re.match(r'^(?:in|within) (\d{1,3}|' + '|'.join(_NUM_WORDS) + r') (day|week)s?$', low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
        days = n * (7 if m.group(2) == 'week' else 1)
        return (note_date + datetime.timedelta(days=days)).isoformat(), False

    return None, False


def resolve_due(due_raw, note_date):
    """Resolve `due_raw` (the text captured inside a `(due …)`/`(by …)` paren, or a bare
    `YYYY-MM-DD`) to an ISO date string, anchored to `note_date` (a `datetime.date`). Returns
    None for anything outside the bounded set — NEVER guesses. Grammar + conventions:
    `resolve_due_detail`."""
    return resolve_due_detail(due_raw, note_date)[0]


# ---------------------------------------------------------------------------
# Text normalization + task id
# ---------------------------------------------------------------------------

def normalize_text(text):
    """lowercase, whitespace-collapsed, trailing '.;:' stripped, any due-paren removed —
    the canonical form task_id() hashes over (so whitespace/case variants of the same task
    collapse to the same id)."""
    if not text:
        return ""
    t = _DUE_PAREN_RE.sub('', text)
    t = _BARE_DATE_PAREN_RE.sub('', t)
    t = t.lower()
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.rstrip('.;: ').strip()
    return t


def task_id(ref, text):
    """id = 'task:' + sha256(ref + '\\n' + normalize_text(text))[:16] — the single owner of
    dedup identity (an id already in the ledger is never re-extracted)."""
    h = hashlib.sha256((f"{ref}\n{normalize_text(text)}").encode('utf-8')).hexdigest()
    return "task:" + h[:16]


# The single owner of engram-tasks fuzzy-dedup similarity. Push holds a near-dup (no create) for
# LLM adjudication when a candidate title scores >= NEAR_DUP_RATIO against a live reminder;
# suggest-complete uses the same threshold to match a resolution-marked line to an open task.
NEAR_DUP_RATIO = 0.8


def title_ratio(a, b):
    """difflib SequenceMatcher ratio over the two texts' normalized forms (0.0–1.0). Compared
    against NEAR_DUP_RATIO for the near-dup / resolution-match decisions."""
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


# ---------------------------------------------------------------------------
# Owner classification
# ---------------------------------------------------------------------------

def _self_row(root):
    """(canonical name, alias cell) of the `self` row of `_manifest/people.md`, or (None, "")."""
    import os
    path = os.path.join(root, "_manifest", "people.md")
    canonical = None
    alias_field = ""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 3:
            continue
        if cells[2].strip().lower() == 'self':
            canonical = cells[0]
            alias_field = cells[1]
            break
    return canonical, alias_field


def self_aliases(root):
    """Parse the self row of `_manifest/people.md` (relationship column == 'self') into a
    lower-cased alias set: canonical name, quoted aliases, the first token of the canonical name
    (the given name), and the literal "me". A store with no `self` row yields just {"me"}."""
    canonical, alias_field = _self_row(root)
    aliases = set()
    if canonical:
        aliases.add(canonical.lower())
        for tok in re.findall(r'"([^"]+)"', alias_field):
            aliases.add(tok.lower())
        first = canonical.split()[0] if canonical.split() else None
        if first:
            aliases.add(first.lower())
    aliases.add("me")   # always present, so the set is never empty
    return aliases


_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')


def self_emails(root):
    """The owner's own e-mail addresses (lower-cased) from the self row's alias cell. SINGLE OWNER
    of "is this address the owner" for every mail source (Gmail, a secondary mailbox, Outlook): put
    all your addresses on that row and nothing else hard-codes them. Empty set if there are none."""
    _canonical, alias_field = _self_row(root)
    return {m.lower() for m in _EMAIL_RE.findall(alias_field or "")}


def owner_class(owner, aliases):
    """'mine' if any self-alias appears word-bounded in `owner` (so a joint owner — "<self> & a
    collaborator" — still counts); unowned (falsy owner) -> 'mine'; else 'waiting_on'."""
    if not owner:
        return "mine"
    low = owner.lower()
    for alias in aliases:
        if not alias:
            continue
        if re.search(r'\b' + re.escape(alias) + r'\b', low):
            return "mine"
    return "waiting_on"


# ---------------------------------------------------------------------------
# digests/open-loops.md bullet grammar (mirrors engram_brief.open_loops(), lines ~33-61 —
# do NOT edit engram_brief.py from here; this is the shared parser it should eventually import).
# ---------------------------------------------------------------------------

def parse_openloop_line(line):
    """Parse one `- <text> — owner: <o> · due: <date|—> · status: <s> · [[slug]]` bullet from
    digests/open-loops.md. Returns None if the line doesn't match the grammar (needs a leading
    '- ', a 'due:' field, and a '[[...]]' link). `due` is the resolved ISO date if `due:` is a
    literal ISO date, else None (raw form kept in `due_raw`)."""
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped.startswith("- ") or "due:" not in stripped or "[[" not in stripped:
        return None
    mdue = re.search(r"due:\s*(\S.*?)(?:\s+·|\s*$)", stripped)
    due_field = mdue.group(1).strip() if mdue else None
    if due_field in (None, "—", "-"):
        due_field = None
    due = None
    if due_field:
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', due_field)
        if m:
            try:
                due = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                due = None
    mst = re.search(r"status:\s*([^·\[]+)", stripped)
    status = mst.group(1).strip() if mst else ""
    text = re.split(r"\s+—\s+owner:|\s+·\s+", stripped[2:])[0].strip()
    mowner = re.search(r"owner:\s*([^·]+)", stripped)
    owner = mowner.group(1).strip() if mowner else ""
    if owner == "—":
        owner = ""
    note = re.search(r"\[\[([^\]]+)\]\]", stripped)
    return {
        "line": raw,
        "text": text,
        "owner": owner,
        "unowned": not owner,
        "due_raw": due_field,
        "due": due,
        "status": status,
        "slug": note.group(1) if note else "",
    }


# ---------------------------------------------------------------------------
# Open-loops section headings <-> domains. SINGLE OWNER of this mapping:
# engram_capture.py files commitments under these `##` headings, and any task
# bridge routes rows back through the inverse. The values MUST match the `##`
# headings in digests/open-loops.md. Import from here — never re-paste.
OPENLOOPS_SECTIONS = {
    "research": "Research",
    "work": "Work",
    "ventures": "Ventures",
    "finance": "Finance",
    "health": "Health",
    "personal": "Personal",
    "infra": "Infrastructure",
}
DOMAIN_BY_SECTION = {v: k for k, v in OPENLOOPS_SECTIONS.items()}
