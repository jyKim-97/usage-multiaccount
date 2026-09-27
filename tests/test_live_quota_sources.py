from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import usage_client
from loaders import claude_usage_api, codex_app_server_probe, ocx_quota_loader
from loaders.codex_app_server_probe import CodexLiveQuota
from loaders.ocx_quota_loader import AccountQuota, QuotaWindow
from menubar import codex_accounts

NOW = 1_790_435_000.0


# --- codex app-server -------------------------------------------------------


def test_parse_rate_limits_maps_windows_by_duration() -> None:
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 57, "windowDurationMins": 300, "resetsAt": NOW + 60},
            "secondary": {"usedPercent": 21, "windowDurationMins": 10080, "resetsAt": NOW + 99},
            "planType": "plus",
        }
    }
    live = codex_app_server_probe.parse_rate_limits(result, fetched_at=NOW)
    assert live is not None
    assert live.plan == "plus"
    assert [(w.kind, w.percent, w.window_seconds) for w in live.windows] == [
        ("short", 57.0, 18000.0),
        ("weekly", 21.0, 604800.0),
    ]


def test_parse_rate_limits_rejects_missing_data() -> None:
    assert codex_app_server_probe.parse_rate_limits({"rateLimits": {}}, fetched_at=NOW) is None
    assert codex_app_server_probe.parse_rate_limits(None, fetched_at=NOW) is None


def test_failed_probe_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(codex_app_server_probe, "_probe", lambda: calls.append(1))
    codex_app_server_probe.live_quota(now=NOW)
    codex_app_server_probe.live_quota(now=NOW + codex_app_server_probe.PROBE_INTERVAL_SECONDS)
    assert len(calls) == 1  # still inside FAILURE_BACKOFF_SECONDS


LIVE = CodexLiveQuota(
    plan="plus",
    fetched_at=NOW,
    windows=(QuotaWindow("short", 57.0, NOW + 3600, 18000.0),),
)


def _account(account_id: str, label: str, updated_at: float | None) -> AccountQuota:
    return AccountQuota(
        account_id=account_id,
        label=label,
        plan=None,
        active=False,
        updated_at=updated_at,
        email=None,
        windows=(QuotaWindow("short", 10.0, NOW + 3600, 18000.0),),
    )


def test_live_quota_stands_in_when_ocx_is_absent() -> None:
    (only,) = codex_accounts.merge_live_quota((), LIVE)
    assert (only.label, only.active, only.windows) == ("plus", True, LIVE.windows)


def test_newer_live_quota_overrides_the_main_account_only() -> None:
    main = _account("__main__", "main", NOW - 600)
    other = _account("pooled", "free", NOW - 600)
    merged_main, merged_other = codex_accounts.merge_live_quota((main, other), LIVE)
    assert merged_main.windows == LIVE.windows
    assert merged_main.label == "plus"  # fallback label upgraded from the plan
    assert merged_other == other


def test_older_live_quota_leaves_the_cache_alone() -> None:
    main = _account("__main__", "work", NOW + 5)
    assert codex_accounts.merge_live_quota((main,), LIVE) == (main,)


# --- ocx config fallback ----------------------------------------------------


def test_read_config_accounts(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "activeCodexAccountId": "chatgpt-1",
                "codexAccounts": [
                    {"id": "chatgpt-1", "email": "someone@example.com", "plan": "free"}
                ],
            }
        ),
        encoding="utf-8",
    )
    parsed = ocx_quota_loader.read_config_accounts(path)
    assert parsed is not None
    infos, order = parsed
    assert order == ("__main__", "chatgpt-1")
    assert infos["__main__"].active is False
    assert infos["chatgpt-1"] == ocx_quota_loader.AccountInfo(
        "free", "free", True, "s***e@example.com"
    )


def test_config_fallback_used_when_proxy_is_down() -> None:
    ocx_quota_loader.OCX_HOME.mkdir(parents=True)
    (ocx_quota_loader.OCX_HOME / "config.json").write_text(
        json.dumps({"codexAccounts": [{"id": "chatgpt-1", "plan": "free"}]}), encoding="utf-8"
    )
    ocx_quota_loader.quota_cache_path().write_text(
        json.dumps({"quotas": {"chatgpt-1": {"monthlyPercent": 3, "monthlyResetAt": NOW + 9}}}),
        encoding="utf-8",
    )
    (account,) = ocx_quota_loader.load_account_quotas(now=NOW)
    assert account.label == "free"


# --- claude usage api -------------------------------------------------------


def test_parse_usage() -> None:
    quota = claude_usage_api.parse_usage(
        {
            "five_hour": {"utilization": 42.0, "resets_at": "2026-09-27T05:00:00Z"},
            "seven_day": {"utilization": 7, "resets_at": None},
        },
        fetched_at=NOW,
    )
    assert quota is not None
    assert quota.five_hour_percent == 42.0
    assert quota.five_hour_resets_at is not None
    assert (quota.seven_day_percent, quota.seven_day_resets_at) == (7.0, None)
    assert claude_usage_api.parse_usage({}, fetched_at=NOW) is None


def test_expired_token_is_never_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    creds = tmp_path / "creds.json"
    monkeypatch.setattr(claude_usage_api, "CREDENTIALS_FILE", creds)
    monkeypatch.setattr(claude_usage_api, "_read_keychain", lambda: None)
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": (NOW - 10) * 1000}}),
        encoding="utf-8",
    )
    assert claude_usage_api._access_token(NOW) is None
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": (NOW + 3600) * 1000}}),
        encoding="utf-8",
    )
    assert claude_usage_api._access_token(NOW) == "t"


