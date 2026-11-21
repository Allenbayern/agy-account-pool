# -*- coding: utf-8 -*-
"""Live log quota-error watcher (429/RESOURCE_EXHAUSTED) and desktop idle-guard."""

import datetime as _dt
import glob
import os
import re
import subprocess
import time

from .config import get_extra_log_patterns, get_paths, redact_text

_RESET_RE = re.compile(
    r"resets?\s+(?:in|after)\s+(?:(\d+)\s*d\s*)?(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(?:(\d+)\s*s)?",
    re.I,
)

_IGNORE_MARKERS = (
    "runner.go:",
    "llm_monitor.go:",
    "monitor sir_flash_lite",
    "unavailable (code 503)",
    "no capacity available",
    "dial tcp",
    "no such host",
    "i/o timeout",
    "connection refused",
)

_CONTEXT_MARKERS = (
    "errorreport.go:",
    "error_parsing.go:",
    "agent executor error",
    "generating and executing",
    "modelapi stream error",
)

_GLOG_ERR_RE = re.compile(r"(?:^|\s)E\d{4}\s+\d{2}:\d{2}:\d{2}")
_QUOTA_ERR_RE = re.compile(
    r"(RESOURCE_EXHAUSTED\s*\(code\s*429\)|Individual quota reached|"
    r"QUOTA_EXHAUSTED|quota\s+(?:reached|exceeded|exhausted)|rateLimitExceeded)",
    re.I,
)
_EXTRA_QUOTA_RE = re.compile(
    r"(exhausted your capacity|quota\s+(?:limit\s+)?(?:reached|exceeded|exhausted)|"
    r"\b429\b.*(?:resource_exhausted|too many requests|quota|rateLimitExceeded|exhausted)|"
    r"(?:resource_exhausted|rateLimitExceeded).*\b429\b)",
    re.I,
)

_STREAM_SKIP_MARKERS = (
    "llm_monitor.go",
    "runner.go",
    "sir_flash_lite",
    "extract_searchable_content",
    "autoupdater",
    "reconcile_summaries",
)

_STREAM_ACTIVE_MARKERS = (
    "streamgeneratecontent",
    "http_helpers.go",
    "generatecontent",
    "agent executor",
)

