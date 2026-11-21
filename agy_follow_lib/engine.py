# -*- coding: utf-8 -*-
"""Unified Desktop + CLI pool engine, legacy modes, watch daemon, and CLI entrypoint."""

import argparse
import copy
import errno
import fcntl
import json
import os
import signal
import sys
import time
import traceback

from .config import (
    DEFAULT_QUARANTINE_SECONDS,
    MAX_QUARANTINE_SECONDS,
    MIN_QUARANTINE_SECONDS,
    MIN_SWITCH_INTERVAL_ERROR,
    MIN_SWITCH_INTERVAL_THRESHOLD,
    acquire_lock,
    get_paths,
    is_account_allowed,
    load_state,
    log,
    read_json,
    redact_text,
    save_state,
)
from .monitor import is_desktop_busy, scan_logs_for_quota_errors
from .quota import (
    describe_table,
    list_accounts,
    pick_healthy,
    prune_cooldowns,
    quarantine_left,
    quota_score,
    set_account_cooldown,
    tools_active_account,
    usable,
)
from .switcher import (
    cli_identity,
    reap_orphan_sidecars,
    switch_desktop_and_cli,
    sync_cli_only,
)


def _quarantine_secs_from_score(score, now):
    rts = score.get("reset_ts") if isinstance(score, dict) else None
    if rts and now < rts <= (now + MAX_QUARANTINE_SECONDS):
        return max(MIN_QUARANTINE_SECONDS, int(rts - now))
    return DEFAULT_QUARANTINE_SECONDS


def run_jit(args):
    with acquire_lock(nonblocking=True) as acquired:
        if not acquired:
            return 0
        desktop_acct, _, _ = tools_active_account("classic")
        if desktop_acct is None:
            return 0
        cli_email, _ = cli_identity()
        want_email = desktop_acct.get("email") or ""
        if (cli_email or "").strip().lower() != want_email.strip().lower():
            res = sync_cli_only(
                desktop_acct,
                no_reload=args.no_reload,
                sync_cursor_flag=True,
                force=False,
            )
            log(
                "JIT_SYNC %s -> %s (hub_reload=%s)"
                % (cli_email, want_email, "yes" if res.get("hub_reload") else "no")
            )
        return 0


