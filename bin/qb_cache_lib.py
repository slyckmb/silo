"""
Shared qB torrents/info cache tooling for silo.

Lease-aware cache daemon and stdlib-only qB client so silo owns the full
qB cache implementation without any cross-repo dependency on hashall.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.1.0"

DEFAULT_QB_CACHE_BASE = Path.home() / ".cache" / "silo-qb"
LEGACY_QB_CACHE_BASE = Path.home() / ".cache" / "hashall-qb"

# Resolve qB daemon path at import time via silo_cache_common discovery chain.
# Falls back gracefully if silo_cache_common is not importable (standalone use).
_bin_dir = Path(__file__).resolve().parent
try:
    sys.path.insert(0, str(_bin_dir))
    from silo_cache_common import discover_daemon_script as _discover_daemon_script
    _discovered_qb_daemon = _discover_daemon_script(
        default_relative=_bin_dir / "silo-cache-daemon.py",
        daemon_name="silo-cache-daemon",
        env_var="SILO_QB_DAEMON_SCRIPT",
    )
except Exception:
    _discovered_qb_daemon = None
_QB_DAEMON_DEFAULT = str(_discovered_qb_daemon or (_bin_dir / "silo-cache-daemon.py"))


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
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    out = out.strip("-._")
    return out or "client"


def _cache_age_seconds(meta: dict, now: float) -> Optional[float]:
    fetched_at = meta.get("fetched_at")
    if fetched_at is None:
        return None
    try:
        return max(0.0, now - float(fetched_at))
    except Exception:
        return None


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return _process_running(pid)


def _active_leases(lease_dir: Path, now: float) -> List[dict]:
    active: List[dict] = []
    if not lease_dir.exists():
        return active
    for lease_path in sorted(lease_dir.glob("*.json")):
        lease = _read_json(lease_path)
        if not lease:
            continue
        try:
            expires_at = float(lease.get("expires_at", 0))
        except Exception:
            expires_at = 0.0
        if expires_at <= now:
            continue
        lease["_path"] = str(lease_path)
        active.append(lease)
    return active


def _write_lease(
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


def _status_payload(*, cache_file: Path, meta_file: Path, lease_dir: Path, pid_file: Path) -> dict:
    now = time.time()
    meta = _read_json(meta_file)
    age = _cache_age_seconds(meta, now)
    active = _active_leases(lease_dir, now)
    try:
        daemon_pid = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.exists() else 0
    except Exception:
        daemon_pid = 0

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
        "daemon_pid": daemon_pid,
        "daemon_running": _process_running(daemon_pid),
        "active_lease_count": len(active),
        "active_leases": active,
        "meta": meta,
    }


def _legacy_cache_pair(cache_file: Path, meta_file: Path) -> Tuple[Path, Path]:
    if cache_file == DEFAULT_QB_CACHE_BASE / "torrents-info.json":
        return LEGACY_QB_CACHE_BASE / "torrents-info.json", LEGACY_QB_CACHE_BASE / "torrents-info.meta.json"
    return cache_file, meta_file


def _fetch_torrents_snapshot(
    *,
    qbit_url: str,
    username: str,
    password: str,
) -> Tuple[str, int, Dict[str, Any], Dict[str, Any]]:
    import http.cookiejar
    import urllib.parse
    import urllib.request

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    opener.open(f"{qbit_url}/api/v2/auth/login", data, timeout=10)

    def _get(path: str) -> str:
        return opener.open(f"{qbit_url}/api/v2/{path}", timeout=10).read().decode()

    qb_version = _get("app/version").strip()
    api_version = _get("app/webapiVersion").strip()
    qb_profile: Dict[str, Any] = {"client_version": qb_version, "api_version": api_version}
    raw = _get("torrents/info?sort=name")
    torrents = json.loads(raw)
    return json.dumps(torrents, indent=2), len(torrents), qb_profile, {"mode": "not_enriched"}


def _ensure_daemon(
    *,
    daemon_cmd: Path,
    cache_file: Path,
    meta_file: Path,
    lease_dir: Path,
    pid_file: Path,
    lock_file: Path,
    log_file: Path,
    default_interval: float,
    min_interval: float,
    max_interval: float,
    idle_grace: float,
) -> bool:
    if _daemon_running(pid_file):
        return True
    if not daemon_cmd.exists():
        return False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(daemon_cmd),
        "--cache-file",
        str(cache_file),
        "--meta-file",
        str(meta_file),
        "--lease-dir",
        str(lease_dir),
        "--pid-file",
        str(pid_file),
        "--lock-file",
        str(lock_file),
        "--default-interval",
        str(default_interval),
        "--min-interval",
        str(min_interval),
        "--max-interval",
        str(max_interval),
        "--idle-grace",
        str(idle_grace),
    ]

    with log_file.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[{_iso(time.time())}] start daemon cmd={' '.join(cmd)}\n")
        log_fp.flush()
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=log_fp,
            stderr=log_fp,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if _daemon_running(pid_file):
            return True
        time.sleep(0.1)
    return _daemon_running(pid_file)


def build_agent_parser() -> argparse.ArgumentParser:
    base_dir = DEFAULT_QB_CACHE_BASE
    parser = argparse.ArgumentParser(
        description="Return qB torrents/info JSON from shared cache, with lease renewal."
    )
    parser.add_argument("--max-age", type=float, default=15.0)
    parser.add_argument("--cache-file", default=str(base_dir / "torrents-info.json"))
    parser.add_argument("--meta-file", default=str(base_dir / "torrents-info.meta.json"))
    parser.add_argument("--lease-dir", default=str(base_dir / "leases"))
    parser.add_argument("--pid-file", default=str(base_dir / "daemon.pid"))
    parser.add_argument("--lock-file", default=str(base_dir / "daemon.lock"))
    parser.add_argument(
        "--daemon-cmd",
        default=_QB_DAEMON_DEFAULT,
    )
    parser.add_argument("--daemon-log-file", default=str(base_dir / "daemon.log"))
    parser.add_argument("--client-id", default="")
    parser.add_argument("--requested-interval", type=float, default=None)
    parser.add_argument("--lease-ttl", type=float, default=45.0)
    parser.add_argument("--ensure-daemon", action="store_true")
    parser.add_argument("--wait-fresh", type=float, default=5.0)
    parser.add_argument(
        "--allow-stale",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--daemon-default-interval", type=float, default=30.0)
    parser.add_argument("--daemon-min-interval", type=float, default=15.0)
    parser.add_argument("--daemon-max-interval", type=float, default=60.0)
    parser.add_argument("--daemon-idle-grace", type=float, default=120.0)
    return parser


def agent_main(argv: Optional[List[str]] = None) -> int:
    parser = build_agent_parser()
    args = parser.parse_args(argv)

    cache_file = Path(args.cache_file).expanduser()
    meta_file = Path(args.meta_file).expanduser()
    lease_dir = Path(args.lease_dir).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    lock_file = Path(args.lock_file).expanduser()
    daemon_cmd = Path(args.daemon_cmd).expanduser()
    daemon_log_file = Path(args.daemon_log_file).expanduser()

    if args.status:
        print(
            json.dumps(
                _status_payload(
                    cache_file=cache_file,
                    meta_file=meta_file,
                    lease_dir=lease_dir,
                    pid_file=pid_file,
                ),
                indent=2,
            )
        )
        return 0

    requested_interval = args.requested_interval if args.requested_interval is not None else args.max_age
    try:
        requested_interval = float(requested_interval)
    except Exception:
        requested_interval = args.daemon_default_interval
    if requested_interval <= 0:
        requested_interval = args.daemon_min_interval
    requested_interval = max(args.daemon_min_interval, min(args.daemon_max_interval, requested_interval))

    client_id = args.client_id.strip() or f"qb-cache-agent-{socket.gethostname()}-{os.getpid()}"
    _write_lease(
        lease_dir=lease_dir,
        client_id=client_id,
        requested_interval_s=requested_interval,
        lease_ttl_s=float(args.lease_ttl),
    )

    if args.ensure_daemon:
        started = _ensure_daemon(
            daemon_cmd=daemon_cmd,
            cache_file=cache_file,
            meta_file=meta_file,
            lease_dir=lease_dir,
            pid_file=pid_file,
            lock_file=lock_file,
            log_file=daemon_log_file,
            default_interval=float(args.daemon_default_interval),
            min_interval=float(args.daemon_min_interval),
            max_interval=float(args.daemon_max_interval),
            idle_grace=float(args.daemon_idle_grace),
        )
        if not started:
            print("qb-cache-agent: failed to ensure daemon is running", file=sys.stderr)

    # max_age <= 0 means "ensure daemon only" — caller doesn't want data, just daemon health.
    # Exit immediately rather than spinning in a wait loop that can never be satisfied.
    if args.max_age <= 0:
        return 0

    def _load_cache_and_meta() -> Tuple[str, dict, Optional[float]]:
        read_cache_file = cache_file
        read_meta_file = meta_file
        if not read_cache_file.exists():
            legacy_cache_file, legacy_meta_file = _legacy_cache_pair(cache_file, meta_file)
            if legacy_cache_file != cache_file and legacy_cache_file.exists():
                read_cache_file = legacy_cache_file
                read_meta_file = legacy_meta_file
        meta = _read_json(read_meta_file)
        age = _cache_age_seconds(meta, time.time())
        text = read_cache_file.read_text(encoding="utf-8") if read_cache_file.exists() else ""
        return text, meta, age

    text, _meta, age = _load_cache_and_meta()
    if text and age is not None and age <= args.max_age:
        sys.stdout.write(text)
        return 0

    deadline = time.time() + args.wait_fresh
    while time.time() < deadline:
        time.sleep(0.2)
        text, _meta, age = _load_cache_and_meta()
        if text and age is not None and age <= args.max_age:
            sys.stdout.write(text)
            return 0

    if args.allow_stale and text:
        sys.stdout.write(text)
        return 0

    if not text:
        print("qb-cache-agent: no cached snapshot available", file=sys.stderr)
    else:
        print(
            f"qb-cache-agent: cache too stale age_s={age if age is not None else 'unknown'} max_age_s={args.max_age}",
            file=sys.stderr,
        )
    return 1


def _requested_interval_from_lease(lease: dict, default_interval: float) -> float:
    value = lease.get("requested_interval_s", default_interval)
    try:
        f = float(value)
    except Exception:
        return default_interval
    if f <= 0:
        return default_interval
    return f


def _cleanup_expired_leases(lease_dir: Path, now: float) -> List[dict]:
    active: List[dict] = []
    lease_dir.mkdir(parents=True, exist_ok=True)
    for lease_path in sorted(lease_dir.glob("*.json")):
        lease = _read_json(lease_path)
        if not lease:
            continue
        try:
            expires_at = float(lease.get("expires_at", 0))
        except Exception:
            expires_at = 0.0
        if expires_at <= now:
            try:
                lease_path.unlink()
            except OSError:
                pass
            continue
        lease["_path"] = str(lease_path)
        active.append(lease)
    return active


def build_daemon_parser() -> argparse.ArgumentParser:
    base_dir = DEFAULT_QB_CACHE_BASE
    parser = argparse.ArgumentParser(
        description="Run qB torrents/info shared cache daemon with lease-based lifecycle."
    )
    parser.add_argument("--cache-file", default=str(base_dir / "torrents-info.json"))
    parser.add_argument("--meta-file", default=str(base_dir / "torrents-info.meta.json"))
    parser.add_argument("--lease-dir", default=str(base_dir / "leases"))
    parser.add_argument("--pid-file", default=str(base_dir / "daemon.pid"))
    parser.add_argument("--lock-file", default=str(base_dir / "daemon.lock"))
    parser.add_argument("--default-interval", type=float, default=10.0)
    parser.add_argument("--min-interval", type=float, default=2.0)
    parser.add_argument("--max-interval", type=float, default=60.0)
    parser.add_argument("--idle-grace", type=float, default=120.0)
    parser.add_argument("--sleep-step", type=float, default=0.5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=10,
        help="Exit daemon after this many consecutive fetch failures (default: 10, 0=disabled)",
    )
    return parser


def daemon_main(argv: Optional[List[str]] = None) -> int:
    parser = build_daemon_parser()
    args = parser.parse_args(argv)

    # Accept the same URL env var chain as qbittorrent.py so that setting any
    # of the standard QBITTORRENT_* vars is picked up by the daemon too.
    qbit_url = (
        os.environ.get("QBIT_URL")
        or os.environ.get("QBITTORRENT_API_URL")
        or os.environ.get("QBITTORRENT_URL")
        or os.environ.get("QBITTORRENT_HOST")
        or os.environ.get("QBITTORRENTAPI_HOST")
        or "http://localhost:9003"
    ).strip()
    username = os.environ.get("QBIT_USER") or os.environ.get("QBITTORRENTAPI_USERNAME") or "admin"
    password = os.environ.get("QBIT_PASS") or os.environ.get("QBITTORRENTAPI_PASSWORD") or "adminpass"

    cache_file = Path(args.cache_file).expanduser()
    meta_file = Path(args.meta_file).expanduser()
    lease_dir = Path(args.lease_dir).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    lock_file = Path(args.lock_file).expanduser()

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lease_dir.mkdir(parents=True, exist_ok=True)

    lock_fp = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    running = True

    def _handle_signal(_signum: int, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(pid_file, f"{os.getpid()}\n")

    def _write_meta(extra: dict) -> None:
        previous = _read_json(meta_file)
        merged = {
            "daemon_pid": os.getpid(),
            "cache_file": str(cache_file),
            **previous,
            **extra,
        }
        _atomic_write_text(meta_file, json.dumps(merged, indent=2) + "\n")

    try:
        if args.once:
            now = time.time()
            payload_text, item_count, profile, tracker_meta = _fetch_torrents_snapshot(
                qbit_url=qbit_url,
                username=username,
                password=password,
            )
            _atomic_write_text(cache_file, payload_text)
            _write_meta(
                {
                    "source": "daemon_once",
                    "fetched_at": now,
                    "fetched_at_iso": _iso(now),
                    "items": item_count,
                    "active_leases": 0,
                    "effective_interval_s": None,
                    "last_error": "",
                    "consecutive_failures": 0,
                    "qb_profile": profile,
                    "tracker_enrichment": tracker_meta,
                    "updated_at": now,
                    "updated_at_iso": _iso(now),
                }
            )
            return 0

        last_active_at = time.time()
        last_fetch_at = 0.0
        effective_interval_s = max(args.min_interval, min(args.max_interval, args.default_interval))
        consecutive_failures = 0
        _MAX_BACKOFF_S = 60.0

        while running:
            now = time.time()
            active_leases = _cleanup_expired_leases(lease_dir, now)
            active_count = len(active_leases)

            if active_count > 0:
                last_active_at = now
                # Daemon controls its own refresh rate — client-requested intervals are
                # recorded in leases (for observability) but do NOT drive the fetch
                # schedule.  Using the fastest client as the clock caused runaway CPU
                # when any client polled aggressively (e.g. watch -n 5 with 5 k torrents).
                effective_interval_s = max(args.min_interval, min(args.max_interval, args.default_interval))
            else:
                effective_interval_s = max(args.min_interval, min(args.max_interval, args.default_interval))
                if (now - last_active_at) >= args.idle_grace:
                    _write_meta(
                        {
                            "source": "daemon_idle_exit",
                            "idle_exit_at": now,
                            "idle_exit_at_iso": _iso(now),
                            "active_leases": 0,
                            "effective_interval_s": effective_interval_s,
                            "updated_at": now,
                            "updated_at_iso": _iso(now),
                        }
                    )
                    break

            should_fetch = active_count > 0 and (
                last_fetch_at <= 0 or (now - last_fetch_at) >= effective_interval_s
            )
            if should_fetch:
                try:
                    payload_text, item_count, profile, tracker_meta = _fetch_torrents_snapshot(
                        qbit_url=qbit_url,
                        username=username,
                        password=password,
                    )
                    now = time.time()
                    _atomic_write_text(cache_file, payload_text)
                    _write_meta(
                        {
                            "source": "daemon_live",
                            "fetched_at": now,
                            "fetched_at_iso": _iso(now),
                            "items": item_count,
                            "active_leases": active_count,
                            "effective_interval_s": effective_interval_s,
                            "last_error": "",
                            "consecutive_failures": 0,
                            "qb_profile": profile,
                            "tracker_enrichment": tracker_meta,
                            "updated_at": now,
                            "updated_at_iso": _iso(now),
                        }
                    )
                    consecutive_failures = 0
                    last_fetch_at = now  # success: next fetch after full interval
                except Exception as exc:
                    now = time.time()
                    consecutive_failures += 1
                    backoff_s = min(
                        2 ** (consecutive_failures - 1) * args.min_interval, _MAX_BACKOFF_S
                    )
                    backoff_s += random.uniform(0, 0.3 * backoff_s)
                    _write_meta(
                        {
                            "source": "daemon_error",
                            "last_error": str(exc),
                            "last_error_at": now,
                            "last_error_at_iso": _iso(now),
                            "active_leases": active_count,
                            "effective_interval_s": effective_interval_s,
                            "backoff_s": backoff_s,
                            "consecutive_failures": consecutive_failures,
                            "updated_at": now,
                            "updated_at_iso": _iso(now),
                        }
                    )
                    if args.max_consecutive_failures > 0 and consecutive_failures >= args.max_consecutive_failures:
                        _write_meta(
                            {
                                "source": "daemon_fatal",
                                "fatal_error": str(exc),
                                "consecutive_failures": consecutive_failures,
                                "max_consecutive_failures": args.max_consecutive_failures,
                                "updated_at": now,
                                "updated_at_iso": _iso(now),
                            }
                        )
                        print(
                            f"qb-cache-daemon: FATAL — {consecutive_failures} consecutive failures "
                            f"(max {args.max_consecutive_failures}). "
                            f"Last error: {exc}",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    # Retry sooner than normal interval; backs off 15s, 30s, 60s cap
                    last_fetch_at = now - (effective_interval_s - backoff_s)

            time.sleep(args.sleep_step)
        return 0
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass
