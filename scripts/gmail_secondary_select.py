#!/usr/bin/env python3
"""Build the KEEP work-list for a SECONDARY mailbox (a separate account, from an mbox/Takeout export).

Scope: the canonical secondary account is a DEDICATED one (e.g. for a company the owner runs), so
unlike the primary pass ("everything I replied to"), we keep every *substantive human* thread
regardless of whether the owner replied — business-of-record (legal/IP, finance, banking,
fundraising, vendors, board) is durable even when the owner was only a CC. We only drop threads
that are 100% automated/bulk, plus Spam/Trash. The distiller still drops non-durable
threads at distillation (reads bodies), same as the personal pass.

Emits one record per KEEP thread (SAME shape as gmail-keep.jsonl so mbox_extract.py and
the distiller work unchanged), plus a `msgids` list per thread for cross-account dedup.

Usage: python3 scripts/gmail_secondary_select.py <index.jsonl> <out-keep.jsonl>
"""
import sys, os, json, re, collections

# Reuse the primary pass's domain router + owner addresses + PHI screen (DRY).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmail_select import provisional_domain, phi_screen, SELF  # noqa: E402

DROP_LABELS = {'Spam', 'Trash'}

# A message is "automated" (robot-only) if its sender local-part or domain is a
# notification/no-reply sender, or it carries bulk/list/auto-submitted headers.
NOREPLY_LOCAL = re.compile(
    r'(^|[._-])(no[._-]?reply|donotreply|do[._-]?not[._-]?reply|noreply|notification|'
    r'notifications|mailer|mailer[-_]?daemon|postmaster|bounce[ds]?|auto[-_]?confirm|'
    r'alerts?|receipts?|statements?|invoices?|billing|mailbot|notify|updates?|news)\b', re.I)

# Pure-robot sender domains (e-sign / mailing-list / account-notice blasts). Conservative: only domains that are
# essentially never a human counterparty. Extend for your own mailbox.
AUTO_DOMAINS = {
    'docusign.net', 'mail.docusign.net', 'echosign.com', 'adobesign.com', 'rightsignature.com',
    'mailchimp.com', 'constantcontact.com', 'accounts.google.com', 'mail-noreply.google.com',
}


def is_auto(rec):
    """True if this single message looks machine-generated."""
    fa = (rec.get('from_addr') or '').lower()
    if not fa:
        return True
    local, _, dom = fa.partition('@')
    if dom in AUTO_DOMAINS:
        return True
    if NOREPLY_LOCAL.search(local):
        return True
    if rec.get('list_unsub'):
        return True
    if rec.get('precedence') in ('bulk', 'list', 'junk'):
        return True
    auto = rec.get('auto_submitted')
    if auto and auto != 'no':
        return True
    return False


def main():
    idx_path, out_path = sys.argv[1], sys.argv[2]
    thr = collections.OrderedDict()
    for line in open(idx_path, encoding='utf-8'):
        r = json.loads(line)
        t = r.get('thrid') or f"_n{r['start']}"
        d = thr.get(t)
        if d is None:
            d = thr[t] = {'thrid': t, 'labels': set(), 'doms': set(), 'human': False,
                          'sent': False, 'subjects': [], 'parts': set(), 'dates': [],
                          'msgs': [], 'msgids': []}
        d['labels'].update(r.get('labels') or [])
        if 'Sent' in (r.get('labels') or []) or r.get('from_addr') in SELF:
            d['sent'] = True
        if not is_auto(r):
            d['human'] = True            # >=1 plausibly-human message -> thread is keepable
        fa = r.get('from_addr')
        if fa:
            d['parts'].add(fa)
            if fa not in SELF:
                d['doms'].add(fa.split('@')[-1])
        d['parts'].update(r.get('to') or [])
        d['parts'].update(r.get('cc') or [])
        if r.get('subject'):
            d['subjects'].append(r['subject'])
        if r.get('date'):
            d['dates'].append(r['date'])
        if r.get('msgid'):
            d['msgids'].append(r['msgid'])
        d['msgs'].append([r['start'], r['end']])

    counts = collections.Counter()
    phi = dropped_auto = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        for t, d in thr.items():
            if DROP_LABELS & d['labels']:
                continue
            if not d['human']:              # 100%-automated thread -> drop pre-distill
                dropped_auto += 1
                continue
            domain, subpath = provisional_domain(d['labels'], d['doms'], d['subjects'])
            phi_risk = any(phi_screen(x) for x in d['doms'])
            dates = sorted(x for x in d['dates'] if x)
            flags = []
            if phi_risk:
                flags.append('phi-screen')
            if domain == 'unsorted':
                flags.append('needs-domain-confirm')
            if not d['sent']:
                flags.append('cc-only')     # kept under dedicated-account scope (the owner didn't reply)
            rec = {
                'thrid': t,
                'domain': domain,
                'subpath': subpath,
                'phi_risk': phi_risk,
                'flags': flags,
                'subject': d['subjects'][0] if d['subjects'] else None,
                'n_msgs': len(d['msgs']),
                'date_start': dates[0] if dates else None,
                'date_end': dates[-1] if dates else None,
                'labels': sorted(d['labels']),
                'participants': sorted(p for p in d['parts'] if p),
                'msgids': d['msgids'],
                'offsets': d['msgs'],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            counts[domain] += 1
            phi += phi_risk

    kept = sum(counts.values())
    print(f"KEEP threads: {kept:,}   (dropped {dropped_auto:,} fully-automated; "
          f"phi-screen flagged: {phi:,})\n")
    for dom, n in counts.most_common():
        print(f"  {n:6,}  {dom}")


if __name__ == '__main__':
    main()
