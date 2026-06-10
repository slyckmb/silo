#!/usr/bin/env python3
"""
silo-rt-cache-daemon — Lease-aware cache daemon for rTorrent.

Polls rTorrent via XMLRPC on a configurable interval, writes display-ready
JSON rows to ~/.cache/silo-rt/torrents.json.  The silo dashboard reads from
this cache file instead of calling XMLRPC on every repaint.

URL resolution order:
  1. --xmlrpc-url CLI arg
  2. RTORRENT_XMLRPC_URL environment variable
  3. rtorrent.xmlrpc_url in config/silo.yml

Fallback transport:
  When the host-side XMLRPC URL is broken (e.g. gluetun port-forwarding
  lost after container restart), the daemon can fall back to running a
  stdlib-only inline Python script inside the RT container via docker exec.
  Enable with --fallback-container (default: rtorrent_vpn).
  Fallback activates after --fallback-threshold consecutive primary failures.

Usage:
  silo-rt-cache-daemon [--xmlrpc-url URL] [--once] [--status]

Run as a background daemon — the dashboard calls ensure_daemon() automatically
when the rTorrent client is active and no daemon is running.
"""

__version__ = "1.1.0"

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure bin/ is importable regardless of CWD
sys.path.insert(0, str(Path(__file__).parent))

import silo_client_rt as _rt
import silo_cache_common as _cc

_CACHE_BASE = Path.home() / ".cache" / "silo-rt"

# Config paths searched in order when resolving the XMLRPC URL from silo.yml
_SILO_YML_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "config" / "silo.yml",
    Path.home() / ".config" / "silo" / "silo.yml",
]


def _resolve_url(arg_url: str) -> str:
    if arg_url:
        return arg_url
    env = os.environ.get("RTORRENT_XMLRPC_URL", "").strip()
    if env:
        return env
    try:
        import yaml  # type: ignore
        for p in _SILO_YML_CANDIDATES:
            if p.exists():
                data = yaml.safe_load(p.read_text()) or {}
                url = (data.get("downloaders") or {}).get("rtorrent", {}).get("xmlrpc_url", "")
                if url:
                    return url
    except Exception:
        pass
    return "http://localhost:18000/RPC2"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="silo-rt-cache-daemon",
        description="rTorrent cache daemon for silo",
    )
    p.add_argument("--xmlrpc-url",       default="",
                   help="rTorrent XMLRPC HTTP endpoint (overrides env/silo.yml)")
    p.add_argument("--cache-file",       default=str(_CACHE_BASE / "torrents.json"))
    p.add_argument("--meta-file",        default=str(_CACHE_BASE / "torrents.meta.json"))
    p.add_argument("--lease-dir",        default=str(_CACHE_BASE / "leases"))
    p.add_argument("--pid-file",         default=str(_CACHE_BASE / "daemon.pid"))
    p.add_argument("--lock-file",        default=str(_CACHE_BASE / "daemon.lock"))
    p.add_argument("--default-interval", type=float, default=30.0,
                   help="Normal poll interval in seconds (default: 30)")
    p.add_argument("--min-interval",     type=float, default=10.0)
    p.add_argument("--max-interval",     type=float, default=120.0)
    p.add_argument("--idle-grace",       type=float, default=120.0,
                   help="Exit after this many idle seconds with no active leases")
    p.add_argument("--sleep-step",       type=float, default=0.5)
    p.add_argument("--once",             action="store_true",
                   help="Fetch once, write cache, then exit (useful for scripts)")
    p.add_argument("--max-consecutive-failures", type=int, default=10,
                   help="Exit daemon after this many consecutive fetch failures (default: 10, 0=disabled)")
    p.add_argument("--status",           action="store_true",
                   help="Print cache/daemon status as JSON and exit")
    # ── Fallback transport ────────────────────────────────────────────────────
    p.add_argument("--fallback-container", default="rtorrent_vpn",
                   help="Docker container name for docker-exec fallback transport "
                        "(default: rtorrent_vpn).  Set to empty string to disable.")
    p.add_argument("--fallback-inner-url", default="http://localhost:8000/RPC2",
                   help="XMLRPC URL reachable from inside the fallback container "
                        "(default: http://localhost:8000/RPC2)")
    p.add_argument("--fallback-threshold", type=int, default=3,
                   help="Switch to fallback after this many consecutive primary "
                        "failures (default: 3)")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    cache_file = Path(args.cache_file)
    meta_file  = Path(args.meta_file)
    lease_dir  = Path(args.lease_dir)
    pid_file   = Path(args.pid_file)
    lock_file  = Path(args.lock_file)

    if args.status:
        print(json.dumps(
            _cc.status_payload(
                cache_file=cache_file,
                meta_file=meta_file,
                lease_dir=lease_dir,
                pid_file=pid_file,
            ),
            indent=2,
        ))
        return 0

    url               = _resolve_url(args.xmlrpc_url)
    fallback_container = args.fallback_container.strip()
    fallback_inner_url = args.fallback_inner_url.strip()
    fallback_threshold = args.fallback_threshold

    # Mutable transport-state dict — written into meta on every _write_meta call
    # via the live-reference mechanism in silo_cache_common.run_daemon().
    _transport: dict = {
        "xmlrpc_url":         url,
        "fallback_container": fallback_container or "",
        "active_transport":   url,
        "using_fallback":     False,
        "primary_failures":   0,
    }

    _primary_failures = 0   # nonlocal counter for the fetch closure

    def fetch() -> str:
        nonlocal _primary_failures

        # ── Primary transport ─────────────────────────────────────────────────
        try:
            rows = _rt.fetch(url)
            if rows:
                _primary_failures = 0
                _transport.update({
                    "active_transport": url,
                    "using_fallback":   False,
                    "primary_failures": 0,
                })
                return json.dumps(rows)
            # Empty list = connection succeeded but RT returned nothing.
            # Treat as a failure so we don't overwrite a healthy cache.
            _primary_failures += 1
        except Exception:
            _primary_failures += 1

        _transport["primary_failures"] = _primary_failures

        # ── Fallback transport (docker exec) ──────────────────────────────────
        if fallback_container and _primary_failures >= fallback_threshold:
            try:
                rows = _rt.fetch_docker_exec(fallback_container, fallback_inner_url)
                if rows:
                    _transport.update({
                        "active_transport": f"docker://{fallback_container}",
                        "using_fallback":   True,
                        "primary_failures": _primary_failures,
                    })
                    return json.dumps(rows)
            except Exception as exc:
                raise RuntimeError(
                    f"all transports failed — primary failures: {_primary_failures}, "
                    f"fallback error: {exc}"
                ) from exc

        raise RuntimeError(
            f"rTorrent returned empty result from {url} "
            f"(primary_failures={_primary_failures}, "
            f"fallback={'disabled' if not fallback_container else 'threshold not reached'})"
        )

    return _cc.run_daemon(
        fetch_fn=fetch,
        cache_file=cache_file,
        meta_file=meta_file,
        lease_dir=lease_dir,
        pid_file=pid_file,
        lock_file=lock_file,
        default_interval=args.default_interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        idle_grace=args.idle_grace,
        sleep_step=args.sleep_step,
        run_once=args.once,
        extra_meta=_transport,
        max_consecutive_failures=args.max_consecutive_failures,
    )


if __name__ == "__main__":
    raise SystemExit(main())