def evaluate_pool_once(args, state, now=None, dry_run=False, init_log_eof=True, pre_hits=None):
    now = now if now is not None else time.time()
    source = args.source or "classic"
    desktop_acct, desktop_reason, desktop_meta = tools_active_account(source)
    desktop_email = desktop_acct.get("email") if desktop_acct else desktop_meta.get("raw_email")
    cli_email, cli_src = cli_identity()
    cds = prune_cooldowns(state, now=now)
    pool = list_accounts()
    by_email = {(a.get("email") or "").lower(): a for a in pool}
    schema_drift = (desktop_meta.get("reason_code") == "missing_field") and bool(pool)

    scan_state = copy.deepcopy(state) if dry_run else state
    error_hits = (
        pre_hits
        if pre_hits is not None
        else scan_logs_for_quota_errors(scan_state, now=now, init_to_eof=init_log_eof)
    )

    d_acct_for_score = desktop_acct or by_email.get((desktop_email or "").lower())
    d_score = quota_score(d_acct_for_score, now=now, group=args.group) if d_acct_for_score else {
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
    _p, _ps, full_table = pick_healthy(
        args.threshold,
        exclude_email=None,
        now=now,
        group=args.group,
        cooldowns=cds,
    )

    decision = "pool_keep"
    target_acct = desktop_acct
    note = ""
    action = "none"
    sync_cursor_flag = bool(args.sync_cursor or args.mode == "pool")

    last_ts = state.get("last_switch_ts") or 0
    last_sw = state.get("last_switch") or {}

    if error_hits:
        desktop_hits = [h for h in error_hits if h.get("source") == "desktop"]
        hit = desktop_hits[-1] if desktop_hits else error_hits[-1]
        affected = desktop_email if hit.get("source") == "desktop" else (cli_email or desktop_email)
        q_secs = hit.get("reset_secs") or DEFAULT_QUARANTINE_SECONDS
        q_secs = max(MIN_QUARANTINE_SECONDS, min(MAX_QUARANTINE_SECONDS, int(q_secs)))
        if not dry_run and affected:
            set_account_cooldown(state, affected, now + q_secs, "quota_error_429", now=now)

        from_last = (last_sw.get("from") or "").lower()
        aff_low = (affected or "").lower()
        buffered_old = bool(aff_low and aff_low == from_last and (now - last_ts) < MIN_SWITCH_INTERVAL_ERROR)
        if buffered_old:
            decision = "error_cooldown"
            note = "刚切换过的旧账号缓冲日志，忽略重复切换"
            if not dry_run:
                save_state(state)
        else:
            picked, _, _ = pick_healthy(
                args.threshold,
                exclude_email=affected,
                now=now,
                group=args.group,
                cooldowns=cds,
                allow_fallback=True,
            )
            if picked is not None:
                target_acct = picked
                decision = "quota_error_429_switch"
                note = hit.get("line_summary") or "检测到 429 额度错误"
                if not dry_run:
                    ok, _res = switch_desktop_and_cli(
                        picked,
                        state,
                        "quota_error_429",
                        desktop_email,
                        now=now,
                        cli_only=args.cli_only,
                        no_reload=args.no_reload,
                        sync_cursor_flag=sync_cursor_flag,
                        notify=not args.no_notify,
                        force=args.force,
                    )
                    action = "switched" if ok else "error"
                    if not ok:
                        decision = "switch_failed"
            else:
                decision = "pool_exhausted"
                note = "检测到 429 额度错误，但池中无其他未隔离健康账号"
                if not dry_run:
                    state.pop("pending_switch", None)
                    save_state(state)
    elif schema_drift:
        decision = "schema_drift_hold"
        note = desktop_reason or "accounts.json 结构异常，暂不切换"
    else:
        d_quarantined = quarantine_left(cds, desktop_email, now) > 0
        d_unusable = (desktop_acct is None) or (not usable(desktop_acct))
        d_eff = d_score.get("effective_frac")
        d_models_min = d_score.get("models_min_frac")
        d_below = bool(d_score["known"] and d_eff is not None and d_eff <= args.threshold)

        if d_unusable or d_quarantined or d_below:
            urgent = (
                d_unusable
                or d_quarantined
                or bool(
                    d_score["known"]
                    and (
                        (d_eff is not None and d_eff <= args.urgent_threshold)
                        or (d_models_min is not None and d_models_min <= 0.0)
                    )
                )
            )
            picked, picked_score, _ = pick_healthy(
                args.threshold,
                exclude_email=desktop_email,
                now=now,
                group=args.group,
                cooldowns=cds,
                allow_fallback=urgent,
            )
            if (
                picked is not None
                and (not d_unusable)
                and (not d_quarantined)
                and picked_score["known"]
                and d_score["known"]
                and (picked_score.get("effective_frac") or 0.0) <= (d_eff or 0.0)
                and (picked_score.get("effective_frac") or 0.0) <= args.threshold
            ):
                picked = None

            if picked is None:
                decision = "pool_exhausted"
                note = desktop_reason or "当前账号额度低于阈值，但池中无更健康账号"
                if not dry_run:
                    state.pop("pending_switch", None)
                    save_state(state)
            elif (not d_unusable) and (now - last_ts) < MIN_SWITCH_INTERVAL_THRESHOLD:
                decision = "threshold_cooldown"
                note = "触发阈值但处于 %ds 防抖冷却期" % MIN_SWITCH_INTERVAL_THRESHOLD
                if not dry_run:
                    save_state(state)
            else:
                deferred = False
                trigger = "threshold_urgent" if urgent else "threshold_idle"
                if (not urgent) and (not args.cli_only):
                    busy, busy_reason = is_desktop_busy(idle_seconds=args.idle_seconds, now=now)
                    if busy:
                        pend = state.get("pending_switch") or {}
                        first_ts = int(pend.get("first_seen_ts") or now)
                        if (now - first_ts) < args.idle_max_wait:
                            deferred = True
                            decision = "deferred_busy"
                            note = "客户端正在生成中(%s)，等待空闲(已等 %ds/%ds)" % (
                                busy_reason,
                                int(now - first_ts),
                                int(args.idle_max_wait),
                            )
                            if not dry_run:
                                state["pending_switch"] = {
                                    "first_seen_ts": first_ts,
                                    "target": picked.get("email"),
                                    "reason": busy_reason,
                                }
                                save_state(state)
                        else:
                            trigger = "threshold_max_wait"

                if not deferred:
                    target_acct = picked
                    decision = trigger
                    if not dry_run:
                        if desktop_email:
                            q_s = _quarantine_secs_from_score(d_score, now)
                            set_account_cooldown(state, desktop_email, now + q_s, trigger, now=now)
                        ok, _res = switch_desktop_and_cli(
                            picked,
                            state,
                            trigger,
                            desktop_email,
                            now=now,
                            cli_only=args.cli_only,
                            no_reload=args.no_reload,
                            sync_cursor_flag=sync_cursor_flag,
                            notify=not args.no_notify,
                            force=args.force,
                        )
                        action = "switched" if ok else "error"
                        if not ok:
                            decision = "switch_failed"
        else:
            in_sync_cli = (
                bool(cli_email)
                and bool(desktop_email)
                and cli_email.strip().lower() == desktop_email.strip().lower()
            )
            if desktop_acct is not None and not in_sync_cli:
                decision = "sync_cli_to_desktop"
                target_acct = desktop_acct
                note = "桌面端健康，仅对齐 CLI 凭据（不重启桌面端）"
                if not dry_run:
                    state.pop("pending_switch", None)
                    sync_cli_only(
                        desktop_acct,
                        no_reload=args.no_reload,
                        sync_cursor_flag=sync_cursor_flag,
                        force=args.force,
                    )
                    save_state(state)
                    log("CLI_SYNC_TO_DESKTOP %s -> %s" % (cli_email, desktop_email))
                    action = "cli_synced"
            else:
                decision = "pool_keep"
                target_acct = desktop_acct
                if not dry_run:
                    state.pop("pending_switch", None)
                    if args.force and desktop_acct is not None:
                        sync_cli_only(
                            desktop_acct,
                            no_reload=args.no_reload,
                            sync_cursor_flag=sync_cursor_flag,
                            force=True,
                        )
                        action = "forced"
                    save_state(state)

    want_email = target_acct.get("email") if target_acct else None
    in_sync = bool(
        desktop_email
        and cli_email
        and want_email
        and desktop_email.strip().lower() == cli_email.strip().lower() == want_email.strip().lower()
    )
    status = {
        "ok": True,
        "mode": args.mode,
        "decision": decision,
        "action": action,
        "desktop_account": desktop_email,
        "desktop_reason": desktop_reason or None,
        "desktop_reason_code": desktop_meta.get("reason_code"),
        "cli_account": cli_email,
        "cli_source": cli_src,
        "target_account": want_email,
        "in_sync": in_sync,
        "threshold": args.threshold,
        "urgent_threshold": args.urgent_threshold,
        "quota_group": args.group,
        "desktop_quota": {
            "weekly_5h": None if d_score["weekly_5h"] is None else round(d_score["weekly_5h"], 4),
            "models_min_frac": None if d_score["models_min_frac"] is None else round(d_score["models_min_frac"], 4),
            "effective_frac": None if d_score["effective_frac"] is None else round(d_score["effective_frac"], 4),
            "zero_models": d_score.get("zero_models") or [],
            "stale": d_score["stale"],
        },
        "cooldowns": cds,
        "pending_switch": state.get("pending_switch"),
        "pool": describe_table(full_table, desktop_email, cli_email, cooldowns=cds, now=now),
        "note": note or None,
    }
    if schema_drift:
        status["warning"] = "schema_drift"
    return status


def _evaluate_legacy(args, state, now, dry_run=False):
    source = args.source or "auto"
    follow_acct, reason, meta = tools_active_account(source)
    have_email, have_src = cli_identity()
    pool = list_accounts()
    by_email = {(a.get("email") or "").lower(): a for a in pool}
    cur_acct = by_email.get((have_email or "").lower())
    cur_score = quota_score(cur_acct, now, args.group) if cur_acct else {"known": False, "effective_frac": None, "weekly_5h": None, "models_min_frac": None, "stale": True}
    tgt_score = quota_score(follow_acct, now, args.group) if follow_acct else {"known": False, "effective_frac": None, "weekly_5h": None, "models_min_frac": None, "stale": True}
    schema_drift = (meta.get("reason_code") == "missing_field") and bool(pool)
    cds = prune_cooldowns(state, now=now)
    _p, _ps, table = pick_healthy(args.threshold, exclude_email=None, now=now, group=args.group, cooldowns=cds)

    def depleted(score, acct):
        if acct is None or not usable(acct):
            return True
        if not score["known"]:
            return False
        eff = score.get("effective_frac")
        return eff is not None and eff <= args.threshold

    desired, decision, note = None, None, None
    if args.mode == "follow":
        desired, decision = follow_acct, "follow_tools"
    elif args.mode == "quota":
        if depleted(cur_score, cur_acct):
            picked, _, _ = pick_healthy(args.threshold, exclude_email=have_email, now=now, group=args.group, cooldowns=cds)
            if picked is not None:
                desired, decision = picked, "quota_switch"
            else:
                desired, decision = cur_acct or follow_acct, "quota_hold"
                note = "当前账号额度不足，但池子里没有更健康的账号"
        else:
            desired, decision = cur_acct, "quota_keep"
    else:  # hybrid
        if follow_acct is not None and not depleted(tgt_score, follow_acct):
            desired, decision = follow_acct, "follow_tools"
        elif cur_acct is not None and not depleted(cur_score, cur_acct):
            desired, decision = cur_acct, "keep_healthy_cli"
            note = "Tools 目标账号不可用或额度耗尽，保持 CLI 现有健康账号"
        else:
            picked, _, _ = pick_healthy(args.threshold, exclude_email=have_email, now=now, group=args.group, cooldowns=cds)
            desired = picked or cur_acct or follow_acct
            decision = "quota_switch" if picked else ("quota_hold" if desired else None)

    if desired is None:
        return {"ok": False, "reason": reason, "meta": meta, "cli_account": have_email}, 3

    want_email = desired.get("email")
    in_sync = bool(have_email) and have_email.strip().lower() == (want_email or "").strip().lower()
    if not dry_run and (not in_sync or args.force):
        sync_cli_only(desired, no_reload=args.no_reload, sync_cursor_flag=args.sync_cursor, force=args.force)
        state["last_switch_ts"] = int(now)
        state["last_switch"] = {"from": have_email, "to": want_email, "decision": decision}
        save_state(state)

    return {
        "ok": True,
        "mode": args.mode,
        "decision": decision,
        "desktop_account": follow_acct.get("email") if follow_acct else meta.get("raw_email"),
        "cli_account": have_email,
        "cli_source": have_src,
        "target_account": want_email,
        "in_sync": in_sync,
        "threshold": args.threshold,
        "quota_group": args.group,
        "warning": "schema_drift" if schema_drift else None,
        "pool": describe_table(table, meta.get("raw_email"), have_email, cooldowns=cds, now=now),
        "note": note,
    }, (0 if in_sync else 2)


def _print_check(status):
    print("模式           : %s" % status.get("mode"))
    print("桌面端当前账号 : %s%s" % (
        status.get("desktop_account"),
        ("  (%s)" % status["desktop_reason"]) if status.get("desktop_reason") else "",
    ))
    print("CLI   当前账号 : %s  (来源 %s)" % (status.get("cli_account"), status.get("cli_source")))
    dq = status.get("desktop_quota")
    if dq:
        print("桌面端额度     : weekly_5h=%s models_min=%s effective=%s zero=%s" % (
            dq.get("weekly_5h"), dq.get("models_min_frac"), dq.get("effective_frac"), dq.get("zero_models"),
        ))
    pool_rows = status.get("pool") or []
    if pool_rows:
        print("额度池 (%s)    : 阈值=%.0f%%" % (status.get("quota_group"), (status.get("threshold") or 0) * 100))
        for row in pool_rows:
            marks = []
            if row.get("is_desktop_account"):
                marks.append("桌面端")
            if row.get("is_cli_account"):
                marks.append("CLI")
            if row.get("quarantine_left_s"):
                marks.append("隔离%ds" % row["quarantine_left_s"])
            tag = ("  ← " + "/".join(marks)) if marks else ""
            print("  %-28s w5h=%-7s mod=%-7s eff=%-7s%s" % (
                row.get("email"), row.get("weekly_5h"), row.get("models_min_frac"), row.get("effective_frac"), tag,
            ))
    if status.get("warning") == "schema_drift":
        print("⚠ 读不到 accounts.json 当前账号字段，保持凭据不动")
    print("决策           : %s -> %s" % (status.get("decision"), status.get("target_account")))
    print("状态           : %s" % ("一致 ✅" if status.get("in_sync") else "不一致 ❌ 需要同步"))
    if status.get("note"):
        print("备注           : %s" % status["note"])


def _handle_switch_to(args):
    email = (args.switch_to or "").strip().lower()
    if not is_account_allowed(email):
        print("ERROR Account excluded or not allowed by policy: %s" % email, file=sys.stderr)
        return 4
    by_email = {(a.get("email") or "").lower(): a for a in list_accounts()}
    acct = by_email.get(email)
    if not acct or not usable(acct):
        print("ERROR 账号不存在或不可用: %s" % email, file=sys.stderr)
        return 3
    with acquire_lock(nonblocking=False):
        state = load_state()
        _, _, meta = tools_active_account("classic")
        from_email = meta.get("raw_email")
        ok, res = switch_desktop_and_cli(
            acct,
            state,
            "manual_switch_to",
            from_email,
            now=time.time(),
            cli_only=args.cli_only,
            no_reload=args.no_reload,
            sync_cursor_flag=True,
            notify=not args.no_notify,
            force=True,
        )
        if not ok:
            print(json.dumps({"ok": False, "error": res.get("error")}, ensure_ascii=False))
            return 4
        if args.json:
            print(json.dumps({"ok": True, "switched_to": acct.get("email"), "cli_only": args.cli_only}, ensure_ascii=False))
        else:
            print("已切换双端账号至: %s (cli_only=%s)" % (acct.get("email"), args.cli_only))
        return 0


def run_watch(args):
    paths = get_paths()
    watch_lock_path = paths["LOCK_PATH"] + ".watch"
    os.makedirs(os.path.dirname(watch_lock_path), exist_ok=True)
    wfd = os.open(watch_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(wfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(wfd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            log("WARN 已有 watch 实例在运行，退出当前重复实例")
            return 0
        raise

    flags = {"stop": False, "wake": False}

    def _on_stop(_signum, _frame):
        flags["stop"] = True

    def _on_wake(_signum, _frame):
        flags["wake"] = True

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    signal.signal(signal.SIGUSR1, _on_wake)

    log("WATCH 启动 (mode=%s, threshold=%.2f, poll=%.1fs, quota_poll=%.1fs)" % (
        args.mode, args.threshold, args.poll_seconds, args.quota_poll_seconds,
    ))

    with acquire_lock(nonblocking=False):
        st = load_state()
        scan_logs_for_quota_errors(st, now=time.time(), init_to_eof=True)
        evaluate_pool_once(args, st, now=time.time(), dry_run=False, init_log_eof=True, pre_hits=[])

    last_full_ts = time.time()
    try:
        last_idx_mtime = os.stat(paths["ACCOUNTS_INDEX"]).st_mtime
    except OSError:
        last_idx_mtime = 0.0

    try:
        while not flags["stop"]:
            slept = 0.0
            while slept < args.poll_seconds and not flags["stop"] and not flags["wake"]:
                step = min(0.5, max(0.05, args.poll_seconds - slept))
                time.sleep(step)
                slept += step
            if flags["stop"]:
                break

            try:
                now = time.time()
                try:
                    cur_mtime = os.stat(paths["ACCOUNTS_INDEX"]).st_mtime
                except OSError:
                    cur_mtime = last_idx_mtime
                mtime_changed = cur_mtime != last_idx_mtime
                last_idx_mtime = cur_mtime

                with acquire_lock(nonblocking=False):
                    state = load_state()
                    hits = scan_logs_for_quota_errors(state, now=now, init_to_eof=True)
                    due_full = (now - last_full_ts) >= args.quota_poll_seconds
                    need_eval = bool(hits) or flags["wake"] or mtime_changed or bool(state.get("pending_switch")) or due_full
                    if need_eval:
                        evaluate_pool_once(args, state, now=now, dry_run=False, init_log_eof=True, pre_hits=hits)
                        last_full_ts = now
                        flags["wake"] = False
                    else:
                        save_state(state)
            except Exception as exc:
                log("ERROR watch 循环异常: %s | %s" % (redact_text(exc), redact_text(traceback.format_exc(limit=2))))
    finally:
        try:
            fcntl.flock(wfd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(wfd)
        log("WATCH 退出")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Antigravity 客户端 + CLI 统一账号池自动切换守护")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只读检查当前双端状态与额度池")
    g.add_argument("--apply", action="store_true", help="执行一次检查并在需要时切换/同步")
    g.add_argument("--force", action="store_true", help="强制重写 CLI 凭据文件")
    g.add_argument("--watch", action="store_true", help="常驻守护监听 429 日志与额度变化")
    g.add_argument("--switch-to", metavar="EMAIL", help="手动双端切换到指定邮箱账号")
    g.add_argument("--clear-cooldowns", action="store_true", help="清空单账号隔离冷却记录")
    ap.add_argument("--mode", choices=["pool", "follow", "quota", "hybrid"], default="pool")
    ap.add_argument("--source", choices=["classic", "auto", "agy-binding", "target"], default=None)
    ap.add_argument("--group", default="gemini", help="监控额度分组（默认 gemini）")
    ap.add_argument("--threshold", type=float, default=0.15, help="百分比切换阈值（默认 0.15）")
    ap.add_argument("--urgent-threshold", type=float, default=0.05, help="紧急无视忙碌阈值（默认 0.05）")
    ap.add_argument("--poll-seconds", type=float, default=3.0, help="watch 模式日志轮询周期（秒）")
    ap.add_argument("--quota-poll-seconds", type=float, default=30.0, help="watch 模式全量额度巡检周期（秒）")
    ap.add_argument("--idle-seconds", type=float, default=15.0, help="判断桌面端生成空闲的静默秒数")
    ap.add_argument("--idle-max-wait", type=float, default=120.0, help="等待桌面端空闲最长秒数")
    ap.add_argument("--sync-cursor", action="store_true", help="同步 current_agy_account_id 游标")
    ap.add_argument("--no-reload", action="store_true", help="不触发 hub 重载标记")
    ap.add_argument("--no-notify", action="store_true", help="不发送 macOS 通知")
    ap.add_argument("--cli-only", action="store_true", help="只切 CLI，不调用 antigravity-tools 切桌面端")
    ap.add_argument("--jit", action="store_true", help="快速 JIT 路径：仅非阻塞同步 CLI 与桌面端一致")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.jit:
        return run_jit(args)

    if args.clear_cooldowns:
        with acquire_lock(nonblocking=False):
            state = load_state()
            state["account_cooldowns"] = {}
            state.pop("pending_switch", None)
            save_state(state)
        print("已清空所有账号隔离冷却记录")
        return 0

    if args.switch_to:
        return _handle_switch_to(args)

    if args.watch:
        return run_watch(args)

    dry_run = args.check or not (args.apply or args.force)
    if args.mode != "pool":
        now = time.time()
        if dry_run:
            status, rc = _evaluate_legacy(args, load_state(), now, dry_run=True)
        else:
            with acquire_lock(nonblocking=False):
                reap_orphan_sidecars()
                status, rc = _evaluate_legacy(args, load_state(), now, dry_run=False)
                if status.get("ok"):
                    rc = 0
        if not status.get("ok"):
            log("ABORT 找不到可用账号：%s" % status.get("reason"))
            if args.json:
                print(json.dumps(status, ensure_ascii=False))
            return rc
        if args.json:
            print(json.dumps(status, ensure_ascii=False))
        elif dry_run:
            _print_check(status)
        return rc

    if dry_run:
        status = evaluate_pool_once(args, load_state(), now=time.time(), dry_run=True, init_log_eof=True)
        if args.json:
            print(json.dumps(status, ensure_ascii=False))
        else:
            _print_check(status)
        return 0 if status.get("in_sync") else 2

    with acquire_lock(nonblocking=False):
        reap_orphan_sidecars()
        state = load_state()
        status = evaluate_pool_once(args, state, now=time.time(), dry_run=False, init_log_eof=True)
    if args.json:
        print(json.dumps(status, ensure_ascii=False))
    return 0 if status.get("action") != "error" else 4
