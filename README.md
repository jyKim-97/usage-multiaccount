<p align="center">
  <img src="docs/readme-logo.png" alt="usage logo" width="128">
</p>

# usage — multi-account fork

### A macOS menu bar monitor for Claude Code and Codex quota, with per-account Codex quota when you pool logins through OpenCodex.

English · [한국어](docs/README.ko.md)

<p align="center">
  <img src="docs/fork-panel.png" alt="The compact Default panel of this fork: Claude Code plus two Codex accounts" width="300">
</p>

> A personal fork of [aqua5230/usage](https://github.com/aqua5230/usage) by lollapalooza. All credit for the app goes to the original project; see its README for the full feature set. This fork only adds the changes below.

## What This Fork Changes

- **Multiple Codex accounts:** if you pool Codex logins with OpenCodex (`ocx`), the Codex card shows one block per account: its label, masked email, and its own 5-hour, weekly, or monthly windows. The menu bar follows the account currently routing requests.
- **Quota that follows you to remote servers:** Claude and Codex quota is account-wide, so the app also reads it from the providers' own quota endpoints. Work done over SSH shows up even though its logs stay on the server.
- **Compact Default panel:** 300 pt wide, the macOS system font and text styles, one line per quota, hairline sections instead of floating cards, and icon-only Refresh / Quit.
- **Compact totals:** today's and yesterday's token totals read `11.2M` instead of `11,150,000`.
- **System font in the HTML report** as well.
- **Runs unbundled:** `uv run python main.py` no longer crashes at launch; notifications are skipped outside an app bundle.

## Run From Source

Requirements: macOS and [uv](https://docs.astral.sh/uv/), which installs Python 3.13. Don't build with conda's Python: its bundled `libffi` / `libsqlite3` make the `.app` crash on launch.

```bash
uv sync
uv run python main.py            # run in the menu bar
uv run python main.py --mock     # fake data, for UI work
./scripts/build_app.sh           # build dist/usage.app
cp -R dist/usage.app /Applications/
```

The refresh interval defaults to 60 seconds (`--interval N`, minimum 30).

## Multiple Codex Accounts

1. Add accounts to OpenCodex's pool (`ocx account login openai`). Once `ocx` has fetched their quota, the Codex card switches to one block per account.
2. Each block is named after the account's OpenCodex label. To rename one, run `ocx account alias openai <account-id> <name>`.
3. `●` marks the account currently routing requests. `⚠ about N minutes ago` means OpenCodex hasn't refreshed that account's quota recently. A window whose reset time has passed shows `--` instead of a stale percentage.

The account Codex itself is signed in to is also read live from `codex app-server` every minute, so it stays current even while OpenCodex is stopped. Accounts that exist only in the OpenCodex pool need `ocx` running to refresh; while it is stopped they keep their last values (with the `⚠` age) and their names come from `~/.opencodex/config.json`. Without OpenCodex at all, the card shows that one live account. Only the Default theme renders per-account blocks; the other 13 themes are unchanged from upstream.

## Data Sources & Privacy

- Nothing here calls an LLM inference API or spends a token. Every network call below is a quota metadata read.
- **Claude:** the statusLine hook file while Claude Code runs locally; otherwise, at most every 10 minutes, Anthropic's OAuth usage endpoint (the one behind `/usage`) with the access token Claude Code keeps in the macOS Keychain. The token is only read: never refreshed or written back, so Claude Code stays signed in. macOS may ask once to allow Keychain access.
- **Codex:** local session logs, plus `codex app-server`'s `account/rateLimits/read` for Codex's own login; Codex handles its own sign-in.
- Per-account Codex quota comes from OpenCodex's local cache, `~/.opencodex/codex-quota-cache.json`. Account labels come from `ocx account list openai --json`, whose output OpenCodex already masks. `usage` never opens `codex-accounts.json` (it holds OAuth tokens) and never runs `ocx account refresh`, which would probe upstream.
- Other network access is the same as upstream: Antigravity's quota endpoint if you use it, the public Claude and Codex status pages, a public model-pricing table, and the update check. The update check looks at upstream's releases and only opens a browser page; turn it off in the menu if you don't want the prompt.

## Everything Else

The status line hook, HTML reports, Antigravity and Grok cards, themes, and the Windows tray work as they do upstream. See the [upstream README](https://github.com/aqua5230/usage#readme) and the [development docs](docs/DEVELOPMENT.md).

## License

AGPL-3.0-only (see [LICENSE](LICENSE)). Copyright © 2026 lollapalooza; this fork's modifications are released under the same license.
