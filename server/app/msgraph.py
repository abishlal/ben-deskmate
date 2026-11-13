"""Microsoft Graph access for To Do and OneNote, using the user's own delegated sign-in.

Sign in once (device code; the token cache is kept in the data volume):
    docker compose exec server python -m app.msgraph login
Other commands: status, logout
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import msal

from . import config

SCOPES = ["Tasks.ReadWrite", "Notes.ReadWrite", "Calendars.ReadWrite", "User.Read"]
CACHE_FILE = config.DATA_DIR / "msal_cache.json"
_http = httpx.AsyncClient(base_url="https://graph.microsoft.com/v1.0", timeout=30)


class GraphError(Exception):
    pass


def configured() -> bool:
    return bool(config.MS_CLIENT_ID)


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if CACHE_FILE.exists():
        cache.deserialize(CACHE_FILE.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
        os.chmod(CACHE_FILE, 0o600)


def _app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        config.MS_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{config.MS_TENANT}",
        token_cache=cache,
    )


def account() -> str | None:
    """The signed-in account's user name, read from the local cache (no network)."""
    if not configured() or not CACHE_FILE.exists():
        return None
    try:
        accounts = _app(_load_cache()).get_accounts()
    except Exception:
        return None
    return accounts[0].get("username") if accounts else None


def signed_in() -> bool:
    return account() is not None


def _access_token() -> str | None:
    if not configured():
        return None
    cache = _load_cache()
    app = _app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    return result.get("access_token") if result else None


async def call(method: str, path: str, *, json=None, content: str | None = None,
               headers: dict | None = None, params: dict | None = None, text: bool = False):
    """One Graph request. Returns parsed JSON, or the raw body when text=True."""
    token = await asyncio.to_thread(_access_token)
    if not token:
        raise GraphError("Not signed in to Microsoft. The user needs to run: "
                         "docker compose exec server python -m app.msgraph login")
    try:
        r = await _http.request(method, path, json=json, content=content, params=params,
                                headers={"Authorization": f"Bearer {token}", **(headers or {})})
    except httpx.HTTPError as e:
        raise GraphError(f"Could not reach Microsoft Graph: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json()["error"]["message"]
        except Exception:
            detail = r.text[:200]
        raise GraphError(f"Microsoft Graph refused the request ({r.status_code}): {detail}")
    if text:
        return r.text
    return r.json() if r.content else {}


# --- command line ------------------------------------------------------------

def _login() -> int:
    if not configured():
        print("MS_CLIENT_ID is not set in .env. See the README section 'Microsoft To Do and OneNote'.")
        return 1
    cache = _load_cache()
    app = _app(cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("Could not start sign-in:", flow.get("error_description") or flow)
        return 1
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)  # waits until you finish in the browser
    _save_cache(cache)
    if "access_token" not in result:
        print("Sign-in failed:", result.get("error_description") or result.get("error"))
        return 1
    print("Signed in as", result.get("id_token_claims", {}).get("preferred_username", "your account"))
    print("The pet will use Microsoft To Do and OneNote from its next conversation.")
    return 0


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "login":
        return _login()
    if cmd == "logout":
        CACHE_FILE.unlink(missing_ok=True)
        print("Signed out. The pet is back to its local todo list and notes.")
        return 0
    print("configured:", configured(), "| signed in as:", account() or "nobody")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
