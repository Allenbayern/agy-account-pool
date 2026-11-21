# -*- coding: utf-8 -*-
"""Desktop (Classic via antigravity-tools) + CLI credential switching & sidecar reaper."""

import base64
import datetime as _dt
import os
import signal
import subprocess
import time

from .config import (
    SCOPE,
    atomic_write_json,
    backup_file,
    get_paths,
    is_account_allowed,
    log,
    read_json,
    redact_text,
    save_state,
)
from .monitor import advance_log_cursors_to_eof


def decode_email(id_token):
    if not id_token or not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        import json
        claims = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    return claims.get("email")


def cli_identity():
    paths = get_paths()
    for path in (paths["CLI_TOKEN"], paths["JETSKI_TOKEN"]):
        data = read_json(path)
        if isinstance(data, dict):
            email = data.get("id_token_email") or decode_email(data.get("id_token"))
            if email:
                return email, path
    data = read_json(paths["GOOGLE_ACCOUNTS"])
    if isinstance(data, dict) and data.get("active"):
        return data["active"], paths["GOOGLE_ACCOUNTS"]
    data = read_json(paths["OAUTH_CREDS"])
    if isinstance(data, dict):
        email = decode_email(data.get("id_token"))
        if email:
            return email, paths["OAUTH_CREDS"]
    return None, None


def rfc3339_z(epoch_seconds):
    return _dt.datetime.fromtimestamp(int(epoch_seconds), _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z"
    )


def desired_payloads(acct):
    paths = get_paths()
    tok = acct["token"]
    email = acct.get("email") or tok.get("email")
    if not is_account_allowed(email):
        raise RuntimeError("Policy blocked: refusing to write disallowed account %s" % email)
    access = tok["access_token"]
    refresh = tok["refresh_token"]
    id_token = tok.get("id_token") or ""
    token_type = tok.get("token_type") or "Bearer"
    expires_in = int(tok.get("expires_in") or 3599)
    expiry_ts = tok.get("expiry_timestamp")
    if expiry_ts is None:
        expiry_ts = int(time.time()) + expires_in

    oauth_creds = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": token_type,
        "expiry_date": int(expiry_ts) * 1000,
        "id_token": id_token,
        "scope": SCOPE,
    }
    google_accounts = {"active": email, "old": []}
    token_block = {
        "access_token": access,
        "token_type": token_type,
        "refresh_token": refresh,
        "expiry": rfc3339_z(expiry_ts),
        "expires_in": expires_in,
    }
    cli_like = {
        "token": token_block,
        "auth_method": "consumer",
        "id_token_email": email,
        "id_token": id_token,
    }
    return {
        paths["OAUTH_CREDS"]: oauth_creds,
        paths["GOOGLE_ACCOUNTS"]: google_accounts,
        paths["CLI_TOKEN"]: cli_like,
        paths["JETSKI_TOKEN"]: cli_like,
    }


def bump_hub_reload(email):
    hub_reload = get_paths()["HUB_RELOAD"]
    try:
        os.makedirs(os.path.dirname(hub_reload), exist_ok=True)
        with open(hub_reload, "w", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (int(time.time()), email))
        return True
    except OSError:
        return False


def sync_cursor(acct):
    accounts_index = get_paths()["ACCOUNTS_INDEX"]
    idx = read_json(accounts_index)
    if not isinstance(idx, dict):
        return False
    want = acct.get("id")
    if not want or idx.get("current_agy_account_id") == want:
        return False
    backup_file(accounts_index)
    idx["current_agy_account_id"] = want
    atomic_write_json(accounts_index, idx)
    log("CURSOR current_agy_account_id -> %s (%s)" % (want, acct.get("email")))
    return True


