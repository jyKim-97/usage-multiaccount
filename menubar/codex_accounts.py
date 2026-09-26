# SPDX-License-Identifier: AGPL-3.0-only
"""Project OpenCodex per-account Codex quota into popover rows.

Kept out of ``menubar/state.py`` so the feature stays a leaf: without ocx the
tuple is empty and every panel renders exactly as before.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from i18n import _t
from loaders import ocx_quota_loader
from loaders.ocx_quota_loader import AccountQuota, QuotaWindow
from menubar.state import (
    CODEX_COLOR,
    CodexStaleState,
    QuotaRowState,
    _missing_row,
    _quota_row,
    codex_stale_state,
)

logger = logging.getLogger(__name__)

_WINDOW_LABEL_KEYS = {
    "short": "session_label",
    "weekly": "weekly_label",
    "monthly": "monthly_label",
}


@dataclass(frozen=True, slots=True)
class CodexAccountState:
    label: str
    plan: str | None
    active: bool
    rows: tuple[QuotaRowState, ...]
    email: str | None = None
    stale: CodexStaleState | None = None


def codex_account_states(*, mock: bool, language: str) -> tuple[CodexAccountState, ...]:
    now = time.time()
    try:
        accounts = _mock_accounts(now) if mock else ocx_quota_loader.load_account_quotas(now=now)
    except Exception:
        logger.debug("ocx account quota load failed", exc_info=True)
        return ()
    return tuple(_account_state(account, now, language) for account in accounts)


def active_account_percent(accounts: tuple[CodexAccountState, ...]) -> float | None:
    """The active account's shortest window (5-hour, else weekly, else monthly)."""
    for account in accounts:
        if not account.active:
            continue
        return next((row.percent for row in account.rows if row.percent is not None), None)
    return None


def _account_state(account: AccountQuota, now: float, language: str) -> CodexAccountState:
    stale = None
    if account.updated_at is not None:
        updated_iso = datetime.fromtimestamp(account.updated_at, UTC).isoformat()
        stale = codex_stale_state(updated_iso, now, language)
    return CodexAccountState(
        label=account.label,
        plan=account.plan,
        active=account.active,
        rows=tuple(_window_row(window, now, language) for window in account.windows),
        email=account.email,
        stale=stale,
    )


def _window_row(window: QuotaWindow, now: float, language: str) -> QuotaRowState:
    title = _t(language, _WINDOW_LABEL_KEYS[window.kind])
    # A window whose reset already passed has rolled over; its percent is obsolete.
    if window.resets_at is not None and window.resets_at <= now:
        return _missing_row(title, CODEX_COLOR, language)
    return _quota_row(
        title,
        window.percent,
        window.resets_at,
        now,
        CODEX_COLOR,
        language,
        window_seconds=None if window.kind == "short" else window.window_seconds,
    )


def _mock_accounts(now: float) -> tuple[AccountQuota, ...]:
    return (
        AccountQuota(
            account_id=ocx_quota_loader.MAIN_ACCOUNT_ID,
            label="plus",
            plan="plus",
            active=True,
            updated_at=now - 60,
            email="j***g@example.com",
            windows=(
                QuotaWindow("short", 55.0, now + 2 * 3600, 18000.0),
                QuotaWindow("weekly", 20.0, now + 5 * 86400, 7 * 86400.0),
            ),
        ),
        AccountQuota(
            account_id="mock-free",
            label="free",
            plan="free",
            active=False,
            updated_at=now - 60,
            email="k***m@example.ac.kr",
            windows=(QuotaWindow("monthly", 12.0, now + 20 * 86400, 30 * 86400.0),),
        ),
    )
