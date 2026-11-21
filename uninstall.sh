#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

PLIST_DST="$HOME/Library/LaunchAgents/com.agy.account-follow.plist"
if [[ -f "$PLIST_DST" ]]; then
  UID_VAL="$(id -u)"
  launchctl bootout "gui/$UID_VAL" "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "Unloaded and removed $PLIST_DST"
fi

if [[ -f "$HOME/.gemini/bin/agy" && -x "$BIN_DIR/agy-follow-wrapper-hook" ]]; then
  python3 "$BIN_DIR/agy-follow-wrapper-hook" --revert || true
fi

for name in agy-account-follow agy-follow-wrapper-hook; do
  target="$BIN_DIR/$name"
  if [[ -L "$target" ]]; then
    resolved="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$target")"
    if [[ "$resolved" == "$REPO_DIR/"* ]]; then
      rm -f "$target"
      echo "Removed symlink $target"
    fi
  fi
done

echo "Uninstall complete. Local credentials in ~/.antigravity_tools and ~/.gemini were not modified."
