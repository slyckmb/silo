#!/usr/bin/env python3
"""
silo_client_rt — rTorrent data/action layer for the silo dashboard.

Pure fetch + actions module.  No TUI code.  Uses stdlib xmlrpc.client only.
Connection: HTTP XMLRPC endpoint, e.g. http://localhost:8080/RPC2
  (configure nginx/lighttpd to proxy the rTorrent SCGI socket via HTTP)

Returns display dicts whose keys exactly match what build_list_block() /
build_narrow_list_block() in silo-dashboard.py expect (same schema as
build_rows() output).
"""

import json
import os
import re
import subprocess
import time
import urllib.parse
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:
    _LOCAL_TZ = timezone.utc

# ── Module-level identity ────────────────────────────────────────────────────

NAME    = "rTorrent"
KEY     = "rtorrent"   # key used in active_client comparisons
VERSION = "1.4.3"

# ── Module-level tracker cache ───────────────────────────────────────────────
# Populated by _fetch_tracker_batch() inside fetch(); persists across daemon
# fetch cycles (the daemon imports this module once and keeps it in memory).
# Refresh happens at most once per _TRACKER_CACHE_TTL seconds.

_tracker_cache: dict      = {}   # hash_lower -> list[dict]  (url/status/tier)
_tracker_cache_time: float = 0.0
_TRACKER_CACHE_TTL: float  = 300.0   # seconds between full tracker refreshes
_TRACKER_BATCH_SIZE: int   = 500     # t.multicall calls per system.multicall chunk
_TRACKER_URL_PATTERN_MAP: dict[str, str] | None = None

# ── rTorrent multicall fields ────────────────────────────────────────────────
#
# API: d.multicall.filtered(view, filter_cmd, filter_value_CONSUMED, field1, ...)
#
# IMPORTANT: arg3 (filter_value) is silently consumed and never returned as data.
# To work around this, _MULTICALL_FIELDS starts with "d.hash=" twice:
#   call args: ('', '', 'd.hash=', 'd.hash=', 'd.name=', ...)
#                       ^^^^^^^^ consumed    ^^^^^^^^ real data index 0
#
# The result list for each torrent therefore starts at the second d.hash= (index 0).
# d.tracker_domain= is NOT available on this rTorrent build — tracker field is "-".

_MULTICALL_FIELDS = [
    "d.hash=",           # [0]  info hash (uppercase from rTorrent)
    "d.name=",           # [1]  display name
    "d.size_bytes=",     # [2]  total size in bytes
    "d.completed_bytes=",# [3]  downloaded bytes
    "d.up.rate=",        # [4]  current upload rate bytes/sec
    "d.down.rate=",      # [5]  current download rate bytes/sec
    "d.ratio=",          # [6]  ratio × 1000 (integer)
    "d.state=",          # [7]  0=stopped, 1=started
    "d.is_active=",      # [8]  0=inactive, 1=active (transfers running)
    "d.message=",        # [9]  tracker/error message
    "d.custom1=",        # [10] ruTorrent label → used as category
    "d.peers_accounted=",# [11] total peer count
    "d.timestamp.started=",# [12] unix timestamp when torrent added/started (per-torrent, unique)
    "d.directory=",      # [13] save directory (multi-file: already includes torrent name)
    "d.hashing=",        # [14] 0=not hashing, 1=hash-checking (d.check_hash in progress)
    # NOTE: d.base_path= and d.tracker_domain= are NOT available on this rTorrent build.
    # base_path is computed in Python from directory + name (see _compute_base_path).
    # NOTE: d.load_date= was incorrect (all torrents share same value: reload timestamp).
    # d.timestamp.started= is the per-torrent "added" date (each torrent has unique value).
]

