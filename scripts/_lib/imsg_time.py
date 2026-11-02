"""Single owner for iMessage chat.db TIME math: the Apple-epoch constant, the `--since` parser
(ISO-8601 UTC timestamp OR bare YYYY-MM-DD date), and the ns→ISO renderers.

`imsg_select.py`, `imsg_extract.py`, and `bump_watermark.py` all import from here so the watermark
boundary math NEVER drifts across the three scripts — the same single-owner discipline that governs
the ref regex (`_lib/provenance.py`) and the backfill list (`_lib/constants.py`). Do NOT paste a
local copy of the ns formula.

chat.db `message.date` is INTEGER nanoseconds since the Apple epoch (2001-01-01T00:00:00Z). Every
conversion here uses INTEGER arithmetic: a nanosecond value (~8e17 for present-day timestamps)
exceeds 2^53, so float math (e.g. `days * 86400e9`) silently loses precision — the exact bug the
sync hardening removed from imsg_select/imsg_extract once already.
"""
import datetime

APPLE_EPOCH = 978307200            # unix seconds at 2001-01-01T00:00:00Z
NS = 10 ** 9
_UNIX_EPOCH_DATE = datetime.date(1970, 1, 1)
_UNIX_EPOCH_DT = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _is_bare_date(s):
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and "T" not in s and " " not in s


def since_to_apple_ns(since):
    """Parse a `--since` value to ns-since-the-Apple-epoch, for `WHERE message.date > ?`.

    Accepts an ISO-8601 UTC timestamp ("2026-06-30T14:23:01Z", "...+00:00", "...T..." naive→UTC,
    or a space separator) OR a bare date ("2026-06-30", treated as 00:00:00Z for back-compat).
    Empty/None → 0 (no lower bound). The query uses strict `>`, so passing an exact prior timestamp
    fetches only STRICTLY-newer messages. Integer math throughout."""
    if not since:
        return 0
    s = since.strip()
    if _is_bare_date(s):
        d = datetime.date.fromisoformat(s)
        return (d - _UNIX_EPOCH_DATE).days * 86400 * NS - APPLE_EPOCH * NS
    iso = s.replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    delta = dt - _UNIX_EPOCH_DT
    unix_ns = (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000
    return unix_ns - APPLE_EPOCH * NS


def apple_ns_to_iso_second(ns, *, ceil=False):
    """A chat.db ns instant → ISO-8601 UTC to the SECOND ("2026-06-30T14:23:01Z").

    `ceil=True` rounds UP to the next whole second and is what the iMessage watermark stores: the
    query is strict `>`, so a watermark that is >= the exact max message instant makes a same-day
    re-sync re-fetch NOTHING already seen (no overlap), while missing nothing — `(max_ns, ceil]`
    holds no message, because `max_ns` was the newest message at extract time. Integer math."""
    unix_ns = ns + APPLE_EPOCH * NS
    unix_s = -(-unix_ns // NS) if ceil else unix_ns // NS
    return datetime.datetime.fromtimestamp(unix_s, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apple_ns_to_iso_minute(ns):
    """A chat.db ns instant → 'YYYY-MM-DD HH:MM' (per-message line rendering). Integer math."""
    unix_s = (ns + APPLE_EPOCH * NS) // NS
    return datetime.datetime.fromtimestamp(unix_s, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
