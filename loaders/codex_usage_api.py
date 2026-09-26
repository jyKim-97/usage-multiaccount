# SPDX-License-Identifier: AGPL-3.0-only
"""Codex quota over plain HTTP: the ChatGPT backend's `/wham/usage` (behind `/status`).

A quota metadata read — no model runs, no token is spent — that costs one
in-process request instead of starting `codex app-server` (~100 MB for half a
second). It needs the access token Codex stores in `$CODEX_HOME/auth.json`,
which is read and never refreshed or written back: Codex rotates its own
tokens, and a refresh from here could sign it out. When the token is missing
or expired the caller falls back to the app-server probe, which lets Codex
refresh it itself.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from loaders.codex_paths import codex_home
from loaders.ocx_quota_loader import QuotaWindow

logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
REQUEST_TIMEOUT_SECONDS = 8.0
TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
_DAY_SECONDS = 86400.0
_WEEK_SECONDS = 7 * _DAY_SECONDS


def access_token(now: float) -> tuple[str, str | None] | None:
    """(access token, ChatGPT account id) when a usable token is stored."""
    try:
        data = json.loads((codex_home() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expires_at = _jwt_expiry(token)
    if expires_at is not None and expires_at - TOKEN_EXPIRY_MARGIN_SECONDS < now:
        return None
    account_id = tokens.get("account_id")
    return token, account_id if isinstance(account_id, str) and account_id else None


def fetch(
    now: float,
) -> tuple[int | None, tuple[str | None, tuple[QuotaWindow, ...]] | None, float | None]:
    """(HTTP status, (plan, windows) or None, Retry-After seconds).

    Status None with no result means the token was unusable: fall back.
    """
    credential = access_token(now)
    if credential is None:
        return None, None, None
    token, account_id = credential
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "usage-menubar",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, parse_usage(payload, now=now), None
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        logger.debug("codex usage api returned %s", exc.code)
        return exc.code, None, _float_or_none(retry_after)
    except (OSError, ValueError):
        logger.debug("codex usage api request failed", exc_info=True)
        return 0, None, None


def parse_usage(
    payload: object, *, now: float
) -> tuple[str | None, tuple[QuotaWindow, ...]] | None:
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limit")
    if not isinstance(limits, dict):
        limits = payload
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        percent = _number(window.get("used_percent"))
        if percent is None:
            continue
        seconds = _number(window.get("limit_window_seconds"))
        resets_at = _number(window.get("reset_at"))
        if resets_at is None and (after := _number(window.get("reset_after_seconds"))) is not None:
            resets_at = now + after
        windows.append(QuotaWindow(_kind(seconds, key), percent, resets_at, seconds))
    if not windows:
        return None
    plan = payload.get("plan_type")
    return (plan if isinstance(plan, str) and plan else None), tuple(windows)


def _kind(seconds: float | None, key: str) -> str:
    if seconds is None:
        return "short" if key == "primary_window" else "weekly"
    if seconds < _DAY_SECONDS:
        return "short"
    if seconds <= _WEEK_SECONDS:
        return "weekly"
    return "monthly"


def _jwt_expiry(token: str) -> float | None:
    """The `exp` claim, read without verifying the signature (only to skip stale tokens)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    return _number(claims.get("exp")) if isinstance(claims, dict) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