def reap_orphan_sidecars():
    gemini_dir = get_paths()["GEMINI_DIR"]
    killed = 0
    cleaned_hubs = 0
    try:
        out = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            if ppid == 1 and "sidecar_sdk/register_loader.js" in parts[2]:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    except Exception:
        pass

    daemon_dir = os.path.join(gemini_dir, "antigravity", "daemon")
    if os.path.isdir(daemon_dir):
        try:
            names = os.listdir(daemon_dir)
        except OSError:
            names = []
        for name in names:
            if not (name.startswith("hub_") and name.endswith(".json")):
                continue
            fpath = os.path.join(daemon_dir, name)
            data = read_json(fpath)
            pid = data.get("pid") if isinstance(data, dict) else None
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    try:
                        os.remove(fpath)
                        cleaned_hubs += 1
                    except OSError:
                        pass
                except (PermissionError, OSError):
                    pass

    if killed or cleaned_hubs:
        log("REAP 清理孤儿 sidecar=%d, 过期 hub=%d" % (killed, cleaned_hubs))
    return killed, cleaned_hubs


def sync_cli_only(acct, no_reload=False, sync_cursor_flag=True, force=False):
    want_email = acct.get("email")
    have_email, _ = cli_identity()
    in_sync = bool(have_email) and have_email.strip().lower() == (want_email or "").strip().lower()
    cursor_synced = sync_cursor(acct) if sync_cursor_flag else False

    if in_sync and not force:
        return {"action": "none", "hub_reload": False, "cursor_synced": cursor_synced, "backups": 0}

    payloads = desired_payloads(acct)
    backups = []
    for path, payload in payloads.items():
        b = backup_file(path)
        if b:
            backups.append(b)
        atomic_write_json(path, payload)

    reloaded = False if no_reload else bump_hub_reload(want_email)
    return {"action": "synced", "hub_reload": reloaded, "cursor_synced": cursor_synced, "backups": len(backups)}


def switch_classic_via_tools(acct, timeout=30):
    paths = get_paths()
    at_bin = paths["AT_BIN"]
    email = acct.get("email")
    want_id = acct.get("id")
    if not email or not os.path.exists(at_bin):
        return False, "antigravity-tools 不可用: %s" % at_bin
    try:
        res = subprocess.run(
            [at_bin, "switch", email, "--target", "classic"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return False, "调用 antigravity-tools 失败: %s" % redact_text(exc)
    if res.returncode != 0:
        msg = redact_text((res.stderr or res.stdout or "").strip())
        return False, "antigravity-tools 退出码 %d: %s" % (res.returncode, msg[:200])
    idx = read_json(paths["ACCOUNTS_INDEX"]) or {}
    if want_id and idx.get("current_classic_account_id") != want_id:
        return False, "切换后 current_classic_account_id 未更新为 %s" % want_id
    return True, ""


def notify_macos(title, message):
    if os.environ.get("AGY_NO_NOTIFY") == "1":
        return
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv)\n"
        "end run"
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, str(message), str(title)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


def switch_desktop_and_cli(
    acct,
    state,
    trigger,
    from_email,
    now=None,
    cli_only=False,
    no_reload=False,
    sync_cursor_flag=True,
    notify=True,
    force=False,
):
    now = now if now is not None else time.time()
    want_email = acct.get("email")
    if not cli_only:
        ok, err = switch_classic_via_tools(acct)
        if not ok:
            log("ERROR Classic 切换失败 (%s -> %s): %s" % (from_email, want_email, err))
            return False, {"error": err}

    cli_res = sync_cli_only(acct, no_reload=no_reload, sync_cursor_flag=sync_cursor_flag, force=force)
    reap_orphan_sidecars()
    advance_log_cursors_to_eof(state)

    state["last_switch_ts"] = int(now)
    state["last_switch"] = {
        "from": from_email,
        "to": want_email,
        "trigger": trigger,
        "ts": int(now),
        "cli_only": bool(cli_only),
    }
    state.pop("pending_switch", None)
    save_state(state)

    if notify:
        notify_macos(
            "Antigravity 账号已切换",
            "%s -> %s (%s)" % (from_email or "?", want_email, trigger),
        )

    log(
        "DUAL_SYNC %s -> %s (trigger=%s, cli_only=%s, backups=%d, hub_reload=%s)"
        % (
            from_email,
            want_email,
            trigger,
            cli_only,
            cli_res.get("backups", 0),
            "yes" if cli_res.get("hub_reload") else "no",
        )
    )
    return True, cli_res
