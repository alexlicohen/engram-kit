#!/usr/bin/env python3
"""Stage B: triage the per-message index into threads + keep/drop classification.

Reads the small gmail-index.jsonl (NOT the multi-GB mbox), groups messages by
X-GM-THRID, applies the keep/drop ruleset (automated senders dropped) + the configured
domain rules (`_manifest/constants/mail_routing.json`, see scripts/_lib/mail_routing.py), and
writes one record per thread to gmail-threads.jsonl (gitignored working file) plus a
human-readable summary to stdout. A human thread no rule matches is `review` — a scope
decision for the owner, not a guess.

A sizing/scoping aid for a first BACKFILL of a large mailbox; incremental syncs use
gmail_select.py / gmail_secondary_select.py.

This does NOT touch _manifest/manifest.jsonl — materializing manifest rows is a
separate, scope-gated step once the owner sets which clusters to distill.

Usage:
  python3 scripts/mbox_triage.py <index.jsonl> <out-threads.jsonl>
"""
import sys, os, json, re, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.actions import self_emails      # noqa: E402  (single owner of the owner's addresses)
from _lib.mail_routing import ROUTING     # noqa: E402  (single owner of header routing)

SELF = self_emails(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- DROP signals (automated / non-durable) -------------------------------------
DROP_LOCAL = re.compile(
    r'(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notification|notify|mailer|'
    r'mailer-daemon|bounce|postmaster|auto[-_.]?(confirm|reply|submit)|alerts?@|'
    r'updates?@|news(letter)?@|info@|support@|noreply)', re.I)
DROP_DOMAIN = re.compile(
    r'(docusign|adobesign|echosign|rightsignature|sharefile|intuit\.com|quickbooks|'
    r'e?\.?usps\.com|ups\.com|fedex\.com|ship-?station|mailchimp|mailchimpapp|sendgrid|'
    r'sparkpost|constantcontact|substack|eventbrite|meetup\.com|linkedin\.com|facebookmail|'
    r'twitter|x\.com|instagram|amazon\.com|amazonses|paypal|venmo|stripe|squareup|'
    r'ticketmaster|google\.com|youtube|calendar-notification|nextdoor|zillow|redfin|'
    r'kickstarter)', re.I)   # generic robot senders; extend for your mailbox
# labels that mean the owner already discarded it
DROP_LABELS = {"Spam", "Trash"}

SUBJ_TRAVEL = re.compile(r'(\btrip\b|travel|itinerary|flight|reservation|booking confirmation)', re.I)


def addr_is_automated(addr):
    if not addr:
        return True
    return bool(DROP_LOCAL.search(addr) or DROP_DOMAIN.search(addr))


def classify(thread):
    """Return (decision, domain, subpath, flags) for a grouped thread dict."""
    msgs = thread["msgs"]
    labels = thread["labels"]
    addrs = thread["addrs"]
    subjects = " || ".join(s for s in thread["subjects"] if s)
    flags = []

    # hard drop: discarded mail
    if DROP_LABELS & set(labels):
        return ("drop", None, None, ["discarded"])

    # a "human" message = a non-self, non-automated sender, OR a message the owner sent
    human_senders = [m["from_addr"] for m in msgs
                     if m["from_addr"] and m["from_addr"] not in SELF and not addr_is_automated(m["from_addr"])]
    owner_sent = any(m["from_addr"] in SELF for m in msgs)
    has_human = bool(human_senders) or (owner_sent and any(
        a for a in addrs if a not in SELF and not addr_is_automated(a)))

    if not has_human:
        return ("drop", None, None, ["automated"])

    # domain routing: the configured rules (first match wins); none -> a scope decision
    doms = {a.split("@")[-1] for a in addrs if a and "@" in a and a not in SELF}
    primary, subpath = ROUTING.route(labels=labels, sender_domains=doms,
                                     subjects=thread["subjects"], addresses=addrs)
    if not primary:
        return ("review", None, None, ["human-unclassified"])
    if not subpath and primary == "personal" and SUBJ_TRAVEL.search(subjects):
        subpath = "travel"
    return ("keep", primary, subpath, flags)


def main():
    idx_path, out_path = sys.argv[1], sys.argv[2]
    threads = collections.OrderedDict()
    n_msgs = 0
    for line in open(idx_path, encoding='utf-8'):
        r = json.loads(line)
        n_msgs += 1
        tid = r["thrid"] or f"_nomsgid:{r['start']}"
        t = threads.get(tid)
        if t is None:
            t = threads[tid] = {"thrid": tid, "msgs": [], "labels": set(),
                                "addrs": set(), "subjects": [], "dates": []}
        t["msgs"].append({"from_addr": r["from_addr"], "start": r["start"], "end": r["end"]})
        t["labels"].update(r.get("labels") or [])
        if r["from_addr"]:
            t["addrs"].add(r["from_addr"])
        t["addrs"].update(r.get("to") or [])
        t["addrs"].update(r.get("cc") or [])
        if r["subject"]:
            t["subjects"].append(r["subject"])
        if r["date"]:
            t["dates"].append(r["date"])

    report = collections.Counter()
    cluster_counts = collections.Counter()
    overlap_counts = collections.Counter()
    with open(out_path, "w", encoding="utf-8") as out:
        for tid, t in threads.items():
            t["labels"] = sorted(t["labels"])
            decision, domain, subpath, flags = classify(t)
            report[decision] += 1
            if decision == "keep":
                cluster_counts[domain] += 1
                for f in flags:
                    if f.startswith("overlap:"):
                        overlap_counts[f] += 1
            dates = sorted(d for d in t["dates"] if d)
            rec = {
                "thrid": tid,
                "decision": decision,
                "domain": domain,
                "subpath": subpath,
                "flags": flags,
                "subject": (t["subjects"][0] if t["subjects"] else None),
                "n_msgs": len(t["msgs"]),
                "date_start": dates[0] if dates else None,
                "date_end": dates[-1] if dates else None,
                "labels": t["labels"],
                "participants": sorted(t["addrs"]),
                "offsets": [[m["start"], m["end"]] for m in t["msgs"]],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Messages: {n_msgs:,}   Threads: {len(threads):,}\n")
    print("Decision breakdown:")
    for d in ("keep", "review", "drop"):
        print(f"  {d:8s} {report[d]:>8,}")
    print("\nKEEP by cluster (candidate distillation volume):")
    for c, n in cluster_counts.most_common():
        print(f"  {c:18s} {n:>6,}")
    print("\nCross-cluster overlaps flagged:")
    for f, n in overlap_counts.most_common():
        print(f"  {f:22s} {n:>5,}")
    print(f"\n  review (human, unclustered — needs scope decision): {report['review']:,}")


if __name__ == "__main__":
    main()
