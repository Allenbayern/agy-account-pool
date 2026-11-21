#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS_NAME="$(uname -s)"
THRESHOLD="0.15"
GROUP="gemini"
POLL_SECONDS="3"
QUOTA_POLL_SECONDS="30"
IDLE_SECONDS="60"
IDLE_MAX_WAIT="300"
BIN_DIR="$HOME/.local/bin"
NO_SERVICE=0
NO_HOOK=0
if [[ "$OS_NAME" == "Linux" ]]; then
  CLI_ONLY=1
  NO_NOTIFY=1
else
  CLI_ONLY=0
  NO_NOTIFY=0
fi
EXTRA_LOGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --poll-seconds) POLL_SECONDS="$2"; shift 2 ;;
    --quota-poll-seconds) QUOTA_POLL_SECONDS="$2"; shift 2 ;;
    --idle-seconds) IDLE_SECONDS="$2"; shift 2 ;;
    --idle-max-wait) IDLE_MAX_WAIT="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --cli-only) CLI_ONLY=1; shift ;;
    --no-notify) NO_NOTIFY=1; shift ;;
    --extra-log) EXTRA_LOGS+=("$2"); shift 2 ;;
    --no-launchagent|--no-systemd) NO_SERVICE=1; shift ;;
    --no-hook) NO_HOOK=1; shift ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./install.sh [options]
  --threshold <0..1>          Quota threshold to trigger switch (default: 0.15)
  --group <name>              Quota group to watch (default: gemini)
  --poll-seconds <sec>        Log watcher poll interval (default: 3)
  --quota-poll-seconds <sec>  Full quota evaluation interval (default: 30)
  --idle-seconds <sec>        Quiet window before mild threshold switch (default: 60)
  --idle-max-wait <sec>       Max defer time while desktop is streaming (default: 300)
  --bin-dir <dir>             Target symlink bin directory (default: ~/.local/bin)
  --cli-only                  Run in pure CLI anchor mode (default on Linux)
  --no-notify                 Disable desktop notifications (default on Linux)
  --extra-log <path_or_glob>  Extra 429 log file or glob to watch (repeatable)
  --no-launchagent            Skip LaunchAgent / systemd --user service installation
  --no-systemd                Alias for --no-launchagent
  --no-hook                   Skip ~/.gemini/bin/agy wrapper hook installation
USAGE
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$OS_NAME" != "Darwin" && "$OS_NAME" != "Linux" ]]; then
  echo "ERROR: Unsupported OS '$OS_NAME'. This installer supports macOS (Darwin) and Linux." >&2
  exit 1
fi

PYTHON3="$(command -v python3 || true)"
if [[ -z "$PYTHON3" ]]; then
  echo "ERROR: python3 not found in PATH." >&2
  exit 1
fi

if ! "$PYTHON3" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "ERROR: Python 3.9+ is required (found: $("$PYTHON3" --version))." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/agy-account-follow" "$BIN_DIR/agy-account-follow"
ln -sfn "$REPO_DIR/agy-follow-wrapper-hook" "$BIN_DIR/agy-follow-wrapper-hook"
echo "Symlinked binaries into $BIN_DIR"

if [[ "$OS_NAME" == "Darwin" && "$CLI_ONLY" -eq 0 ]]; then
  AT_BIN=""
  if command -v antigravity-tools >/dev/null 2>&1; then
    AT_BIN="$(command -v antigravity-tools)"
  elif [[ -x "$BIN_DIR/antigravity-tools" ]]; then
    AT_BIN="$BIN_DIR/antigravity-tools"
  elif [[ -x "/Applications/Antigravity Tools.app/Contents/MacOS/antigravity-tools" ]]; then
    AT_BIN="/Applications/Antigravity Tools.app/Contents/MacOS/antigravity-tools"
    ln -sfn "$AT_BIN" "$BIN_DIR/antigravity-tools"
    echo "Linked antigravity-tools CLI into $BIN_DIR/antigravity-tools"
  fi

  if [[ -z "$AT_BIN" ]]; then
    echo "WARN: antigravity-tools CLI was not found. Desktop switching requires Antigravity Tools:"
    echo "      Install Antigravity Tools.app and ensure 'antigravity-tools' is in PATH or $BIN_DIR."
  fi
fi

if [[ "$NO_HOOK" -eq 0 && -f "$HOME/.gemini/bin/agy" ]]; then
  "$PYTHON3" "$BIN_DIR/agy-follow-wrapper-hook" --install
fi

if [[ "$OS_NAME" == "Darwin" ]]; then
  if [[ "$NO_SERVICE" -eq 0 ]]; then
    PLIST_DST="$HOME/Library/LaunchAgents/com.agy.account-follow.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    "$PYTHON3" - "$REPO_DIR/templates/com.agy.account-follow.plist.tmpl" "$PLIST_DST" \
      "$HOME" "$PYTHON3" "$BIN_DIR" "$THRESHOLD" "$GROUP" "$POLL_SECONDS" "$QUOTA_POLL_SECONDS" "$IDLE_SECONDS" "$IDLE_MAX_WAIT" <<'PY'
import sys
tmpl_path, out_path, home, py3, bindir, thresh, grp, poll, qpoll, idle, maxw = sys.argv[1:]
with open(tmpl_path, "r", encoding="utf-8") as fh:
    txt = fh.read()
