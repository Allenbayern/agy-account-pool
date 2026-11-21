# -*- coding: utf-8 -*-
"""Account pool loading, whitelist checks, dual-layer quota scoring, and quarantine."""

import datetime as _dt
import os
import time

from .config import STALE_SECONDS, get_paths, is_account_allowed, read_json


def parse_iso_utc(ts_str):
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def list_accounts():
    accounts_dir = get_paths()["ACCOUNTS_DIR"]
    out = []
    try:
        names = sorted(os.listdir(accounts_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        acct = read_json(os.path.join(accounts_dir, name))
        if isinstance(acct, dict) and acct.get("email"):
            out.append(acct)
    return out


def usable(acct):
    if not isinstance(acct, dict):
        return False
    if not is_account_allowed(acct.get("email")):
        return False
    if acct.get("disabled") or acct.get("banned") or acct.get("validation_blocked"):
        return False
    tok = acct.get("token") or {}
    if not tok.get("access_token") or not tok.get("refresh_token"):
        return False
    quota = acct.get("quota") or {}
    if quota.get("is_forbidden"):
        return False
    return True


def tools_active_account(source="classic"):
    """Resolve active account from ~/.antigravity_tools/accounts.json.

    Returns (acct_dict_or_None, reason, meta) where meta['reason_code'] distinguishes
    schema/index issues from whitelist or disabled account blocks.
    """
    paths = get_paths()
    idx = read_json(paths["ACCOUNTS_INDEX"])
    if not isinstance(idx, dict):
        return None, "accounts.json 不可读", {"source": source, "reason_code": "missing_index", "fields": {}}

    target = idx.get("current_target_ide")
    by_target = {
        "agy": idx.get("current_agy_account_id"),
        "ide": idx.get("current_ide_account_id"),
        "classic": idx.get("current_classic_account_id"),
    }.get(target)
    candidates = {
        "classic": [
            ("current_classic_account_id", idx.get("current_classic_account_id")),
            ("current_account_id", idx.get("current_account_id")),
        ],
        "auto": [
            ("current_account_id", idx.get("current_account_id")),
            ("current_agy_account_id", idx.get("current_agy_account_id")),
        ],
        "agy-binding": [("current_agy_account_id", idx.get("current_agy_account_id"))],
        "target": [
            ("current_target_ide=%s" % target, by_target),
            ("current_account_id", idx.get("current_account_id")),
        ],
    }.get(source, [])

    meta = {
        "source": source,
        "current_target_ide": target,
        "reason_code": "ok",
        "raw_email": None,
        "account_id": None,
        "fields": {
            "current_account_id": idx.get("current_account_id"),
            "current_agy_account_id": idx.get("current_agy_account_id"),
            "current_ide_account_id": idx.get("current_ide_account_id"),
            "current_classic_account_id": idx.get("current_classic_account_id"),
        },
    }

    aid, used = None, None
    for name, value in candidates:
        if value:
            aid, used = value, name
            break
    if not aid:
        meta["reason_code"] = "missing_field"
        return None, "accounts.json 里没有可用的当前账号字段（source=%s）" % source, meta

    meta["used_field"] = used
    meta["account_id"] = aid
    acct = read_json(os.path.join(paths["ACCOUNTS_DIR"], "%s.json" % aid))
    if not isinstance(acct, dict):
        meta["reason_code"] = "missing_account_file"
        return None, "账号文件缺失: %s.json" % aid, meta

    raw_email = acct.get("email")
    meta["raw_email"] = raw_email
    if not is_account_allowed(raw_email):
        meta["reason_code"] = "whitelist_blocked"
        return None, "Account %s is excluded or not allowed by policy" % raw_email, meta
    if acct.get("disabled") or acct.get("banned") or acct.get("validation_blocked"):
        meta["reason_code"] = "account_disabled"
        return None, "当前账号 %s 处于 disabled/banned/blocked 状态" % raw_email, meta
    tok = acct.get("token") or {}
    if not tok.get("access_token") or not tok.get("refresh_token"):
        meta["reason_code"] = "missing_tokens"
        return None, "账号 %s 缺少 access_token/refresh_token" % raw_email, meta
    return acct, "", meta


def select_group(quota, prefer="gemini"):
    groups = [g for g in (quota.get("quota_groups") or []) if isinstance(g, dict)]
    if not groups:
        return None
    key = (prefer or "").strip().lower()
    if not key or key == "all":
        return None
    if key == "gemini":
        for g in groups:
            name = str(g.get("display_name") or g.get("name") or g.get("group") or "").lower()
            if "gemini" in name and "argon" not in name:
                return g
    for g in groups:
        name = str(g.get("display_name") or g.get("name") or g.get("group") or "").lower()
        if key == "claude":
            if "claude" in name or "gpt" in name:
                return g
        elif key in name:
            return g
    return groups[0]


def _model_matches_group(model_name, group):
    name = (model_name or "").strip().lower()
    if not name:
        return False
    key = (group or "gemini").strip().lower()
    if key == "all":
        return True
    if key == "gemini":
        return name.startswith("gemini-") and ("argon" not in name) and ("orchestrator" not in name)
    if key == "argon":
        return ("argon" in name) or ("orchestrator" in name)
    if key == "claude":
        return ("claude" in name) or ("gpt" in name)
    return key in name


def models_min_score(quota, group="gemini"):
    models = quota.get("models") or []
    fracs = []
    zero_models = []
    low_resets = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "")
        if not _model_matches_group(name, group):
            continue
        pct = m.get("percentage")
        if not isinstance(pct, (int, float)):
            continue
        frac = max(0.0, min(1.0, float(pct) / 100.0))
        fracs.append(frac)
        if pct <= 0:
            zero_models.append(name)
        if pct <= 20:
            rts = parse_iso_utc(m.get("reset_time"))
            if rts is not None:
                low_resets.append(rts)
    if not fracs:
        return None, [], None
    return min(fracs), zero_models, (min(low_resets) if low_resets else None)


def quota_score(acct, now=None, group="gemini"):
    now = now if now is not None else time.time()
    quota = (acct or {}).get("quota") or {}
    chosen = select_group(quota, group)
    groups = [chosen] if chosen else [g for g in (quota.get("quota_groups") or []) if isinstance(g, dict)]
    fracs, windowed, observed, low_resets, all_resets = [], [], [], [], []
    for g in groups:
        for b in (g.get("buckets") or []):
            if not isinstance(b, dict):
                continue
            frac = b.get("remaining_fraction")
            if not isinstance(frac, (int, float)):
                continue
            fval = float(frac)
            fracs.append(fval)
            obs = b.get("observed_at")
            if isinstance(obs, (int, float)):
                observed.append(obs / 1000.0)
            if b.get("window") in ("5h", "weekly"):
                windowed.append(fval)
            rts = parse_iso_utc(b.get("reset_time"))
            if rts is not None:
                all_resets.append(rts)
                if fval <= 0.25:
                    low_resets.append(rts)

    models_min, zero_models, model_reset = models_min_score(quota, group=group)
    if not fracs and models_min is None:
        return {
            "known": False,
            "min_frac": None,
            "weekly_5h": None,
            "models_min_frac": None,
            "effective_frac": None,
            "zero_models": [],
            "reset_ts": None,
            "stale": True,
            "group_name": None,
        }

    weekly_5h = (min(windowed) if windowed else min(fracs)) if fracs else None
    min_frac = min(fracs) if fracs else models_min
    eff_candidates = [v for v in (weekly_5h, models_min) if v is not None]
    effective_frac = min(eff_candidates) if eff_candidates else None

    newest = max(observed) if observed else (quota.get("last_updated") or 0)
    stale = (not newest) or ((now - newest) > STALE_SECONDS)

    reset_candidates = []
    if low_resets:
        reset_candidates.extend(low_resets)
    if model_reset is not None:
        reset_candidates.append(model_reset)
    if not reset_candidates and all_resets:
        reset_candidates.extend(all_resets)

    return {
        "known": True,
        "min_frac": min_frac,
        "weekly_5h": weekly_5h,
        "models_min_frac": models_min,
        "effective_frac": effective_frac,
        "zero_models": zero_models,
        "reset_ts": min(reset_candidates) if reset_candidates else None,
        "stale": stale,
        "group_name": str((chosen or {}).get("display_name") or (chosen or {}).get("name") or "all"),
    }


def prune_cooldowns(state, now=None):
    now = now if now is not None else time.time()
    cds = state.get("account_cooldowns")
    if not isinstance(cds, dict):
        state["account_cooldowns"] = {}
        return state["account_cooldowns"]
    alive = {}
    for email, info in cds.items():
        if not isinstance(info, dict):
            continue
        until = info.get("until") or 0
        if until > now:
            alive[email.lower()] = info
    state["account_cooldowns"] = alive
    return alive


def set_account_cooldown(state, email, until_ts, reason, now=None):
    if not email:
        return
    now = now if now is not None else time.time()
    cds = prune_cooldowns(state, now=now)
    key = email.strip().lower()
    prev = cds.get(key) or {}
    until = max(int(until_ts), int(prev.get("until") or 0))
    cds[key] = {"until": until, "reason": str(reason), "set_at": int(now)}


def quarantine_left(state_or_cds, email, now=None):
    if not email:
        return 0
    now = now if now is not None else time.time()
    cds = state_or_cds.get("account_cooldowns", state_or_cds) if isinstance(state_or_cds, dict) else {}
    if not isinstance(cds, dict):
        return 0
    info = cds.get(email.strip().lower())
    if not isinstance(info, dict):
        return 0
    return max(0, int((info.get("until") or 0) - now))


def _above_threshold(eff, threshold):
    if eff is None:
        return False
    if threshold <= 0:
        return eff >= 0.0
    return eff > threshold


def pick_healthy(threshold, exclude_email=None, now=None, group="gemini", cooldowns=None, allow_fallback=False):
    now = now if now is not None else time.time()
    cds = cooldowns if isinstance(cooldowns, dict) else {}
    excl = (exclude_email or "").strip().lower()
    table = []
    for acct in list_accounts():
        if not usable(acct):
            continue
        score = quota_score(acct, now, group)
        table.append((acct, score))

    def rank(item):
        acct, s = item
        q_left = quarantine_left(cds, acct.get("email"), now)
        quarantined = 1 if q_left > 0 else 0
        if not s["known"]:
            return (quarantined, 2, 1, 0.0, 0.0)
        below = 0 if _above_threshold(s["effective_frac"], threshold) else 1
        fresh = 0 if not s["stale"] else 1
        eff = s["effective_frac"] if s["effective_frac"] is not None else 0.0
        w5 = s["weekly_5h"] if s["weekly_5h"] is not None else 0.0
        return (quarantined, below, fresh, -eff, -w5)

    table.sort(key=rank)
    for acct, s in table:
        em = (acct.get("email") or "").lower()
        if excl and em == excl:
            continue
        if quarantine_left(cds, em, now) > 0:
            continue
        if s["known"] and _above_threshold(s["effective_frac"], threshold):
            return acct, s, table

    if allow_fallback:
        for acct, s in table:
            em = (acct.get("email") or "").lower()
            if excl and em == excl:
                continue
            if quarantine_left(cds, em, now) > 0:
                continue
            return acct, s, table

    return None, None, table


def describe_table(table, desktop_email, cli_email, cooldowns=None, now=None):
    now = now if now is not None else time.time()
    cds = cooldowns if isinstance(cooldowns, dict) else {}
    d_low = (desktop_email or "").strip().lower()
    c_low = (cli_email or "").strip().lower()
    rows = []
    for acct, s in table:
        em = acct.get("email") or ""
        em_low = em.lower()
        rows.append({
            "email": em,
            "is_desktop_account": bool(d_low) and em_low == d_low,
            "is_cli_account": bool(c_low) and em_low == c_low,
            "weekly_5h": None if s["weekly_5h"] is None else round(s["weekly_5h"], 4),
            "min_frac": None if s["min_frac"] is None else round(s["min_frac"], 4),
            "models_min_frac": None if s["models_min_frac"] is None else round(s["models_min_frac"], 4),
            "effective_frac": None if s["effective_frac"] is None else round(s["effective_frac"], 4),
            "zero_models": list(s.get("zero_models") or []),
            "quarantine_left_s": quarantine_left(cds, em_low, now),
            "stale": s["stale"],
            "group": s.get("group_name"),
        })
    return rows
