#!/usr/bin/env python3
"""Build the clarification queue: harvest engram's low-confidence signals into a ranked
list of questions for the store's owner to answer in an interview (the `/clarify` skill drives it).

    python3 scripts/build_clarify_queue.py            # regenerate _manifest/clarify-queue.jsonl
    python3 scripts/build_clarify_queue.py --summary  # also print bucket counts

What it harvests (genuinely-open low-confidence, ranked by impact x uncertainty):
  - people-dedup    : a name that looks like a spelling/alias variant of another person ALREADY in
                      people.md (e.g. "Jan Exampel" ~ "Jan Example"). Swept over FOUR
                      name spaces: people-discovered.md rows, registry rows, every `participants:`
                      string in index.jsonl, and person-shaped [[wikilinks]] in obsidian notes.
  - people-cluster  : a whole connected group of spelling variants NONE of which is in people.md —
                      asked ONCE for the group ("which spelling is canonical?") instead of pairwise
                      (see `cluster_items` for why the pairwise form was unanswerable there). This
                      question ASSERTS that the group is one person, so its edges use the STRICTER
                      `cluster_edge` relation; a pair that fails it stays a neutral people-dedup.
  - people-promote  : a recurring discovered name NOT yet in people.md (verify -> add?).
  - people-role     : a canonical person whose role is "tentative" / "(role TBD)".
  - domain          : notes flagged `needs-domain-confirm` / `domain-review`, PLUS every note parked
                      in `unsorted/` — which CLAUDE.md's own convention makes a note awaiting a
                      domain decision, and which nothing asked about before (zero domain items
                      existed while 27 notes sat there).
  - thin-source     : `empty-source` / `source-limited` notes (keep or archive?).

Idempotent + resumable: an item's stable `id` is derived from its content, so re-running
preserves any non-`open` status (answered / skipped / deferred) from a prior interview.
The `/clarify` skill writes answers back into people.md / notes / manifest and clears the
underlying flag; the next regen then simply stops emitting that question.
"""
import re, glob, os, json, sys, difflib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS as TOPS
from _lib.notes import split_frontmatter, get_field   # single owner: frontmatter parsing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
QUEUE = "_manifest/clarify-queue.jsonl"
MANUAL = "_manifest/clarify-manual.jsonl"
_MANUAL_REQUIRED = {"id", "category", "priority", "entity", "question", "candidates",
                    "evidence", "status", "added"}


def slug(s):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', s.lower())).strip('-')


# ---- parse _manifest/people.md -------------------------------------------------------
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]*\w')

# Cells that carry a FLAG, not a role. The role hint is "the last cell that isn't a flag or a bare
# domain word", and the Verify? column was NOT in this set: every 5-column harvest row
# (`| Name | Aliases/emails | Role | Domain | Verify? |`) therefore resolved to a role of 'true'
# — 80 of 456 parsed rows on 2026-08-04 — which went straight into the people-verify question text
# ("role hint: 'true'"). Compared lowercased, so 'True'/'YES' are covered too.
_ROLE_FLAG_CELLS = {"yes", "no", "true", "false", "verify", "tentative", "n/a", "none",
                    "—", "–", "-", ""}

# A canonical name legitimately appearing on TWO rows: a cross-section role entry (one person listed
# under two sections of people.md) or an explicit tombstone left by an earlier merge
# ("DUPLICATE — merged into the collaborators table above"). BOTH kinds say so in
# the row text; an accidental collision (the same person independently re-harvested into a sync
# section) does not. Only ever suppresses a WARN — it can never merge or edit anything.
_CROSS_REF_RE = re.compile(r'\bduplicate\b|\bmerged\s+(?:in)?to\b|\bsee\s+[^|]{0,40}\btable\b'
                           r'|\bfull\s+aliases\s+in\b', re.I)


def _merge_person_row(prev, role, tentative, verify, section, rowtext):
    """Fold a SECOND people.md row for an already-seen canonical name into the first.

    `canon[name] = dict(...)` used to be unconditional, so the LAST row silently won: a name in a
    curated section plus one in an auto-harvest section lost the curated role AND inherited the
    harvest row's `verify:true` (live example, fixed by hand: a person with a curated "Principal
    at Example Partners" role + a Gmail-harvest row). Merge rules:
      * role      -> every DISTINCT non-empty role, joined ' | ', first-seen order (people.md lists
                     curated sections before harvest ones). Picking one would discard the other,
                     and both are shown to the reviewer as the hint they are asked to confirm;
                     joining is also what the hand-repair of that collision did.
      * tentative -> any(): a "role TBD" marker on any row still needs pinning down.
      * verify    -> all(): a CONFIRMED row is a human's own answer, and a later auto-harvest row
                     re-adding the same name is a DUPLICATE, not a de-confirmation. (Last-wins
                     re-opened confirmed people as people-verify questions.)
      * aliases / emails -> union (aliases are added by the caller, after this merge).
    `sections` / `rowtexts` accumulate so `people_row_collisions` can report the real collisions.
    """
    if role and role not in prev["role"].split(" | "):
        prev["role"] = " | ".join(x for x in (prev["role"], role) if x)
    prev["tentative"] = prev["tentative"] or tentative
    prev["verify"] = prev["verify"] and verify
    prev["emails"] |= set(_EMAIL_RE.findall(rowtext))
    prev["sections"].append(section)
    prev["rowtexts"].append(rowtext)
    return prev


# A BARE (unquoted) token in an Aliases cell that is safe to register as an alias. Quoting was a
# load-bearing convention nobody enforced: parse_people harvested only `"quoted"` strings, so
# `| Sam Sample | Samuel Sample | …` registered NOTHING and `samuel sample` resolved to
# None — the alias was visible to a human and invisible to every consumer. Deliberately narrow:
# a title-cased name-shaped token only. Prose ("full aliases in the consortium table"), paths, phone
# numbers and emails are all excluded, so widening the reader cannot start ingesting descriptions.
_BARE_ALIAS_RE = re.compile(r"^[A-Z][\w.'\-]*(?:\s+[\w.'\-]+){0,4}$")

# Aliases cells are not always aliases: some rows put an AFFILIATION there
# (`| Ann Example | University of Example | …`), which is name-shaped and would otherwise register
# 'university of example' as an alias of a person. Institutions only — never a surname.
# Long stems match as PREFIXES (`universit\w*` catches University/Universities); the short,
# surname-ambiguous ones match as WHOLE WORDS only, so 'Banks' and 'Groups' stay valid surnames.
_NOT_A_PERSON_RE = re.compile(
    r'\b(?:universit\w*|institut\w*|hospital\w*|college\w*|school\w*|clinic\w*|foundation\w*'
    r'|laborator\w*|department\w*|division\w*|centre|center)\b'
    r'|\b(?:inc|llc|ltd|corp|group|partners|capital|bank|associates)\b', re.I)


def _alias_cell_tokens(cell):
    """Alias strings in an Aliases cell — the quoted ones AND the bare name-shaped ones.

    Quoted strings are taken verbatim (the established shape, and the only one the old reader
    saw). What remains after removing them is split on ';'/',' and kept only when it looks like a
    person name; anything containing a backtick, parenthesis, colon, '@' or a digit-run is treated
    as annotation, not an alias.
    """
    if not cell or cell.strip() in ("—", "–", "-", ""):
        return []
    out = [q for q in re.findall(r'"([^"]+)"', cell)]
    rest = re.sub(r'"[^"]*"', " ", cell)
    for tok in re.split(r'[;,]', rest):
        tok = tok.strip()
        if not tok or "@" in tok or "`" in tok or "(" in tok or ")" in tok or ":" in tok:
            continue
        if re.search(r'\d', tok):                     # phone numbers, ids
            continue
        if _NOT_A_PERSON_RE.search(tok):              # an affiliation, not an alias
            continue
        if _BARE_ALIAS_RE.match(tok):
            out.append(tok)
    return out


def parse_people():
    """Return (canonical {name -> {aliases,role,tentative,verify,section,emails,...}}, alias_to_canon).

    A canonical name on more than one row is MERGED (see `_merge_person_row`), never overwritten,
    and `people_row_collisions` reports the ones that are not a by-design cross-reference.

    Aliases are read BOTH from any quoted string anywhere in the row (the historical convention)
    and STRUCTURALLY from the row's Aliases column when its table has one — see
    `_alias_cell_tokens` for why the quoted-only reader silently dropped real aliases.
    """
    txt = open("_manifest/people.md", encoding="utf-8").read()
    canon, alias_to_canon, section = {}, {}, ""
    in_discovered = False
    alias_col = None
    for line in txt.splitlines():
        h = re.match(r'^##\s+(.*)', line)
        if h:
            section = h.group(1).strip()
            in_discovered = section.lower().startswith(("discovered", "networks"))
            alias_col = None                                  # tables never span sections
            continue
        if in_discovered or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0]
        if name in ("Canonical name", "Name", "") or set(name) <= set("-: "):
            # A real HEADER row records which column of THIS table holds aliases. The `|---|---|`
            # SEPARATOR also lands here, and must NOT reset it — doing so cleared the index between
            # the header and the first data row, which is why the structural read found nothing.
            if not all(set(c) <= set("-: ") for c in cells):
                alias_col = next((i for i, c in enumerate(cells)
                                  if i and re.search(r'alias', c, re.I)), None)
            continue
        if name.startswith("_") or name.startswith("["):  # placeholder / link rows
            continue
        rowtext = " ".join(cells[1:])
        tentative = bool(re.search(r'tentative|role TBD|\(role TBD\)|TBD', rowtext, re.I)) \
            or "Tentative role" in section
        # verify flag: a "yes" cell in a "Verify?" column, OR an inline "(verify)" / "verify:true"
        # marker (some sections — e.g. COI — have no Verify? column and mark rows inline).
        verify = any(c.strip().lower() in ("yes", "true", "verify") for c in cells[1:]) \
            or "(verify)" in rowtext.lower() or "verify:true" in rowtext.lower() \
            or "verify: true" in rowtext.lower()
        # role hint = the most descriptive cell: skip blanks, flag cells, and bare domain names
        role = next((c for c in reversed(cells[1:])
                     if c.lower() not in _ROLE_FLAG_CELLS and c.lower() not in TOPS), "")
        row = canon.get(name)
        if row is None:
            row = canon[name] = dict(aliases=set(), role=role, tentative=tentative,
                                     verify=verify, section=section,
                                     emails=set(_EMAIL_RE.findall(rowtext)),
                                     sections=[section], rowtexts=[rowtext])
        else:
            _merge_person_row(row, role, tentative, verify, section, rowtext)
        for q in re.findall(r'"([^"]+)"', rowtext):           # quoted aliases, anywhere in the row
            row["aliases"].add(q); alias_to_canon[q.lower()] = name
        if alias_col is not None and alias_col < len(cells):  # ...plus the Aliases COLUMN
            for al in _alias_cell_tokens(cells[alias_col]):
                row["aliases"].add(al); alias_to_canon[al.lower()] = name
        alias_to_canon[name.lower()] = name
    return canon, alias_to_canon


def people_row_collisions(canon):
    """[(name, [sections])] for every canonical name on >1 people.md row where NO row cross-
    references another — i.e. the accidental duplicates, not the by-design cross-section entries
    or the "DUPLICATE — merged above" tombstones (see `_CROSS_REF_RE`).

    Reported as a WARN by `build()`, deliberately NOT as a clarify item: there is no write-back
    recipe that can fold two rows together (`_RECIPES` in clarify_writeback.py has no such op), so
    an emitted item would be exactly the unanswerable kind this pass exists to stop shipping. The
    WARN lands in `check.sh` output, where a human/agent fixes people.md by hand.
    """
    out = []
    for name, m in sorted(canon.items()):
        if len(m.get("sections", ())) < 2:
            continue
        if any(_CROSS_REF_RE.search(t or "") for t in m.get("rowtexts", ())):
            continue
        out.append((name, list(m["sections"])))
    return out


# ---- parse _manifest/people-discovered.md --------------------------------------------
# Which column of a discovered-people row is the "top domain" hint: whichever cell IS a canonical
# domain word. Derived from the single owner (_lib.constants.DOMAINS, imported as TOPS) — a local
# copy is how this set silently stopped recognising a renamed domain.
_DOMAIN_HINT = set(TOPS)


def parse_discovered():
    """Discovered-people rows across BOTH table formats in people-discovered.md:
      * Granola harvest:  `| Name | <meetings int> | Top domain | In registry? |`
      * Outlook harvest:  `| Name | <email> | Verify? |`  (col 2 is an email, not a count)
    The old parser required an integer in column 2, so it silently dropped every Outlook row (fixed
    2026-07-07). `meetings` is the explicit count when present, else None (main() falls back to the
    live participant-frequency from index.jsonl). `domain` is taken from whichever column is a known
    domain word, else ''. `email` is the address in whichever column carries one (the Outlook
    harvest's column 2), else '' — it is the strongest identity evidence the store holds about an
    unregistered name, and every dedup question used to hide it (see `email_tell`)."""
    out = []
    if not os.path.exists("_manifest/people-discovered.md"):   # fresh store: nothing harvested yet
        return out
    for line in open("_manifest/people-discovered.md", encoding="utf-8"):
        m = re.match(r'^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|', line)
        if not m:
            continue
        name, c2, c3 = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if name in ("Name", "Canonical name") or set(name) <= set("-: "):  # header / separator row
            continue
        meetings = int(c2) if c2.isdigit() else None
        domain = c3 if c3.lower() in _DOMAIN_HINT else (c2 if c2.lower() in _DOMAIN_HINT else "")
        email = next((e.group(0) for e in (_EMAIL_RE.search(c2), _EMAIL_RE.search(c3)) if e), "")
        out.append(dict(name=name, meetings=meetings, domain=domain, email=email))
    return out


# ---- scan note frontmatter for control flags -----------------------------------------
def scan_flagged():
    flags = collections.defaultdict(list)   # flag -> [(path, title)]
    files = [f for d in TOPS for f in glob.glob(f"{d}/**/*.md", recursive=True)
             if not f.endswith("_index.md")]
    for fp in files:
        txt = open(fp, encoding="utf-8").read()
        if not txt.startswith("---"):
            continue
        head = txt[:txt.index("\n---", 3)] if "\n---" in txt[3:] else txt
        tags = (re.search(r'^tags:\s*\[(.*?)\]', head, re.M) or ["", ""])[1]
        title = (re.search(r'^title:\s*"?(.*?)"?\s*$', head, re.M) or ["", os.path.basename(fp)])[1]
        for f in ("third-party-sensitive", "needs-domain-confirm", "domain-review",
                  "needs-review", "empty-source", "source-limited"):
            if f in tags:
                flags[f].append((fp, title))
    return flags


def note_title(fp):
    try:
        head = open(fp, encoding="utf-8").read()
    except OSError:
        return os.path.basename(fp)
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', head, re.M)
    return m.group(1) if m else os.path.basename(fp)


_EXCERPT_CHARS = 300


def note_brief(fp):
    """{title, source, tags, participants, related, excerpt} for a note — enough for a reviewer to decide
    its DOMAIN from the queue without opening the file (the unsorted/ domain items, section 3b).

    Frontmatter is read through `_lib.notes` (the single owner) rather than this file's ad-hoc
    header regexes; `excerpt` is the note's first line of real prose — the `## Summary` opener for a
    distilled note, the first non-heading line otherwise — truncated on a word boundary. `related`
    matters because 17 of the 27 unsorted notes are obsidian PERSON STUBS with an entirely empty
    body: their only domain signal is who they are linked to."""
    empty = dict(title=os.path.basename(fp), source="", tags="", participants="", related="",
                 excerpt="")
    try:
        txt = open(fp, encoding="utf-8").read()
    except OSError:
        return empty
    head, body = split_frontmatter(txt)
    if head is None:
        return empty
    excerpt = ""
    for ln in (body or "").split("\n"):
        s = ln.strip()
        if not s or s in ("---",) or s.startswith("#") or s.startswith("---"):
            continue
        excerpt = re.sub(r'\s+', ' ', s)
        break
    if len(excerpt) > _EXCERPT_CHARS:
        excerpt = excerpt[:_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
    return dict(title=get_field(head, "title") or os.path.basename(fp),
                source=get_field(head, "source"),
                tags=get_field(head, "tags"),
                participants=get_field(head, "participants"),
                related=get_field(head, "related"),
                excerpt=excerpt)


def parse_review_report():
    """Some adjudicated flags live in an optional, hand-written `_manifest/review-report.md`
    (prose; absent in a fresh store, so this harvests nothing), not in note frontmatter.
    Harvest the `path.md` references under the third-party / thin-source findings."""
    flags = collections.defaultdict(list)
    fp = "_manifest/review-report.md"
    if not os.path.exists(fp):
        return flags
    section = ""
    for line in open(fp, encoding="utf-8"):
        h = re.match(r'^#{2,3}\s+(.*)', line)
        if h:
            section = h.group(1).lower()
            continue
        for path in re.findall(r'`([^`]+\.md)`', line):
            if not os.path.exists(path):
                continue
            if "third-party" in section:
                flags["third-party-sensitive"].append((path, note_title(path)))
            elif "empty-source" in line or "empty at source" in line:
                flags["empty-source"].append((path, note_title(path)))
            elif "source-limited" in line:
                flags["source-limited"].append((path, note_title(path)))
    return flags


# Role questions for a people.md section whose answers are gated on an artifact nobody has yet
# (an org chart, a signed roster): the item is still emitted, but DE-prioritised and stamped with
# `blocked_on` so an interview can skip it. `(section-heading prefix, blocker text)`, lowercased
# prefix match. Empty is the right default — add an entry only for a section you know is blocked.
BLOCKED_ROLE_SECTIONS = ()

# ---- name-similarity heuristic for dedup ---------------------------------------------
# One owner for the similarity threshold: `looks_like_variant` is the ONLY name-similarity
# decision in this file, and `_variant_candidates` reuses this constant for its cheap
# difflib upper-bound prefilter, so the two can never drift apart.
VARIANT_RATIO = 0.84
FIRST_NAME_RATIO = 0.70     # given-name similarity that counts as "same person, other form"
FIRST_NAME_PREFIX = 3       # ... or this many shared leading characters (cath|erine, jac|quelyn)

# Clause (e): a hand-written ALLOWLIST of diminutive / formal given-name equivalences. It only
# ever WIDENS the set of QUESTIONS asked — it never merges anything, never edits people.md, and
# never rewrites a note; a wrong entry costs the reviewer one extra "no" in /clarify. Two given names in
# the same tuple count as related. Deliberately small and explicit: an algorithmic nickname
# expander would re-open exactly the family-member false positives the surname guard closes.
_DIMINUTIVE_GROUPS = (
    ("charles", "chuck", "charlie"), ("michael", "mike", "mick"),
    ("robert", "bob", "rob", "bobby"), ("william", "bill", "will", "billy"),
    ("richard", "rick", "dick"), ("james", "jim", "jamie"), ("thomas", "tom"),
    ("david", "dave"), ("joseph", "joe"), ("edward", "ed", "ted"), ("kenneth", "ken"),
    ("elizabeth", "liz", "beth", "betsy"),
    ("katherine", "catherine", "kate", "kathy", "cathy"),
    ("margaret", "peg", "peggy", "maggie"), ("patricia", "pat", "patty", "tricia"),
    ("jennifer", "jen", "jenny"), ("deborah", "deb", "debbie"), ("susan", "sue"),
    ("anthony", "tony"), ("lawrence", "larry"), ("gregory", "greg"), ("matthew", "matt"),
    ("nicholas", "nick"), ("samuel", "sam"), ("benjamin", "ben"), ("alexander", "alex"),
    ("jonathan", "jon"), ("christopher", "chris"), ("daniel", "dan", "danny"),
    ("stephen", "steven", "steve"), ("andrew", "andy", "drew"), ("timothy", "tim"),
    ("peter", "pete"), ("ronald", "ron"), ("donald", "don"), ("frederick", "fred"),
)
_DIMINUTIVE_OF = {n: i for i, group in enumerate(_DIMINUTIVE_GROUPS) for n in group}

# Clause (d): a given-name token that is a bare initial — 'j', 'j.', 'm.m.', 'r.h.'. The dotted
# form is required for multi-letter runs so a genuine short name ('bo', 'jo', 'al') is never read
# as a pair of initials.
_INITIAL_TOKEN = re.compile(r'^(?:[a-z]\.)+$|^[a-z]$')


def _as_initials(given):
    """The letter sequence if EVERY given-name token is a bare initial, else None."""
    letters = []
    for t in given:
        if not _INITIAL_TOKEN.match(t):
            return None
        letters.extend(ch for ch in t if ch.isalpha())
    return letters or None


def _initials_lead(letters, given):
    """Do `letters` match the leading letters of `given`'s tokens, IN ORDER and fully?

    Requires one given-name token per initial ('a','b' vs ['ann','beth'] -> yes), which is
    what keeps 'A.B. Example' ~ 'Ann Beth Example' while dropping 'A.B. Example' ~ 'Alice Example':
    two initials cannot be satisfied by one given name. The surname equality in
    `looks_like_variant` is what stops an initial from matching everyone who shares it."""
    if not letters or len(letters) > len(given):
        return False
    return all(tok.startswith(ch) for ch, tok in zip(letters, given))


def given_names_related(ga, gb):
    """Could these two GIVEN-name token lists (lowercased, dots KEPT) belong to one person?

    Guards the surname branch of `looks_like_variant`. A shared first INITIAL is NOT enough: over
    the ~1.9k-name participant space that fires on family members and same-surname colleagues
    ('Alan Example' ~ 'Alice Example' — two relatives; 'Pat Placeholder' ~ 'Pam
    Placeholder'), and a wrong "yes" to one of those rewrites participant rosters store-wide. Related =
      (a) identical, (b) ratio >= FIRST_NAME_RATIO, (c) a shared FIRST_NAME_PREFIX-char prefix,
      (d) one side is bare initials leading the other ('S.' ~ 'Sam', 'A.B.' ~ 'Ann Beth'),
      (e) both sides are in one _DIMINUTIVE_GROUPS tuple ('Chuck' ~ 'Charles').
    (c)-(e) are what keep the short-form pairs the whole-name ratio branch cannot see ('Cathy' ~
    'Catherine' ratio 0.57, 'Wilhelmina' ~ 'Mina' 0.57, 'Chuck' ~ 'Charles' 0.33)."""
    if not ga or not gb:
        return False
    ia, ib = _as_initials(ga), _as_initials(gb)
    if ia or ib:                                    # (d) — an initialled form matches no other way
        return (_initials_lead(ia, gb) if ia else False) or \
               (_initials_lead(ib, ga) if ib else False)     # symmetric: order must not matter
    a, b = ga[0].replace(".", ""), gb[0].replace(".", "")
    if a == b:                                                                          # (a)
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= FIRST_NAME_RATIO:                  # (b)
        return True
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    if n >= FIRST_NAME_PREFIX:                                                           # (c)
        return True
    return a in _DIMINUTIVE_OF and _DIMINUTIVE_OF[a] == _DIMINUTIVE_OF.get(b)            # (e)


def looks_like_variant(a, b):
    # `na`/`nb` (dots stripped) drive the whole-name ratio branch and the surname equality exactly
    # as they always did; the TOKENS keep their dots, because clause (d) cannot tell 'A.B.' from a
    # name like 'Bo' once the dots are gone.
    la, lb = a.lower(), b.lower()
    na, nb = la.replace(".", ""), lb.replace(".", "")
    if na == nb:
        return False
    if difflib.SequenceMatcher(None, na, nb).ratio() >= VARIANT_RATIO:
        return True
    ta, tb = la.split(), lb.split()
    # same surname + a RELATED given name (e.g. "Sam Sample" ~ "Samuel Q. Sample")
    #
    # KNOWN GAP: the equality is on the WHOLE last token, so a hyphenated / appended surname is
    # invisible here — "Ida Sample" ~ "Ida Moss-Sample" has last tokens 'sample' vs
    # 'moss-sample', and its whole-name ratio is 0.800 (< VARIANT_RATIO), so that pair is NOT
    # detected by either branch. This docstring used to cite it as a worked example, which was
    # false. Widening the equality to a suffix/substring test is deliberately NOT done: over the
    # ~1.9k-name participant space it would relate every hyphenated surname sharing a stem.
    if ta and tb and ta[-1].replace(".", "") == tb[-1].replace(".", "") \
            and given_names_related(ta[:-1], tb[:-1]):
        return True
    return False


def surnames_agree(a, b):
    """Do these two names' SURNAMES agree closely enough to help ASSERT one identity?

    NOT a new similarity function: it is `looks_like_variant`'s own dot-stripped last-token
    equality, widened by the SAME difflib ratio at the SAME VARIANT_RATIO threshold, applied to
    the last token alone — 'Exampler' ~ 'Exempler' 0.875 and 'Specimen' ~ 'Specimn' 0.933 agree;
    'Lee' ~ 'Ellis' 0.25, 'Sample' ~ 'Simple' 0.833 and 'Ames' ~ 'Amos' 0.75 do not.
    """
    ta, tb = a.lower().split(), b.lower().split()
    if not ta or not tb:
        return False
    sa, sb = ta[-1].replace(".", ""), tb[-1].replace(".", "")
    if sa == sb:
        return True
    return difflib.SequenceMatcher(None, sa, sb).ratio() >= VARIANT_RATIO


def cluster_edge(a, b):
    """May this variant pair ASSERT identity — i.e. become an edge of a people-cluster item?

    `looks_like_variant` is deliberately loose because a people-dedup item only ASKS ("is X the
    same person as Y? yes / no"). A people-cluster item ASSERTS its premise ("these N spellings
    are ONE person — which is canonical?"), and one wrong answer promotes a row into people.md and
    rewrites `participants:` + [[wikilinks]] store-wide. So a cluster edge must clear a higher bar
    than the flat whole-name ratio: BOTH name parts have to agree.

    Measured on the live store (2026-08-01), the whole-name ratio alone welded distinct people into
    5 shipped cluster items, in two ways the two clauses close respectively:
      * DIFFERENT surname, shared long given name — a long shared given name alone pushes the
        whole-name ratio over VARIANT_RATIO. 'Maximilian Example' ~ 'Maximilian Sample' 0.914 (two
        unrelated people), 'Bartholomew Ames' ~ 'Bartholomew Amos' 0.938. -> `surnames_agree` is False.
      * SAME surname, UNRELATED given names — 'A. Specimen' ~ 'Sam Specimen' 0.909 welded an
        unrelated A. Specimen onto Sam/Samuel Specimen. Surnames agree,
        so only `given_names_related` can see it: that guard already owns the same-surname decision
        for `looks_like_variant`'s surname branch, and the ratio branch was silently OVERRIDING it
        (the shared surname is most of the string, so the ratio clears 0.84 on its own).

    A pair rejected here is NOT dropped: `looks_like_variant` is untouched, so it falls back to the
    neutral pairwise people-dedup yes/no question, which asserts nothing.
    """
    ta, tb = a.lower().split(), b.lower().split()
    return bool(ta) and bool(tb) and surnames_agree(a, b) \
        and given_names_related(ta[:-1], tb[:-1])


def participant_freq():
    """name(LOWERCASED) -> # notes the person is a participant in (from index.jsonl).

    The live ranking signal for discovered people whose table carries no explicit meeting count
    (the Outlook harvest), and the note count behind the people-verify / dedup evidence.
    Keys are lowercased, so EVERY caller must look up `name.lower()` — a second, exact-cased copy
    of this function used to shadow-define below it, and the one caller that used exact casing
    silently scored every such discovered row at 0 (fixed 2026-08-01)."""
    cnt = collections.Counter()
    if os.path.exists("_manifest/index.jsonl"):
        for line in open("_manifest/index.jsonl", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            for p in r.get("participants", []):
                cnt[p.lower()] += 1
    return cnt


def distinct_note_total(counts):
    """Total notes behind a SET of spellings, counted once per identity key.

    `counts` is {spelling: its participant_freq count}. participant_freq() keys on `p.lower()`, so
    two spellings that differ only in case ('Ann Example' / 'ann example', 'Pat McSample' /
    'Pat Mcsample') SHARE one counter and each reports the FULL count — summing per member then
    double-counts (one cluster reported total_notes 6 over 3 distinct notes). Collapsing
    to distinct lowercase keys first is the only correct way to aggregate that number over a name
    set; participant_freq() stays its single owner."""
    return sum({n.lower(): c for n, c in counts.items()}.values())


# ---- the two name spaces the registry-only dedup pass never sees ---------------------
# The dedup pass above only compares people-discovered.md rows and registry rows against the
# registry. Two much larger name spaces were invisible to it, so a variant that exists ONLY
# there was never surfaced (e.g. 'Ann Exampel' ~ 'Ann Example', obsidian [[Samuel Placeholder]] ~
# registry 'Sam Placeholder'):
#   (a) every `participants:` string in _manifest/index.jsonl   (~1.9k distinct vs ~370 rows)
#   (b) person-shaped [[wikilink]] targets in `source: obsidian` notes (~1.3k, ~135 registered)
# Both are harvested below and swept for variant clusters by the SAME `looks_like_variant`.

# a token that makes a `[[Foo Bar]]` target / participant string an org, a project, or a thing
# rather than a person. Only ever SUPPRESSES a question — it can never merge or edit anything.
_NON_PERSON_TOKENS = {
    "university", "universities", "college", "school", "institute", "hospital", "clinic",
    "center", "centre", "foundation", "associates", "association", "society", "capital",
    "partners", "ventures", "holdings", "llc", "inc", "ltd", "corp", "corporation", "company",
    "bank", "database", "dataset", "registry", "computing", "laboratory", "lab", "labs",
    "meeting", "meetings", "committee", "council", "board", "project", "projects", "program",
    "programs", "grant", "grants", "study", "studies", "trial", "trials", "cohort", "network",
    "team", "department", "division", "seminar", "rounds", "conference", "retreat",
    "fellowship", "award", "awards", "initiative", "consortium", "systems", "solutions",
    "technologies", "therapeutics", "pharmaceuticals", "biosciences", "media", "services",
    "trust", "fund", "funds", "docs", "task", "tasks", "place", "core", "group",
}
# lowercase surname particles that legitimately break the capitalised-token rule
_NAME_PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "dos", "du",
                   "la", "le", "el", "al", "bin", "ibn", "ter", "ten"}
_WIKILINK_RE = re.compile(r'\[\[([^\]|#\n]+)')


# Document/attachment suffixes: a wikilink target ending in one of these is a FILE, not a person.
_FILE_EXT_RE = re.compile(
    r'\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|md|png|jpe?g|gif|heic|zip|eml|ics)$', re.I)


def person_shaped(name):
    """Is this string plausibly a PERSON's name (vs an org / project / phone / handle)?

    Deliberately conservative — a rejected string simply never becomes a dedup question; nothing
    is ever merged or rewritten on the strength of this. Requires 2-4 capitalised alphabetic
    tokens, no digits/@/+, no org word, no bare acronym ('ABC Neurology', 'State University',
    'Example Foundation', '+15550143927' are all rejected; 'A.B. Example', 'Jean St. Sample',
    'José Ejemplo Muestra', 'Ida Moss-Sample', 'Patty (Patricia) Example' are accepted).
    Single-token strings ('Sarah', 'Val') are rejected: they cannot be resolved to a person and
    pair up into pure noise ('Ann' ~ 'Anne', 'Sam' ~ 'Sami')."""
    name = (name or "").strip()
    if not (2 < len(name) <= 60):
        return False
    if any(ch.isdigit() for ch in name) or "@" in name or "+" in name or "/" in name:
        return False
    # An ATTACHMENT is not a person. `[[Ann Example.pdf]]` is a link to a file that happens to be
    # named after someone, and it passed every other test here — so it paired with the real
    # `[[Ann Example]]` and shipped a dedup question whose only correct answer was "don't rewrite
    # this, you'll break the file pointer". Extension check only; a trailing initial ('Jean St. S')
    # and 'A.B. Example' must still pass, so this matches a known document suffix, not any dot.
    if _FILE_EXT_RE.search(name):
        return False
    toks = name.split()
    if not (2 <= len(toks) <= 4):
        return False
    for t in toks:
        core = "".join(ch for ch in t if ch.isalpha())
        if not core:
            return False
        low = core.lower()
        if low in _NON_PERSON_TOKENS:
            return False
        if len(core) >= 2 and core.isupper() and "." not in t:   # 'IBM', 'TNC' — but 'A.B.' is fine
            return False
        if not core[0].isupper() and low not in _NAME_PARTICLES:
            return False
    return True


def harvest_name_spaces():
    """Scan index.jsonl (+ the obsidian note bodies it points at) for the two extra name spaces.

    Returns (names, cooc, line_cooc):
      names     -> {exact-cased name: {"link_uses": int, "example": path|None, "spaces": set}}
                   `spaces` ⊆ {"participants", "obsidian-link"}; `example` is the lexicographically
                   first note the name was seen in (stable across runs -> stable queue output).
      cooc      -> Counter[(name_a, name_b)] (a < b) = # notes naming BOTH — as participants OR as
                   person-shaped [[wikilinks]] in one obsidian body. Two people in the same room
                   are almost never two spellings of one person, so this is a distinct-people TELL
                   — recorded as evidence + a priority penalty, never a filter.
      line_cooc -> {(a, b): {"notes": int, "example": path}} = the same tell on ONE LINE. Weighted
                   higher (see `SAME_LINE_PENALTY`) because a single comma-separated line naming
                   both is a hand-written roster, not two mentions that merely share a file.

    WHY the obsidian half exists (H3, 2026-08-04): `cooc` was built from `participants:` ALONE.
    A previous session widened variant DETECTION to obsidian [[wikilinks]] (they are harvested into
    `names` right below) without widening the TELL, so for a pair living only in wikilinks the
    safety check was GUARANTEED to read 0. That shipped a wrong merge: one cluster
    reported `cooccurring_pairs: 0` while an obsidian meeting note — a hand-written attendee list for
    one breakout — names `[[Ann Testcase]]` and `[[Anne Testcase]]` on the SAME LINE as
    separate attendees. Two distinct people were merged and the line ended up naming Ann twice.

    Participant NOTE COUNTS are not computed here: they come from `participant_freq()`, which
    stays the single owner of that number.
    """
    names, cooc, line_cooc = {}, collections.Counter(), {}
    if not os.path.exists("_manifest/index.jsonl"):
        return names, cooc, line_cooc

    def touch(n, space, path):
        e = names.setdefault(n, dict(link_uses=0, example=None, spaces=set()))
        e["spaces"].add(space)
        if path and (e["example"] is None or path < e["example"]):
            e["example"] = path
        return e

    def pairs(sorted_names):
        for i in range(len(sorted_names)):
            for j in range(i + 1, len(sorted_names)):
                yield (sorted_names[i], sorted_names[j])

    for line in open("_manifest/index.jsonl", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = r.get("path") or ""
        parts = sorted({p.strip() for p in (r.get("participants") or []) if p and p.strip()})
        for p in parts:
            touch(p, "participants", path)
        for pair in pairs(parts):              # co-occurrence within ONE note's roster
            cooc[pair] += 1
        if r.get("source") != "obsidian" or not path or not os.path.exists(path):
            continue
        try:
            body = open(path, encoding="utf-8").read()
        except OSError:
            continue
        linked = sorted({t for t in (m.strip() for m in _WIKILINK_RE.findall(body))
                         if person_shaped(t)})
        for target in linked:                  # per-NOTE, not per-occurrence (unchanged)
            touch(target, "obsidian-link", path)["link_uses"] += 1
        for pair in pairs(linked):             # ... and the same-note tell the sweep never had
            cooc[pair] += 1
        on_one_line = set()
        for raw in body.splitlines():
            hits = sorted({t for t in (m.strip() for m in _WIKILINK_RE.findall(raw))
                           if person_shaped(t)})
            if len(hits) > 1:
                on_one_line.update(pairs(hits))
        for pair in sorted(on_one_line):       # counted once per NOTE, like link_uses
            e = line_cooc.setdefault(pair, dict(notes=0, example=None))
            e["notes"] += 1
            if e["example"] is None or path < e["example"]:
                e["example"] = path
    return names, cooc, line_cooc


def _variant_candidates(names):
    """Every unordered pair of `names` that COULD satisfy looks_like_variant(), as (a, b) tuples.

    A blocking pass, NOT a second similarity function: the pairs it yields are a strict SUPERSET
    of what a brute-force O(n²) looks_like_variant() scan would accept, and every survivor is
    still decided by looks_like_variant() itself. Two blocks, mirroring that helper's two branches:
      * surname branch — exact grouping on the last token alone (NOT on the first initial too:
        `given_names_related` can accept differing initials, e.g. 'Kathy' ~ 'Cathy', and a
        surname group is small enough that the looser key costs nothing).
      * ratio branch  — difflib's own cheap upper bounds. real_quick_ratio() is 2*min(len)/sum(len)
        and quick_ratio() is the character-multiset bound; both are >= ratio(), so screening on
        them can never drop a true pair. Sorting by length turns the real_quick_ratio bound into
        an early `break` (lengths only grow), which is what keeps the ~3k-name sweep ~2s instead
        of ~30s.
    """
    norm = {n: n.lower().replace(".", "") for n in names}
    out = set()
    groups = collections.defaultdict(list)
    for n in names:
        toks = norm[n].split()
        if toks:
            groups[toks[-1]].append(n)
    for members in groups.values():
        members.sort()
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out.add((members[i], members[j]))
    order = sorted(names, key=lambda n: (len(norm[n]), n))
    sm = difflib.SequenceMatcher(None)
    for i, a in enumerate(order):
        la = len(norm[a])
        sm.set_seq2(norm[a])                       # set_seq2 caches; set_seq1 is the cheap side
        for b in order[i + 1:]:
            lb = len(norm[b])
            if 2 * la < VARIANT_RATIO * (la + lb):  # == real_quick_ratio() < VARIANT_RATIO
                break                               # sorted by length -> no later b can pass
            sm.set_seq1(norm[b])
            if sm.quick_ratio() >= VARIANT_RATIO:
                out.add((a, b) if a < b else (b, a))
    return out


# ---- cluster-then-promote ------------------------------------------------------------
# WHY (measured 2026-08-01, before this existed): of ~115 open people-dedup items, ~108 had NEITHER
# side in people.md (`evidence.canonical_in_registry == false`). Those are UNANSWERABLE — the
# write-back recipe (clarify_writeback.apply_people_dedup) adds an alias to an existing canonical
# ROW, so with no row it raises "no row for 'X'". Worse, the pairwise form explodes over a variant
# cluster: 'Sam Q. Specimen'(14 notes) / 'Sam Specimen'(9) / 'Sammy Specimen'(3) / 'S. Specimen'(1)
# produced SIX items, none of them applicable.
#
# So: group the SAME `looks_like_variant` relation into connected components, and ask a component
# with NO registered member ONCE ("which spelling is canonical?" -> promote it, alias the rest)
# instead of pairwise. A component WITH a registered member keeps the pairwise people-dedup path
# EXACTLY as it was: that path is already tuned and its answer (add an alias to an existing row) is
# directly applicable. No new similarity function is introduced — the graph's edges are precisely
# the pairs the pairwise path would have emitted.
# Priority. Every cluster item used to pin at a flat 0.6, so 39% of the queue carried no ranking
# at all and the final sort degenerated to alphabetical: a 30-note cluster ranked
# identically to a 1-note pair. The only ranking input was the co-occurrence penalty, which is
# structurally unreachable here (two spellings of ONE identity essentially never share a
# `participants:` roster — 0 of 91 shipped items carried it). So scale by VOLUME, the way
# people-promote / people-verify / the pairwise path already do. Band: 0.45 .. 0.70 (a saturated
# cluster ties the registry-anchored pairwise item rather than always sitting below it —
# deliberate: 35 notes of one misspelt person is worth more than one alias confirmation).
CLUSTER_PRIORITY_BASE = 0.45
CLUSTER_PRIORITY_SPAN = 0.25
CLUSTER_VOLUME_CAP = 12         # notes at which the volume bonus saturates (live p90 = 8, max 35)
CLUSTER_CHAINED_PENALTY = 0.1   # joined transitively -> likelier to be several people, ask later

# ONE owner for the co-occurrence demotion, shared by the cluster and the pairwise paths (they had
# two copies of 0.3 and would have drifted the moment the same-line tell was added to one of them).
COOCCUR_PENALTY = 0.3        # both names in ONE note (participants roster or an obsidian body)
SAME_LINE_PENALTY = 0.45     # ... on ONE hand-written line — a roster, so near-certainly 2 people
PRIORITY_FLOOR = 0.05        # a demoted item still ranks; it must never sort as "negative value"
CLUSTER_COOCCUR_PENALTY = COOCCUR_PENALTY     # back-compat alias for the old name


def cooccur_penalty(cooccurs, same_line):
    """The demotion for the distinct-people tells, in one place. Same-line SUPERSEDES same-note
    (it implies it), so the two never stack."""
    return SAME_LINE_PENALTY if same_line else (COOCCUR_PENALTY if cooccurs else 0.0)


def cluster_priority(total_notes, chained, cooccurs, same_line=False):
    """0.45..0.70 by note volume, demoted for the distinct-people tells. See the block above.

    `same_line` is keyword-defaulted on purpose: it was added after the M7 priority tests pinned
    the 3-argument form, and those pins are the ranking contract."""
    p = CLUSTER_PRIORITY_BASE + CLUSTER_PRIORITY_SPAN * min(1.0, total_notes / CLUSTER_VOLUME_CAP)
    if chained:
        p -= CLUSTER_CHAINED_PENALTY
    p -= cooccur_penalty(cooccurs, same_line)
    return round(max(PRIORITY_FLOOR, p), 3)


def email_tell(named_emails):
    """A one-line institution tell for [(name, [emails])] — '' when nothing is known.

    An email address is the strongest identity evidence the store holds, and NO dedup question
    carried it. That shipped a wrong merge (M5, 2026-08-04): `Ann Sample` is recorded in
    people-discovered.md with ann.sample@hospital.example — a real mailbox — while
    `Anne Q. Sample` is a colleague at another institution. With neither address on screen the pair read as
    an obvious typo and was merged. Surfaced as evidence + question text ONLY: an address can be
    personal-vs-work or stale, so it never filters and never changes a priority (unlike
    co-occurrence, which is an observation about ONE note and cannot be explained away).
    """
    have = [(n, sorted(e)) for n, e in named_emails if e]
    if not have:
        return ""
    bits = "; ".join("'%s' = %s" % (n, ", ".join(e)) for n, e in have)
    if len(have) < 2:
        return f" NOTE: known email — {bits} — nothing on file for the other spelling(s)."
    addrs = {a for _, e in have for a in e}
    if len(addrs) == 1:
        return f" NOTE: known emails — {bits} — the SAME address, a strong tell for ONE person."
    # Two spellings of ONE person share a mailbox; two mailboxes mean two people, whether or not
    # they sit at the same institution ('ann.example@' vs 'amy.example@hospital.example'
    # is as decisive as a two-institution split).
    doms = {a.rsplit("@", 1)[-1] for a in addrs}
    where = ("DIFFERENT domains (%s)" % ", ".join(sorted(doms))) if len(doms) > 1 \
        else "DIFFERENT mailboxes at the same domain"
    return (f" NOTE: known emails — {bits} — {where}; an address mismatch is as strong a tell for "
            f"DISTINCT people as co-occurrence.")


class _DSU:
    """Union-find over the name pool. The root of a component is its lexicographically smallest
    member, so component grouping (and therefore the queue) is byte-stable across runs."""

    __slots__ = ("parent",)

    def __init__(self, names):
        self.parent = {n: n for n in names}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:                       # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self.parent[hi] = lo

    def groups(self):
        """root -> sorted member list, for every component (singletons included)."""
        out = collections.defaultdict(list)
        for n in sorted(self.parent):
            out[self.find(n)].append(n)
        return out


def variant_graph(pool_names, candidates):
    """(dsu, edges) over `pool_names` — the IDENTITY-ASSERTING subset of the pairwise relation.

    An edge is a candidate pair that `looks_like_variant` accepts AND `cluster_edge` clears. The
    second condition is what keeps distinct same-first-name / same-surname people out of one
    cluster; see `cluster_edge` for the measured false positives it closes. A pair it rejects is
    NOT lost — the pairwise people-dedup path re-tests `looks_like_variant` itself and asks the
    neutral yes/no question instead.

    Two names whose `slug()` is identical ('Ann B. Placeholder' / 'Ann B Placeholder') are ALSO
    unioned but are not edges: `looks_like_variant` returns False for them (their dot-stripped
    forms are equal) and the pairwise path skips them as "same id both sides — nothing to ask",
    yet they are plainly one identity and must land in one cluster so a promote rewrites both
    spellings.
    """
    dsu = _DSU(pool_names)
    edges = []
    for a, b in candidates:
        if slug(a) == slug(b):
            dsu.union(a, b)
            continue
        if looks_like_variant(a, b) and cluster_edge(a, b):
            dsu.union(a, b)
            edges.append((a, b))
    return dsu, edges


def cluster_items(comps, roots, meta, cooc, line_cooc, edges, emails_of):
    """One `people-cluster` item per fully-unregistered component (`roots`, already filtered).

    Same envelope as every other item (id / category / priority / entity / question / candidates /
    evidence / writeback / blocked_on / status) so nothing downstream has to special-case it; the
    write-back recipe (clarify_writeback.apply_people_cluster) reads `evidence.members` (each with
    its note count) and `evidence.domain`.

    The `id` is derived from the component ROOT so that answering an item and then re-running the
    builder over a store with new notes cannot resurrect a dismissed question; `priority` scales
    with the component's distinct note volume.
    """
    edge_set = {frozenset(e) for e in edges}
    items = []
    for root in sorted(roots):
        members = comps[root]
        metas = {n: meta(n) for n in members}
        # rank = most-used spelling first (the SUGGESTED canonical), slug order to break ties.
        # Presentational only — it must never reach the `id`, see below.
        rank = sorted(members, key=lambda n: (-(metas[n]["notes"] + metas[n]["links"]), slug(n)))
        top = rank[0]
        total_notes = distinct_note_total({n: metas[n]["notes"] for n in members})

        co_pairs, line_pairs, direct = [], [], 0
        for i, x in enumerate(members):
            for y in members[i + 1:]:
                key = (min(x, y), max(x, y))
                c = cooc.get(key, 0)
                if c:
                    co_pairs.append(dict(a=x, b=y, notes=c))
                ln = line_cooc.get(key)
                if ln:
                    line_pairs.append(dict(a=x, b=y, notes=ln["notes"], example=ln["example"]))
                if frozenset((x, y)) in edge_set or slug(x) == slug(y):
                    direct += 1
        # a component that is not a clique was joined TRANSITIVELY (a~b, b~c, but a!~c) — the way a
        # chain of unrelated people gets welded into one group. Surfaced, never used to filter.
        chained = direct < len(members) * (len(members) - 1) // 2

        doms = collections.Counter()
        for n in members:
            ex = metas[n]["example"] or ""
            d = ex.split("/")[0]
            if d in TOPS:
                doms[d] += 1
        domain = doms.most_common(1)[0][0] if doms else ""

        def plural(k, word):
            return f"{k} {word}" if k == 1 else f"{k} {word}s"

        def describe(n):
            m = metas[n]
            bits = plural(m["notes"], "note")
            if m["links"]:
                bits += ", " + plural(m["links"], "obsidian link")
            if emails_of(n):
                bits += ", " + ", ".join(emails_of(n))
            return f"'{n}' ({bits})"

        q = (f"{len(members)} spellings look like ONE person and NONE of them is in people.md: "
             + ", ".join(describe(n) for n in rank)
             + ". Which spelling is canonical? (it gets promoted into people.md with verify:true, "
               "the others become its aliases, and every note's participants:/[[wikilinks]] are "
               "rewritten to it.) Or are these NOT one person?")
        if co_pairs:
            q += (" NOTE: " + "; ".join(f"'{p['a']}' + '{p['b']}' appear together in the SAME note "
                                        f"{p['notes']}x" for p in co_pairs)
                  + " — usually the tell for DISTINCT people.")
        if line_pairs:
            q += (" NOTE: " + "; ".join(f"'{p['a']}' + '{p['b']}' are named on the SAME LINE of "
                                        f"{p['example']}" for p in line_pairs)
                  + " — a hand-written roster listing both is near-certainly DISTINCT people.")
        q += email_tell([(n, emails_of(n)) for n in rank])
        if chained:
            q += (" NOTE: this group was joined transitively (not every pair looks alike directly) —"
                  " check whether it is really one person before choosing.")

        items.append(dict(
            # id from the _DSU ROOT (the component's lexicographically smallest member), NOT from
            # the count-ranked `top`. `top` moves whenever the note counts move — measured, 68 of
            # 91 shipped cluster items flipped their top on a swing of <= 2 notes — and a flipped
            # id makes build()'s preserve-by-id loop miss the prior entry, silently DISCARDING a
            # previous "not one person — skip" exactly for the false positives that most need to
            # stay dismissed. The root is count-independent and stable for as long as the
            # component's membership is. `top` is kept as evidence.suggested_canonical.
            id=f"cluster:{slug(root)}", category="people-cluster",
            priority=cluster_priority(total_notes, chained, bool(co_pairs), bool(line_pairs)),
            entity=top,
            question=q,
            candidates=[f"canonical: {n} ({plural(metas[n]['notes'], 'note')})" for n in rank]
                       + ["not one person — skip"],
            evidence=dict(
                members=[dict(name=n, notes=metas[n]["notes"], links=metas[n]["links"],
                              spaces=metas[n]["spaces"], example=metas[n]["example"],
                              in_registry=metas[n]["in_registry"], emails=emails_of(n))
                         for n in rank],
                suggested_canonical=top, cluster_size=len(members), domain=domain,
                total_notes=total_notes,
                variant_links=[[x, y] for x, y in edges if x in metas and y in metas],
                chained=chained, cooccurrences=co_pairs, cooccurring_pairs=len(co_pairs),
                same_line_cooccurrences=line_pairs, same_line_pairs=len(line_pairs),
                found_by="cluster-then-promote"),
            writeback=dict(file="_manifest/people.md", op="promote-cluster",
                           note=("promote the chosen spelling as a new verify:true row, record the "
                                 "other %d as its aliases, and rewrite participants:/[[wikilinks]] "
                                 "in every affected note" % (len(members) - 1))),
            blocked_on=None, status="open"))
    return items


def sweep_dedup_items(canon, alias_to_canon, pfreq, already_paired, emails_by_name):
    """(items, promote_needed) for the participant / obsidian-wikilink name spaces (+ the registry
    names, so a variant can pair with a person who has no notes at all): people-dedup items for the
    variant pairs, plus people-cluster items for the fully-unregistered components (`cluster_items`).

    `already_paired` is the set of frozenset({slug_a, slug_b}) pairs the registry-only passes
    already asked about — an unordered key, so a pair is never asked twice in either order.
    Emits the SAME item shape as those passes (evidence.name / evidence.canonical are what
    clarify_writeback.apply_people_dedup reads), so /clarify consumes them unchanged.

    `promote_needed` is {canonical name -> (priority, domain, notes)} for every emitted pair whose
    CANONICAL side has no people.md row at all — see the ANSWERABILITY block below. build() turns
    each into the people-promote item that unblocks it.
    """
    spaces, cooc, line_cooc = harvest_name_spaces()
    pool = {n: m for n, m in spaces.items() if person_shaped(n)}
    for n in canon:                       # registry rows join the pool with 0 observed uses
        if person_shaped(n):
            pool.setdefault(n, dict(link_uses=0, example=None, spaces=set()))["spaces"].add("registry")
    # a slug-keyed view of the registry (names AND aliases) so punctuation/casing variants of an
    # ALREADY-registered alias ("Ann B. Placeholder" vs "Ann B Placeholder") resolve together.
    slug_to_canon = {}
    for a, c in sorted(alias_to_canon.items()):
        slug_to_canon.setdefault(slug(a), c)

    def registered(n):
        return slug_to_canon.get(slug(n))

    def meta(n):
        m = pool.get(n) or dict(link_uses=0, example=None, spaces=set())
        return dict(notes=pfreq.get(n.lower(), 0), links=m["link_uses"],
                    example=m["example"], spaces=sorted(m["spaces"]),
                    in_registry=registered(n) is not None)

    emails_of = email_lookup(emails_by_name, registered)

    # --- the variant graph, walked ONCE and shared by both paths -----------------------
    candidates = sorted(_variant_candidates(list(pool)))
    dsu, edges = variant_graph(list(pool), candidates)
    comps = dsu.groups()
    # a multi-member component with NO registered member (canonical name OR alias) is unanswerable
    # pairwise -> it becomes ONE people-cluster item and its internal pairs are suppressed below.
    cluster_roots = [root for root, members in comps.items()
                     if len(members) > 1 and not any(registered(n) for n in members)]
    clustered = {n: root for root in cluster_roots for n in comps[root]}
    component_anchored = {root: any(registered(n) for n in members)
                          for root, members in comps.items()}

    items, promote_needed = [], {}
    for a, b in candidates:
        if slug(a) == slug(b):                       # same id both sides — nothing to ask
            continue
        if not looks_like_variant(a, b):
            continue
        # both sides inside ONE cluster item -> asked there, not pairwise. It must be the SAME
        # component: cluster edges are a strict SUBSET of `looks_like_variant` (see cluster_edge),
        # so a pair can now straddle two components ('A. Specimen' ~ 'Sam Specimen') or join a
        # clustered name to a singleton ('Maximilian Ames' ~ 'Maximilian Amos'). Those are exactly the
        # pairs that must fall through to the neutral pairwise question — an `a in clustered`
        # test would swallow them and the queue would lose the question altogether.
        if clustered.get(a) is not None and clustered.get(a) == clustered.get(b):
            continue
        ca, cb = registered(a), registered(b)
        if ca and cb and ca == cb:                   # already one registered identity
            continue
        ma, mb = meta(a), meta(b)
        # Canonical side. A REGISTERED name always wins (its row is what an alias attaches to), and
        # among two registered ones the more-used. But when NEITHER side is registered there is no
        # row either way, so the counts carry no meaning — and letting them order the pair made the
        # `dedup:<variant>~<canonical>` id count-dependent: one new note flipped it, build()'s
        # preserve-by-id loop then missed the prior entry and silently dropped an answered status
        # (two such items were in the shipped queue). Order by slug alone there: count-independent.
        # Ties break toward the SMALLER slug — the direction `cluster_items`' rank uses — so the
        # two paths can never name opposite canonicals for the same pair.
        anchored = ma["in_registry"] or mb["in_registry"]

        def rank_key(t, _anchored=anchored):
            name, m = t
            if not _anchored:
                return (0, 0, slug(name))
            return (not m["in_registry"], -(m["notes"] + m["links"]), slug(name))

        (canonical, mc), (variant, mv) = sorted(((a, ma), (b, mb)), key=rank_key)
        # ANSWERABILITY, part 1 — `apply_people_dedup` adds the variant as an alias OF A ROW, and
        # PeopleDoc.find_row matches the NAME cell only. So an `evidence.canonical` that is a
        # registered ALIAS STRING ("Ann Example Sample", an alias of the row "Ann Example") raised
        # "no row for 'X'" and the item could never be applied. Resolve the canonical side through
        # the alias map to the row that actually exists. The VARIANT side is deliberately NOT
        # resolved: it is the literal string `plan_people_rename` rewrites in participants: and
        # [[wikilinks]], so replacing it with a row name would leave the misspelling in the notes.
        seen_as = canonical
        if mc["in_registry"]:
            canonical = registered(canonical)
            mc = dict(mc, in_registry=True)
        if slug(variant) == slug(canonical):     # the variant IS that row — nothing left to ask
            continue
        # keyed on the RESOLVED pair, so two aliases of one row cannot produce two items with the
        # same `dedup:<variant>~<row>` id (which would break preserve-by-id).
        key = frozenset((slug(variant), slug(canonical)))
        if key in already_paired:
            continue
        already_paired.add(key)
        pair_key = (min(a, b), max(a, b))
        co = cooc.get(pair_key, 0)
        line = line_cooc.get(pair_key)
        # a pair one of whose sides is a registered person is directly actionable (add an alias);
        # a pair of two unregistered strings needs a promote first, so it ranks lower. Sharing one
        # note is the distinct-people tell, and sharing one LINE is the stronger form of it ->
        # demoted, never dropped.
        priority = round(max(PRIORITY_FLOOR, (0.7 if mc["in_registry"] else 0.55)
                             - cooccur_penalty(co, line)), 3)

        def where(m):
            return "+".join(m["spaces"]) or "registry"

        q = (f"Is '{variant}' ({mv['notes']} notes, {mv['links']} obsidian links, seen in "
             f"{where(mv)}) the same person as '{canonical}' ({mc['notes']} notes, "
             f"{mc['links']} obsidian links, {'in' if mc['in_registry'] else 'NOT in'} people.md)?")
        if seen_as != canonical:
            q += f" ('{canonical}' is the people.md row; '{seen_as}' is one of its aliases.)"
        if co:
            q += (f" NOTE: they appear together in the SAME note {co}x — "
                  f"usually the tell for two DISTINCT people.")
        if line:
            q += (f" NOTE: they are named on the SAME LINE of {line['example']}"
                  + (f" ({line['notes']} notes)" if line["notes"] > 1 else "")
                  + " — a hand-written roster listing both is near-certainly two DISTINCT people.")
        q += email_tell([(variant, emails_of(variant)), (canonical, emails_of(canonical))])
        # ANSWERABILITY, part 2 — a canonical with NO row anywhere still cannot be aliased onto.
        # It is NOT turned into a people-cluster item: the cluster question ASSERTS "these N
        # spellings are ONE person", and every such pair is one `cluster_edge` REJECTED (that is
        # precisely why it is here and not inside a cluster) — the welds those regression tests
        # exist to keep out. Instead the prerequisite is emitted as its own people-promote item,
        # which asserts nothing and IS applicable; answering it makes this pair answerable on the
        # next regen (and the id is stable across that, since `canonical` is the smaller slug
        # either way). Skipped when the component already has a registered member: the anchored
        # pairs in that component are the right instrument there.
        if not mc["in_registry"] and not component_anchored[dsu.find(canonical)]:
            prev = promote_needed.get(canonical)
            bump = round(min(0.95, priority + 0.01), 3)
            if prev is None or bump > prev[0]:
                dom = (mc["example"] or "").split("/")[0]
                promote_needed[canonical] = (bump, dom if dom in TOPS else "", mc["notes"])
        items.append(dict(
            id=f"dedup:{slug(variant)}~{slug(canonical)}", category="people-dedup",
            priority=priority, entity=canonical,
            question=q,
            candidates=[f"yes — add '{variant}' as an alias of {canonical}"
                        if mc["in_registry"] else
                        f"yes — same person (promote '{canonical}' first: answer "
                        f"promote:{slug(canonical)}, then this item aliases '{variant}' onto it)",
                        "no — distinct people (leave both as they are)"],
            evidence=dict(name=variant, canonical=canonical,
                          notes=mv["notes"], canonical_notes=mc["notes"],
                          links=mv["links"], canonical_links=mc["links"],
                          in_registry=mv["in_registry"], canonical_in_registry=mc["in_registry"],
                          spaces=mv["spaces"], canonical_spaces=mc["spaces"],
                          example=mv["example"], canonical_example=mc["example"],
                          emails=emails_of(variant), canonical_emails=emails_of(canonical),
                          canonical_seen_as=seen_as,
                          blocked_by=(None if mc["in_registry"]
                                      else f"promote:{slug(canonical)}"),
                          cooccurs_in_notes=co,
                          cooccurs_same_line=(line or {}).get("notes", 0),
                          cooccurs_same_line_example=(line or {}).get("example"),
                          found_by="name-space-sweep"),
            writeback=dict(file="_manifest/people.md",
                           op="add-alias OR promote-as-new",
                           note=("if alias: add to %s row, update inbound participants/[[wikilinks]]"
                                 % canonical) if mc["in_registry"] else
                                ("'%s' has no people.md row yet — promote it first, then add '%s' "
                                 "as its alias" % (canonical, variant))),
            # deliberately NOT `blocked_on`: /clarify PARKS a blocked item, and "no — distinct
            # people" is applicable with or without the row. The prerequisite is carried in
            # evidence.blocked_by + the candidate text, and its promote item is ranked one notch
            # ABOVE this one, so a priority-ordered interview reaches it first anyway.
            blocked_on=None, status="open"))
    items.extend(cluster_items(comps, cluster_roots, meta, cooc, line_cooc, edges, emails_of))
    return items, promote_needed


def email_lookup(emails_by_name, resolve):
    """name -> its known addresses, sorted; falling back to the people.md ROW it is an alias of.

    ONE owner, shared by build()'s two registry passes and the name-space sweep — they resolve a
    name to its row differently (`alias_to_canon` vs the sweep's slug-keyed `registered`), which is
    the only thing `resolve` abstracts."""
    def of(name):
        own = emails_by_name.get(name.lower())
        if own:
            return sorted(own)
        row = resolve(name)
        return sorted(emails_by_name.get(row.lower(), ())) if row else []
    return of


def promote_item(name, freq, domain, priority, question_suffix="", extra_evidence=None):
    """The ONE people-promote item shape. Emitted from two places — the people-discovered.md pass
    and (as the prerequisite of an unanswerable dedup pair) the name-space sweep — so it lives in
    one function rather than being pasted twice."""
    ev = dict(name=name, meetings=freq, domain=domain)
    ev.update(extra_evidence or {})
    return dict(
        id=f"promote:{slug(name)}", category="people-promote",
        priority=priority, entity=name,
        question=f"'{name}' appears in {freq} notes (top domain {domain}) "
                 f"but isn't in people.md. Real person to add? Canonical name + role?"
                 + question_suffix,
        candidates=["add — give canonical name + relationship/role",
                    "alias of someone already listed", "noise / skip"],
        evidence=ev,
        writeback=dict(file="_manifest/people.md", op="promote",
                       note="add row to the right domain table; drop verify flag"),
        blocked_on=None, status="open")


def load_manual_items():
    """Read the hand-curated sidecar (`_manifest/clarify-manual.jsonl`) and return its `open`
    entries in the same record shape `build()` emits (writeback/blocked_on defaulted when absent).

    `build()` fully regenerates QUEUE every run, which wipes any question hand-added straight into
    the queue file -- this sidecar is the committed, id-stable place for a question no harvester
    produces (e.g. a `dossier-conflict`: two ingested notes disagree with each other or with a
    dossier, and nothing short of asking the owner resolves it). It is COMMITTED (unlike the generated
    queue's transient regen) and merged in on every build, so it survives `check.sh`.

    `resolved` entries are skipped -- once the owner's answer is applied by hand (edit the dossier via
    `scripts/reconcile_dossier.py`, then flip the sidecar entry to resolved, per the clarify
    SKILL), the question stops appearing; the resolution itself lives only in the sidecar (git
    history), never copied into the regenerated queue.

    Fails LOUDLY (raises ValueError) on a malformed line, a missing required field, a bad `status`,
    or a duplicate `id` within the sidecar itself -- a hand-edited file with a data-entry mistake
    should stop the build, never be silently dropped or blank-filled."""
    items = []
    if not os.path.exists(MANUAL):
        return items
    seen_ids = set()
    with open(MANUAL, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{MANUAL}:{lineno}: invalid JSON ({e})") from e
            if not isinstance(rec, dict):
                raise ValueError(f"{MANUAL}:{lineno}: expected a JSON object, "
                                 f"got {type(rec).__name__}")
            missing = _MANUAL_REQUIRED - rec.keys()
            if missing:
                raise ValueError(f"{MANUAL}:{lineno}: missing required field(s) "
                                 f"{sorted(missing)}")
            if rec["status"] not in ("open", "resolved"):
                raise ValueError(f"{MANUAL}:{lineno}: status must be 'open' or 'resolved', "
                                 f"got {rec['status']!r}")
            if rec["id"] in seen_ids:
                raise ValueError(f"{MANUAL}:{lineno}: duplicate id '{rec['id']}' within {MANUAL}")
            seen_ids.add(rec["id"])
            ev = rec.get("evidence")
            if not isinstance(ev, dict) or not isinstance(ev.get("dossier"), str) \
                    or not isinstance(ev.get("notes"), list) \
                    or not all(isinstance(n, str) for n in ev.get("notes", [])):
                raise ValueError(
                    f"{MANUAL}:{lineno}: evidence must be an object with a single string "
                    f"'dossier' path and a list of string 'notes' paths (got "
                    f"{type(ev).__name__ if not isinstance(ev, dict) else 'dossier=' + repr(ev.get('dossier'))})")
            if rec["status"] != "open":
                continue
            item = dict(rec)
            item.setdefault("writeback", dict(
                file=(rec.get("evidence") or {}).get("dossier"),
                op="manual-dossier-conflict",
                note="apply by hand: edit the dossier via scripts/reconcile_dossier.py citing "
                     "the owner's verbatim answer, then flip this sidecar entry to status:resolved "
                     "(see .claude/skills/clarify/SKILL.md) -- no automated recipe writes this"))
            item.setdefault("blocked_on", None)
            items.append(item)
    return items


def build():
    canon, alias_to_canon = parse_people()
    for name, sections in people_row_collisions(canon):
        print("WARN duplicate people.md row: '%s' appears in %d sections (%s) with no cross-"
              "reference between them — merged here (roles joined; verify only if EVERY row is "
              "unconfirmed), but people.md should hold ONE row per canonical name."
              % (name, len(sections), "; ".join(sections)), file=sys.stderr)
    discovered = parse_discovered()
    pfreq = participant_freq()
    flags = scan_flagged()
    for k, v in parse_review_report().items():      # merge prose-recorded flags, dedup paths
        seen = {p for p, _ in flags[k]}
        flags[k].extend((p, t) for p, t in v if p not in seen)
    for d in discovered:   # effective freq: explicit meeting count (Granola) else live note-participant count
        # `.lower()`: participant_freq() is lowercase-keyed. This site used the raw cased name, so
        # every count-less discovered row (the whole Outlook harvest) scored 0 and never cleared
        # the `freq >= 2` promote gate — see participant_freq's docstring.
        d["freq"] = d["meetings"] if d["meetings"] is not None else pfreq.get(d["name"].lower(), 0)
    # ... and collapse rows for the SAME person: people-discovered.md holds one table per harvest,
    # so a name can appear in both the Granola table (with a count) and the Outlook one (without).
    # Two rows emit two items with the same `promote:<slug>` id, which breaks preserve-by-id
    # (last line wins) and asks the same question twice. Unreachable while the count-less rows
    # all scored 0 — fixing that casing bug is what exposed it. Insertion order = stable output.
    merged = {}
    for d in discovered:
        cur = merged.get(d["name"])
        if cur is None:
            merged[d["name"]] = dict(d)
            continue
        cur["freq"] = max(cur["freq"], d["freq"])
        cur["meetings"] = cur["meetings"] if cur["meetings"] is not None else d["meetings"]
        cur["domain"] = cur["domain"] or d["domain"]
        cur["email"] = cur["email"] or d["email"]
    discovered = list(merged.values())
    # name(LOWERCASED) -> known addresses, from BOTH registries. `emails_of` in the sweep resolves
    # an unregistered spelling through this map, else through the row it is an alias of.
    emails_by_name = collections.defaultdict(set)
    for n, m in canon.items():
        emails_by_name[n.lower()] |= {e.lower() for e in m.get("emails", ())}
    for d in discovered:
        if d["email"]:
            emails_by_name[d["name"].lower()].add(d["email"].lower())
    emails_of = email_lookup(emails_by_name, lambda n: alias_to_canon.get(n.lower()))
    maxf = max((d["freq"] for d in discovered), default=1) or 1
    items = []

    def impact(freq):
        return round(0.3 + 0.7 * freq / maxf, 3)   # 0.3..1.0 by meeting frequency

    # 1. people: dedup + promote -------------------------------------------------------
    canon_names = list(canon)
    for d in discovered:
        name, freq = d["name"], d["freq"]
        if name.lower() in alias_to_canon:           # already canonical or a known alias
            continue
        match = next((c for c in canon_names if looks_like_variant(name, c)), None)
        if match:
            items.append(dict(
                # flat-high: a duplicate corrupts participant rosters / retrieval and is
                # cheap to confirm — its value doesn't scale with the variant's (low) freq.
                id=f"dedup:{slug(name)}~{slug(match)}", category="people-dedup",
                priority=0.8, entity=match,
                question=f"Is '{name}' ({freq} notes, top domain {d['domain']}) the same "
                         f"person as the canonical '{match}'?"
                         + email_tell([(name, emails_of(name)), (match, emails_of(match))]),
                candidates=[f"yes — add '{name}' as an alias of {match}",
                            "no — distinct person (promote separately)"],
                evidence=dict(name=name, meetings=freq, domain=d["domain"], canonical=match,
                              emails=emails_of(name), canonical_emails=emails_of(match)),
                writeback=dict(file="_manifest/people.md",
                               op="add-alias OR promote-as-new",
                               note="if alias: add to %s row, update inbound participants/[[wikilinks]]" % match),
                blocked_on=None, status="open"))
        elif freq >= 2:
            items.append(promote_item(name, freq, d["domain"], round(0.5 * impact(freq), 3)))

    # 1b. people: verify auto-added registry rows (verify:true) -------------------------
    # Rows added with Verify?=yes are in people.md but UNCONFIRMED. Surface each for a human to
    # confirm/correct (a person looking like a variant of a CONFIRMED name -> dedup question).
    # `or 1` guards the freq/vmax division below: `default=1` covers an EMPTY verify pool, but a pool
    # whose only members have 0 notes (e.g. a deferred name-only handle) makes max()==0 → ZeroDivision.
    vmax = max((pfreq[n.lower()] + sum(pfreq[a.lower()] for a in m["aliases"])
                for n, m in canon.items() if m.get("verify")), default=1) or 1
    for name, meta in canon.items():
        if not meta.get("verify"):
            continue
        freq = pfreq[name.lower()] + sum(pfreq[a.lower()] for a in meta["aliases"])
        # is this unconfirmed row a likely variant of an already-CONFIRMED canonical person?
        match = next((c for c, m in canon.items()
                      if not m.get("verify") and c != name and looks_like_variant(name, c)), None)
        if match:
            items.append(dict(
                id=f"dedup:{slug(name)}~{slug(match)}", category="people-dedup",
                priority=0.8, entity=match,
                question=f"Is the auto-added '{name}' ({freq} notes; role '{meta['role']}') the "
                         f"same person as the confirmed '{match}'?"
                         + email_tell([(name, emails_of(name)), (match, emails_of(match))]),
                candidates=[f"yes — merge '{name}' into {match} as an alias",
                            "no — distinct person (keep + confirm separately)"],
                evidence=dict(name=name, notes=freq, role=meta["role"],
                              section=meta["section"], canonical=match,
                              emails=emails_of(name), canonical_emails=emails_of(match)),
                writeback=dict(file="_manifest/people.md", op="merge-or-keep",
                               note="if merge: fold row into %s, update inbound participants/[[wikilinks]]; "
                                    "else drop the verify flag" % match),
                blocked_on=None, status="open"))
        else:
            items.append(dict(
                id=f"verify:{slug(name)}", category="people-verify",
                priority=round(0.35 + 0.35 * (freq / vmax), 3), entity=name,
                question=f"'{name}' was auto-added to the registry from {freq} notes "
                         f"(role hint: '{meta['role']}', section '{meta['section']}') but is "
                         f"UNCONFIRMED. Is the identity + role right?",
                candidates=["confirm — identity + role correct (drop verify flag)",
                            "correct the name and/or role", "not a real/distinct person — merge or remove"],
                evidence=dict(name=name, notes=freq, role=meta["role"], section=meta["section"]),
                writeback=dict(file="_manifest/people.md", op="confirm-correct-or-merge",
                               note="on confirm/correct, change the Verify? cell from 'yes' to '—'"),
                blocked_on=None, status="open"))

    # 1c. people: dedup sweep over the participant + obsidian-wikilink name spaces ------
    # Additive: 1 and 1b keep emitting exactly what they always did; this only asks about pairs
    # they structurally cannot see. `already_paired` is seeded from what they emitted so the same
    # unordered pair is never asked twice (their ids are `dedup:<variant>~<canonical>`).
    already_paired = {frozenset(it["id"].split(":", 1)[1].split("~"))
                      for it in items if it["category"] == "people-dedup"}
    sweep_items, promote_needed = sweep_dedup_items(canon, alias_to_canon, pfreq,
                                                    already_paired, emails_by_name)
    items.extend(sweep_items)
    # ... and the promote prerequisites those pairs need to become applicable at all (see the
    # ANSWERABILITY block in sweep_dedup_items). Ranked one notch above the pair that needs it, so
    # a priority-ordered interview promotes the row BEFORE being asked to alias onto it. An `id`
    # already emitted by pass 1 wins — that item carries the discovered-row meeting count.
    have = {it["id"] for it in items}
    for name in sorted(promote_needed):
        priority, domain, notes = promote_needed[name]
        if f"promote:{slug(name)}" in have:
            continue
        items.append(promote_item(
            name, notes, domain, priority,
            question_suffix=" (needed before any 'yes' on the dedup question(s) below: an alias "
                            "can only attach to an existing row.)",
            extra_evidence=dict(found_by="dedup-prerequisite")))

    # 2. people: tentative roles -------------------------------------------------------
    for name, meta in canon.items():
        if not meta["tentative"] or meta.get("verify"):   # verify rows handled in 1b
            continue
        # ANY of the person's sections, not just the first: a person listed under two sections is
        # still subject to a blocker on EITHER of them, and `section` reports only the first now
        # that duplicate rows are merged instead of last-wins.
        blocked = next((b for pre, b in BLOCKED_ROLE_SECTIONS
                        for s in meta.get("sections", [meta["section"]])
                        if s.lower().startswith(pre)), None)
        items.append(dict(
            id=f"role:{slug(name)}", category="people-role",
            priority=0.3 if blocked else 0.6, entity=name,
            question=f"What is {name}'s precise role? (current tentative hint: "
                     f"'{meta['role']}', section '{meta['section']}')",
            candidates=["confirm the tentative hint", "give the correct role"],
            evidence=dict(name=name, section=meta["section"], hint=meta["role"]),
            writeback=dict(file="_manifest/people.md", op="set-role",
                           note="replace tentative role; drop the tentative marker"),
            blocked_on=blocked, status="open"))

    # 3. domain confirmation -----------------------------------------------------------
    domain_ids = set()
    for flag in ("needs-domain-confirm", "domain-review"):
        for fp, title in flags.get(flag, []):
            iid = f"domain:{slug(os.path.basename(fp))}"
            if iid in domain_ids:
                continue                     # already asked (a note carrying BOTH flags)
            domain_ids.add(iid)
            items.append(dict(
                id=iid, category="domain",
                priority=0.4, entity=fp,
                question=f"'{title}' ({fp.split('/')[0]}) is flagged {flag}. Is the domain right?",
                candidates=["domain is correct — clear flag", "move to another domain"],
                evidence=dict(path=fp, title=title, current_domain=fp.split("/")[0], flag=flag),
                writeback=dict(file=fp, op="confirm-or-move-domain",
                               note="if move: relocate file + update inbound [[wikilinks]] same step"),
                blocked_on=None, status="open"))

    # 3b. notes parked in unsorted/ ------------------------------------------------------
    # A note in `unsorted/` IS a note awaiting a domain decision — that is CLAUDE.md's own
    # convention ("Ambiguous source/domain -> put it in `unsorted/` and tag `flagged`"). The queue
    # only ever asked about the two explicit flags above, which nothing in the store carries: ZERO
    # domain items existed while 27 notes sat in unsorted/ tagged `flagged`/`unsorted`/`empty-source`
    # instead. The recipe had nothing to apply itself to.
    #
    # The question carries the title, source, tags and the note's opening prose, so a reviewer can pick a
    # domain from the queue without opening the file — and `candidates` lists the real domains
    # (from the single owner, `_lib.constants.DOMAINS`) in the exact phrasing
    # clarify_writeback._domain_decision parses. Ids are shared with the flag-driven items above
    # (same `domain:<slug>` shape), so a note that is BOTH flagged and unsorted is asked once and
    # keeps its answered/deferred status across regens.
    for fp in sorted(f for f in glob.glob("unsorted/**/*.md", recursive=True)
                     if os.path.basename(f) != "_index.md"):
        iid = f"domain:{slug(os.path.basename(fp))}"
        if iid in domain_ids:
            continue
        domain_ids.add(iid)
        meta = note_brief(fp)
        # An empty-bodied obsidian person stub has no excerpt; its `related:` links are then the
        # only domain signal there is, so fall back to them rather than asking a blind question.
        detail = meta["excerpt"] or (f"no body; links to {meta['related']}"
                                     if meta["related"] not in ("", "[]") else "")
        items.append(dict(
            id=iid, category="domain",
            priority=0.4, entity=fp,
            question=f"'{meta['title']}' is parked in unsorted/ (source: {meta['source'] or '?'}"
                     f"{', tags: ' + meta['tags'] if meta['tags'] else ''}). Which domain does it "
                     f"belong to?" + (f" — {detail}" if detail else ""),
            candidates=[f"move to {d}" for d in TOPS if d != "unsorted"]
                       + ["domain is correct — leave it in unsorted/"],
            evidence=dict(path=fp, title=meta["title"], current_domain="unsorted",
                          source=meta["source"], tags=meta["tags"], excerpt=meta["excerpt"],
                          participants=meta["participants"], related=meta["related"],
                          flag=None, found_by="unsorted-dir"),
            writeback=dict(file=fp, op="confirm-or-move-domain",
                           note="if move: relocate file + re-stamp domain: + record the frozen-field "
                                "transition in _manifest/frontmatter-overrides.jsonl"),
            blocked_on=None, status="open"))

    # 4. thin-source notes -------------------------------------------------------------
    for flag in ("empty-source", "source-limited"):
        for fp, title in flags.get(flag, []):
            items.append(dict(
                id=f"thin:{slug(os.path.basename(fp))}", category="thin-source",
                priority=0.25, entity=fp,
                question=f"'{title}' is `{flag}` (little/no source content). Keep, enrich, or archive?",
                candidates=["keep as a stub", "enrich (you provide detail)", "archive"],
                evidence=dict(path=fp, title=title, flag=flag),
                writeback=dict(file=fp, op="keep-enrich-or-archive", note="never delete; archive moves to _archive/"),
                blocked_on=None, status="open"))

    # 5. hand-curated sidecar (dossier-conflict, etc.) ---------------------------------
    manual_items = load_manual_items()
    items.extend(manual_items)

    # ---- preserve prior non-open statuses (resumable) --------------------------------
    prior = {}
    if os.path.exists(QUEUE):
        for line in open(QUEUE, encoding="utf-8"):
            line = line.strip()
            if line:
                r = json.loads(line)
                prior[r["id"]] = r
    for it in items:
        old = prior.get(it["id"])
        if old and old.get("status") != "open":
            it["status"] = old["status"]
            if "answer" in old:
                it["answer"] = old["answer"]

    items.sort(key=lambda x: (-x["priority"], x["category"], x["id"]))
    with open(QUEUE, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return items


if __name__ == "__main__":
    items = build()
    openn = [i for i in items if i["status"] == "open"]
    manual_open = len([i for i in load_manual_items()])   # already open-only; re-parse for the count
    print(f"clarify queue: {len(items)} items ({len(openn)} open, {manual_open} from "
          f"{MANUAL} manual) -> {QUEUE}")
    if "--summary" in sys.argv:
        by = collections.Counter(i["category"] for i in openn)
        for cat, n in by.most_common():
            print(f"  {cat:18} {n}")