# Indices into the data result list (0-based, post-consumption of arg3)
_F_HASH      = 0
_F_NAME      = 1
_F_SIZE      = 2
_F_COMPLETED = 3
_F_UPRATE    = 4
_F_DLRATE    = 5
_F_RATIO     = 6
_F_STATE     = 7
_F_ACTIVE    = 8
_F_MESSAGE   = 9
_F_CUSTOM1   = 10
_F_PEERS     = 11
_F_LOADED    = 12
_F_DIRECTORY = 13
_F_HASHING   = 14

# ── Tracker batch fetch ───────────────────────────────────────────────────────

def _url_domain(url: str) -> str:
    """Extract hostname from a tracker URL ('http://tracker.example.com:6969/ann' → 'tracker.example.com')."""
    try:
        return urllib.parse.urlparse(url).hostname or "-"
    except Exception:
        return "-"


def _find_tracker_registry() -> Path:
    env_path = os.environ.get("QBIT_TRACKER_REGISTRY_FILE", "")
    if env_path:
        return Path(env_path)
    mnt = Path("/mnt/config/tools/traktor/config/tracker-registry.yml")
    if mnt.exists():
        return mnt
    for parent in Path(__file__).resolve().parents:
        sibling = parent.parent / "traktor" / "config" / "tracker-registry.yml"
        if sibling.exists():
            return sibling
    return Path(__file__).parent.parent / "config" / "tracker-registry.yml"


def _load_tracker_url_pattern_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        trackers = data.get("trackers") or {}
        result: dict[str, str] = {}
        for tracker_key, tracker_cfg in trackers.items():
            if not isinstance(tracker_cfg, dict):
                continue
            key = str(tracker_key).strip()
            if not key:
                continue
            qbm = tracker_cfg.get("qbitmanage") or {}
            pattern = str(qbm.get("tracker_url_pattern") or "").strip()
            if pattern:
                result[pattern] = key
        return result
    except Exception:
        return {}


def _tracker_label_from_url(tracker_url: str) -> str:
    tracker_url = str(tracker_url or "").strip()
    if not tracker_url:
        return "-"
    global _TRACKER_URL_PATTERN_MAP
    if _TRACKER_URL_PATTERN_MAP is None:
        _TRACKER_URL_PATTERN_MAP = _load_tracker_url_pattern_map(_find_tracker_registry())
    for pattern, key in (_TRACKER_URL_PATTERN_MAP or {}).items():
        try:
            if re.search(pattern, tracker_url, re.IGNORECASE):
                return key
        except re.error:
            continue
    return _url_domain(tracker_url) or "-"


def _fetch_tracker_batch(
    proxy: xmlrpc.client.ServerProxy,
    hashes: list,
) -> dict:
    """Fetch tracker data for all hashes via system.multicall, in chunks.

    Returns dict: hash_lower -> list[dict{url, status, tier}].
    Each chunk bundles _TRACKER_BATCH_SIZE t.multicall calls into one HTTP
    request, keeping RT XMLRPC load low even for large swarms.

    Fields used: t.url= and t.is_usable= — confirmed available on this build.
    t.scrape_success= is NOT available and must not be requested.
    Status mapping: is_usable=1 → "working", is_usable=0 → "disabled".
    """
    result: dict = {}
    for i in range(0, len(hashes), _TRACKER_BATCH_SIZE):
        chunk = hashes[i : i + _TRACKER_BATCH_SIZE]
        calls = [
            {
                "methodName": "t.multicall",
                "params": [h.upper(), "", "t.url=", "t.is_usable="],
            }
            for h in chunk
        ]
        try:
            responses = proxy.system.multicall(calls)
        except Exception:
            # Partial failure: skip this chunk, leave hashes unmapped
            continue
        for h, resp in zip(chunk, responses):
            # system.multicall wraps each result in a 1-element list on success,
            # or returns a fault dict on per-call error.
            if isinstance(resp, dict) and "faultCode" in resp:
                result[h] = []
                continue
            rows = resp[0] if (resp and isinstance(resp, list)) else []
            trackers = []
            for row in rows:
                try:
                    url    = str(row[0])
                    usable = int(row[1])
                    status = "working" if usable else "disabled"
                    trackers.append({"url": url, "status": status, "tier": "0"})
                except Exception:
                    continue
            result[h] = trackers
    return result