def test_poll_runs_every_minute_even_when_local_hook_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def poll() -> tuple[None, None, None]:
        calls.append(1)
        return None, None, None

    monkeypatch.setattr(claude_usage_api, "_poll", poll)
    claude_usage_api.latest_quota(now=NOW)
    assert calls == [1]
    claude_usage_api.latest_quota(now=NOW + 59)
    assert calls == [1]
    claude_usage_api.latest_quota(now=NOW + 60)
    assert calls == [1, 1]


def test_429_waits_out_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_usage_api, "_poll", lambda: (429, None, 1800.0))
    claude_usage_api.latest_quota(now=NOW)
    assert claude_usage_api._next_poll_at == NOW + 1800.0


def test_newer_api_snapshot_replaces_missing_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    quota = claude_usage_api.ClaudeApiQuota(42.0, now + 3600, 7.0, now + 86400, now)
    monkeypatch.setattr(claude_usage_api, "latest_quota", lambda **_: quota)
    client = usage_client.ClaudeUsageClient(interval_seconds=60, mock=False)
    missing = usage_client.PollOutcome(state=usage_client.PollState.TOKEN_ERROR)
    monkeypatch.setattr(client, "_fetch_local", lambda: asyncio.sleep(0, result=missing))
    outcome = asyncio.run(client.fetch_once())
    assert outcome.snapshot is not None
    assert outcome.snapshot.current_percent == 42
    assert outcome.snapshot.data_source == usage_client.API_DATA_SOURCE


def test_account_wide_api_snapshot_takes_priority_over_newer_local_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    quota = claude_usage_api.ClaudeApiQuota(
        42.0,
        now + 3600,
        7.0,
        now + 86400,
        now - 10,
    )
    local = usage_client.UsageSnapshot(
        current_percent=5,
        current_reset_at=now + 3600,
        weekly_percent=1,
        weekly_reset_at=now + 86400,
        current_status="",
        polled_at=now,
        data_source="hook",
    )
    monkeypatch.setattr(claude_usage_api, "latest_quota", lambda: quota)
    client = usage_client.ClaudeUsageClient(interval_seconds=60, mock=False)
    local_outcome = usage_client.PollOutcome(
        state=usage_client.PollState.SUCCESS,
        snapshot=local,
    )
    monkeypatch.setattr(client, "_fetch_local", lambda: asyncio.sleep(0, result=local_outcome))

    outcome = asyncio.run(client.fetch_once())

    assert outcome.snapshot is not None
    assert outcome.snapshot.current_percent == 42
    assert outcome.snapshot.data_source == usage_client.API_DATA_SOURCE


# --- codex http -------------------------------------------------------------


def _jwt(exp: float) -> str:
    import base64

    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_codex_usage_parse() -> None:
    from loaders import codex_usage_api

    parsed = codex_usage_api.parse_usage(
        {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 58,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 120,
                },
                "secondary_window": {
                    "used_percent": 21,
                    "limit_window_seconds": 604800,
                    "reset_at": NOW + 5,
                },
            },
        },
        now=NOW,
    )
    assert parsed == (
        "plus",
        (
            QuotaWindow("short", 58.0, NOW + 120, 18000.0),
            QuotaWindow("weekly", 21.0, NOW + 5, 604800.0),
        ),
    )


def test_codex_token_expiry_is_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from loaders import codex_usage_api

    monkeypatch.setattr(codex_usage_api, "codex_home", lambda: tmp_path)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": _jwt(NOW - 5)}}), encoding="utf-8")
    assert codex_usage_api.access_token(NOW) is None
    fresh = {"tokens": {"access_token": _jwt(NOW + 3600), "account_id": "acct"}}
    auth.write_text(json.dumps(fresh), encoding="utf-8")
    assert codex_usage_api.access_token(NOW) == (_jwt(NOW + 3600), "acct")


def test_http_first_then_app_server_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from loaders import codex_usage_api

    probes: list[int] = []

    def probe() -> CodexLiveQuota:
        probes.append(1)
        return LIVE

    monkeypatch.setattr(codex_app_server_probe, "_probe", probe)
    windows = (QuotaWindow("short", 5.0, NOW + 60, 18000.0),)
    monkeypatch.setattr(codex_usage_api, "fetch", lambda now: (200, ("plus", windows), None))
    first = codex_app_server_probe.live_quota(now=NOW)
    assert first is not None
    assert first.windows == windows
    assert probes == []

    monkeypatch.setattr(codex_usage_api, "fetch", lambda now: (None, None, None))
    later = NOW + codex_app_server_probe.PROBE_INTERVAL_SECONDS
    assert codex_app_server_probe.live_quota(now=later) == LIVE
    assert probes == [1]


def test_http_429_skips_the_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from loaders import codex_usage_api

    probes: list[int] = []
    monkeypatch.setattr(codex_app_server_probe, "_probe", lambda: probes.append(1))
    monkeypatch.setattr(codex_usage_api, "fetch", lambda now: (429, None, 900.0))
    codex_app_server_probe.live_quota(now=NOW)
    assert probes == []
    assert codex_app_server_probe._next_probe_at == NOW + 900.0
