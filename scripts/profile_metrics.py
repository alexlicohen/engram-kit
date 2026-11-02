#!/usr/bin/env python3
"""engram-profile Phase 1 — the DETERMINISTIC metrics pack (M1-M6 + bias ledger).

Reads ONLY already-generated artifacts — no note bodies enter this process's *analysis*
except a deterministic grep for the M6 worklists (which emits path + matched line, never
bodies-into-context):

    _manifest/index.jsonl        one record per note (path,title,date,domain,subpath,tags,
                                 status,source,ref,participants,related,ingested)
    digests/decisions.md         decision-bearing notes  (### <date> · [dom] · [t](path))
    digests/action-items.md      action-bearing notes    (## <domain> / ### ... (path))
    digests/open-loops.md        open commitments w/ status + [[wikilink]]
    _manifest/backlinks.md       curated-inbound reverse index (curated notes)
    dossiers/*.md                synthesized one-pagers (inventory only)

Emits markdown + JSON sidecars to _profile/metrics/. Idempotent, deterministic given a
fixed store state, no network, no LLM. NOT gated by check.sh (writes only under _profile/).

The de-biasing method: never let raw note count stand in for attention. M2 puts
six independent estimators (E0 raw + E1..E5) side by side as SHARES per domain, so the
divergence between where the notes ARE and where the attention WENT is visible at a glance.

One-owner rule (repo CLAUDE.md): the domain list comes from _manifest/constants/domains.json
via _lib.constants — never hardcoded here.

Usage:  python3 scripts/profile_metrics.py
"""
import collections
import datetime
import glob
import json
import os
import re
import statistics
import subprocess
import sys

# scripts/ on sys.path[0] when run as `python3 scripts/profile_metrics.py` -> _lib importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS  # noqa: E402  (single owner of the domain list)
from _lib.actions import _self_row   # noqa: E402  (single owner of the people.md self row)