# ── Path helpers ─────────────────────────────────────────────────────────────

def _compute_base_path(directory: str, name: str) -> str:
    """Derive the full content path without relying on d.base_path=.

    rTorrent stores:
      - Multi-file torrent: d.directory= = /base/TorrentName  (name already appended)
      - Single-file torrent: d.directory= = /base             (name is the filename)

    Strategy: try directory/name as a file first; if it exists that is the
    single-file case.  Otherwise fall back to directory itself (multi-file).
    """
    if not directory:
        return ""
    candidate = Path(directory) / name if name else None
    if candidate and candidate.is_file():
        return str(candidate)
    return directory


# ── Formatting helpers (mirrors dashboard equivalents) ───────────────────────

def _size_str(value) -> str:
    if value is None:
        return "-"
    try:
        value = float(value)
    except Exception:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _speed_str(value) -> str:
    if value is None:
        return "-"
    try:
        value = float(value)
    except Exception:
        return "-"
    if value <= 0:
        return "0"
    return f"{_size_str(value)}/s"


def _eta_str(seconds: int) -> str:
    if seconds <= 0 or seconds >= 8640000:
        return "-"
    mins, sec = divmod(int(seconds), 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h{mins:02d}m"
    return f"{mins}m"


def _added_str(ts) -> str:
    if not ts or int(ts) <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts), _LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _added_short_str(ts) -> str:
    if not ts or int(ts) <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts), _LOCAL_TZ).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


# ── State normalization ──────────────────────────────────────────────────────

def _normalize_state(state: int, is_active: int, message: str,
                     size_bytes: int, completed_bytes: int,
                     dl_rate: int, up_rate: int = 0, hashing: int = 0) -> str:
    """
    Map rTorrent state integers to a qBit-compatible API state string.

    The returned string must be a key in STATUS_MAPPING (silo-dashboard.py)
    so ColorScheme.status_color() maps it to the right color automatically.

    rTorrent state logic (evaluated top-to-bottom):
      hashing=1                              → "checkingDL" / "checkingUP"
      state=0, completed                     → "stoppedUP"  (seeded then stopped)
      state=0                                → "stoppedDL"  (paused group)
      state=1, is_active=0, completed        → "pausedUP"
      state=1, is_active=0                   → "pausedDL"
      state=1, is_active=1, size=0, dl>0     → "metaDL"     (magnet, no metadata yet)
      state=1, is_active=1, incomplete, message != "" → "error"
      state=1, is_active=1, dl_rate > 0      → "downloading"
      state=1, is_active=1, completed, up>0  → "uploading"
      state=1, is_active=1, completed        → "stalledUP"  (seeding, no peers)
      else                                   → "stalledDL"
    """
    completed = size_bytes > 0 and completed_bytes >= size_bytes
    if hashing:
        return "checkingUP" if completed else "checkingDL"
    if state == 0:
        return "stoppedUP" if completed else "stoppedDL"
    if is_active == 0:
        return "pausedUP" if completed else "pausedDL"
    # state=1, is_active=1 — active
    if size_bytes == 0 and dl_rate > 0:
        return "metaDL"   # magnet still downloading metadata
    if not completed and message:
        return "error"
    if dl_rate > 0:
        return "downloading"
    if completed:
        return "uploading" if up_rate > 0 else "stalledUP"
    return "stalledDL"


# Map full state strings to short 2-char codes (mirrors STATE_CODE in dashboard)
_STATE_CODE = {
    "downloading":  "D",
    "uploading":    "U",
    "pausedDL":     "PD",
    "pausedUP":     "PU",
    "stoppedDL":    "PD",  # renders same as pausedDL
    "stoppedUP":    "PU",
    "stalledDL":    "SD",
    "stalledUP":    "SU",
    "checkingDL":   "CD",
    "checkingUP":   "CU",
    "metaDL":       "MD",
    "error":        "E",
    "checking":     "CR",
}


