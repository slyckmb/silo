#!/usr/bin/env python3
"""
silo_cache_common — Generic lease-aware cache daemon infrastructure.

Client-agnostic: callers inject a fetch_fn() -> str (returns JSON text to
cache) and pass standard path/timing args.  Shared by silo-rt-cache-daemon.py
and any future silo client cache daemons.

Mirrors the design of hashall's qb_cache.py daemon loop without any
qBittorrent-specific code.
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

__version__ = "1.3.0"


# ── Utilities ────────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_name(value: str) -> str:
    out = "".join(c if c.isalnum() or c in "._-" else "-" for c in value.strip())
    return out.strip("-._") or "client"


# ── Process / PID helpers ────────────────────────────────────────────────────

def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_running(pid_file: Path) -> bool:
    """Return True if the daemon recorded in pid_file is still alive."""
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return _process_running(pid)


def stop_daemon(pid_file: Path) -> bool:
    """Send SIGTERM to the daemon recorded in pid_file.

    Returns True if a signal was delivered (process existed), False if there
    was nothing to kill.  Safe to call even if the daemon is not running.
    """
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if not _process_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


# ── Lease helpers ────────────────────────────────────────────────────────────

def write_lease(
    *,
    lease_dir: Path,
    client_id: str,
    requested_interval_s: float,
    lease_ttl_s: float,
) -> Path:
    now = time.time()
    lease = {
        "client_id": client_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "requested_interval_s": requested_interval_s,
        "lease_ttl_s": lease_ttl_s,
        "updated_at": now,
        "updated_at_iso": _iso(now),
        "expires_at": now + lease_ttl_s,
        "expires_at_iso": _iso(now + lease_ttl_s),
    }
    lease_dir.mkdir(parents=True, exist_ok=True)
    lease_path = lease_dir / f"{_safe_name(client_id)}.json"
    _atomic_write_text(lease_path, json.dumps(lease, indent=2) + "\n")
    return lease_path


def active_leases(lease_dir: Path, now: float) -> List[dict]:
    result: List[dict] = []
    if not lease_dir.exists():
        return result
    for p in sorted(lease_dir.glob("*.json")):
        lease = _read_json(p)
        if not lease:
            continue
        try:
            exp = float(lease.get("expires_at", 0))
        except Exception:
            exp = 0.0
        if exp <= now:
            continue
        lease["_path"] = str(p)
        result.append(lease)
    return result


def _cleanup_expired_leases(lease_dir: Path, now: float) -> List[dict]:
    """Remove expired lease files and return the surviving active leases."""
    result: List[dict] = []
    lease_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(lease_dir.glob("*.json")):
        lease = _read_json(p)
        if not lease:
            continue
        try:
            exp = float(lease.get("expires_at", 0))
        except Exception:
            exp = 0.0
        if exp <= now:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        lease["_path"] = str(p)
        result.append(lease)
    return result


# ── Cache meta helpers ───────────────────────────────────────────────────────

def cache_age_seconds(meta: dict, now: float) -> Optional[float]:
    fetched_at = meta.get("fetched_at")
    if fetched_at is None:
        return None
    try:
        return max(0.0, now - float(fetched_at))
    except Exception:
        return None


def status_payload(
    *, cache_file: Path, meta_file: Path, lease_dir: Path, pid_file: Path
) -> dict:
    now = time.time()
    meta = _read_json(meta_file)
    age = cache_age_seconds(meta, now)
    leases = active_leases(lease_dir, now)
    try:
        dpid = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.exists() else 0
    except Exception:
        dpid = 0
    return {
        "now": now,
        "now_iso": _iso(now),
        "cache_file": str(cache_file),
        "meta_file": str(meta_file),
        "lease_dir": str(lease_dir),
        "pid_file": str(pid_file),
        "cache_exists": cache_file.exists(),
        "meta_exists": meta_file.exists(),
        "cache_age_s": age,
        "daemon_pid": dpid,
        "daemon_running": _process_running(dpid),
        "active_lease_count": len(leases),
        "active_leases": leases,
        "meta": meta,
    }


# ── Daemon lifecycle ─────────────────────────────────────────────────────────

def _validate_daemon_script(
    script_path: Path,
    daemon_name: str = "silo-rt-cache-daemon",
) -> bool:
    """
    Validate that a file looks like a valid silo daemon script.

    Checks:
      - File exists and is readable
      - File has content (size > 0)
      - File contains daemon_name in docstring/header (first 500 chars)
      - File starts with Python shebang
    """
    if not script_path.exists() or not script_path.is_file():
        return False

    try:
        stat = script_path.stat()
        if stat.st_size == 0:
            return False
    except OSError:
        return False

    try:
        content = script_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    # Check for Python shebang
    lines = content.split("\n", 2)
    if not lines or not lines[0].startswith("#!"):
        return False
    if "python" not in lines[0].lower():
        return False

    # Check for daemon marker in first 500 chars (docstring/comments)
    if daemon_name not in content[:500]:
        return False

    return True


def _find_running_daemon(
    daemon_name: str = "silo-rt-cache-daemon",
) -> Path | None:
    """
    Scan /proc for a running silo daemon process and return its script path.

    Returns None if not found or /proc is not available.
    Safe to call on non-Linux systems (gracefully returns None).
    """
    script_filename = f"{daemon_name}.py"
    try:
        import glob
        proc_dirs = glob.glob("/proc/[0-9]*/cmdline")
    except Exception:
        return None

    for cmdline_file in proc_dirs:
        try:
            with open(cmdline_file, "rb") as f:
                cmdline_bytes = f.read()
        except OSError:
            # Permission denied or file disappeared
            continue

        try:
            # cmdline uses null bytes as separators; split and decode
            parts = cmdline_bytes.split(b"\x00")
            cmdline = [p.decode("utf-8", errors="ignore") for p in parts if p]
        except Exception:
            continue

        if any(script_filename in arg for arg in cmdline):
            for arg in cmdline:
                if script_filename in arg:
                    script_path = Path(arg)
                    if _validate_daemon_script(script_path, daemon_name=daemon_name):
                        return script_path

    return None


def discover_daemon_script(
    default_relative: Path,
    daemon_name: str = "silo-rt-cache-daemon",
    env_var: str = "SILO_RT_DAEMON_SCRIPT",
) -> Path | None:
    """
    Discover a silo daemon script by trying multiple strategies in order:

      1. env_var environment variable (if set and valid)
      2. Running process detection (scan /proc for active daemon)
      3. Default relative path (provided by caller)
      4. Common alternate paths

    daemon_name: marker string that must appear in the script's first 500 chars.
    env_var:     name of the environment variable for explicit override.

    Returns:
      - Path object if a valid daemon script is found
      - None if no valid daemon script is found (graceful degradation)
    """
    # Strategy 1: Environment variable override
    env_override = os.environ.get(env_var, "").strip()
    if env_override:
        env_path = Path(env_override).expanduser().resolve()
        if _validate_daemon_script(env_path, daemon_name=daemon_name):
            return env_path

    # Strategy 2: Find running daemon process
    running = _find_running_daemon(daemon_name=daemon_name)
    if running:
        return running

    # Strategy 3: Default relative path (original behavior)
    if _validate_daemon_script(default_relative, daemon_name=daemon_name):
        return default_relative

    # Strategy 4: Try common alternate paths
    script_filename = f"{daemon_name}.py"
    alternates = [
        Path.home() / "dev" / "tools" / "silo" / "bin" / script_filename,
        Path("/tmp") / script_filename,
    ]

    # Also try paths from any .agent/worktrees subdirectories
    try:
        silo_root = default_relative.parent.parent
        agent_dir = silo_root / ".agent" / "worktrees"
        if agent_dir.exists():
            for worktree_dir in agent_dir.iterdir():
                if worktree_dir.is_dir():
                    candidate = worktree_dir / "bin" / script_filename
                    if _validate_daemon_script(candidate, daemon_name=daemon_name):
                        return candidate
    except Exception:
        pass

    # Check remaining alternates
    for alt_path in alternates:
        if _validate_daemon_script(alt_path, daemon_name=daemon_name):
            return alt_path

    return None


def ensure_daemon(
    *,
    daemon_script: Path,
    cache_file: Path,
    meta_file: Path,
    lease_dir: Path,
    pid_file: Path,
    lock_file: Path,
    log_file: Path,
    extra_args: List[str] | None = None,
    default_interval: float,
    min_interval: float,
    max_interval: float,
    idle_grace: float,
) -> bool:
    """Start daemon_script as a background process if not already running."""
    if daemon_running(pid_file):
        return True
    if not daemon_script.exists():
        return False
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(daemon_script),
        "--cache-file", str(cache_file),
        "--meta-file",  str(meta_file),
        "--lease-dir",  str(lease_dir),
        "--pid-file",   str(pid_file),
        "--lock-file",  str(lock_file),
        "--default-interval", str(default_interval),
        "--min-interval",     str(min_interval),
        "--max-interval",     str(max_interval),
        "--idle-grace",       str(idle_grace),
        *(extra_args or []),
    ]
    env = {k: v for k, v in os.environ.items() if len(k.encode()) + len(v.encode()) <= 8192}
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(f"[{_iso(time.time())}] starting daemon: {' '.join(cmd)}\n")
        lf.flush()
        subprocess.Popen(
            cmd, start_new_session=True,
            stdout=lf, stderr=lf,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=env,
        )
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if daemon_running(pid_file):
            return True
        time.sleep(0.1)
    return daemon_running(pid_file)


# ── Main daemon loop ─────────────────────────────────────────────────────────

def run_daemon(
    *,
    fetch_fn: Callable[[], str],
    cache_file: Path,
    meta_file: Path,
    lease_dir: Path,
    pid_file: Path,
    lock_file: Path,
    default_interval: float,
    min_interval: float,
    max_interval: float,
    idle_grace: float,
    sleep_step: float,
    run_once: bool = False,
    extra_meta: Optional[dict] = None,
    max_consecutive_failures: int = 10,
) -> int:
    """
    Generic lease-aware daemon loop.

    fetch_fn() must return JSON text to atomically write to cache_file,
    or raise on failure (triggers exponential backoff).

    The daemon exits cleanly when:
      - no active leases remain for idle_grace seconds
      - SIGTERM / SIGINT received
      - run_once=True (fetch once then exit)
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lease_dir.mkdir(parents=True, exist_ok=True)

    lock_fp = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0   # another instance already holds the lock

    running = True

    def _stop(_sig, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(pid_file, f"{os.getpid()}\n")

    base_meta: dict = {"daemon_pid": os.getpid(), "cache_file": str(cache_file)}
    # _live_extra is kept as a reference — the caller can mutate the dict
    # between fetch cycles to update transport state visible in the meta file.
    _live_extra: dict = extra_meta if extra_meta is not None else {}

    def _write_meta(extra: dict) -> None:
        prev = _read_json(meta_file)
        # Merge order: base → prev (persisted history) → _live_extra (current
        # daemon's mutable transport state) → extra (per-fetch result).
        # _live_extra must come after prev so the running daemon's transport
        # state overwrites stale values left by a previous daemon instance.
        _atomic_write_text(meta_file, json.dumps(
            {**base_meta, **prev, **_live_extra, **extra}, indent=2) + "\n")

    try:
        if run_once:
            now = time.time()
            text = fetch_fn()
            count = len(json.loads(text)) if text else 0
            _atomic_write_text(cache_file, text)
            _write_meta({"source": "daemon_once", "fetched_at": now,
                         "fetched_at_iso": _iso(now), "items": count,
                         "active_leases": 0, "effective_interval_s": None,
                         "last_error": "", "consecutive_failures": 0,
                         "updated_at": now, "updated_at_iso": _iso(now)})
            return 0

        last_active_at = time.time()
        last_fetch_at  = 0.0
        eff_interval   = max(min_interval, min(max_interval, default_interval))
        consecutive_failures = 0
        _MAX_BACKOFF = 60.0

        while running:
            now    = time.time()
            leases = _cleanup_expired_leases(lease_dir, now)
            n      = len(leases)

            if n > 0:
                last_active_at = now
            elif (now - last_active_at) >= idle_grace:
                _write_meta({"source": "daemon_idle_exit", "idle_exit_at": now,
                             "idle_exit_at_iso": _iso(now), "active_leases": 0,
                             "effective_interval_s": eff_interval,
                             "updated_at": now, "updated_at_iso": _iso(now)})
                break

            if n > 0 and (last_fetch_at <= 0 or (now - last_fetch_at) >= eff_interval):
                try:
                    text  = fetch_fn()
                    now   = time.time()
                    count = len(json.loads(text)) if text else 0
                    _atomic_write_text(cache_file, text)
                    _write_meta({"source": "daemon_live", "fetched_at": now,
                                 "fetched_at_iso": _iso(now), "items": count,
                                 "active_leases": n, "effective_interval_s": eff_interval,
                                 "last_error": "", "consecutive_failures": 0,
                                 "updated_at": now, "updated_at_iso": _iso(now)})
                    consecutive_failures = 0
                    last_fetch_at = now
                except Exception as exc:
                    now = time.time()
                    consecutive_failures += 1
                    backoff = min(2 ** (consecutive_failures - 1) * min_interval, _MAX_BACKOFF)
                    backoff += random.uniform(0, 0.3 * backoff)
                    _write_meta({"source": "daemon_error", "last_error": str(exc),
                                 "last_error_at": now, "last_error_at_iso": _iso(now),
                                 "active_leases": n, "effective_interval_s": eff_interval,
                                 "backoff_s": backoff,
                                 "consecutive_failures": consecutive_failures,
                                 "updated_at": now, "updated_at_iso": _iso(now)})
                    if max_consecutive_failures > 0 and consecutive_failures >= max_consecutive_failures:
                        _write_meta({"source": "daemon_fatal",
                                     "fatal_error": str(exc),
                                     "consecutive_failures": consecutive_failures,
                                     "max_consecutive_failures": max_consecutive_failures,
                                     "updated_at": now, "updated_at_iso": _iso(now)})
                        print(
                            f"{sys.argv[0]}: FATAL — {consecutive_failures} consecutive failures "
                            f"(max {max_consecutive_failures}). "
                            f"Last error: {exc}",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    last_fetch_at = now - (eff_interval - backoff)

            time.sleep(sleep_step)
        return 0

    finally:
        try:
            if (pid_file.exists()
                    and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())):
                pid_file.unlink()
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass
