"""MSAL device-code auth, with the token cache in the macOS Keychain.

Interactive sign-in CANNOT happen inside the MCP server: it is a stdio JSON-RPC
process with no TTY whose stdout is the protocol. Prompting there would corrupt
the transport and hang on a stdin that belongs to Claude Desktop.

So auth is strictly out-of-band. `onenote-auth.sh` runs this module's __main__
in a real terminal. From then on the server silently acquires access tokens from
the cached refresh token.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
from pathlib import Path

import keyring
import msal

log = logging.getLogger(__name__)

SCOPES = ["Notes.ReadWrite"]                       # NOT Notes.ReadWrite.All
AUTHORITY = "https://login.microsoftonline.com/common"
KEYRING_SERVICE = "job-tracker-mcp"
KEYRING_USER = "msal-token-cache"
FALLBACK_DIR = Path.home() / ".job-tracker-mcp"
FALLBACK_FILE = FALLBACK_DIR / "msal_cache.json"

# A macOS Keychain "allow access" dialog BLOCKS; it does not raise. In a process
# Claude Desktop launched with no TTY and no window, nobody can click the button
# and the server hangs forever. A hung tool is strictly worse than a failed one,
# because the model cannot see it. So every Keychain read is time-boxed.
KEYCHAIN_TIMEOUT_S = 5.0

ACCOUNT_KEY = "home_account_id"


class AuthError(Exception):
    def __init__(self, message: str, reason: str = "not_authenticated"):
        super().__init__(message)
        self.reason = reason


def auth_command() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return str(root / "onenote-auth.sh")


def not_authenticated(reason: str = "not_authenticated") -> AuthError:
    return AuthError(
        f"OneNote is not authenticated, or the saved sign-in expired. "
        f"Run {auth_command()} in a terminal, then retry.",
        reason=reason,
    )


def client_id() -> str:
    cid = os.environ.get("JOB_TRACKER_MS_CLIENT_ID", "").strip()
    if not cid:
        raise AuthError(
            "JOB_TRACKER_MS_CLIENT_ID is not set. Register an app at "
            "https://entra.microsoft.com (public client, delegated Notes.ReadWrite, "
            "allow public client flows), then set it in the Claude Desktop config "
            "`env` block -- Claude Desktop does not read your shell profile.",
            reason="not_configured",
        )
    return cid


# ------------------------------------------------------------- token cache

def _timed(fn, *args):
    """Run fn on a worker thread; treat a hang as a failure, never a wait."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(fn, *args).result(timeout=KEYCHAIN_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise AuthError(
                f"The macOS Keychain did not respond within {KEYCHAIN_TIMEOUT_S:.0f}s. "
                f"It is probably showing an 'allow access' dialog that this process "
                f"cannot answer -- usually because .venv was rebuilt since you last "
                f"signed in. Run {auth_command()} in a terminal to re-authorize.",
                reason="keychain_timeout",
            ) from None


def _read_cache_blob() -> str | None:
    try:
        blob = _timed(keyring.get_password, KEYRING_SERVICE, KEYRING_USER)
        if blob:
            return blob
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 -- headless, no Keychain, locked, ...
        log.warning("keyring read failed (%s); falling back to file", exc)
    if FALLBACK_FILE.exists():
        return FALLBACK_FILE.read_text()
    return None


def _write_cache_blob(blob: str) -> None:
    try:
        _timed(keyring.set_password, KEYRING_SERVICE, KEYRING_USER, blob)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("keyring write failed (%s); falling back to file", exc)
    FALLBACK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(FALLBACK_FILE, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(blob)


def load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    blob = _read_cache_blob()
    if blob:
        cache.deserialize(blob)
    return cache


def save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        _write_cache_blob(cache.serialize())


def build_app(cache=None) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id(), authority=AUTHORITY, token_cache=cache or load_cache()
    )


def _select_account(app, home_account_id: str | None):
    """get_accounts() may return several (personal + work).

    Silently taking accounts[0] is how this tool ends up reading the wrong
    notebook, so the chosen account is persisted at sign-in and selected by id.
    """
    accounts = app.get_accounts()
    if not accounts:
        return None
    if home_account_id:
        for a in accounts:
            if a.get("home_account_id") == home_account_id:
                return a
        log.warning("saved account %s not in cache; falling back", home_account_id)
    if len(accounts) > 1:
        log.warning("%d accounts cached; using the first. Re-run %s to choose.",
                    len(accounts), auth_command())
    return accounts[0]


def acquire_token_silent(home_account_id: str | None = None) -> str:
    """Never triggers interactive auth. MSAL handles refresh internally."""
    cache = load_cache()
    app = build_app(cache)
    account = _select_account(app, home_account_id)
    if account is None:
        raise not_authenticated()
    result = app.acquire_token_silent(SCOPES, account=account)
    save_cache(cache)
    if not result or "access_token" not in result:
        raise not_authenticated()
    return result["access_token"]


def account_info(home_account_id: str | None = None) -> dict | None:
    cache = load_cache()
    app = build_app(cache)
    acct = _select_account(app, home_account_id)
    if not acct:
        return None
    return {"username": acct.get("username"),
            "home_account_id": acct.get("home_account_id")}


# ------------------------------------------------------------------ __main__
#
# This block is the ONLY place in the project permitted to call print(): it is a
# terminal program, not the MCP server. Nothing at module scope may print.

def _device_code_flow() -> int:
    cache = load_cache()
    app = build_app(cache)

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("Failed to start device flow:", json.dumps(flow, indent=2), file=sys.stderr)
        return 1

    print()
    print("  " + flow["message"])
    print()
    print("  Waiting for you to finish signing in...")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print("\n  Sign-in failed: "
              f"{result.get('error_description', result.get('error', 'unknown'))}")
        return 1

    save_cache(cache)
    accounts = app.get_accounts()
    acct = accounts[0] if accounts else {}
    home_id = acct.get("home_account_id", "")

    # Persist the chosen account so the server never guesses between
    # a personal and a work sign-in.
    try:
        from job_tracker_mcp.database import Database
        from job_tracker_mcp.onenote.store import OneNoteStore
        db = Database()
        OneNoteStore(db).set_state(ACCOUNT_KEY, home_id)
        db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"\n  WARNING: signed in, but could not record the account: {exc}")

    print(f"\n  Signed in as {acct.get('username', 'unknown')}")
    print("  Token cached. You can close this terminal.\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        raise SystemExit(_device_code_flow())
    except AuthError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        raise SystemExit(2) from None
