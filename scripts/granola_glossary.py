#!/usr/bin/env python3
"""Generate the "# KNOWN NAMES & TERMS" block of the owner's Granola summarizer template from the
people registry, so `_manifest/people.md` is the single owner of the PEOPLE glossary and the owner only
pastes the output into Granola when it changes.

    python3 scripts/granola_glossary.py            # write _granola-pending/granola-glossary.generated.txt
    python3 scripts/granola_glossary.py --check    # exit 1 if that file is stale (advisory; NOT in check.sh)
    python3 scripts/granola_glossary.py --why      # also print why each person was kept
    python3 scripts/granola_glossary.py --min-meetings 2 --days 365 --stdout

OWNERSHIP
  * PEOPLE blocks — owned HERE, derived from people.md (parsed by build_clarify_queue.parse_people,
    the one people.md parser; never a second one), `_manifest/index.jsonl` participants and
    `_manifest/corrections/granola.jsonl`.
  * The section's header rule text, and the ORGS / ENTITIES, CLINICAL / TECHNICAL and TOOLS blocks —
    copied VERBATIM from the current template (`--template`). Edit those in the template; this
    script never invents or reorders them. The template's PEOPLE names are NOT a keep-list any more;
    they are only diffed, so every one that falls out is printed as `DROPPED from current template`.

SELECTION — the glossary serves speech-to-text, so it is deliberately short
  POOL: registry people in the `participants:` of >= --min-meetings (3) GRANOLA notes dated within
  [anchor - --days (180), anchor]. Email-only contacts are never spoken, so gmail/outlook don't
  count. The anchor is the newest granola `ingested` date (overridable with --as-of) — NOT today —
  so the output, and --check, only change when the store or registry changes.
  Skipped: the owner himself (role `self`; the template handles him), non-person-shaped names, and
  tombstone rows ("DUPLICATE — see X", counted for the live row that lists X as an alias).
  KEEP a pool member only if at least one holds (--why prints which):
    (r) regular — >= --regular-min (10) granola meetings in the window, whatever the name.
    (a) hard-name — `is_hard_name`: hyphen, accent, internal capital (McSample), 3+ tokens
        (multi-word surname / middle name), a 1-2 letter surname, or a non-English spelling pattern
        in the first name or surname (`_NON_ENGLISH_*`: e.g. aa/kh/dh/sch/consonant+j anywhere; a
        surname with z or eu/oi/au/ua, or ending a/o/i/is/ez/sky/cky/ov/ich/bach/ik; a first name
        ending -i). Tokens in the short COMMON list never count as non-English.
    (b) spoken-alias — the registry row records a nickname / ASR variant that survives the aka rule.
    (c) collision — a registry alias claimed by >1 person (emitted when a claimant is in the pool
        OR a granola correction quotes the alias, e.g. 'Sam Example'; every claimant is kept, even
        below the meeting threshold), or two pool members whose first names sound alike
        (`first_name_key`: Katrin/Catrin, Sofia/Sophia, Mathilde/Matilda, Brian/Bryan, Tim/Timothy).
        Every party is kept and gets a ⚠ line under AMBIGUOUS SPOKEN FORMS.
    (d) corrected — a granola correction finding names them (full canonical name anywhere, or first
        name / surname / alias when they are a participant of that finding's own note).
  An easy name with none of these is dropped (the summarizer spells it right unaided).

AMBIGUOUS SPOKEN FORMS
  * one line per registry alias claimed by >1 person (rule (c) above);
  * one line per first-name key held by a KEPT person, naming the kept parties with that key plus
    registry people OUTSIDE the glossary with it — only two-token-or-longer names that are a
    participant in >= 1 in-window GRANOLA note (other sources don't count) — most-seen first,
    capped at MAX_PARTIES (3) names. Outside parties are named with their group only and are NOT
    added as PEOPLE entries: they exist so "Katrin" alone is never snapped to Catrin Sample.

GROUPING (the template snaps a spelling only when the meeting's context matches the group)
  domain -> group (DOMAIN_GROUP below — edit it with your domain list): work -> Work;
  research, literature -> Research; finance -> Finance; ventures -> Ventures/advisory;
  personal, health, infra, unsorted -> Personal/other.
  OVERRIDE: a person the registry marks family/personal (registry domain `personal`, or a family
  relationship — wife/mother/brother/in-law/… — in the role) is always Personal/other.
  Otherwise the MAJORITY group over the domains of their in-window granola notes; with none, the
  registry row's domain (section heading "(domain `x`)", else the last domain word in the row).
  Ties go to the registry-domain group if it is among the tied, else to the earlier group above.
  (The earlier "(also)" secondary listing was removed 2026-09-27: at this size it was noise.)

PER PERSON  `Canonical (aka X, Y)` — no role by default. The template lets glossary roles flow
  into ATTENDEES and registry roles go stale ("Director candidate", "New faculty"), so the groups
  and ⚠ lines carry the context. `--roles` restores ` — <role>` using the role rule below.
  * aka: the row's aliases that are short spoken variants (<= 3 name-shaped tokens, no digits/@,
    no "Dr." form, not a mere subset/superset of the canonical tokens). A same-surname nickname is
    shortened to the first name ("Liz Example" -> "Liz") — unless that first name is merely the
    formal form of the canonical one ("Samuel Example" on Sam Example is dropped). An alias claimed by MORE THAN ONE
    registry person (or equal to another person's canonical name) is never emitted as an aka; it
    becomes a one-line ⚠ note under AMBIGUOUS SPOKEN FORMS instead.
  * role (only with --roles): only from rows whose Verify is NOT true (parse_people's merged verify = all rows
    unconfirmed), and not from rows flagged tentative/TBD; first clause, markup/quotes/emails/phones
    stripped, junk pieces ("role TBD", "unconfirmed", "owner-confirmed …", "DUPLICATE …") dropped,
    meeting/event pieces ("… meeting", "… call", "… attendee") dropped, <= 8 words built from
    WHOLE ', '/' — ' pieces — never cut mid-phrase: a too-long first piece falls back to a later
    piece that fits, else no role. verify:true people get no role and a ⚠ mark.
  * never emails or phone numbers.
"""
import argparse
import collections
import contextlib
import datetime
import json
import os
import re
import sys
import unicodedata

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
ROOT = os.path.dirname(_SCRIPTS)