# ── Connection ───────────────────────────────────────────────────────────────

def connect(url: str) -> xmlrpc.client.ServerProxy:
    """Return an xmlrpc ServerProxy for the given HTTP XMLRPC URL."""
    return xmlrpc.client.ServerProxy(url, allow_none=True)


# ── Fetch ────────────────────────────────────────────────────────────────────

def _process_results(
    results: list,
    proxy: xmlrpc.client.ServerProxy | None = None,
) -> list[dict]:
    """
    Convert raw d.multicall.filtered result rows into display dicts and enrich
    with tracker data.

    proxy — live ServerProxy used to refresh the tracker cache.  Pass None
    when the results came from a fallback path (e.g. docker exec); stale
    module-level tracker cache is still applied, just not refreshed.
    """
    global _tracker_cache, _tracker_cache_time

    rows: list[dict] = []
    for item in results:
        try:
            hash_val    = str(item[_F_HASH]).lower()
            name        = str(item[_F_NAME])
            size_bytes  = int(item[_F_SIZE])      or 0
            completed_b = int(item[_F_COMPLETED]) or 0
            up_rate     = int(item[_F_UPRATE])    or 0
            dl_rate     = int(item[_F_DLRATE])    or 0
            ratio_raw   = int(item[_F_RATIO])     or 0
            state       = int(item[_F_STATE])     or 0
            is_active   = int(item[_F_ACTIVE])    or 0
            message     = str(item[_F_MESSAGE] or "")
            label       = str(item[_F_CUSTOM1] or "")
            peers       = int(item[_F_PEERS])     or 0
            
            # Primary Date Added: d.timestamp.load
            created     = int(item[_F_LOADED]) if item[_F_LOADED] else 0
                
            directory   = str(item[_F_DIRECTORY] or "")
            hashing     = int(item[_F_HASHING]) if len(item) > _F_HASHING else 0
            base_path   = _compute_base_path(directory, name)

            ratio_float  = ratio_raw / 1000.0
            api_state    = _normalize_state(
                state, is_active, message, size_bytes, completed_b,
                dl_rate, up_rate, hashing,
            )
            progress_f   = (completed_b / size_bytes) if size_bytes > 0 else 0.0
            progress_pct = int(progress_f * 100)
            eta_secs     = 0
            if dl_rate > 0 and size_bytes > completed_b:
                eta_secs = (size_bytes - completed_b) // dl_rate

            category = label or "-"
            complete = size_bytes > 0 and completed_b >= size_bytes

            raw: dict = {
                "hash":              hash_val,
                "name":              name,
                "state":             api_state,
                "progress":          progress_f,
                "dlspeed":           dl_rate,
                "upspeed":           up_rate,
                "size":              size_bytes,
                "ratio":             ratio_float,
                "eta":               eta_secs,
                "category":          category,
                "tags":              label or "-",
                "added_on":          int(created) if created else 0,
                "tracker":           "-",
                "trackers":          [],
                "trackers_http":     [],
                "trackers_count":    0,
                "real_trackers_count": 0,
                "directory":         directory,
                "base_path":         base_path,
                "message":           message,
                "complete":          complete,
                "hashing":           hashing,
            }

            rows.append({
                "name":         name,
                "save_path":    directory.rstrip("/") or "-",
                "nohl":         " ",
                "state":        api_state,
                "st":           _STATE_CODE.get(api_state, "?"),
                "progress":     f"{progress_pct}%",
                "progress_pct": str(progress_pct),
                "size":         _size_str(size_bytes),
                "ratio":        f"{ratio_float:.2f}",
                "dlspeed":      _speed_str(dl_rate),
                "upspeed":      _speed_str(up_rate),
                "uploaded_raw": 0,
                "seeds":        0,
                "peers":        peers,
                "eta":          _eta_str(eta_secs),
                "added":        _added_str(created),
                "added_short":  _added_short_str(created),
                "tracker":      "-",
                "category":     category,
                "tags":         label or "-",
                "hash":         hash_val,
                "raw":          raw,
            })
        except Exception:
            continue

    # ── Tracker enrichment ────────────────────────────────────────────────────
    if rows:
        now = time.time()
        if proxy is not None and now - _tracker_cache_time >= _TRACKER_CACHE_TTL:
            try:
                hashes = [r["hash"] for r in rows]
                _tracker_cache = _fetch_tracker_batch(proxy, hashes)
                _tracker_cache_time = now
            except Exception:
                pass  # keep stale cache

        for row in rows:
            h        = row["hash"]
            trackers = _tracker_cache.get(h, [])
            working  = [t for t in trackers if t["status"] == "working"]
            primary_url = (
                working[0]["url"] if working
                else trackers[0]["url"] if trackers
                else ""
            )
            tracker_label = _tracker_label_from_url(primary_url) if primary_url else "-"
            http_urls = [t["url"] for t in trackers if t["url"].startswith("http")]
            raw = row["raw"]
            raw["tracker"]             = primary_url or "-"
            raw["trackers"]            = trackers
            raw["trackers_http"]       = http_urls
            raw["trackers_count"]      = len(trackers)
            raw["real_trackers_count"] = len(working)
            row["tracker"]             = tracker_label

    return rows


