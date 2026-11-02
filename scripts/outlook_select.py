#!/usr/bin/env python3
"""Group the Outlook per-message index into THREADS, apply the keep/drop + PHI screen, and render
each KEEP thread to `_sources/outlook-threads/<thrid>.txt` (the exact header+per-MSG format the
distiller reads — same shape as `_sources/gmail-threads/<thrid>.txt`). Emits
`_sources/outlook-keep.jsonl`, one record per KEEP thread, with the fields `phi_scan.py` consumes
(thrid, domain, subject, labels, phi_risk).

THREADING (Outlook/Exchange is richer than a raw mbox; union-find over three signals, most→least
reliable, NO subject-only merge — that over-merges yearly "renewal notice"/"reminder" noise):
  1. Exchange `Thread-Index` conversation GUID (first 22 decoded bytes) — the authoritative
     Exchange conversation identity.
  2. RFC822 `References`/`In-Reply-To` — union a message with every in-corpus message it cites.
  3. otherwise the message stands alone (a singleton thread).

KEEP/DROP (scope = "substantive incl. received-only", the owner 2026-07-06): DROP obvious bulk —
[Bulk]-tagged, newsletters/digests, no-reply/notification/system senders, facilities notices,
listserv blasts — UNLESS the owner sent into the thread (engagement = keep). Everything else is KEPT for
the distiller, which reads the body and makes the final durability call.

PHI (fail-closed): work mail at a clinical institution can be mostly clinical. This step only
SCREENS/FLAGS:
  * `phi_risk` = any participant at a PHI-screen domain (`phi_screen_sender_domains` in
    `_manifest/constants/mail_routing.json`; feeds the mandatory screen — the scan already covers
    every thread by default).
  * `phi_hold_subject` = the subject matches an unambiguous clinical-roster/handoff pattern (weekend
    service email, sign-out, census, consult/patient list), an Epic sender/subject, an MRN, or a
    bare all-lowercase person-name subject from a clinical sender. These are HELD regardless of the
    heuristic scanner — belt-and-suspenders. `outlook_worklist.py` unions this with the
    `phi_scan.py` high|medium flags to exclude them from distillation, and a hold is STICKY there
    (once held, held until released) so a growing thread cannot dilute its way back into a wave.
  * `drop_rule` = a deterministic NON-PHI drop (`clinical-task-nurse`): mail from the configured
    `clinical_task_senders` (clinic staff whose every message is a task about a patient) never
    enters engram. `outlook_worklist.py` subtracts it and
    writes the `_sources/outlook-drops.d/<thrid>.json` marker.
Nothing is written into a domain dir here; `_sources/` is gitignored.

    python3 scripts/outlook_select.py [--index _sources/outlook-index.jsonl]
        [--keep _sources/outlook-keep.jsonl] [--threads-dir _sources/outlook-threads]
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from outlook_extract import OWNER_RX   # noqa: E402  (single owner of "is this address the owner")
from _lib.mail_routing import ROUTING  # noqa: E402  (single owner of PHI-screen domains + routing)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAXBODY = 6000   # per-message body cap in the rendered thread (matches gmail_render)

# --- clinical institutions: participation => PHI-screen (feeds phi_risk) --------------------------
# Configured, not coded: `phi_screen_sender_domains` in _manifest/constants/mail_routing.json.
# Read through the module-level ROUTING at call time (tests swap in their own Routing).
def phi_screen(dom):
    return ROUTING.phi_screen(dom)


# --- deterministic PHI holds (fail-closed belt; the distiller is the suspenders) -------------------
# The general phi_scan.py heuristic (tuned for the whole store) MISSES short EHR (e.g. Epic)
# notifications: a patient-message subject "msg <name> <MRN>" or a bare patient name carries no
# clinical CONTEXT terms for its density check to fire. So screen those deterministically here.
#
# (a) roster / hand-off subjects — tight + clinical-specific (must NOT catch a facilities "weekend
#     valet" notice or a grant "coverage"/salary thread).
HARD_HOLD_SUBJ = re.compile(
    r'\bsign[-\s]?out\b|\bhand[-\s]?off\b|\bcensus\b|consult list|patient list|'
    r'service list|rounding list|round(ing)? list|admit list|inpatient list|coverage list|'
    r'discharge summary|(weekend|holiday|on-?call) (coverage|list)',
    re.I)
# (b) Epic / EHR automated senders — a message from the clinical record system is patient care.
EPIC_SENDER = re.compile(r'(^|[._-])(epic|mychart|in-?basket|inbasket|cadence|haiku|canto|'
                         r'interconnect|resultsmgr|reportdistribution|hl7)@', re.I)
# (c) patient-message / result subjects (Epic in-basket forwards, MyChart, results).
EPIC_SUBJ = re.compile(
    r'^\s*(re|fw|fwd)?\s*:?\s*msg\b|in-?basket|staff message|patient message|\bmychart\b|'
    r'result(s)? (note|for|available|ready)|new result|critical result|refill request|'
    r'\brefill\b|prior auth|prescription|\brx\b(?!\w)|imaging (result|report)|path(ology)? (result|report)',
    re.I)
# (d) a standalone 7-or-8-digit number in a clinical subject = an MRN (dates/years/phones don't run
#     7-8 as a bare token; e.g. "jane doe 12345678"). The SPACED/HYPHENATED 3-2-2 form is the same id
#     typed by hand ("jane doe 123 45 67") and leaked past the bare-\d{7,8} form — a date runs 4-2-2
#     and a phone 3-3-4, so 3-2-2 does not collide. Adjust to YOUR institution's MRN shape.
MRN_SUBJ = re.compile(r'\b\d{7,8}\b|\b\d{3}[ -]\d{2}[ -]\d{2}\b')
# (e) BARE-NAME SUBJECT (a measured leak: a clinic nurse's patient-name subject with a near-empty body
#     scored `none` in phi_scan and went straight into the wave).
#
#     HISTORY — a bare person-name hold was tried and REMOVED once before: a case-INSENSITIVE
#     `Firstname Lastname` is pattern-indistinguishable from `Adjective Noun` ("Wednesday Clinic",
#     "Master Calendar", "Division Updates", a collaborator's name) and over-held most real work
#     content. Held == excluded by default, so over-holding LOSES real notes — the wrong error.
#     This rule is NOT that rule. Three things narrow it to high precision:
#       1. the normalized subject must be ALL-LOWERCASE alphabetic tokens. That is the discriminator
#          the old rule lacked: a colleague/meeting/org subject is title-cased ("Smith/Jones Research
#          Meeting", "Wednesday Clinic"); the nurse typing a patient name into the subject line is
#          not. Case survives `norm_subject` (which only strips Re:/Fw:/[tags] and collapses space).
#       2. 2–3 tokens only, and NO token may be an ordinary work/time/clinic word (STOPWORDS below).
#       3. a correspondent at a PHI-screen domain who is NOT the OWNER (`OWNER_RX`). The owner's own
#          address is usually at that domain, so counting it makes the test "any work thread" and the
#          narrowing evaporates. Scoped to the whole participant list, NOT to non-owner SENDERS: some
#          real holds are the owner's sent-ONLY replies to the clinic nurse (her original never made
#          the export), so a sender-side filter leaves no sender at all and fails OPEN.
#     Measured on the store this kit came from: every held thread a patient name, 0 FPs. STOPWORDS is
#     what buys that: as many again had the same shape and are saved by it — "checking in", "master
#     calendar", "this week", "vacation next week", "grant writing support", "zoom meeting today".
#     An over-hold is recoverable: the thread lands in the PHI-exclusion ledger, which is exactly the
#     surface the owner reviews, and `_sources/outlook-released.txt` releases it. Extend STOPWORDS rather
#     than loosening the shape when a non-name subject slips through. Do NOT stop-word words that are
#     also common surnames (brown, young, …): a patient named that would leak.
BARE_NAME_STOPWORDS = frozenset("""
a an and at be by for from in into of on or re the to with about via cc fwd
i me my we our us you your he she they them it its his her their this that these those
all any more most new next last other same some such both each every
today tomorrow yesterday morning afternoon evening tonight now soon later week weeks weekend
month months year years day days date dates time times am pm hour hours minute minutes
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
jan feb mar apr may jun jul aug sep sept oct nov dec spring fall winter
january february march april june july august september october november december
q1 q2 q3 q4 fy eod eow asap
meeting meetings call calls zoom teams webex huddle rounds clinic clinics visit visits appt appts
appointment appointments schedule scheduling agenda calendar invite rsvp reminder reminders
follow up update updates question questions thanks thank hi hello fyi info information
help favor request requests intro introduction chat sync catch touch base check checkin checking
paper papers draft drafts manuscript abstract poster slides deck decks figure figures
data dataset datasets analysis analyses results result templates template report reports
review reviews revision revisions comments comment feedback thoughts ideas idea notes note
grant grants budget invoice award proposal protocol irb submission letter letters
form forms order orders med meds rx rxs msg msgs message messages
lab labs project projects study studies network networks brain imaging scan scans motion measures
email address addresses phone number numbers link links file files folder doc docs document
leadership team teams staff faculty department division program programs committee
policy policies training onboarding badge parking travel reimbursement payroll benefits
vacation pto leave coverage remote office door room rooms building desk
gift card cards food lunch dinner coffee
epic mychart chart charts referral referrals consult consults patient patients
resident residents fellow fellows student students mentee mentor
important urgent quick small final pending remaining open closed
master main general misc stuff things items item list lists
pip dd aa oo ooo tbd na
mentioned discussed attached below above talk talks presentation seminar upcoming
school work home access account password login version versions changes change edits edit
plan plans steps timeline deadline status summary recap confirm confirmation receipt payment
expense expenses survey poll announcement announcements notice notification notifications
welcome congrats congratulations holiday holidays writing scoring
""".split())
BARE_NAME_TOKEN = re.compile(r"[a-z][a-z'\-]+$")


def is_bare_name_subject(norm_subj):
    """True iff the NORMALIZED subject is 2-3 all-lowercase alphabetic tokens, none of them an
    ordinary work/time/clinic word. Case-sensitive on purpose — see the (e) note above."""
    toks = (norm_subj or "").split()
    if not 2 <= len(toks) <= 3:
        return False
    return all(BARE_NAME_TOKEN.match(t) and t not in BARE_NAME_STOPWORDS for t in toks)


# --- clinical-task senders: a DROP rule, not a PHI hold ----------------------------------------------
# Clinic staff whose every message is a task about an identifiable patient (check a message, call a
# patient, sign something) are not durable context — they are worklist chatter. Dropping them by RULE
# also stops the distiller from burning tokens rediscovering that every time.
#
# Configured, not coded: `clinical_task_senders` in _manifest/constants/mail_routing.json (exact,
# lower-case). Justify each sender from `_sources/outlook-index.jsonl` (the sender's own subject
# distribution), not from a display name — display names rarely carry an RN/NP credential.


def clinical_task_drop(members):
    """True iff EVERY non-owner sender in the thread is a configured clinical-task sender. The `every`
    guard is deliberate: a nurse cc'd onto (or replying into) a research/collaborator thread must
    NOT drag that thread out of the store — an unlisted third sender fails the rule and the thread
    falls through to the normal PHI/distiller path (the conservative error for a DROP)."""
    others = {(m.get("from_addr") or "").lower() for m in members if not m.get("sent")}
    others.discard("")
    return bool(others) and others <= ROUTING.clinical_task_senders

# --- keep/drop: bulk / newsletter / system noise --------------------------------------------------
# NOT keyed on the system SENDER, deliberately. The obvious fix for the delegation-spam leak below
# was to add an employer's system-list senders (`people-ops@`, `hr@`, `it-helpdesk@`, ...) to
# this pattern. That drops real notes: genuine staff-lifecycle events (a staff request, an end-date
# change, a departure) can arrive through the same robot as the spam, so the sender carries no
# signal and a sender rule costs exactly the content the store should hold. The leak is handled by
# NOISE_SUBJ below instead (subject-keyed). Do not add such sender rules here.
# Note also that a bulk DROP is quieter than a PHI hold: bulk threads never reach KEEP, so
# `outlook_worklist`'s "distilled-but-now-held" warning cannot see them — nothing would have told us.
NOREPLY_RX = re.compile(
    r'(^|[._-])(no-?reply|do-?not-?reply|donotreply|noreply|mailer|bounce|bounces|notification|'
    r'notifications|onbehalfof|comments-noreply|calendar-notification|automated|auto-?confirm|'
    r'newsletter|news|updates?|alerts?|mailings?|marketing|campaign|list|listserv)@', re.I)
NEWSLETTER_DOM = re.compile(
    r'(doximity\.com|connectedcommunity\.org|manuscriptcentral\.com|editorialmanager\.com|'
    r'calendly\.com|slack\.com|mail\.slack\.com|substack\.com|mailchimp|constantcontact|sendgrid|'
    r'eventbrite|linkedin\.com|researchgate\.net|doodle\.com|surveymonkey|qualtrics)', re.I)
NOISE_SUBJ = re.compile(
    r'weekly digest|daily digest|newsletter|unsubscribe|out of office|automatic reply|read:|'
    r'delivery (status|has failed)|undeliverable|webinar|save the date|renewal notice|'
    r'open enrollment|payroll|paystub|mandatory training|expense report|'
    r'system (maintenance|downtime)|survey:|take the survey|'
    # Auto-notifications about the mail system's own plumbing (a delegation, an account). Subject-
    # keyed, not sender-keyed (see NOREPLY_RX above). Deliberately NOT here: notices that name a
    # real person's real event. Add your own institution's recurring bulk subjects to this list.
    r'approval delegation|delegation pending|'
    r'account (has been )?(locked|expired|disabled)|password (will )?expire',
    re.I)

# --- provisional domain (headers only; the distiller reads the body and sets the authoritative one)
# The configured domain_rules (mail_routing.json) decide; mail at a PHI-screen institution that no
# rule claims is provisionally the day job (`work`); anything else unclaimed is `unsorted`.
DOM_CLINICAL_SUBJ = re.compile(
    r'\b(clinic|inpatient|consult|patient|rounds|referral|econsult|discharge|admission|'
    r'on-?call|epic|order set)\b', re.I)


def provisional_domain(doms, subjects, sent_any):
    domain, subpath = ROUTING.route(sender_domains=doms, subjects=subjects)
    if domain:
        return domain, subpath
    subj = " || ".join(subjects)
    if DOM_CLINICAL_SUBJ.search(subj) or any(phi_screen(d) for d in doms):
        # clinical-institution mail that no rule routes elsewhere -> provisional work
        return "work", None
    return "unsorted", None


PREFIX_RX = re.compile(r'^\s*((re|fw|fwd|回复|答复|回覆|转发|轉發|automatic reply|读)\s*[:：]\s*)+', re.I)
TAG_RX = re.compile(r'\[(external|bulk|suspicious)\]', re.I)


def norm_subject(s):
    s = TAG_RX.sub("", s or "")
    prev = None
    while prev != s:            # strip stacked Re:/Fw:/CJK prefixes
        prev = s
        s = PREFIX_RX.sub("", s)
    return re.sub(r'\s+', " ", s).strip()


def phi_hold(members, subjects, norm_subjects, doms):
    """Deterministic PHI hold decision (fail-closed belt). Returns (held: bool, reason: str|None).
    Catches the institution-specific clinical notifications the general phi_scan heuristic misses."""
    if any(HARD_HOLD_SUBJ.search(s) for s in subjects):
        return True, "roster"
    if any(EPIC_SENDER.search(m.get("from_addr", "") or "") for m in members):
        return True, "epic-sender"
    if any(EPIC_SUBJ.search(s) for s in subjects):
        return True, "epic-message"
    if any(phi_screen(d) for d in doms) and any(MRN_SUBJ.search(s) for s in subjects):
        return True, "mrn-subject"
    # (e) bare all-lowercase person-name subject with a clinical correspondent OTHER THAN OWNER —
    #     see the note above. The owner's own address is usually itself at a PHI-screen domain, so
    #     counting it would turn the documented narrowing ("a sender at a clinical institution") into
    #     "any work thread", the over-hold class that got the earlier bare-name rule removed.
    non_owner_doms = {a.split("@")[-1] for a in
                     (x.lower() for m in members
                      for x in ([m.get("from_addr") or ""] + (m.get("to") or [])
                                + (m.get("cc") or [])) if x)
                     if "@" in a and not OWNER_RX.match(a)}
    if any(phi_screen(d) for d in non_owner_doms) and any(
            is_bare_name_subject(s) for s in norm_subjects):
        return True, "bare-name-subject"
    return False, None


# ---- union-find ---------------------------------------------------------------------------------
class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def dedupe_by_msgid(members):
    """Multiple physical .eml/.mime files can carry the IDENTICAL Message-ID — the same email
    landed in _sources/outlook/ more than once (e.g. saved manually once, then also copied in by
    the automated local-cache export). Union-find correctly threads them together via their shared
    Thread-Index GUID, but without this they'd survive as separate members of one thread's list —
    duplicating that message's body in the rendered .txt the distiller reads. Keep exactly one
    record per unique msgid (first occurrence, order is the caller's business); keep every record
    with no msgid at all (can't safely tell if it's a duplicate)."""
    seen = set()
    out = []
    for m in members:
        mid = m.get("msgid")
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
        out.append(m)
    return out


def build_threads(recs):
    """Return {thrid: [rec, ...]} grouped by conversation. Union-find on Thread-Index GUID, then on
    References/In-Reply-To (never on subject alone)."""
    uf = UF(len(recs))
    by_guid = {}
    by_msgid = {}
    for i, r in enumerate(recs):
        if r.get("msgid"):
            by_msgid.setdefault(r["msgid"], i)   # first occurrence owns the id
    for i, r in enumerate(recs):
        g = r.get("thridx_guid")
        if g:
            if g in by_guid:
                uf.union(i, by_guid[g])
            else:
                by_guid[g] = i
        for ref in (r.get("refs") or []):
            j = by_msgid.get(ref)
            if j is not None:
                uf.union(i, j)

    groups = collections.defaultdict(list)
    for i, r in enumerate(recs):
        groups[uf.find(i)].append(r)

    threads = {}
    for members in groups.values():
        members = dedupe_by_msgid(members)
        # STABLE thrid — INVARIANT as a conversation grows, so a later delta re-export recognizes a
        # continued thread as already-done instead of minting a duplicate/fragment note (a
        # code-review finding). Anchor on the Exchange conversation GUID (identical across every message
        # of a conversation — the 22-byte Thread-Index prefix), else the References-root message-id
        # (a thread's oldest ancestor, cited by every reply's References[0]), else a singleton's own
        # msgid, else the filename. NOT the msgid-SET, which changes the moment a message is added.
        guids = sorted(m["thridx_guid"] for m in members if m.get("thridx_guid"))
        roots = sorted(m["refs"][0] for m in members if m.get("refs"))
        mids = sorted(m["msgid"] for m in members if m.get("msgid"))
        if guids:
            key = "g:" + guids[0]
        elif roots:
            key = "r:" + roots[0]
        elif mids:
            key = "m:" + mids[0]
        else:
            key = "f:" + sorted(m["file"] for m in members)[0]
        thrid = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        members.sort(key=lambda m: (m.get("ts") or "", m.get("file") or ""))
        threads[thrid] = members
    return threads


def is_bulk(members):
    """A thread is bulk/noise iff no message is the owner-sent AND a newsletter/no-reply/system signal
    fires (label, sender pattern, newsletter domain, or a single-message noise subject)."""
    if any(m.get("sent") for m in members):
        return False
    if any("Bulk" in (m.get("labels") or []) for m in members):
        return True
    senders = [m.get("from_addr", "") for m in members]
    if senders and all(NOREPLY_RX.search(s or "") for s in senders):
        return True
    doms = {s.split("@")[-1] for s in senders if "@" in s}
    if any(NEWSLETTER_DOM.search(d) for d in doms):
        return True
    if len(members) == 1 and NOISE_SUBJ.search(members[0].get("subject") or ""):
        return True
    return False


def render(thrid, members):
    parts, labels, dates = set(), set(), []
    for m in members:
        if m.get("from_addr"):
            parts.add(m["from_addr"])
        parts.update(m.get("to") or [])
        parts.update(m.get("cc") or [])
        labels.update(m.get("labels") or [])
        if m.get("date"):
            dates.append(m["date"])
    subject = members[0].get("subject") or ""
    hdr = [
        f"THREAD {thrid} | {len(members)} msg | "
        f"{(dates[0] if dates else '')}..{(dates[-1] if dates else '')} | "
        f"labels: {','.join(sorted(labels))}",
        f"subject: {subject}",
        "participants: " + ", ".join(sorted(p for p in parts if p)),
        "=" * 72,
    ]
    for i, m in enumerate(members, 1):
        hdr.append(
            f"\n--- MSG {i} | {m.get('ts') or m.get('date') or ''} | From: {m.get('from_addr','')} | "
            f"To: {', '.join((m.get('to') or [])[:12])} ---\n{(m.get('body') or '')[:MAXBODY]}")
    return "\n".join(hdr)


def main():
    ap = argparse.ArgumentParser(description="Thread + keep/drop + render the Outlook export.")
    ap.add_argument("--index", default=os.path.join(ROOT, "_sources", "outlook-index.jsonl"))
    ap.add_argument("--keep", default=os.path.join(ROOT, "_sources", "outlook-keep.jsonl"))
    ap.add_argument("--threads-dir", default=os.path.join(ROOT, "_sources", "outlook-threads"))
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(args.index, encoding="utf-8") if l.strip()]
    threads = build_threads(recs)
    os.makedirs(args.threads_dir, exist_ok=True)

    counts = collections.Counter()
    hold_reasons = collections.Counter()
    n_keep = n_drop = n_phi_risk = n_hold = n_drop_rule = 0
    with open(args.keep, "w", encoding="utf-8") as out:
        for thrid, members in sorted(threads.items()):
            if is_bulk(members):
                n_drop += 1
                continue
            subjects = [m.get("subject") or "" for m in members]
            doms = {a.split("@")[-1] for m in members
                    for a in ([m.get("from_addr", "")] + (m.get("to") or []) + (m.get("cc") or []))
                    if "@" in (a or "")}
            norm_subjects = [norm_subject(s) for s in subjects]
            domain, subpath = provisional_domain(doms, subjects, any(m.get("sent") for m in members))
            phi_risk = any(phi_screen(d) for d in doms)
            held_subject, hold_reason = phi_hold(members, subjects, norm_subjects, doms)
            drop_rule = "clinical-task-nurse" if clinical_task_drop(members) else None
            labels = sorted({x for m in members for x in (m.get("labels") or [])})
            parts = sorted({a for m in members
                            for a in ([m.get("from_addr", "")] + (m.get("to") or []) + (m.get("cc") or []))
                            if a})
            dates = sorted(m["date"] for m in members if m.get("date"))
            flags = []
            if phi_risk:
                flags.append("phi-screen")
            if held_subject:
                flags.append("phi-hold:" + hold_reason)
            if domain == "unsorted":
                flags.append("needs-domain-confirm")
            if drop_rule:
                flags.append("drop:" + drop_rule)

            # render the thread text the distiller reads
            open(os.path.join(args.threads_dir, f"{thrid}.txt"), "w", encoding="utf-8").write(
                render(thrid, members))

            rec = {
                "thrid": thrid,
                "domain": domain,
                "subpath": subpath,
                "phi_risk": phi_risk,
                "phi_hold_subject": held_subject,
                "phi_hold_reason": hold_reason,
                # a deterministic NON-PHI drop decision (see clinical_task_drop).
                # `outlook_worklist.py` subtracts these from pending and writes the drop marker —
                # the thread stays in KEEP (and stays rendered + PHI-screened) so the decision is
                # auditable rather than silently vanishing at the select step.
                "drop_rule": drop_rule,
                "flags": flags,
                "subject": subjects[0] if subjects else None,
                "norm_subject": norm_subjects[0] if norm_subjects else None,
                "n_msgs": len(members),
                "sent": any(m.get("sent") for m in members),
                "date_start": dates[0] if dates else None,
                "date_end": dates[-1] if dates else None,
                "labels": labels,
                "participants": parts,
                "files": [m["file"] for m in members],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_keep += 1
            n_phi_risk += phi_risk
            n_hold += held_subject
            if held_subject:
                hold_reasons[hold_reason] += 1
            if drop_rule:
                n_drop_rule += 1
            counts[domain] += 1

    print(f"threads: {len(threads):,}  KEEP: {n_keep:,}  DROP(bulk): {n_drop:,}")
    print(f"  phi_risk(screen): {n_phi_risk:,}   phi_hold_subject(hard-hold): {n_hold:,} "
          f"[{', '.join(f'{k}={v:,}' for k, v in sorted(hold_reasons.items()))}]")
    print(f"  drop_rule(clinical-task-nurse, subtracted by outlook_worklist): {n_drop_rule:,}")
    print(f"KEEP -> {os.path.relpath(args.keep, ROOT)}   threads -> {os.path.relpath(args.threads_dir, ROOT)}/")
    print("\nprovisional domains (KEEP):")
    for dom, n in counts.most_common():
        print(f"  {n:6,}  {dom}")


if __name__ == "__main__":
    main()
