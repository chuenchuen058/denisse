"""One-time Microsoft Graph sign-in for Denisse's Outlook drafts.

Chuen runs this himself, once, in a terminal:

    .venv\\Scripts\\python.exe scripts\\graph_signin.py           # sign in
    .venv\\Scripts\\python.exe scripts\\graph_signin.py --status  # who is signed in
    .venv\\Scripts\\python.exe scripts\\graph_signin.py --sign-out

Signing in prints a short code and Microsoft's own sign-in address; he opens
it, types the code and signs in there with his KCL account. No password ever
passes through Denisse. What is asked for is email_drafts.json graph.scopes
(Mail.ReadWrite: create drafts -- never Mail.Send). The token lands in
graph.token_cache, which email_drafts.py then reuses silently; until this has
been run, drafts are .eml files as before.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import email_drafts  # noqa: E402


def status(cache=None) -> list:
    cache = cache or email_drafts.load_token_cache()
    return [a.get("username", "") for a in email_drafts.cached_accounts(cache)]


def sign_out() -> bool:
    path = email_drafts._cache_path()
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def sign_in(out=print, app=None, cache=None) -> str:
    """Device-code sign-in; returns the account name. Raises on failure."""
    cache = cache or email_drafts.load_token_cache()
    app = app or email_drafts.graph_app(cache)
    scopes = email_drafts.CONFIG["graph"]["scopes"]
    hint = email_drafts.CONFIG["graph"]["account"].strip()
    flow = app.initiate_device_flow(scopes=scopes, **({"login_hint": hint} if hint else {}))
    if "user_code" not in flow:
        raise RuntimeError(flow.get("error_description") or str(flow))
    out(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description") or result.get("error") or str(result))
    email_drafts.save_token_cache(cache)
    return (result.get("id_token_claims") or {}).get("preferred_username", "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="show the signed-in account(s)")
    ap.add_argument("--sign-out", action="store_true", help="forget the saved sign-in")
    a = ap.parse_args(argv)
    if a.status:
        who = status()
        print("Signed in: " + ", ".join(who) if who else "Not signed in.")
        return 0
    if a.sign_out:
        print("Signed out." if sign_out() else "Wasn't signed in.")
        return 0
    try:
        who = sign_in()
    except Exception as exc:
        print(f"Sign-in failed: {exc}")
        return 1
    print(f"Signed in as {who}. Denisse's drafts now go to your Outlook Drafts folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
