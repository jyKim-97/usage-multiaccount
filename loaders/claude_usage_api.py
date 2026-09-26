# SPDX-License-Identifier: AGPL-3.0-only
"""Claude quota from Anthropic's OAuth usage endpoint (the call behind `/usage`).

The statusLine hook only records quota when Claude Code runs on this machine,
so work on a remote server never reaches it. This endpoint reports the same
account-wide 5-hour / weekly utilization from anywhere. It is a metadata read:
no model runs and no token is spent.

The access token is read from Claude Code's own credential store (macOS
Keychain item "Claude Code-credentials", else ~/.claude/.credentials.json) and
used as-is. This module never refreshes or writes it back: rotating the
refresh token here would sign Claude Code out. An expired token just means no
API snapshot until Claude Code refreshes it.

Anthropic rate-limits the endpoint aggressively, so calls are spaced by
POLL_INTERVAL_SECONDS, skipped while the local hook file is fresh, and a 429
waits out Retry-After (at least doubling the interval, capped at an hour).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from usage_common.time_utils import parse_iso8601_utc_or_raise

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path(os.path.expanduser("~/.claude/.credentials.json"))
POLL_INTERVAL_SECONDS = 600.0
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


def latest_quota(
    *, local_polled_at: float | None, now: float | None = None
) -> ClaudeApiQuota | None:
    """Cached API snapshot, polling first when due and the local hook is stale."""
    global _cached, _next_poll_at, _backoff
    current = time.time() if now is None else now
    local_fresh = local_polled_at is not None and current - local_polled_at < POLL_INTERVAL_SECONDS
    with _lock:
        if not local_fresh and current >= _next_poll_at:
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
            return response.status, parse_usage(payload, fetched_at=time.time()), None
    except urllib.error.HTTPError as exc:
        retry_after = _float_or_none(exc.headers.get("Retry-After") if exc.headers else None)
        logger.debug("claude usage api returned %s", exc.code)
        return exc.code, None, retry_after
    except (OSError, ValueError):
        logger.debug("claude usage api request failed", exc_info=True)
        return None, None, None


def _access_token(now: float) -> str | None:
    raw = _read_keychain() if sys.platform == "darwin" else None
    if raw is None:
        try:
            raw = CREDENTIALS_FILE.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return None
    if (
        isinstance(expires_ms, int | float)
        and expires_ms / 1000 - TOKEN_EXPIRY_MARGIN_SECONDS < now
    ):
        return None
    return token


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