_GLOG_TS_RE = re.compile(r"[IWEF](\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
_MAX_SCAN_BYTES = 512 * 1024
_TAIL_BUSY_BYTES = 64 * 1024


def parse_reset_duration(line):
    if not line:
        return None
    m = _RESET_RE.search(line)
    if not m:
        return None
    d, h, mins, s = m.groups()
    if not any((d, h, mins, s)):
        return None
    total = (
        int(d or 0) * 86400
        + int(h or 0) * 3600
        + int(mins or 0) * 60
        + int(s or 0)
    )
    return total if total > 0 else None


def is_quota_error_line(line, extra_source=False):
    if not line:
        return False
    low = line.lower()
    for marker in _IGNORE_MARKERS:
        if marker in low:
            return False
    if extra_source:
        return bool(_QUOTA_ERR_RE.search(line) or _EXTRA_QUOTA_RE.search(line))
    has_ctx = any(m in low for m in _CONTEXT_MARKERS) or bool(_GLOG_ERR_RE.search(line))
    if not has_ctx:
        return False
    return bool(_QUOTA_ERR_RE.search(line))


def _watched_log_paths(cli_only=False, extra_logs=None):
    paths = get_paths()
    targets = []
    if not cli_only:
        ls_log = paths["DESKTOP_LS_LOG"]
        if ls_log and os.path.isfile(ls_log):
            targets.append(("desktop", ls_log))
    cli_dir = paths["CLI_LOG_DIR"]
    if cli_dir and os.path.isdir(cli_dir):
        candidates = glob.glob(os.path.join(cli_dir, "cli-*.log"))
        candidates = [p for p in candidates if os.path.isfile(p)]
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in candidates[:3]:
            targets.append(("cli", p))

    seen = {os.path.realpath(p) for _, p in targets}
    for pat in get_extra_log_patterns(extra_logs):
        if any(ch in pat for ch in "*?["):
            matched = [p for p in glob.glob(pat) if os.path.isfile(p)]
            matched.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            matched = matched[:5]
        else:
            matched = [pat] if os.path.isfile(pat) else []
        for p in matched:
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                targets.append(("extra", p))
    return targets


def _prune_cursors(cursors, active_paths):
    if len(cursors) <= 20:
        return
    keep = set(active_paths)
    for k in list(cursors.keys()):
        if k not in keep and len(cursors) > 20:
            cursors.pop(k, None)


def scan_logs_for_quota_errors(state, now=None, init_to_eof=True, cli_only=False, extra_logs=None):
    now = now if now is not None else time.time()
    cursors = state.setdefault("log_cursors", {})
    targets = _watched_log_paths(cli_only=cli_only, extra_logs=extra_logs)
    hits = []
    for source, path in targets:
        try:
            st = os.stat(path)
        except OSError:
            continue
        cur = cursors.get(path)
        if cur is None:
            if init_to_eof:
                cursors[path] = {"inode": st.st_ino, "offset": st.st_size}
                continue
            offset = 0
        else:
            if cur.get("inode") != st.st_ino or st.st_size < int(cur.get("offset") or 0):
                offset = 0
            else:
                offset = int(cur.get("offset") or 0)

        if st.st_size <= offset:
            cursors[path] = {"inode": st.st_ino, "offset": st.st_size}
            continue

        read_from = offset
        if (st.st_size - read_from) > _MAX_SCAN_BYTES:
            read_from = st.st_size - _MAX_SCAN_BYTES

        try:
            with open(path, "rb") as fh:
                fh.seek(read_from)
                chunk = fh.read(st.st_size - read_from)
        except OSError:
            continue

        cursors[path] = {"inode": st.st_ino, "offset": st.st_size}
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if is_quota_error_line(line, extra_source=(source == "extra")):
                hits.append({
                    "source": source,
                    "path": path,
                    "line_summary": redact_text(line.strip())[:240],
                    "reset_secs": parse_reset_duration(line),
                    "detected_at": int(now),
                })

    _prune_cursors(cursors, [p for _, p in targets])
    return hits


def advance_log_cursors_to_eof(state, cli_only=False, extra_logs=None):
    cursors = state.setdefault("log_cursors", {})
    for _, path in _watched_log_paths(cli_only=cli_only, extra_logs=extra_logs):
        try:
            st = os.stat(path)
            cursors[path] = {"inode": st.st_ino, "offset": st.st_size}
        except OSError:
            pass


def _desktop_process_running():
    if os.environ.get("AGY_ASSUME_DESKTOP_RUNNING") == "1":
        return True
    try:
        res = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Antigravity"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return res.returncode == 0
    except Exception:
        return False


def _parse_glog_local_ts(line, ref_now):
    m = _GLOG_TS_RE.search(line)
    if not m:
        return None
    mm, dd, hh, mi, ss = (int(x) for x in m.groups())
    ref_dt = _dt.datetime.fromtimestamp(ref_now)
    year = ref_dt.year
    try:
        cand = _dt.datetime(year, mm, dd, hh, mi, ss)
    except ValueError:
        return None
    ts = cand.timestamp()
    # Handle Dec 31 -> Jan 1 year rollover
    if (ts - ref_now) > 180 * 86400:
        try:
            ts = _dt.datetime(year - 1, mm, dd, hh, mi, ss).timestamp()
        except ValueError:
            return None
    return ts


def is_desktop_busy(idle_seconds=15, now=None):
    now = now if now is not None else time.time()
    log_path = get_paths()["DESKTOP_LS_LOG"]
    try:
        st = os.stat(log_path)
    except OSError:
        return False, "no_desktop_log"
    if (now - st.st_mtime) >= idle_seconds:
        return False, "log_quiet"
    if not _desktop_process_running():
        return False, "desktop_not_running"

    try:
        with open(log_path, "rb") as fh:
            size = st.st_size
            fh.seek(max(0, size - _TAIL_BUSY_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False, "log_unreadable"

    for line in reversed(tail.splitlines()):
        low = line.lower()
        if any(s in low for s in _STREAM_SKIP_MARKERS):
            continue
        if not any(a in low for a in _STREAM_ACTIVE_MARKERS):
            continue
        lts = _parse_glog_local_ts(line, now)
        if lts is not None and (now - lts) < idle_seconds and lts <= (now + 5):
            return True, "active_stream"
    return False, "no_recent_stream"