for k, v in (
    ("{{HOME}}", home),
    ("{{PYTHON3}}", py3),
    ("{{BIN_DIR}}", bindir),
    ("{{THRESHOLD}}", thresh),
    ("{{GROUP}}", grp),
    ("{{POLL_SECONDS}}", poll),
    ("{{QUOTA_POLL_SECONDS}}", qpoll),
    ("{{IDLE_SECONDS}}", idle),
    ("{{IDLE_MAX_WAIT}}", maxw),
):
    txt = txt.replace(k, v)
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(txt)
PY
    chmod 600 "$PLIST_DST"
    plutil -lint "$PLIST_DST"
    UID_VAL="$(id -u)"
    launchctl bootout "gui/$UID_VAL" "$PLIST_DST" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_VAL" "$PLIST_DST"
    echo "Loaded LaunchAgent: $PLIST_DST"
  fi

  cat <<EOF

Post-install checklist:
1. Open Antigravity Tools -> Settings:
   - Disable built-in Classic Auto-Switch (prevents dual-switcher conflicts)
   - Set Auto-Refresh Interval to 5 minutes (keep Auto-Refresh enabled)
   - Enable Launch at Login (recommended so quota data refreshes after reboot)
2. Verify status:
   $BIN_DIR/agy-account-follow --check --mode pool --threshold $THRESHOLD
3. Optional account filtering (~/.config/agy-account-follow/config.json):
   Copy config.example.json and add excluded_accounts or allowed_accounts.
EOF
else
  ACCOUNTS_DIR="${AGY_FOLLOW_TOOLS_DIR:-$HOME/.antigravity_tools}/accounts"
  if ! compgen -G "$ACCOUNTS_DIR/*.json" >/dev/null 2>&1; then
    echo "WARN: No account JSON files found in $ACCOUNTS_DIR yet."
    echo "      Sync your allowed account JSON files into $ACCOUNTS_DIR before relying on auto-switching."
  fi

  if [[ "$NO_SERVICE" -eq 0 ]]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agy-account-follow"
    UNIT_DST="$UNIT_DIR/agy-account-follow.service"
    mkdir -p "$UNIT_DIR" "$STATE_DIR"
    "$PYTHON3" - "$REPO_DIR/templates/agy-account-follow.service.tmpl" "$UNIT_DST" \
      "$HOME" "$PYTHON3" "$BIN_DIR" "$CLI_ONLY" "$NO_NOTIFY" "$THRESHOLD" "$GROUP" "$POLL_SECONDS" "$QUOTA_POLL_SECONDS" "${EXTRA_LOGS[@]}" <<'PY'
import re
import sys

tmpl_path, out_path, home, py3, bindir, cli_only, no_notify, thresh, grp, poll, qpoll, *extra_logs = sys.argv[1:]
with open(tmpl_path, "r", encoding="utf-8") as fh:
    txt = fh.read()

def _sd_quote(val):
    if re.fullmatch(r"[A-Za-z0-9_./*?~-]+", val):
        return val
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'

extra_args = "".join(" --extra-log " + _sd_quote(p) for p in extra_logs if p.strip())
replacements = (
    ("{{HOME}}", home),
    ("{{PYTHON3}}", py3),
    ("{{BIN_DIR}}", bindir),
    ("{{CLI_ONLY_ARG}}", " --cli-only" if cli_only == "1" else ""),
    ("{{NO_NOTIFY_ARG}}", " --no-notify" if no_notify == "1" else ""),
    ("{{THRESHOLD}}", thresh),
    ("{{GROUP}}", grp),
    ("{{POLL_SECONDS}}", poll),
    ("{{QUOTA_POLL_SECONDS}}", qpoll),
    ("{{EXTRA_LOG_ARGS}}", extra_args),
)
for k, v in replacements:
    txt = txt.replace(k, v)
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(txt)
PY
    chmod 644 "$UNIT_DST"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload
      systemctl --user enable agy-account-follow.service
      systemctl --user restart agy-account-follow.service
      echo "Enabled & started systemd user service: $UNIT_DST"
    else
      echo "WARN: systemctl not found; wrote service file to $UNIT_DST without starting it."
    fi
    if command -v loginctl >/dev/null 2>&1; then
      LINGER_STATE="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)"
      if [[ "$LINGER_STATE" != "yes" ]]; then
        echo "WARN: loginctl Linger=$LINGER_STATE (not 'yes'). To keep user services running without an active login session, run:"
        echo "      sudo loginctl enable-linger $USER"
      fi
    fi
  fi

  cat <<EOF

Post-install checklist (Linux Pure CLI):
1. Ensure pool JSONs exist in $ACCOUNTS_DIR and configure allowlist/exclusions in ~/.config/agy-account-follow/config.json.
2. If agy-swap's wrapper (~/.local/bin/agy) is used, ensure AGY_SWAP_SKIP_SYNC=1 is set so agy-swap --sync-session does not overwrite switched credentials.
3. Verify status:
   $BIN_DIR/agy-account-follow --check --mode pool --cli-only --threshold $THRESHOLD
4. Follow logs:
   tail -f ${XDG_STATE_HOME:-$HOME/.local/state}/agy-account-follow/agy-account-follow.log
   journalctl --user -u agy-account-follow.service -f
EOF
fi
