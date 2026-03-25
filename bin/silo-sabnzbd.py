#!/usr/bin/env python3
"""Interactive SABnzbd dashboard with modes, hotkeys, and paging."""
import argparse
import json
import os
import re
import select
import shutil
import sys
import readline  # Enables line editing for input()
import termios
import time
import tty
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

SCRIPT_NAME = "sabnzbd-dashboard"
VERSION = "1.1.0"
LAST_UPDATED = "2026-01-21"

COLOR_CYAN = "\033[36m"
COLOR_GREEN = "\033[32m"
COLOR_RED = "\033[31m"
COLOR_YELLOW = "\033[33m"
COLOR_BLUE = "\033[34m"
COLOR_MAGENTA = "\033[35m"
COLOR_GREY = "\033[90m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


def get_key() -> str:
    """Get single keypress."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = ""
            if select.select([sys.stdin], [], [], 0.1)[0]:
                seq = sys.stdin.read(1)
                if seq == "[":
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        _ = sys.stdin.read(1)
                        return ""
                if seq == "O":
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        _ = sys.stdin.read(1)
                        return ""
            return ""
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def terminal_width() -> int:
    try:
        return max(40, shutil.get_terminal_size((100, 20)).columns)
    except Exception:
        return 100


def read_api_key(path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("SABNZBD_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def read_api_url_from_config(path: Path) -> str:
    if not path.exists():
        return ""
    if yaml is not None:
        try:
            data = yaml.safe_load(path.read_text()) or {}
            api_url = (data.get("downloaders") or {}).get("sabnzbd", {}).get("api_url", "")
            if api_url:
                return api_url
        except Exception:
            pass

    api_url = ""
    in_downloaders = False
    in_sab = False
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_downloaders = line.strip() == "downloaders:"
            in_sab = False
            continue
        if in_downloaders and line.startswith("  ") and line.strip().endswith(":"):
            in_sab = line.strip() == "sabnzbd:"
            continue
        if in_downloaders and in_sab and line.strip().startswith("api_url:"):
            api_url = line.split("api_url:", 1)[1].strip()
            break
    return api_url


def sab_api_request(api_url: str, api_key: str, params: dict, timeout: int = 10) -> dict:
    url = f"{api_url}?{urllib.parse.urlencode({**params, 'apikey': api_key, 'output': 'json'})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}
    if body.strip().startswith("<!DOCTYPE") or body.strip().startswith("<html"):
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def age_str(value) -> str:
    if not value:
        return "n/a"
    dt = None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        try:
            dt = datetime.fromtimestamp(int(value), timezone.utc)
        except Exception:
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                dt = None
    if not dt:
        return "n/a"
    now = datetime.now(timezone.utc)
    delta = now - dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def summarize(queue: dict, history: dict) -> str:
    q = queue.get("queue", {}) if isinstance(queue, dict) else {}
    status = q.get("status") or q.get("state") or "unknown"
    speed = q.get("speed") or q.get("kbpersec") or "-"
    size_left = q.get("sizeleft") or q.get("mbleft") or "-"
    time_left = q.get("timeleft") or q.get("time_left") or q.get("eta") or "-"
    slot_count = q.get("noofslots") or len(q.get("slots") or [])

    h = history.get("history", {}) if isinstance(history, dict) else {}
    slots = h.get("slots") or []
    hist_counts = {}
    for slot in slots:
        s = str(slot.get("status") or "unknown").lower()
        hist_counts[s] = hist_counts.get(s, 0) + 1

    summary = f"queue:{slot_count} status:{status} speed:{speed} left:{size_left} eta:{time_left}"
    if slots:
        total_hist = sum(hist_counts.values())
        failed = hist_counts.get("failed", 0) + hist_counts.get("fail", 0)
        summary += f" history:{total_hist} failed:{failed}"
    return summary


def normalize_status(value: str) -> str:
    return str(value or "unknown").strip()


def status_color(status: str) -> str:
    s = (status or "").lower()
    if "fail" in s or "error" in s:
        return COLOR_RED
    if "pause" in s:
        return COLOR_CYAN
    if "queue" in s or "wait" in s:
        return COLOR_YELLOW
    if "download" in s or "repair" in s or "extract" in s or "propagating" in s:
        return COLOR_GREEN
    if "complete" in s:
        return COLOR_GREEN
    return COLOR_RESET


def build_rows(queue: dict, history: dict) -> list:
    rows = []
    q = queue.get("queue", {}) if isinstance(queue, dict) else {}
    for slot in q.get("slots") or []:
        name = slot.get("filename") or slot.get("name") or ""
        status = normalize_status(slot.get("status"))
        percent = slot.get("percentage") or slot.get("percent") or ""
        if percent and isinstance(percent, (int, float)):
            percent = f"{int(percent)}%"
        elif percent and isinstance(percent, str) and not percent.endswith("%"):
            percent = percent + "%"
        size = slot.get("size") or slot.get("mb") or "-"
        eta = slot.get("timeleft") or slot.get("time_left") or "-"
        category = slot.get("cat") or slot.get("category") or "-"
        nzo_id = slot.get("nzo_id") or slot.get("nzoid") or ""
        rows.append({
            "source": "Q",
            "status": status,
            "name": name,
            "progress": percent or "-",
            "size": size,
            "eta_age": eta or "-",
            "category": category,
            "id": nzo_id,
            "raw": slot,
        })

    h = history.get("history", {}) if isinstance(history, dict) else {}
    for slot in h.get("slots") or []:
        name = slot.get("name") or slot.get("filename") or ""
        status = normalize_status(slot.get("status"))
        size = slot.get("size") or "-"
        category = slot.get("cat") or slot.get("category") or "-"
        nzo_id = slot.get("nzo_id") or slot.get("nzoid") or ""
        age = age_str(slot.get("completed"))
        rows.append({
            "source": "H",
            "status": status,
            "name": name,
            "progress": "100%",
            "size": size,
            "eta_age": age,
            "category": category,
            "id": nzo_id,
            "raw": slot,
        })
    return rows


def format_rows(rows: list, page: int, page_size: int) -> list:
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, len(rows))
    return rows[start:end], total_pages, page


def apply_action(api_url: str, api_key: str, mode: str, item: dict) -> str:
    nzo_id = item.get("id")
    source = item.get("source")
    status = (item.get("status") or "").lower()

    if not nzo_id:
        return "Missing nzo_id"

    if mode == "p":
        if source != "Q":
            return "Pause/resume is only for queue items"
        action = "resume" if "pause" in status else "pause"
        result = sab_api_request(api_url, api_key, {"mode": "queue", "name": action, "value": nzo_id})
        return "OK" if result.get("status") else "Failed"
    if mode == "d":
        if source == "Q":
            result = sab_api_request(api_url, api_key, {"mode": "queue", "name": "delete", "value": nzo_id})
        else:
            result = sab_api_request(api_url, api_key, {"mode": "history", "name": "delete", "value": nzo_id})
        return "OK" if result.get("status") else "Failed"
    if mode == "c":
        print("Enter new category (blank cancels): ", end="", flush=True)
        value = input().strip()
        if not value:
            return "Cancelled"
        result = sab_api_request(api_url, api_key, {"mode": "change_cat", "name": nzo_id, "value": value})
        return "OK" if result.get("status") else "Failed"
    if mode == "t":
        if source != "H":
            return "Retry is only for history items"
        result = sab_api_request(api_url, api_key, {"mode": "retry", "value": nzo_id})
        return "OK" if result.get("status") else "Failed"
    if mode == "m":
        if source != "H":
            return "Mark-complete is only for history items"
        result = sab_api_request(api_url, api_key, {"mode": "history", "name": "mark_as_completed", "value": nzo_id})
        return "OK" if result.get("status") else "Failed"
    return "Unknown mode"


def print_details(item: dict) -> None:
    raw = item.get("raw") or {}
    print(f"{COLOR_BOLD}Details{COLOR_RESET}")
    print(f"  Name: {item.get('name')}")
    print(f"  Status: {item.get('status')}")
    print(f"  Source: {item.get('source')}")
    print(f"  Category: {item.get('category')}")
    print(f"  Size: {item.get('size')}")
    print(f"  Progress: {item.get('progress')}")
    print(f"  ETA/Age: {item.get('eta_age')}")
    print(f"  ID: {item.get('id')}")
    for key in ("path", "storage", "message", "priority", "completed", "time_added"):
        if key in raw:
            print(f"  {key}: {raw.get(key)}")
    for key in ("fail_message", "fail_message_short", "fail_msg", "failmsg"):
        if key in raw and raw.get(key):
            print(f"  {key}: {raw.get(key)}")
    print("")
    print("Press any key to continue...", end="", flush=True)
    _ = get_key()


def print_raw(item: dict) -> None:
    raw = item.get("raw") or {}
    print(f"{COLOR_BOLD}Raw JSON{COLOR_RESET}")
    print(json.dumps(raw, indent=2, sort_keys=True))
    print("")
    print("Press any key to continue...", end="", flush=True)
    _ = get_key()


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive SABnzbd dashboard")
    parser.add_argument("--config", default=os.environ.get("SABNZBD_CONFIG_FILE"), help="Path to request-cache.yml")
    parser.add_argument("--history-limit", type=int, default=int(os.environ.get("SABNZBD_HISTORY_LIMIT", "25")))
    parser.add_argument("--no-history", action="store_true", help="Disable history fetch")
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("SABNZBD_PAGE_SIZE", "10")))
    args = parser.parse_args()
    show_history_env = os.environ.get("SABNZBD_SHOW_HISTORY", "").strip().lower()
    if show_history_env in ("0", "false", "no"):
        args.no_history = True

    config_path = Path(args.config) if args.config else (Path(__file__).parent.parent / "config" / "request-cache.yml")
    api_url = os.environ.get("SABNZBD_URL") or read_api_url_from_config(config_path) or "http://localhost:8080"
    api_url = api_url.rstrip("/")
    if not api_url.endswith("/api"):
        api_url = api_url + "/api"

    api_key = os.environ.get("SABNZBD_API_KEY") or read_api_key(Path(os.environ.get("SABNZBD_API_KEY_FILE", "/mnt/config/secrets/sabnzbd/sabnzbd.env")))
    if not api_key:
        print("ERROR: SABNZBD_API_KEY not found (set SABNZBD_API_KEY or SABNZBD_API_KEY_FILE)", file=sys.stderr)
        return 1

    mode = "i"
    scope = "all"
    page = 0
    filter_term = ""

    while True:
        queue = sab_api_request(api_url, api_key, {"mode": "queue"}) or {}
        history = {}
        if not args.no_history:
            history = sab_api_request(api_url, api_key, {"mode": "history", "limit": args.history_limit}) or {}
        rows = build_rows(queue, history)

        if scope == "queue":
            rows = [r for r in rows if r.get("source") == "Q"]
        elif scope == "history":
            rows = [r for r in rows if r.get("source") == "H"]

        if filter_term:
            f = filter_term.lower()
            rows = [r for r in rows if f in (r.get("name") or "").lower()]

        page_rows, total_pages, page = format_rows(rows, page, args.page_size)

        os.system("clear")
        print(f"{COLOR_BOLD}SABNZBD DASHBOARD (TUI){COLOR_RESET}")
        print(f"API: {api_url}")
        print(f"Summary: {summarize(queue, history)}")
        print("")

        scope_label = {"all": "ALL", "queue": "QUEUE", "history": "HISTORY"}[scope]
        mode_label = {"i": "INFO", "p": "PAUSE/RESUME", "d": "DELETE", "c": "CATEGORY", "t": "RETRY", "m": "MARK DONE"}[mode]
        filter_label = filter_term if filter_term else "-"
        page_label = f"Page {page + 1}/{total_pages}"
        print(f"Mode: {COLOR_BLUE}{mode_label}{COLOR_RESET}  Scope: {COLOR_MAGENTA}{scope_label}{COLOR_RESET}  Filter: {COLOR_GREY}{filter_label}{COLOR_RESET}  {page_label}")

        print("")
        print(f"{'No':<3} {'Src':<3} {'Status':<12} {'Name':<44} {'Prog':<7} {'Size':<10} {'ETA/Age':<8} {'Category':<12} ID")
        print("-" * max(80, terminal_width()))

        for idx, item in enumerate(page_rows, 1):
            color = status_color(item.get("status") or "")
            name = (item.get("name") or "")[:44]
            status = (item.get("status") or "")[:12]
            print(
                f"{idx:<3} {item.get('source', ''):<3} "
                f"{color}{status:<12}{COLOR_RESET} "
                f"{color}{name:<44}{COLOR_RESET} "
                f"{str(item.get('progress') or '-'): <7} "
                f"{str(item.get('size') or '-'): <10} "
                f"{str(item.get('eta_age') or '-'): <8} "
                f"{str(item.get('category') or '-'): <12} "
                f"{item.get('id') or ''}"
            )

        print("")
        print(
            "Keys: 1-9=Apply  r=Refresh  f=Filter  a=All  q=Queue  h=History  "
            "[/=Next ]=Prev  i/p/d/c/t/m=Mode  R=Raw  ?=Help  x=Quit"
        )

        key = get_key()
        if key in ("x", "\x1b"):
            break
        if key == "?":
            print("Modes: i=info, p=pause/resume, d=delete, c=change category, t=retry (history), m=mark done (history)")
            print("Paging: ] or / next page, [ previous page")
            print("Scope: a=all, q=queue, h=history  Raw: R + item number")
            print("Press any key to continue...", end="", flush=True)
            _ = get_key()
            continue
        if key == "R":
            print("\nRaw item number (blank cancels): ", end="", flush=True)
            raw_choice = input().strip()
            if raw_choice.isdigit():
                idx = int(raw_choice)
                if 1 <= idx <= len(page_rows):
                    print("")
                    print_raw(page_rows[idx - 1])
            continue
        if key == "r":
            continue
        if key == "a":
            scope = "all"
            page = 0
            continue
        if key == "q":
            scope = "queue"
            page = 0
            continue
        if key == "h":
            scope = "history"
            page = 0
            continue
        if key == "f":
            print("\nFilter (blank clears): ", end="", flush=True)
            filter_term = input().strip()
            page = 0
            continue
        if key in "ipdctm":
            mode = key
            continue
        if key == "[":
            page = total_pages - 1 if page == 0 else page - 1
            continue
        if key in ("]", "/"):
            page = 0 if page >= total_pages - 1 else page + 1
            continue
        if key and key.isdigit():
            idx = int(key)
            if 1 <= idx <= len(page_rows):
                item = page_rows[idx - 1]
                if mode == "i":
                    print("")
                    print_details(item)
                else:
                    if mode == "d":
                        print(f"Delete {item.get('name')}? (y/N): ", end="", flush=True)
                        confirm = input().strip().lower()
                        if confirm != "y":
                            continue
                    result = apply_action(api_url, api_key, mode, item)
                    print(f"{mode_label}: {result}")
                    time.sleep(0.6)
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
