from __future__ import annotations

import json
from dataclasses import replace

import pytest

from loaders import ocx_quota_loader
from loaders.ocx_quota_loader import AccountInfo, QuotaWindow
from menubar import codex_accounts
from menubar.state import _error_state
from panels.payload import _state_payload

NOW = 1_790_435_000.0


def _write_cache(quotas: dict[str, object]) -> None:
    path = ocx_quota_loader.quota_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "quotas": quotas}), encoding="utf-8")


PLUS = {
    "updatedAt": int((NOW - 30) * 1000),
    "weeklyPercent": 20,
    "weeklyResetAt": int(NOW + 5 * 86400),
    "shortPercent": 55,
    "shortResetAt": int(NOW + 7000),
    "shortWindowSeconds": 18000,
    "resetCredits": 3,
}
FREE = {
    "updatedAt": int((NOW - 30) * 1000),
    "monthlyPercent": 0,
    "monthlyResetAt": int(NOW + 20 * 86400),
}


def test_missing_cache_returns_nothing() -> None:
    assert ocx_quota_loader.load_account_quotas(now=NOW) == ()


def test_corrupt_cache_returns_nothing() -> None:
    path = ocx_quota_loader.quota_cache_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert ocx_quota_loader.load_account_quotas(now=NOW) == ()


def test_parses_windows_and_falls_back_to_id_labels() -> None:
    _write_cache({"acct-free-1234567890": FREE, "__main__": PLUS})

    main, free = ocx_quota_loader.load_account_quotas(now=NOW)

    assert main.label == "main"
    assert main.updated_at == pytest.approx(NOW - 30)
    assert main.windows == (
        QuotaWindow("short", 55.0, float(PLUS["shortResetAt"]), 18000.0),
        QuotaWindow("weekly", 20.0, float(PLUS["weeklyResetAt"]), 7 * 86400.0),
    )
    assert free.label == "acct-f"
    assert [w.kind for w in free.windows] == ["monthly"]


def test_account_list_supplies_labels_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_cache({"__main__": PLUS, "acct-free": FREE})
    listing = "⚠️  notice line printed before the JSON body\n" + json.dumps(
        {
            "accounts": [
                {"id": "acct-free", "label": "free", "plan": "free", "active": True},
                {"id": "__main__", "label": "plus", "plan": "plus", "active": False},
            ]
        }
    )
    monkeypatch.setattr(
        ocx_quota_loader,
        "_fetch_account_list",
        lambda: ocx_quota_loader.parse_account_list(listing),
    )

    accounts = ocx_quota_loader.load_account_quotas(now=NOW)

    assert [(a.label, a.plan, a.active) for a in accounts] == [
        ("free", "free", True),
        ("plus", "plus", False),
    ]


def test_labels_are_cached_and_survive_a_failed_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_cache({"__main__": PLUS})
    calls: list[int] = []
    answers = [({"__main__": AccountInfo("plus", "plus", True)}, ("__main__",)), None]

    def fetch() -> object:
        calls.append(1)
        return answers[len(calls) - 1]

    monkeypatch.setattr(ocx_quota_loader, "_fetch_account_list", fetch)

    assert ocx_quota_loader.load_account_quotas(now=NOW)[0].label == "plus"
    assert ocx_quota_loader.load_account_quotas(now=NOW + 10)[0].label == "plus"
    assert len(calls) == 1
    later = NOW + ocx_quota_loader.LABEL_TTL_SECONDS + 1
    assert ocx_quota_loader.load_account_quotas(now=later)[0].label == "plus"
    assert len(calls) == 2


def test_account_states_blank_a_window_whose_reset_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    rolled_over = {**PLUS, "shortResetAt": int(NOW - 60)}
    _write_cache({"__main__": rolled_over})
    monkeypatch.setattr("menubar.codex_accounts.time.time", lambda: NOW)

    (state,) = codex_accounts.codex_account_states(mock=False, language="en")

    short, weekly = state.rows
    assert short.available is False
    assert short.percent_text == "--"
    assert weekly.percent == 20.0
    assert state.stale is None


def test_payload_carries_accounts() -> None:
    state = _error_state("stub", mock=True, language="en")
    state.codex_accounts = codex_accounts.codex_account_states(mock=True, language="en")

    payload = _state_payload(state)

    rendered = payload["codexAccounts"]
    assert isinstance(rendered, list)
    assert [a["label"] for a in rendered] == ["plus", "free"]
    assert [r["title"] for r in rendered[0]["rows"]] == ["Session", "Weekly"]
    assert [r["title"] for r in rendered[1]["rows"]] == ["Monthly"]
    assert rendered[0]["email"] == "j***g@example.com"


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("j***g@example.com", "j***g@example.com"),
        ("jungyoung@example.com", "j***g@example.com"),
        ("ab@example.com", "a***@example.com"),
        ("no-at-sign", "no-at-sign"),
    ],
)
def test_masked_email(email: str, expected: str) -> None:
    assert ocx_quota_loader.masked_email(email) == expected


def test_account_list_carries_masked_email() -> None:
    parsed = ocx_quota_loader.parse_account_list(
        json.dumps({"accounts": [{"id": "a", "label": "plus", "email": "jungyoung@example.com"}]})
    )
    assert parsed is not None
    assert parsed[0]["a"].email == "j***g@example.com"


def test_active_account_percent_prefers_active_shortest_window() -> None:
    plus, free = codex_accounts.codex_account_states(mock=True, language="en")
    assert codex_accounts.active_account_percent((plus, free)) == 55.0  # plus is active in mock
    inactive = tuple(replace(account, active=False) for account in (plus, free))
    assert codex_accounts.active_account_percent(inactive) is None
    free_active = (replace(plus, active=False), replace(free, active=True))
    assert codex_accounts.active_account_percent(free_active) == 12.0  # monthly-only plan


def test_fsevents_keeps_only_the_quota_cache_from_ocx() -> None:
    import fsevents_watch

    cache = str(ocx_quota_loader.quota_cache_path())
    other = str(ocx_quota_loader.OCX_HOME / "usage.jsonl")
    codex = "/tmp/codex/sessions/a.jsonl"
    paths, flags = fsevents_watch.drop_unrelated_ocx_events([cache, other, codex], [1, 2, 3])
    assert paths == [cache, codex]
    assert flags == [1, 3]
