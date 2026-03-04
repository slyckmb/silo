#!/usr/bin/env python3
"""
qbit-cache-daemon.py

Shared qBittorrent torrents/info cache daemon.

Lifecycle:
- Runs while at least one active lease exists.
- Exits after idle grace once all leases expire.

Scheduling:
- Effective poll interval is the fastest requested interval among active leases.
- Interval is clamped to min/max bounds.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

import fcntl


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


def _fetch_torrents_json(
    *,
    qbit_url: str,
    username: str,
    password: str,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> tuple[str, int]:
    cookie_jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    login_url = qbit_url.rstrip("/") + "/api/v2/auth/login"
    info_url = qbit_url.rstrip("/") + "/api/v2/torrents/info"
    login_body = urllib.parse.urlencode({"username": username, "password": password}).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req_login = urllib.request.Request(login_url, data=login_body, method="POST")
            with opener.open(req_login, timeout=timeout) as resp:
                login_text = resp.read().decode("utf-8", errors="replace").strip()
            if login_text != "Ok.":
                raise RuntimeError(f"qB login failed: {login_text!r}")

            req_info = urllib.request.Request(info_url, method="GET")
            with opener.open(req_info, timeout=timeout) as resp:
                payload_text = resp.read().decode("utf-8", errors="strict")

            parsed = json.loads(payload_text)
            if not isinstance(parsed, list):
                raise RuntimeError("qB torrents/info payload is not a JSON array")
            return payload_text, len(parsed)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_delay)
                continue

    assert last_error is not None
    raise last_error


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _cleanup_expired_leases(lease_dir: Path, now: float) -> list[dict]:
    active: list[dict] = []
    lease_dir.mkdir(parents=True, exist_ok=True)
    for lease_path in sorted(lease_dir.glob("*.json")):
        lease = _read_json(lease_path)
        if not lease:
            continue
        expires_at = lease.get("expires_at")
        try:
            expires_at_f = float(expires_at) if expires_at is not None else 0.0
        except Exception:
            expires_at_f = 0.0
        if expires_at_f <= now:
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            continue
        lease["_path"] = str(lease_path)
        active.append(lease)
    return active


def _requested_interval_from_lease(lease: dict, default_interval: float) -> float:
    value = lease.get("requested_interval_s", default_interval)
    try:
        f = float(value)
    except Exception:
        return default_interval
    if f <= 0:
        return default_interval
    return f


def parse_args() -> argparse.Namespace:
    base_dir = Path.home() / ".cache" / "qbitui"
    parser = argparse.ArgumentParser(
        description="Run qB torrents/info shared cache daemon with lease-based lifecycle."
    )
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
        help="Daemon singleton lock file path.",
    )
    parser.add_argument("--default-interval", type=float, default=10.0, help="Default interval seconds.")
    parser.add_argument("--min-interval", type=float, default=2.0, help="Minimum interval seconds.")
    parser.add_argument("--max-interval", type=float, default=60.0, help="Maximum interval seconds.")
    parser.add_argument("--idle-grace", type=float, default=120.0, help="Exit after this many idle seconds.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Fetch retries.")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Fetch retry delay seconds.")
    parser.add_argument("--sleep-step", type=float, default=0.5, help="Main loop sleep step seconds.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and exit (ignores lease lifecycle).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.default_interval <= 0:
        print("--default-interval must be > 0", file=sys.stderr)
        return 2
    if args.min_interval <= 0:
        print("--min-interval must be > 0", file=sys.stderr)
        return 2
    if args.max_interval <= 0:
        print("--max-interval must be > 0", file=sys.stderr)
        return 2
    if args.min_interval > args.max_interval:
        print("--min-interval must be <= --max-interval", file=sys.stderr)
        return 2
    if args.idle_grace < 0:
        print("--idle-grace must be >= 0", file=sys.stderr)
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
    if args.sleep_step <= 0:
        print("--sleep-step must be > 0", file=sys.stderr)
        return 2

    qbit_url = os.environ.get("QBIT_URL", "http://localhost:9003").strip()
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
        # Another daemon instance is already active.
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
            payload_text, item_count = _fetch_torrents_json(
                qbit_url=qbit_url,
                username=username,
                password=password,
                timeout=args.timeout,
                retries=args.retries,
                retry_delay=args.retry_delay,
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
                    "updated_at": now,
                    "updated_at_iso": _iso(now),
                }
            )
            return 0

        last_active_at = time.time()
        last_fetch_at = 0.0
        effective_interval_s = _clamp(args.default_interval, args.min_interval, args.max_interval)

        while running:
            now = time.time()
            active_leases = _cleanup_expired_leases(lease_dir, now)
            active_count = len(active_leases)

            if active_count > 0:
                last_active_at = now
                fastest_requested = min(
                    _requested_interval_from_lease(lease, args.default_interval)
                    for lease in active_leases
                )
                effective_interval_s = _clamp(fastest_requested, args.min_interval, args.max_interval)
            else:
                effective_interval_s = _clamp(args.default_interval, args.min_interval, args.max_interval)
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

            should_fetch = active_count > 0 and (last_fetch_at <= 0 or (now - last_fetch_at) >= effective_interval_s)
            if should_fetch:
                try:
                    payload_text, item_count = _fetch_torrents_json(
                        qbit_url=qbit_url,
                        username=username,
                        password=password,
                        timeout=args.timeout,
                        retries=args.retries,
                        retry_delay=args.retry_delay,
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
                            "updated_at": now,
                            "updated_at_iso": _iso(now),
                        }
                    )
                except Exception as exc:
                    now = time.time()
                    _write_meta(
                        {
                            "source": "daemon_error",
                            "last_error": str(exc),
                            "last_error_at": now,
                            "last_error_at_iso": _iso(now),
                            "active_leases": active_count,
                            "effective_interval_s": effective_interval_s,
                            "updated_at": now,
                            "updated_at_iso": _iso(now),
                        }
                    )
                last_fetch_at = now

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


if __name__ == "__main__":
    raise SystemExit(main())

