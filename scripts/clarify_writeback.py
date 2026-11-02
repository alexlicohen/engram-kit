#!/usr/bin/env python3
"""Deterministic write-back engine for the `/clarify` queue (Feature E, first half — plan
generic-bubbling-eagle.md). Ports the six write-back recipes that live today ONLY as prose in
`.claude/skills/clarify/SKILL.md` into importable, tested code, usable both as a CLI and as a
library function (a future review_server.py imports `apply`).

    python3 scripts/clarify_writeback.py --id <id> --answer "<text>" [--dry-run] [--root <path>]

Scope: this module owns "apply an answer to a clarify-queue item". It does NOT construct the queue
(that stays `build_clarify_queue.py`'s job) — it records the answer by setting the item's
`status` + `answer` IN PLACE, exactly as the conversational skill does by hand, so the NEXT
`build_clarify_queue.py` regen carries it forward via its existing preserve-by-id logic. It appends
one audit line to `_manifest/clarify-log.jsonl` per apply.

Guarded-writer discipline (mirrors scripts/engram_capture.py): every recipe assembles the full set
of edits in memory FIRST, then a transaction applies them, runs `lint_store.py` AND the frozen-field
gate (`verify_frontmatter_preserved.check_path`, under the profile check.sh would apply), and ROLLS
BACK every touched file (byte-for-byte, moves reversed, created dirs removed) if either fails — so a
rejected apply never leaves a half-written store. Rollback is attempted for EVERY undo closure even
if one raises, and is triggered by BaseException, so a Ctrl-C inside lint still unwinds.
`--dry-run` prints the full list of files that would change, plus the queue rewrite and any ledger
appends, and touches nothing. It is a convention for multi-file edits, not a machine-enforced rule —
there is deliberately no `is_multifile` gate; the safety property is the transaction, not the report.

Single-owner reuse: DOMAINS from _lib.constants (the store's canonical domain list). The people.md
table editor (PeopleDoc), the in-place frontmatter/body edits, and the participants/`[[wikilink]]`
propagation are genuinely new here (no prior owner — the behavior only existed as prose in SKILL.md;
_lib.frontmatter_diff only DIFFS heads, it does not rewrite them).

Sanctioned-override ledger (added 2026-08-01, seam fix): a people rename REPLACES a name in
`participants:`, which drops the old spelling — and `participants` is append-only under the
`redistill` preserve profile, so check.sh's gate (verify_frontmatter_preserved.py) hard-failed every
distilled-source note a merge touched, making a people-dedup performable but never committable. The
rename recipes now record each participants transition in `_manifest/frontmatter-overrides.jsonl` —
the SAME append-only ledger edit_note_frontmatter.py writes and the gate already consults — through
the same transaction, so a rolled-back merge leaves no orphan entry. See
`plan_participant_overrides` for why the excuse cannot be broader than the exact rename performed.

The `domain` recipe records its own entries the same way (added on an explicit ruling
after the trade-off was put to him). Answering "move to <domain>" re-stamps `domain:` and strips the
flag tag — both FROZEN under the `redistill` profile — so since the move-bypass was closed the recipe
was hard-refused on every distilled-source note and the ~27 notes in `unsorted/` had no answerable
path at all. It now mints an entry per frozen field it changes (`plan_domain_overrides`) and hands
them to `plan_move_note`, the same guarded-caller seam `edit_note_frontmatter.py`'s re-domain path
uses. What makes the widening acceptable is the audit trail: the ledger `reason` names the clarify
item id AND the human's verbatim answer, so every excused change points at the decision that
authorized it (and `_manifest/clarify-log.jsonl` records the same pair independently). ONLY
`domain:`/`tags:` may be excused here — every other frozen field a move touches stays unexcused and
the in-transaction gate still refuses it.

Gate discipline (hardened 2026-08-02 after an adversarial review; each numbered item is a fixed,
reproduced defect — do not relax one without re-reading its rationale):
  1. The override entry is minted from the EXPECTED transition (HEAD's participants with this
     rename applied), never from the observed working-tree diff, and a note that is dirty in any
     way the ledger has not ALREADY sanctioned is refused outright. Minting from the observed diff
     let one entry silently excuse every unrelated participant drop sitting in the working tree.
  2. Every rollback path catches BaseException (a Ctrl-C during the ~7,700-note lint used to skip
     rollback entirely, leaving the store half-written), and a rollback that cannot restore a file
     keeps going and reports every failure instead of aborting on the first.
  3. A person whose canonical name is ALSO a note filename cannot be renamed here (47 of them
     exist): the rewrite cannot tell a person-mention `[[Pat Placeholder]]` from a link to the note
     `research/Pat Placeholder.md`. Renaming a NOTE is `plan_move_note`'s job.
  4. The transaction runs the frozen-field gate itself (`verify_frontmatter_preserved.check_path`,
     routed to the profile check.sh would apply), not just lint_store — otherwise an unexcused
     frozen-field change lands on disk, the queue is marked answered, and the store is left
     uncommittable with a green result.

Answer contract (how free-text `--answer` routes to a branch, per category):
  people-dedup   "yes"/"same"/"merge"/"alias …" -> add variant as alias of the canonical (fold the
                   variant's own row if it already has one), rewrite participants + [[Name]] links;
                   "no"/"distinct"/"different"   -> mark answered, no store edit (per SKILL, promote
                   separately via the promote item).
  people-cluster "canonical: <Member>" (or a bare member name; optional labelled clauses
                   "; role: <text>" and "; only: <Member>, <Member>", both resolved against the
                   KNOWN member list) -> PROMOTE <Member> into people.md as a new verify:true
                   row in the dated `## Promoted via /clarify (<today>)` table, record the other
                   members as its aliases, and rewrite participants + [[Name]] links in every
                   affected note (one corpus pass, so two members in the SAME note collapse
                   correctly); "skip"/"not one person" -> mark answered, no store edit.
  people-promote "add[ — ]<role text>"          -> insert a row (canonical=entity, role=text) into a
                   dated `## Promoted via /clarify (<today>)` table; "alias of X" -> dedup add-alias;
                   "noise"/"skip" -> skipped.
  people-verify  "confirm[ed]"/"yes"            -> flip the row's Verify? cell to —;
                   "correct[ed]: <role>"        -> replace role text, flip Verify? -> —
                                                    (name change: "rename to <Name>" -> rename+propagate);
                   "merge into X"/"same as X"   -> dedup fold; "remove"/"not a real …" -> skipped.
  people-role    "confirm"                      -> keep the tentative hint, drop the tentative marker;
                   any other text               -> set the role cell to that text (marker dropped).
  domain         "correct"/"clear"/"confirm"    -> strip the needs-domain-confirm/domain-review tag;
                   "move to <domain>"/"<domain>"-> relocate the file to <domain>/, set domain:, strip
                                                    the flag, rewrite inbound [[slug]] if the slug moved,
                                                    and record the domain:/tags: transition in the
                                                    sanctioned-override ledger (see above). A target
                                                    that is not in _lib.constants.DOMAINS is refused.
  thin-source    "keep"                         -> mark answered (flag left in place, per SKILL);
                   "enrich[: ]<detail>"         -> append the detail as a manual addition;
                   "archive"                    -> move the note to _archive/ (never delete).
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

from _lib.constants import DOMAINS                     # noqa: E402  (single owner: domain list)
from _lib.notes import (split_frontmatter, get_field,  # noqa: E402  (single owner: frontmatter read)
                        note_slug)
from _lib.frontmatter_diff import (diff_frontmatter, PROFILES,          # noqa: E402
                                   OVERRIDES_LEDGER, head_blob_sha, git_show_head,
                                   override_entry, merge_overrides, load_overrides,
                                   parse_inline_list, unquote_member, render_inline_list,
                                   REDISTILL_SOURCES)
# The frozen-field gate check.sh runs, imported as a LIBRARY (single owner — this module must judge a
# note by exactly the check the store will apply at commit time, never by a local re-implementation).
from verify_frontmatter_preserved import (check_path as _gate_check_path,      # noqa: E402
                                        rename_source as _rename_source)

QUEUE_REL = "_manifest/clarify-queue.jsonl"
LOG_REL = "_manifest/clarify-log.jsonl"
PEOPLE_REL = "_manifest/people.md"

# REDISTILL_SOURCES (the HEAD `source:` values check.sh routes through the `redistill` preserve-field
# gate) is IMPORTED from _lib.frontmatter_diff above, not redefined here. It used to be a local
# frozenset "kept in sync with check.sh's own case list" by hand — a third copy of one decision, so
# adding a sixth distilled source and missing a copy silently reopened the seam this gate closed.
# A people rename on any OTHER source is not gated by that profile, so it needs no override entry.

# link-bearing knowledge dirs beyond the domain notes: the synthesized hubs + literature + top-level
# docs can all contain [[wikilinks]] (matches lint_store.py's all_slugs source set), so a rename
# propagation must sweep them too. participants: frontmatter only ever lives in domain notes (TOPS).
_EXTRA_LINK_GLOBS = ["dossiers/*.md", "digests/*.md", "profile/**/*.md", "literature/*.md", "*.md"]

FLAG_TAGS = ("needs-domain-confirm", "domain-review")
THIN_TAGS = ("empty-source", "source-limited")


class WritebackError(Exception):
    """Raised when a recipe cannot proceed (ambiguous answer, missing row) or verification fails."""


# ======================================================================================
# people.md table editor — header-driven, single-space GFM, edits one row in place
# ======================================================================================
_SEP_RE = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$')     # a `|---|---|` separator row
_HEADING_RE = re.compile(r'^##\s+(.*)$')

# The dated sections `insert_promote_row` owns, and the shape it writes them in. Aliases sits LEFT
# of the role cell deliberately — see insert_promote_row's docstring (parse_people reads the role
# hint right-to-left, so an aliases column to the RIGHT of role would be mistaken for the role).
_PROMOTED_SECTION_RE = re.compile(r'^Promoted via /clarify\b')
ALIASES_HEADER = "Aliases / emails"
PROMOTE_HEADER = ["Canonical name", ALIASES_HEADER, "Role / affiliation", "Domain", "Verify?"]
PROMOTE_COLS = ["name", "aliases", "role", "domain", "verify"]


def _sep_line(ncols):
    """`|---|---|` for `ncols` columns — the separator style people.md already uses everywhere."""
    return "|" + "---|" * ncols


def _split_cells(line):
    """`| a | b | c |` -> ['a','b','c'] (stripped). Assumes no escaped pipes (none in people.md)."""
    return [c.strip() for c in line.strip().strip('|').split('|')]


def _join_cells(cells):
    """['a','b','c'] -> `| a | b | c |` (single-spaced GFM, matching the file's existing rows)."""
    return "| " + " | ".join(cells) + " |"


# header-cell text (lowercased) -> logical column kind. Ordered so the most specific match wins.
def _col_kind(header):
    h = header.strip().lower()
    if "canonical name" in h or h == "name":
        return "name"
    if "alias" in h or "handle" in h:
        return "aliases"
    if "verify" in h:
        return "verify"
    if h == "domain":
        return "domain"
    if "relationship" in h or "role" in h or "affiliation" in h or "what it is" in h:
        return "role"
    return None


class _Table:
    __slots__ = ("section", "header_idx", "cols", "row_idxs", "width")

    def __init__(self, section, header_idx, header_cells):
        self.section = section
        self.header_idx = header_idx
        self.width = len(header_cells)
        # kind -> column index (first matching header wins for duplicate kinds)
        self.cols = {}
        for i, hc in enumerate(header_cells):
            k = _col_kind(hc)
            if k and k not in self.cols:
                self.cols[k] = i
        self.row_idxs = []


class PeopleDoc:
    """Loads _manifest/people.md and edits individual table rows by canonical name, preserving every
    untouched line byte-for-byte. Genuine markdown-table editing (find row / edit a cell / append an
    alias / insert a row / fold a row into another) — the fiddly part called out in the plan."""

    def __init__(self, text):
        self._trailing_nl = text.endswith("\n")
        self.lines = text.split("\n")
        if self._trailing_nl and self.lines and self.lines[-1] == "":
            self.lines.pop()          # drop the artifact empty element from a trailing newline
        self._parse()

    def _parse(self):
        self.tables = []
        section = ""
        i = 0
        n = len(self.lines)
        while i < n:
            ln = self.lines[i]
            m = _HEADING_RE.match(ln)
            if m:
                section = m.group(1).strip()
                i += 1
                continue
            # a header row is a `|...|` line whose NEXT line is a separator
            if ln.lstrip().startswith("|") and i + 1 < n and _SEP_RE.match(self.lines[i + 1]) \
                    and "-" in self.lines[i + 1]:
                tbl = _Table(section, i, _split_cells(ln))
                j = i + 2
                while j < n and self.lines[j].lstrip().startswith("|"):
                    tbl.row_idxs.append(j)
                    j += 1
                self.tables.append(tbl)
                i = j
                continue
            i += 1

    # ---- lookup -----------------------------------------------------------------------
    def _name_of(self, tbl, idx):
        cells = _split_cells(self.lines[idx])
        ncol = tbl.cols.get("name", 0)
        return cells[ncol].strip() if ncol < len(cells) else ""

    def find_row(self, name):
        """Return (table, line_idx) for the data row whose name cell == `name` (exact, or the part
        before a ' (' annotation). None if not found."""
        target = name.strip()
        for tbl in self.tables:
            for idx in tbl.row_idxs:
                cell = self._name_of(tbl, idx)
                if cell == target or cell.split(" (")[0].strip() == target:
                    return tbl, idx
        return None

    def find_row_ci(self, name):
        """`find_row`, case-insensitively. Used by the promote-cluster staleness guards: those
        exist to stop a name becoming a canonical row AND someone else's alias at once, and
        build_clarify_queue keys its own registry map on `name.lower()` / `slug()`. An exact-case
        guard is therefore strictly weaker than the check it backs up — 'Ann Mcexample' gaining a
        row would not block promoting 'Ann McExample', producing two rows that differ only by case
        and that parse_people collapses last-wins."""
        target = name.strip().casefold()
        for tbl in self.tables:
            for idx in tbl.row_idxs:
                cell = self._name_of(tbl, idx)
                if cell.casefold() == target or cell.split(" (")[0].strip().casefold() == target:
                    return tbl, idx
        return None

    # ---- cell edits (operate on a specific line, rewrite it in place) ------------------
    def get_cell(self, tbl, idx, kind):
        cells = _split_cells(self.lines[idx])
        c = tbl.cols.get(kind)
        return cells[c].strip() if c is not None and c < len(cells) else ""

    def set_cell(self, tbl, idx, kind, value):
        cells = _split_cells(self.lines[idx])
        c = tbl.cols.get(kind)
        if c is None:
            raise WritebackError(f"table in section '{tbl.section}' has no '{kind}' column")
        while len(cells) <= c:
            cells.append("")
        cells[c] = value
        self.lines[idx] = _join_cells(cells)

    def set_verify_confirmed(self, tbl, idx):
        if "verify" in tbl.cols:
            self.set_cell(tbl, idx, "verify", "—")

    def append_alias(self, tbl, idx, alias):
        """Record `alias` on the canonical row. If the table has an Aliases column, append there
        (quoting a multi-word name, reusing the cell's existing ';' or ',' separator). If it does
        NOT (Org-style tables, or any table shape that ever loses the column again), embed a quoted
        `(alias: "…")` in the descriptive cell — the store's established convention (e.g. `…
        (alias: "Pat Example")`); parse_people harvests quoted strings from the WHOLE row, so
        either placement registers in alias_to_canon.

        The embedded branch is why `_preserve_alias_tokens` exists: it makes the ROLE cell the only
        place the alias lives, so a full-cell role write destroys it. The Promoted-via-/clarify
        tables now carry an `Aliases / emails` column precisely so they take the structured branch
        (see `insert_promote_row` / `migrate_promoted_alias_column`); the embedded branch remains for
        the table shapes that still lack one."""
        if "aliases" in tbl.cols:
            cur = self.get_cell(tbl, idx, "aliases")
            formatted = _format_alias(alias)
            if cur and cur != "—" and (formatted in cur or formatted.strip('"') in cur):
                return                                   # already present — idempotent
            # ...and idempotent ACROSS cells: a row migrated out of the embedded convention can
            # still state an alias in its role prose (`… ; alias "Sam Placeholder"`), where
            # parse_people already harvests it. Re-adding it to the new column would duplicate a
            # mapping that already resolves. Only the quoted form counts, which is exactly the form
            # parse_people reads.
            if f'"{alias}"' in self.get_cell(tbl, idx, "role"):
                return
            if not cur or cur == "—":
                new = formatted
            else:
                sep = "; " if ";" in cur else (", " if "," in cur else "; ")
                new = cur + sep + formatted
            self.set_cell(tbl, idx, "aliases", new)
            return
        if "role" not in tbl.cols:
            raise WritebackError(f"row in section '{tbl.section}' has neither an aliases nor a "
                                 f"descriptive column to record the alias '{alias}'")
        cur = self.get_cell(tbl, idx, "role")
        if f'"{alias}"' in cur:
            return                                       # already present — idempotent
        quoted = f'"{alias}"'                            # always quote so parse_people harvests it
        new = f"{cur} (alias: {quoted})" if cur and cur != "—" else f"(alias: {quoted})"
        self.set_cell(tbl, idx, "role", new)

    def remove_row(self, tbl, idx):
        """Delete a data row line and re-parse (line indices shift). Registry curation, not a note
        deletion — the never-delete rule governs note FILES, not table rows (git keeps history)."""
        del self.lines[idx]
        self._parse()

    def _row_for(self, tbl, values):
        """Render a data row for `tbl` from a {column-kind: text} map, so a row always matches the
        header the table actually has. Any column the table carries that `values` does not name
        (or that has no recognised kind) gets the empty marker."""
        width = max(tbl.width, max(tbl.cols.values(), default=-1) + 1)
        cells = ["—"] * width
        for kind, i in tbl.cols.items():
            if kind in values:
                cells[i] = values[kind]
        return _join_cells(cells)

    def insert_promote_row(self, name, role, domain, today, verify="—"):
        """Insert a row for `name` into a dated `## Promoted via /clarify (<today>)` table, creating
        that section at EOF if absent. Deterministic home for engine-promoted rows, matching the
        file's existing 'Promoted via /clarify (<date>)' convention. `verify` defaults to the
        confirmed marker (people-promote: a human named the role, so the row is confirmed on arrival);
        the people-cluster recipe passes "true", because choosing a canonical SPELLING is not the
        same as confirming the person's identity/role — that row must come back as a people-verify
        question on the next queue build.

        The section is created with an `Aliases / emails` column (PROMOTE_HEADER) so `append_alias`
        takes its STRUCTURED branch on these rows: without it the alias lived inside the role cell
        and every full-cell role write (people-verify `correct:`, people-role) wiped it — 28 aliases
        across 24 rows in one session, with green tests and a green check.sh, because nothing lints
        role-cell contents. Aliases sits LEFT of the role cell on purpose: build_clarify_queue's
        parse_people takes the role hint from the LAST non-flag, non-domain cell, so an aliases
        column to the RIGHT of the role would be read as the role. That is also the shape its
        existing 5-column harvest rows already use.

        A row is rendered against the TARGET table's own header (`_row_for`), not a fixed 4-tuple,
        so a legacy 4-column Promoted section that has not been migrated still gets a well-formed
        row (its aliases then fall back to the embedded `(alias: "…")` convention)."""
        section = f"Promoted via /clarify ({today})"
        values = {"name": name, "aliases": "—", "role": role, "domain": domain or "—",
                  "verify": verify}
        for tbl in self.tables:
            if tbl.section == section and tbl.row_idxs:
                self.lines.insert(tbl.row_idxs[-1] + 1, self._row_for(tbl, values))
                self._parse()
                return
        # append a fresh section at EOF
        while self.lines and self.lines[-1].strip() == "":
            self.lines.pop()
        row = _join_cells([values[k] for k in PROMOTE_COLS])
        self.lines += [
            "", f"## {section}", "",
            "Recurring participants promoted via the clarify write-back engine, human-confirmed.", "",
            _join_cells(PROMOTE_HEADER), _sep_line(len(PROMOTE_HEADER)), row,
        ]
        self._parse()

    def migrate_promoted_alias_column(self):
        """Give every `## Promoted via /clarify …` table an `Aliases / emails` column and move each
        row's embedded `(alias: "X")` tokens out of the role cell into it. Returns
        [(canonical_name, [alias, ...])] for the rows whose tokens moved.

        Structural half of the alias-clobber fix (the defensive half, `_preserve_alias_tokens`,
        stays): once the column exists, `append_alias` writes aliases to their own cell and a role
        write can no longer collide with alias storage.

        Equivalence is the hard constraint — parse_people harvests quoted strings from the WHOLE
        row, so both placements register in alias_to_canon, and this must not change a single pair.
        Hence: the alias is re-emitted in its EXACT quoted form (never re-formatted through
        _format_alias, which would unquote a single-token alias and drop it from the harvest), the
        role text is preserved verbatim minus the tokens, and no other table shape is touched.
        Idempotent: a table that already has the column is skipped.

        Cell insertion is line-count-preserving, so `row_idxs` captured before the loop stay valid;
        the doc is re-parsed once at the end."""
        moved, touched = [], False
        for tbl in list(self.tables):
            if not _PROMOTED_SECTION_RE.match(tbl.section) or "aliases" in tbl.cols:
                continue
            touched = True
            at = tbl.cols.get("name", 0) + 1
            role_col = tbl.cols.get("role")
            hcells = _split_cells(self.lines[tbl.header_idx])
            hcells.insert(at, ALIASES_HEADER)
            self.lines[tbl.header_idx] = _join_cells(hcells)
            self.lines[tbl.header_idx + 1] = _sep_line(len(hcells))
            for idx in tbl.row_idxs:
                cells = _split_cells(self.lines[idx])
                while len(cells) < at:
                    cells.append("")
                aliases = []
                if role_col is not None and role_col < len(cells):
                    for _tok, alias in _alias_tokens(cells[role_col]):
                        if alias not in aliases:
                            aliases.append(alias)
                    if aliases:
                        cells[role_col] = _strip_alias_tokens(cells[role_col]) or "—"
                        moved.append((cells[tbl.cols.get("name", 0)], aliases))
                cells.insert(at, "; ".join(f'"{a}"' for a in aliases) if aliases else "—")
                self.lines[idx] = _join_cells(cells)
        if touched:
            self._parse()
        return moved

    def render(self):
        text = "\n".join(self.lines)
        return text + "\n" if self._trailing_nl else text


def _format_alias(name):
    """Quote a multi-word alias (people.md convention: `"Ann Specimen"`); leave a single token or an
    email bare (`Sam Sample` stays `Sam Sample`? no — has a space -> quoted; `sam@example.com` -> bare)."""
    name = name.strip()
    if " " in name and "@" not in name:
        return f'"{name}"'
    return name


# ======================================================================================
# frontmatter / body edit helpers on note text
# ======================================================================================
def _requote_like(token, new):
    """Render `new` with the SAME quoting the member `token` used (`'"Samuel Testcase"'` + `Sam Testcase` ->
    `'"Sam Testcase"'`). A bare token that would need quoting after the substitution gets quoted."""
    q = token[:1] if token[:1] in ('"', "'") else ""
    if not q and re.search(r'[,\[\]"\']', new):
        q = '"'                       # a bare inline-list member cannot carry a comma/bracket/quote
    return f"{q}{new}{q}" if q else new


def _rewrite_participants(text, old, new):
    """Replace an exact participant name in a note's `participants: [..]` list. Returns (text, changed).

    Tokenized through _lib.frontmatter_diff.parse_inline_list — the SAME owner the preserve gate
    diffs with — so quoted members (`participants: ["Samuel Testcase", ...]`, ~128 notes) are matched,
    not silently skipped. Each surviving member keeps its ORIGINAL quoting, and the substituted one
    inherits the quoting of the member it replaces, so the rewrite changes names and nothing else.
    De-duplication is on the UNQUOTED value (two members of one cluster in one note collapse to a
    single canonical entry even when only one of them was quoted)."""
    m = re.search(r'^participants:\s*(\[.*?\])\s*$', text, re.M)
    if not m:
        return text, False
    members = parse_inline_list(m.group(1))
    if old not in [unquote_member(x) for x in members]:
        return text, False
    out, seen = [], set()
    for tok in members:
        repl = _requote_like(tok, new) if unquote_member(tok) == old else tok
        key = unquote_member(repl)
        if key not in seen:
            seen.add(key)
            out.append(repl)
    new_line = "participants: " + render_inline_list(out)
    return text[:m.start()] + new_line + text[m.end():], True


def _rewrite_wikilink(text, old_target, new_target):
    """Replace every `[[old_target]]` token with `[[new_target]]`. Exact whole-target match (the
    [[ ]] delimiters make it unambiguous), covers body + related:/supersedes: frontmatter uniformly."""
    pat = re.compile(r'\[\[' + re.escape(old_target) + r'\]\]')
    new_text, n = pat.subn(f"[[{new_target}]]", text)
    return new_text, n > 0


def _strip_flag_tags(text, flags):
    """Remove any of `flags` from a note's inline `tags: [..]`. Returns (text, changed)."""
    m = re.search(r'^tags:\s*\[(.*?)\]\s*$', text, re.M)
    if not m:
        return text, False
    tags = [t.strip() for t in m.group(1).split(",") if t.strip()]
    kept = [t for t in tags if t not in flags]
    if kept == tags:
        return text, False
    return text[:m.start()] + "tags: [" + ", ".join(kept) + "]" + text[m.end():], True


def _set_domain_field(text, domain):
    """Set the `domain:` frontmatter scalar. Returns (text, changed)."""
    def repl(m):
        return f"domain: {domain}"
    new_text, n = re.subn(r'^domain:\s*.*$', repl, text, count=1, flags=re.M)
    return new_text, (n > 0 and new_text != text)


_ALIAS_TOKEN_RE = re.compile(r'\s*\(alias:\s*"[^"]+"\)')


def _alias_tokens(text):
    """[(token, alias)] for every embedded `(alias: "…")` in `text`. ONE owner for that token's
    shape — shared by `_preserve_alias_tokens` (which carries tokens across a role write) and
    `PeopleDoc.migrate_promoted_alias_column` (which moves them into a real column)."""
    return [(t.strip(), re.search(r'"([^"]+)"', t).group(1))
            for t in _ALIAS_TOKEN_RE.findall(text or "")]


def _strip_alias_tokens(text):
    """`text` with every embedded `(alias: "…")` removed, whitespace tidied. The inverse of the
    embedding `append_alias` does; the rest of the cell is preserved verbatim."""
    return re.sub(r'\s{2,}', ' ', _ALIAS_TOKEN_RE.sub("", text or "")).strip()


def _preserve_alias_tokens(current, new_role):
    """Carry `(alias: "…")` tokens from `current` into `new_role`.

    In tables WITHOUT an Aliases column (the Org/Promoted-style ones) `append_alias` embeds the
    alias inside the ROLE cell — so the role cell is the only place that alias exists, and any
    blind `set_cell(..., "role", answer)` silently destroys it. That is not hypothetical: the
    people-verify `correct:` and people-role recipes did exactly this 24 times in one session,
    wiping ~29 alias keys the same session had just created, with green tests and a green
    check.sh (nothing lints role-cell contents). Aliases the NEW text already states are not
    duplicated, so re-answering a row is idempotent."""
    out = (new_role or "").strip()
    for tok, alias in _alias_tokens(current):
        if f'"{alias}"' not in out:
            out = f"{out} {tok}".strip()
    return out


def _strip_tentative(role):
    """Drop the tentative / (role TBD) / TBD markers from a role string, tidy punctuation."""
    r = re.sub(r'\(?\brole TBD\b\)?', '', role, flags=re.I)
    r = re.sub(r'\btentative\b', '', r, flags=re.I)
    r = re.sub(r'\bTBD\b', '', r)
    r = re.sub(r'\s{2,}', ' ', r).strip(" ;,·-")
    return r.strip()


# ======================================================================================
# Store — resolves paths, scans the corpus, holds the read caches a recipe needs
# ======================================================================================
class Store:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    # --- corpus iteration --------------------------------------------------------------
    def domain_notes(self):
        """Every `<domain>/**/*.md` note (participants: lives only here), sorted, _index.md skipped."""
        for d in DOMAINS:
            for fp in sorted(glob.glob(os.path.join(self.root, d, "**", "*.md"), recursive=True)):
                if os.path.basename(fp) != "_index.md":
                    yield fp

    def link_files(self):
        """Every link-bearing knowledge file (domain notes + dossiers/digests/profile/literature +
        top-level docs) — the universe a [[wikilink]] rename must sweep. Matches lint's slug sources."""
        seen = set()
        for fp in self.domain_notes():
            if fp not in seen:
                seen.add(fp)
                yield fp
        for g in _EXTRA_LINK_GLOBS:
            for fp in sorted(glob.glob(os.path.join(self.root, g), recursive=True)):
                if os.path.isfile(fp) and fp not in seen:
                    seen.add(fp)
                    yield fp

    def all_slugs(self):
        """{note slug -> rel path} over the SAME file set lint_store.py's `all_slugs` builds (domain
        notes + dossiers/profile/literature/top-level docs; `link_files` additionally covers
        digests/, which are link targets too). A `[[token]]` matching one of these slugs is a link to
        a FILE — which is why a person whose canonical name is also a note filename may not be
        renamed by the person-rename sweep (see plan_people_renames)."""
        out = {}
        for fp in self.link_files():
            out[note_slug(fp)] = self.rel(fp)
        return out

    def rel(self, abspath):
        return os.path.relpath(abspath, self.root)


# ======================================================================================
# Plan — the in-memory description of a recipe's edits (assembled before any disk write)
# ======================================================================================
class Plan:
    """Ordered file ops plus the queue bookkeeping a recipe produces. `writes` overwrite/create a file
    in place; `moves` relocate src->dst (with possibly-changed content, e.g. a domain move or archive)."""

    def __init__(self):
        self.writes = []     # list of (rel_path, new_content)
        self.moves = []      # list of (src_rel, dst_rel, new_content)
        self.overrides = []  # sanctioned-override ledger entries this plan must record (see
                             # plan_participant_overrides) — bookkeeping, like the queue/log, so it
                             # is NOT part of touched(); the transaction writes + rolls it back.
        self.status = "answered"
        self.edit = ""
        self.messages = []

    def touched(self):
        files = [p for p, _ in self.writes]
        for src, dst, _ in self.moves:
            files += [src, dst]
        return files


# ======================================================================================
# Transaction — apply a plan + bookkeeping, verify, roll back everything on failure
# ======================================================================================
class _Tx:
    def __init__(self, root):
        self.root = root
        self._undo = []     # LIFO list of () -> None restore closures

    def _abs(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, content):
        ap = self._abs(rel)
        orig = None
        if os.path.exists(ap):
            with open(ap, "rb") as f:
                orig = f.read()
        existed = orig is not None

        # L4: record directories this write has to CREATE, innermost-last, so undo can remove them.
        # Without this, "moves reversed" was not literally true — a rollback left empty dirs behind,
        # which a future subpath-creating recipe would leak into the regen/index scans.
        made_dirs = []
        d = os.path.dirname(ap)
        while d and not os.path.exists(d):
            made_dirs.append(d)
            d = os.path.dirname(d)
        made_dirs.reverse()

        def undo():
            if existed:
                with open(ap, "wb") as f:
                    f.write(orig)
            elif os.path.exists(ap):
                os.remove(ap)
            for created in reversed(made_dirs):        # innermost first
                try:
                    os.rmdir(created)
                except OSError:
                    pass                               # non-empty (someone else's file) — leave it
        self._undo.append((rel, undo))
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            f.write(content)

    def move(self, src_rel, dst_rel, content):
        # model a move as write(dst) + remove(src) so a single undo stack reconstructs the state
        self.write(dst_rel, content)
        src_ap = self._abs(src_rel)
        with open(src_ap, "rb") as f:
            orig = f.read()

        def undo():
            os.makedirs(os.path.dirname(src_ap), exist_ok=True)
            with open(src_ap, "wb") as f:
                f.write(orig)
        self._undo.append((src_rel, undo))
        os.remove(src_ap)

    def rollback(self):
        """Undo every applied write/move, LIFO. Each closure is attempted INDEPENDENTLY: a failing
        undo (a file made read-only mid-run, a vanished parent dir) must not abandon the remaining
        restores — that would turn one unrestorable file into a wholly half-written store. The undo
        stack is cleared unconditionally (rollback is idempotent; the `except BaseException` handlers
        below may call it a second time), and every failure is reported together at the end so the
        operator learns EVERY path that still needs manual restoration, not just the first."""
        failures = []
        for rel, undo in reversed(self._undo):
            try:
                undo()
            except Exception as e:                    # keep restoring the rest of the stack
                failures.append(f"{rel}: {e}")
        self._undo = []
        if failures:
            raise WritebackError(
                "ROLLBACK INCOMPLETE — the store is half-written and needs manual repair. "
                f"{len(failures)} file(s) could not be restored:\n  " + "\n  ".join(failures))


# ======================================================================================
# queue + log bookkeeping
# ======================================================================================
def load_queue(store):
    items = []
    with open(store.path(QUEUE_REL), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def find_item(store, item_id):
    for it in load_queue(store):
        if it.get("id") == item_id:
            return it
    raise WritebackError(f"no queue item with id {item_id!r} in {QUEUE_REL}")


def _queue_with_status(store, item_id, status, answer):
    """Return the FULL new queue-file content with `item_id`'s status+answer updated in place."""
    out = []
    found = False
    with open(store.path(QUEUE_REL), encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            obj = json.loads(s)
            if obj.get("id") == item_id:
                obj["status"] = status
                obj["answer"] = answer
                found = True
            out.append(json.dumps(obj, ensure_ascii=False))
    if not found:
        raise WritebackError(f"id {item_id!r} vanished from the queue")
    return "\n".join(out) + "\n"


def _log_appended(store, entry):
    """Return the FULL new clarify-log content with `entry` appended (create the file if absent)."""
    p = store.path(LOG_REL)
    prev = ""
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            prev = f.read()
    if prev and not prev.endswith("\n"):
        prev += "\n"
    return prev + json.dumps(entry, ensure_ascii=False) + "\n"


# ======================================================================================
# propagation planners (people rename / note move) — reused by several recipes
# ======================================================================================
def plan_people_renames(store, old_names, new_name):
    """Every note whose participants: contains ANY of `old_names`, and every link file containing
    any `[[old]]`, rewritten old->new_name. Returns (edits, messages) where edits is a list of
    (rel_path, new_content).

    ONE corpus pass over the whole list rather than N single-name passes: two members of a name
    cluster can appear in the SAME note, and per-name passes each re-read the file from disk, so the
    last write would silently clobber the earlier one. `_rewrite_participants` already de-duplicates
    the rewritten list, so a note listing two members ends up with a single canonical entry.

    REFUSES a name that is also a NOTE FILENAME. `[[Pat Placeholder]]` is ambiguous: it may be a mention
    of the person OR a link to the note `research/Pat Placeholder.md` — and 47 people.md canonicals are
    currently also note slugs. Rewriting it re-points a link at a file that was never renamed,
    dangling it; lint_store only validates wikilinks inside related:/supersedes:/superseded_by:, so a
    broken BODY link is invisible and the transaction commits clean. Renaming a note is
    `plan_move_note`'s job (it moves the file AND its inbound links together), so this refuses and
    says so rather than half-doing it."""
    olds = list(dict.fromkeys(old_names))
    if not olds:
        return [], []
    slugs = store.all_slugs()
    clash = [(o, slugs[o]) for o in olds if o in slugs]
    if clash:
        raise WritebackError(
            "refusing to rewrite [[wikilinks]] for a person whose name is also a NOTE FILENAME — "
            "a link to that note is indistinguishable from a mention of the person, and this sweep "
            "would re-point it at a file that is not being renamed: "
            + "; ".join(f"'{n}' is the note {p}" for n, p in clash)
            + ". Rename the NOTE first (move_note / plan_move_note, which moves the file and its "
              "inbound links together), or resolve this item by hand.")
    edits = {}
    for fp in store.domain_notes():
        with open(fp, encoding="utf-8") as f:
            txt = f.read()
        changed = False
        for old in olds:
            txt, ch = _rewrite_participants(txt, old, new_name)
            changed = changed or ch
        if changed:
            edits[fp] = txt
    link_hits = 0
    for fp in store.link_files():
        base = edits.get(fp)
        txt = base if base is not None else open(fp, encoding="utf-8").read()
        changed = False
        for old in olds:
            txt, ch = _rewrite_wikilink(txt, old, new_name)
            changed = changed or ch
        if changed:
            link_hits += 1
            edits[fp] = txt
    messages = []
    # A rewritten link now points at [[new_name]]. If nothing in the store carries that slug the
    # link resolves to no file — harmless for a person-mention (the store's normal usage), but it is
    # exactly what a silently-broken link looks like, and lint cannot see it outside related:. Say so.
    if link_hits and new_name not in slugs:
        messages.append(f"{link_hits} file(s) now link [[{new_name}]], which resolves to no note "
                        f"(fine for a person mention; lint only validates related:/supersedes: links)")
    return [(store.rel(fp), c) for fp, c in edits.items()], messages


def plan_people_rename(store, old_name, new_name):
    """Single-name rename — the one-element case of `plan_people_renames` (single owner of the
    propagation sweep; do not re-implement it here). Returns (edits, messages)."""
    return plan_people_renames(store, [old_name], new_name)


def _members(val):
    """Unquoted members of an inline frontmatter list value — the same view the gate compares."""
    return [unquote_member(x) for x in parse_inline_list(val or "")]


def _dropped(old_val, new_val):
    """Members present in `old_val` but not in `new_val` (sorted)."""
    return sorted(set(_members(old_val)) - set(_members(new_val)))


def _refuse_extra_drops(rel, dropped, old_names, new_name):
    """Every dropped participant must be a name this rename is actually renaming. Anything else is a
    drop the working tree already contained, which a verbatim-minted entry would silently excuse."""
    extra = sorted(set(dropped) - set(old_names))
    if extra:
        raise WritebackError(
            f"{rel}: this rename would record a sanctioned-override entry that also excuses dropping "
            f"{extra} from participants — name(s) it is not renaming, and nobody's sanctioned change. "
            f"Commit or revert that drop first, then re-run. (renaming: {list(old_names)} -> "
            f"'{new_name}')")


def _describe_violations(viols):
    """Human-readable one-liner for a list of gate violations (used in refusal messages)."""
    return "; ".join(
        f"{v['field']} dropped {v.get('removed')}" if v["kind"] == "removed"
        else f"{v['field']} changed {v['old']!r} -> {v['new']!r}" for v in viols)


def _expected_participants(head_txt, old_names, new_name):
    """The `participants:` value a note WOULD have if this rename — and only this rename — were
    applied to `head_txt`. Computed with the same rewriter the plan used, so the comparison is
    byte-exact (quoting and member order included)."""
    txt = head_txt
    for old in old_names:
        txt, _ = _rewrite_participants(txt, old, new_name)
    head, _ = split_frontmatter(txt)
    return get_field(head, "participants") if head is not None else ""


def plan_participant_overrides(store, edits, old_names, new_name, reason, today=None):
    """Sanctioned-override ledger entries for the `participants:` renames contained in `edits`
    (a list of (rel_path, new_content) as returned by plan_people_rename). Returns
    (entries, messages).

    WHY (the seam this closes): a people-dedup/rename REPLACES a participant name, which necessarily
    DROPS the old spelling. But `participants` is append-only under the `redistill` preserve profile
    (_lib/frontmatter_diff.PROFILES), so check.sh's gate — verify_frontmatter_preserved.py — hard-
    fails every distilled-source note the merge touched. The merge could be performed but never
    committed. It is a seam, not an integrity failure: a people-dedup IS the store's sanctioned way
    to fix a misspelled person. So record it in the SAME append-only ledger edit_note_frontmatter.py
    writes (`_manifest/frontmatter-overrides.jsonl`), which the gate already consults.

    NARROW BY CONSTRUCTION — this does not weaken the gate:
      * `field`/`old`/`new` are byte-identical to what the gate will compute (the diff runs
        diff_frontmatter under the redistill profile, HEAD vs the planned new content) — a
        hand-derived tuple that is even slightly off excuses nothing;
      * the transition is validated against the EXPECTED one, not merely read off the observed
        diff. diff_frontmatter reports ONE aggregate participants record carrying the whole
        old/new list, so minting it verbatim handed the entry every drop present in the working
        tree — including drops this rename did not perform. Reproduced: HEAD
        `[Ann Example, Pat Placeholder, Sam Sampple]` + an uncommitted redistill that dropped Pat
        Placeholder + a dedup renaming Sampple->Sample produced ONE entry that excused BOTH drops, and
        check.sh went green on the unrelated one. Highly reachable, because /clarify applies items
        one at a time without committing between them. Now: every dropped member must be one of
        `old_names`, and on a note that is clean at HEAD the resulting list must equal HEAD's list
        with exactly this rename applied;
      * a note ALREADY modified relative to HEAD is refused unless the store has already sanctioned
        its current state — i.e. `check_path` (the gate itself, with the live ledger) finds no
        violation on the PRE-edit file. That admits the real batch workflow, where several merges
        land on one note before any commit and each successive state is covered by the previous
        entry (8 such paths in the 2026-08-01 dedup batch), while refusing exactly the case above,
        where the pre-edit drop is nobody's sanctioned change. The invariant is inductive: a
        gate-clean note plus a pure-rename delta stays gate-clean under the new entry;
      * only `participants` / kind="removed" violations are kept. A frozen field the rename also
        touched (e.g. a `[[Person]]` token inside `related:`) is deliberately NOT excused — the
        in-transaction gate (`_run_preserve_gate`) then refuses the whole apply;
      * `head_sha` binds the entry to the note's CURRENT HEAD blob, so it stops matching the moment
        that note's HEAD moves; and the entry goes inert the moment the LEDGER LINE itself is
        committed (load_overrides subtracts every key already at HEAD), exactly like an
        edit_note_frontmatter.py entry. Both are needed: the sha is an expiry/precision check, and
        being-uncommitted is what makes the excuse a one-shot rather than a standing authorization;
      * notes untracked at HEAD (nothing committed to violate) and notes whose HEAD `source:` is not
        a distilled connector source (check.sh does not apply the redistill profile to them) get no
        entry at all — the latter are reported in `messages` instead, because check.sh will still
        stop on them with its SUSPICIOUS halt.
    """
    today = today or datetime.date.today().isoformat()
    prof = PROFILES["redistill"]
    olds = list(dict.fromkeys(old_names))
    overrides = load_overrides(store.root)
    entries, non_distilled = [], []
    for rel, new_content in edits:
        head_txt = git_show_head(store.root, rel)
        if head_txt is None:
            continue                                   # untracked at HEAD — the gate skips it too
        old_head, _ = split_frontmatter(head_txt)
        new_head, _ = split_frontmatter(new_content)
        if old_head is None or new_head is None:
            continue
        if get_field(old_head, "source") not in REDISTILL_SOURCES:
            if rel.split("/", 1)[0] in DOMAINS:
                non_distilled.append(rel)              # no entry; check.sh SUSPICIOUS-halts instead
            continue                                   # not gated by the redistill profile
        sha = head_blob_sha(store.root, rel)
        if not sha:
            continue                                   # no HEAD blob to bind to -> unmatchable entry
        for v in diff_frontmatter(old_head, new_head, prof["preserve"], prof["mutable"]):
            if v["field"] != "participants" or v["kind"] != "removed":
                continue
            pre_txt = store.read(rel)
            pre_head, _ = split_frontmatter(pre_txt)
            pre_val = get_field(pre_head, "participants") if pre_head is not None else None
            if pre_val == v["old"]:
                # Clean at HEAD: the transition is fully determined. Require every dropped member to
                # be one this rename actually renames, AND the whole resulting list to equal HEAD's
                # with exactly this rename applied (byte-for-byte — quoting and order included).
                _refuse_extra_drops(rel, _dropped(v["old"], v["new"]), olds, new_name)
                expected = _expected_participants(head_txt, olds, new_name)
                if v["new"] != expected:
                    raise WritebackError(
                        f"{rel}: the planned participants value {v['new']!r} is not HEAD's value with "
                        f"this rename applied ({expected!r}) — refusing to record an override entry "
                        f"for a transition this rename does not explain.")
            else:
                # Already modified before this rename. Admissible ONLY if the store has ALREADY
                # sanctioned the current state — i.e. the gate itself, reading the PRE-edit file
                # against the live ledger, finds nothing to complain about (nothing is written during
                # planning). That is the real batch case: several merges land on one note before any
                # commit, each state covered by the previous entry. It is NOT the case where an
                # unrelated uncommitted drop is sitting there waiting to be folded into this excuse.
                stale = _gate_check_path(store.root, rel, "redistill", overrides)
                if stale:
                    raise WritebackError(
                        f"{rel} was already modified before this rename in a way the store has not "
                        f"sanctioned ({_describe_violations(stale)}). Recording an override entry now "
                        f"would fold that change into this rename's excuse. Commit or revert it (or "
                        f"record it via edit_note_frontmatter.py) first, then re-run.")
                # The pre-edit state is sanctioned, so only THIS edit's delta needs justifying: a
                # pure rename drops nothing but `olds` and adds nothing but `new_name`.
                _refuse_extra_drops(rel, _dropped(pre_val, v["new"]), olds, new_name)
                added = sorted(set(_members(v["new"])) - set(_members(pre_val)) - {new_name})
                if added:
                    raise WritebackError(
                        f"{rel}: this rename would also ADD participant(s) {added} — refusing to "
                        f"record an override entry for a transition it does not explain.")
            # kind is carried from the gate's own violation record (always "removed" here — the
            # filter above keeps nothing else), so the entry cannot excuse a "changed" violation
            # that happens to share the same (field, old, new).
            entries.append(override_entry(rel, "participants", v["old"], v["new"],
                                          sha, today, reason, kind=v["kind"]))
    messages = []
    if non_distilled:
        shown = ", ".join(non_distilled[:8]) + (" ..." if len(non_distilled) > 8 else "")
        messages.append(
            f"WARNING: {len(non_distilled)} modified note(s) are NOT from a distilled source, so they "
            f"get no override entry — but check.sh still stops on them with its SUSPICIOUS halt "
            f"(exit 1) until they are reviewed and committed: {shown}")
    return entries, messages


def _plan_rename_bookkeeping(plan, store, edits, old_names, new_name, reason, messages=()):
    """Attach a rename's edits, sanctioned-override entries and operator messages to `plan`. Single
    place where the three rename recipes wire the propagation sweep to the ledger, so none of them
    can drift into minting entries on its own terms."""
    for rel, content in edits:
        plan.writes.append((rel, content))
    plan.overrides, override_msgs = plan_participant_overrides(
        store, edits, old_names, new_name, reason)
    plan.messages.extend(list(messages) + override_msgs)
    return plan


def _overrides_appended(store, entries):
    """Return the FULL new override-ledger content with `entries` recorded (create if absent).
    Mirrors _log_appended: the transaction writes whole files so a rollback restores byte-for-byte
    (or removes a ledger this run created), leaving no orphan entry behind.

    Delegates to _lib.frontmatter_diff.merge_overrides — the ledger's single owner — so a SECOND
    merge on the same (path, field) COLLAPSES into the first still-uncommitted entry instead of
    leaving a stale intermediate live beside it (L4)."""
    return merge_overrides(store.root, entries)


def plan_move_overrides(store, src_rel, dst_rel, new_content, reason, today=None):
    """Sanctioned-override ledger entries for the `title:`/`aliases:` re-stamp `plan_move_note`
    performs when a note's SLUG changes (M6). Returns (entries, messages).

    Same construction — and the same narrowness — as `plan_participant_overrides`:
      * `field`/`old`/`new`/`kind` come from `diff_frontmatter`'s OWN violation records (HEAD:<src>
        vs the planned destination content, under the profile check.sh applies), so they are
        byte-identical to what the gate will compute; a hand-derived tuple excuses nothing;
      * ONLY `title` and `aliases` are kept, and only when the transition is EXACTLY the re-stamp —
        title `old_slug` -> `new_slug`, aliases gaining exactly the old title and nothing else.
        Every OTHER frozen field a move touches is deliberately left unexcused: a domain move's
        `domain:`/`tags:` re-stamp is the H4 defect itself, and the in-transaction gate must keep
        refusing it;
      * the source must be CLEAN at HEAD, so `old` is HEAD's value and the transition is fully
        determined (a dirty source mints nothing and the gate decides);
      * `head_sha` binds to `HEAD:<src>` — the same blob the gate resolves for the destination
        through its own rename lookup — and the entry is live only while its ledger line is
        uncommitted, exactly like every other entry."""
    today = today or datetime.date.today().isoformat()
    prof_name = _gate_profile(store, src_rel)
    if prof_name is None:
        return [], []                                  # check.sh applies no field gate to this note
    head_txt = git_show_head(store.root, src_rel)
    if head_txt is None:
        return [], []
    if store.read(src_rel) != head_txt:
        return [], [f"WARNING: {src_rel} had uncommitted changes, so the title re-stamp records NO "
                    f"sanctioned-override entry (the ledger's `old` must be HEAD's value). Commit or "
                    f"revert them first if the frozen-field gate refuses this move."]
    old_head, _ = split_frontmatter(head_txt)
    new_head, _ = split_frontmatter(new_content)
    if old_head is None or new_head is None:
        return [], []
    sha = head_blob_sha(store.root, src_rel)
    if not sha:
        return [], []
    old_slug, new_slug = note_slug(src_rel), note_slug(dst_rel)
    prof = PROFILES[prof_name]
    entries = []
    for v in diff_frontmatter(old_head, new_head, prof["preserve"], prof["mutable"]):
        if v["kind"] != "changed" or v["field"] not in ("title", "aliases"):
            continue                                   # not the re-stamp — let the gate refuse it
        if v["field"] == "title":
            if unquote_member(v["old"]) != old_slug or unquote_member(v["new"]) != new_slug:
                continue
        elif set(_members(v["new"])) != set(_members(v["old"])) | {old_slug}:
            continue
        entries.append(override_entry(dst_rel, v["field"], v["old"], v["new"],
                                      sha, today, reason, kind=v["kind"]))
    return entries, []


# The ONLY frozen fields a clarify `domain` answer may excuse. An explicit ruling widened
# this module's sanctioned-override writing from `participants`/removed to include the domain
# recipe's own re-stamp — and to exactly these two fields. Everything else a move touches (title,
# provenance.ref, related, ...) stays unexcused, so `_run_preserve_gate` refuses it and rolls back.
DOMAIN_OVERRIDE_FIELDS = ("domain", "tags")


def plan_domain_overrides(store, src_rel, dst_rel, new_content, target, reason,
                          drop_tags=FLAG_TAGS, today=None):
    """Sanctioned-override ledger entries for the `domain:`/`tags:` re-stamp the `domain` recipe
    performs when an answer moves a note to another domain dir. Returns (entries, messages).

    WHY (the seam this closes): `domain:` and `tags:` are both FROZEN under the `redistill` profile,
    and lint_store requires `domain:` to equal the note's top-level directory — so on a
    distilled-source note the field change and the file move are ONE operation, and since the
    move-bypass was closed (H4, 2026-08-04) the recipe was hard-refused on every such note. Nothing
    in `unsorted/` could be filed by answering its own clarify question. The ruling was
    that this recipe may record the transition, with the trade-off stated: a free-text queue answer
    now authorizes a frozen-field excuse.

    NARROW BY CONSTRUCTION — the same shape as `plan_move_overrides`/`plan_participant_overrides`:
      * `field`/`old`/`new`/`kind` come from `diff_frontmatter`'s OWN violation records (HEAD:<src>
        vs the planned destination content, under the profile check.sh applies), so they are
        byte-identical to what the gate will compute; a hand-derived tuple excuses nothing;
      * ONLY `domain` and `tags` are kept (`DOMAIN_OVERRIDE_FIELDS`), and only when the transition is
        EXACTLY the re-stamp: `domain` must land on the ANSWERED target, and `tags` may only LOSE
        members of `drop_tags` — a tag added, or any other tag dropped, mints nothing and the
        in-transaction gate then refuses the whole apply. `verified:` can never appear here (it is
        not in this tuple), and the gate hard-denies it regardless of any entry;
      * the source must be CLEAN at HEAD, so `old` is HEAD's value and the transition is fully
        determined (a dirty source mints nothing and the gate decides);
      * `head_sha` binds to `HEAD:<src>` — the same blob the gate resolves for the destination
        through its own rename lookup — and the entry is live only while its ledger line is
        uncommitted, exactly like every other entry;
      * `reason` (built by the caller) names the clarify item id and the human's verbatim answer. That is
        the accountability this widening rests on: the audit trail points at the human decision,
        which a hand-typed `--reason` does not guarantee."""
    today = today or datetime.date.today().isoformat()
    prof_name = _gate_profile(store, src_rel)
    if prof_name is None:
        return [], []                                  # check.sh applies no field gate to this note
    head_txt = git_show_head(store.root, src_rel)
    if head_txt is None:
        return [], []
    if store.read(src_rel) != head_txt:
        return [], [f"WARNING: {src_rel} had uncommitted changes, so the domain re-stamp records NO "
                    f"sanctioned-override entry (the ledger's `old` must be HEAD's value). Commit or "
                    f"revert them first if the frozen-field gate refuses this move."]
    old_head, _ = split_frontmatter(head_txt)
    new_head, _ = split_frontmatter(new_content)
    if old_head is None or new_head is None:
        return [], []
    sha = head_blob_sha(store.root, src_rel)
    if not sha:
        return [], []
    prof = PROFILES[prof_name]
    entries = []
    for v in diff_frontmatter(old_head, new_head, prof["preserve"], prof["mutable"]):
        if v["kind"] != "changed" or v["field"] not in DOMAIN_OVERRIDE_FIELDS:
            continue                                   # not the re-stamp — let the gate refuse it
        if v["field"] == "domain":
            if unquote_member(v["new"]) != target:
                continue
        elif set(_members(v["new"])) != set(_members(v["old"])) - set(drop_tags):
            continue
        entries.append(override_entry(dst_rel, v["field"], v["old"], v["new"],
                                      sha, today, reason, kind=v["kind"]))
    return entries, []


def _restamp_title(text, old_slug, new_slug):
    """Re-stamp `title:` to the note's NEW slug when the old title WAS the old slug, keeping the old
    title in `aliases:`. Returns (text, changed).

    WHY (M6): `plan_move_note` moved the file, re-stamped `domain:`, stripped flag tags and rewrote
    every inbound `[[wikilink]]` — but left `title:` on the old name. `regen.py`/`build_index.py`
    key DISPLAY off `title`, so the store's primary index-then-drill path kept surfacing the
    merged-away spelling after a people-dedup rename (5 notes; the data was backfilled by hand).

    Scoped to `title == old_slug` ON PURPOSE. That equality is the person-note / verbatim-obsidian
    convention (`research/Patricia Example.md` titled "Patricia Example"), and it is the only case where
    the new slug IS the correct new title. A DATED note's title is a human phrase, not its slug
    (`2026-01-05-vendor-sync.md` / "Vendor sync"), so deriving one from the slug there would
    FABRICATE a title — CLAUDE.md's first rule. Such a note keeps its title and says nothing."""
    m = re.search(r'^title:\s*(.*)$', text, re.M)
    if not m:
        return text, False
    cur = m.group(1).strip()
    if unquote_member(cur) != old_slug:
        return text, False
    quoted = f'"{new_slug}"' if cur[:1] in ('"', "'") else new_slug
    out = text[:m.start()] + f"title: {quoted}" + text[m.end():]
    return _record_alias(out, old_slug), True


def _record_alias(text, alias):
    """Add `alias` to a note's inline `aliases:` list (idempotent), creating the field directly after
    `title:` if the note has none — every note in the store carries one, so that branch is a
    fallback, not a path. Quoted uniformly: the store's aliases are `["Pat Example"]`."""
    m = re.search(r'^aliases:\s*(\[.*?\])\s*$', text, re.M)
    if not m:
        t = re.search(r'^title:\s*.*$', text, re.M)
        if not t:
            return text
        return text[:t.end()] + f'\naliases: ["{alias}"]' + text[t.end():]
    members = parse_inline_list(m.group(1))
    if any(unquote_member(x) == alias for x in members):
        return text
    members.append(f'"{alias}"')
    return text[:m.start()] + "aliases: " + render_inline_list(members) + text[m.end():]


def plan_move_note(store, old_rel, new_rel, new_domain=None, drop_tags=(), restamp_title=True,
                   content=None, overrides=()):
    """Move a note old_rel -> new_rel, optionally re-stamping domain: and stripping flag tags, and
    rewriting every inbound [[old_slug]] -> [[new_slug]] when the slug changes. Returns a Plan.

    When the slug changes, `title:` is re-stamped to match it and the old title is kept in
    `aliases:` (M6 — see `_restamp_title` for why, and why only when the title WAS the slug). Pass
    `restamp_title=False` to move the file and its links while leaving the title exactly as it is.
    `title`/`aliases` are FROZEN, so the re-stamp mints the matching sanctioned-override entries
    inside this same plan (`plan_move_overrides`), which the transaction writes and rolls back with
    the note.

    `content` / `overrides` (added 2026-08-04) are the seam for a GUARDED CALLER that has already
    assembled the destination frontmatter itself and already minted the sanctioned-override entries
    for the frozen fields it changes — today that is exactly one caller,
    `edit_note_frontmatter.py`'s re-domain path, the only sanctioned way to move a DISTILLED-source
    note between domain dirs (`domain:` is frozen, and lint requires `domain:` == the top-level dir,
    so the field change and the file move are one operation). Passing `content` means "use this text
    verbatim as the destination", so `new_domain`/`drop_tags` — which are how the UNGUARDED recipe
    path derives its own content — may not be combined with it: that would give one decision two
    owners. `overrides` are recorded through THIS plan's transaction, so a rolled-back move leaves no
    orphan ledger entry, exactly like the title re-stamp's own entries. They are not a bypass: the
    in-transaction gate (`_run_preserve_gate`) still runs, reads the ledger from disk, and refuses
    anything the entries do not exactly cover."""
    plan = Plan()
    plan.overrides = list(overrides)
    old_ap = store.path(old_rel)
    if not os.path.isfile(old_ap):
        raise WritebackError(f"note not found: {old_rel}")
    if os.path.abspath(old_ap) == os.path.abspath(store.path(new_rel)):
        raise WritebackError(f"source and destination are identical ({old_rel}); "
                             f"a domain 'move' to the note's current domain is a no-op — use 'correct'")
    if content is not None and (new_domain or drop_tags):
        raise WritebackError("plan_move_note: `content` is the caller's fully-assembled destination "
                             "text, so `new_domain`/`drop_tags` may not be combined with it")
    if content is None:
        with open(old_ap, encoding="utf-8") as f:
            content = f.read()
        if new_domain:
            content, _ = _set_domain_field(content, new_domain)
        if drop_tags:
            content, _ = _strip_flag_tags(content, drop_tags)

    old_slug = os.path.splitext(os.path.basename(old_rel))[0]
    new_slug = os.path.splitext(os.path.basename(new_rel))[0]
    retitled = False
    if old_slug != new_slug:
        content, _ = _rewrite_wikilink(content, old_slug, new_slug)     # self-links, if any
        if restamp_title:
            content, retitled = _restamp_title(content, old_slug, new_slug)
            if retitled:
                plan.messages.append(
                    f"title re-stamped '{old_slug}' -> '{new_slug}' (old title kept in aliases:) — "
                    f"regen/build_index key display off `title`, so a renamed note that keeps the "
                    f"old one goes on surfacing it in the index")
        for fp in store.link_files():
            if os.path.abspath(fp) == os.path.abspath(old_ap):
                continue
            txt = open(fp, encoding="utf-8").read()
            txt2, ch = _rewrite_wikilink(txt, old_slug, new_slug)
            if ch:
                plan.writes.append((store.rel(fp), txt2))
    plan.moves.append((old_rel, new_rel, content))
    if retitled:
        mint, msgs = plan_move_overrides(
            store, old_rel, new_rel, content,
            f"note renamed {old_rel} -> {new_rel}: title re-stamped to the new slug, old title "
            f"recorded in aliases")
        plan.overrides.extend(mint)       # extend, never assign: a guarded caller may have passed
        plan.messages.extend(msgs)        # its own entries in (see `overrides` in the docstring)
    return plan


# ======================================================================================
# answer parsing (per category) — see the module docstring for the full contract
# ======================================================================================
def _low(s):
    return (s or "").strip().lower()


def _dedup_decision(answer):
    a = _low(answer)
    if re.match(r'^(no\b|not\b|distinct|different)', a) or "not the same" in a or "distinct person" in a:
        return "distinct"
    if a.startswith("yes") or "same" in a or a.startswith("merge") or "alias" in a:
        return "same"
    return None


def _match_member(members, text, prefix):
    """Resolve `text` to ONE cluster member. Returns (member_or_None, ambiguous).

    Case is load-bearing here: a cluster's whole point is that members can differ ONLY by case
    ('Ann McExample'/'Ann Mcexample', 'Sam DeSample'/'Sam Desample'), so a plain case-insensitive
    match resolves to whichever appears first in `members` — silently selecting the spelling the
    answer REJECTED. Two such merges shipped before this existed.

    Longest match still wins (so 'Sam Specimen' cannot shadow 'Sam Specimen Jr'), but the
    exact/insensitive preference is applied WITHIN each length tier rather than as two full
    passes — otherwise a *shorter* exact-case member beats a *longer* case-insensitive one and
    inverts the longest-wins rule. When a tier offers several case-insensitive matches and no
    exact one, this reports `ambiguous` instead of picking: the caller then refuses, and the
    error already lists every member. Guessing here writes the wrong person into the registry.
    """
    cands = [m for m in members if m]
    for length in sorted({len(m) for m in cands}, reverse=True):
        tier = [m for m in cands if len(m) == length]
        hit = next((m for m in tier if text.startswith(m) if prefix), None) if prefix \
            else next((m for m in tier if text == m), None)
        if hit:
            return hit, False
        ci = [m for m in tier if (text.lower().startswith(m.lower()) if prefix
                                  else text.lower() == m.lower())]
        uniq = list(dict.fromkeys(ci))
        if len(uniq) == 1:
            return uniq[0], False
        if len(uniq) > 1:
            return None, True
    return None, False


def _cluster_decision(answer, members):
    """Route a people-cluster answer -> (decision, canonical, only_members, role).

    decision is "skip" (no store edit), "canonical" (promote + alias), or None (unparseable ->
    the caller raises rather than guessing). Accepted forms, all ANCHORED on the item's own
    `members` list — a member name can itself contain a dash, a dot or parentheses, so nothing here
    splits on punctuation alone:

        skip | no | not one person | distinct | different | noise
        <Member>                                   (bare, exactly as offered in `candidates`)
        canonical: <Member>
        canonical: <Member> (20 notes)             (a `candidates` string pasted back verbatim)
        canonical: <Member>; role: <role text>
        canonical: <Member>; only: <Member>, <Member>     (merge a SUBSET — see apply_people_cluster)
        ... `role:` and `only:` in either order, ';'-separated.

    CASE IS PART OF THE CONTRACT — it decides WHICH PERSON gets promoted, because the defining
    shape of a cluster is members that differ only by case ('Ann McExample'/'Ann Mcexample',
    'Sam DeSample'/'Sam Desample'). Two merges shipped the wrong spelling before this was pinned
    down, so the rules are:

      * EXACT case wins. A member reproduced character-for-character resolves to itself, whatever
        order the members arrive in and whatever else matches case-insensitively.
      * A case-insensitive match resolves ONLY when it is unambiguous. An answer that
        case-insensitively matches two or more members ('ann mcexample' against both spellings) is
        REFUSED (decision None -> the caller raises and re-asks) — never resolved by list order,
        which is itself unstable (`rank()` slugs the name, so case-variants tie and the tie-break
        degenerates to note counts).
      * LONGEST match still wins, with the exact/insensitive preference applied WITHIN each length
        tier — not as two full passes, which would let a SHORTER exact-case member beat a LONGER
        case-insensitive one and invert longest-wins.
      * The same contract governs the `only:` list, which `apply_people_cluster` resolves against
        `members` (exact first, then case-insensitive): `only:` names the member to MERGE, so
        picking its case-variant merges — and rewrites the participants of — the wrong person.

    Trailing text that is NOT one of those labelled clauses returns None instead of being taken as
    a role. That is deliberate: a conversational answer ('Samuel Specimen and Sam Specimen are the same
    but S. Specimen is someone else') would otherwise be written verbatim into the registry's role
    cell. Refusing costs one retry; guessing writes a fabricated role (CLAUDE.md: never fabricate).
    """
    raw = (answer or "").strip()
    if not raw:
        return None, None, None, ""
    # (1) an EXACT member name — including a `candidates` string pasted back verbatim — is
    # unambiguous, so it is resolved BEFORE anything else.
    bare = re.sub(r'\(\s*\d+\s+notes?\s*\)$', '', raw).strip()
    hit, ambiguous = _match_member(members, bare, prefix=False)
    if ambiguous:
        return None, None, None, ""
    if hit:
        return "canonical", hit, None, ""
    explicit = re.match(r'^(?:canonical|yes|same)\b\s*[—–:=,-]*\s*(.*)$', raw, re.I)
    rest = explicit.group(1).strip() if explicit else raw
    # (2) longest member prefix wins, so a member that is a prefix of another ('Sam Specimen' vs
    # 'Sam Specimen Jr') cannot shadow it. Checked BEFORE the skip keywords so a real name whose
    # first token merely looks like one ('No Example — …', 'Noisette Placeholder') is not read as "no".
    hit, ambiguous = _match_member(members, rest, prefix=True)
    if ambiguous:
        return None, None, None, ""
    if hit is None:
        if re.match(r'^(skip|no\b|not\b|none\b|distinct|different|noise)', _low(raw)) \
                or "not one person" in _low(raw):
            return "skip", None, None, ""
        return None, None, None, ""
    tail = re.sub(r'^\(\s*\d+\s+notes?\s*\)', '', rest[len(hit):].strip()).strip(" —–-:;,")
    role, only = "", None
    for seg in (s.strip() for s in tail.split(";") if s.strip()):
        m = re.match(r'^(?:merge\s+)?only\s*[:=]\s*(.+)$', seg, re.I)
        if m:
            only = [x.strip() for x in re.split(r'[,|]', m.group(1)) if x.strip()]
            continue
        m = re.match(r'^role\s*[:=]\s*(.+)$', seg, re.I)
        if m:
            role = m.group(1).strip()
            continue
        return None, None, None, ""          # unlabelled trailing prose — refuse, never guess
    return "canonical", hit, only, role


def _promote_decision(answer):
    m = re.search(r'alias of\s+(.+)$', answer, re.I)
    if m:
        return "alias", m.group(1).strip().rstrip(".")
    a = _low(answer)
    if re.match(r'^(noise|skip|not a|ignore|drop)', a):
        return "noise", None
    role = re.sub(r'^add\b[\s:—–-]*', '', answer, flags=re.I).strip()
    if a.startswith("add"):
        return "add", role
    if a and a not in ("yes", "confirm"):
        return "add", answer.strip()
    return None, None


def _verify_decision(answer):
    a = _low(answer)
    m = re.search(r'(?:merge (?:into|with)?|same as)\s+(.+)$', answer, re.I)
    if "merge" in a or "same as" in a:
        target = m.group(1).strip().rstrip(".") if m else None
        return "merge", target
    if re.match(r'^(remove|not a real|not a distinct|noise|delete)', a):
        return "remove", None
    mr = re.search(r'rename to\s+(.+)$', answer, re.I)
    if mr:
        return "rename", mr.group(1).strip().rstrip(".")
    if a.startswith("correct") or a.startswith("fix"):
        text = re.sub(r'^(corrected|correct|fix)\b[\s:—–-]*', '', answer, flags=re.I).strip()
        return "correct", text
    if a.startswith("confirm") or a.startswith("yes"):
        return "confirm", None
    # substantive non-keyword text = a corrected role
    if a:
        return "correct", answer.strip()
    return None, None


def _domain_decision(answer):
    a = _low(answer)
    m = re.search(r'move(?:\s+it)?(?:\s+to)?\s+([a-z][a-z-]+)', a)
    if m and m.group(1) in DOMAINS:
        return "move", m.group(1)
    if a in DOMAINS:
        return "move", a
    if re.match(r'^(correct|confirm|clear|keep|domain is correct|yes|right)', a):
        return "correct", None
    # An answer that names a move TARGET the store has no domain for is reported as such rather than
    # as a generic parse failure — the domain list has one owner (_manifest/constants/domains.json via
    # _lib.constants.DOMAINS) and the refusal should say what it checked against. Reached LAST on
    # purpose, so "keep — do not move to another domain" still resolves to `correct` as it always did.
    if m:
        return "unknown-domain", m.group(1)
    return None, None


def _thin_decision(answer):
    a = _low(answer)
    if a.startswith("archive"):
        return "archive", None
    if a.startswith("enrich"):
        return "enrich", re.sub(r'^enrich\b[\s:—–-]*', '', answer, flags=re.I).strip()
    if a.startswith("keep") or a.startswith("stub") or a == "":
        return "keep", None
    return "enrich", answer.strip()


# ======================================================================================
# recipes — each returns a Plan (pure; no disk writes)
# ======================================================================================
def _people_row_or_die(pd, name):
    hit = pd.find_row(name)
    if not hit:
        raise WritebackError(f"no row for '{name}' in {PEOPLE_REL}")
    return hit


def _plan_add_alias(store, variant, canonical, recipe="people-dedup", item_id=""):
    """Shared alias write-back: add `variant` as an alias of `canonical` in people.md, fold the
    variant's own row if it has one, and rewrite participants + [[variant]] links to `canonical`.

    `recipe`/`item_id` are used only to build the human-readable `reason` on each sanctioned-override
    ledger entry the participants rewrite needs (see plan_participant_overrides)."""
    pd = PeopleDoc(store.read(PEOPLE_REL))
    ctbl, cidx = _people_row_or_die(pd, canonical)
    # if the variant already owns a row (the 1b auto-added case), fold it: carry its own aliases over
    # to the canonical row, add the variant name itself as an alias, then remove the variant row.
    # Otherwise just add the variant name as an alias of the canonical row. All appends happen before
    # the removal (in-place line edits don't shift indices), so ctbl/cidx and vidx stay valid.
    vhit = pd.find_row(variant)
    if vhit and vhit[1] != cidx:
        vtbl, vidx = vhit
        for extra in _parse_alias_cell(pd.get_cell(vtbl, vidx, "aliases")):
            pd.append_alias(ctbl, cidx, extra)
        pd.append_alias(ctbl, cidx, variant)
        pd.remove_row(vtbl, vidx)
    else:
        pd.append_alias(ctbl, cidx, variant)

    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    renames, msgs = plan_people_rename(store, variant, canonical)
    _plan_rename_bookkeeping(plan, store, renames, [variant], canonical,
                             f"{recipe}: '{variant}' -> alias of '{canonical}'"
                             + (f" via clarify item {item_id}" if item_id else ""), msgs)
    n = len(plan.writes) - 1
    plan.edit = f"add-alias: '{variant}' -> alias of {canonical}; rewrote {n} note(s)"
    return plan


def _parse_alias_cell(cell):
    """Split an aliases cell into individual alias tokens (strip quotes + trailing annotations)."""
    if not cell or cell == "—":
        return []
    out = []
    for part in re.split(r'[;,]', cell):
        p = part.strip()
        m = re.match(r'^"([^"]+)"', p)
        if m:
            out.append(m.group(1))
        elif p and "@" not in p and "(" not in p:
            out.append(p)
    return out


def apply_people_dedup(store, item, answer):
    ev = item.get("evidence", {})
    variant = ev.get("name") or item.get("entity")
    canonical = ev.get("canonical") or item.get("entity")
    decision = _dedup_decision(answer)
    if decision is None:
        raise WritebackError("dedup answer must indicate 'yes/same' or 'no/distinct'")
    if decision == "distinct":
        plan = Plan()
        plan.status = "answered"
        plan.edit = f"distinct: '{variant}' is NOT {canonical} (no store edit; promote separately)"
        return plan
    return _plan_add_alias(store, variant, canonical,
                           recipe="people-dedup", item_id=item.get("id", ""))


# Role text for a cluster-promoted row. Choosing which SPELLING is canonical says nothing about who
# the person is, so the row lands unconfirmed (Verify? = true, matching the store's 32 existing
# verify rows) with an explicitly-unknown role — build_clarify_queue then asks for both as the next
# question (a people-verify item). Never invent a role here: CLAUDE.md "Never fabricate".
CLUSTER_ROLE_TBD = "role TBD (promoted via /clarify cluster)"


def _plan_promote_cluster(store, canonical, others, domain, role, item_id=""):
    """Promote `canonical` as a NEW people.md row and fold `others` into it as aliases.

    This is the people-dedup recipe's missing half: `_plan_add_alias` requires the canonical to
    ALREADY have a row (it edits one), which is exactly why an all-unregistered variant pair was
    unanswerable. Everything downstream of the row insert is shared with that recipe — the same
    append_alias, the same rename propagation, and the SAME sanctioned-override ledger entries, so
    check.sh's frozen-field gate accepts the participants rewrites here for the same reason."""
    today = datetime.date.today().isoformat()
    pd = PeopleDoc(store.read(PEOPLE_REL))
    if pd.find_row_ci(canonical):
        raise WritebackError(
            f"'{canonical}' already has a row in {PEOPLE_REL} — this cluster is no longer fully "
            f"unregistered. Re-run build_clarify_queue.py; it will emit answerable people-dedup "
            f"item(s) against that row instead.")
    # ... and the SAME staleness check for the members being folded in as aliases. Only the
    # canonical was guarded before, so a member registered between queue build and apply became a
    # canonical row AND someone else's alias at once — a registry contradiction parse_people would
    # resolve arbitrarily. A dedup item against the existing row is the right instrument for that.
    registered = [o for o in others if pd.find_row_ci(o)]
    if registered:
        raise WritebackError(
            f"cluster member(s) {registered} already have a row in {PEOPLE_REL} — this cluster is no "
            f"longer fully unregistered, and folding them in as aliases of '{canonical}' would make "
            f"one name both a canonical row and another row's alias. Re-run build_clarify_queue.py; "
            f"it will emit answerable people-dedup item(s) against those rows instead.")
    pd.insert_promote_row(canonical, role or CLUSTER_ROLE_TBD, domain, today, verify="true")
    tbl, idx = _people_row_or_die(pd, canonical)
    for other in others:                       # in-place cell edits — tbl/idx stay valid throughout
        pd.append_alias(tbl, idx, other)

    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    renames, msgs = plan_people_renames(store, others, canonical)
    _plan_rename_bookkeeping(
        plan, store, renames, others, canonical,
        "people-cluster: promoted '%s'; %s -> alias(es)" % (canonical, ", ".join(repr(o) for o in others))
        + (f" via clarify item {item_id}" if item_id else ""), msgs)
    plan.edit = (f"promote-cluster: '{canonical}' -> new verify:true row with {len(others)} alias(es) "
                 f"({', '.join(others)}); rewrote {len(plan.writes) - 1} note(s)")
    return plan


def apply_people_cluster(store, item, answer):
    ev = item.get("evidence", {})
    members = [m.get("name") for m in ev.get("members", []) if isinstance(m, dict) and m.get("name")]
    if len(members) < 2:
        raise WritebackError("people-cluster item carries no usable evidence.members")
    decision, canonical, only, role = _cluster_decision(answer, members)
    if decision == "skip":
        plan = Plan()
        plan.status = "answered"
        plan.edit = (f"not-one-person: the {len(members)} spellings "
                     f"({', '.join(members)}) left exactly as they are (no store edit)")
        return plan
    if decision != "canonical":
        raise WritebackError(
            'cluster answer must name one member as canonical ("canonical: <Name>", optionally '
            'followed by "; role: <text>" and/or "; only: <Name>, <Name>" — unlabelled trailing '
            'text is refused rather than written into the registry as a role) or say '
            '"not one person" / "skip". Members: ' + "; ".join(members))
    others = [m for m in members if m != canonical]
    if only is not None:
        resolved = []
        for want in only:
            # Exact case first, same contract as _cluster_decision: an `only:` list naming one of
            # two case-variant members must merge the one NAMED, not whichever sorts first.
            hit = (next((m for m in members if m == want), None)
                   or next((m for m in members if m.lower() == want.lower()), None))
            if hit is None:
                raise WritebackError(f"'{want}' is not a member of this cluster; members: "
                                     + "; ".join(members))
            if hit != canonical and hit not in resolved:
                resolved.append(hit)
        if not resolved:
            raise WritebackError("'only:' named no member other than the canonical — nothing to merge")
        others = resolved
    if not others:
        raise WritebackError("cluster has no member to merge into the canonical")
    return _plan_promote_cluster(store, canonical, others, ev.get("domain", ""), role,
                                 item.get("id", ""))


def apply_people_promote(store, item, answer):
    name = item.get("entity") or item.get("evidence", {}).get("name")
    domain = item.get("evidence", {}).get("domain", "")
    decision, detail = _promote_decision(answer)
    if decision == "alias":
        return _plan_add_alias(store, name, detail,
                               recipe="people-promote", item_id=item.get("id", ""))
    if decision == "noise":
        plan = Plan()
        plan.status = "skipped"
        plan.edit = f"noise/skip: '{name}' not promoted"
        return plan
    if decision != "add":
        raise WritebackError("promote answer must be 'add — <role>', 'alias of X', or 'noise'")
    if not detail:
        raise WritebackError("promote 'add' requires a role/affiliation in the answer")
    today = datetime.date.today().isoformat()
    pd = PeopleDoc(store.read(PEOPLE_REL))
    pd.insert_promote_row(name, detail, domain, today)
    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    plan.edit = f"promote: added '{name}' ({detail}) to 'Promoted via /clarify ({today})'"
    return plan


def apply_people_verify(store, item, answer):
    name = item.get("entity") or item.get("evidence", {}).get("name")
    decision, detail = _verify_decision(answer)
    if decision == "merge":
        if not detail:
            raise WritebackError("verify 'merge' needs a target: 'merge into <Name>'")
        return _plan_add_alias(store, name, detail,
                               recipe="people-verify", item_id=item.get("id", ""))
    if decision == "remove":
        plan = Plan()
        plan.status = "skipped"
        plan.edit = f"remove/skip: '{name}' left for human review (no row deleted)"
        return plan
    if decision == "rename":
        return _plan_rename_person(store, name, detail, confirm_verify=True,
                                   item_id=item.get("id", ""))
    pd = PeopleDoc(store.read(PEOPLE_REL))
    tbl, idx = _people_row_or_die(pd, name)
    if decision == "correct":
        if not detail:
            raise WritebackError("verify 'correct' needs the corrected role text")
        detail = _preserve_alias_tokens(pd.get_cell(tbl, idx, "role"), detail)
        pd.set_cell(tbl, idx, "role", detail)
        pd.set_verify_confirmed(tbl, idx)
        edit = f"verify-correct: '{name}' role -> '{detail}'; Verify? -> —"
    elif decision == "confirm":
        pd.set_verify_confirmed(tbl, idx)
        edit = f"verify-confirm: '{name}' Verify? -> —"
    else:
        raise WritebackError("verify answer must be confirm / correct / merge / remove / rename")
    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    plan.edit = edit
    return plan


def _plan_rename_person(store, old_name, new_name, confirm_verify=False, item_id=""):
    pd = PeopleDoc(store.read(PEOPLE_REL))
    tbl, idx = _people_row_or_die(pd, old_name)
    pd.set_cell(tbl, idx, "name", new_name)
    if confirm_verify:
        pd.set_verify_confirmed(tbl, idx)
    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    renames, msgs = plan_people_rename(store, old_name, new_name)
    # same participants-drop seam as the dedup path (this recipe reuses plan_people_rename)
    _plan_rename_bookkeeping(plan, store, renames, [old_name], new_name,
                             f"people-verify: renamed '{old_name}' -> '{new_name}'"
                             + (f" via clarify item {item_id}" if item_id else ""), msgs)
    plan.edit = f"rename: '{old_name}' -> '{new_name}'; rewrote {len(plan.writes) - 1} note(s)"
    return plan


def apply_people_role(store, item, answer):
    name = item.get("entity") or item.get("evidence", {}).get("name")
    hint = item.get("evidence", {}).get("hint", "")
    pd = PeopleDoc(store.read(PEOPLE_REL))
    tbl, idx = _people_row_or_die(pd, name)
    if _low(answer).startswith("confirm"):
        role = _strip_tentative(hint or pd.get_cell(tbl, idx, "role"))
    else:
        role = _strip_tentative(answer)
    if not role:
        raise WritebackError("role answer resolved to empty text")
    role = _preserve_alias_tokens(pd.get_cell(tbl, idx, "role"), role)
    pd.set_cell(tbl, idx, "role", role)
    plan = Plan()
    plan.writes.append((PEOPLE_REL, pd.render()))
    plan.edit = f"set-role: '{name}' role -> '{role}'"
    return plan


def apply_domain(store, item, answer):
    ev = item.get("evidence", {})
    note_rel = ev.get("path") or item.get("entity")
    decision, target = _domain_decision(answer)
    if decision == "correct":
        txt = store.read(note_rel)
        txt2, ch = _strip_flag_tags(txt, FLAG_TAGS)
        plan = Plan()
        if ch:
            plan.writes.append((note_rel, txt2))
            plan.edit = f"domain-correct: cleared flag tag on {note_rel}"
        else:
            plan.messages.append(f"no flag tag present on {note_rel}")
            plan.edit = f"domain-correct: no flag tag to clear on {note_rel}"
        return plan
    if decision == "unknown-domain":
        raise WritebackError(
            f"'{target}' is not a store domain, so there is nowhere to move {note_rel}. The "
            f"canonical list (_manifest/constants/domains.json) is: {', '.join(DOMAINS)}.")
    if decision == "move":
        new_rel = os.path.join(target, os.path.basename(note_rel))
        # The destination text is assembled HERE rather than via plan_move_note's own
        # new_domain/drop_tags derivation (the two are mutually exclusive by design), because the
        # ledger entries must be minted from EXACTLY the bytes that will be written. Same
        # guarded-caller seam edit_note_frontmatter.py's re-domain path uses; the helpers are
        # plan_move_note's own, so this is not a second derivation of "what a domain move writes".
        try:
            content = store.read(note_rel)
        except OSError as e:
            raise WritebackError(f"note not found: {note_rel} ({e})")
        content, _ = _set_domain_field(content, target)
        content, dropped_flag = _strip_flag_tags(content, FLAG_TAGS)
        reason = (f"clarify item {item.get('id', '') or '<no id>'} answered {answer!r}: "
                  f"domain -> '{target}', note moved {note_rel} -> {new_rel}"
                  + (f", flag tag(s) {'/'.join(FLAG_TAGS)} cleared" if dropped_flag else ""))
        entries, msgs = plan_domain_overrides(store, note_rel, new_rel, content, target, reason)
        plan = plan_move_note(store, note_rel, new_rel, content=content, overrides=entries)
        plan.messages.extend(msgs)
        # The recipe drops only the two DOMAIN-CONFIRM flags. A note filed out of `unsorted/` keeps
        # any `unsorted`/`flagged` tag it carries — off-vocab for its new home but not a lint error,
        # so nothing else would ever mention it. Say so; clearing it is a separate deliberate edit
        # (edit_note_frontmatter.py --set tags=...), not something to infer from a domain answer.
        m_tags = re.search(r'^tags:\s*(\[.*?\])\s*$', content, re.M)
        stale = [t for t in ("unsorted", "flagged")
                 if t in _members(m_tags.group(1) if m_tags else "")]
        if stale:
            plan.messages.append(
                f"{new_rel} still carries triage tag(s) {stale} — stale now that "
                f"the domain is decided, but NOT cleared here (a domain answer sanctions the "
                f"domain:/tags: re-stamp it names, nothing more). Clear them deliberately with "
                f"edit_note_frontmatter.py --set tags=\"[...]\" if you want them gone.")
        plan.edit = f"domain-move: {note_rel} -> {new_rel} (domain={target}, flag cleared)"
        return plan
    raise WritebackError("domain answer must be 'correct'/'clear' or 'move to <domain>'")


def apply_thin_source(store, item, answer):
    ev = item.get("evidence", {})
    note_rel = ev.get("path") or item.get("entity")
    decision, detail = _thin_decision(answer)
    if decision == "keep":
        plan = Plan()
        plan.status = "answered"
        plan.edit = f"thin-keep: {note_rel} kept as-is (flag left in place)"
        return plan
    if decision == "enrich":
        if not detail:
            raise WritebackError("thin 'enrich' needs the detail text to add")
        txt = store.read(note_rel)
        addition = f"\n\n## Manual addition (source: manual)\n\n{detail}\n"
        new_txt = txt.rstrip("\n") + addition
        plan = Plan()
        plan.writes.append((note_rel, new_txt))
        plan.edit = f"thin-enrich: appended manual detail to {note_rel}"
        return plan
    if decision == "archive":
        new_rel = os.path.join("_archive", os.path.basename(note_rel))
        content = store.read(note_rel)
        plan = Plan()
        plan.moves.append((note_rel, new_rel, content))
        # inbound related:/[[slug]] refs would dangle after archiving — surface, don't rewrite
        slug = os.path.splitext(os.path.basename(note_rel))[0]
        inbound = [store.rel(fp) for fp in store.link_files()
                   if f"[[{slug}]]" in open(fp, encoding="utf-8").read()
                   and os.path.abspath(fp) != os.path.abspath(store.path(note_rel))]
        if inbound:
            plan.messages.append(f"WARNING: {len(inbound)} note(s) still [[{slug}]]-link the archived "
                                 f"note (would dangle): {', '.join(inbound[:8])}")
        plan.edit = f"thin-archive: {note_rel} -> {new_rel} (never deleted)"
        return plan
    raise WritebackError("thin answer must be keep / enrich / archive")


_RECIPES = {
    "people-dedup": apply_people_dedup,
    "people-cluster": apply_people_cluster,
    "people-promote": apply_people_promote,
    "people-verify": apply_people_verify,
    "people-role": apply_people_role,
    "domain": apply_domain,
    "thin-source": apply_thin_source,
}


# ======================================================================================
# top-level apply (plan -> dry-run report OR transactional execute+verify)
# ======================================================================================
def _run_lint(root):
    r = subprocess.run([sys.executable, os.path.join(_SCRIPTS, "lint_store.py")],
                       cwd=root, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def _gate_profile(store, rel):
    """The preserve profile check.sh would apply to `rel` once it is modified in place, or None when
    check.sh applies no FIELD gate to it. Mirrors check.sh's own routing (classify by the note's
    source AT HEAD): dossiers -> `dossier`; a domain note whose HEAD source is a distilled connector
    -> `redistill`; everything else -> None (untracked notes have nothing to preserve against, and
    non-distilled sources hit check.sh's blunt SUSPICIOUS halt, which names no field — see the
    non-distilled WARNING in plan_participant_overrides)."""
    if not rel.endswith(".md"):
        return None
    top = rel.split("/", 1)[0]
    if top == "dossiers":
        return "dossier"
    if top not in DOMAINS:
        return None
    head_txt = git_show_head(store.root, rel)
    if head_txt is None:
        # No HEAD blob usually means a genuinely new note (nothing to preserve against). But it is
        # ALSO true of a note already MOVED in this uncommitted window, and returning None there
        # made the in-transaction gate silently pass a SECOND move of the same note — the exact
        # "guard narrow in one dimension" class this codebase has now been bitten by twice. Fall
        # back to the note's pre-move path so the second move is gated against the ORIGINAL, as
        # check.sh's `--moved` already does. (check.sh caught it either way; this stops the writer
        # from reporting OK on a change the gate would then reject.)
        src = _rename_source(store.root, rel)
        if src is None:
            return None
        head_txt = git_show_head(store.root, src)
        if head_txt is None:
            return None
    head, _ = split_frontmatter(head_txt)
    if head is None or get_field(head, "source") not in REDISTILL_SOURCES:
        return None
    return "redistill"


def _run_preserve_gate(store, rels, moves=()):
    """Run the frozen-frontmatter gate over the just-written paths and MOVE PAIRS, returning
    [(label, profile, violations)].

    `moves` is [(src_rel, dst_rel)] and is NOT interchangeable with a path list (H4): a moved note
    has no `HEAD:<dst>` blob, so passing the destination alone made `_gate_profile` return None and
    the gate skip it entirely — `plan_move_note(..., new_domain=X, drop_tags=...)` re-stamped
    `domain:` and `tags:`, both FROZEN under the redistill profile, and this reported nothing. The
    identical change made in place is hard-refused, so the weaker operation was the one that passed.
    A pair is judged by what the note WAS at its ORIGINAL path — the profile from `HEAD:<src>`'s
    `source:`, the diff against `HEAD:<src>`'s frontmatter.

    WHY this runs inside the transaction: lint_store.py knows nothing about frozen fields, so before
    this, ANY frozen-field change the ledger did not excuse — a collateral `[[Person]]` rewrite
    inside `related:`, a flag tag stripped off a distilled note — landed fully on disk, marked the
    queue item answered and returned success, leaving a store that check.sh would then refuse to
    accept. This is the integration check for that seam: same function check.sh calls
    (verify_frontmatter_preserved.check_path), same profiles, same ledger, run while a rollback is
    still possible. It is deliberately NOT a re-implementation — a local copy would be free to drift
    from the gate it is supposed to predict."""
    overrides = load_overrides(store.root)
    failures = []
    for rel in rels:
        prof = _gate_profile(store, rel)
        if prof is None:
            continue
        viols = _gate_check_path(store.root, rel, prof, overrides)
        if viols:
            failures.append((rel, prof, viols))
    for src, dst in moves:
        prof = _gate_profile(store, src)
        if prof is None:
            continue
        viols = _gate_check_path(store.root, dst, prof, overrides, baseline=src)
        if viols:
            failures.append((f"{src} -> {dst}", prof, viols))
    return failures


def _format_gate_failures(failures):
    lines = []
    for rel, prof, viols in failures:
        lines.append(f"  [{prof}] {rel}")
        for v in viols:
            if v["kind"] == "removed":
                lines.append(f"      {v['field']}: members REMOVED {v.get('removed')} "
                             f"(old={v['old']!r} new={v['new']!r}) — append-only")
            else:
                lines.append(f"      {v['field']}: CHANGED old={v['old']!r} -> new={v['new']!r}")
    lines.append("These fields are FROZEN for in-place rewrites, and no sanctioned-override ledger "
                 "entry covers the change above.")
    lines.append("A deliberate frozen-field change is made with scripts/edit_note_frontmatter.py "
                 "(guarded, ledger-recorded), not as a side effect of a clarify answer.")
    return "\n".join(lines)


def build_plan(store, item, answer):
    cat = item.get("category")
    fn = _RECIPES.get(cat)
    if not fn:
        raise WritebackError(f"unknown category {cat!r}")
    return fn(store, item, answer)


def dry_run_report(plan, item, answer):
    lines = [f"DRY RUN — id={item.get('id')} category={item.get('category')}",
             f"  answer: {answer}",
             f"  -> {plan.edit}",
             f"  queue: status -> {plan.status}"]
    files = plan.touched()
    if files:
        lines.append(f"  WOULD CHANGE {len(files)} file(s):")
        for rel, _ in plan.writes:
            lines.append(f"    WRITE  {rel}")
        for src, dst, _ in plan.moves:
            lines.append(f"    MOVE   {src} -> {dst}")
    else:
        lines.append("  WOULD CHANGE 0 store files (queue/log bookkeeping only)")
    # L3: apply() rewrites the queue WHOLESALE, but plan.touched() is note-scoped, so the dry-run
    # never named the one file it always rewrites. Report it explicitly.
    lines.append(f"  WOULD REWRITE {QUEUE_REL} (whole file; this item's status -> {plan.status})")
    lines.append(f"  WOULD APPEND clarify-log.jsonl entry (files: {files or ['—']})")
    if plan.overrides:
        n = len(plan.overrides)
        lines.append(f"  WOULD APPEND {n} sanctioned-override entr{'y' if n == 1 else 'ies'} "
                     f"to {OVERRIDES_LEDGER}:")
        for e in plan.overrides:
            lines.append(f"    OVERRIDE {e['path']}")
            lines.append(f"             {e['field']}: {e['old']!r} -> {e['new']!r}")
            lines.append(f"             head_sha={e['head_sha']}  reason: {e['reason']}")
    for msg in plan.messages:
        lines.append(f"  NOTE: {msg}")
    lines.append("  (nothing written)")
    return "\n".join(lines)


def move_note(root, old_rel, new_rel, new_domain=None, drop_tags=(), verify=True,
              restamp_title=True, content=None, overrides=()):
    """Standalone guarded note move/rename: relocate old_rel -> new_rel (optionally re-stamping
    domain: + stripping flag tags), re-stamping `title:` when the slug changes (M6;
    `restamp_title=False` opts out) and rewriting every inbound [[old_slug]], all through one
    rollback-on-lint-failure transaction. Importable for the domain-move recipe, the future review
    server, and a direct rename. Returns a result dict; never touches the queue/log.

    `content`/`overrides` pass straight through to `plan_move_note` — the guarded-caller seam used
    by edit_note_frontmatter.py's re-domain path; see that docstring."""
    store = Store(root)
    plan = plan_move_note(store, old_rel, new_rel, new_domain=new_domain, drop_tags=drop_tags,
                          restamp_title=restamp_title, content=content, overrides=overrides)
    tx = _Tx(store.root)
    try:
        for rel, content in plan.writes:
            tx.write(rel, content)
        for src, dst, content in plan.moves:
            tx.move(src, dst, content)
        # The override ledger goes through the SAME transaction as the note it excuses (mirroring
        # `apply`), and BEFORE the gate below — so the rename's own sanctioned title/aliases
        # transition is already excused when the gate reads it, and a rolled-back move leaves no
        # orphan entry behind.
        if plan.overrides:
            tx.write(OVERRIDES_LEDGER, _overrides_appended(store, plan.overrides))
        if verify:
            # Frozen-field gate first (cheap, and the failure names a field): a slug rewrite lands in
            # the linkers' `related:`/`supersedes:`, which are frozen on distilled-source notes.
            bad = _run_preserve_gate(store, [p for p, _ in plan.writes],
                                     moves=[(s, d) for s, d, _ in plan.moves])
            if bad:
                tx.rollback()
                raise WritebackError("frozen-frontmatter gate rejected the move (rolled back):\n"
                                     + _format_gate_failures(bad))
            ok, out = _run_lint(store.root)
            if not ok:
                tx.rollback()
                raise WritebackError(f"lint_store rejected the move (rolled back):\n{out}")
    # BaseException, not Exception: `_run_lint` shells a ~7,700-note lint, so a Ctrl-C (or a
    # SystemExit) lands here far more often than any error would — and an `except Exception` let it
    # skip the rollback entirely, leaving every rewritten file on disk. Verified before the fix.
    except BaseException:
        tx.rollback()
        raise
    return {"moved": plan.moves, "rewrote": [p for p, _ in plan.writes],
            "overrides": plan.overrides, "messages": plan.messages}


def apply(root, item, answer, dry_run=False, verify=True):
    """Apply `answer` to clarify-queue `item` (a dict, or an id string). Returns a result dict.
    dry_run: assemble + report, touch nothing. verify: lint after writing, roll back on failure."""
    store = Store(root)
    if isinstance(item, str):
        item = find_item(store, item)
    plan = build_plan(store, item, answer)
    files = plan.touched()

    if dry_run:
        return {"id": item.get("id"), "category": item.get("category"), "status": plan.status,
                "edit": plan.edit, "files": files, "messages": plan.messages,
                "overrides": plan.overrides, "dry_run": True,
                "report": dry_run_report(plan, item, answer)}

    log_entry = {"id": item.get("id"), "answer": answer, "edit": plan.edit, "files": files}
    tx = _Tx(store.root)
    try:
        for rel, content in plan.writes:
            tx.write(rel, content)
        for src, dst, content in plan.moves:
            tx.move(src, dst, content)
        # The override ledger goes through the SAME transaction as the notes it excuses: a rolled-back
        # merge must not leave an orphan entry that could later excuse an unrelated change.
        if plan.overrides:
            tx.write(OVERRIDES_LEDGER, _overrides_appended(store, plan.overrides))
        tx.write(QUEUE_REL, _queue_with_status(store, item.get("id"), plan.status, answer))
        tx.write(LOG_REL, _log_appended(store, log_entry))
        if verify:
            # Frozen-field gate BEFORE lint: it is the cheaper check, it names the offending field,
            # and it runs after the override ledger was written above, so this recipe's own
            # sanctioned participants transitions are already excused — anything it still flags is a
            # change nothing sanctions, which check.sh would reject at commit time. Move pairs go in
            # as PAIRS: a destination path alone has no HEAD blob, so the gate skipped it (H4).
            bad = _run_preserve_gate(store, [p for p, _ in plan.writes],
                                     moves=[(s, d) for s, d, _ in plan.moves])
            if bad:
                tx.rollback()
                raise WritebackError("frozen-frontmatter gate rejected the edits (rolled back):\n"
                                     + _format_gate_failures(bad))
            ok, out = _run_lint(store.root)
            if not ok:
                tx.rollback()
                raise WritebackError(f"lint_store rejected the edits (rolled back):\n{out}")
    # BaseException, not Exception — see move_note: a Ctrl-C during the lint used to skip rollback
    # and leave every rewritten note, the ledger append and the answered queue item on disk.
    except BaseException:
        tx.rollback()
        raise
    return {"id": item.get("id"), "category": item.get("category"), "status": plan.status,
            "edit": plan.edit, "files": files, "messages": plan.messages,
            "overrides": plan.overrides, "dry_run": False}


def migrate_promoted_tables(root, dry_run=False, verify=True):
    """Run `PeopleDoc.migrate_promoted_alias_column` over the store's real people.md, through the
    same guarded-writer transaction every recipe uses (write -> lint -> roll back on failure).

    Idempotent and safe to re-run: a Promoted table that already has the column is skipped, so a
    second run is a no-op and reports 0 rows."""
    store = Store(root)
    pd = PeopleDoc(store.read(PEOPLE_REL))
    moved = pd.migrate_promoted_alias_column()
    out = pd.render()
    changed = out != store.read(PEOPLE_REL)
    res = {"changed": changed, "rows": moved,
           "aliases": sum(len(a) for _, a in moved), "dry_run": dry_run}
    if not changed or dry_run:
        return res
    tx = _Tx(store.root)
    try:
        tx.write(PEOPLE_REL, out)
        if verify:
            ok, lint_out = _run_lint(store.root)
            if not ok:
                tx.rollback()
                raise WritebackError(f"lint_store rejected the migration (rolled back):\n{lint_out}")
    except BaseException:
        tx.rollback()
        raise
    return res


# ======================================================================================
# CLI
# ======================================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply an answer to a clarify-queue item (Feature E).")
    ap.add_argument("--id", help="clarify-queue item id")
    ap.add_argument("--answer", help="the human's verbatim answer (routes per the contract)")
    ap.add_argument("--migrate-promoted-tables", action="store_true",
                    help="one-shot: give every '## Promoted via /clarify' table an "
                         "'Aliases / emails' column and move embedded (alias: \"…\") tokens into it")
    ap.add_argument("--dry-run", action="store_true", help="print the full file list; touch nothing")
    ap.add_argument("--no-verify", action="store_true", help="skip the post-write lint_store check")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS), help="store root (tests point here)")
    a = ap.parse_args(argv)
    if a.migrate_promoted_tables:
        try:
            res = migrate_promoted_tables(a.root, dry_run=a.dry_run, verify=not a.no_verify)
        except WritebackError as e:
            print(f"clarify_writeback: ERROR: {e}", file=sys.stderr)
            return 2
        verb = "WOULD move" if a.dry_run else "moved"
        print(f"clarify_writeback: {'OK' if res['changed'] else 'no-op'} — {verb} "
              f"{res['aliases']} alias(es) on {len(res['rows'])} row(s) into the new column")
        for name, aliases in res["rows"]:
            print(f"    {name}: {', '.join(aliases)}")
        return 0
    if not a.id or a.answer is None:
        ap.error("--id and --answer are required (or use --migrate-promoted-tables)")
    try:
        res = apply(a.root, a.id, a.answer, dry_run=a.dry_run, verify=not a.no_verify)
    except WritebackError as e:
        print(f"clarify_writeback: ERROR: {e}", file=sys.stderr)
        return 2
    if a.dry_run:
        print(res["report"])
        return 0
    print(f"clarify_writeback: OK — {res['edit']}")
    print(f"  status: {res['status']}   files: {res['files'] or '—'}")
    if res.get("overrides"):
        n = len(res["overrides"])
        fields = ", ".join(sorted({e["field"] for e in res["overrides"]}))
        print(f"  ledger: +{n} sanctioned-override entr{'y' if n == 1 else 'ies'} "
              f"({fields}) in {OVERRIDES_LEDGER} — REVIEW each line before committing")
        for e in res["overrides"]:
            print(f"    {e['path']}: {e['old']!r} -> {e['new']!r}")
    for msg in res["messages"]:
        print(f"  {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
