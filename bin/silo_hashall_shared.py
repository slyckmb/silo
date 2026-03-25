#!/usr/bin/env python3
"""Helpers for consuming hashall's shared qB tooling from silo."""

from __future__ import annotations

import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from http.cookiejar import CookieJar

__version__ = "1.2.0"

DEFAULT_HASHALL_ROOT = Path("/home/michael/dev/work/hashall")
DEFAULT_HASHALL_CACHE_BASE = Path.home() / ".cache" / "hashall-qb"


def check_auth_bypass(api_url: str) -> bool:
    """Check if the qB API is accessible without login (e.g. localhost whitelist)."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        # Normalize URL: ensure no trailing slash
        url = api_url.rstrip("/")
        req = urllib.request.Request(f"{url}/api/v2/app/version", method="GET")
        with opener.open(req, timeout=5) as resp:
            return resp.getcode() == 200
    except Exception:
        return False


def resolve_hashall_root() -> Path:
    candidates: list[Path] = []

    env_root = os.environ.get("HASHALL_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).expanduser())

    candidates.extend(
        [
            DEFAULT_HASHALL_ROOT,
            Path.home() / "dev" / "work" / "hashall",
        ]
    )
    # Also search active worktrees under the canonical hashall root — the
    # canonical bin/ may contain a shim, while the real implementation lives
    # in a worktree.
    worktree_base = DEFAULT_HASHALL_ROOT / ".agent" / "worktrees"
    if worktree_base.is_dir():
        candidates.extend(sorted(worktree_base.iterdir(), reverse=True))

    for root in candidates:
        agent = root / "bin" / "qb-cache-agent.py"
        daemon = root / "bin" / "qb-cache-daemon.py"
        if not (agent.exists() and daemon.exists()):
            continue
        # Skip shims that exec back into silo — they create a circular exec loop.
        # A shim is identified by importing or referencing silo_hashall_shared.
        try:
            text = agent.read_text(encoding="utf-8", errors="ignore")
            if "silo_hashall_shared" in text or "DEPRECATED SHIM" in text:
                continue

        except OSError:
            continue
        return root

    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Unable to locate hashall qB tooling. "
        f"Set HASHALL_ROOT or ensure hashall exists at one of: {searched}"
    )


def resolve_hashall_script(script_name: str) -> Path:
    script_path = resolve_hashall_root() / "bin" / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Missing hashall script: {script_path}")
    return script_path


def exec_hashall_script(script_name: str, use_bypass: bool = False):
    hashall_root = resolve_hashall_root()
    script_path = hashall_root / "bin" / script_name
    env = os.environ.copy()

    if use_bypass:
        # Try optimistic bypass to avoid triggering bans in background tasks
        qbit_url = (
            env.get("QBIT_URL")
            or env.get("QBITTORRENT_API_URL")
            or "http://localhost:9003"
        ).strip()
        if check_auth_bypass(qbit_url):
            # Whitelist active: clear password to prevent the agent from attempting login
            env.pop("QBIT_PASS", None)
            env.pop("QBITTORRENTAPI_PASSWORD", None)
            env.pop("QBITTORRENT_PASSWORD", None)

    hashall_src = str(hashall_root / "src")
    current_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = hashall_src if not current_pythonpath else f"{hashall_src}{os.pathsep}{current_pythonpath}"
    # Strip oversized env vars (bash functions/aliases can push os.environ past
    # the kernel ARG_MAX limit for execve, causing E2BIG / Errno 7).
    # Keep anything ≤ 8 KB; larger values are almost never needed by the agent.
    env = {k: v for k, v in env.items() if len(k.encode()) + len(v.encode()) <= 8192}
    os.execve(sys.executable, [sys.executable, str(script_path), *sys.argv[1:]], env)