def fetch(url: str) -> list[dict]:
    """
    Fetch all torrents via host-accessible XMLRPC URL.

    Returns [] on any connection or XMLRPC error.
    """
    try:
        proxy   = connect(url)
        results = proxy.d.multicall.filtered("", "", "d.hash=", *_MULTICALL_FIELDS)
    except Exception:
        raise
    return _process_results(results, proxy)


def fetch_docker_exec(
    container: str = "rtorrent_vpn",
    inner_url: str = "http://localhost:8000/RPC2",
    timeout: int   = 60,
) -> list[dict]:
    """
    Fetch all torrents by running a stdlib-only XMLRPC call inside the named
    container via 'docker exec'.  Used as a fallback when the host-side transport
    (e.g. localhost:18000) is broken but the container-local port is healthy.

    No silo code needs to be present inside the container — only stdlib
    xmlrpc.client and json are used in the inline script.

    Returns [] on any failure.
    Tracker cache is NOT refreshed (no live proxy available), but existing
    stale module-level cache is applied to all rows.
    """
    # Build field list for the inline call — mirrors _MULTICALL_FIELDS exactly.
    # The consumed arg3 ('d.hash=') is prepended separately inside the script.
    fields_repr = repr(list(_MULTICALL_FIELDS))
    code = (
        "import xmlrpc.client,json,sys;"
        f"p=xmlrpc.client.ServerProxy({inner_url!r},allow_none=True);"
        f"r=p.d.multicall.filtered('','','d.hash=',*{fields_repr});"
        "print(json.dumps(r,default=str))"
    )
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "python3", "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker exec exited {proc.returncode}: "
                f"{proc.stderr.strip()[:200]}"
            )
        results = json.loads(proc.stdout)
    except Exception:
        return []
    return _process_results(results, proxy=None)


# ── Per-torrent detail fetchers ──────────────────────────────────────────────

def fetch_trackers(proxy: xmlrpc.client.ServerProxy, hash_val: str) -> list:
    """Return tracker list as dicts with url/status/tier (matches render_trackers_lines).

    Uses t.url=, t.is_usable=, t.failed_counter= — all confirmed available on
    this rTorrent build.  t.scrape_success= is NOT available and must not be
    requested.

    Status mapping:
      is_usable=0                       → "disabled"
      is_usable=1, failed_counter=0     → "working"
      is_usable=1, failed_counter>0     → "not working"
    """
    try:
        results = proxy.t.multicall(hash_val.upper(), "",
            "t.url=", "t.is_usable=", "t.failed_counter=") or []
        out = []
        for row in results:
            url          = str(row[0])
            is_usable    = int(row[1])
            failed_count = int(row[2])
            if not is_usable:
                status = "disabled"
            elif failed_count == 0:
                status = "working"
            else:
                status = "not working"
            out.append({"url": url, "status": status, "tier": "0"})
        return out
    except Exception:
        return []


