#!/usr/bin/env python3
"""Screen extracted thread text for clinical-patient PHI signals.

engram's one hard sensitivity rule (CLAUDE.md): no clinical patient PHI in the store.
This scans extracted thread .txt files for HIPAA Safe-Harbor identifiers + clinical
patient context and scores each thread, so PHI-bearing threads can be HELD from
auto-distillation (routed to the de-identified-letter plan) rather than written raw.

Heuristic, tuned to over-flag (recall > precision): a flagged thread is reviewed/held,
never silently distilled. Reads the KEEP list to scope. DEFAULT (Phase 4): scan EVERY
extracted thread — the school-letter PHI class arrived from a non-MED sender, so the old
phi_risk-only default was a recall hole. Pass --phi-risk-only to restore the narrow scan
(--all is retained as a no-op alias for the now-default behavior).

Usage:
  python3 scripts/phi_scan.py <gmail-keep.jsonl> <threads-dir> <out-report.jsonl> [--phi-risk-only]
"""
import sys, os, json, re, datetime

# Strong identifiers (any hit = high suspicion)
STRONG = [
    ('mrn', re.compile(r'\b(MRN|medical record (no|number|#))\b', re.I)),
    ('ssn', re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
    ('dob', re.compile(r'\b(DOB|date of birth)\b', re.I)),
    ('acct', re.compile(r'\b(accession|path(ology)? (no|number|#)|specimen (id|no))\b', re.I)),
]
# Clinical patient-context terms (need several to flag)
# NOTE (1.4): the trailing `\b` on the alternation defeats bare prefixes — `epilep\b` and
# `prescrib\b` matched NOTHING (a word boundary can't follow "epilep"/"prescrib"), and `seizure\b`
# missed "seizures". Inflected forms below (`\w*` / `s?`) consume the suffix before the `\b`.
CTX = re.compile(
    r'\b(patient|pt\.?|diagnos(is|es|ed)|\bdx\b|biopsy|lesions?|tumou?rs?|seizures?|epilep\w*|'
    r'year[- ]old|y/?o\b|presented with|chief complaint|history of present|'
    r'\bH&P\b|discharge summary|admitted|clinic(al)? note|prescrib\w*|\bmg\b|\bdose|'
    r'MRI brain|EEG|workup|referral|consult note|onset|symptoms?)\b', re.I)
# Academic/admin terms that DOWN-weight (grant/faculty/teaching, not patient care)
ACADEMIC = re.compile(
    r'\b(grant|R01|K award|study section|manuscript|co-?author|faculty|seminar|'
    r'lecture|curriculum|committee|abstract|poster|conference|reviewer|stipend|'
    r'lab meeting|IRB protocol|enrollment|subject|participant)\b', re.I)


# ---------------------------------------------------------------------------
# Phase-4 lightweight detectors (no NER, no new deps). Single-owner: BOTH the
# source-side screen (this file's main) and the store audit (phi_scan_store.py)
# inherit these through scan(). Detectors report only WHICH signal fired — the
# matched substring (a name / a date) is never returned or printed.
# ---------------------------------------------------------------------------

# (a) Unlabeled DOB — a bare date in genuine clinical proximity (a labeled
#     "DOB:" is already a STRONG hit above). Precision guards, because every
#     note has a frontmatter date and financial notes are date-dense:
#       * the date must sit within DOB_PROX chars of a clinical CTX term;
#       * 4-digit years must be plausible birth years (1900..today-2 — a DOB is
#         a past date; kills meeting/deadline dates clustered around "now");
#       * metadata lines (`date:` / `created:` ... ) never count;
#       * academic-dominated text is suppressed (see scan()).
DOB_PROX = 200
_MAX_DOB_YEAR = datetime.date.today().year - 2
BARE_DATE_NUM = re.compile(  # 6/15/2015, 06-15-15, 6.15.2015 (consistent separator)
    r'\b(?P<a>\d{1,2})(?P<sep>[/.-])(?P<b>\d{1,2})(?P=sep)(?P<y>\d{4}|\d{2})\b')
BARE_DATE_MON = re.compile(  # June 15, 2015 / Jun 15 2015 (capital-M "May" only: modal verb)
    r'\b(?:[Jj]an(?:uary)?|[Ff]eb(?:ruary)?|[Mm]ar(?:ch)?|[Aa]pr(?:il)?|May|'
    r'[Jj]un(?:e)?|[Jj]ul(?:y)?|[Aa]ug(?:ust)?|[Ss]ep(?:t(?:ember)?)?|'
    r'[Oo]ct(?:ober)?|[Nn]ov(?:ember)?|[Dd]ec(?:ember)?)\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})\b')
_META_LINE = re.compile(r'[ \t]*(dates?|created|updated|modified|due|scheduled)[ \t]*:', re.I)


def _on_meta_line(text, pos):
    ls = text.rfind('\n', 0, pos) + 1
    return bool(_META_LINE.match(text, ls))


def _unlabeled_dob(text, ctx_positions):
    # need SOME real clinical density (>=2 CTX terms in the note), not one stray
    # "referral"/"dose" — kills date-dense estate/logistics notes with ctx=1
    # (measured: pure noise), while any genuine patient note clears 2 easily.
    if len(ctx_positions) < 2:
        return False

    def near_ctx(p):
        return any(abs(p - q) <= DOB_PROX for q in ctx_positions)

    for m in BARE_DATE_NUM.finditer(text):
        a, b, y = int(m.group('a')), int(m.group('b')), m.group('y')
        if not ((1 <= a <= 12 and 1 <= b <= 31) or (1 <= a <= 31 and 1 <= b <= 12)):
            continue  # not a plausible day/month pair
        if len(y) == 4 and not (1900 <= int(y) <= _MAX_DOB_YEAR):
            continue  # not a plausible birth year (2-digit years: century unknowable, accept)
        if _on_meta_line(text, m.start()) or not near_ctx(m.start()):
            continue
        return True
    for m in BARE_DATE_MON.finditer(text):
        if not (1 <= int(m.group('d')) <= 31 and 1900 <= int(m.group('y')) <= _MAX_DOB_YEAR):
            continue
        if _on_meta_line(text, m.start()) or not near_ctx(m.start()):
            continue
        return True
    return False


# (b) Named minor + diagnosis — the "school letter" class (a patient's child
#     name + pediatric marker + diagnosis term co-occurring) that slipped the
#     density heuristic. Fires only when a plausible Firstname Lastname sits
#     within NAME_PROX chars of BOTH a pediatric marker AND a diagnosis term.
#     A lab member's own family's ordinary life events carry no diagnosis term
#     nearby, so they don't trip this; a rare benign hit is an acceptable over-flag.
NAME_PROX = 250
NAME_RX = re.compile(r'\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b')
# capitalized words that are not plausible personal-name components (orgs,
# places, months, salutations, headings) — either word in the stoplist rejects
# the pair. Tune here, never by weakening the co-occurrence rule.
# ADD YOUR OWN institution, campus and city names here — they are the highest-frequency
# false-positive source in any one store, and only you know them.
NAME_STOP = {
    'Children', 'Childrens', 'Hospital', 'Medical', 'School', 'University',
    'College', 'Center', 'Centre', 'Institute', 'Institutes', 'Department', 'Division',
    'General', 'Health', 'Healthcare', 'Clinic', 'Clinical', 'Foundation', 'National',
    'American', 'International', 'Journal', 'Review', 'Board', 'Committee', 'Society',
    'Association', 'Academy', 'Conference', 'Program', 'Project', 'Group', 'Team',
    'Grand', 'Rounds', 'Emergency', 'Special', 'Education', 'Individualized',
    'United', 'States',
    'Grade', 'Patient',
    'Doctor', 'Family', 'Parent', 'Parents', 'Child', 'The', 'This', 'That', 'These',
    'Those', 'There', 'Please', 'Thanks', 'Thank', 'Dear', 'Best', 'Regards',
    'Sincerely', 'From', 'Subject', 'Date', 'Sent', 'Note', 'Notes', 'Meeting',
    'Action', 'Items', 'Next', 'Steps', 'Summary', 'Overview', 'Background',
    'Update', 'Status', 'Today', 'Tomorrow', 'Yesterday',
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December', 'Monday', 'Tuesday', 'Wednesday',
    'Thursday', 'Friday', 'Saturday', 'Sunday', 'Dr', 'Mr', 'Mrs', 'Ms', 'Prof',
}
# Markers of an INDIVIDUAL minor, not a population/specialty. Measured on the
# store: bare "children"/"pediatric" fire on research
# populations and job titles (in a lab or clinic that works with children the whole professional graph
# matches), while the school-letter class always frames one
# child: age <=17, school/grade/IEP/504, "your son/daughter", "pediatric patient".
PED_RX = re.compile(
    r'\b(?:'
    # "minor" only as an unambiguous noun — the adjective ("minor repairs" near a
    # car "diagnostic") is pure noise in estate/business notes
    r'minors|a minor(?=\s+(?:with|who|diagnosed|under)\b|\s*[,.;:!?)]|\s*$)|'
    r'(?:my|our|your|his|her|their)\s+(?:sons?|daughters?|child(?:ren)?(?!\s+and\s+adolescent))|'
    r'child(?!\s+(?:and adolescent|neuro|psychiat|psycholog|development|health|hospital|welfare|life|mind))|'
    r'p(?:a)?ediatric(?:\s+\w+)?\s+(?:patient|case)|'
    r'kindergart\w+|preschooler?|daycare|toddler|infant(?!ile)|'
    r'adolescent(?!\s+(?:neurolog|psychiat|medicine))|teen(?:ager)?|'
    r'IEP|504 plan|'
    r'(?<!medical )(?<!med )(?<!graduate )(?<!grad )(?<!law )(?<!nursing )school(?!s? of)|'
    r'(?<!high[- ])(?<!low[- ])grade'
    r')\b', re.I)
AGE_RX = re.compile(  # age-worded mentions count as pediatric only when <= 17
    r'\b(?P<age>\d{1,2})(?:[- ]?(?:year|yr)s?[- ]old|\s?y/?o)\b', re.I)
DX_RX = re.compile(
    r'\b(?:diagnos(?:is|es|ed|tic)|dx|epilep(?!tolog)\w+|seizures?|autism|autistic|ADHD|ASD|'
    r'tumou?rs?|glioma|leukemia|lymphoma|cancer|palsy|syndromes?|disorders?|'
    r'diabet\w+|asthma|concussion|encephal\w+|malformation)\b', re.I)
# YAML frontmatter mask (store notes): participants/tags there are registry-
# resolved colleagues, never patients — masking them keeps hundreds of meeting
# notes ("Clinic diagnosis review" + participants) from tripping the detector.
# Tolerates one prefix line (the store audit prepends the file path) and keeps
# that prefix in place. Bounded so an unclosed '---' can't mask a whole note.
_FM_RX = re.compile(r'\A([^\n]*\n)?---[ \t]*\n.{0,4000}?\n---[ \t]*\n', re.S)


def _named_minor_dx(text):
    t = _FM_RX.sub(lambda m: m.group(1) or '', text, count=1)
    ped = [m.start() for m in PED_RX.finditer(t)]
    ped += [m.start() for m in AGE_RX.finditer(t) if int(m.group('age')) <= 17]
    if not ped:
        return False
    dx = [m.start() for m in DX_RX.finditer(t)]
    if not dx:
        return False
    for m in NAME_RX.finditer(t):
        if m.group(1) in NAME_STOP or m.group(2) in NAME_STOP:
            continue
        p = m.start()
        if any(abs(p - q) <= NAME_PROX for q in ped) and \
           any(abs(p - q) <= NAME_PROX for q in dx):
            return True
    return False


def scan(text):
    strong = [name for name, rx in STRONG if rx.search(text)]
    ctx_positions = [m.start() for m in CTX.finditer(text)]
    ctx = len(ctx_positions)
    acad = len(ACADEMIC.findall(text))
    # Phase-4 proximity detectors — suppressed only in clearly academic-dominated
    # text (grants/faculty digests), biased to recall: mild academic presence
    # (acad < 3) never suppresses.
    if not (acad > ctx and acad >= 3):
        if _unlabeled_dob(text, ctx_positions):
            strong.append('dob_unlabeled')
        if _named_minor_dx(text):
            strong.append('named_minor_dx')
    # decision: strong id, OR dense clinical context not dominated by academic terms
    phi = bool(strong) or (ctx >= 6 and ctx > acad)
    level = 'high' if strong else ('medium' if phi else ('low' if ctx >= 3 else 'none'))
    return {'phi': phi, 'level': level, 'strong': strong, 'ctx': ctx, 'acad': acad}


def main():
    keep_path, threads_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    # Phase 4: default = scan EVERY thread. The old default (phi_risk-only, i.e.
    # MED-sender threads) missed the school-letter class, which had a non-MED
    # sender. --phi-risk-only restores the narrow scan; --all is a no-op alias.
    risk_only = '--phi-risk-only' in sys.argv
    rows = []
    counts = {'high': 0, 'medium': 0, 'low': 0, 'none': 0, 'flagged': 0, 'scanned': 0}
    for line in open(keep_path, encoding='utf-8'):
        rec = json.loads(line)
        if risk_only and not rec.get('phi_risk'):
            continue
        fp = os.path.join(threads_dir, f"{rec['thrid']}.txt")
        if not os.path.exists(fp):
            continue
        text = open(fp, encoding='utf-8', errors='replace').read()
        r = scan(text)
        counts['scanned'] += 1
        counts[r['level']] += 1
        if r['phi']:
            counts['flagged'] += 1
        rows.append({'thrid': rec['thrid'], 'domain': rec['domain'],
                     'subject': rec.get('subject'), 'labels': rec.get('labels'),
                     **r})
    rows.sort(key=lambda x: (x['level'] != 'high', x['level'] != 'medium', -x['ctx']))
    with open(out_path, 'w', encoding='utf-8') as o:
        for r in rows:
            o.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f"scanned={counts['scanned']:,}  PHI-flagged(HOLD)={counts['flagged']:,}  "
          f"[high={counts['high']} medium={counts['medium']} low={counts['low']} none={counts['none']}]")
    print(f"report -> {out_path}")
    print("\nTop suspected-PHI threads:")
    for r in rows[:15]:
        if not r['phi']:
            break
        print(f"  [{r['level']:6s}] ctx={r['ctx']:2d} acad={r['acad']:2d} strong={r['strong']} | {str(r['subject'])[:60]}")


if __name__ == '__main__':
    main()