def _self_names(root):
    canonical, alias_field = _self_row(root)
    if not canonical:
        return []
    return [canonical] + re.findall(r'"([^"]+)"', alias_field or "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "_manifest", "index.jsonl")
DECISIONS = os.path.join(ROOT, "digests", "decisions.md")
ACTIONS = os.path.join(ROOT, "digests", "action-items.md")
OPENLOOPS = os.path.join(ROOT, "digests", "open-loops.md")
BACKLINKS = os.path.join(ROOT, "_manifest", "backlinks.md")
DOSSIERS_DIR = os.path.join(ROOT, "dossiers")
OUT = os.path.join(ROOT, "_profile", "metrics")

# ---------------------------------------------------------------------------------------
# Constants that shape the analysis (module-level so tests + the memo can re-cut them).
# ---------------------------------------------------------------------------------------

# Eras are trivially re-cuttable: (label, lo_year_inclusive_or_None, hi_year_inclusive_or_None).
# Illustrative uniform 4-year bins: set your own cut points (adopters re-cut these freely).
ERAS = [
    ("≤2014", None, 2014),
    ("2015-2018", 2015, 2018),
    ("2019-2022", 2019, 2022),
    ("2023+", 2023, None),
]

# The owner's own name forms (participants equal to one of these are the owner, not a correspondent):
# the people.md self row (single owner) — canonical name + quoted aliases.
OWNER_NAMES = {"the owner"} | set(_self_names(ROOT))

# E1 synchronous-time: granola-sourced OR meeting-tagged. ~45 min/calendared meeting assumed
# (only affects the ABSOLUTE hours column; the SHARE is assumption-free).
MEETING_MINUTES = 45
E1_MEETING_TAGS = {"meeting"}
E1_GRANOLA_SOURCE = "granola"

# E3 hand-authored: deliberate, self-initiated sources.
E3_SOURCES = {"obsidian", "manual", "claude-chat"}

# E4 human-conversation: chat sources + gmail threads the owner plausibly COMPOSED (heuristic below).
E4_CHAT_SOURCES = {"imessage", "whatsapp"}

# The composed-gmail heuristic (frontmatter-only — index.jsonl carries no bodies). Emitted into
# M2 so the Phase-2c calibration pass can measure its precision/recall and re-cut it.
COMPOSED_TAGS = {"decision", "meeting"}
COMPOSED_PART_MIN, COMPOSED_PART_MAX = 1, 2
TRANSACTIONAL_RE = re.compile(
    r"statement|confirmation|receipt|invoice|newsletter|alert|payment|"
    r"shipped|delivered|order",
    re.I,
)
COMPOSED_RULE_TEXT = (
    "source==gmail AND (%d-%d non-owner participants) AND (tags ∩ {decision,meeting} ≠ ∅) "
    "AND title NOT matching /%s/i"
) % (COMPOSED_PART_MIN, COMPOSED_PART_MAX, TRANSACTIONAL_RE.pattern)

# M6 grep patterns (case-insensitive). Path lists only — NOT interpreted here.
PRIORITY_RE = re.compile(
    r"my (top )?priorit|this year I|resolv(e|ution)|goal for|focus on|"
    r"step back from|say no to|priorit(y|ies)",
    re.I,
)
DECLINE_RE = re.compile(
    r"declin|passed on|said no|won'?t be able|not interested|will pass|decided against",
    re.I,
)

ESTIMATORS = ["E0", "E1", "E2", "E3", "E4", "E5"]
ESTIMATOR_LABELS = {
    "E0": "raw-count",
    "E1": "sync-time",
    "E2": "decision-density",
    "E3": "hand-authored",
    "E4": "human-conversation",
    "E5": "curation",
}

RUN_DATE = datetime.date.today()


# ---------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------

def load_index(path=INDEX):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def year_of(rec):
    """Return the int year from a YYYY-MM-DD date, or None if unparseable."""
    d = rec.get("date") or ""
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None


def month_of(rec):
    d = rec.get("date") or ""
    return d[:7] if len(d) >= 7 and d[:4].isdigit() and d[5:7].isdigit() else None


def era_of(year):
    if year is None:
        return None
    for label, lo, hi in ERAS:
        if (lo is None or year >= lo) and (hi is None or year <= hi):
            return label
    return None


def digest_paths(fname):
    """Distinct note paths referenced by `### ... [title](path.md)` headers in a digest."""
    paths = set()
    with open(fname, encoding="utf-8") as f:
        for line in f:
            if line.startswith("### "):
                m = re.search(r"\]\(([^)]+\.md)\)\s*$", line.rstrip())
                if m:
                    paths.add(m.group(1))
    return paths


def curated_slugs(path=BACKLINKS):
    """Slugs (note basenames) with >=1 curated inbound wikilink — the blocks BEFORE the
    `## True orphans` section in backlinks.md."""
    slugs = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("## True orphans"):
                break
            m = re.match(r"\*\*(.+?)\*\* ←", line)
            if m:
                slugs.add(m.group(1))
    return slugs


def slug_of(rec):
    return os.path.splitext(os.path.basename(rec["path"]))[0]


# ---------------------------------------------------------------------------------------
# Estimators (pure — the test drives these on a synthetic index)
# ---------------------------------------------------------------------------------------

def is_composed_gmail(rec):
    """Frontmatter-only heuristic: does this gmail note look like a thread the owner COMPOSED
    (relational/decision-bearing) rather than a transactional/automated one?"""
    if rec.get("source") != "gmail":
        return False
    non_owner = [p for p in rec.get("participants", []) if p not in OWNER_NAMES]
    if not (COMPOSED_PART_MIN <= len(non_owner) <= COMPOSED_PART_MAX):
        return False
    if not (set(rec.get("tags", [])) & COMPOSED_TAGS):
        return False
    if TRANSACTIONAL_RE.search(rec.get("title", "") or ""):
        return False
    return True


def estimator_counts(recs, decision_paths, curated_paths):
    """Per-domain raw estimator NUMERATORS (counts) over `recs`.

    E0 raw notes · E1 granola|meeting · E2 in decisions|action digest ·
    E3 hand-authored source · E4 chat source | composed-gmail · E5 curated-inbound."""
    counts = {d: dict.fromkeys(ESTIMATORS, 0) for d in DOMAINS}
    for r in recs:
        dom = r.get("domain")
        if dom not in counts:
            counts[dom] = dict.fromkeys(ESTIMATORS, 0)  # tolerate an out-of-list domain
        c = counts[dom]
        src = r.get("source")
        tags = set(r.get("tags", []))
        c["E0"] += 1
        if src == E1_GRANOLA_SOURCE or (tags & E1_MEETING_TAGS):
            c["E1"] += 1
        if r["path"] in decision_paths:
            c["E2"] += 1
        if src in E3_SOURCES:
            c["E3"] += 1
        if src in E4_CHAT_SOURCES or is_composed_gmail(r):
            c["E4"] += 1
        if r["path"] in curated_paths:
            c["E5"] += 1
    return counts


def estimator_shares(counts):
    """Normalize each estimator column to a share across domains (each column sums to ~1.0,
    or 0.0 when that estimator has no notes in the slice)."""
    totals = {e: sum(counts[d][e] for d in counts) for e in ESTIMATORS}
    shares = {}
    for d in counts:
        shares[d] = {
            e: (counts[d][e] / totals[e] if totals[e] else 0.0) for e in ESTIMATORS
        }
    return shares, totals


# ---------------------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------------------

def _git_sha():
    try:
        return subprocess.check_output(
            ["git", "-C", ROOT, "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


SHA = _git_sha()


def header(title):
    return (
        f"# {title}\n\n"
        f"_Generated by `scripts/profile_metrics.py` from `_manifest/index.jsonl` + digests + "
        f"`backlinks.md`. Store `{SHA[:12]}`, run {RUN_DATE.isoformat()}. Deterministic; "
        f"re-runnable. NOT a check.sh gate — an instrument._\n\n"
    )


def write_md(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)


def write_json(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------
# M1 — count matrices
# ---------------------------------------------------------------------------------------

def build_m1(recs):
    years = sorted({y for y in (year_of(r) for r in recs) if y is not None})
    sources = sorted({r["source"] for r in recs})
    doms_present = [d for d in DOMAINS if any(r.get("domain") == d for r in recs)]

    dy = collections.Counter((r.get("domain"), year_of(r)) for r in recs)
    sy = collections.Counter((r["source"], year_of(r)) for r in recs)
    sd = collections.Counter((r["source"], r.get("domain")) for r in recs)

    def matrix_md(title, row_keys, col_keys, counter):
        rows = []
        for rk in row_keys:
            row = [rk] + [counter.get((rk, ck), 0) for ck in col_keys] + \
                  [sum(counter.get((rk, ck), 0) for ck in col_keys)]
            rows.append(row)
        tot = ["**total**"] + \
              [sum(counter.get((rk, ck), 0) for rk in row_keys) for ck in col_keys] + \
              [sum(counter.values())]
        rows.append(tot)
        return "## " + title + "\n\n" + md_table(["·"] + [str(c) for c in col_keys] + ["total"], rows) + "\n"

    md = header("M1 — count matrices (domain × year, source × year, source × domain)")
    md += matrix_md("Domain × year", doms_present, years, dy)
    md += matrix_md("Source × year", sources, years, sy)
    md += matrix_md("Source × domain", sources, doms_present, sd)

    js = {
        "years": years,
        "sources": sources,
        "domains": doms_present,
        "domain_x_year": {f"{d}|{y}": dy[(d, y)] for d in doms_present for y in years if dy[(d, y)]},
        "source_x_year": {f"{s}|{y}": sy[(s, y)] for s in sources for y in years if sy[(s, y)]},
        "source_x_domain": {f"{s}|{d}": sd[(s, d)] for s in sources for d in doms_present if sd[(s, d)]},
        "total_notes": len(recs),
    }
    write_md("M1.md", md)
    write_json("M1.json", js)


# ---------------------------------------------------------------------------------------
# M2 — the attention X-ray
# ---------------------------------------------------------------------------------------

def _dossier_inventory():
    inv = []
    for fp in sorted(glob.glob(os.path.join(DOSSIERS_DIR, "*.md"))):
        base = os.path.basename(fp)
        if base == "README.md":
            continue
        inv.append({"file": base, "bytes": os.path.getsize(fp)})
    return inv


def _shares_block(recs, decision_paths, curated_paths, doms_present):
    counts = estimator_counts(recs, decision_paths, curated_paths)
    shares, totals = estimator_shares(counts)
    headers = ["domain", "N"] + [f"{e} {ESTIMATOR_LABELS[e]}%" for e in ESTIMATORS]
    rows = []
    for d in doms_present:
        row = [d, counts[d]["E0"]] + [f"{100 * shares[d][e]:.1f}" for e in ESTIMATORS]
        rows.append(row)
    tot_row = ["**total**", sum(counts[d]["E0"] for d in doms_present)] + \
              [f"{100 * sum(shares[d][e] for d in doms_present):.0f}" for e in ESTIMATORS]
    rows.append(tot_row)
    denom_row = ["**denominator (N)**", ""] + [str(totals[e]) for e in ESTIMATORS]
    rows.append(denom_row)
    return counts, shares, totals, md_table(headers, rows)


def build_m2(recs, decision_paths, curated_paths):
    doms_present = [d for d in DOMAINS if any(r.get("domain") == d for r in recs)]

    md = header("M2 — the attention X-ray (flagship)")
    md += (
        "Each estimator is a SHARE across domains (its column sums to 100%). **E0 raw-count is "
        "reported, never interpreted alone.** Divergence between E0 and E1..E5 for a domain IS the "
        "signal: a domain whose raw share far exceeds its decision / meeting / hand-authored shares "
        "is paperwork-heavy, not a life-share (or the reverse). No interpretation lives here — "
        "that is the memo's job.\n\n"
        "| Estimator | Proxy for | Numerator |\n|---|---|---|\n"
        "| E0 raw-count | note volume | every note |\n"
        f"| E1 sync-time | calendared hours (≈{MEETING_MINUTES}min/mtg) | source=granola OR `meeting` tag |\n"
        "| E2 decision-density | active cognitive engagement | note in decisions.md ∪ action-items.md |\n"
        f"| E3 hand-authored | deliberate self-initiated attention | source ∈ {sorted(E3_SOURCES)} |\n"
        f"| E4 human-conversation | relational investment | source ∈ {sorted(E4_CHAT_SOURCES)} OR composed-gmail |\n"
        "| E5 curation | what the synthesis layer links | curated-inbound note (backlinks.md) |\n\n"
        f"**Composed-gmail heuristic (E4):** {COMPOSED_RULE_TEXT}\n\n"
    )

    overall_counts, overall_shares, overall_totals, overall_tbl = _shares_block(
        recs, decision_paths, curated_paths, doms_present
    )
    md += "## Overall (all eras)\n\n" + overall_tbl + "\n"

    era_blocks = {}
    for label, lo, hi in ERAS:
        sub = [r for r in recs if era_of(year_of(r)) == label]
        if not sub:
            continue
        doms_e = [d for d in DOMAINS if any(r.get("domain") == d for r in sub)]
        c, s, t, tbl = _shares_block(sub, decision_paths, curated_paths, doms_e)
        md += f"## Era {label} (N={len(sub)})\n\n" + tbl + "\n"
        era_blocks[label] = {
            "n_notes": len(sub),
            "counts": c,
            "shares": {d: s[d] for d in c},
            "denominators": t,
        }

    inv = _dossier_inventory()
    md += (
        "## Dossier inventory (E5 companion — raw, un-attributed)\n\n"
        "Dossier→domain attribution is deferred to the memo (property/topic dossiers do not map "
        "1:1 to domains). Emitted as raw file+size only.\n\n"
        + md_table(["dossier", "bytes"], [[x["file"], x["bytes"]] for x in inv])
        + f"\n_{len(inv)} dossiers._\n"
    )

    composed_n = sum(1 for r in recs if is_composed_gmail(r))
    gmail_n = sum(1 for r in recs if r.get("source") == "gmail")
    md += (
        f"\n## Composed-gmail calibration hook\n\n"
        f"- gmail notes: {gmail_n}\n- classified composed: {composed_n} "
        f"({100 * composed_n / gmail_n:.1f}% of gmail)\n"
        f"- rule: `{COMPOSED_RULE_TEXT}`\n"
        f"- Phase 2c reads a stratified ~150-200 sample of bodies to measure this rule's "
        f"precision/recall; report the error bar with every gmail-derived claim.\n"
    )

    write_md("M2.md", md)
    write_json("M2.json", {
        "estimators": {e: ESTIMATOR_LABELS[e] for e in ESTIMATORS},
        "composed_gmail_rule": COMPOSED_RULE_TEXT,
        "composed_gmail_count": composed_n,
        "gmail_count": gmail_n,
        "meeting_minutes_assumption": MEETING_MINUTES,
        "overall": {
            "counts": overall_counts,
            "shares": overall_shares,
            "denominators": overall_totals,
        },
        "eras": era_blocks,
        "dossiers": inv,
    })


# ---------------------------------------------------------------------------------------
# M3 — people dynamics
# ---------------------------------------------------------------------------------------

def build_m3(recs):
    people = collections.defaultdict(lambda: {
        "total": 0, "by_year": collections.Counter(), "by_domain": collections.Counter(),
        "years": set(),
    })
    # new-in-domain: first year a person appears in a given domain
    first_in_domain = {}  # (person, domain) -> year
    for r in recs:
        y = year_of(r)
        dom = r.get("domain")
        for p in r.get("participants", []):
            if p in OWNER_NAMES:
                continue
            rec = people[p]
            rec["total"] += 1
            rec["by_domain"][dom] += 1
            if y is not None:
                rec["by_year"][y] += 1
                rec["years"].add(y)
                key = (p, dom)
                if key not in first_in_domain or y < first_in_domain[key]:
                    first_in_domain[key] = y

    def summarize(p):
        rec = people[p]
        yrs = sorted(rec["years"])
        return {
            "total": rec["total"],
            "first_year": yrs[0] if yrs else None,
            "last_year": yrs[-1] if yrs else None,
            "by_year": {str(y): rec["by_year"][y] for y in yrs},
            "by_domain": dict(sorted(rec["by_domain"].items(), key=lambda kv: (-kv[1], kv[0]))),
        }

    all_people = sorted(people, key=lambda p: (-people[p]["total"], p))
    people_json = {p: summarize(p) for p in all_people}

    # new-people rate per domain/year: count of people whose FIRST appearance in that domain == y
    doms_present = [d for d in DOMAINS if any(r.get("domain") == d for r in recs)]
    years = sorted({y for y in (year_of(r) for r in recs) if y is not None})
    newrate = collections.Counter()
    for (p, dom), y in first_in_domain.items():
        newrate[(dom, y)] += 1

    md = header("M3 — people dynamics (from participants[])")
    md += f"Distinct non-owner participants: **{len(all_people)}**.\n\n"

    top = all_people[:30]
    rows = []
    for p in top:
        s = people_json[p]
        rows.append([p, s["total"], s["first_year"], s["last_year"],
                     (s["last_year"] - s["first_year"] + 1) if s["first_year"] else 0,
                     "; ".join(f"{d}:{n}" for d, n in list(s["by_domain"].items())[:3])])
    md += "## Top 30 people by note count (inner-circle candidates)\n\n" + md_table(
        ["person", "notes", "first", "last", "span-yrs", "top domains"], rows) + "\n"

    # inner-circle stability: of the top-20, how many are active each year
    inner = top[:20]
    rows = []
    for y in years:
        active = sum(1 for p in inner if people[p]["by_year"].get(y, 0) > 0)
        rows.append([y, active, f"{100 * active / len(inner):.0f}%"])
    md += ("## Inner-circle stability (top-20 people active per year)\n\n"
           + md_table(["year", "active of 20", "%"], rows) + "\n")

    rows = []
    for d in doms_present:
        row = [d] + [newrate.get((d, y), 0) for y in years] + \
              [sum(newrate.get((d, y), 0) for y in years)]
        rows.append(row)
    md += ("## New-people rate (first appearance in a domain), domain × year\n\n"
           + md_table(["domain"] + [str(y) for y in years] + ["total"], rows) + "\n")

    write_md("M3.md", md)
    write_json("M3.json", {
        "n_people": len(all_people),
        "people": people_json,
        "new_people_by_domain_year": {f"{d}|{y}": newrate[(d, y)]
                                      for d in doms_present for y in years if newrate[(d, y)]},
        "inner_circle_top20": inner,
    })


# ---------------------------------------------------------------------------------------
# M4 — temporal fingerprints
# ---------------------------------------------------------------------------------------

def _parse_open_loops(slug2rec):
    """Yield (status, domain, note_date, age_days) for each open-loop bullet with a
    resolvable [[wikilink]]."""
    out = []
    with open(OPENLOOPS, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s.startswith("- "):
                continue
            wikis = re.findall(r"\[\[([^\]]+)\]\]", s)
            if not wikis:
                continue
            slug = wikis[-1]
            rec = slug2rec.get(slug)
            if not rec:
                continue
            mstat = re.search(r"status:\s*([^·\n]+)", s)
            status = mstat.group(1).strip() if mstat else ""
            d = rec.get("date") or ""
            age = None
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                try:
                    age = (RUN_DATE - datetime.date.fromisoformat(d)).days
                except ValueError:
                    age = None
            out.append((status, rec.get("domain"), d, age))
    return out


def build_m4(recs, slug2rec):
    years = sorted({y for y in (year_of(r) for r in recs) if y is not None})

    # tag first/last appearance + trajectories
    tag_years = collections.defaultdict(set)
    tag_by_year = collections.defaultdict(collections.Counter)
    tag_total = collections.Counter()
    for r in recs:
        y = year_of(r)
        for t in r.get("tags", []):
            tag_total[t] += 1
            if y is not None:
                tag_years[t].add(y)
                tag_by_year[t][y] += 1

    md = header("M4 — temporal fingerprints")

    # first/last appearance — era-boundary candidates
    rows = []
    fa = sorted(tag_years, key=lambda t: (min(tag_years[t]), t))
    for t in fa:
        ys = sorted(tag_years[t])
        rows.append([t, ys[0], ys[-1], tag_total[t]])
    md += ("## Tag first/last appearance (era-boundary candidates)\n\n"
           + md_table(["tag", "first", "last", "total"], rows) + "\n")

    # trajectories for top-25 tags
    top_tags = [t for t, _ in tag_total.most_common(25)]
    rows = []
    for t in top_tags:
        rows.append([t] + [tag_by_year[t].get(y, 0) for y in years] + [tag_total[t]])
    md += ("## Tag trajectories (top-25 tags), tag × year\n\n"
           + md_table(["tag"] + [str(y) for y in years] + ["total"], rows) + "\n")

    # monthly burst detection (z-score over all months present)
    months = collections.Counter(m for m in (month_of(r) for r in recs) if m)
    mseq = sorted(months)
    vals = [months[m] for m in mseq]
    mean = statistics.mean(vals) if vals else 0.0
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    bursts = []
    for m in mseq:
        z = (months[m] - mean) / std if std else 0.0
        if z >= 2.0:
            bursts.append((m, months[m], round(z, 2)))
    md += (f"## Monthly note-count bursts (z≥2.0; mean={mean:.1f}, sd={std:.1f} over "
           f"{len(mseq)} months)\n\n"
           + md_table(["month", "count", "z"], [[m, c, z] for m, c, z in bursts])
           + f"\n_{len(bursts)} burst months. NOTE: bursts track connector-onboarding as much as "
           f"life events — read against the M5 coverage windows before interpreting._\n\n")

    # open-loop age distribution per domain
    loops = _parse_open_loops(slug2rec)
    open_loops = [(dom, age) for (status, dom, d, age) in loops
                  if status.lower().startswith("open") and age is not None]
    per_dom = collections.defaultdict(list)
    for dom, age in open_loops:
        per_dom[dom].append(age)
    rows = []
    for dom in sorted(per_dom):
        ages = sorted(per_dom[dom])
        rows.append([dom, len(ages), min(ages), int(statistics.median(ages)),
                     ages[int(0.9 * (len(ages) - 1))], max(ages)])
    all_ages = sorted(a for ages in per_dom.values() for a in ages)
    if all_ages:
        rows.append(["**all**", len(all_ages), min(all_ages),
                     int(statistics.median(all_ages)),
                     all_ages[int(0.9 * (len(all_ages) - 1))], max(all_ages)])
    md += (f"## Open-loop age distribution per domain (status=open, age vs {RUN_DATE.isoformat()})\n\n"
           + md_table(["domain", "n", "min", "median", "p90", "max"], rows)
           + f"\n_Parsed {len(loops)} open-loop bullets with a resolvable [[wikilink]]; "
           f"{len(open_loops)} were status=open with a dated source note._\n")

    write_md("M4.md", md)
    write_json("M4.json", {
        "tag_first_last": {t: {"first": min(tag_years[t]), "last": max(tag_years[t]),
                               "total": tag_total[t]} for t in tag_years},
        "tag_trajectories": {t: {str(y): tag_by_year[t].get(y, 0) for y in years}
                             for t in top_tags},
        "monthly_counts": {m: months[m] for m in mseq},
        "burst_months": [{"month": m, "count": c, "z": z} for m, c, z in bursts],
        "burst_stats": {"mean": mean, "pstdev": std, "n_months": len(mseq)},
        "open_loop_age_by_domain": {dom: sorted(per_dom[dom]) for dom in sorted(per_dom)},
    })


# ---------------------------------------------------------------------------------------
# M5 — bias ledger (the go/no-go GATE on temporal claims)
# ---------------------------------------------------------------------------------------

def _coverage(recs):
    win = {}
    per_source_era = collections.Counter()
    for r in recs:
        s = r["source"]
        d = r.get("date") or ""
        if len(d) >= 7 and d[:4].isdigit():
            lo, hi = win.get(s, (None, None))
            if lo is None or d < lo:
                lo = d
            if hi is None or d > hi:
                hi = d
            win[s] = (lo, hi)
        per_source_era[(s, era_of(year_of(r)))] += 1
    return win, per_source_era


def build_m5(recs):
    win, pse = _coverage(recs)
    sources = sorted(win)
    era_labels = [e[0] for e in ERAS]

    # The selection-bar gate. A source ingested by TWO selectors (e.g. a one-off mbox backfill and the
    # incremental connector sync) is only comparable across eras if both apply the SAME inclusion
    # bar. That is a fact about your scripts, not your data, so it is recorded here as UNVERIFIED
    # until you compare them (read both selectors; set bars_match in your own copy once you have).
    gate = {
        "bars_match": None,
        "verdict": (
            "UNVERIFIED: if a source was ingested by more than one selector (a backfill and an "
            "incremental sync), compare their inclusion bars before licensing any cross-era volume "
            "claim on that source. Until then, cross-era volume comparisons are QUALITATIVE ONLY."
        ),
    }
    onboard = sorted((win[s][0], s) for s in sources if win[s][0])

    md = header("M5 / bias-ledger — source × era coverage + claim-permission rules")
    md += ("**This ledger governs whether temporal / share claims are allowed at all. Every "
           "temporal or share claim in the memo must cite the rule that licenses it.**\n\n")

    md += "## Source coverage windows (derived from index.jsonl)\n\n" + md_table(
        ["source", "first date", "last date", "notes"],
        [[s, win[s][0], win[s][1], sum(pse[(s, e)] for e in era_labels + [None])]
         for s in sources]) + "\n"

    md += "## Source × era note counts\n\n" + md_table(
        ["source"] + era_labels + ["undated"],
        [[s] + [pse[(s, e)] for e in era_labels] + [pse[(s, None)]] for s in sources]) + "\n"

    md += (
        "## Claim-permission rules\n\n"
        "1. **Cross-era VOLUME comparisons: within ONE source only**, and only for a source whose "
        "coverage window spans both eras and whose selection bars are verified (see the GATE below). "
        "Mixing sources across eras conflates life change with connector onboarding.\n"
        "2. **Synchronous-time (E1) claims: meeting-recorder era ONLY** (from the first granola note, "
        "see the coverage windows). Before it, meeting time is invisible except as mentions.\n"
        "3. **Sparse early eras are capped LOW-CONFIDENCE ([I-tent])** unless corroborated by a "
        "hand-authored artifact: this is what the record PRESERVED, not what happened.\n"
        "4. **The connector-onboarding mirage:** a note explosion that starts where a source's coverage "
        "starts is connector onboarding, not life change. Onboarding dates in this store: "
        + (", ".join(f"{src} {first[:7]}" for first, src in onboard) or "(none)") + ".\n"
        "5. **A narrow-window source** (e.g. a work mailbox exported for only a few months) is licensed "
        "only as a within-window snapshot; never compare attention across its start boundary.\n\n"
    )

    md += (
        "## THE SELECTION-BAR GATE\n\n"
        f"- **Bars match:** {gate['bars_match']}\n"
        f"\n**VERDICT:** {gate['verdict']}\n"
    )

    write_md("bias-ledger.md", md)
    write_md("M5.md", header("M5 — bias ledger")
             + "M5's deliverable IS the bias ledger. See [bias-ledger.md](bias-ledger.md).\n")
    write_json("bias-ledger.json", {
        "coverage_windows": {s: {"first": win[s][0], "last": win[s][1]} for s in sources},
        "source_x_era": {f"{s}|{e or 'undated'}": pse[(s, e)]
                         for s in sources for e in era_labels + [None] if pse[(s, e)]},
        "eras": [{"label": e[0], "lo": e[1], "hi": e[2]} for e in ERAS],
        "gmail_gate": gate,
    })


# ---------------------------------------------------------------------------------------
# M6 — worklists (grep-built path lists; uninterpreted)
# ---------------------------------------------------------------------------------------

def _grep_paths(paths, regex):
    """(rel_path, lineno, matched_line) for every regex hit in the given files. Bodies are
    read into THIS process and written to a file — never surfaced as analysis."""
    hits = []
    for rel in paths:
        fp = os.path.join(ROOT, rel)
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        hits.append((rel, i, line.strip()))
        except OSError:
            continue
    return hits


def build_m6(recs):
    # (a) stated-priorities corpus: hand-authored + composed-gmail + grants-tagged notes + dossiers
    corpus = sorted({
        r["path"] for r in recs
        if r.get("source") in E3_SOURCES
        or is_composed_gmail(r)
        or ("grants" in set(r.get("tags", [])))
    })
    dossier_files = sorted(
        os.path.relpath(fp, ROOT) for fp in glob.glob(os.path.join(DOSSIERS_DIR, "*.md"))
        if os.path.basename(fp) != "README.md"
    )
    prio_hits = _grep_paths(corpus, PRIORITY_RE) + _grep_paths(dossier_files, PRIORITY_RE)

    # (b) declines corpus: ALL domain notes (revealed preference)
    all_paths = sorted(r["path"] for r in recs)
    decline_hits = _grep_paths(all_paths, DECLINE_RE)

    def hits_md(title, note, hits):
        rows = [[p, ln, txt.replace("|", "\\|")[:180]] for p, ln, txt in hits]
        return (f"## {title}\n\n{note}\n\n"
                + md_table(["path", "line", "matched text"], rows)
                + f"\n_{len(hits)} matched lines._\n\n")

    md = header("M6 — worklists (grep-built path lists; UNINTERPRETED)")
    md += ("These are raw path+line lists for later phases to read in full — NO interpretation "
           "here. The stated-priorities corpus is hand-authored + composed-gmail + grants-tagged "
           "notes + dossiers; the declines corpus is all notes.\n\n")
    md += hits_md(
        "(a) Stated-priorities corpus",
        f"Corpus = {len(corpus)} notes (source∈{sorted(E3_SOURCES)} | composed-gmail | grants-tagged) "
        f"+ {len(dossier_files)} dossiers. Grep: `{PRIORITY_RE.pattern}`",
        prio_hits)
    md += hits_md(
        "(b) Declines corpus (revealed preference)",
        f"Corpus = all {len(all_paths)} notes. Grep: `{DECLINE_RE.pattern}`",
        decline_hits)

    write_md("M6.md", md)
    write_json("M6.json", {
        "stated_priorities": {
            "corpus_note_count": len(corpus),
            "dossier_count": len(dossier_files),
            "pattern": PRIORITY_RE.pattern,
            "hits": [{"path": p, "line": ln, "text": txt} for p, ln, txt in prio_hits],
        },
        "declines": {
            "corpus_note_count": len(all_paths),
            "pattern": DECLINE_RE.pattern,
            "hits": [{"path": p, "line": ln, "text": txt} for p, ln, txt in decline_hits],
        },
    })


# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------

def main():
    os.makedirs(OUT, exist_ok=True)
    recs = load_index()

    decision_paths = digest_paths(DECISIONS) | digest_paths(ACTIONS)
    slugs = curated_slugs()
    slug2rec = {}
    for r in sorted(recs, key=lambda x: x["path"]):
        slug2rec.setdefault(slug_of(r), r)
    curated_paths = {slug2rec[s]["path"] for s in slugs if s in slug2rec}

    build_m1(recs)
    build_m2(recs, decision_paths, curated_paths)
    build_m3(recs)
    build_m4(recs, slug2rec)
    build_m5(recs)
    build_m6(recs)

    print(f"profile_metrics: {len(recs)} notes | "
          f"decision-bearing {len(decision_paths)} | curated {len(curated_paths)} "
          f"({len(slugs)} slugs, {len(slugs) - len(curated_paths)} unresolved) | "
          f"wrote M1-M6 + bias-ledger to {os.path.relpath(OUT, ROOT)}/")


if __name__ == "__main__":
    main()