def fetch_files(proxy: xmlrpc.client.ServerProxy, hash_val: str) -> list:
    """Return file list as dicts with name/size/progress/priority (matches render_files_lines)."""
    try:
        results = proxy.f.multicall(hash_val.upper(), "",
            "f.path=", "f.size_bytes=", "f.completed_chunks=",
            "f.size_chunks=", "f.priority=") or []
        out = []
        for row in results:
            path, size_bytes, completed, total_chunks, priority = row
            progress = completed / total_chunks if total_chunks else 0.0
            out.append({
                "name":     path,
                "size":     int(size_bytes),
                "progress": progress,
                "priority": int(priority),
            })
        return out
    except Exception:
        return []


def fetch_peers(proxy: xmlrpc.client.ServerProxy, hash_val: str) -> dict:
    """Return peer dict in format expected by render_peers_lines."""
    try:
        results = proxy.p.multicall(hash_val.upper(), "",
            "p.address=", "p.port=", "p.client_version=",
            "p.down_rate=", "p.up_rate=", "p.completed_percent=",
            "p.is_incoming=") or []
        peers: dict = {}
        for row in results:
            addr, port, client, dl_rate, ul_rate, pct, is_incoming = row
            key = f"{addr}:{port}"
            peers[key] = {
                "dl_speed": int(dl_rate),
                "up_speed": int(ul_rate),
                "client":   str(client),
                "progress": float(pct) / 100.0,
                "flags":    "I" if is_incoming else "",
            }
        return {"peers": peers}
    except Exception:
        return {}


# ── Actions ──────────────────────────────────────────────────────────────────

def _action_pause_resume(proxy: xmlrpc.client.ServerProxy,
                          hash_val: str, item: dict) -> str:
    """Pause if active, resume if paused/stopped."""
    try:
        state   = item.get("raw", {}).get("state", "")
        paused  = state in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP")
        if paused:
            proxy.d.start(hash_val.upper())
            return "Resumed"
        else:
            proxy.d.stop(hash_val.upper())
            return "Paused"
    except Exception as e:
        return f"Error: {e}"


def _action_verify(proxy: xmlrpc.client.ServerProxy,
                   hash_val: str, item: dict) -> str:
    """Trigger hash check."""
    try:
        proxy.d.check_hash(hash_val.upper())
        return "Hash check started"
    except Exception as e:
        return f"Error: {e}"


def _action_delete(proxy: xmlrpc.client.ServerProxy,
                   hash_val: str, item: dict) -> str:
    """
    Remove torrent from rTorrent (data is NOT deleted).
    Uses d.erase — equivalent to 'Remove torrent' in ruTorrent.
    To also delete data, a separate filesystem call would be needed.
    """
    name = item.get("name", hash_val[:8])
    # Inline y/N prompt — raw mode is restored by caller before this runs
    try:
        answer = input(f"Remove '{name}' from rTorrent? Data kept. [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "Cancelled"
    if answer != "y":
        return "Cancelled"
    try:
        proxy.d.erase(hash_val.upper())
        return "Removed"
    except Exception as e:
        return f"Error: {e}"


# ACTIONS dict: key → (display_label, fn(proxy, hash_val, item) -> str)
# Keys use the same letters as qBit actions where semantically identical.
ACTIONS: dict[str, tuple[str, object]] = {
    "P": ("Pause/Resume", _action_pause_resume),
    "V": ("Verify",       _action_verify),
    "D": ("Delete",       _action_delete),
}
