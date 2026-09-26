# SPDX-License-Identifier: AGPL-3.0-only
"""Live Codex quota for Codex's own login, via `codex app-server`.

`account/rateLimits/read` is the app-server call behind Codex's `/status`:
a quota metadata read that runs no model and spends no token. Codex does its
own sign-in and token refresh, so this module never touches a credential. It
covers only the account Codex itself is logged in to (OpenCodex's `__main__`);
accounts that exist only in the OpenCodex pool still come from its cache.

`live_quota` first tries the same data over plain HTTP (`codex_usage_api`),
which avoids starting the app server; the probe below is the fallback for a
missing or expired stored token, and starting it lets Codex refresh that token.
Calls are throttled to PROBE_INTERVAL_SECONDS; a 429 waits out Retry-After, and
a failure backs off so a broken install costs one attempt per interval at most.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from loaders import codex_usage_api
from loaders.ocx_quota_loader import QuotaWindow

logger = logging.getLogger(__name__)

PROBE_INTERVAL_SECONDS = 60.0
FAILURE_BACKOFF_SECONDS = 300.0
MAX_BACKOFF_SECONDS = 3600.0
PROBE_TIMEOUT_SECONDS = 15.0
# The ocx shim passes `app-server` straight through, but prefer the real binary
# when OpenCodex has installed one. A Finder-launched .app has a minimal PATH.
CODEX_CANDIDATES = (
    os.path.expanduser("~/.local/bin/codex.opencodex-real"),
    os.path.expanduser("~/.local/bin/codex"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)
_WEEK_MINUTES = 7 * 24 * 60


@dataclass(frozen=True, slots=True)
class CodexLiveQuota:
    plan: str | None
    fetched_at: float  # epoch seconds
    windows: tuple[QuotaWindow, ...]


_lock = threading.Lock()
_cached: CodexLiveQuota | None = None
_next_probe_at = 0.0
_backoff = PROBE_INTERVAL_SECONDS


def live_quota(*, now: float | None = None) -> CodexLiveQuota | None:
    """Latest probe result, probing first when the interval has elapsed."""
    global _cached, _next_probe_at, _backoff
    current = time.time() if now is None else now
    with _lock:
        if current < _next_probe_at:
            return _cached
        status, http_result, retry_after = codex_usage_api.fetch(current)
        if status == 429:
            # Same backend as the app server: don't retry through it either.
            _backoff = min(max(retry_after or 0.0, _backoff * 2), MAX_BACKOFF_SECONDS)
            _next_probe_at = current + _backoff
            return _cached
        if http_result is not None:
            plan, windows = http_result
            result: CodexLiveQuota | None = CodexLiveQuota(plan, time.time(), windows)
        else:
            result = _probe()
        if result is not None:
            _cached = result
            _backoff = PROBE_INTERVAL_SECONDS
            _next_probe_at = current + PROBE_INTERVAL_SECONDS
        else:
            _next_probe_at = current + FAILURE_BACKOFF_SECONDS
        return _cached


def _codex_executable() -> str | None:
    for candidate in CODEX_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("codex")


def _probe() -> CodexLiveQuota | None:
    executable = _codex_executable()
    if executable is None:
        return None
    requests = "".join(
        json.dumps(message) + "\n"
        for message in (
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "usage", "version": "0"}},
            },
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        )
    )
    try:
        process = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        logger.debug("codex app-server failed to start", exc_info=True)
        return None
    timer = threading.Timer(PROBE_TIMEOUT_SECONDS, process.kill)
    timer.start()
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(requests)
        process.stdin.flush()
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                return parse_rate_limits(message.get("result"), fetched_at=time.time())
        return None
    except (OSError, ValueError):
        logger.debug("codex app-server probe failed", exc_info=True)
        return None
    finally:
        timer.cancel()
        process.kill()
        process.wait()


def parse_rate_limits(result: object, *, fetched_at: float) -> CodexLiveQuota | None:
    if not isinstance(result, dict):
        return None
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        return None
    windows = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        percent = _number(window.get("usedPercent"))
        minutes = _number(window.get("windowDurationMins"))
        resets_at = _number(window.get("resetsAt"))
        if percent is None:
            continue
        windows.append(
            QuotaWindow(
                kind=_window_kind(minutes, key),
                percent=percent,
                resets_at=resets_at,
                window_seconds=None if minutes is None else minutes * 60,
            )
        )
    if not windows:
        return None
    plan = limits.get("planType")
    return CodexLiveQuota(
        plan=plan if isinstance(plan, str) and plan != "unknown" else None,
        fetched_at=fetched_at,
        windows=tuple(windows),
    )


def _window_kind(minutes: float | None, slot: str) -> str:
    if minutes is None:
        return "short" if slot == "primary" else "weekly"
    if minutes < 24 * 60:
        return "short"
    if minutes <= _WEEK_MINUTES:
        return "weekly"
    return "monthly"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
