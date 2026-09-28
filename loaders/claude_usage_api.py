# SPDX-License-Identifier: AGPL-3.0-only
"""Claude quota from Anthropic's OAuth usage endpoint (the call behind `/usage`).

The statusLine hook only records quota when Claude Code runs on this machine,
so work on a remote server never reaches it. This endpoint reports the same
account-wide 5-hour / weekly utilization from anywhere. It is a metadata read:
no model runs and no token is spent.

The access token is read from Claude Code's own credential store (macOS
Keychain item "Claude Code-credentials", else ~/.claude/.credentials.json).
When it expires, the refresh grant rotates both tokens and updates that same
store before quota polling continues. Failed or malformed refreshes leave the
existing credential untouched.

Calls are spaced by POLL_INTERVAL_SECONDS, and a 429 waits out Retry-After
(at least doubling the interval, capped at an hour).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from loaders.claude_paths import claude_json_path
from usage_common.time_utils import parse_iso8601_utc_or_raise

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path(os.path.expanduser("~/.claude/.credentials.json"))
POLL_INTERVAL_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0
REQUEST_TIMEOUT_SECONDS = 8.0
TOKEN_EXPIRY_MARGIN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ClaudeApiQuota:
    five_hour_percent: float | None
    five_hour_resets_at: float | None  # epoch seconds
    seven_day_percent: float | None
    seven_day_resets_at: float | None
    fetched_at: float


_lock = threading.Lock()
_cached: ClaudeApiQuota | None = None
_next_poll_at = 0.0
_backoff = POLL_INTERVAL_SECONDS
_auth_required = False


def auth_required() -> bool:
    """Whether the last credential check proved interactive login is required."""
    return _auth_required


def account_name() -> str | None:
    """Return Claude Code's non-secret local display name, when available."""
    try:
        data = json.loads(claude_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    for key in ("displayName", "emailAddress", "email", "organizationName"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def latest_quota(*, now: float | None = None) -> ClaudeApiQuota | None:
    """Return the cached API snapshot, polling first when the interval is due."""
    global _cached, _next_poll_at, _backoff
    current = time.time() if now is None else now
    with _lock:
        if current >= _next_poll_at:
            status, quota, retry_after = _poll()
            if quota is not None:
                _cached = quota
                _backoff = POLL_INTERVAL_SECONDS
                _next_poll_at = current + POLL_INTERVAL_SECONDS
            elif status == 429:
                _backoff = min(max(retry_after or 0.0, _backoff * 2), MAX_BACKOFF_SECONDS)
                _next_poll_at = current + _backoff
            else:
                _next_poll_at = current + POLL_INTERVAL_SECONDS
        return _cached


def _poll() -> tuple[int | None, ClaudeApiQuota | None, float | None]:
    global _auth_required
    token = _access_token(time.time())
    if token is None:
        return None, None, None
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "usage-menubar",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            _auth_required = False
            return response.status, parse_usage(payload, fetched_at=time.time()), None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            refreshed = _refresh_stored_token(time.time(), force=True)
            if refreshed is not None and refreshed != token:
                return _poll_with_token(refreshed)
            _auth_required = True
        retry_after = _float_or_none(exc.headers.get("Retry-After") if exc.headers else None)
        logger.debug("claude usage api returned %s", exc.code)
        return exc.code, None, retry_after
    except (OSError, ValueError):
        logger.debug("claude usage api request failed", exc_info=True)
        return None, None, None


def _poll_with_token(token: str) -> tuple[int | None, ClaudeApiQuota | None, float | None]:
    global _auth_required
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "usage-menubar",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            _auth_required = False
            return response.status, parse_usage(payload, fetched_at=time.time()), None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            _auth_required = True
        retry_after = _float_or_none(exc.headers.get("Retry-After") if exc.headers else None)
        logger.debug("claude usage api returned %s after token refresh", exc.code)
        return exc.code, None, retry_after
    except (OSError, ValueError):
        logger.debug("claude usage api request failed after token refresh", exc_info=True)
        return None, None, None


def _access_token(now: float) -> str | None:
    global _auth_required
    raw = _read_keychain() if sys.platform == "darwin" else None
    if raw is None:
        try:
            raw = CREDENTIALS_FILE.read_text(encoding="utf-8")
        except OSError:
            _auth_required = True
            return None
    try:
        credentials = json.loads(raw)
        oauth = credentials.get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        _auth_required = True
        return None
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        _auth_required = True
        return None
    if (
        isinstance(expires_ms, int | float)
        and expires_ms / 1000 - TOKEN_EXPIRY_MARGIN_SECONDS < now
    ):
        return _refresh_credentials(credentials, now)
    return token


def _refresh_stored_token(now: float, *, force: bool = False) -> str | None:
    raw = _read_keychain() if sys.platform == "darwin" else None
    if raw is None:
        try:
            raw = CREDENTIALS_FILE.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        credentials = json.loads(raw)
        oauth = credentials.get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        return None
    expires_ms = oauth.get("expiresAt")
    token = oauth.get("accessToken")
    if (
        not force
        and isinstance(token, str)
        and token
        and isinstance(expires_ms, int | float)
        and expires_ms / 1000 - TOKEN_EXPIRY_MARGIN_SECONDS >= now
    ):
        return token
    return _refresh_credentials(credentials, now)


def _refresh_credentials(credentials: object, now: float) -> str | None:
    global _auth_required
    if not isinstance(credentials, dict):
        _auth_required = True
        return None
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        _auth_required = True
        return None
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        _auth_required = True
        return None
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _claude_user_agent(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            _auth_required = True
        logger.debug("claude oauth token refresh failed", exc_info=True)
        return None
    except (OSError, ValueError):
        logger.debug("claude oauth token refresh failed", exc_info=True)
        return None
    if not isinstance(refreshed, dict):
        return None
    access_token = refreshed.get("access_token")
    new_refresh_token = refreshed.get("refresh_token")
    expires_in = refreshed.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(new_refresh_token, str)
        or not new_refresh_token
        or not isinstance(expires_in, int | float)
        or isinstance(expires_in, bool)
        or expires_in <= 0
    ):
        _auth_required = True
        logger.debug("claude oauth token refresh returned an invalid payload")
        return None
    updated = dict(credentials)
    updated_oauth = dict(oauth)
    updated_oauth["accessToken"] = access_token
    updated_oauth["refreshToken"] = new_refresh_token
    updated_oauth["expiresAt"] = int((now + float(expires_in)) * 1000)
    refresh_expires_in = refreshed.get("refresh_token_expires_in")
    if (
        isinstance(refresh_expires_in, int | float)
        and not isinstance(refresh_expires_in, bool)
        and refresh_expires_in > 0
    ):
        updated_oauth["refreshTokenExpiresAt"] = int((now + float(refresh_expires_in)) * 1000)
    updated["claudeAiOauth"] = updated_oauth
    serialized = json.dumps(updated, separators=(",", ":"))
    if sys.platform == "darwin":
        if not _write_keychain(serialized):
            return None
    else:
        try:
            CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            CREDENTIALS_FILE.write_text(serialized, encoding="utf-8")
            CREDENTIALS_FILE.chmod(0o600)
        except OSError:
            logger.debug("write refreshed Claude credentials failed", exc_info=True)
            return None
    _auth_required = False
    return access_token


def _claude_user_agent() -> str:
    candidates = [
        shutil.which("claude"),
        str(Path.home() / ".local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    for executable in candidates:
        if not executable:
            continue
        try:
            completed = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"\b\d+\.\d+\.\d+\b", completed.stdout)
        if completed.returncode == 0 and match is not None:
            return f"claude-cli/{match.group(0)}"
    return "claude-cli/usage-menubar"


def _read_keychain() -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _write_keychain(credentials: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-a",
                getpass.getuser(),
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
                credentials,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        logger.debug("write refreshed Claude credentials to Keychain failed")
        return False
    return True


def parse_usage(payload: object, *, fetched_at: float) -> ClaudeApiQuota | None:
    if not isinstance(payload, dict):
        return None
    five_pct, five_reset = _bucket(payload.get("five_hour"))
    seven_pct, seven_reset = _bucket(payload.get("seven_day"))
    if five_pct is None and seven_pct is None:
        return None
    return ClaudeApiQuota(five_pct, five_reset, seven_pct, seven_reset, fetched_at)


def _bucket(value: object) -> tuple[float | None, float | None]:
    if not isinstance(value, dict):
        return None, None
    utilization = value.get("utilization")
    percent = (
        float(utilization)
        if isinstance(utilization, int | float) and not isinstance(utilization, bool)
        else None
    )
    resets_at = None
    raw_reset = value.get("resets_at")
    if isinstance(raw_reset, str) and raw_reset:
        try:
            resets_at = parse_iso8601_utc_or_raise(raw_reset).timestamp()
        except ValueError:
            resets_at = None
    return percent, resets_at


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
