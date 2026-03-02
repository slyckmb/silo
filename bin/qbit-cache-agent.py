#!/usr/bin/env python3
"""
qbit-cache-agent.py

Lease-aware helper for qB torrents/info shared cache.

Behavior:
- Renews a client lease with requested poll interval.
- Optionally ensures daemon is running.
- Returns cached torrents/info JSON (fresh when possible; stale fallback optional).
- Optional status mode prints daemon/cache/lease summary JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_age_seconds(meta: dict, now: float) -> float | None:
    fetched_at = meta.get("fetched_at")
    if fetched_at is None:
        return None
    try:
        return max(0.0, now - float(fetched_at))
    except Exception:
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_name(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    out = out.strip("-._")
    return out or "client"


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


def _active_leases(lease_dir: Path, now: float) -> list[dict]:
    active: list[dict] = []
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
    timeout: float,
    retries: int,
    retry_delay: float,
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
        "--timeout",
        str(timeout),
        "--retries",
        str(retries),
        "--retry-delay",
        str(retry_delay),
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


def parse_args() -> argparse.Namespace:
    base_dir = Path.home() / ".cache" / "qbitui"
    parser = argparse.ArgumentParser(
        description="Return qB torrents/info JSON from shared cache, with lease renewal."
    )
    parser.add_argument("--max-age", type=float, default=15.0, help="Max cache age seconds (default: 15).")
    parser.add_argument(
        "--cache-file",
        default=str(base_dir / "torrents-info.json"),
        help="Cache JSON file path.",
    )
    parser.add_argument(
        "--meta-file",
        default=str(base_dir / "torrents-info.meta.json"),
        help="Cache metadata file path.",
    )
    parser.add_argument(
        "--lease-dir",
        default=str(base_dir / "leases"),
        help="Lease directory path.",
    )
    parser.add_argument(
        "--pid-file",
        default=str(base_dir / "daemon.pid"),
        help="Daemon pid file path.",
    )
    parser.add_argument(
        "--lock-file",
        default=str(base_dir / "daemon.lock"),
        help="Daemon lock file path.",
    )
    parser.add_argument(
        "--daemon-cmd",
        default=str(Path(__file__).with_name("qbit-cache-daemon.py")),
        help="Path to daemon command.",
    )
    parser.add_argument(
        "--daemon-log-file",
        default=str(base_dir / "daemon.log"),
        help="Daemon log file path.",
    )
    parser.add_argument("--client-id", default="", help="Lease client id (default: derived from script+pid).")
    parser.add_argument(
        "--requested-interval",
        type=float,
        default=None,
        help="Requested poll interval seconds for this lease (default: max-age, clamped).",
    )
    parser.add_argument("--lease-ttl", type=float, default=45.0, help="Lease TTL seconds (default: 45).")
    parser.add_argument(
        "--ensure-daemon",
        action="store_true",
        help="Start daemon if needed before reading cache.",
    )
    parser.add_argument(
        "--wait-fresh",
        type=float,
        default=5.0,
        help="Max seconds to wait for fresh snapshot when stale (default: 5).",
    )
    parser.add_argument(
        "--allow-stale",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow stale cache fallback if fresh data is unavailable (default: true).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print daemon/cache/lease status JSON and exit.",
    )

    # Daemon defaults used when --ensure-daemon starts it.
    parser.add_argument("--daemon-default-interval", type=float, default=10.0)
    parser.add_argument("--daemon-min-interval", type=float, default=2.0)
    parser.add_argument("--daemon-max-interval", type=float, default=60.0)
    parser.add_argument("--daemon-idle-grace", type=float, default=120.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.max_age < 0:
        print("--max-age must be >= 0", file=sys.stderr)
        return 2
    if args.lease_ttl <= 0:
        print("--lease-ttl must be > 0", file=sys.stderr)
        return 2
    if args.wait_fresh < 0:
        print("--wait-fresh must be >= 0", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be > 0", file=sys.stderr)
        return 2
    if args.retries < 1:
        print("--retries must be >= 1", file=sys.stderr)
        return 2
    if args.retry_delay < 0:
        print("--retry-delay must be >= 0", file=sys.stderr)
        return 2
    if args.daemon_min_interval <= 0 or args.daemon_max_interval <= 0 or args.daemon_default_interval <= 0:
        print("daemon interval arguments must be > 0", file=sys.stderr)
        return 2
    if args.daemon_min_interval > args.daemon_max_interval:
        print("--daemon-min-interval must be <= --daemon-max-interval", file=sys.stderr)
        return 2

    cache_file = Path(args.cache_file).expanduser()
    meta_file = Path(args.meta_file).expanduser()
    lease_dir = Path(args.lease_dir).expanduser()
    pid_file = Path(args.pid_file).expanduser()
    lock_file = Path(args.lock_file).expanduser()
    daemon_cmd = Path(args.daemon_cmd).expanduser()
    daemon_log_file = Path(args.daemon_log_file).expanduser()

    if args.status:
        status = _status_payload(
            cache_file=cache_file,
            meta_file=meta_file,
            lease_dir=lease_dir,
            pid_file=pid_file,
        )
        print(json.dumps(status, indent=2))
        return 0

    requested_interval = args.requested_interval
    if requested_interval is None:
        requested_interval = args.max_age
    try:
        requested_interval = float(requested_interval)
    except Exception:
        requested_interval = args.daemon_default_interval
    if requested_interval <= 0:
        requested_interval = args.daemon_min_interval

    requested_interval = max(args.daemon_min_interval, min(args.daemon_max_interval, requested_interval))

    if args.client_id.strip():
        client_id = args.client_id.strip()
    else:
        client_id = f"cache-agent-{socket.gethostname()}-{os.getpid()}"

    _write_lease(
        lease_dir=lease_dir,
        client_id=client_id,
        requested_interval_s=requested_interval,
        lease_ttl_s=args.lease_ttl,
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
            default_interval=args.daemon_default_interval,
            min_interval=args.daemon_min_interval,
            max_interval=args.daemon_max_interval,
            idle_grace=args.daemon_idle_grace,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
        if not started:
            print("qbit-cache-agent: failed to ensure daemon is running", file=sys.stderr)

    def _load_cache_and_meta() -> tuple[str, dict, float | None]:
        meta = _read_json(meta_file)
        age = _cache_age_seconds(meta, time.time())
        text = cache_file.read_text(encoding="utf-8") if cache_file.exists() else ""
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

    text, _meta, age = _load_cache_and_meta()
    if args.allow_stale and text:
        sys.stdout.write(text)
        return 0

    if not text:
        print("qbit-cache-agent: no cached snapshot available", file=sys.stderr)
    else:
        print(
            f"qbit-cache-agent: cache too stale age_s={age if age is not None else 'unknown'} max_age_s={args.max_age}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