_CWD = os.getcwd()
import build_clarify_queue as _bcq                     # noqa: E402  (single owner: people.md parser)
os.chdir(_CWD)                                          # its import chdirs to ROOT; undo that
from _lib.constants import DOMAINS                     # noqa: E402

TEMPLATE_REL = "_granola-pending/granola-template-v2.md"
OUT_REL = "_granola-pending/granola-glossary.generated.txt"
SECTION_HEADER = "# KNOWN NAMES & TERMS"
IN_SCOPE_SOURCES = ("granola",)   # speech-to-text glossary: only spoken meetings count
DEFAULT_MIN_MEETINGS = 3
DEFAULT_REGULAR_MIN = 10          # this many granola meetings = always kept
MAX_PARTIES = 3                   # parties named on one first-name ambiguity line
DEFAULT_DAYS = 180
TARGET_CHARS = 8000               # Granola paste target

G_WORK, G_RES, G_FIN, G_VENT, G_PERS = "work", "res", "fin", "vent", "pers"
GROUP_ORDER = [G_WORK, G_RES, G_FIN, G_VENT, G_PERS]
GROUP_SHORT = {G_WORK: "work", G_RES: "research", G_FIN: "finance",
               G_VENT: "ventures", G_PERS: "personal"}
GROUP_HEADERS = {
    G_WORK: "PEOPLE — Work:",
    G_RES: "PEOPLE — Research collaborators:",
    G_FIN: "PEOPLE — Finance (never appear in work/research meetings):",
    G_VENT: "PEOPLE — Ventures / advisory:",
    G_PERS: "PEOPLE — Personal / other:",
}
DOMAIN_GROUP = {"work": G_WORK, "research": G_RES, "literature": G_RES,
                "finance": G_FIN, "ventures": G_VENT,
                "personal": G_PERS, "health": G_PERS, "infra": G_PERS, "unsorted": G_PERS}
assert set(DOMAIN_GROUP) == set(DOMAINS), "domain list changed: update DOMAIN_GROUP"

_PERSON_NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ][\w.'’\- ]*$")
_ALIAS_RE = re.compile(r"^[A-Z][\w.'’\-]*(?:\s+[A-Za-z][\w.'’\-]*){0,2}$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_ROLE_JUNK_RE = re.compile(r"\bTBD\b|tentative|unknown|unclear|confirmed|\?|^\s*role\b|promoted"
                           r"|^\s*(?:ONE|same) person\b", re.I)
# a piece describing a meeting/event, not a role ("quarterly budget review meeting")
_EVENT_RE = re.compile(r"(?<!on-)\b(?:meeting|call|conversation|discussion|webinar|attendee"
                       r"|interview)s?\b", re.I)
# a registry relationship that makes someone family: grouped Personal/other regardless of notes
_FAMILY_RE = re.compile(r"\b(?:wife|husband|spouse|mother|father|brother|sister|sibling|twin"
                        r"|in-law|cousin|aunt|uncle|niece|nephew|grand(?:mother|father|parent)"
                        r"|stepmother|stepfather)\b|^\s*(?:child|son|daughter)\s*(?:$|[—–(,;])", re.I)
# ("child" is family; "Child Life program coordinator" is not)
_SECTION_DOMAIN_RE = re.compile(r"domain\s+`([\w-]+)`")


@contextlib.contextmanager
def _cwd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


# ---- inputs --------------------------------------------------------------------------------
def load_registry(root):
    """parse_people() reads `_manifest/people.md` relative to cwd — run it inside `root`."""
    with _cwd(root):
        return _bcq.parse_people()


def load_index(root):
    out = []
    with open(os.path.join(root, "_manifest", "index.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def split_template(text):
    """-> (preamble_lines, people_lines, tail_lines) of the KNOWN NAMES section, plus the section's
    (start, end) line span in the template. preamble = header + rule text; people = every
    `PEOPLE …:` block; tail = the ORGS / CLINICAL / TOOLS blocks (kept verbatim)."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(SECTION_HEADER)), None)
    if start is None:
        raise SystemExit(f"template has no '{SECTION_HEADER}' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    sec = lines[start:end]
    while sec and not sec[-1].strip():
        sec.pop()
    first_people = next((i for i, l in enumerate(sec) if l.startswith("PEOPLE")), None)
    if first_people is None:
        return sec, [], [], (start, end)
    tail_start = next((i for i in range(first_people, len(sec))
                       if sec[i] and not sec[i][0].isspace() and not sec[i].startswith("PEOPLE")
                       and not sec[i].startswith("⚠")
                       and not sec[i].startswith("AMBIGUOUS")), len(sec))
    return sec[:first_people], sec[first_people:tail_start], sec[tail_start:], (start, end)


def template_people_names(people_lines):
    """Names in the template's PEOPLE lines: split entries on ';' / ',' and drop the
    '(aka …)'/'(org)' parentheticals, ' — role' suffix and ⚠ marks."""
    names = []
    for line in people_lines:
        if not line.startswith((" ", "\t")):
            continue
        body = re.sub(r"\([^)]*\)", "", line)
        for ent in re.split(r"[;,]", body):
            ent = ent.split(" — ")[0].replace("⚠", "").strip()
            if ent and _PERSON_NAME_RE.match(ent):
                names.append(ent)
    return names


# ---- derivations ---------------------------------------------------------------------------
def is_self(row):
    return any(r.strip().lower() == "self" for r in (row.get("role") or "").split(" | "))


def is_person_name(name):
    return bool(_PERSON_NAME_RE.match(name)) and not re.search(r"\d|@|\+|\?", name)


def registry_domain(row):
    for sec in row.get("sections", []):
        m = _SECTION_DOMAIN_RE.search(sec)
        if m and m.group(1) in DOMAIN_GROUP:
            return m.group(1)
    for rt in reversed(row.get("rowtexts", [])):
        hits = [t for t in re.findall(r"(?<![\w`-])([a-z][a-z-]+)(?![\w`-])", rt) if t in DOMAIN_GROUP]
        if hits:
            return hits[-1]
    return None


def is_personal(row):
    """Registry marks them family/personal: registry domain `personal`, or a family relationship
    in any role segment. Overrides the note majority (estate mail about a parent is still family)."""
    return registry_domain(row) == "personal" or any(
        _FAMILY_RE.search(seg) for seg in (row.get("role") or "").split(" | "))


def resolve(name, alias_to_canon):
    return alias_to_canon.get((name or "").strip().lower())


def alias_claims(canon):
    """lowercased alias/canonical string -> set of canonical names claiming it (whole registry)."""
    claims = collections.defaultdict(set)
    for name, row in canon.items():
        claims[name.lower()].add(name)
        for al in row.get("aliases", ()):
            claims[al.strip().lower()].add(name)
    return claims


def _fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _tokens(s):
    return [t for t in re.split(r"[\s.\-]+", s.strip()) if len(t) > 1]


def _token_ok(tok):
    """Name-cased token: 'Liz', 'McSample', "O'Sample", or an initial 'M.' — not 'NASA', 'garbles'."""
    return bool(re.match(r"^[A-Z](?=.*[a-z])", tok) or re.match(r"^[A-Z]\.?$", tok))


def _related(al, name):
    """An alias must share a 2-letter prefix with some canonical token (Femi~Olufemi is a
    substring, also accepted; so is a Soundex match on the first three codes, Robert~Rupert): keeps nicknames/ASR garbles, drops quoted prose like "Acme"."""
    ct = [_fold(t) for t in _tokens(name)]
    for a in (_fold(t) for t in _tokens(al) or [al]):
        for c in ct:
            if a[:2] == c[:2] or (len(a) >= 4 and a in c):
                return True
            sa, sc = _soundex(a), _soundex(c)
            if len(sa) >= 3 and sa[:3] == sc[:3]:
                return True                             # ASR sound-alike: Robert~Rupert
    return False


def _is_formal_form(alias_first, canon_first):
    """'Matthew' vs canonical 'Matt', 'Timothy' vs 'Tim': the alias first name is LONGER and
    shares the canonical first name's 3-letter stem — the formal form, not a spoken variant.
    Shorter forms (Liz/Elizabeth, Sue/Susannah, Bea/Beatrix) are nicknames and stay."""
    a, c = _fold(alias_first), _fold(canon_first)
    return len(a) > len(c) and len(c) >= 3 and a.startswith(c[:3])


def spoken_aliases(name, row, claims):
    """-> (akas, ambiguous_aliases) for one person; see module doc for the rule."""
    akas, ambiguous = [], []
    ctoks = {_fold(t) for t in _tokens(name)}
    for al in sorted(row.get("aliases", ()), key=lambda a: (len(a), a)):
        al = al.strip()
        low = al.lower()
        if not al or low == name.lower() or not _ALIAS_RE.match(al):
            continue
        if re.search(r"\d|@", al) or re.match(r"(?i)dr\.?\s", al):
            continue
        if not all(_token_ok(t) for t in al.split()) or not _related(al, name):
            continue
        if len(claims.get(low, ())) > 1:
            ambiguous.append(al)
            continue
        atoks = {_fold(t) for t in _tokens(al)}
        if atoks and (atoks <= ctoks or ctoks <= atoks):
            continue                                    # "Ann B. Example" / "Ann Example"
        at, ct = _tokens(al), _tokens(name)
        shown = at[0] if (len(at) >= 2 and ct and _fold(at[-1]) == _fold(ct[-1])
                          and _fold(at[0]) != _fold(ct[0])) else al
        if shown is not al and _is_formal_form(at[0], ct[0]):
            continue                                    # "Samuel Example" on Sam Example: noise
        if shown.lower() not in (a.lower() for a in akas):
            akas.append(shown)
    return akas[:4], ambiguous


_TOMBSTONE_RE = re.compile(r"\bDUPLICATE\b|\bmerged\s+(?:in)?to\b")


def is_tombstone(row):
    """Every row for this canonical is a 'DUPLICATE — see X' pointer left by an earlier merge."""
    texts = row.get("rowtexts") or []
    return bool(texts) and all(re.search(r"\bDUPLICATE\b", t) for t in texts)


def _nwords(s):
    return len([w for w in s.split() if re.search(r"\w", w)])


def _clip_words(role, limit=8):
    """<= limit words (dashes don't count), NEVER cut mid-phrase. Pieces are split on ', ' and
    ' — '; junk pieces (_ROLE_JUNK_RE) and meeting/event pieces (_EVENT_RE) are dropped. Whole
    pieces are kept from the first while they fit; if the first piece alone is too long, the first
    later piece that fits is used instead; if none fits, the role is dropped ('')."""
    parts = re.split(r"(,\s|\s[—–]\s)", role)
    pieces = [(parts[i - 1] if i else "", parts[i].strip()) for i in range(0, len(parts), 2)]
    pieces = [(sep, p) for sep, p in pieces
              if p and not _ROLE_JUNK_RE.search(p) and not _EVENT_RE.search(p)]
    if not pieces:
        return ""
    if _nwords(pieces[0][1]) > limit:
        return next((p for _sep, p in pieces[1:] if _nwords(p) <= limit), "")
    out = pieces[0][1]
    for sep, p in pieces[1:]:
        cand = out + sep + p
        if _nwords(cand) > limit:
            break
        out = cand
    return out


def short_role(row):
    if row.get("verify") or row.get("tentative"):
        return ""
    for seg in (row.get("role") or "").split(" | "):
        if _TOMBSTONE_RE.search(seg):
            continue
        r = re.sub(r"\*\*|`|__", "", seg)
        r = re.sub(r'"[^"]*"', "", r)
        r = _EMAIL_RE.sub("", r)
        r = _PHONE_RE.sub("", r)
        while re.search(r"\([^()]*\)", r):
            r = re.sub(r"\s*\([^()]*\)", "", r)
        r = re.split(r"\(|;|(?<=[a-z]{3})\.(?:\s|$)|\|", r)[0]
        r = re.sub(r"\s+", " ", r).strip(" ,—-–:")
        # junk / event PIECES are dropped inside _clip_words ("IBM — role unconfirmed" -> "IBM")
        r = _clip_words(r).strip(" ,—-–:")
        if r:
            return r
    return ""


def _majority(counter, fallback_group):
    if not counter:
        return fallback_group or G_PERS
    top = max(counter.values())
    tied = [g for g in GROUP_ORDER if counter.get(g) == top]
    if fallback_group in tied:
        return fallback_group
    return tied[0]


# ---- build ---------------------------------------------------------------------------------
# ---- the "worth a glossary line" filter ---------------------------------------------------
# A name is HARD (speech-to-text will likely misspell it) when it is structurally unusual —
# hyphen, accent, 3+ tokens (multi-word surname / middle name), internal capital (McSample,
# DeSample), a 1-2 letter surname — or when a token shows a non-English spelling pattern below.
# Tokens in the short COMMON list are never "non-English". Everything else is EASY and is kept
# only for another reason (spoken alias, collision, correction).
_NON_ENGLISH_ANY = re.compile(
    r"aa|ii|uu|ij|iy|kh|zh|dh|bh|jh|sch|cz|sz|dz|q(?!u)|x(?!$)"
    r"|[bcdfgklmnprstvz]j|j[bcdfgklmnprstvz]")
_NON_ENGLISH_FIRST_END = re.compile(r"[^e]i$")              # Imani, Olufemi (not Josie)
_NON_ENGLISH_SURNAME = re.compile(
    r"z|eu|oi|au|ua|uo|ae|(?:a|o|u|i|is|ez|sky|ski|cky|cki|ov|ova|ev|ich|bach|ik)$")
COMMON = set("""
laura paul maria mary davis ellis curtis willis lewis harris morris travis dennis louis
francis julia joshua dana lisa linda diana anna sarah hannah andrea emma olivia sofia maya
""".split())


def _token_hard(tok, surname):
    f = _fold(tok)
    if f in COMMON:
        return False
    if _NON_ENGLISH_ANY.search(f):
        return True
    if surname:
        return bool(_NON_ENGLISH_SURNAME.search(f))
    return bool(_NON_ENGLISH_FIRST_END.search(f))


def is_hard_name(name):
    if "-" in name or _fold(name) != name.lower() or re.search(r"[a-z][A-Z]", name):
        return True
    toks = [t for t in name.split() if not re.match(r"^[A-Z]\.?$", t)]
    if len(toks) >= 3 or len(toks) < 2 or len(toks[-1]) <= 2:
        return True
    return _token_hard(toks[0], False) or _token_hard(toks[-1], True)


# spoken nickname -> formal first name, only so the first-name key sees Tim ~ Timothy
NICKNAMES = {"jen": "jennifer", "jenn": "jennifer", "jess": "jessica", "matt": "matthew",
             "mike": "michael", "chris": "christopher", "kris": "kristen", "steph": "stephanie",
             "steve": "stephen", "abby": "abigail", "liz": "elizabeth", "beth": "elizabeth",
             "kate": "katherine", "katie": "katherine", "dave": "david", "rob": "robert",
             "bob": "robert", "tom": "thomas", "tim": "timothy", "sam": "samuel",
             "maddie": "madison", "ray": "raymond"}


def _soundex(word):
    w = re.sub(r"[^a-z]", "", _fold(word))
    if not w:
        return ""
    w = w.replace("ph", "f")
    w = ("k" + w[1:]) if w[0] == "c" else w
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
             **dict.fromkeys("dt", "3"), "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
    out, prev = w[0].upper(), codes.get(w[0], "")
    for ch in w[1:]:
        c = codes.get(ch, "")
        if c and c != prev:
            out += c
        if ch not in "hw":
            prev = c
    return out


def first_name_key(name):
    """First two letters + untruncated Soundex + vowel-group count (~syllables) of the
    nickname-expanded first name, with 'C'->'K' and 'ph'->'f' first: Katrin ~ Catrin,
    Sofia ~ Sophia, Mathilde ~ Matilda, Tim ~ Timothy, Brian ~ Bryan — but not Simon ~ Shannon,
    Robert ~ Rupert, Michelle ~ Michael or Stephanie ~ Steve."""
    first = _fold(name.split()[0]) if name.split() else ""
    first = NICKNAMES.get(first, first)
    first = ("k" + first[1:]) if first.startswith("c") else first.replace("ph", "f")
    vowel_groups = len(re.findall(r"[aeiouy]+", first))
    return f"{first[:2]}:{_soundex(first)}:{vowel_groups}"


def load_corrections(root, source="granola"):
    path = os.path.join(root, "_manifest", "corrections", f"{source}.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def corrected_people(corrections, index_by_path, canon, res):
    """People a granola correction is about. A finding names someone by full canonical name
    (anywhere), or by first name / surname when that person is a participant of the finding's
    own note (findings say 'Rafael', 'Lucia's', 'Greta's')."""
    hits = set()
    for c in corrections:
        text = c.get("finding") or ""
        low = _fold(text)
        for n in canon:
            if len(n.split()) >= 2 and _fold(n) in low:
                hits.add(n)
        rec = index_by_path.get(c.get("note_path") or "")
        for p in {res(x) for x in (rec or {}).get("participants") or []} - {None}:
            forms = {_fold(t) for t in _tokens(p) if len(t) >= 3}
            forms |= {_fold(a) for a in canon.get(p, {}).get("aliases", ())
                      if _ALIAS_RE.match(a) and len(a) >= 3}
            if any(re.search(r"\b" + re.escape(f) + r"\b", low) for f in forms):
                hits.add(p)
    return hits


# ---- build ---------------------------------------------------------------------------------
def build(root, min_meetings=DEFAULT_MIN_MEETINGS, days=DEFAULT_DAYS, as_of=None,
          template_rel=TEMPLATE_REL, regular_min=DEFAULT_REGULAR_MIN, roles=False):
    """-> (text, stats). Pure: reads root's people.md / index.jsonl / corrections / template,
    writes nothing."""
    canon, alias_to_canon = load_registry(root)
    index = load_index(root)
    with open(os.path.join(root, template_rel), encoding="utf-8") as f:
        template = f.read()
    preamble, people_lines, tail, _span = split_template(template)

    scoped = [r for r in index if r.get("source") in IN_SCOPE_SOURCES]
    if as_of:
        anchor = as_of
    else:
        anchor = max((r.get("ingested") or "" for r in scoped), default="") or \
            datetime.date.today().isoformat()
    lo = (datetime.date.fromisoformat(anchor) - datetime.timedelta(days=days)).isoformat()

    # A tombstone row ("Ann Example | DUPLICATE — see Ann B. Example") can own the
    # alias_to_canon entry for its old spelling; redirect it to the one row that lists that
    # spelling as an alias, and keep tombstones out of the alias-claim map.
    redirect = {}
    for n, row in canon.items():
        if is_tombstone(row):
            tgt = [c for c, r in canon.items() if c != n and not is_tombstone(r)
                   and n.lower() in (a.lower() for a in r.get("aliases", ()))]
            redirect[n] = tgt[0] if len(tgt) == 1 else None
    live = {n: r for n, r in canon.items() if n not in redirect}

    def res(p):
        c = resolve(p, alias_to_canon)
        return redirect.get(c, c) if c in redirect else c

    all_groups = collections.defaultdict(collections.Counter)   # any source/date: out-of-pool
    for r in index:
        g = DOMAIN_GROUP.get(r.get("domain"))
        for p in {res(x) for x in (r.get("participants") or [])} - {None}:
            if g:
                all_groups[p][g] += 1

    counts = collections.Counter()
    win_groups = collections.defaultdict(collections.Counter)
    for r in scoped:
        if not lo <= (r.get("date") or "") <= anchor:
            continue
        g = DOMAIN_GROUP.get(r.get("domain"))
        for p in {res(x) for x in (r.get("participants") or [])} - {None}:
            counts[p] += 1
            if g:
                win_groups[p][g] += 1

    def eligible(n):
        return n in live and not is_self(live[n]) and is_person_name(n)

    pool = {n for n, c in counts.items() if c >= min_meetings and eligible(n)}
    claims = alias_claims(live)
    alias_info = {n: spoken_aliases(n, live[n], claims) for n in pool}
    index_by_path = {r.get("path"): r for r in index}
    corrections = load_corrections(root)
    corrected = corrected_people(corrections, index_by_path, live, res) & pool

    reasons = collections.defaultdict(list)
    for n in pool:
        if counts[n] >= regular_min:
            reasons[n].append("regular")
        if is_hard_name(n):
            reasons[n].append("hard-name")
        if alias_info[n][0] or alias_info[n][1]:
            reasons[n].append("spoken-alias")
        if n in corrected:
            reasons[n].append("corrected")
    # (c) collisions: a registry alias claimed by >1 person (every claimant kept), and a shared
    # first-name sound key among the pool (every party kept).
    # A registry alias claimed by >1 person is emitted when a claimant is in the pool OR a granola
    # correction quotes the alias (a 'Sam Example' garble) — then every claimant is kept.
    corr_text = " ".join(_fold(c.get("finding") or "") for c in corrections)
    ambiguous = {}
    for al_low, who_all in claims.items():
        who = sorted(w for w in who_all if eligible(w))
        if len(who) < 2:
            continue
        if not (set(who) & pool) and not re.search(r"\b" + re.escape(_fold(al_low)) + r"\b",
                                                    corr_text):
            continue
        shown = next((a for w in who for a in live[w].get("aliases", ()) if a.lower() == al_low),
                     al_low)
        if not _ALIAS_RE.match(shown):
            continue
        ambiguous.setdefault(al_low, (shown, who))
    by_key = collections.defaultdict(list)
    for n in pool:
        by_key[first_name_key(n)].append(n)
    first_collisions = [sorted(v) for k, v in sorted(by_key.items()) if k and len(v) > 1]
    for grp in first_collisions:
        for n in grp:
            reasons[n].append("collision")
    for _al, who in ambiguous.values():
        for n in who:
            reasons[n].append("collision")

    selected = {n for n, why in reasons.items() if why}
    for n in selected:
        if n not in alias_info:
            alias_info[n] = spoken_aliases(n, live[n], claims)

    def group_of(n):
        row = live[n]
        fb = DOMAIN_GROUP.get(registry_domain(row))
        if is_personal(row):
            return G_PERS
        return _majority(win_groups.get(n) or all_groups.get(n), fb)

    groups = {g: [] for g in GROUP_ORDER}
    for n in sorted(selected):
        row = live[n]
        akas = alias_info[n][0]
        entry = n + (f" (aka {', '.join(akas)})" if akas else "")
        if row.get("verify"):
            entry += " ⚠"
        elif roles:
            role = short_role(row)
            if role:
                entry += f" — {role}"
        groups[group_of(n)].append(entry)

    # First-name sound-alike lines: for every KEPT person, the kept parties sharing their
    # first-name key plus registry people OUTSIDE the glossary with that key who are participants
    # in >= 1 in-window GRANOLA note (email/iMessage presence doesn't count — never spoken),
    # most-seen first,
    # capped at MAX_PARTIES names per line. Outside parties are named with their group only;
    # they are NOT added as PEOPLE entries.
    outside_by_key = collections.defaultdict(list)
    for n in live:
        if n in selected or not eligible(n) or len(n.split()) < 2:
            continue                                   # a bare "Caroline" disambiguates nothing
        if not counts.get(n):
            continue                                   # never spoken in a window meeting
        outside_by_key[first_name_key(n)].append(n)
    first_lines = []
    for k in sorted({first_name_key(n) for n in selected}):
        if not k:
            continue
        kept = sorted(n for n in selected if first_name_key(n) == k)
        outside = sorted(outside_by_key.get(k, []), key=lambda n: (-counts[n], n))
        parties = kept + outside[:max(0, MAX_PARTIES - len(kept))]
        if len(parties) < 2:
            continue
        spoken = []
        for n in parties:
            f = n.split()[0]
            if _fold(f) not in (_fold(x) for x in spoken):
                spoken.append(f)
        who = " / ".join(f"{n} ({GROUP_SHORT[group_of(n)]})" for n in parties)
        first_lines.append(f'  ⚠ "{"/".join(spoken)}" alone = {who} — by context')

    out = list(preamble)
    while out and not out[-1].strip():
        out.pop()
    for g in GROUP_ORDER:
        if groups[g]:
            out.append(GROUP_HEADERS[g])
            out.append("  " + "; ".join(groups[g]))
    notes = [f'  ⚠ "{al}" = {" or ".join(who)} — by context, else as heard + [sp?]'
             for _k, (al, who) in sorted(ambiguous.items()) if len(who) > 1]
    notes += first_lines
    if notes:
        out.append("AMBIGUOUS SPOKEN FORMS (never snap blindly):")
        out.extend(notes)
    out.extend(tail)
    text = "\n".join(out).rstrip() + "\n"

    dropped = []
    for tn in template_people_names(people_lines):
        c = res(tn)
        if c in selected or (c and not eligible(c)):
            continue
        if not c:
            why = "not in people.md"
        elif counts.get(c, 0) < min_meetings:
            why = f"{counts.get(c, 0)} granola meeting(s) in window"
        else:
            why = "easy name, not a regular, no alias/collision/correction"
        if (tn, why) not in dropped:
            dropped.append((tn, why))
    stats = dict(anchor=anchor, window_start=lo, pool=len(pool), selected=len(selected),
                 group_sizes={g: len(groups[g]) for g in GROUP_ORDER},
                 reasons={n: reasons[n] for n in sorted(selected)},
                 ambiguous=len(notes), dropped_template_names=dropped, chars=len(text))
    return text, stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=ROOT, help="store root (tests point this at a fixture)")
    ap.add_argument("--min-meetings", type=int, default=DEFAULT_MIN_MEETINGS,
                    help="granola meetings in the window needed to be considered")
    ap.add_argument("--regular-min", type=int, default=DEFAULT_REGULAR_MIN,
                    help="granola meetings in the window that always keep a person")
    ap.add_argument("--roles", action="store_true",
                    help="append registry roles (off: registry roles go stale and the template "
                         "lets glossary roles flow into ATTENDEES)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="recency window, days")
    ap.add_argument("--as-of", help="window anchor YYYY-MM-DD (default: newest granola ingest)")
    ap.add_argument("--template", default=TEMPLATE_REL, help="template path, relative to --root")
    ap.add_argument("--out", default=OUT_REL, help="output path, relative to --root")
    ap.add_argument("--check", action="store_true", help="exit 1 if --out is stale; write nothing")
    ap.add_argument("--stdout", action="store_true", help="print the block instead of writing it")
    ap.add_argument("--why", action="store_true", help="print each kept person's reasons")
    a = ap.parse_args(argv)

    text, st = build(a.root, a.min_meetings, a.days, a.as_of, a.template,
                     regular_min=a.regular_min, roles=a.roles)
    out_path = os.path.join(a.root, a.out)
    if a.check:
        try:
            with open(out_path, encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current != text:
            print(f"STALE {a.out} — regenerate: python3 scripts/granola_glossary.py", file=sys.stderr)
            return 1
        print(f"OK {a.out} is current")
        return 0
    if a.stdout:
        sys.stdout.write(text)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    for tn, why in st["dropped_template_names"]:
        print(f"DROPPED from current template: {tn} ({why})", file=sys.stderr)
    if a.why:
        for n, why in st["reasons"].items():
            print(f"KEPT {n}: {', '.join(why)}", file=sys.stderr)
    gs = ", ".join(f"{g}={st['group_sizes'][g]}" for g in GROUP_ORDER)
    print(f"{'' if a.stdout else 'wrote ' + a.out + ': '}{st['chars']} chars "
          f"(target < {TARGET_CHARS}); window {st['window_start']}..{st['anchor']}; "
          f"{st['selected']} kept of {st['pool']} with >= {a.min_meetings} granola meetings; "
          f"groups {gs}; ambiguity lines {st['ambiguous']}", file=sys.stderr)
    if st["chars"] >= TARGET_CHARS:
        print(f"WARN {st['chars']} chars >= {TARGET_CHARS}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
