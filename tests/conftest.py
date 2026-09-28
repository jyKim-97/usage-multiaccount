from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import ResumeHookPaths, SetupHookPaths, TerseHookPaths
from tests.helpers import patch_resume_hook_paths as _patch_resume_hook_paths
from tests.helpers import patch_setup_hook_paths as _patch_setup_hook_paths
from tests.helpers import patch_terse_hook_paths as _patch_terse_hook_paths

# These modules import PyObjC-backed code (menubar, login_item, panels.web_panel)
# at module level, so they can only be collected on macOS.
collect_ignore = (
    []
    if sys.platform == "darwin"
    else [
        "test_analyzer_pipeline.py",
        "test_login_item.py",
        "test_menubar.py",
        "test_panels.py",
        "test_web_panel_payload.py",
    ]
)


@pytest.fixture(autouse=True)
def _isolate_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test out of the real ~/Library/Logs/usage directory.

    ``main.main()`` calls ``_setup_logging()``, so any test that exercises it
    attaches a RotatingFileHandler to the root logger. Without this the handler
    points at the user's real log file and every later test writes into it.
    """
    import usage_common.usage_logging as usage_logging

    monkeypatch.setattr(usage_logging, "LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _isolate_claude_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep dynamic Claude Code paths out of the user's real config directory."""
    from loaders import claude_paths

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    claude_paths.cache_clear()
    yield
    claude_paths.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_ocx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep refreshes off the real ~/.opencodex, the ocx / codex CLIs and the network."""
    from loaders import ocx_quota_loader

    monkeypatch.setattr(ocx_quota_loader, "OCX_HOME", tmp_path / "opencodex")
    monkeypatch.setattr(ocx_quota_loader, "_fetch_account_list", lambda: None)
    monkeypatch.setattr(ocx_quota_loader, "_label_cache", {})
    monkeypatch.setattr(ocx_quota_loader, "_label_order", ())
    monkeypatch.setattr(ocx_quota_loader, "_label_checked_at", None)

    from loaders import codex_app_server_probe

    monkeypatch.setattr(codex_app_server_probe, "_probe", lambda: None)
    monkeypatch.setattr(codex_app_server_probe, "_backoff", 60.0)
    from loaders import codex_usage_api

    monkeypatch.setattr(codex_usage_api, "fetch", lambda now: (None, None, None))
    monkeypatch.setattr(codex_app_server_probe, "_cached", None)
    monkeypatch.setattr(codex_app_server_probe, "_next_probe_at", 0.0)

    from loaders import claude_usage_api

    monkeypatch.setattr(claude_usage_api, "_poll", lambda: (None, None, None))
    monkeypatch.setattr(claude_usage_api, "_cached", None)
    monkeypatch.setattr(claude_usage_api, "_next_poll_at", 0.0)
    monkeypatch.setattr(claude_usage_api, "_auth_required", False)


@pytest.fixture(autouse=True)
def _isolate_muse_sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep detectors and history scans out of real Muse journals."""
    from loaders import muse_loader

    monkeypatch.setattr(muse_loader, "MUSE_SESSIONS_DIR", tmp_path / "muse-sessions")


@pytest.fixture(autouse=True)
def _isolate_user_state_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep shared startup workers out of the user's real config directories."""
    import prefs
    import service_status
    import usage_diagnosis_snapshot
    from analyzer import usage_snapshot

    state_dir = tmp_path / "user-state"
    monkeypatch.setattr(prefs, "PREFERENCES_FILE", state_dir / "usage-preferences.json")
    monkeypatch.setattr(
        usage_diagnosis_snapshot,
        "SNAPSHOT_PATH",
        state_dir / "usage-diagnosis.json",
    )
    monkeypatch.setattr(
        usage_snapshot,
        "SNAPSHOT_PATH",
        state_dir / "usage_snapshot.json",
    )
    monkeypatch.setattr(
        service_status,
        "ALERT_STATE_PATH",
        state_dir / "service-alert-state.json",
    )
    monkeypatch.setattr(
        service_status,
        "CLAUDE_STATUS",
        replace(
            service_status.CLAUDE_STATUS,
            cache_path=state_dir / "anthropic-status-cache.json",
        ),
    )
    monkeypatch.setattr(
        service_status,
        "CODEX_STATUS",
        replace(
            service_status.CODEX_STATUS,
            cache_path=state_dir / "openai-status-cache.json",
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_dispatch_ledger_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep report builds from reading real Codex/Antigravity sessions and quota caches."""
    from loaders import agy_loader, agy_quota_probe, codex_loader

    monkeypatch.setattr(codex_loader, "SESSIONS_DIR", tmp_path / "codex-home" / "sessions")
    monkeypatch.setattr(codex_loader, "LOGS_DB", tmp_path / "codex-home" / "logs_2.sqlite")
    monkeypatch.setattr(agy_loader, "AGY_SESSIONS_DIR", tmp_path / "agy-conversations")
    monkeypatch.setattr(agy_quota_probe, "CACHE_PATH", tmp_path / "agy_quota_cache.json")


@pytest.fixture(autouse=True)
def _isolate_codex_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prevent self_heal from writing a user's real Codex, Antigravity or Grok config."""
    from installer import setup_hook

    codex_dir = tmp_path / "codex"
    monkeypatch.setattr(setup_hook, "CODEX_CONFIG", codex_dir / "config.toml")
    monkeypatch.setattr(setup_hook, "CODEX_BACKUP", codex_dir / "usage-backup.json")
    monkeypatch.setattr(setup_hook, "LEGACY_CODEX_BACKUP", codex_dir / "tt-backup.json")
    agy_dir = tmp_path / "antigravity-cli"
    monkeypatch.setattr(setup_hook, "AGY_SETTINGS", agy_dir / "settings.json")
    monkeypatch.setattr(setup_hook, "AGY_HOOK_TARGET", agy_dir / "usage-statusline-agy.py")
    monkeypatch.setattr(
        setup_hook,
        "AGY_PREVIOUS_STATUSLINE",
        agy_dir / "usage-previous-statusline.json",
    )
    grok_dir = tmp_path / "grok"
    monkeypatch.setattr(setup_hook, "GROK_SETTINGS", grok_dir / "config.toml")
    monkeypatch.setattr(setup_hook, "GROK_HOOK_TARGET", grok_dir / "usage-statusline-grok.py")
    monkeypatch.setattr(
        setup_hook,
        "GROK_PREVIOUS_STATUSLINE",
        grok_dir / "usage-previous-statusline-grok.json",
    )


@pytest.fixture
def patch_setup_hook_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., SetupHookPaths]:
    def factory(**kwargs: Any) -> SetupHookPaths:
        return _patch_setup_hook_paths(monkeypatch, tmp_path, **kwargs)

    return factory


@pytest.fixture
def patch_resume_hook_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., ResumeHookPaths]:
    def factory(**kwargs: Any) -> ResumeHookPaths:
        return _patch_resume_hook_paths(monkeypatch, tmp_path, **kwargs)

    return factory


@pytest.fixture
def patch_terse_hook_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., TerseHookPaths]:
    def factory(**kwargs: Any) -> TerseHookPaths:
        return _patch_terse_hook_paths(monkeypatch, tmp_path, **kwargs)

    return factory
