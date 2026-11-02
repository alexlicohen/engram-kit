#!/usr/bin/env python3
"""OAuth2 device-code auth for Microsoft Graph — replaces the manual Outlook `.eml` export.

Stdlib-only (urllib), matching this project's no-new-dependency convention. Implements the
protocol directly per Microsoft's own docs (learn.microsoft.com/entra/identity-platform/
v2-oauth2-device-code) rather than pulling in MSAL.

CONFIGURATION (environment, never committed):
  ENGRAM_GRAPH_CLIENT_ID   the public-client (application) id to sign in with: your own Azure/Entra
                           app registration (public client, delegated `Mail.Read`, "allow public
                           client flows" on), or a first-party public client your tenant already
                           trusts. REQUIRED — there is no default.
  ENGRAM_GRAPH_TENANT      the authority's tenant segment: your tenant id or domain, or
                           `organizations` (any work/school account; the default).
A device-code flow is a public-client flow, so there is no client secret. Whether your tenant
permits user consent for the app + `Mail.Read` is the one real unknown; if the device-code request
itself errors (e.g. AADSTS650053 unauthorized_client / admin consent required, or a Conditional
Access block), that's the answer — fall back to the manual `.eml` export (SYNC.md › Outlook).

Token cache lives OUTSIDE the repo (`~/.engram_outlook_token_cache.json`, chmod 600) — never in
`_sources/` or anywhere git-adjacent. It holds a live refresh token; per CLAUDE.md, secrets never
belong in the repo, encrypted or not (`_vault/` is for credentials that must be committed; this
one shouldn't be).

Usage:
    python3 scripts/outlook_graph_auth.py login    # interactive device-code sign-in (once)
    python3 scripts/outlook_graph_auth.py status    # show cache state, no secrets printed
    python3 scripts/outlook_graph_auth.py token     # print a valid access token to stdout
                                                      # (auto-refreshes; re-runs login if no cache)

`outlook_graph_export.py` imports `get_access_token()` directly rather than shelling out.
"""
import argparse
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Both from the environment (see the module docstring); nothing tenant-specific is committed.
TENANT = os.environ.get("ENGRAM_GRAPH_TENANT", "organizations")  # default: any work/school account
CLIENT_ID = os.environ.get("ENGRAM_GRAPH_CLIENT_ID", "")
SCOPES = "offline_access Mail.Read"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
DEVICECODE_URL = f"{AUTHORITY}/oauth2/v2.0/devicecode"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"

CACHE_PATH = os.path.expanduser("~/.engram_outlook_token_cache.json")

# Refresh a bit before actual expiry to avoid racing a request against a token that expires
# mid-flight.
EXPIRY_SAFETY_MARGIN_S = 120


HTTP_TIMEOUT_S = 15


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        try:
            return json.loads(payload), e.code
        except json.JSONDecodeError:
            return {"error": "http_error", "error_description": payload}, e.code


def _save_cache(token_resp):
    cache = {
        "access_token": token_resp["access_token"],
        "refresh_token": token_resp.get("refresh_token"),
        "expires_at": time.time() + int(token_resp.get("expires_in", 3600)),
        "scope": token_resp.get("scope", SCOPES),
    }
    fd = os.open(CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(cache, f)
    os.chmod(CACHE_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600, belt-and-suspenders on umask


def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH) as f:
        return json.load(f)


def _require_client_id():
    if not CLIENT_ID:
        sys.exit("ENGRAM_GRAPH_CLIENT_ID is not set — export your app registration's client id "
                 "(see the docstring of scripts/outlook_graph_auth.py), or use the manual .eml export.")


def device_code_login():
    """Interactive: request a device code, print instructions, poll until the user completes
    sign-in in their own browser. Never sees or handles the user's actual credentials — that
    exchange happens entirely inside Microsoft's login page in the user's browser session."""
    _require_client_id()
    resp, status = _post_form(DEVICECODE_URL, {"client_id": CLIENT_ID, "scope": SCOPES})
    if status != 200:
        print(f"devicecode request failed ({status}): {resp.get('error')} — "
              f"{resp.get('error_description', '')[:500]}", file=sys.stderr)
        sys.exit(1)

    print(resp["message"])
    print(f"(user_code={resp['user_code']}  verification_uri={resp['verification_uri']}  "
          f"expires_in={resp['expires_in']}s)")

    device_code = resp["device_code"]
    interval = resp.get("interval", 5)
    deadline = time.time() + resp["expires_in"]

    while time.time() < deadline:
        time.sleep(interval)
        token_resp, status = _post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
            "device_code": device_code,
        })
        if status == 200:
            _save_cache(token_resp)
            print(f"login OK — token cached at {CACHE_PATH} (scope: {token_resp.get('scope')})")
            return
        err = token_resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"login failed: {err} — {token_resp.get('error_description', '')[:800]}",
              file=sys.stderr)
        sys.exit(1)

    print("device code expired before sign-in completed — re-run to try again", file=sys.stderr)
    sys.exit(1)


def _refresh(refresh_token):
    token_resp, status = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": SCOPES,
    })
    if status != 200:
        return None
    # A refresh-grant response is allowed to omit refresh_token (meaning: keep using the one you
    # already have) — don't let _save_cache null out a still-valid cached refresh token just
    # because this particular response didn't repeat it.
    if "refresh_token" not in token_resp:
        token_resp["refresh_token"] = refresh_token
    _save_cache(token_resp)
    return token_resp["access_token"]


def get_access_token(interactive_fallback=True):
    """Returns a valid access token, refreshing or (if interactive_fallback) re-running the
    device-code login as needed. Raises RuntimeError if no valid token can be obtained
    non-interactively and interactive_fallback is False."""
    cache = _load_cache()
    if cache and cache.get("access_token") and time.time() < cache["expires_at"] - EXPIRY_SAFETY_MARGIN_S:
        return cache["access_token"]
    if cache and cache.get("refresh_token"):
        tok = _refresh(cache["refresh_token"])
        if tok:
            return tok
    if not interactive_fallback:
        raise RuntimeError("no valid cached token and interactive_fallback=False — "
                            "run `python3 scripts/outlook_graph_auth.py login` first")
    device_code_login()
    cache = _load_cache()
    return cache["access_token"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "status", "token"])
    args = ap.parse_args()

    if args.command == "login":
        device_code_login()
    elif args.command == "status":
        cache = _load_cache()
        if not cache:
            print("no token cache — never logged in")
            return
        remaining = cache["expires_at"] - time.time()
        print(f"cache: {CACHE_PATH}")
        print(f"access_token: {'valid' if remaining > 0 else 'expired'} "
              f"({remaining:+.0f}s from now)")
        print(f"refresh_token: {'present' if cache.get('refresh_token') else 'absent'}")
        print(f"scope: {cache.get('scope')}")
    elif args.command == "token":
        print(get_access_token())


if __name__ == "__main__":
    main()
