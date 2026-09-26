# SPDX-License-Identifier: AGPL-3.0-only
"""Per-account Codex quota from OpenCodex's local quota cache.

OpenCodex (`ocx`) pools several Codex logins and keeps one quota snapshot per
account in ``~/.opencodex/codex-quota-cache.json``. Reading that file is the
whole data path: this module never asks ocx to refresh upstream, and never opens
``codex-accounts.json``, which holds the OAuth tokens.

Display labels come from ``ocx account list openai --json``, whose output is
masked. It only answers while the ocx proxy runs, so the last good answer is
cached and a failure falls back to ids ("main", or a short id prefix).
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
from pathlib import Path

logger = logging.getLogger(__name__)

OCX_HOME = Path(os.path.expanduser("~/.opencodex"))
QUOTA_CACHE_FILENAME = "codex-quota-cache.json"
MAIN_ACCOUNT_ID = "__main__"
MAIN_ACCOUNT_FALLBACK_LABEL = "main"
LABEL_TTL_SECONDS = 300.0
LABEL_TIMEOUT_SECONDS = 3.0
# A .app launched from Finder gets a minimal PATH without Homebrew.
OCX_FALLBACK_PATHS = (
    "/opt/homebrew/bin/ocx",
    "/usr/local/bin/ocx",
    os.path.expanduser("~/.local/bin/ocx"),
)
# (cache key prefix, window kind); the short window carries its own length.
_WINDOW_KINDS = (("short", "short"), ("weekly", "weekly"), ("monthly", "monthly"))
_WEEK_SECONDS = 7 * 86400.0
_MONTH_SECONDS = 30 * 86400.0


@dataclass(frozen=True, slots=True)
class AccountInfo:
    label: str
    plan: str | None
    active: bool
    email: str | None = None  # masked, e.g. "j***g@example.com"


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    kind: str  # "short" | "weekly" | "monthly"
    percent: float | None
    resets_at: float | None  # epoch seconds
    window_seconds: float | None


@dataclass(frozen=True, slots=True)
class AccountQuota:
    account_id: str
    label: str
    plan: str | None
    active: bool
    updated_at: float | None  # epoch seconds
    email: str | None
    windows: tuple[QuotaWindow, ...]


_label_lock = threading.Lock()
_label_cache: dict[str, AccountInfo] = {}
_label_order: tuple[str, ...] = ()
_label_checked_at: float | None = None


def quota_cache_path() -> Path:
    return OCX_HOME / QUOTA_CACHE_FILENAME


def load_account_quotas(*, now: float | None = None) -> tuple[AccountQuota, ...]:
    """Return one entry per account in the ocx quota cache, or () without ocx."""
    quotas = _read_quota_cache(quota_cache_path())
    if not quotas:
        return ()
    infos, order = _account_infos(time.time() if now is None else now)
    ranked = [account_id for account_id in order if account_id in quotas]
    ranked += sorted(
        (account_id for account_id in quotas if account_id not in ranked),
        key=lambda account_id: (account_id != MAIN_ACCOUNT_ID, account_id),
    )
    result = []
    for account_id in ranked:
        raw = quotas[account_id]
        info = infos.get(account_id)
        result.append(
            AccountQuota(
                account_id=account_id,
                label=info.label if info else _fallback_label(account_id),
                plan=info.plan if info else None,
                active=info.active if info else False,
                updated_at=_epoch_seconds(raw.get("updatedAt")),
                email=info.email if info else None,
                windows=_windows(raw),
            )
        )
    return tuple(result)


def _read_quota_cache(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.debug("unreadable ocx quota cache: %s", path, exc_info=True)
        return {}
    quotas = data.get("quotas") if isinstance(data, dict) else None
    if not isinstance(quotas, dict):
        return {}
    return {
        str(account_id): entry for account_id, entry in quotas.items() if isinstance(entry, dict)
    }


def _windows(raw: dict[str, object]) -> tuple[QuotaWindow, ...]:
    windows = []
    for prefix, kind in _WINDOW_KINDS:
        percent = _number(raw.get(f"{prefix}Percent"))
        resets_at = _epoch_seconds(raw.get(f"{prefix}ResetAt"))
        if percent is None and resets_at is None:
            continue
        if kind == "short":
            window_seconds = _number(raw.get("shortWindowSeconds"))
        else:
            window_seconds = _WEEK_SECONDS if kind == "weekly" else _MONTH_SECONDS
        windows.append(QuotaWindow(kind, percent, resets_at, window_seconds))
    return tuple(windows)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _epoch_seconds(value: object) -> float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    # ocx mixes units: updatedAt is milliseconds, *ResetAt is seconds.
    return number / 1000.0 if number > 1e11 else number


def _fallback_label(account_id: str) -> str:
    if account_id == MAIN_ACCOUNT_ID:
        return MAIN_ACCOUNT_FALLBACK_LABEL
    return account_id[:6]


def _account_infos(now: float) -> tuple[dict[str, AccountInfo], tuple[str, ...]]:
    global _label_cache, _label_order, _label_checked_at
    with _label_lock:
        if _label_checked_at is None or now - _label_checked_at >= LABEL_TTL_SECONDS:
            _label_checked_at = now
            fetched = _fetch_account_list()
            if fetched is not None:
                _label_cache, _label_order = fetched
        return dict(_label_cache), _label_order


def _ocx_executable() -> str | None:
    found = shutil.which("ocx")
    if found:
        return found
    return next((path for path in OCX_FALLBACK_PATHS if os.access(path, os.X_OK)), None)


def _fetch_account_list() -> tuple[dict[str, AccountInfo], tuple[str, ...]] | None:
    executable = _ocx_executable()
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "account", "list", "openai", "--json"],
            capture_output=True,
            text=True,
            timeout=LABEL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ocx account list failed", exc_info=True)
        return None
    if completed.returncode != 0:
        return None
    return parse_account_list(completed.stdout)


def parse_account_list(output: str) -> tuple[dict[str, AccountInfo], tuple[str, ...]] | None:
    # ocx may print a notice line (e.g. a shim repair) before the JSON body.
    start = output.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(output[start:])
    except ValueError:
        return None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return None
    infos: dict[str, AccountInfo] = {}
    order: list[str] = []
    for account in accounts:
        if not isinstance(account, dict) or not isinstance(account.get("id"), str):
            continue
        account_id = account["id"]
        label = account.get("label")
        plan = account.get("plan")
        email = account.get("email")
        infos[account_id] = AccountInfo(
            label=label if isinstance(label, str) and label else _fallback_label(account_id),
            plan=plan if isinstance(plan, str) and plan else None,
            active=account.get("active") is True,
            email=masked_email(email) if isinstance(email, str) and email else None,
        )
        order.append(account_id)
    return infos, tuple(order)


def masked_email(email: str) -> str:
    """Keep ocx's own masking; mask the local part ourselves if it arrives bare."""
    local, at, domain = email.partition("@")
    if not at or "*" in local:
        return email
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"
