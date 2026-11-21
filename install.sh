#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THRESHOLD="0.15"
GROUP="gemini"
POLL_SECONDS="3"
QUOTA_POLL_SECONDS="30"
IDLE_SECONDS="60"
IDLE_MAX_WAIT="300"
BIN_DIR="$HOME/.local/bin"
NO_LAUNCHAGENT=0
NO_HOOK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --poll-seconds) POLL_SECONDS="$2"; shift 2 ;;
    --quota-poll-seconds) QUOTA_POLL_SECONDS="$2"; shift 2 ;;
    --idle-seconds) IDLE_SECONDS="$2"; shift 2 ;;
    --idle-max-wait) IDLE_MAX_WAIT="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --no-launchagent) NO_LAUNCHAGENT=1; shift ;;
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
  --no-launchagent            Skip LaunchAgent installation/bootstrap
  --no-hook                   Skip ~/.gemini/bin/agy wrapper hook installation
USAGE
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: This installer targets macOS (Darwin)." >&2
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

if [[ "$NO_HOOK" -eq 0 && -f "$HOME/.gemini/bin/agy" ]]; then
  "$PYTHON3" "$BIN_DIR/agy-follow-wrapper-hook" --install
fi

if [[ "$NO_LAUNCHAGENT" -eq 0 ]]; then
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
