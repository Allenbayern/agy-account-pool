# agy-account-pool

[English](README.md) | [简体中文](README.zh-CN.md)

Unified macOS auto-switcher that keeps **Antigravity Desktop (Classic)** and **`agy` CLI** locked onto the same healthy account across your local account pool — triggered by either **live `429` quota errors** or **configurable quota percentage thresholds** without interrupting active generations.

---

## Why This Exists

- **Eliminates Desktop vs. CLI drift**: Antigravity Tools' built-in Classic auto-switch updates the Desktop keychain/files (`oauth_creds.json`, `google_accounts.json`, `jetski-standalone-oauth-token`) without updating `~/.gemini/antigravity-cli/antigravity-oauth-token`, leaving CLI agents stuck on an exhausted account.
- **Closes the "fake healthy group" blind spot**: A quota group summary can report `6%` remaining while individual Gemini models inside `quota.models[]` have already hit `0%`. `agy-account-pool` scores `min(group_5h_weekly, primary_models_min)`.
- **Zero-disruption switching**: Mild threshold switches (`5%–15%`) wait for Desktop streaming to go quiet before reloading `language_server`; hard `429` errors or `0%`/`≤5%` exhaustion switch both clients immediately.

## Architecture

```text
  ~/.antigravity_tools/accounts/*.json      Desktop language_server.log & cli-*.log
                  │                                        │
        (quota threshold ≤ 15%)                 (429 RESOURCE_EXHAUSTED)
                  └───────────────────┬────────────────────┘
                                      ▼
                     agy-account-follow --watch (--mode pool)
                       ├─ Desktop idle-guard (60s quiet, max wait 300s)
                       ├─ Per-account quarantine (from "Resets in ...")
                       └─ Authoritative Desktop drift fix (CLI-only sync)
                                      │
          ┌───────────────────────────┴───────────────────────────┐
          ▼                                                       ▼
  antigravity-tools switch --target classic          Sync 4 ~/.gemini/* CLI files
     (Desktop Keychain + language_server)             + current_agy_account_id
```

## Features

- **Dual triggers**:
  1. **Live 429 watcher**: Tails Desktop `language_server.log` and the newest `cli-*.log` files by `(inode, offset)`, ignores monitor/503/network noise, parses `Resets in XhYmZs`, quarantines the depleted account, and switches both targets immediately.
  2. **Dual-layer quota threshold** (default `15%` on `gemini` group): Ignores unrelated Claude/GPT exhaustion, bypasses busy wait if `≤ 5%` or any primary model hits `0%`, otherwise waits for `60s` idle (capped at `300s`).
- **Authoritative Desktop drift invariant**: If Desktop is healthy and CLI drifted, only syncs CLI credentials and cursor — never restarts Desktop `language_server`.
- **Fast `--jit` wrapper hook**: Optional `~/.gemini/bin/agy` hook aligns CLI credentials before every `agy` invocation with a non-blocking `flock`.
- **Automatic log rotation & quiet stderr**: Rotates `~/Library/Logs/agy-account-follow.log` at 2 MB (keeping `.log.1`) and sets `AGY_FOLLOW_QUIET=1` in LaunchAgent so `err.log` captures only uncaught exceptions.
- **Zero external Python dependencies**: Standard library only (`Python 3.9+` on macOS).

## Prerequisites

- **macOS** (uses `launchd`, `osascript` notifications, and `antigravity-tools` Keychain integration)
- **Python 3.9+** (`/usr/bin/python3` works out of the box)
- **Antigravity.app** and **[Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager)** (`antigravity-tools` CLI available in `PATH`, `~/.local/bin/`, or `/Applications/Antigravity Tools.app/Contents/MacOS/antigravity-tools`)

## Quick Start

```bash
git clone https://github.com/Allenbayern/agy-account-pool.git
cd agy-account-pool
./install.sh
```

Recommended **Antigravity Tools** settings (prevents two switchers from fighting):

1. **Disable** built-in **Classic Auto-Switch** (`classic_auto_switch.enabled = false`)
2. **Keep Auto-Refresh enabled** and set **Refresh Interval** to **5 minutes**
3. **Enable Launch at Login** (so quota JSON keeps refreshing after reboot)

## Account Filtering (Optional)

By default, every valid account in `~/.antigravity_tools/accounts/` is eligible. To exclude a personal account or restrict to an explicit allowlist, create `~/.config/agy-account-follow/config.json` (see `config.example.json`):

```json
{
  "allowed_accounts": [],
  "excluded_accounts": ["personal@example.com"]
}
```

Environment overrides `AGY_EXCLUDED_ACCOUNTS` and `AGY_ALLOWED_ACCOUNTS` (comma-separated) are also supported.

## Common Commands

```bash
# Inspect Desktop vs. CLI status and pool quota table
agy-account-follow --check --mode pool --threshold 0.15

# Manually switch both Desktop and CLI (run from an external terminal)
agy-account-follow --switch-to acct2@example.com

# Clear per-account 429/threshold quarantine timers
agy-account-follow --clear-cooldowns

# Check or reinstall the ~/.gemini/bin/agy JIT hook
agy-follow-wrapper-hook --status
agy-follow-wrapper-hook --install

# Follow live daemon logs
tail -f ~/Library/Logs/agy-account-follow.log

# Run unit tests
python3 -m unittest discover -s tests -v

# Uninstall LaunchAgent, wrapper hook, and symlinks
./uninstall.sh
```

## Security, Privacy & Disclaimer

- Operates strictly locally; no network calls or telemetry are added by this project.
- Token-like strings (`ya29.*`, `1//*`, `eyJ*`, `Bearer ...`) are automatically redacted in logs.
- Designed solely for managing your own authorized accounts across local clients; use responsibly in accordance with applicable Terms of Service.

## License

[MIT](LICENSE)
