# -*- coding: utf-8 -*-
"""Sanitized unit tests for agy-account-pool (agy-account-follow)."""

import base64
import datetime as _dt
import fcntl
import json
import os
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agy_follow_lib import config, engine, monitor, quota, switcher  # noqa: E402


def _make_jwt(email):
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    pay = base64.urlsafe_b64encode(json.dumps({"email": email}).encode("utf-8")).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (hdr, pay)


def _acct_payload(aid, email, w5h=1.0, weekly=1.0, gem_pct=100, claude_pct=100, now=None):
    now = now or int(time.time())
    return {
        "id": aid,
        "email": email,
        "disabled": False,
        "banned": False,
        "validation_blocked": False,
        "token": {
            "access_token": "tok-access-" + aid,
            "refresh_token": "tok-refresh-" + aid,
            "id_token": _make_jwt(email),
            "expiry_timestamp": now + 3600,
        },
        "quota": {
            "is_forbidden": False,
            "last_updated": now,
            "quota_groups": [
                {
                    "display_name": "Gemini Models",
                    "buckets": [
                        {"window": "weekly", "remaining_fraction": weekly, "observed_at": now * 1000, "reset_time": "2026-11-28T10:00:00Z"},
                        {"window": "5h", "remaining_fraction": w5h, "observed_at": now * 1000, "reset_time": "2026-11-22T01:00:00Z"},
                    ],
                },
                {
                    "display_name": "Claude and GPT models",
                    "buckets": [{"window": "weekly", "remaining_fraction": claude_pct / 100.0, "observed_at": now * 1000}],
                },
                {
                    "display_name": "Gemini 4 Argon",
                    "buckets": [{"window": "weekly", "remaining_fraction": 1.0, "observed_at": now * 1000}],
                },
            ],
            "models": [
                {"name": "gemini-4-hydrogen-high", "percentage": gem_pct, "reset_time": "2026-11-22T01:00:00Z"},
                {"name": "gemini-3.8-flash-medium", "percentage": gem_pct, "reset_time": "2026-11-22T01:00:00Z"},
                {"name": "gemini-4-argon-high", "percentage": 100},
                {"name": "claude-sonnet-5-5-high", "percentage": claude_pct},
            ],
        },
    }


class TestAgyAccountPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.tools = os.path.join(self.root, "tools")
        self.gemini = os.path.join(self.root, "gemini")
        self.cli_logs = os.path.join(self.gemini, "antigravity-cli", "log")
        self.desktop_log = os.path.join(self.root, "language_server.log")
        self.log_path = os.path.join(self.root, "follow.log")
        self.lock_path = os.path.join(self.root, "follow.lock")
        self.state_path = os.path.join(self.root, "state.json")
        self.cfg_path = os.path.join(self.root, "config.json")
        self.at_bin = os.path.join(self.root, "mock-at")
        self.at_calls = os.path.join(self.root, "at_calls.log")
        os.makedirs(os.path.join(self.tools, "accounts"), exist_ok=True)
        os.makedirs(self.cli_logs, exist_ok=True)

        self._write_mock_at()
        self._orig_env = dict(os.environ)
        os.environ.update({
            "AGY_FOLLOW_TOOLS_DIR": self.tools,
            "AGY_FOLLOW_GEMINI_DIR": self.gemini,
            "AGY_DESKTOP_LS_LOG": self.desktop_log,
            "AGY_CLI_LOG_DIR": self.cli_logs,
            "AGY_FOLLOW_LOG_PATH": self.log_path,
            "AGY_AT_BIN": self.at_bin,
            "AGY_LOCK_PATH": self.lock_path,
            "AGY_STATE_PATH": self.state_path,
            "AGY_FOLLOW_CONFIG": self.cfg_path,
            "AGY_NO_NOTIFY": "1",
            "AGY_ASSUME_DESKTOP_RUNNING": "1",
            "AGY_FOLLOW_QUIET": "1",
        })
        os.environ.pop("AGY_ALLOWED_ACCOUNTS", None)
        os.environ.pop("AGY_EXCLUDED_ACCOUNTS", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self.tmp.cleanup()

    def _write_mock_at(self):
        script = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"CALLS = {self.at_calls!r}\n"
            f"TOOLS = {self.tools!r}\n"
            "with open(CALLS, 'a', encoding='utf-8') as fh: fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "email = sys.argv[2]\n"
            "idx_path = os.path.join(TOOLS, 'accounts.json')\n"
            "idx = json.load(open(idx_path, encoding='utf-8'))\n"
            "for fn in os.listdir(os.path.join(TOOLS, 'accounts')):\n"
            "    d = json.load(open(os.path.join(TOOLS, 'accounts', fn), encoding='utf-8'))\n"
            "    if d.get('email') == email:\n"
            "        idx['current_classic_account_id'] = d['id']\n"
            "        idx['current_account_id'] = d['id']\n"
            "        json.dump(idx, open(idx_path, 'w', encoding='utf-8'))\n"
            "        sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        with open(self.at_bin, "w", encoding="utf-8") as fh:
            fh.write(script)
        os.chmod(self.at_bin, 0o755)

    def _seed_pool(self, desktop_id="id-1", cli_acct=None, extra_accts=None):
        now = int(time.time())
        accts = [
            _acct_payload("id-1", "acct1@example.com", w5h=1.0, gem_pct=100, now=now),
            _acct_payload("id-2", "acct2@example.com", w5h=0.95, gem_pct=95, now=now),
            _acct_payload("id-3", "acct3@example.com", w5h=0.99, gem_pct=99, claude_pct=0, now=now),
            _acct_payload("id-4", "acct4@example.com", w5h=0.0607, weekly=0.7284, gem_pct=0, claude_pct=45, now=now),
        ]
        if extra_accts:
            accts.extend(extra_accts)
        for a in accts:
            config.atomic_write_json(os.path.join(self.tools, "accounts", "%s.json" % a["id"]), a)
        config.atomic_write_json(
            os.path.join(self.tools, "accounts.json"),
            {
                "current_account_id": desktop_id,
                "current_classic_account_id": desktop_id,
                "current_agy_account_id": (cli_acct["id"] if cli_acct else desktop_id),
                "current_target_ide": "classic",
            },
        )
        if cli_acct:
            switcher.sync_cli_only(cli_acct, no_reload=True, sync_cursor_flag=False, force=True)
        return {a["email"]: a for a in accts}

    def _args(self, **overrides):
        base = {
            "mode": "pool", "source": "classic", "group": "gemini", "threshold": 0.15,
            "urgent_threshold": 0.05, "idle_seconds": 15.0, "idle_max_wait": 120.0,
            "sync_cursor": True, "no_reload": False, "no_notify": True,
            "cli_only": False, "force": False,
        }
        base.update(overrides)
        return engine.argparse.Namespace(**base)

    def test_1_policy_filtering_and_no_false_schema_drift(self):
        # Default (no config file): any valid account is allowed
        self.assertTrue(config.is_account_allowed("anyone@example.com"))

        # Blacklist via config.json
        personal = _acct_payload("id-p", "personal@example.com")
        self._seed_pool(desktop_id="id-p", extra_accts=[personal])
        config.atomic_write_json(self.cfg_path, {"allowed_accounts": [], "excluded_accounts": ["personal@example.com"]})
        self.assertFalse(config.is_account_allowed("personal@example.com"))
        self.assertTrue(config.is_account_allowed("acct1@example.com"))

        acct, _, meta = quota.tools_active_account("classic")
        self.assertIsNone(acct)
        self.assertEqual(meta["reason_code"], "whitelist_blocked")
        st = engine.evaluate_pool_once(self._args(), {}, now=time.time(), dry_run=True)
        self.assertNotIn("warning", st)

        # Explicit allowlist via env
        os.environ["AGY_ALLOWED_ACCOUNTS"] = "acct1@example.com,acct2@example.com"
        self.assertTrue(config.is_account_allowed("acct2@example.com"))
        self.assertFalse(config.is_account_allowed("acct3@example.com"))

    def test_2_dual_layer_quota_scoring(self):
        accts = self._seed_pool()
        gs = quota.quota_score(accts["acct4@example.com"], group="gemini")
        self.assertEqual(gs["effective_frac"], 0.0)
        self.assertIn("gemini-4-hydrogen-high", gs["zero_models"])
        ss = quota.quota_score(accts["acct3@example.com"], group="gemini")
        self.assertAlmostEqual(ss["effective_frac"], 0.99, places=2)

    def test_3_quota_error_watcher_and_ignores(self):
        self.assertEqual(monitor.parse_reset_duration("Resets in 1h16m57s."), 4617)
        with open(self.desktop_log, "w", encoding="utf-8") as fh:
            fh.write("E1121 10:00:00 1 errorreport.go:285] agent executor error: RESOURCE_EXHAUSTED (code 429): Resets in 10m\n")
        state = {}
        self.assertEqual(monitor.scan_logs_for_quota_errors(state, init_to_eof=True), [])

        with open(self.desktop_log, "a", encoding="utf-8") as fh:
            fh.write("I1121 10:01:00 1 runner.go:956] Monitor sir_flash_lite FINISHED: reason=\"RESOURCE_EXHAUSTED (code 429)\"\n")
            fh.write("I1121 10:01:01 1 llm_monitor.go:700] UNAVAILABLE (code 503): No capacity available\n")
            fh.write("E1121 10:01:02 1 errorreport.go:285] dial tcp: lookup oauth2.googleapis.com: no such host\n")
            fh.write("E1121 10:01:05 1 errorreport.go:285] agent executor error: generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h16m57s.\n")
        hits = monitor.scan_logs_for_quota_errors(state, init_to_eof=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reset_secs"], 4617)

        with open(self.desktop_log, "w", encoding="utf-8") as fh:
            fh.write("E1121 10:02:00 1 error_parsing.go:87] RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 5m\n")
        hits2 = monitor.scan_logs_for_quota_errors(state, init_to_eof=True)
        self.assertEqual(len(hits2), 1)
        self.assertEqual(hits2[0]["reset_secs"], 300)

    def test_4_idle_guard_defer_max_wait_and_urgent_bypass(self):
        now = time.time()
        low = _acct_payload("id-low", "low@example.com", w5h=0.12, gem_pct=12, now=int(now))
        accts = self._seed_pool(desktop_id="id-low", cli_acct=low, extra_accts=[low])
        ts_str = _dt.datetime.fromtimestamp(now).strftime("%m%d %H:%M:%S")
        with open(self.desktop_log, "w", encoding="utf-8") as fh:
            fh.write("I%s 1 http_helpers.go:709] streamGenerateContent?alt=sse\n" % ts_str)
        os.utime(self.desktop_log, (now, now))

        state = {}
        st1 = engine.evaluate_pool_once(self._args(), state, now=now, dry_run=False)
        self.assertEqual(st1["decision"], "deferred_busy")
        self.assertFalse(os.path.exists(self.at_calls))

        ts2 = _dt.datetime.fromtimestamp(now + 125).strftime("%m%d %H:%M:%S")
        with open(self.desktop_log, "a", encoding="utf-8") as fh:
            fh.write("I%s 1 http_helpers.go:709] streamGenerateContent\n" % ts2)
        os.utime(self.desktop_log, (now + 125, now + 125))
        st2 = engine.evaluate_pool_once(self._args(), state, now=now + 125, dry_run=False)
        self.assertEqual(st2["decision"], "threshold_max_wait")
        self.assertEqual(st2["target_account"], "acct1@example.com")

        self._seed_pool(desktop_id="id-4", cli_acct=accts["acct4@example.com"])
        st3 = engine.evaluate_pool_once(self._args(), {}, now=now + 200, dry_run=False)
        self.assertEqual(st3["decision"], "threshold_urgent")
        self.assertEqual(st3["target_account"], "acct1@example.com")

    def test_5_quarantine_and_pool_exhausted_no_thrash(self):
        now = time.time()
        a1 = _acct_payload("id-1", "acct1@example.com", w5h=0.10, gem_pct=10, now=int(now))
        a2 = _acct_payload("id-2", "acct2@example.com", w5h=0.12, gem_pct=12, now=int(now))
        for a in (a1, a2):
            config.atomic_write_json(os.path.join(self.tools, "accounts", "%s.json" % a["id"]), a)
        config.atomic_write_json(
            os.path.join(self.tools, "accounts.json"),
            {"current_account_id": "id-1", "current_classic_account_id": "id-1", "current_agy_account_id": "id-1"},
        )
        switcher.sync_cli_only(a1, no_reload=True, sync_cursor_flag=False, force=True)
        state = {"account_cooldowns": {"acct2@example.com": {"until": int(now) + 900, "reason": "quota_error_429"}}}
        st = engine.evaluate_pool_once(self._args(), state, now=now, dry_run=False)
        self.assertEqual(st["decision"], "pool_exhausted")
        self.assertFalse(os.path.exists(self.at_calls))

    def test_6_authoritative_desktop_drift_syncs_cli_only(self):
        accts = self._seed_pool(desktop_id="id-1", cli_acct=_acct_payload("id-2", "acct2@example.com"))
        self.assertEqual(switcher.cli_identity()[0], "acct2@example.com")
        st = engine.evaluate_pool_once(self._args(), {}, now=time.time(), dry_run=False)
        self.assertEqual(st["decision"], "sync_cli_to_desktop")
        self.assertEqual(switcher.cli_identity()[0], "acct1@example.com")
        self.assertFalse(os.path.exists(self.at_calls))
        idx = config.read_json(os.path.join(self.tools, "accounts.json"))
        self.assertEqual(idx["current_agy_account_id"], accts["acct1@example.com"]["id"])

    def test_7_dual_target_switch_on_429(self):
        accts = self._seed_pool(desktop_id="id-1")
        switcher.sync_cli_only(accts["acct1@example.com"], no_reload=True, sync_cursor_flag=False, force=True)
        with open(self.desktop_log, "w", encoding="utf-8") as fh:
            fh.write("init\n")
        state = {}
        monitor.scan_logs_for_quota_errors(state, init_to_eof=True)
        with open(self.desktop_log, "a", encoding="utf-8") as fh:
            fh.write("E1121 20:00:00 1 errorreport.go:285] agent executor error: RESOURCE_EXHAUSTED (code 429): Resets in 20m\n")
        st = engine.evaluate_pool_once(self._args(), state, now=time.time(), dry_run=False)
        self.assertEqual(st["decision"], "quota_error_429_switch")
        self.assertEqual(st["target_account"], "acct3@example.com")
        with open(self.at_calls, encoding="utf-8") as fh:
            calls = fh.read().strip().splitlines()
        self.assertEqual(calls, ["switch acct3@example.com --target classic"])
        self.assertEqual(switcher.cli_identity()[0], "acct3@example.com")
        idx = config.read_json(os.path.join(self.tools, "accounts.json"))
        self.assertEqual(idx["current_agy_account_id"], "id-3")
        self.assertTrue(os.path.exists(config.get_paths()["HUB_RELOAD"]))
        for p_key in ("OAUTH_CREDS", "GOOGLE_ACCOUNTS", "CLI_TOKEN", "JETSKI_TOKEN"):
            self.assertTrue(os.path.exists(config.get_paths()[p_key]))

    def test_8_jit_fast_sync_and_lock_skip(self):
        self._seed_pool(desktop_id="id-1", cli_acct=_acct_payload("id-2", "acct2@example.com"))
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc = engine.main(["--apply", "--mode", "pool", "--jit"])
            self.assertEqual(rc, 0)
            self.assertEqual(switcher.cli_identity()[0], "acct2@example.com")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        rc2 = engine.main(["--apply", "--mode", "pool", "--jit"])
        self.assertEqual(rc2, 0)
        self.assertEqual(switcher.cli_identity()[0], "acct1@example.com")
        self.assertFalse(os.path.exists(self.at_calls))


if __name__ == "__main__":
    unittest.main()
