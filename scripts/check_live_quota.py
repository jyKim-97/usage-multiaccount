#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Check the fork's live quota sources once, printing results but never a token.

Run from the repo root:  uv run python scripts/check_live_quota.py

Each source is called exactly once, bypassing the app's throttles, so running
this repeatedly still counts against the providers' request limits.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders import (  # noqa: E402
    claude_usage_api,
    codex_app_server_probe,
    codex_usage_api,
    ocx_quota_loader,
)


def _age(epoch: float | None) -> str:
    return "--" if epoch is None else f"{(time.time() - epoch) / 60:.0f} min ago"


def check_claude() -> None:
    print("[Claude] Anthropic OAuth usage endpoint")
    token = claude_usage_api._access_token(time.time())
    if token is None:
        print("  token: missing or expired (the app skips the API and uses the hook file)")
        return
    print(f"  token: present ({len(token)} chars, not shown)")
    status, quota, retry_after = claude_usage_api._poll()
    print(f"  HTTP status: {status}" + (f", Retry-After {retry_after:.0f}s" if retry_after else ""))
    if quota is None:
        print("  result: no quota parsed")
        return
    print(f"  5-hour: {quota.five_hour_percent}%  weekly: {quota.seven_day_percent}%")


def check_codex_http() -> None:
    print("[Codex] ChatGPT backend /wham/usage over HTTP")
    credential = codex_usage_api.access_token(time.time())
    if credential is None:
        print("  token: missing or expired (the app falls back to codex app-server)")
        return
    token, account_id = credential
    print(f"  token: present ({len(token)} chars, not shown), account id: {bool(account_id)}")
    started = time.time()
    status, result, retry_after = codex_usage_api.fetch(time.time())
    print(f"  HTTP status: {status}" + (f", Retry-After {retry_after:.0f}s" if retry_after else ""))
    if result is None:
        print("  result: no quota parsed")
        return
    plan, windows = result
    shown = ", ".join(f"{w.kind} {w.percent:.0f}%" for w in windows)
    print(f"  plan: {plan}  {shown}  ({time.time() - started:.1f}s)")


def check_codex() -> None:
    print("[Codex] codex app-server account/rateLimits/read (fallback)")
    started = time.time()
    live = codex_app_server_probe._probe()
    if live is None:
        print("  result: failed (codex missing, not signed in, or protocol changed)")
        return
    windows = ", ".join(f"{w.kind} {w.percent:.0f}%" for w in live.windows)
    print(f"  plan: {live.plan}  {windows}  ({time.time() - started:.1f}s)")


def check_ocx() -> None:
    print("[OpenCodex] quota cache")
    accounts = ocx_quota_loader.load_account_quotas()
    if not accounts:
        print("  no cache (OpenCodex not installed or no quota fetched yet)")
        return
    for account in accounts:
        windows = ", ".join(
            f"{w.kind} {w.percent:.0f}%" for w in account.windows if w.percent is not None
        )
        marker = "●" if account.active else " "
        print(f"  {marker} {account.label:<8} {windows}  (updated {_age(account.updated_at)})")


if __name__ == "__main__":
    for check in (check_claude, check_codex_http, check_codex, check_ocx):
        try:
            check()
        except Exception as exc:  # a diagnostic should report, not crash
            print(f"  error: {type(exc).__name__}: {exc}")
        print()
