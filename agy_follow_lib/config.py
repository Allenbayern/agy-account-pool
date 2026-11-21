# -*- coding: utf-8 -*-
"""Configuration, path resolution, user policy filtering, redaction, state I/O, and locking."""

from contextlib import contextmanager
import datetime as _dt
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import time

SCOPE = (
    "https://www.googleapis.com/auth/userinfo.email openid "
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.profile"
)

STALE_SECONDS = 12 * 3600
MIN_SWITCH_INTERVAL_THRESHOLD = 60
MIN_SWITCH_INTERVAL_ERROR = 15
DEFAULT_QUARANTINE_SECONDS = 1800
MIN_QUARANTINE_SECONDS = 300
MAX_QUARANTINE_SECONDS = 6 * 3600

_REDACT_RE = re.compile(
    r"(ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]+|eyJ[A-Za-z0-9._-]+|"
    r"Bearer\s+\S+|#t=\S+|token[\"=: ]+\S+|secret[\"=: ]+\S+|password[\"=: ]+\S+)",
    re.I,
)


def _env_path(primary, secondary, default):
    raw = os.environ.get(primary) or os.environ.get(secondary) or default
    return os.path.expanduser(raw)


def get_paths():
    home = os.path.expanduser("~")
    tools_dir = _env_path("AGY_FOLLOW_TOOLS_DIR", "TOOLS_DIR", os.path.join(home, ".antigravity_tools"))
    gemini_dir = _env_path("AGY_FOLLOW_GEMINI_DIR", "GEMINI_DIR", os.path.join(home, ".gemini"))
    cli_dir = os.path.join(gemini_dir, "antigravity-cli")
    cache_dir = os.path.join(cli_dir, "cache")
    config_default = os.path.join(home, ".config", "agy-account-follow", "config.json")
    return {
        "HOME": home,
        "TOOLS_DIR": tools_dir,
        "ACCOUNTS_INDEX": os.path.join(tools_dir, "accounts.json"),
        "ACCOUNTS_DIR": os.path.join(tools_dir, "accounts"),
        "GEMINI_DIR": gemini_dir,
        "CLI_DIR": cli_dir,
        "OAUTH_CREDS": os.path.join(gemini_dir, "oauth_creds.json"),
        "GOOGLE_ACCOUNTS": os.path.join(gemini_dir, "google_accounts.json"),
        "CLI_TOKEN": os.path.join(cli_dir, "antigravity-oauth-token"),
        "JETSKI_TOKEN": os.path.join(gemini_dir, "jetski-standalone-oauth-token"),
        "HUB_RELOAD": os.path.join(cache_dir, "agy-swap-hub-reload"),
        "DESKTOP_LS_LOG": _env_path(
            "AGY_DESKTOP_LS_LOG",
            "DESKTOP_LS_LOG",
            os.path.join(home, "Library", "Logs", "Antigravity", "language_server.log"),
        ),
        "CLI_LOG_DIR": _env_path("AGY_CLI_LOG_DIR", "CLI_LOG_DIR", os.path.join(cli_dir, "log")),
        "LOG_PATH": _env_path(
            "AGY_FOLLOW_LOG_PATH",
            "LOG_PATH",
            os.path.join(home, "Library", "Logs", "agy-account-follow.log"),
        ),
        "AT_BIN": _env_path("AGY_AT_BIN", "AT_BIN", os.path.join(home, ".local", "bin", "antigravity-tools")),
        "LOCK_PATH": _env_path("AGY_LOCK_PATH", "LOCK_PATH", os.path.join(cache_dir, "agy-follow.lock")),
        "STATE_PATH": _env_path("AGY_STATE_PATH", "STATE_PATH", os.path.join(cache_dir, "agy-follow-state.json")),
        "CONFIG_PATH": _env_path("AGY_FOLLOW_CONFIG", "AGY_CONFIG_PATH", config_default),
    }


def load_user_config():
    cfg_path = get_paths()["CONFIG_PATH"]
    data = read_json(cfg_path)
    return data if isinstance(data, dict) else {}


def _norm_email_set(items):
    out = set()
    if isinstance(items, str):
        items = items.split(",")
    if isinstance(items, (list, tuple, set)):
        for x in items:
            if isinstance(x, str) and x.strip():
                out.add(x.strip().lower())
    return out


def is_account_allowed(email):
    if not email:
        return False
    e = email.strip().lower()
    user_cfg = load_user_config()

    excluded = _norm_email_set(user_cfg.get("excluded_accounts"))
    excluded |= _norm_email_set(os.environ.get("AGY_EXCLUDED_ACCOUNTS"))
    if e in excluded:
        return False

    env_allowed = _norm_email_set(os.environ.get("AGY_ALLOWED_ACCOUNTS"))
    if env_allowed:
        return e in env_allowed

    cfg_allowed = _norm_email_set(user_cfg.get("allowed_accounts"))
    if cfg_allowed:
        return e in cfg_allowed

    return True


def redact_text(text):
    if not text:
        return ""
    return _REDACT_RE.sub("<REDACTED>", str(text))


def log(msg):
    paths = get_paths()
    stamp = _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    line = "%s %s" % (stamp, redact_text(msg))
    try:
        os.makedirs(os.path.dirname(paths["LOG_PATH"]), exist_ok=True)
        with open(paths["LOG_PATH"], "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if os.environ.get("AGY_FOLLOW_QUIET") != "1":
        print(line, file=sys.stderr)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def atomic_write_json(path, payload, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-agyfollow-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_file(path, keep=5):
    if not os.path.exists(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.bak-agyfollow-%s" % (path, stamp)
    try:
        shutil.copy2(path, dst)
        os.chmod(dst, 0o600)
    except OSError:
        return None
    base = os.path.basename(path)
    cands = sorted(
        (n for n in os.listdir(os.path.dirname(path)) if n.startswith(base + ".bak-agyfollow-")),
        reverse=True,
    )
    for old in cands[keep:]:
        try:
            os.remove(os.path.join(os.path.dirname(path), old))
        except OSError:
            pass
    return dst


def load_state():
    data = read_json(get_paths()["STATE_PATH"])
    return data if isinstance(data, dict) else {}


def save_state(state):
    try:
        atomic_write_json(get_paths()["STATE_PATH"], state)
    except OSError:
        pass


@contextmanager
def acquire_lock(nonblocking=False):
    lock_path = get_paths()["LOCK_PATH"]
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
    locked = False
    try:
        try:
            fcntl.flock(fd, flags)
            locked = True
        except OSError as exc:
            if nonblocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                yield False
                return
            raise
        yield True
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
