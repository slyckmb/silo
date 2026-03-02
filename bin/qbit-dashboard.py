#!/usr/bin/env python3
"""Interactive qBittorrent dashboard with modes, hotkeys, and paging."""
import argparse
import json
import os
import shlex
import select
import shutil
import sys
import signal
import readline  # Enables line editing for input()
import re
import subprocess
import termios
import fcntl
import struct
import time
import tty
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - optional dependency
    ZoneInfo = None
from pathlib import Path
from http.cookiejar import CookieJar
from typing import Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

SCRIPT_NAME = "qbit-dashboard"
VERSION = "1.12.7"
LAST_UPDATED = "2026-03-02"
FULL_TUI_MIN_WIDTH = 120

# ============================================================================
# COLOR SYSTEM - Claude Code Dark Mode Inspired
# ============================================================================

class ColorScheme:
    """Color scheme management with YAML override support."""

    # Type stubs for dynamically generated attributes
    CYAN: str
    BLUE: str
    PURPLE: str
    YELLOW: str
    ORANGE: str
    GREEN: str
    LAVENDER: str
    ERROR: str
    FG_PRIMARY: str
    FG_SECONDARY: str
    FG_TERTIARY: str
    BG_SELECTED: str
    CYAN_BOLD: str
    GREEN_BOLD: str
    BLUE_BOLD: str
    YELLOW_BOLD: str
    ORANGE_BOLD: str
    ERROR_BOLD: str
    PURPLE_BOLD: str
    SELECTION: str

    def __init__(self, yaml_path: Optional[Path] = None):
        """Initialize colors from YAML file or defaults."""
        self.BOLD = "\033[1m"
        self.DIM = "\033[2m"
        self.UNDERLINE = "\033[4m"
        self.RESET = "\033[0m"

        # Default palette (Claude Code Dark Mode inspired)
        self._palette = {
            'bg_primary': '#2C001E',
            'bg_secondary': '#352840',
            'bg_selected': '#3A2F5F',
            'fg_primary': '#E5E7EB',
            'fg_secondary': '#A0AEC0',
            'fg_tertiary': '#6B7280',
            'cyan': '#4EC9B0',
            'blue': '#61AFEF',
            'purple': '#C678DD',
            'yellow': '#E5C07B',
            'orange': '#D19A66',
            'green': '#98C379',
            'lavender': '#B4A5D1',
            'error': '#E06C75',
        }

        # Load YAML override if provided
        if yaml_path and yaml_path.exists() and yaml:
            try:
                with open(yaml_path) as f:
                    config = yaml.safe_load(f)
                    self._load_palette_from_yaml(config)
            except Exception as e:
                print(f"Warning: Could not load color theme: {e}", file=sys.stderr)

        self._generate_ansi_codes()

    def _load_palette_from_yaml(self, config: dict):
        """Parse YAML structure and update palette."""
        if 'palette' not in config:
            return

        palette = config['palette']
        for category in ['background', 'foreground', 'accents', 'status']:
            if category in palette:
                for name, data in palette[category].items():
                    if isinstance(data, dict) and 'hex' in data:
                        key = f"{category[:2]}_{name}" if category in ['background', 'foreground'] else name
                        self._palette[key] = data['hex']

    def _hex_to_rgb(self, hex_color: str) -> tuple[int, int, int]:
        """Convert hex color to RGB tuple."""
        hex_color = hex_color.lstrip('#')
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))

    def _rgb_to_ansi_fg(self, r: int, g: int, b: int) -> str:
        """Generate ANSI 24-bit foreground color."""
        return f"\033[38;2;{r};{g};{b}m"

    def _rgb_to_ansi_bg(self, r: int, g: int, b: int) -> str:
        """Generate ANSI 24-bit background color."""
        return f"\033[48;2;{r};{g};{b}m"

    def _generate_ansi_codes(self):
        """Generate all ANSI color codes from palette."""
        for key, hex_val in self._palette.items():
            r, g, b = self._hex_to_rgb(hex_val)
            setattr(self, key.upper(), self._rgb_to_ansi_fg(r, g, b))

        # Background colors
        r, g, b = self._hex_to_rgb(self._palette['bg_selected'])
        self.BG_SELECTED = self._rgb_to_ansi_bg(r, g, b)

        # Common combinations
        self.CYAN_BOLD = self.CYAN + self.BOLD
        self.GREEN_BOLD = self.GREEN + self.BOLD
        self.BLUE_BOLD = self.BLUE + self.BOLD
        self.YELLOW_BOLD = self.YELLOW + self.BOLD
        self.ORANGE_BOLD = self.ORANGE + self.BOLD
        self.ERROR_BOLD = self.ERROR + self.BOLD
        self.PURPLE_BOLD = self.PURPLE + self.BOLD

        # Selection style
        self.SELECTION = self.BG_SELECTED + self.FG_PRIMARY

    def status_color(self, state: str) -> str:
        """Map torrent state to semantic color."""
        if state in STATE_DOWNLOAD:
            return self.CYAN_BOLD
        elif state in STATE_UPLOAD:
            return self.BLUE
        elif state in STATE_PAUSED:
            return self.FG_SECONDARY
        elif state in STATE_ERROR:
            return self.ERROR_BOLD
        elif state in STATE_CHECKING:
            return self.BLUE
        elif state in STATE_COMPLETED:
            return self.GREEN
        return self.FG_PRIMARY

LOCAL_TZ = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
PRESET_FILE = Path(__file__).parent.parent / "config" / "qbit-filter-presets.yml"
TRACKER_REGISTRY_FILE = Path(__file__).parent.parent / "config" / "tracker-registry.yml"
TRACKERS_LIST_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
QC_TAG_TOOL = Path(__file__).resolve().parent / "media_qc_tag.py"
QC_LOG_DIR = Path.home() / ".logs" / "media_qc"
ACTIVE_QC_PROCESSES = {}  # hash -> (pid, start_time)
ACTIVE_MI_PROCESSES = {}  # hash -> (Popen, log_file, start_time)

MEDIA_EXTS = {
    # Video
    ".3g2", ".3gp", ".asf", ".asx", ".avi", ".divx", ".f4v", ".flv", ".m2p", ".m2ts",
    ".m2v", ".m4v", ".mjp", ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts",
    ".ogm", ".ogv", ".qt", ".rm", ".rmvb", ".swf", ".ts", ".vob", ".webm", ".wmv",
    ".xvid",
    # Audio
    ".aa3", ".aac", ".ac3", ".acm", ".adts", ".aif", ".aifc", ".aiff", ".amr", ".ape",
    ".au", ".caf", ".dts", ".flac", ".fla", ".m4a", ".m4b", ".m4p", ".mid", ".mka",
    ".mod", ".mp2", ".mp3", ".mp4", ".mpc", ".oga", ".ogg", ".opus", ".ra", ".ram",
    ".wav", ".wma", ".wv"
}

STATUS_MAPPING = [
    {"code": "A",   "api": "allocating",          "group": "downloading", "desc": "Allocating space"},
    {"code": "A",   "api": "preallocating",       "group": "downloading", "desc": "Preallocating"},
    {"code": "D",   "api": "downloading",         "group": "downloading", "desc": "Downloading"},
    {"code": "CD",  "api": "checkingDL",          "group": "checking",    "desc": "Checking Download"},
    {"code": "FD",  "api": "forcedDL",            "group": "downloading", "desc": "Forced Download"},
    {"code": "MD",  "api": "metaDL",              "group": "downloading", "desc": "Downloading Metadata"},
    {"code": "FMD", "api": "forcedMetaDL",        "group": "downloading", "desc": "Forced Metadata DL"},
    {"code": "PD",  "api": "pausedDL",            "group": "paused",      "desc": "Paused Download"},
    {"code": "PD",  "api": "stoppedDL",           "group": "paused",      "desc": "Stopped Download"}, # v5 alias
    {"code": "QD",  "api": "queuedDL",            "group": "downloading", "desc": "Queued Download"},
    {"code": "SD",  "api": "stalledDL",           "group": "downloading", "desc": "Stalled Download"},
    {"code": "E",   "api": "error",               "group": "error",       "desc": "Error"},
    {"code": "MF",  "api": "missingFiles",        "group": "error",       "desc": "Missing Files"},
    {"code": "U",   "api": "uploading",           "group": "uploading",   "desc": "Uploading"},
    {"code": "CU",  "api": "checkingUP",          "group": "checking",    "desc": "Checking Upload"},
    {"code": "FU",  "api": "forcedUP",            "group": "uploading",   "desc": "Forced Upload"},
    {"code": "PU",  "api": "pausedUP",            "group": "paused",      "desc": "Paused Upload"},
    {"code": "PU",  "api": "stoppedUP",           "group": "paused",      "desc": "Stopped Upload"}, # v5 alias
    {"code": "QU",  "api": "queuedUP",            "group": "uploading",   "desc": "Queued Upload"},
    {"code": "SU",  "api": "stalledUP",           "group": "uploading",   "desc": "Stalled Upload"},
    {"code": "QC",  "api": "queuedForChecking",   "group": "checking",    "desc": "Queued for Checking"},
    {"code": "CR",  "api": "checkingResumeData",  "group": "checking",    "desc": "Checking Resume Data"},
    {"code": "C",   "api": "checking",            "group": "checking",    "desc": "Checking"},
    {"code": "MV",  "api": "moving",              "group": "other",       "desc": "Moving"},
    {"code": "?",   "api": "unknown",             "group": "other",       "desc": "Unknown"},
    {"code": "OK",  "api": "completed",           "group": "completed",   "desc": "Completed"},
]

# Generate derived lookups
STATE_CODE = {item["api"]: item["code"] for item in STATUS_MAPPING}
STATE_DOWNLOAD = {item["api"] for item in STATUS_MAPPING if item["group"] == "downloading"}
STATE_UPLOAD = {item["api"] for item in STATUS_MAPPING if item["group"] == "uploading"}
STATE_PAUSED = {item["api"] for item in STATUS_MAPPING if item["group"] == "paused"}
STATE_ERROR = {item["api"] for item in STATUS_MAPPING if item["group"] == "error"}
STATE_CHECKING = {item["api"] for item in STATUS_MAPPING if item["group"] == "checking"}
STATE_COMPLETED = {item["api"] for item in STATUS_MAPPING if item["group"] == "completed"}

# Build lookup maps
API_TERM_MAP = {item["api"].lower(): item["api"] for item in STATUS_MAPPING}

# Build filter map
STATUS_FILTER_MAP = {
    "downloading": STATE_DOWNLOAD,
    "seeding": STATE_UPLOAD,
    "completed": STATE_COMPLETED,
    "paused": STATE_PAUSED,
    "errored": STATE_ERROR,
    "checking": STATE_CHECKING,
    "stalleddownloading": {"stalledDL"},
    "stalleduploading": {"stalledUP"},
    "stalled": {"stalledDL", "stalledUP"},
    "active": STATE_DOWNLOAD | STATE_UPLOAD,
    "inactive": STATE_PAUSED | {"stalledDL", "stalledUP"},
}


STASHED_KEY = ""
NEED_RESIZE = False

def handle_winch(signum, frame):
    global NEED_RESIZE
    NEED_RESIZE = True

def read_input_queue() -> list[str]:
    """Read all pending input and return a list of mapped keys."""
    keys = []
    fd = sys.stdin.fileno()
    while True:
        # Non-blocking check for input
        r, _, _ = select.select([fd], [], [], 0)
        if not r:
            break
            
        try:
            b = os.read(fd, 1)
            if not b:
                break
        except (EOFError, OSError):
            break
            
        ch = b.decode('utf-8', errors='ignore')
        if ch == "\x1b":
            # Peek for sequence
            seq = ""
            start = time.monotonic()
            while (time.monotonic() - start) < 0.3:
                # Wait up to 100ms for next character in sequence
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        c_b = os.read(fd, 1)
                        if not c_b:
                            break
                        c = c_b.decode('utf-8', errors='ignore')
                        seq += c
                        # Common terminators for ANSI sequences
                        if seq.endswith(("A", "B", "C", "D", "H", "F", "Z", "~")):
                            break
                        # Catch-all for other terminators
                        if len(seq) > 1 and seq[-1].isalpha() and seq[-1] not in "O[": 
                            break
                    except (EOFError, OSError):
                        break
                else:
                    # No more data currently available
                    break
            
            if not seq:
                # Discard lone ESC to prevent arrow key leakage
                pass
            elif seq in ("[Z", "[1;2Z", "[1;2I"):
                keys.append("SHIFT_TAB")
            elif seq in ("[A", "OA"):
                keys.append("'")
            elif seq in ("[B", "OB"):
                keys.append("/")
            elif seq in ("[C", "OC"):
                keys.append(".")   # Right arrow → page next
            elif seq in ("[D", "OD"):
                keys.append(",")   # Left arrow → page prev
            elif seq.startswith("[1;5") or seq.startswith("[1;6"):
                if seq.endswith("I") or seq.endswith("Z"):
                    keys.append("CTRL_TAB")
            # All other sequences are intentionally ignored/swallowed
        else:
            keys.append(ch)
    return keys

def get_key() -> str:
    # Deprecated compatibility wrapper if needed, but we will remove calls to it
    # For read_line, it expects to call input(), so we don't need this.
    # We'll leave a dummy or remove it.
    return ""



def read_line(prompt: str) -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        cooked = termios.tcgetattr(fd)
        cooked[3] |= termios.ICANON | termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, cooked)
        return input(prompt)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def terminal_width() -> int:
    # Prefer live TTY width (works better in tmux than env-based sizing).
    for fd in (sys.stdout.fileno(), sys.stdin.fileno()):
        try:
            packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            if cols and cols > 0:
                return max(40, int(cols))
        except Exception:
            pass
    try:
        return max(40, shutil.get_terminal_size((100, 20)).columns)
    except Exception:
        return 100


def terminal_width_raw() -> int:
    for fd in (sys.stdout.fileno(), sys.stdin.fileno()):
        try:
            packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            if cols and cols > 0:
                return max(10, int(cols))
        except Exception:
            pass
    try:
        return max(10, shutil.get_terminal_size((100, 20)).columns)
    except Exception:
        return 100


def read_qbit_config(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    if yaml is not None:
        try:
            data = yaml.safe_load(path.read_text()) or {}
            qb = (data.get("downloaders") or {}).get("qbittorrent", {}) or {}
            return qb.get("api_url", "") or "", qb.get("credentials_file", "") or ""
        except Exception:
            pass

    api_url = ""
    creds = ""
    in_downloaders = False
    in_qbit = False
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_downloaders = line.strip() == "downloaders:"
            in_qbit = False
            continue
        if in_downloaders and line.startswith("  ") and line.strip().endswith(":"):
            in_qbit = line.strip() == "qbittorrent:"
            continue
        if in_downloaders and in_qbit and line.strip().startswith("api_url:"):
            api_url = line.split("api_url:", 1)[1].strip()
        if in_downloaders and in_qbit and line.strip().startswith("credentials_file:"):
            creds = line.split("credentials_file:", 1)[1].strip()
    return api_url, creds


def read_credentials(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    username = ""
    password = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("QBITTORRENTAPI_USERNAME="):
            username = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("QBITTORRENTAPI_PASSWORD="):
            password = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("QBITTORRENT_USERNAME="):
            username = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("QBITTORRENT_PASSWORD="):
            password = line.split("=", 1)[1].strip().strip('"').strip("'")
    return username, password


def make_opener() -> urllib.request.OpenerDirector:
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def qbit_login(opener: urllib.request.OpenerDirector, api_url: str, username: str, password: str) -> bool:
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(f"{api_url}/api/v2/auth/login", data=data, method="POST")
    try:
        with opener.open(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    return body == "Ok." or body.strip() == ""


def qbit_request(opener: urllib.request.OpenerDirector, api_url: str, method: str, path: str, params: dict | None = None) -> str:
    url = f"{api_url}{path}"
    data = None
    if params:
        encoded = urllib.parse.urlencode(params)
        if method.upper() == "GET":
            url = f"{url}?{encoded}"
        else:
            data = encoded.encode()
    req = urllib.request.Request(url, data=data, method=method.upper())
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return f"HTTP {exc.code}: {body}".strip()
    except Exception as exc:
        return f"Error: {exc}"


def fetch_public_trackers(url: str) -> list[str]:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    trackers = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        trackers.append(line)
    return trackers


def spawn_media_qc(hash_value: str) -> str:
    if not QC_TAG_TOOL.exists():
        return f"Missing tool ({QC_TAG_TOOL})"

    # Clean up completed processes
    for h in list(ACTIVE_QC_PROCESSES.keys()):
        pid, _ = ACTIVE_QC_PROCESSES[h]
        try:
            os.kill(pid, 0)  # Check if process exists (doesn't actually kill)
        except OSError:
            # Process doesn't exist, remove from tracking
            del ACTIVE_QC_PROCESSES[h]

    # Check if QC is already running for this hash
    if hash_value in ACTIVE_QC_PROCESSES:
        pid, start_time = ACTIVE_QC_PROCESSES[hash_value]
        elapsed = int(time.time() - start_time)
        return f"QC already running (PID {pid}, {elapsed}s ago)"

    QC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = QC_LOG_DIR / f"qc_tag_{hash_value[:8]}.log"
    cmd = [sys.executable, str(QC_TAG_TOOL), "--hash", hash_value, "--apply"]
    with log_path.open("a") as handle:
        handle.write(f"\n=== qc-tag-media {hash_value} @ {datetime.now(LOCAL_TZ).isoformat()} ===\n")
        handle.write(f"cmd={' '.join(cmd)}\n")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_path.open("a"),
            stderr=log_path.open("a"),
            start_new_session=True,
        )
        # Track this process
        ACTIVE_QC_PROCESSES[hash_value] = (proc.pid, time.time())
    except Exception as exc:
        return f"Failed ({exc})"
    return f"Queued (PID {proc.pid}, log: {log_path})"


def run_macro(macro: dict, hash_value: str) -> str:
    """Execute macro command with hash substitution.

    Args:
        macro: Dict with 'name', 'cmd', 'desc' keys
        hash_value: 40-char torrent hash

    Returns:
        Status message for banner display
    """
    cmd_template = macro.get("cmd", "")
    if not cmd_template:
        return "Macro missing 'cmd' field"

    # Substitute {hash} placeholder
    cmd_str = cmd_template.replace("{hash}", hash_value)

    # Create log directory
    MACRO_LOG_DIR = Path.home() / ".logs" / "qbit_macros"
    MACRO_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Log file: macro-name_hash8_timestamp.log
    timestamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
    safe_name = macro["name"].replace("/", "-")
    log_path = MACRO_LOG_DIR / f"{safe_name}_{hash_value[:8]}_{timestamp}.log"

    # Write command to log
    with log_path.open("w") as handle:
        handle.write(f"=== Macro: {macro['name']} @ {datetime.now(LOCAL_TZ).isoformat()} ===\n")
        handle.write(f"Hash: {hash_value}\n")
        handle.write(f"Command: {cmd_str}\n\n")

    try:
        # Run in bash from repo root so macros can use repo-relative paths consistently.
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(
            ["bash", "-lc", cmd_str],
            stdout=log_path.open("a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(repo_root),
        )
        # Detect immediate failures to avoid false "Started" messages.
        time.sleep(0.2)
        rc = proc.poll()
        if rc is None:
            return f"{macro['name']}: Started (PID {proc.pid}, log: {log_path.name})"
        if rc == 0:
            return f"{macro['name']}: Completed (log: {log_path.name})"
        return f"{macro['name']}: Failed (exit {rc}, log: {log_path.name})"
    except Exception as exc:
        return f"{macro['name']}: Failed ({exc})"


def state_group(state: str) -> str:
    s = state or ""
    if s in STATE_ERROR:
        return "error"
    if s in STATE_PAUSED:
        return "paused"
    if s in STATE_DOWNLOAD:
        return "downloading"
    if s in STATE_UPLOAD:
        return "uploading"
    if s in STATE_COMPLETED:
        return "completed"
    if s in STATE_CHECKING:
        return "checking"
    if s.startswith("queued"):
        return "queued"
    return "other"


# Legacy wrapper functions - will be removed after migration
# Note: These require colors to be initialized in main()
def status_color(state: str, colors_instance=None) -> str:
    """Deprecated: Use colors.status_color() instead."""
    if colors_instance:
        return colors_instance.status_color(state)
    # Fallback for backward compatibility during migration
    s = (state or "").strip()
    if s in STATE_ERROR:
        return "\033[38;2;224;108;117m"  # error
    if s in STATE_DOWNLOAD:
        return "\033[38;2;78;201;176m\033[1m"  # cyan_bold
    if s in STATE_UPLOAD:
        return "\033[38;2;97;175;239m"  # blue
    if s in STATE_PAUSED:
        return "\033[38;2;160;174;192m"  # fg_secondary
    if s in STATE_COMPLETED:
        return "\033[38;2;152;195;121m"  # green
    if s in STATE_CHECKING:
        return "\033[38;2;97;175;239m"  # blue
    return "\033[38;2;229;231;235m"  # fg_primary


def mode_color(mode: str, colors_instance=None) -> str:
    """Deprecated: Use colors directly instead."""
    if not colors_instance:
        # Fallback for backward compatibility during migration
        mode_map = {
            "i": "\033[96m",  # bright_cyan
            "p": "\033[93m",  # bright_yellow
            "d": "\033[91m",  # bright_red
            "c": "\033[38;5;214m",  # orange
            "t": "\033[38;5;210m",  # pink
            "v": "\033[94m",  # bright_blue
            "A": "\033[92m",  # bright_green
            "Q": "\033[95m",  # bright_purple
            "l": "\033[97m",  # bright_white
            "m": "\033[35m",  # magenta
        }
        return mode_map.get(mode, "\033[0m")

    # Use new color scheme
    mode_map = {
        "i": colors_instance.CYAN_BOLD,
        "p": colors_instance.YELLOW_BOLD,
        "d": colors_instance.ERROR_BOLD,
        "c": colors_instance.ORANGE,
        "t": colors_instance.PURPLE,
        "v": colors_instance.BLUE_BOLD,
        "A": colors_instance.GREEN_BOLD,
        "Q": colors_instance.PURPLE_BOLD,
        "l": colors_instance.FG_PRIMARY,
        "m": colors_instance.PURPLE,
    }
    return mode_map.get(mode, colors_instance.RESET)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(value: str) -> int:
    """Calculate display width by stripping ANSI codes."""
    return len(ANSI_RE.sub("", value))


def wrap_ansi(value: str, width: int) -> list[str]:
    if width <= 0:
        return [value]
    lines = []
    current = ""
    for chunk in value.split(" "):
        if not current:
            current = chunk
            continue
        if visible_len(current) + 1 + visible_len(chunk) <= width:
            current = current + " " + chunk
        else:
            lines.append(current)
            current = chunk
    if current:
        lines.append(current)
    return lines


def size_str(value: int | float | None) -> str:
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


def speed_str(value: int | float | None) -> str:
    if value is None:
        return "-"
    try:
        value = float(value)
    except Exception:
        return "-"
    if value <= 0:
        return "0"
    return f"{size_str(value)}/s"


def eta_str(value: int | None) -> str:
    if value is None:
        return "-"
    try:
        value = int(value)
    except Exception:
        return "-"
    if value <= 0 or value >= 8640000:
        return "-"
    mins, sec = divmod(value, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h{mins:02d}m"
    return f"{mins}m"


def truncate(value: str, max_len: int) -> str:
    if visible_len(value) <= max_len:
        return value
    if max_len <= 1:
        return "~"
    # Walk raw string counting only visible (non-ANSI) chars to find cut point
    visible = 0
    raw_pos = 0
    while raw_pos < len(value) and visible < max_len - 1:
        m = ANSI_RE.match(value, raw_pos)
        if m:
            raw_pos = m.end()
            continue
        visible += 1
        raw_pos += 1
    return value[:raw_pos] + "\x1b[0m~"


def truncate_mid(value: str, max_len: int) -> str:
    """Truncate from the middle, showing beginning and end of long plain strings."""
    if len(value) <= max_len:
        return value
    if max_len <= 4:
        return value[:max_len - 1] + "~"
    left_len = (max_len - 1) * 3 // 5   # ~60% left
    right_len = max_len - 1 - left_len   # ~40% right
    return value[:left_len] + "~" + value[len(value) - right_len:]


CACHE_DIR = Path(os.environ.get("QBIT_MEDIAINFO_CACHE_DIR", "") or (Path.home() / ".logs" / "media_qc" / "cache" / "mediainfo"))
MI_CACHE_MAX_ITEMS = 1000  # Limit queue to 1000 items to prevent memory leak
MI_CACHE_MAX_AGE_SECONDS = 86400  # 24 hours


def get_content_path(torrent_raw: dict) -> str:
    """Extract content path from torrent metadata."""
    content_path = torrent_raw.get("content_path")
    if not content_path:
        save_path = torrent_raw.get("save_path") or ""
        item_name = torrent_raw.get("name") or ""
        content_path = str(Path(save_path) / item_name) if save_path and item_name else ""
    return content_path


def get_largest_media_file(content_path: str) -> Optional[Path]:
    if not content_path:
        return None
    path = Path(content_path)
    if not path.exists():
        return None
    
    files = []
    if path.is_file():
        if path.suffix.lower() in MEDIA_EXTS:
            files.append(path)
    else:
        for item_path in path.rglob("*"):
            if item_path.is_file() and item_path.suffix.lower() in MEDIA_EXTS:
                files.append(item_path)
    
    if not files:
        return None
    
    # Sort by size descending
    files.sort(key=lambda x: x.stat().st_size, reverse=True)
    return files[0]


def get_mediainfo_summary(hash_value: str, content_path: str) -> str:
    if not hash_value:
        return "ERROR: Missing hash"
    
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{hash_value}.summary"

    if cache_file.exists():
        val = cache_file.read_text().strip()
        # Invalidate old error messages or formats without dots
        if val and " • " in val and not val.startswith("MediaInfo"):
            return val

    if hash_value in ACTIVE_MI_PROCESSES:
        return "MI: loading..."

    target = get_largest_media_file(content_path)
    if not target:
        mi_summary = "No media content."
        cache_file.write_text(mi_summary)
        return mi_summary

    tool = shutil.which("mediainfo")
    if not tool:
        return "ERROR: mediainfo not found"

    # Template covers General, Video, and Audio tracks.
    inform = "General;%Format%|%Duration/String3%|%OverallBitRate/String%|Video;%Width%x%Height% %Format% %BitRate/String%|Audio;%Format% %Channel(s)%ch"

    # Start process in background
    try:
        # We'll use a temporary file for the output to avoid pipe deadlocks and keep it truly non-blocking
        out_path = CACHE_DIR / f"{hash_value}.tmp"
        handle = out_path.open("w")
        proc = subprocess.Popen(
            [tool, f"--Inform={inform}", str(target)],
            stdout=handle,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        ACTIVE_MI_PROCESSES[hash_value] = (proc, handle, time.time())
        return "MI: loading..."
    except Exception as exc:
        return f"MI: error ({exc})"


def get_mediainfo_summary_cached(hash_value: str, content_path: str, background_only: bool = False) -> str:
    cache_file = CACHE_DIR / f"{hash_value}.summary"
    if cache_file.exists():
        return cache_file.read_text().strip()
    if background_only:
        return "MI: loading..."
    return get_mediainfo_summary(hash_value, content_path)


def get_mediainfo_for_hash(hash_value: str, content_path: str) -> str:
    if not hash_value:
        return "ERROR: Missing hash"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{hash_value}.txt"

    if cache_file.exists():
        return cache_file.read_text()

    if not content_path:
        return "ERROR: No content path"
    
    path = Path(content_path)
    if not path.exists():
        return f"ERROR: Path not found ({path})"

    files = []
    if path.is_file():
        files = [path]
    else:
        for item_path in sorted(path.rglob("*")):
            if item_path.is_file() and item_path.suffix.lower() in MEDIA_EXTS:
                files.append(item_path)
    
    if not files:
        return "ERROR: No media files found"

    table = mediainfo_table(files)
    cache_file.write_text(table)
    return table


def mediainfo_table(paths: list[Path]) -> str:
    tool = shutil.which("mediainfo")
    if not tool:
        return "ERROR: mediainfo not found"

    # Raw numeric fields for clean formatting; no embedded units in data
    inform = (
        "General;%FileName%|%Duration/String3%|%FileSize%|%OverallBitRate%|"
        "%Format%|%Width%x%Height%|%FrameRate%|%Channel(s)%|%SamplingRate%\n"
    )

    # (header, unit, fmt_fn, cap, align)  align: "l"=left, "r"=right
    def _fmt_mib(v: str) -> str:
        try: return str(int(v.replace(",", "").replace(" ", "")) >> 20)
        except Exception: return v

    def _fmt_kbps(v: str) -> str:
        try: return str(int(v.replace(",", "").replace(" ", "")) // 1000)
        except Exception: return v

    def _fmt_fps(v: str) -> str:
        try:
            f = float(v)
            s = f"{f:.3f}".rstrip("0").rstrip(".")
            return s
        except Exception: return v

    def _fmt_khz(v: str) -> str:
        try: return f"{int(v.replace(',', '').replace(' ', '')) / 1000:.1f}"
        except Exception: return v

    COLS = [
        ("File",     "",      None,      40, "l"),
        ("Duration", "h:m:s", None,      11, "l"),
        ("Size",     "MiB",   _fmt_mib,   7, "r"),
        ("BR",       "kb/s",  _fmt_kbps,  7, "r"),
        ("Fmt",      "",      None,       8, "l"),
        ("WxH",      "px",    None,      11, "l"),
        ("FPS",      "fps",   _fmt_fps,   6, "r"),
        ("Ch",       "#",     None,       3, "r"),
        ("kHz",      "kHz",   _fmt_khz,   6, "r"),
    ]
    n_cols = len(COLS)

    rows = []
    for path in paths:
        result = subprocess.run(
            [tool, f"--Inform={inform}", str(path)],
            capture_output=True,
            text=True,
        )
        line = (result.stdout or "").strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < n_cols:
            parts += [""] * (n_cols - len(parts))
        # Apply format helpers (skip File col 0 and Duration col 1)
        formatted = []
        for i, (_, _, fmt_fn, _, _) in enumerate(COLS):
            val = parts[i] if i < len(parts) else ""
            if fmt_fn is not None:
                val = fmt_fn(val.strip())
            formatted.append(val.strip())
        rows.append(formatted)

    if not rows:
        return "No mediainfo output"

    # Dynamic column widths: max of header/unit/data, capped
    widths = []
    for i, (hdr, unit, _, cap, _) in enumerate(COLS):
        data_max = max(len(r[i]) for r in rows) if rows else 0
        widths.append(min(cap, max(len(hdr), len(unit), data_max)))

    # V2 pipe-delimited format
    hdr_line = "QBITUI_MI_V2|" + "|".join(hdr for hdr, _, _, _, _ in COLS)
    unit_line = "|".join(unit for _, unit, _, _, _ in COLS)
    data_lines = ["|".join(r) for r in rows]
    return "\n".join([hdr_line, unit_line] + data_lines)


def added_str(value: int | float | None) -> str:
    if value is None:
        return "-"
    try:
        value = int(value)
    except Exception:
        return "-"
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def added_short_str(value: int | float | None) -> str:
    if value is None:
        return "-"
    try:
        value = int(value)
    except Exception:
        return "-"
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value, LOCAL_TZ).strftime("%m-%d %H:%M")


def format_ts(value: int | float | None) -> str:
    if value is None:
        return "-"
    try:
        value = int(value)
    except Exception:
        return "-"
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value, LOCAL_TZ).isoformat()


def summary(torrents: list[dict]) -> str:
    counts = {"down": 0, "up": 0, "paused": 0, "error": 0, "completed": 0, "other": 0}
    for t in torrents:
        group = state_group(t.get("state", ""))
        if group == "downloading":
            counts["down"] += 1
        elif group == "uploading":
            counts["up"] += 1
        elif group == "paused":
            counts["paused"] += 1
        elif group == "error":
            counts["error"] += 1
        elif group == "completed":
            counts["completed"] += 1
        else:
            counts["other"] += 1
    return " ".join([f"{k}:{v}" for k, v in counts.items()])


def _fmt_cache_status_line(cache_info: dict, colors: ColorScheme) -> str:
    """Compact one-line cache status for the header."""
    if not cache_info.get("enabled"):
        return f"{colors.FG_TERTIARY}Cache: OFF (direct API){colors.RESET}"
    dot = f"{colors.GREEN}●{colors.RESET}" if cache_info.get("daemon_running") else f"{colors.ERROR}○{colors.RESET}"
    path_short = str(cache_info.get("base_path", "")).replace(str(Path.home()), "~")
    interval = cache_info.get("interval_s")
    interval_str = f"{float(interval):.0f}s" if interval is not None else "?"
    hits = cache_info.get("cache_hits", 0)
    direct = cache_info.get("direct_hits", 0)
    total = hits + direct
    hit_pct = f"{(hits / total * 100):.0f}%" if total > 0 else "--"
    age = cache_info.get("cache_age_s")
    age_str = f"age {float(age):.1f}s" if age is not None else "age ?"
    items = cache_info.get("items")
    items_str = f"  {colors.FG_TERTIARY}{items} items{colors.RESET}" if items is not None else ""
    err = str(cache_info.get("last_error") or "")
    err_str = f"  {colors.ERROR}err:{err[:25]}{colors.RESET}" if err else ""
    leases = cache_info.get("active_leases", 0)
    leases_str = f"  {colors.FG_TERTIARY}{leases}L{colors.RESET}"
    return (
        f"{colors.FG_SECONDARY}Cache:{colors.RESET} {dot} {colors.FG_TERTIARY}{path_short}{colors.RESET}"
        f"  {colors.FG_SECONDARY}every {colors.YELLOW}{interval_str}{colors.RESET}"
        f"  {colors.FG_SECONDARY}↑cache {colors.CYAN}{hits}{colors.RESET}"
        f"  {colors.FG_SECONDARY}↓qbit {colors.BLUE}{direct}{colors.RESET}"
        f"  {colors.FG_SECONDARY}hit {colors.GREEN}{hit_pct}{colors.RESET}"
        f"  {colors.FG_TERTIARY}{age_str}{colors.RESET}"
        f"{items_str}{leases_str}{err_str}"
    )


def draw_header_full_compact(
    colors: ColorScheme,
    api_url: str,
    version: str,
    torrents: list[dict],
    scope: str,
    sort_field: str,
    sort_desc: bool,
    page: int,
    total_pages: int,
    filters: list[dict],
    width: int,
    cache_info: dict | None = None,
) -> list[str]:
    def short(value: str) -> str:
        return truncate(value, width)

    downloading = sum(1 for t in torrents if t.get("state") in STATE_DOWNLOAD)
    seeding = sum(1 for t in torrents if t.get("state") in STATE_UPLOAD)
    paused = sum(1 for t in torrents if t.get("state") in STATE_PAUSED)
    completed = sum(1 for t in torrents if (t.get("progress") or 0) >= 1.0)
    errors = sum(1 for t in torrents if t.get("state") in STATE_ERROR)

    total_dl = sum(t.get("dlspeed", 0) or 0 for t in torrents)
    total_ul = sum(t.get("upspeed", 0) or 0 for t in torrents)

    def fmt_mib(v: int | float | None) -> str:
        try:
            x = float(v or 0) / (1024 * 1024)
        except Exception:
            x = 0.0
        if x >= 100:
            return f"{x:,.0f}"
        if x >= 10:
            return f"{x:,.1f}".rstrip("0").rstrip(".")
        return f"{x:.1f}".rstrip("0").rstrip(".") or "0"

    scope_display = scope.upper() if scope != "all" else "ALL"
    sort_arrow = "↓" if sort_desc else "↑"
    active_filters = len([f for f in filters if f.get("enabled", True)])

    line1 = (
        f"{colors.CYAN_BOLD}QBTUI{colors.RESET} {colors.FG_SECONDARY}v{version}{colors.RESET}  "
        f"{colors.FG_SECONDARY}@ {colors.BLUE}{api_url}{colors.RESET}  "
        f"{colors.FG_SECONDARY}{datetime.now().strftime('%Y-%m-%d')}{colors.RESET}"
    )
    line2 = (
        f"{colors.CYAN}DL MiB/s{colors.RESET} {colors.CYAN_BOLD}{fmt_mib(total_dl)}{colors.RESET}  "
        f"{colors.BLUE}UL MiB/s{colors.RESET} {colors.BLUE_BOLD}{fmt_mib(total_ul)}{colors.RESET}  "
        f"{colors.FG_SECONDARY}Dn:{downloading} Up:{seeding} Pa:{paused} Ok:{completed} Err:{errors}{colors.RESET}"
    )
    _left3 = (
        f"{colors.FG_SECONDARY}Scope:{colors.RESET} {colors.YELLOW_BOLD}{scope_display}{colors.RESET}  "
        f"{colors.FG_SECONDARY}Sort:{colors.RESET} {colors.YELLOW}{sort_field} {sort_arrow}{colors.RESET}  "
        f"{colors.FG_SECONDARY}Filters:{active_filters}{colors.RESET}"
    )
    _pg_plain = f"Pg:{page + 1}/{total_pages}"
    _pg_colored = f"{colors.FG_SECONDARY}{_pg_plain}{colors.RESET}"
    _pad3 = max(0, width - visible_len(_left3) - len(_pg_plain))
    line3 = _left3 + " " * _pad3 + _pg_colored
    # Cache line sits directly under the server/title line (position 2)
    lines = [short(line1)]
    if cache_info is not None:
        lines.append(short(_fmt_cache_status_line(cache_info, colors)))
    _l2 = short(line2)
    _l3 = short(line3)
    sep_width = max(visible_len(l) for l in lines + [_l2, _l3])
    lines.extend([_l2, _l3, "-" * sep_width])
    return lines


def draw_footer_full_compact(
    colors: ColorScheme,
    width: int,
    has_selection: bool = False,
    macros: list[dict] | None = None,
    sep_width: int = 0,
) -> list[str]:
    def short(value: str) -> str:
        return truncate(value, width)

    if has_selection:
        actions = (
            f"{colors.CYAN_BOLD}P{colors.RESET}{colors.FG_SECONDARY}=Pause  "
            f"{colors.CYAN_BOLD}V{colors.RESET}{colors.FG_SECONDARY}=Verify  "
            f"{colors.CYAN_BOLD}C{colors.RESET}{colors.FG_SECONDARY}=Cat  "
            f"{colors.CYAN_BOLD}E{colors.RESET}{colors.FG_SECONDARY}=Tags  "
            f"{colors.CYAN_BOLD}T{colors.RESET}{colors.FG_SECONDARY}=Trackers  "
            f"{colors.CYAN_BOLD}Q{colors.RESET}{colors.FG_SECONDARY}=QC  "
            f"{colors.ORANGE_BOLD}D{colors.RESET}{colors.FG_SECONDARY}=Delete  "
            f"{colors.CYAN_BOLD}Tab{colors.RESET}{colors.FG_SECONDARY}=Content tabs{colors.RESET}"
        )
        actions_lbl = f"{colors.YELLOW_BOLD}Actions:{colors.RESET}"
    else:
        actions = f"{colors.YELLOW}(select a torrent to enable actions){colors.RESET}"
        actions_lbl = f"{colors.FG_TERTIARY}Actions:{colors.RESET}"
    line1 = f"{actions_lbl} {actions}"
    line2 = (
        f"{colors.FG_SECONDARY}Nav:{colors.RESET} "
        f"{colors.YELLOW_BOLD}0-9/Space/Enter{colors.RESET}{colors.FG_SECONDARY}=select  "
        f"{colors.YELLOW_BOLD}↑↓ '/{colors.RESET}{colors.FG_SECONDARY}=move  "
        f"{colors.YELLOW_BOLD}←→ ,.{colors.RESET}{colors.FG_SECONDARY}=page  "
        f"{colors.PURPLE_BOLD}~{colors.RESET}{colors.FG_SECONDARY}=back/clear  "
        f"{colors.PURPLE_BOLD}?{colors.RESET}{colors.FG_SECONDARY}=help  "
        f"{colors.PURPLE_BOLD}i{colors.RESET}{colors.FG_SECONDARY}=cache  "
        f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=quit{colors.RESET}"
    )
    def _k3(key: str, label: str, key_color: str) -> str:
        return f"{key_color}{key}{colors.RESET}{colors.FG_SECONDARY}={label}{colors.RESET}"

    _sc = colors.CYAN    # scope keys
    _so = colors.YELLOW  # sort keys
    _fi = colors.ORANGE  # filter keys
    _vi = colors.PURPLE  # view keys
    keys_parts = [
        f"{colors.FG_SECONDARY}Keys:{colors.RESET}",
        _k3("a", "All", _sc), _k3("w", "↓", _sc), _k3("u", "↑", _sc),
        _k3("v", "Pause", _sc), _k3("e", "Done", _sc), _k3("g", "Err", _sc),
        f" {colors.FG_SECONDARY}│{colors.RESET}",
        _k3("s", "sort", _so), _k3("o", "dir", _so),
        f" {colors.FG_SECONDARY}│{colors.RESET}",
        _k3("f", "status", _fi), _k3("c", "cat", _fi), _k3("#", "tag", _fi),
        _k3("l", "text", _fi), _k3("x", "toggle", _fi), _k3("p", "preset", _fi),
        f" {colors.FG_SECONDARY}│{colors.RESET}",
        _k3("t", "tags", _vi), _k3("d", "date", _vi), _k3("h", "hash", _vi),
        _k3("n", "narrow", _vi), _k3("m", "media", _vi), _k3("z", "reset", _vi),
    ]
    line3 = " ".join(keys_parts)
    _l1, _l2, _l3 = short(line1), short(line2), short(line3)
    _sw = sep_width if sep_width > 0 else max(visible_len(_l1), visible_len(_l2), visible_len(_l3))
    lines = ["-" * _sw, _l1, _l2, _l3]
    if macros:
        items = [f"{idx}:{m.get('desc','')[:10]}" for idx, m in enumerate(macros[:9], start=1)]
        line4 = (
            f"{colors.FG_SECONDARY}Macros:{colors.RESET} " +
            "  ".join(items) +
            f"  {colors.FG_TERTIARY}[M menu, Shift+# direct]{colors.RESET}"
        )
        lines.append(short(line4))
    return lines


# ============================================================================
# HEADER & FOOTER REDESIGN (v1.7.0)
# ============================================================================

def draw_header_v2(
    colors: ColorScheme,
    api_url: str,
    version: str,
    torrents: list[dict],
    scope: str,
    sort_field: str,
    sort_desc: bool,
    page: int,
    total_pages: int,
    filters: list[dict],
    width: int,
    cache_info: dict | None = None,
) -> list[str]:
    """
    Render professional 3-line header.

    Line 1: App branding + API endpoint + date
    Line 2: Stats dashboard with real-time bandwidth
    Line 3: Border
    """
    lines = []

    # Calculate stats
    downloading = sum(1 for t in torrents if t.get("state") in STATE_DOWNLOAD)
    seeding = sum(1 for t in torrents if t.get("state") in STATE_UPLOAD)
    paused = sum(1 for t in torrents if t.get("state") in STATE_PAUSED)
    completed = sum(1 for t in torrents if t.get("progress", 0) == 1.0)
    errors = sum(1 for t in torrents if t.get("state") in STATE_ERROR)

    # Calculate bandwidth
    total_dl = sum(t.get("dlspeed", 0) for t in torrents)
    total_ul = sum(t.get("upspeed", 0) for t in torrents)

    def fmt_speed(speed: int) -> str:
        if speed == 0: return "0"
        elif speed < 1024: return f"{speed} B/s"
        elif speed < 1024 * 1024: return f"{speed / 1024:.1f} KB/s"
        else: return f"{speed / (1024 * 1024):.1f} MB/s"

    # Line 1: Title bar (ASCII chars for consistent width)
    left = f"{colors.CYAN_BOLD}* QBTUI{colors.RESET} {colors.FG_SECONDARY}v{version}{colors.RESET}"
    center = f"{colors.FG_SECONDARY}@ {colors.BLUE}{api_url}{colors.RESET}"
    right = f"{colors.FG_SECONDARY}{datetime.now().strftime('%Y-%m-%d')}{colors.RESET}"

    # Calculate spacing using visible_len (accounts for emoji width)
    left_visible = visible_len(left)
    center_visible = visible_len(center)
    right_visible = visible_len(right)

    total_visible = left_visible + center_visible + right_visible
    remaining = (width - 4) - total_visible  # -4 for "│ " and " │"

    if remaining > 0:
        left_pad = remaining // 2
        right_pad = remaining - left_pad
        title_line = f"{left}{' ' * left_pad}{center}{' ' * right_pad}{right}"
    else:
        title_line = f"{left}  {center}  {right}"

    lines.append(f"┌{'─' * (width - 2)}┐")
    lines.append(f"│ {title_line} │")
    # Cache status line directly under title, before the stats separator
    if cache_info is not None:
        _cl = _fmt_cache_status_line(cache_info, colors)
        _cl_pad = max(0, (width - 4) - visible_len(_cl))
        lines.append(f"│ {_cl}{' ' * _cl_pad} │")
    lines.append(f"├{'─' * (width - 2)}┤")

    # Line 2: Stats dashboard
    stats = []

    # Real-time bandwidth (if active) - always show both for consistency
    if total_dl > 0 or total_ul > 0:
        stats.append(f"{colors.CYAN}*{colors.RESET}")
        dl_color = colors.CYAN_BOLD if total_dl > 0 else colors.FG_TERTIARY
        stats.append(f"{colors.CYAN}↓ {dl_color}{fmt_speed(total_dl)}{colors.RESET}")
        ul_color = colors.BLUE_BOLD if total_ul > 0 else colors.FG_TERTIARY
        stats.append(f"{colors.BLUE}↑ {ul_color}{fmt_speed(total_ul)}{colors.RESET}")
        stats.append(f"{colors.FG_SECONDARY}│{colors.RESET}")

    # Counts
    stats.append(f"{colors.CYAN}↓ {colors.CYAN_BOLD}{downloading}{colors.RESET}")
    stats.append(f"{colors.BLUE}↑ {colors.BLUE_BOLD}{seeding}{colors.RESET}")
    stats.append(f"{colors.FG_SECONDARY}⏸ {paused}{colors.RESET}")
    stats.append(f"{colors.GREEN}✓ {colors.GREEN_BOLD}{completed}{colors.RESET}")

    if errors > 0:
        stats.append(f"{colors.ERROR}✗ {colors.ERROR_BOLD}{errors}{colors.RESET}")
    else:
        stats.append(f"{colors.FG_TERTIARY}{colors.DIM}✗ 0{colors.RESET}")

    stats.append(f"{colors.FG_SECONDARY}│{colors.RESET}")

    # Scope/Sort/Pagination
    scope_display = scope.upper() if scope != "all" else "ALL"
    stats.append(f"{colors.FG_SECONDARY}Showing: {colors.YELLOW_BOLD}{scope_display}{colors.RESET}")
    stats.append(f"{colors.FG_SECONDARY}│{colors.RESET}")

    sort_arrow = "↓" if sort_desc else "↑"
    stats.append(f"{colors.FG_SECONDARY}Sort: {colors.YELLOW}{sort_field} {sort_arrow}{colors.RESET}")
    stats.append(f"{colors.FG_SECONDARY}│{colors.RESET}")

    stats.append(f"{colors.FG_SECONDARY}Pg {page + 1}/{total_pages}{colors.RESET}")

    # Active filters indicator
    if filters:
        active = [f for f in filters if f.get("enabled", True)]
        if active:
            stats.append(f"{colors.FG_SECONDARY}│{colors.RESET}")
            stats.append(f"{colors.ORANGE}[{len(active)} filters]{colors.RESET}")

    stats_line = "  ".join(stats)
    # Pad stats line to match border width
    stats_visible = visible_len(stats_line)
    padding_needed = (width - 4) - stats_visible  # -4 for "│ " and " │"
    if padding_needed > 0:
        stats_line += " " * padding_needed
    lines.append(f"│ {stats_line} │")
    lines.append(f"└{'─' * (width - 2)}┘")

    return lines


def draw_header_minimal(
    colors: ColorScheme,
    version: str,
    scope: str,
    page: int,
    total_pages: int,
    width: int
) -> list[str]:
    scope_display = scope.upper() if scope != "all" else "ALL"
    left = f"{colors.CYAN_BOLD}QBTUI{colors.RESET} {colors.FG_SECONDARY}v{version} {scope_display}{colors.RESET}"
    right = f"{colors.FG_SECONDARY}Pg {page + 1}/{total_pages}{colors.RESET}"

    inner_width = max(1, width - 4)
    base = f"{left} {right}"
    if visible_len(base) <= inner_width:
        pad = inner_width - visible_len(left) - visible_len(right)
        line = f"{left}{' ' * max(1, pad)}{right}"
    else:
        line = truncate(base, inner_width)

    return [
        f"│ {line} │",
        "─" * width,
    ]


def draw_footer_v2(
    colors: ColorScheme,
    context: str,
    width: int,
    has_selection: bool = False,
    macros: list[dict] | None = None
) -> list[str]:
    """
    Render context-sensitive grouped footer.

    Args:
        context: 'main', 'trackers', or 'mediainfo'
        width: Terminal width
        has_selection: Whether a torrent is selected
    """
    lines = []
    lines.append(f"┌{'─' * (width - 2)}┐")

    if context == "main":
        # ── Line 1: ACTIONS ──────────────────────────────────────────────────
        # Keys requiring a selection: dim when idle, bright when active.
        if has_selection:
            # Active: bright key + secondary label
            _k = colors.CYAN_BOLD
            _l = colors.FG_SECONDARY
            _del_k = colors.ORANGE_BOLD
            _tab_k = colors.CYAN_BOLD
        else:
            # Idle: everything muted
            _k = colors.FG_TERTIARY + colors.DIM
            _l = colors.FG_TERTIARY + colors.DIM
            _del_k = colors.FG_TERTIARY + colors.DIM
            _tab_k = colors.FG_TERTIARY + colors.DIM

        def _act(key: str, label: str, key_color: str = _k) -> str:
            return f"{key_color}{key}{colors.RESET}{_l}={label}{colors.RESET}"

        actions_parts = [
            _act("P", "Pause"),
            _act("V", "Verify"),
            _act("C", "Category"),
            _act("E", "Tags"),
            _act("T", "Trackers"),
            _act("Q", "QC"),
            _act("D", "Delete", _del_k),
            _act("Tab", "Content tabs", _tab_k),
            _act("M", "Macros", _k),
        ]
        _actions_lbl_color = colors.YELLOW_BOLD if has_selection else colors.FG_TERTIARY
        actions_line = (
            f"{_actions_lbl_color}ACTIONS:{colors.RESET}  " +
            "  ".join(actions_parts)
        )

        # ── Line 2: NAVIGATE ─────────────────────────────────────────────────
        nav_parts = [
            f"{colors.YELLOW_BOLD}0-9/Space/Enter{colors.RESET}{colors.FG_SECONDARY}=select{colors.RESET}",
            f"{colors.YELLOW_BOLD}↑↓ '/{colors.RESET}{colors.FG_SECONDARY}=move{colors.RESET}",
            f"{colors.YELLOW_BOLD}←→ ,.{colors.RESET}{colors.FG_SECONDARY}=page{colors.RESET}",
            f"{colors.PURPLE_BOLD}~{colors.RESET}{colors.FG_SECONDARY}=back/clear{colors.RESET}",
        ]
        global_parts = [
            f"{colors.PURPLE_BOLD}?{colors.RESET}{colors.FG_SECONDARY}=help{colors.RESET}",
            f"{colors.PURPLE_BOLD}i{colors.RESET}{colors.FG_SECONDARY}=cache{colors.RESET}",
            f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=quit{colors.RESET}",
        ]
        nav_line = (
            f"{colors.FG_SECONDARY}NAV:{colors.RESET}  " +
            "  ".join(nav_parts) +
            f"  {colors.FG_SECONDARY}│{colors.RESET}  " +
            "  ".join(global_parts)
        )

        # ── Line 3: KEYS (no-selection required) ─────────────────────────────
        def _k2(key: str, label: str, key_color: str = colors.CYAN) -> str:
            return f"{key_color}{key}{colors.RESET}{colors.FG_SECONDARY}={label}{colors.RESET}"

        _sc = colors.CYAN    # scope keys
        _so = colors.YELLOW  # sort keys
        _fi = colors.ORANGE  # filter keys
        _vi = colors.PURPLE  # view keys
        keys_parts = [
            f"{colors.FG_SECONDARY}Scope:{colors.RESET}",
            _k2("a", "All", _sc), _k2("w", "↓", _sc), _k2("u", "↑", _sc),
            _k2("v", "Pause", _sc), _k2("e", "Done", _sc), _k2("g", "Err", _sc),
            f"  {colors.FG_SECONDARY}Sort:{colors.RESET}",
            _k2("s", "field", _so), _k2("o", "dir", _so),
            f"  {colors.FG_SECONDARY}Filter:{colors.RESET}",
            _k2("f", "status", _fi), _k2("c", "cat", _fi), _k2("#", "tag", _fi),
            _k2("l", "text", _fi), _k2("x", "toggle", _fi), _k2("p", "preset", _fi),
            f"  {colors.FG_SECONDARY}View:{colors.RESET}",
            _k2("t", "tags", _vi), _k2("d", "date", _vi), _k2("h", "hash", _vi),
            _k2("n", "narrow", _vi), _k2("m", "media", _vi), _k2("z", "reset", _vi),
        ]
        keys_line = " ".join(keys_parts)

        # ── Macros ────────────────────────────────────────────────────────────
        all_lines = [actions_line, nav_line, keys_line]
        if macros:
            macro_items = [
                f"{idx}:{macro['desc'][:10]}"
                for idx, macro in enumerate(macros[:9], start=1)
            ]
            macro_line = (
                f"{colors.FG_SECONDARY}MACROS:{colors.RESET} " +
                "  ".join(macro_items) +
                f"  {colors.FG_TERTIARY}[M menu, Shift+# direct]{colors.RESET}"
            )
            all_lines.append(macro_line)

        for ln in all_lines:
            ln_clipped = truncate(ln, width - 4)
            pad = max(0, width - visible_len(ln_clipped) - 4)
            lines.append(f"│ {ln_clipped}{' ' * pad} │")

    elif context == "trackers":
        title_line = f"{colors.CYAN_BOLD}TRACKER VIEW{colors.RESET}"
        padding = width - visible_len(title_line) - 4
        lines.append(f"│ {title_line}{' ' * max(0, padding)} │")

        actions = [
            f"{colors.CYAN_BOLD}R{colors.RESET}{colors.FG_SECONDARY}=Reannounce{colors.RESET}",
        ]

        nav = [
            f"{colors.CYAN_BOLD}Tab{colors.RESET}{colors.FG_SECONDARY}=Next tab{colors.RESET}",
            f"{colors.CYAN_BOLD}Shift-Tab{colors.RESET}{colors.FG_SECONDARY}=Prev tab{colors.RESET}",
            f"{colors.YELLOW_BOLD}←/→{colors.RESET}{colors.FG_SECONDARY}=tab nav{colors.RESET}",
            f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=exit{colors.RESET}",
        ]

        cmd_line = truncate(
            "  ".join(actions) + f"  {colors.FG_SECONDARY}│{colors.RESET}  " + "  ".join(nav),
            width - 4,
        )
        padding = max(0, width - visible_len(cmd_line) - 4)
        lines.append(f"│ {cmd_line}{' ' * padding} │")

    elif context == "mediainfo":
        title_line = f"{colors.LAVENDER}MEDIAINFO VIEW{colors.RESET}"
        padding = width - visible_len(title_line) - 4
        lines.append(f"│ {title_line}{' ' * max(0, padding)} │")

        nav = [
            f"{colors.CYAN_BOLD}Tab{colors.RESET}{colors.FG_SECONDARY}=Next{colors.RESET}",
            f"{colors.CYAN_BOLD}Shift-Tab{colors.RESET}{colors.FG_SECONDARY}=Prev{colors.RESET}",
            f"{colors.YELLOW_BOLD}←/→{colors.RESET}{colors.FG_SECONDARY}=tab nav{colors.RESET}",
            f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=exit{colors.RESET}",
        ]

        cmd_line = truncate("  ".join(nav), width - 4)
        padding = max(0, width - visible_len(cmd_line) - 4)
        lines.append(f"│ {cmd_line}{' ' * padding} │")

    elif context == "info":
        title_line = f"{colors.CYAN_BOLD}INFO VIEW{colors.RESET}"
        _pad = max(0, width - visible_len(title_line) - 4)
        lines.append(f"│ {title_line}{' ' * _pad} │")
        nav = [
            f"{colors.CYAN_BOLD}Tab{colors.RESET}{colors.FG_SECONDARY}=Next tab{colors.RESET}",
            f"{colors.CYAN_BOLD}Shift-Tab{colors.RESET}{colors.FG_SECONDARY}=Prev tab{colors.RESET}",
            f"{colors.YELLOW_BOLD}←/→{colors.RESET}{colors.FG_SECONDARY}=tab nav{colors.RESET}",
            f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=exit{colors.RESET}",
        ]
        cmd_line = truncate("  ".join(nav), width - 4)
        _pad2 = max(0, width - visible_len(cmd_line) - 4)
        lines.append(f"│ {cmd_line}{' ' * _pad2} │")

    elif context == "content":
        title_line = f"{colors.CYAN_BOLD}CONTENT VIEW{colors.RESET}"
        _pad = max(0, width - visible_len(title_line) - 4)
        lines.append(f"│ {title_line}{' ' * _pad} │")
        nav = [
            f"{colors.CYAN_BOLD}Tab{colors.RESET}{colors.FG_SECONDARY}=Next tab{colors.RESET}",
            f"{colors.CYAN_BOLD}Shift-Tab{colors.RESET}{colors.FG_SECONDARY}=Prev tab{colors.RESET}",
            f"{colors.YELLOW_BOLD}←/→{colors.RESET}{colors.FG_SECONDARY}=tab nav{colors.RESET}",
            f"{colors.PURPLE_BOLD}q{colors.RESET}{colors.FG_SECONDARY}=exit{colors.RESET}",
        ]
        cmd_line = truncate("  ".join(nav), width - 4)
        _pad2 = max(0, width - visible_len(cmd_line) - 4)
        lines.append(f"│ {cmd_line}{' ' * _pad2} │")

    lines.append(f"└{'─' * (width - 2)}┘")

    return lines


def load_tracker_keyword_map(path: Path) -> dict[str, str]:
    if not path.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        trackers = data.get("trackers") or {}
        keyword_to_trackers: dict[str, set[str]] = {}
        for tracker_key, tracker_cfg in trackers.items():
            if not isinstance(tracker_cfg, dict):
                continue
            key = str(tracker_key).strip()
            if not key:
                continue
            key_l = key.lower()
            keyword_to_trackers.setdefault(key_l, set()).add(key)
            qbm = tracker_cfg.get("qbitmanage") or {}
            tags = qbm.get("tags") or []
            if isinstance(tags, list):
                for tag in tags:
                    tag_s = str(tag).strip().lower()
                    if tag_s:
                        keyword_to_trackers.setdefault(tag_s, set()).add(key)

        # Keep only unambiguous aliases; this avoids generic shared tags
        # (e.g. "private") mapping to the wrong tracker.
        result: dict[str, str] = {}
        for keyword, owners in keyword_to_trackers.items():
            if len(owners) == 1:
                result[keyword] = next(iter(owners))
        return result
    except Exception:
        return {}


def _short_tracker_name(url: str) -> str:
    """Extract a short display name from a tracker announce URL."""
    if not url:
        return "-"
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        for prefix in ("tracker.", "www.", "bt.", "announce."):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.split(".")[0] if "." in host else host
        return host[:12] if host else "-"
    except Exception:
        return "-"


def resolve_tracker_from_tags(tags_value: str, tracker_keyword_map: dict[str, str]) -> str:
    if not tracker_keyword_map:
        return "-"
    for tag in [t.strip().lower() for t in (tags_value or "").split(",") if t.strip()]:
        if tag in tracker_keyword_map:
            return tracker_keyword_map[tag]
    return "-"


def build_rows(torrents: list[dict], tracker_keyword_map: dict[str, str]) -> list[dict]:
    rows = []
    for t in torrents:
        progress_raw = t.get("progress")
        tags_value = t.get("tags") or "-"
        progress = "-"
        progress_pct = "-"
        if isinstance(progress_raw, (int, float)):
            pct = int(progress_raw * 100)
            progress = f"{pct}%"
            progress_pct = str(pct)
        elif progress_raw is not None:
            progress = str(progress_raw)
        uploaded_raw = t.get("uploaded")
        if not isinstance(uploaded_raw, (int, float)):
            uploaded_raw = t.get("total_uploaded")
        if not isinstance(uploaded_raw, (int, float)):
            uploaded_raw = t.get("uploaded_session")
        seeds_raw = t.get("num_seeds")
        if not isinstance(seeds_raw, (int, float)):
            seeds_raw = t.get("num_complete")
        peers_raw = t.get("num_leechs")
        if not isinstance(peers_raw, (int, float)):
            peers_raw = t.get("num_incomplete")
        _trk = resolve_tracker_from_tags(str(tags_value), tracker_keyword_map)
        if _trk == "-":
            _trk = _short_tracker_name(t.get("tracker") or "")
        rows.append({
            "name": t.get("name", ""),
            "state": t.get("state", ""),
            "st": STATE_CODE.get(t.get("state", ""), "?"),
            "progress": progress,
            "progress_pct": progress_pct,
            "size": size_str(t.get("size") or t.get("total_size")),
            "ratio": f"{t.get('ratio', 0):.2f}" if isinstance(t.get("ratio"), (int, float)) else "-",
            "dlspeed": speed_str(t.get("dlspeed")),
            "upspeed": speed_str(t.get("upspeed")),
            "uploaded_raw": uploaded_raw if isinstance(uploaded_raw, (int, float)) else 0,
            "seeds": int(seeds_raw) if isinstance(seeds_raw, (int, float)) else 0,
            "peers": int(peers_raw) if isinstance(peers_raw, (int, float)) else 0,
            "eta": eta_str(t.get("eta")),
            "added": added_str(t.get("added_on")),
            "added_short": added_short_str(t.get("added_on")),
            "tracker": _trk,
            "category": t.get("category") or "-",
            "tags": tags_value,
            "hash": t.get("hash") or "",
            "raw": t,
        })
    return rows


def format_rows(rows: list, page: int, page_size: int) -> tuple[list, int, int]:
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, len(rows))
    return rows[start:end], total_pages, page


def parse_tag_filter(value: str) -> dict | None:
    raw = value.strip()
    if not raw:
        return None
    expr = parse_tag_expr(raw)
    if expr:
        return {"type": "tag", "raw": raw, "expr": expr}
    if "+" in raw:
        tags = [t.strip().lower() for t in raw.split("+") if t.strip()]
        return {"type": "tag", "mode": "and", "tags": tags, "raw": raw}
    if "," in raw:
        tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
        return {"type": "tag", "mode": "or", "tags": tags, "raw": raw}
    return {"type": "tag", "mode": "or", "tags": [raw.lower()], "raw": raw}


def _tokenize_tag_expr(value: str) -> list[str]:
    tokens = []
    i = 0
    ops = set("+,()!")
    while i < len(value):
        ch = value[i]
        if ch.isspace():
            i += 1
            continue
        if ch in ops:
            tokens.append(ch)
            i += 1
            continue
        start = i
        while i < len(value) and not value[i].isspace() and value[i] not in ops:
            i += 1
        tokens.append(value[start:i])
    return tokens


def parse_tag_expr(value: str):
    tokens = _tokenize_tag_expr(value)
    if not tokens:
        return None
    idx = 0

    def parse_expr():
        return parse_or()

    def parse_or():
        node = parse_and()
        items = [node]
        while current() == ",":
            advance()
            items.append(parse_and())
        if len(items) == 1:
            return items[0]
        return ("or", items)

    def parse_and():
        node = parse_unary()
        items = [node]
        while current() == "+":
            advance()
            items.append(parse_unary())
        if len(items) == 1:
            return items[0]
        return ("and", items)

    def parse_unary():
        if current() == "!":
            advance()
            return ("not", parse_unary())
        if current() == "(":
            advance()
            node = parse_expr()
            if current() != ")":
                return None
            advance()
            return node
        token = current()
        if token in (None, "+", ",", ")", "!"):
            return None
        advance()
        return ("tag", token.lower())

    def current():
        return tokens[idx] if idx < len(tokens) else None

    def advance():
        nonlocal idx
        idx += 1

    tree = parse_expr()
    if tree is None:
        return None
    if idx != len(tokens):
        return None
    return tree


def eval_tag_expr(node, tag_set: set[str]) -> bool:
    kind = node[0]
    if kind == "tag":
        return node[1] in tag_set
    if kind == "not":
        return not eval_tag_expr(node[1], tag_set)
    if kind == "and":
        return all(eval_tag_expr(item, tag_set) for item in node[1])
    if kind == "or":
        return any(eval_tag_expr(item, tag_set) for item in node[1])
    return False

def parse_filter_line(line: str, existing: list[dict]) -> list[dict]:
    tokens = shlex.split(line)
    if not tokens:
        return existing
    updated = [f for f in existing if f.get("type") not in ("text", "category", "tag")]
    updates: dict[str, dict] = {}
    for token in tokens:
        if "=" not in token:
            # Treat as text filter
            value = token.strip()
            if not value: continue
            negate = False
            if value.startswith("!"):
                negate = True
                value = value[1:]
            updates["text"] = {"type": "text", "value": value, "enabled": True, "negate": negate}
            continue

        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key in ("text", "q", "name"):
            negate = False
            if value.startswith("!"):
                negate = True
                value = value[1:]
            updates["text"] = {"type": "text", "value": value, "enabled": True, "negate": negate}
        elif key in ("cat", "category"):
            negate = False
            if value.startswith("!"):
                negate = True
                value = value[1:]
            updates["category"] = {"type": "category", "value": value, "enabled": True, "negate": negate}
        elif key in ("tag", "tags"):
            parsed = parse_tag_filter(value)
            if parsed:
                parsed["enabled"] = True
                updates["tag"] = parsed
        elif key in ("hash", "h"):
            negate = False
            if value.startswith("!"):
                negate = True
                value = value[1:]
            updates["hash"] = {"type": "hash", "value": value, "enabled": True, "negate": negate}
        elif key in ("status", "s"):
            negate = False
            if value.startswith("!"):
                negate = True
                value = value[1:]
            statuses = [s.strip().lower() for s in value.split(",") if s.strip()]
            updates["status"] = {"type": "status", "values": statuses, "enabled": True, "negate": negate}
    updated.extend(updates.values())
    return updated


def summarize_filters(filters: list[dict]) -> str:
    active = [f for f in filters if f.get("enabled", True)]
    if not active:
        return "-"
    parts = []
    for flt in active:
        prefix = "!" if flt.get("negate") else ""
        if flt["type"] == "text":
            parts.append(f"text={prefix}{flt['value']}")
        elif flt["type"] == "category":
            parts.append(f"cat={prefix}{flt['value']}")
        elif flt["type"] == "tag":
            raw = flt.get("raw", "")
            if prefix and raw and not raw.startswith("!"):
                raw = prefix + raw
            parts.append(f"tag={raw}")
        elif flt["type"] == "hash":
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"hash={prefix}{flt['value']}")
        elif flt["type"] == "status":
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"status={prefix}{','.join(flt['values'])}")
    return " ".join(parts)


def format_filters_line(filters: list[dict], colors: ColorScheme) -> str:
    if not filters:
        return "Filters: -"
    parts = []
    for flt in filters:
        active = flt.get("enabled", True)
        color = colors.PURPLE if active else ""
        reset = colors.RESET if active else ""
        if flt["type"] == "text":
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"text={color}{prefix}{flt['value']}{reset}")
        elif flt["type"] == "category":
            cat_color = colors.PURPLE if active else ""
            cat_reset = colors.RESET if active else ""
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"cat={cat_color}{prefix}{flt['value']}{cat_reset}")
        elif flt["type"] == "tag":
            raw = flt.get("raw", "")
            if flt.get("negate") and raw and not raw.startswith("!"):
                raw = "!" + raw
            parts.append(f"tag={color}{raw}{reset}")
        elif flt["type"] == "hash":
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"hash={color}{prefix}{flt['value']}{reset}")
        elif flt["type"] == "status":
            prefix = "!" if flt.get("negate") else ""
            parts.append(f"status={color}{prefix}{','.join(flt['values'])}{reset}")
    return "Filters: " + " ".join(parts)


def load_presets(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if yaml is None:
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return data.get("slots") or {}
    except Exception:
        return {}


def save_presets(path: Path, slots: dict) -> None:
    if yaml is None:
        return
    payload = {"slots": slots}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def load_macros(path: Path) -> list[dict]:
    """Load macro definitions from YAML config.

    Returns list of dicts with keys: name, cmd, desc
    Returns empty list if file missing or invalid.
    """
    if not path.exists():
        return []
    try:
        if yaml is None:
            return []
        data = yaml.safe_load(path.read_text()) or {}
        macros = data.get("macros") or []
        # Validate structure
        valid = []
        for m in macros:
            if isinstance(m, dict) and "name" in m and "cmd" in m and "desc" in m:
                valid.append(m)
        return valid
    except Exception:
        return []


def serialize_filters(filters: list[dict]) -> list[dict]:
    out = []
    for flt in filters:
        if flt["type"] == "text":
            out.append({"type": "text", "value": flt["value"], "enabled": flt.get("enabled", True), "negate": flt.get("negate", False)})
        elif flt["type"] == "category":
            out.append({"type": "category", "value": flt["value"], "enabled": flt.get("enabled", True), "negate": flt.get("negate", False)})
        elif flt["type"] == "tag":
            out.append({
                "type": "tag",
                "raw": flt.get("raw", ""),
                "mode": flt.get("mode", "or"),
                "tags": flt.get("tags", []),
                "enabled": flt.get("enabled", True),
                "negate": flt.get("negate", False),
            })
        elif flt["type"] == "hash":
            out.append({"type": "hash", "value": flt["value"], "enabled": flt.get("enabled", True), "negate": flt.get("negate", False)})
        elif flt["type"] == "status":
            out.append({"type": "status", "values": flt["values"], "enabled": flt.get("enabled", True), "negate": flt.get("negate", False)})
    return out


def restore_filters(items: list[dict]) -> list[dict]:
    filters = []
    for item in items or []:
        ftype = item.get("type")
        if ftype == "text" and item.get("value"):
            filters.append({"type": "text", "value": item["value"], "enabled": item.get("enabled", True), "negate": item.get("negate", False)})
        elif ftype == "category" and item.get("value"):
            filters.append({"type": "category", "value": item["value"], "enabled": item.get("enabled", True), "negate": item.get("negate", False)})
        elif ftype == "tag":
            raw = item.get("raw", "")
            if raw:
                parsed = parse_tag_filter(raw) or {}
                parsed.update({
                    "raw": raw,
                    "mode": item.get("mode", parsed.get("mode", "or")),
                    "tags": item.get("tags", parsed.get("tags", [])),
                    "enabled": item.get("enabled", True),
                    "negate": item.get("negate", False),
                })
                filters.append(parsed)
        elif ftype == "hash" and item.get("value"):
            filters.append({"type": "hash", "value": item["value"], "enabled": item.get("enabled", True), "negate": item.get("negate", False)})
        elif ftype == "status" and item.get("values"):
            filters.append({"type": "status", "values": item["values"], "enabled": item.get("enabled", True), "negate": item.get("negate", False)})
    return filters


def apply_filters(rows: list[dict], filters: list[dict]) -> list[dict]:
    active = [f for f in filters if f.get("enabled", True)]
    if not active:
        return rows
    filtered = rows
    for flt in active:
        if flt["type"] == "text":
            term = flt["value"].lower()
            def match_text(r):
                present = term in (r.get("name") or "").lower()
                return not present if flt.get("negate") else present
            filtered = [r for r in filtered if match_text(r)]
        elif flt["type"] == "category":
            category = flt["value"].lower()
            def match_cat(r):
                raw_cat = (r.get("raw", {}).get("category") or "").strip().lower()
                if category == "-":
                    present = not raw_cat
                else:
                    present = raw_cat == category
                return not present if flt.get("negate") else present
            filtered = [r for r in filtered if match_cat(r)]
        elif flt["type"] == "tag":
            tags = set(flt.get("tags") or [])
            mode = flt.get("mode", "or")
            expr = flt.get("expr")
            if not tags and not expr:
                continue
            def match(row: dict) -> bool:
                raw_tags = row.get("raw", {}).get("tags") or ""
                tag_set = {t.strip().lower() for t in raw_tags.split(",") if t.strip()}
                if expr:
                    present = eval_tag_expr(expr, tag_set)
                elif mode == "and":
                    present = tags.issubset(tag_set)
                else:
                    present = bool(tags & tag_set)
                return not present if flt.get("negate") else present
            filtered = [r for r in filtered if match(r)]
        elif flt["type"] == "hash":
            hash_fragment = flt["value"].lower()
            def match_hash(r):
                full_hash = (r.get("hash") or "").lower()
                present = hash_fragment in full_hash
                return not present if flt.get("negate") else present
            filtered = [r for r in filtered if match_hash(r)]
        elif flt["type"] == "status":
            target_states = set()
            for status_term in flt["values"]:
                if status_term == "all":
                    # If "all" is specified, include all possible states
                    target_states = {k.lower() for k in STATE_CODE.keys()}
                    break
                # Try to map status_term to one of the defined state groups or raw states
                mapped_states = STATUS_FILTER_MAP.get(status_term)
                if mapped_states:
                    target_states.update({s.lower() for s in mapped_states})
                elif status_term in API_TERM_MAP: # Allow filtering by raw API states directly (case-insensitive)
                    target_states.add(status_term) # API_TERM_MAP keys are already lowered
                else:
                    # Check if it's a QBT code (e.g. 'D', 'SD')
                    for m in STATUS_MAPPING:
                        if m["code"].lower() == status_term:
                            target_states.add(m["api"].lower())

            def match_status(r):
                raw_state = (r.get("raw", {}).get("state") or "").lower()
                present = raw_state in target_states
                return not present if flt.get("negate") else present
            
            # Only apply filter if there are valid target states
            if target_states:
                filtered = [r for r in filtered if match_status(r)]
    return filtered


def confirm_delete(item: dict) -> tuple[bool, bool]:
    name = item.get("name") or "Unknown"
    hash_value = item.get("hash") or "unknown"
    remove_ok = read_line("Remove torrent? (y/N): ").strip().lower() == "y"
    if not remove_ok:
        return False, False
    delete_files = read_line("Delete data too? (y/N): ").strip().lower() == "y"
    summary = f"Confirm remove {'+ delete data ' if delete_files else ''}{name} ({hash_value})? (y/N): "
    final_ok = read_line(summary).strip().lower() == "y"
    if not final_ok:
        return False, False
    return True, delete_files


def reannounce_torrent(opener: urllib.request.OpenerDirector, api_url: str, hash_value: str) -> str:
    """Trigger a tracker reannounce for the given torrent hash."""
    resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/reannounce", {"hashes": hash_value})
    return "OK" if resp in ("Ok.", "") else resp


def apply_action(opener: urllib.request.OpenerDirector, api_url: str, action: str, item: dict) -> str:
    hash_value = item.get("hash")
    if not hash_value:
        return "Missing hash"
    state = item.get("state") or ""
    raw = item.get("raw", {})

    if action == "P":
        is_paused = "paused" in state.lower() or "stopped" in state.lower()
        action = "start" if is_paused else "stop"
        resp = qbit_request(opener, api_url, "POST", f"/api/v2/torrents/{action}", {"hashes": hash_value})
        # Try fallbacks for older versions if start/stop 404
        if "HTTP 404" in resp:
            old_action = "resume" if is_paused else "pause"
            resp = qbit_request(opener, api_url, "POST", f"/api/v2/torrents/{old_action}", {"hashes": hash_value})
        return "OK" if resp in ("Ok.", "") else resp
    if action == "D":
        confirmed, delete_files = confirm_delete(item)
        if not confirmed:
            return "Cancelled"
        resp = qbit_request(
            opener,
            api_url,
            "POST",
            "/api/v2/torrents/delete",
            {"hashes": hash_value, "deleteFiles": "true" if delete_files else "false"},
        )
        return "OK" if resp in ("Ok.", "") else resp
    if action == "C":
        value = read_line("Enter new category (blank cancels): ").strip()
        if not value:
            return "Cancelled"
        resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/setCategory", {"hashes": hash_value, "category": value})
        return "OK" if resp in ("Ok.", "") else resp
    if action == "E":
        existing_tags = (item.get("tags") or "").strip()
        if existing_tags:
            print(f"Current tags: {existing_tags}")
        value = read_line("Tags (comma-separated, '-' to remove, '--' to clear all, blank cancels): ").strip()
        if not value:
            return "Cancelled"
        if value == "--":
            existing = (item.get("tags") or "").strip()
            if not existing:
                return "No tags to clear"
            resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/removeTags", {"hashes": hash_value, "tags": existing})
            return "OK" if resp in ("Ok.", "") else resp
        if value.startswith("-"):
            tags = value[1:].strip()
            if not tags:
                return "Cancelled"
            resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/removeTags", {"hashes": hash_value, "tags": tags})
            return "OK" if resp in ("Ok.", "") else resp
        resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/addTags", {"hashes": hash_value, "tags": value})
        return "OK" if resp in ("Ok.", "") else resp
    if action == "V":
        resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/recheck", {"hashes": hash_value})
        return "OK" if resp in ("Ok.", "") else resp
    if action == "T":
        priv = raw.get("private")
        if priv is None:
            # Fetch properties to confirm
            props_raw = qbit_request(opener, api_url, "GET", "/api/v2/torrents/properties", {"hash": hash_value})
            try:
                props = json.loads(props_raw)
                priv = props.get("private")
                if priv is None:
                    priv = props.get("is_private")
            except Exception:
                pass
        
        if priv is None:
            return "Skip (private=unknown)"
            
        if isinstance(priv, str):
            priv = priv.strip().lower()
            if priv in ("true", "1", "yes"):
                return "Skip (private)"
            if priv in ("false", "0", "no"):
                priv = False
        if priv:
            return "Skip (private)"
        trackers = fetch_public_trackers(TRACKERS_LIST_URL)
        if not trackers:
            return "Failed (no trackers)"
        urls = "\n".join(trackers)
        resp = qbit_request(opener, api_url, "POST", "/api/v2/torrents/addTrackers", {"hash": hash_value, "urls": urls})
        if resp.startswith("HTTP "):
            return f"Failed ({resp})"
        return f"OK ({len(trackers)})"
    if action == "Q":
        return spawn_media_qc(hash_value)
    return "Unknown action"





def fetch_trackers(opener: urllib.request.OpenerDirector, api_url: str, hash_value: str) -> list[dict]:
    raw = qbit_request(opener, api_url, "GET", "/api/v2/torrents/trackers", {"hash": hash_value})
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def fetch_files(opener: urllib.request.OpenerDirector, api_url: str, hash_value: str) -> list[dict]:
    raw = qbit_request(opener, api_url, "GET", "/api/v2/torrents/files", {"hash": hash_value})
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def fetch_peers(opener: urllib.request.OpenerDirector, api_url: str, hash_value: str) -> dict:
    raw = qbit_request(opener, api_url, "GET", "/api/v2/sync/torrentPeers", {"hash": hash_value})
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def render_info_lines(item: dict, width: int) -> list[str]:
    raw = item.get("raw") or {}
    lines = [
        f"Name: {item.get('name')}",
        "-" * width,
        f"State: {item.get('state')}",
        f"Category: {item.get('category')}",
        f"Tags: {item.get('tags')}",
        f"Size: {item.get('size')}",
        f"Progress: {item.get('progress')}",
        f"Ratio: {item.get('ratio')}",
        f"DL/UL: {item.get('dlspeed')} / {item.get('upspeed')}",
        f"ETA: {item.get('eta')}",
        f"Hash: {item.get('hash')}",
    ]
    for key in ("save_path", "content_path", "tracker", "completion_on", "added_on", "last_activity"):
        if key in raw:
            value = raw.get(key)
            if key.endswith("_on") and isinstance(value, (int, float)):
                value = format_ts(value)
            lines.append(f"{key}: {value}")
    wrapped = []
    for line in lines:
        wrapped.extend(wrap_ansi(line, width))
    return wrapped


def render_trackers_lines(trackers: list[dict], width: int, max_rows: int) -> list[str]:
    if not trackers:
        return ["No trackers."]
    headers = ["Status", "Tier", "URL"]
    widths = [10, 6, max(20, width - 20)]
    lines = []
    lines.append(f"{headers[0]:<{widths[0]}} {headers[1]:<{widths[1]}} {headers[2]}")
    lines.append("-" * width)
    for row in trackers[:max_rows]:
        status = str(row.get("status", ""))
        tier = str(row.get("tier", ""))
        url = str(row.get("url", ""))
        url = truncate(url, widths[2])
        lines.append(f"{status:<{widths[0]}} {tier:<{widths[1]}} {url}")
    if len(trackers) > max_rows:
        lines.append(f"... ({len(trackers) - max_rows} more)")
    return lines


def resolve_torrent_file_paths(content_path: str, file_name: str) -> list[Path]:
    if not content_path or not file_name:
        return []
    root_path = Path(content_path)
    relative_path = Path(file_name)
    candidates: list[Path] = []

    def add_candidate(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    if relative_path.is_absolute():
        add_candidate(relative_path)
        return candidates

    if root_path.is_file():
        add_candidate(root_path)
        add_candidate(root_path.parent / relative_path)
        return candidates

    add_candidate(root_path / relative_path)
    add_candidate(root_path.parent / relative_path)

    if relative_path.parts and relative_path.parts[0] == root_path.name and len(relative_path.parts) > 1:
        add_candidate(root_path / Path(*relative_path.parts[1:]))

    return candidates


def file_inode_and_links(content_path: str, file_name: str) -> tuple[str, str]:
    candidate_paths = resolve_torrent_file_paths(content_path, file_name)
    for file_path in candidate_paths:
        for path_variant in (file_path, Path(f"{file_path}.!qB")):
            try:
                file_stat = path_variant.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return "?", "?"
            return str(file_stat.st_ino), str(file_stat.st_nlink)
    return "-", "-"


def render_files_lines(files: list[dict], width: int, max_rows: int, content_path: str = "") -> list[str]:
    if not files:
        return ["No files found."]
    files.sort(key=lambda x: x.get("name", ""))
    headers = ["Index", "Inode", "Links", "Name", "Size", "Prog", "Priority"]
    col_sep = "  "
    index_width = 5
    inode_width = 12
    links_width = 5
    size_width = 10
    progress_width = 6
    priority_width = 10
    name_width = max(
        8,
        width - (
            index_width
            + inode_width
            + links_width
            + size_width
            + progress_width
            + priority_width
            + (len(headers) - 1) * len(col_sep)
        ),
    )
    widths = [index_width, inode_width, links_width, name_width, size_width, progress_width, priority_width]
    row_align = {
        0: "right",
        1: "right",
        2: "right",
        3: "left",
        4: "right",
        5: "right",
        6: "left",
    }
    header_align = {
        0: "center",
        1: "center",
        2: "center",
        3: "left",
        4: "center",
        5: "center",
        6: "left",
    }

    def fmt_cell(col_idx: int, value: str, align_map: dict[int, str]) -> str:
        text = truncate(str(value), widths[col_idx])
        mode = align_map.get(col_idx, "left")
        if mode == "right":
            return text.rjust(widths[col_idx])
        if mode == "center":
            return text.center(widths[col_idx])
        return text.ljust(widths[col_idx])

    lines = []
    header_line = col_sep.join(fmt_cell(i, h, header_align) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("-" * width)
    priority_map = {0: "Do not DL", 1: "Normal", 2: "High", 6: "Max", 7: "Forced"}
    for idx, f in enumerate(files[:max_rows]):
        file_name = str(f.get("name", ""))
        name = truncate(file_name, widths[3])
        size = size_str(f.get("size", 0))
        prog = f"{int(f.get('progress', 0) * 100)}%"
        prio = priority_map.get(f.get("priority", 1), str(f.get("priority")))
        inode, links = file_inode_and_links(content_path, file_name)
        row_values = [str(idx), inode, links, name, size, prog, truncate(prio, widths[6])]
        line = col_sep.join(fmt_cell(i, row_values[i], row_align) for i in range(len(widths)))
        lines.append(line)
    if len(files) > max_rows:
        lines.append(f"... ({len(files) - max_rows} more)")
    return lines


def render_peers_lines(peers_payload: dict, width: int, max_rows: int) -> list[str]:
    peers = peers_payload.get("peers") or {}
    if not peers:
        return ["No peers."]
    rows = []
    for addr, info in peers.items():
        dl_speed = info.get("dl_speed") or 0
        ul_speed = info.get("up_speed") or 0
        rows.append({
            "addr": addr,
            "client": info.get("client", ""),
            "progress": int((info.get("progress", 0) or 0) * 100),
            "dl": speed_str(dl_speed),
            "ul": speed_str(ul_speed),
            "dl_raw": dl_speed,
            "ul_raw": ul_speed,
            "flags": info.get("flags", ""),
        })
    rows.sort(key=lambda x: (x["dl_raw"], x["ul_raw"]), reverse=True)
    headers = ["Peer", "Prog", "DL", "UL", "Flags", "Client"]
    widths = [18, 6, 10, 10, 8, max(20, width - 60)]
    lines = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("-" * width)
    for row in rows[:max_rows]:
        client = truncate(row["client"], widths[5])
        line = (
            f"{row['addr']:<{widths[0]}} "
            f"{str(row['progress']) + '%':<{widths[1]}} "
            f"{row['dl']:<{widths[2]}} "
            f"{row['ul']:<{widths[3]}} "
            f"{row['flags']:<{widths[4]}} "
            f"{client:<{widths[5]}}"
        )
        lines.append(line)
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more)")
    return lines


def render_mediainfo_lines(item: dict, width: int, colors: ColorScheme) -> list[str]:
    raw = item.get("raw") or {}
    content_path = get_content_path(raw)
    info = get_mediainfo_for_hash(str(item.get("hash") or ""), content_path)

    if not info:
        return [f"{colors.FG_TERTIARY}No MediaInfo.{colors.RESET}"]

    src_lines = [l.rstrip() for l in str(info).splitlines()]
    lines: list[str] = []

    is_v2 = bool(src_lines) and src_lines[0].startswith("QBITUI_MI_V2|")
    is_v1 = (not is_v2 and len(src_lines) >= 2 and src_lines[1].lstrip().startswith("-"))

    if is_v2:
        col_headers = src_lines[0].split("|")[1:]   # skip "QBITUI_MI_V2" tag
        col_units   = src_lines[1].split("|")        # leading empty for File col
        data_rows   = [l.split("|") for l in src_lines[2:] if l.strip()]
        n_cols = len(col_headers)

        # Per-column display colors
        col_colors = [
            colors.FG_PRIMARY,    # 0 File
            colors.YELLOW,        # 1 Duration
            colors.YELLOW,        # 2 Size
            colors.YELLOW,        # 3 BR
            colors.CYAN,          # 4 Fmt
            colors.FG_SECONDARY,  # 5 WxH
            colors.YELLOW,        # 6 FPS
            colors.FG_SECONDARY,  # 7 Ch
            colors.YELLOW,        # 8 kHz
        ]
        right_cols = {2, 3, 6, 7, 8}

        # Dynamic display widths from data
        disp_w = []
        for i in range(n_cols):
            hdr_w  = len(col_headers[i])
            unit_w = len(col_units[i]) if i < len(col_units) else 0
            data_w = max((len(r[i].strip()) if i < len(r) else 0 for r in data_rows), default=0)
            disp_w.append(max(hdr_w, unit_w, data_w))

        # Give spare terminal width to File column
        sep = "  "
        total = sum(disp_w) + len(sep) * (n_cols - 1)
        spare = width - total
        if spare > 0:
            disp_w[0] += spare

        # Header line — full-width lavender
        hdr_cells = [
            col_headers[i].rjust(disp_w[i]) if i in right_cols else col_headers[i].ljust(disp_w[i])
            for i in range(n_cols)
        ]
        hdr_str = sep.join(hdr_cells)
        hdr_padded = hdr_str + " " * max(0, width - len(hdr_str))
        lines.append(f"{colors.LAVENDER}{hdr_padded}{colors.RESET}")

        # Units sub-row (dim, centered)
        unit_cells = [
            (col_units[i] if i < len(col_units) else "").center(disp_w[i])
            for i in range(n_cols)
        ]
        lines.append(f"{colors.FG_TERTIARY}{sep.join(unit_cells)}{colors.RESET}")

        # Full-width separator
        lines.append(f"{colors.FG_TERTIARY}{'-' * width}{colors.RESET}")

        # Data rows with per-column coloring
        for row in data_rows:
            cells_disp = []
            for i in range(n_cols):
                val = row[i].strip() if i < len(row) else ""
                if i == 0:
                    val = truncate_mid(val, disp_w[i])
                else:
                    val = val[:disp_w[i]]
                aligned = val.rjust(disp_w[i]) if i in right_cols else val.ljust(disp_w[i])
                c = col_colors[i] if i < len(col_colors) else colors.FG_PRIMARY
                cells_disp.append(f"{c}{aligned}{colors.RESET}")
            lines.append(sep.join(cells_disp))

    elif is_v1:
        # Pre-v1.12.7 space-padded table: header lavender (full-width), sep full-width, data plain
        for i, line in enumerate(src_lines):
            if not line.strip():
                lines.append("")
            elif i == 0:
                padded = line + " " * max(0, width - len(line))
                lines.append(f"{colors.LAVENDER}{padded}{colors.RESET}")
            elif line.lstrip().startswith("-"):
                lines.append(f"{colors.FG_TERTIARY}{'-' * width}{colors.RESET}")
            else:
                lines.extend(wrap_ansi(line, width))

    else:
        # Key:value fallback (raw mediainfo output not from mediainfo_table())
        for line in src_lines:
            line_s = line.strip()
            if not line_s:
                lines.append("")
                continue
            if ":" in line_s:
                parts = line_s.split(":", 1)
                if len(parts) == 2:
                    key, value = parts[0].strip(), parts[1].strip()
                    colored_key = f"{colors.LAVENDER}{key}:{colors.RESET}"
                    if any(u in value.lower() for u in ["kb/s", "mb/s", "gb/s", "gb", "mb", "kb", "bits"]):
                        colored_value = f"{colors.YELLOW}{value}{colors.RESET}"
                    elif value.replace(".", "").replace("-", "").isdigit():
                        colored_value = f"{colors.YELLOW}{value}{colors.RESET}"
                    elif "/" in value or "\\" in value or ":" in value:
                        colored_value = f"{colors.BLUE}{value}{colors.RESET}"
                    else:
                        colored_value = f"{colors.FG_PRIMARY}{value}{colors.RESET}"
                    lines.extend(wrap_ansi(f"{colored_key} {colored_value}", width))
                else:
                    lines.extend(wrap_ansi(line_s, width))
            else:
                lines.extend(wrap_ansi(line_s, width))

    return lines or [f"{colors.FG_TERTIARY}No MediaInfo.{colors.RESET}"]


def resolve_available_tabs(opener: urllib.request.OpenerDirector, api_url: str, item: dict) -> list[str]:
    available = ["Info"]
    hash_value = item.get("hash")
    if not hash_value:
        return available
    trackers = fetch_trackers(opener, api_url, hash_value)
    if trackers:
        available.append("Trackers")
    files = fetch_files(opener, api_url, hash_value)
    if files:
        available.append("Content")
    peers_payload = fetch_peers(opener, api_url, hash_value)
    if peers_payload.get("peers"):
        available.append("Peers")
    raw = item.get("raw") or {}
    content_path = get_content_path(raw)
    if content_path and shutil.which("mediainfo") and get_largest_media_file(content_path):
        available.append("MediaInfo")
    return available


def capture_key_sequences() -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(f"\nDebug key capture v{VERSION}: press Ctrl-Q to exit.", flush=True)
    try:
        tty.setraw(fd)
        esc_pending = False
        esc_buffer = b""
        bracket_pending = False
        bracket_started = 0.0
        while True:
            buf = sys.stdin.buffer.read(1)
            if not buf:
                continue
            if buf == b"\x11":
                print("EXIT", flush=True)
                break
            if bracket_pending and time.monotonic() - bracket_started > 1.0:
                bracket_pending = False
            if buf == b"\x1b":
                esc_pending = True
                esc_buffer = b""
                continue
            if buf == b"[" and not esc_pending:
                bracket_pending = True
                bracket_started = time.monotonic()
                continue
            if bracket_pending:
                bracket_pending = False
                if buf in (b"A", b"B", b"C", b"D"):
                    seq = b"\x5b" + buf
                    hex_bytes = " ".join(f"{b:02x}" for b in seq)
                    print(f"SEQ {hex_bytes}  {seq!r}", flush=True)
                    continue
            if esc_pending:
                if not esc_buffer and buf in (b"[", b"O"):
                    esc_buffer = buf
                    continue
                esc_buffer += buf
                if esc_buffer.endswith((b"A", b"B", b"C", b"D")):
                    seq = b"\x1b" + esc_buffer
                    hex_bytes = " ".join(f"{b:02x}" for b in seq)
                    print(f"SEQ {hex_bytes}  {seq!r}", flush=True)
                    esc_pending = False
                    esc_buffer = b""
                continue
            if esc_pending:
                print("ESC", flush=True)
                esc_pending = False
                esc_buffer = b""
            hex_bytes = " ".join(f"{b:02x}" for b in buf)
            print(f"KEY {hex_bytes}  {buf!r}", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def main() -> int:
    global NEED_RESIZE
    parser = argparse.ArgumentParser(description="Interactive qBittorrent dashboard")
    parser.add_argument("--config", default=os.environ.get("QBITTORRENT_CONFIG_FILE"), help="Path to request-cache.yml")
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("QBITTORRENT_PAGE_SIZE", "10")))
    parser.add_argument("--debug-keys", help="Write raw key sequences to a file (TTY only).")
    parser.add_argument("--color-theme", type=Path, metavar='PATH', help='Path to YAML color theme file (overrides default colors)')
    # Shared cache flags
    parser.add_argument("--use-shared-cache", action=argparse.BooleanOptionalAction, default=True, help="Use qbit-cache-agent for list polling instead of direct API calls (default: true).")
    parser.add_argument("--cache-max-age", type=float, default=15.0, help="Max cache age in seconds (default: 15).")
    parser.add_argument("--cache-wait-fresh", type=float, default=5.0, help="Seconds to wait for fresh cache snapshot (default: 5).")
    parser.add_argument("--cache-allow-stale", action=argparse.BooleanOptionalAction, default=True, help="Allow stale cache fallback (default: true).")
    parser.add_argument("--cache-agent-cmd", type=Path, default=Path(__file__).with_name("qbit-cache-agent.py"), help="Path to qbit-cache-agent.py (default: bin/qbit-cache-agent.py alongside this script).")
    parser.add_argument("--cache-status", action="store_true", help="Print cache/daemon status JSON and exit (requires --use-shared-cache).")
    parser.add_argument("--cache-base-dir", type=Path, default=Path.home() / ".cache" / "qbitui", help="Shared cache base directory (default: ~/.cache/qbitui).")
    parser.add_argument("--mediainfo-cache-dir", type=Path, default=None, help="Override mediainfo cache directory (default: ~/.logs/media_qc/cache/mediainfo, or QBIT_MEDIAINFO_CACHE_DIR env).")
    args = parser.parse_args()

    # Initialize global color scheme
    colors = ColorScheme(yaml_path=args.color_theme)

    # Resolve mediainfo cache dir (CLI flag > env var > default)
    global CACHE_DIR
    if args.mediainfo_cache_dir:
        CACHE_DIR = args.mediainfo_cache_dir.expanduser()

    config_path = Path(args.config) if args.config else (Path(__file__).parent.parent / "config" / "request-cache.yml")
    cfg_api_url, cfg_creds = read_qbit_config(config_path)
    api_url = os.environ.get("QBITTORRENT_API_URL") or cfg_api_url or "http://localhost:9003"
    creds_file = os.environ.get("QBITTORRENT_CREDENTIALS_FILE") or cfg_creds or "/mnt/config/secrets/qbittorrent/api.env"

    username = os.environ.get("QBITTORRENT_USERNAME", "")
    password = os.environ.get("QBITTORRENT_PASSWORD", "")
    if not username or not password:
        username, password = read_credentials(Path(creds_file))
    if not username or not password:
        print("ERROR: QBITTORRENT credentials not found (set env or credentials file)", file=sys.stderr)
        return 1

    # --cache-status: delegate to cache agent and exit (no curses required)
    if args.cache_status:
        if not args.use_shared_cache:
            print("ERROR: --cache-status requires --use-shared-cache", file=sys.stderr)
            return 1
        cache_env = {**os.environ, "QBIT_URL": api_url, "QBIT_USER": username, "QBIT_PASS": password}
        result = subprocess.run(
            [sys.executable, str(args.cache_agent_cmd), "--status"],
            env=cache_env,
            capture_output=False,
        )
        return result.returncode

    opener = make_opener()
    if not qbit_login(opener, api_url, username, password):
        print("ERROR: qBittorrent login failed", file=sys.stderr)
        return 1

    scope = "all"
    page = 0
    filters: list[dict] = []
    presets = load_presets(PRESET_FILE)
    macro_config_path = Path(__file__).parent.parent / "config" / "macros.yaml"
    macros_global = load_macros(macro_config_path)
    tracker_keyword_map = load_tracker_keyword_map(TRACKER_REGISTRY_FILE)
    macros_mtime = macro_config_path.stat().st_mtime if macro_config_path.exists() else -1.0
    last_macro_check = 0.0
    sort_fields = ["added_on", "name", "state", "ratio", "progress", "eta", "size", "dlspeed", "upspeed"]
    sort_index = 0
    sort_desc = True
    show_tags = False
    show_mediainfo_inline = False
    show_full_hash = False
    show_added = True
    narrow_mode = False
    narrow_mode_auto = True
    focus_idx = 0
    selection_hash: str | None = None
    selection_name: str | None = None
    in_tab_view = False
    tabs = ["Info", "Trackers", "Content", "Peers", "MediaInfo"]
    active_tab = 0
    banner_text = ""
    banner_until = 0.0
    last_banner_time = 0.0

    def set_banner(message: str, duration: float = 2.0, min_interval: float = 0.6) -> None:
        nonlocal banner_text, banner_until, last_banner_time
        now = time.time()
        if banner_text == message and now - last_banner_time < min_interval:
            return
        banner_text = message
        banner_until = now + duration
        last_banner_time = now

    def refresh_macros_if_changed(force: bool = False) -> None:
        nonlocal macros_global, macros_mtime, last_macro_check
        now = time.monotonic()
        if not force and (now - last_macro_check) < 0.8:
            return
        last_macro_check = now
        current_mtime = macro_config_path.stat().st_mtime if macro_config_path.exists() else -1.0
        if force or current_mtime != macros_mtime:
            macros_global = load_macros(macro_config_path)
            macros_mtime = current_mtime

    if args.debug_keys:
        try:
            log_path = Path(args.debug_keys).expanduser()
        except Exception:
            log_path = Path(args.debug_keys)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as handle:
            handle.write(f"\n=== key capture v{VERSION} {datetime.now(LOCAL_TZ).isoformat()} ===\n")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                buf = sys.stdin.buffer.read(1)
                if not buf:
                    continue
                if buf == b"\x11":
                    with log_path.open("a") as handle:
                        handle.write("EXIT (Ctrl-Q)\n")
                    break
                if buf == b"\x1b":
                    start = time.monotonic()
                    rest = b""
                    while time.monotonic() - start < 0.25:
                        if select.select([sys.stdin], [], [], 0.02)[0]:
                            rest += sys.stdin.buffer.read(1)
                            if rest.endswith((b"A", b"B", b"C", b"D")):
                                break
                        else:
                            if rest:
                                break
                    if not rest:
                        with log_path.open("a") as handle:
                            handle.write("ESC\n")
                        continue
                    seq = buf + rest
                    hex_bytes = " ".join(f"{b:02x}" for b in seq)
                    with log_path.open("a") as handle:
                        handle.write(f"SEQ {hex_bytes}  {seq!r}\n")
                    continue
                hex_bytes = " ".join(f"{b:02x}" for b in buf)
                with log_path.open("a") as handle:
                    handle.write(f"KEY {hex_bytes}  {buf!r}\n")
            return 0
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # Setup Resize Handler
    signal.signal(signal.SIGWINCH, handle_winch)

    cached_torrents: list[dict] = []
    cached_rows: list[dict] = []
    cache_time = 0.0
    fetch_interval = 2.0
    # Cache hit tracking (this session)
    cache_hit_count = 0    # requests served from shared cache daemon
    direct_hit_count = 0   # requests served directly from qbit API
    # Daemon meta file for header stats
    _cache_base = Path(args.cache_base_dir).expanduser()
    cache_meta_file = _cache_base / "torrents-info.meta.json"
    cache_meta: dict = {}
    last_meta_read = 0.0
    list_start_row = 0
    list_block_height = 0
    have_full_draw = False
    mi_bootstrap_done = False
    mi_queue: list[str] = []
    mi_queue_index = 0
    mi_last_tick = 0.0
    last_key_debug = "-"
    need_redraw = True
    last_term_w = terminal_width()
    output_buffer = ""

    def cycle_tabs(direction: int = 1, exit_after_last: bool = False) -> None:
        nonlocal in_tab_view, active_tab, have_full_draw
        if not selection_hash:
            set_banner("Select an item.")
            return
        
        selected_row = next((r for r in page_rows if r.get("hash") == selection_hash), None)
        if not selected_row:
            set_banner("Selection moved off page.")
            return
            
        available = resolve_available_tabs(opener, api_url, selected_row)
        if not available:
            available = ["Info"]
            
        if not in_tab_view:
            in_tab_view = True
            # When entering, try to preserve last active tab if available, else first/last based on direction
            if direction > 0:
                current_label = tabs[active_tab]
                if current_label not in available:
                    active_tab = tabs.index(available[0])
            else:
                # Entering backward: jump to last available
                active_tab = tabs.index(available[-1])
            have_full_draw = False
            return

        current_label = tabs[active_tab]
        if current_label not in available:
            active_tab = tabs.index(available[0])
            return
            
        idx = available.index(current_label)
        
        if exit_after_last and direction > 0 and idx == len(available) - 1:
            in_tab_view = False
            active_tab = 0 # Reset for next entry
            have_full_draw = False
            return
            
        # Normal cycle
        new_idx = (idx + direction) % len(available)
        next_label = available[new_idx]
        active_tab = tabs.index(next_label)
        have_full_draw = False

    def print_at(row: int, text: str) -> None:
        tui_print(f"\033[{row};1H\033[2K{text}", end="")

    def tui_print(text: str = "", end: str = "\r\n") -> None:
        # Buffer the output instead of immediate write
        nonlocal output_buffer
        output_buffer += f"{text}{end}"

    def tui_flush() -> None:
        nonlocal output_buffer
        if output_buffer:
            sys.stdout.write(output_buffer)
            sys.stdout.flush()
            output_buffer = ""

    def build_list_block(page_rows_local: list[dict], content_width_local: int) -> list[str]:
        lines: list[str] = []

        def fmt_scaled(value: int | float | None, scale: float) -> str:
            try:
                x = float(value or 0) / scale
            except Exception:
                x = 0.0
            if x <= 0:
                return "0"
            if x >= 100:
                return f"{x:,.0f}"
            return f"{x:,.1f}".rstrip("0").rstrip(".")

        def fmt_eta_minutes(value: int | float | None) -> str:
            try:
                sec = int(value or 0)
            except Exception:
                sec = 0
            if sec <= 0 or sec >= 8640000:
                return "-"
            return str(max(1, int(round(sec / 60))))

        w = {
            "f": 1,
            "no": max(2, len(str(max(0, len(page_rows_local) - 1)))),
            "st": 2,
            "name": 44,
            "trk": 8,
            "cat": 8,
            "pct": 3,
            "sz": 6,
            "up": 6,
            "dl": 6,
            "ul": 6,
            "sd": 4,
            "pr": 4,
            "eta": 4,
            "add": 11 if show_added else 0,
            "hash": 40 if show_full_hash else 6,
        }
        mins = {
            "name": 10,
            "trk": 3,
            "cat": 3,
            "hash": 12 if show_full_hash else 6,
            "add": 8 if show_added else 0,
            "sz": 4,
            "up": 4,
            "dl": 4,
            "ul": 4,
            "sd": 2,
            "pr": 2,
            "eta": 2,
            "pct": 2,
            "no": 1,
        }

        cols = ["f", "no", "st", "name", "trk", "cat", "pct", "sz", "up", "dl", "ul", "sd", "pr", "eta"]
        if show_added:
            cols.append("add")
        cols.append("hash")

        def total_width() -> int:
            return sum(w[c] for c in cols) + (len(cols) - 1)

        overflow = max(0, total_width() - content_width_local)

        def shrink(key: str) -> None:
            nonlocal overflow
            dec = min(overflow, max(0, w[key] - mins[key]))
            w[key] -= dec
            overflow -= dec

        for key in ("name", "trk", "cat", "hash", "add", "sz", "up", "dl", "ul", "sd", "pr", "eta", "pct", "no"):
            if overflow <= 0:
                break
            if key in w:
                shrink(key)

        # Expand name column to fill remaining terminal width
        spare = content_width_local - total_width()
        if spare > 0:
            w["name"] += spare

        headers = {
            "f": "F",
            "no": "No",
            "st": "ST",
            "name": "Name",
            "trk": "Trk",
            "cat": "Cat",
            "pct": "%",
            "sz": "SzGiB",
            "up": "UpGiB",
            "dl": "DLMiB",
            "ul": "ULMiB",
            "sd": "Sd",
            "pr": "Pr",
            "eta": "ETAm",
            "add": "Added",
            "hash": "Hash",
        }
        right_cols = {"no", "pct", "sz", "up", "dl", "ul", "sd", "pr", "eta"}

        def cell(key: str, value: str) -> str:
            text = truncate(str(value), w[key])
            return text.rjust(w[key]) if key in right_cols else text.ljust(w[key])

        render_width = total_width()
        lines.append(" ".join(cell(c, headers[c]) for c in cols))
        lines.append("-" * render_width)

        for idx, item in enumerate(page_rows_local, 0):
            selected = selection_hash == item.get("hash")
            status_col = colors.status_color(item.get("state") or "")
            focus_marker = ">" if idx == focus_idx else " "
            raw = item.get("raw") or {}

            hash_value = str(item.get("hash") or "")
            hash_display = hash_value if show_full_hash else hash_value[:6] or "-"
            pct_value = str(item.get("progress_pct") or "-")
            size_raw = raw.get("size") or raw.get("total_size") or 0
            values = {
                "f": focus_marker,
                "no": str(idx),
                "st": str(item.get("st") or "?"),
                "name": str(item.get("name") or "-"),
                "trk": str(item.get("tracker") or "-"),
                "cat": str(item.get("category") or "-"),
                "pct": pct_value,
                "sz": fmt_scaled(size_raw, 1024.0 ** 3),
                "up": fmt_scaled(item.get("uploaded_raw"), 1024.0 ** 3),
                "dl": fmt_scaled(raw.get("dlspeed") or 0, 1024.0 ** 2),
                "ul": fmt_scaled(raw.get("upspeed") or 0, 1024.0 ** 2),
                "sd": f"{int(item.get('seeds') or 0):,}",
                "pr": f"{int(item.get('peers') or 0):,}",
                "eta": fmt_eta_minutes(raw.get("eta")),
                "add": str(item.get("added_short") or "-"),
                "hash": hash_display,
            }

            if selected:
                plain = " ".join(cell(c, values[c]) for c in cols)
                plain = ANSI_RE.sub("", plain)
                plain = plain.ljust(render_width)
                lines.append(f"{colors.SELECTION}{plain}{colors.RESET}")
            else:
                row_parts = []
                for c in cols:
                    piece = cell(c, values[c])
                    if c == "f" and idx == focus_idx:
                        row_parts.append(f"{colors.CYAN}{piece}{colors.RESET}")
                    elif c in ("st", "name"):
                        row_parts.append(f"{status_col}{piece}{colors.RESET}")
                    elif c == "trk":
                        row_parts.append(f"{colors.ORANGE}{piece}{colors.RESET}")
                    elif c == "cat":
                        row_parts.append(f"{colors.PURPLE}{piece}{colors.RESET}")
                    else:
                        row_parts.append(piece)
                lines.append(" ".join(row_parts))

            if show_mediainfo_inline:
                raw_item = item.get("raw") or {}
                content_path = get_content_path(raw_item)
                mi_summary = get_mediainfo_summary_cached(str(item.get("hash") or ""), content_path, background_only=True)
                indent = "     mi: "
                width = max(10, content_width_local - len(indent))
                if mi_summary and " • " in mi_summary:
                    parts = mi_summary.split(" • ")
                    colored_parts = []
                    for part in parts:
                        part = part.strip()
                        if any(unit in part.lower() for unit in ["b/s", "gb", "mb", "kb", "bits"]):
                            colored_parts.append(f"{colors.YELLOW}{part}{colors.RESET}")
                        elif part in ["Matroska", "MPEG-4", "AVI", "MP4", "MKV", "WebM"]:
                            colored_parts.append(f"{colors.LAVENDER}{part}{colors.RESET}")
                        else:
                            colored_parts.append(f"{colors.FG_PRIMARY}{part}{colors.RESET}")
                    mi_line = f"{colors.FG_SECONDARY} • {colors.RESET}".join(colored_parts)
                    for line in wrap_ansi(mi_line, width):
                        lines.append(indent + line)
                else:
                    mi_line = mi_summary or "MediaInfo pending..."
                    for line in wrap_ansi(f"{colors.FG_TERTIARY}{mi_line}{colors.RESET}", width):
                        lines.append(indent + line)

            if show_tags:
                tags_raw = str(item.get("tags") or "").strip()
                if tags_raw:
                    tag_parts = []
                    for tag in [t.strip() for t in tags_raw.split(",") if t.strip()]:
                        if "FAIL" in tag.upper():
                            tag_parts.append(f"{colors.ERROR}{tag}{colors.RESET}")
                        elif "cross-seed" in tag.lower():
                            tag_parts.append(f"{colors.ORANGE}{tag}{colors.RESET}")
                        else:
                            tag_parts.append(f"{colors.PURPLE}{tag}{colors.RESET}")
                    tags_line = ", ".join(tag_parts)
                    indent = "     tags: "
                    width = max(10, content_width_local - len(indent))
                    for line in wrap_ansi(tags_line, width):
                        lines.append(indent + line)
        return lines, render_width

    def build_narrow_list_block(page_rows_local: list[dict], content_width_local: int) -> list[str]:
        lines: list[str] = []
        no_width = max(2, len(str(max(0, len(page_rows_local) - 1))))
        trk_width = min(12, 6 + max(0, content_width_local - 96) // 12)
        cat_width = min(16, 8 + max(0, content_width_local - 96) // 10)
        added_width = 11
        pct_width = 4
        reserved_width = 25 + no_width + trk_width + cat_width
        name_width = max(1, content_width_local - reserved_width)
        narrow_header = f"{'F':<1} {'No':<{no_width}} {'ST':<2} {'Name':<{name_width}} {'Trk':<{trk_width}} {'Cat':<{cat_width}} {'Added':<{added_width}} {'%':>{pct_width}}"
        narrow_divider = "-" * content_width_local

        lines.append(truncate(narrow_header, content_width_local))
        lines.append(narrow_divider)

        for idx, item in enumerate(page_rows_local, 0):
            selected = selection_hash == item.get("hash")
            focus_marker = ">" if idx == focus_idx else " "
            st = str(item.get("st") or "?")
            name = truncate(str(item.get("name") or "-"), name_width).ljust(name_width)
            trk = truncate(str(item.get("tracker") or "-"), trk_width).ljust(trk_width)
            cat = truncate(str(item.get("category") or "-"), cat_width).ljust(cat_width)
            added_short = str(item.get("added_short") or "-")[:added_width].ljust(added_width)
            pct_value = str(item.get("progress") or "-")
            pct = truncate(pct_value, pct_width).rjust(pct_width)

            if selected:
                name_p = ANSI_RE.sub("", name).ljust(name_width)
                trk_p  = ANSI_RE.sub("", trk).ljust(trk_width)
                cat_p  = ANSI_RE.sub("", cat).ljust(cat_width)
                row_plain = (
                    f"{focus_marker:<1} {idx:<{no_width}} {st:<2} {name_p} "
                    f"{trk_p} {cat_p} {added_short} {pct}"
                )
                row_plain = row_plain[:content_width_local].ljust(content_width_local)
                lines.append(f"{colors.SELECTION}{row_plain}{colors.RESET}")
            else:
                status_col = colors.status_color(item.get("state") or "")
                st_colored = f"{status_col}{st:<2}{colors.RESET}"
                name_colored = f"{status_col}{name}{colors.RESET}"
                focus_col = f"{colors.CYAN}{focus_marker}{colors.RESET}" if idx == focus_idx else " "
                trk_colored = f"{colors.ORANGE}{trk}{colors.RESET}"
                cat_colored = f"{colors.PURPLE}{cat}{colors.RESET}"
                line = f"{focus_col} {idx:<{no_width}} {st_colored} {name_colored} {trk_colored} {cat_colored} {added_short} {pct}"
                if visible_len(line) > content_width_local:
                    line = truncate(line, content_width_local)
                else:
                    line = line + (" " * (content_width_local - visible_len(line)))
                lines.append(line)

            if show_mediainfo_inline:
                raw_item = item.get("raw") or {}
                content_path = get_content_path(raw_item)
                mi_summary = get_mediainfo_summary_cached(str(item.get("hash") or ""), content_path, background_only=True)
                indent = "     mi: "
                width = max(10, content_width_local - len(indent))
                if mi_summary and " • " in mi_summary:
                    parts = mi_summary.split(" • ")
                    colored_parts = []
                    for part in parts:
                        part = part.strip()
                        if any(unit in part.lower() for unit in ["b/s", "gb", "mb", "kb", "bits"]):
                            colored_parts.append(f"{colors.YELLOW}{part}{colors.RESET}")
                        elif part in ["Matroska", "MPEG-4", "AVI", "MP4", "MKV", "WebM"]:
                            colored_parts.append(f"{colors.LAVENDER}{part}{colors.RESET}")
                        else:
                            colored_parts.append(f"{colors.FG_PRIMARY}{part}{colors.RESET}")
                    mi_line = f"{colors.FG_SECONDARY} • {colors.RESET}".join(colored_parts)
                    for line in wrap_ansi(mi_line, width):
                        lines.append(indent + line)
                else:
                    mi_line = mi_summary or "MediaInfo pending..."
                    for line in wrap_ansi(f"{colors.FG_TERTIARY}{mi_line}{colors.RESET}", width):
                        lines.append(indent + line)

            if show_tags:
                tags_raw = str(item.get("tags") or "").strip()
                if tags_raw:
                    tag_parts = []
                    for tag in [t.strip() for t in tags_raw.split(",") if t.strip()]:
                        if "FAIL" in tag.upper():
                            tag_parts.append(f"{colors.ERROR}{tag}{colors.RESET}")
                        elif "cross-seed" in tag.lower():
                            tag_parts.append(f"{colors.ORANGE}{tag}{colors.RESET}")
                        else:
                            tag_parts.append(f"{colors.PURPLE}{tag}{colors.RESET}")
                    tags_line = ", ".join(tag_parts)
                    indent = "     tags: "
                    width = max(10, content_width_local - len(indent))
                    for line in wrap_ansi(tags_line, width):
                        lines.append(indent + line)
        return lines

    def update_mediainfo_cache(rows_sorted: list[dict], page_rows_visible: list[dict]) -> bool:
        """Process MediaInfo queue and active processes. Returns True if redraw needed."""
        nonlocal mi_bootstrap_done, mi_queue, mi_queue_index, mi_last_tick
        now_tick = time.monotonic()
        redraw_needed = False

        # 1. Poll active processes
        for h in list(ACTIVE_MI_PROCESSES.keys()):
            proc, handle, start_time = ACTIVE_MI_PROCESSES[h]
            if proc.poll() is not None:
                # Process finished
                handle.close()
                tmp_path = CACHE_DIR / f"{h}.tmp"
                cache_file = CACHE_DIR / f"{h}.summary"
                
                res = ""
                if tmp_path.exists():
                    res = tmp_path.read_text().strip()
                    tmp_path.unlink(missing_ok=True)
                
                if res:
                    parts = [p.strip() for p in res.split("|") if p.strip()]
                    mi_summary = " • ".join(parts).replace("  ", " ").strip()
                    cache_file.write_text(mi_summary)
                else:
                    cache_file.write_text("MediaInfo failed.")
                
                del ACTIVE_MI_PROCESSES[h]
                if any(r.get("hash") == h for r in page_rows_visible):
                    redraw_needed = True
            elif (now_tick - start_time) > 15.0:
                # Timeout stuck process
                proc.kill()
                handle.close()
                tmp_path = CACHE_DIR / f"{h}.tmp"
                if tmp_path.exists(): tmp_path.unlink(missing_ok=True)
                (CACHE_DIR / f"{h}.summary").write_text("MI: timeout")
                del ACTIVE_MI_PROCESSES[h]

        if not rows_sorted:
            return redraw_needed
            
        if now_tick - mi_last_tick < 0.2: 
            return redraw_needed
        mi_last_tick = now_tick

        # Limit active processes to avoid I/O thrashing
        if len(ACTIVE_MI_PROCESSES) >= 3:
            return redraw_needed

        # 2. Always prioritize current page items
        page_hashes_needed = []
        for item in page_rows_visible:
            hash_value = item.get("hash") or ""
            if not hash_value: continue
            if (CACHE_DIR / f"{hash_value}.summary").exists(): continue
            if hash_value in ACTIVE_MI_PROCESSES: continue
            page_hashes_needed.append(hash_value)

        # 3. Process one item (prioritize page)
        hash_to_start = None
        if page_hashes_needed:
            hash_to_start = page_hashes_needed[0]
        elif mi_queue:
            # Simple queue rotation
            idx = mi_queue_index % len(mi_queue)
            mi_queue_index += 1
            cand = mi_queue[idx]
            if not (CACHE_DIR / f"{cand}.summary").exists() and cand not in ACTIVE_MI_PROCESSES:
                hash_to_start = cand

        if hash_to_start:
            item = next((r for r in rows_sorted if r.get("hash") == hash_to_start), None)
            if item:
                get_mediainfo_summary(hash_to_start, get_content_path(item.get("raw") or {}))
                if any(r.get("hash") == hash_to_start for r in page_rows_visible):
                    redraw_needed = True

        # Bootstrap queue if empty
        if not mi_bootstrap_done:
            mi_queue = [str(r.get("hash")) for r in rows_sorted if r.get("hash")]
            mi_bootstrap_done = True
            mi_queue_index = 0

        return redraw_needed

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        start_width = terminal_width_raw()
        narrow_mode = start_width < FULL_TUI_MIN_WIDTH
        if narrow_mode:
            set_banner(f"Auto narrow mode (width < {FULL_TUI_MIN_WIDTH})", duration=3.0)
        while True:
            now = time.monotonic()
            data_changed = False
            refresh_macros_if_changed()
            # Periodically refresh daemon meta file for header stats
            if args.use_shared_cache and (now - last_meta_read) >= 4.0:
                try:
                    _mt = cache_meta_file.read_text("utf-8") if cache_meta_file.exists() else ""
                    cache_meta = json.loads(_mt) if _mt else {}
                except Exception:
                    pass
                last_meta_read = now
            current_term_w = terminal_width_raw() if narrow_mode and not in_tab_view else terminal_width()
            if narrow_mode_auto and not in_tab_view:
                auto_narrow = current_term_w < FULL_TUI_MIN_WIDTH
                if auto_narrow != narrow_mode:
                    narrow_mode = auto_narrow
                    have_full_draw = False
                    need_redraw = True
            if current_term_w != last_term_w:
                last_term_w = current_term_w
                have_full_draw = False
                need_redraw = True
            if not cached_rows or (now - cache_time) >= fetch_interval:
                if args.use_shared_cache:
                    cache_env = {**os.environ, "QBIT_URL": api_url, "QBIT_USER": username, "QBIT_PASS": password}
                    _agent_result = subprocess.run(
                        [
                            sys.executable, str(args.cache_agent_cmd),
                            "--max-age", str(args.cache_max_age),
                            "--wait-fresh", str(args.cache_wait_fresh),
                            "--ensure-daemon",
                            "--allow-stale" if args.cache_allow_stale else "--no-allow-stale",
                        ],
                        env=cache_env,
                        capture_output=True,
                        text=True,
                    )
                    if _agent_result.returncode == 0 and _agent_result.stdout.strip():
                        raw = _agent_result.stdout
                        cache_hit_count += 1
                    else:
                        set_banner("Cache agent failed; falling back to direct API")
                        raw = qbit_request(opener, api_url, "GET", "/api/v2/torrents/info")
                        direct_hit_count += 1
                else:
                    raw = qbit_request(opener, api_url, "GET", "/api/v2/torrents/info")
                    direct_hit_count += 1
                if raw.startswith("Error:") or raw.startswith("HTTP "):
                    set_banner(f"Network error: {raw}")
                    cache_time = now
                else:
                    try:
                        torrents = json.loads(raw) if raw else []
                        rows = build_rows(torrents, tracker_keyword_map)
                        cached_torrents = torrents
                        cached_rows = rows
                        data_changed = True
                    except json.JSONDecodeError:
                        set_banner("Error: Invalid JSON response")
                    cache_time = now

            rows_to_render = cached_rows
            if scope != "all":
                rows_to_render = [r for r in rows_to_render if state_group(r.get("raw", {}).get("state", "")) == scope]

            rows_to_render = apply_filters(rows_to_render, filters)

            sort_field = sort_fields[sort_index]
            def sort_key(row: dict):
                raw = row.get("raw", {})
                if sort_field == "added_on": return raw.get("added_on") or 0
                if sort_field == "name": return row.get("name", "")
                if sort_field == "state": return state_group(raw.get("state", ""))
                if sort_field == "ratio": return raw.get("ratio") or 0
                if sort_field == "progress": return raw.get("progress") or 0
                if sort_field == "eta": return raw.get("eta") or 0
                if sort_field == "size": return raw.get("size") or raw.get("total_size") or 0
                if sort_field == "dlspeed": return raw.get("dlspeed") or 0
                if sort_field == "upspeed": return raw.get("upspeed") or 0
                return row.get("name", "")
            rows_to_render.sort(key=sort_key, reverse=sort_desc)
            
            page_rows, total_pages, page = format_rows(rows_to_render, page, args.page_size)

            if focus_idx >= len(page_rows):
                focus_idx = max(0, len(page_rows) - 1)

            # Update selection state
            selected_row_all = None
            if selection_hash:
                selected_row_all = next((r for r in cached_rows if r.get("hash") == selection_hash), None)
                if not selected_row_all:
                    selection_hash = selection_name = None
                    in_tab_view = False
                    set_banner("Selection cleared: item removed.")
                    data_changed = True
                else:
                    selection_name = selected_row_all.get("name") or selection_name

            # Background MediaInfo processing
            if update_mediainfo_cache(rows_to_render, page_rows):
                data_changed = True # Trigger redraw to show new MI data

            if NEED_RESIZE:
                term_w = current_term_w
                have_full_draw = False
                need_redraw = True
                NEED_RESIZE = False

            # Build cache_info for header display
            _now_wall = time.time()
            _fetched_at = cache_meta.get("fetched_at")
            _cache_age: float | None = None
            if _fetched_at is not None:
                try:
                    _cache_age = max(0.0, _now_wall - float(_fetched_at))
                except Exception:
                    pass
            _pid_val = cache_meta.get("daemon_pid")
            _daemon_alive = bool(_pid_val and cache_meta.get("source") not in ("daemon_idle_exit",))
            _total_hits = cache_hit_count + direct_hit_count
            cache_info = {
                "enabled": args.use_shared_cache,
                "base_path": str(_cache_base),
                "interval_s": cache_meta.get("effective_interval_s"),
                "cache_hits": cache_hit_count,
                "direct_hits": direct_hit_count,
                "daemon_running": _daemon_alive,
                "cache_age_s": _cache_age,
                "items": cache_meta.get("items"),
                "last_error": cache_meta.get("last_error", ""),
                "active_leases": cache_meta.get("active_leases", 0),
            }

            if data_changed or need_redraw:
                term_w = current_term_w
                banner_line = ""
                if banner_text and time.time() < banner_until:
                    banner_line = f"{colors.YELLOW_BOLD}{banner_text}{colors.RESET}"
                elif not selection_hash:
                    banner_line = f"{colors.FG_SECONDARY}Select an item.{colors.RESET}"
                else:
                    short_hash = (selection_hash or "")[:10]
                    banner_line = f"{colors.CYAN_BOLD}Selected:{colors.RESET} {selection_name} ({short_hash})"

                if narrow_mode and not in_tab_view:
                    content_width = term_w
                    divider_line = "-" * content_width
                    list_block_lines = build_narrow_list_block(page_rows, content_width)
                else:
                    content_width = term_w
                    list_block_lines, table_render_width = build_list_block(page_rows, content_width)
                    divider_line = "-" * table_render_width

                if in_tab_view and selection_hash:
                    output_buffer = "\033[H\033[J" # Start with clear

                    # In tab view, use full terminal width for consistency
                    tab_display_width = term_w
                    content_width = tab_display_width  # Override for footer/dividers
                    divider_line = "-" * tab_display_width

                    # Use new v2 header
                    header_lines = draw_header_v2(
                        colors=colors,
                        api_url=api_url,
                        version=VERSION,
                        torrents=cached_torrents,
                        scope=scope,
                        sort_field=sort_fields[sort_index],
                        sort_desc=sort_desc,
                        page=page,
                        total_pages=total_pages,
                        filters=filters,
                        width=tab_display_width,
                        cache_info=cache_info,
                    )
                    for line in header_lines:
                        tui_print(line)

                    # Banner/selection info (if needed)
                    if banner_line:
                        tui_print(banner_line)
                        tui_print("")
                    selected_row = next((r for r in page_rows if r.get("hash") == selection_hash), None)
                    tab_divider = "-" * tab_display_width
                    if not selected_row:
                        tui_print("Selection not available on this page.")
                        tui_print(tab_divider)
                    else:
                        available_tabs = resolve_available_tabs(opener, api_url, selected_row)
                        if not available_tabs: available_tabs = ["Info"]
                        active_label = tabs[active_tab]
                        if active_label not in available_tabs:
                            active_tab = tabs.index(available_tabs[0])
                            active_label = tabs[active_tab]
                        tab_labels = []
                        for label in available_tabs:
                            if label == active_label:
                                tab_labels.append(f"{colors.YELLOW_BOLD}[{label}]{colors.RESET}")
                            else:
                                tab_labels.append(f"{colors.FG_SECONDARY}{label}{colors.RESET}")
                        tui_print("Tabs: " + " ".join(tab_labels))
                        tab_divider = "-" * tab_display_width
                        tui_print(tab_divider)
                        tab_width = tab_display_width
                        max_rows = max(10, shutil.get_terminal_size((100, 30)).lines - 15)
                        if active_label == "Info": content_lines = render_info_lines(selected_row, tab_width)
                        elif active_label == "Trackers":
                            trackers = fetch_trackers(opener, api_url, selection_hash)
                            content_lines = render_trackers_lines(trackers, tab_width, max_rows)
                        elif active_label == "Content":
                            files = fetch_files(opener, api_url, selection_hash)
                            content_lines = render_files_lines(
                                files,
                                tab_width,
                                max_rows,
                                get_content_path(selected_row.get("raw") or {}),
                            )
                        elif active_label == "Peers":
                            peers_payload = fetch_peers(opener, api_url, selection_hash)
                            content_lines = render_peers_lines(peers_payload, tab_width, max_rows)
                        else: content_lines = render_mediainfo_lines(selected_row, tab_width, colors)
                        for line in content_lines[:max_rows]: tui_print(line)
                        tui_print(tab_divider)
                        footer_row = 10 + len(content_lines[:max_rows]) + 1
                else:
                    if not have_full_draw:
                        output_buffer = "\033[H\033[J" # Start with clear
                        if narrow_mode:
                            header_lines = draw_header_minimal(
                                colors=colors,
                                version=VERSION,
                                scope=scope,
                                page=page,
                                total_pages=total_pages,
                                width=content_width
                            )
                        else:
                            header_lines = draw_header_full_compact(
                                colors=colors,
                                api_url=api_url,
                                version=VERSION,
                                torrents=cached_torrents,
                                scope=scope,
                                sort_field=sort_fields[sort_index],
                                sort_desc=sort_desc,
                                page=page,
                                total_pages=total_pages,
                                filters=filters,
                                width=content_width,
                                cache_info=cache_info,
                            )
                        for line in header_lines:
                            tui_print(line)

                        # Banner line (if any)
                        if banner_line:
                            tui_print(banner_line)
                            tui_print("")

                        # Torrent list
                        list_start_row = len(header_lines) + (2 if banner_line else 0)
                        for line in list_block_lines: tui_print(line)
                        tui_print(divider_line)
                        list_block_height = len(list_block_lines) + 1
                        footer_row = list_start_row + list_block_height
                        have_full_draw = True
                    else:
                        # For incremental updates, force full redraw to keep header in sync
                        # This ensures header stats are always current
                        have_full_draw = False
                        need_redraw = True
                        continue
                        print_at(row, divider_line)
                        list_block_height = current_height
                        footer_row = row + 1

                # Render Footer with absolute positioning
                row = footer_row

                if narrow_mode and not in_tab_view:
                    print_at(row, divider_line); row += 1
                    tui_flush()
                    need_redraw = False
                    continue

                # Show filters if any active
                if filters:
                    print_at(row, format_filters_line(filters, colors)); row += 1
                    print_at(row, divider_line); row += 1

                if in_tab_view and selection_hash:
                    footer_context = "main"
                    active_label = tabs[active_tab] if active_tab < len(tabs) else "Info"
                    if active_label == "Trackers":
                        footer_context = "trackers"
                    elif active_label == "MediaInfo":
                        footer_context = "mediainfo"
                    elif active_label == "Info":
                        footer_context = "info"
                    elif active_label == "Content":
                        footer_context = "content"
                    footer_lines = draw_footer_v2(
                        colors=colors,
                        context=footer_context,
                        width=content_width,
                        has_selection=bool(selection_hash),
                        macros=macros_global
                    )
                else:
                    footer_lines = draw_footer_full_compact(
                        colors=colors,
                        width=content_width,
                        has_selection=bool(selection_hash),
                        macros=macros_global,
                        sep_width=table_render_width,
                    )

                for line in footer_lines:
                    print_at(row, line)
                    row += 1

                # Debug key display
                print_at(row, divider_line); row += 1
                print_at(row, f"Last Key: {colors.CYAN}{last_key_debug}{colors.RESET}\033[J")
                tui_flush()
                need_redraw = False

            # Wait for input or timeout
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not r:
                continue

            events = read_input_queue()
            for key in events:
                if not key: continue
                last_key_debug = key
                need_redraw = True

                # ── Tab-view guard ────────────────────────────────────────────
                if in_tab_view and selection_hash:
                    active_label = tabs[active_tab]

                    # q exits tab view (bare ESC is discarded by read_input_queue)
                    if key == "q":
                        in_tab_view = False
                        have_full_draw = False
                        continue

                    # Arrow keys → tab navigation
                    if key == ".":   # Right arrow → next tab
                        cycle_tabs(direction=1, exit_after_last=False)
                        continue
                    if key == ",":   # Left arrow → prev tab
                        cycle_tabs(direction=-1, exit_after_last=False)
                        continue

                    # Tab-specific action keys
                    if active_label == "Trackers" and key == "R":
                        reannounce_torrent(opener, api_url, selection_hash)
                        set_banner("Reannouncing…")
                        continue

                    # Tab / Shift-Tab / Ctrl-Tab fall through to cycle_tabs below
                    if key in {"\t", "SHIFT_TAB", "CTRL_TAB"}:
                        pass
                    else:
                        continue   # ignore all other keys; don't exit
                # ─────────────────────────────────────────────────────────────

                if key == "\x11": return 0 # Ctrl-Q
                if key == "X":
                    if CACHE_DIR.exists():
                        shutil.rmtree(CACHE_DIR)
                        set_banner("MediaInfo cache cleared.")
                    continue
                if key == "?":
                    # Prepare help content
                    help_lines = []
                    help_lines.append(f"{colors.CYAN_BOLD}QBITUI HELP{colors.RESET}")
                    help_lines.append("")
                    help_lines.append(f"{colors.YELLOW}Navigation:{colors.RESET}  ↑/↓ or '/=move  , .=page  PgUp/PgDn=page  0-9=jump to row  Space/Enter=toggle-select")
                    help_lines.append(f"             Tab=open detail tabs  Shift-Tab=prev tab  ~=back/clear selection")
                    help_lines.append(f"{colors.YELLOW}Scope:{colors.RESET}       a=All  w=Downloading  u=Uploading  v=Paused  e=Completed  g=Error")
                    help_lines.append(f"{colors.YELLOW}Sort:{colors.RESET}        s=cycle sort field  o=toggle asc/desc")
                    help_lines.append(f"{colors.YELLOW}Filter:{colors.RESET}      f=status  c=category  #=tag  l=compound  x=pause/resume all  p=presets")
                    help_lines.append(f"{colors.YELLOW}View:{colors.RESET}        z=reset all  t=tags  d=date  h=hash  n=narrow  m=media inline  X=clear MI cache")
                    help_lines.append(f"{colors.YELLOW}Global:{colors.RESET}      ?=help  i=cache status  q=quit  Ctrl-Q=quit")
                    help_lines.append(f"{colors.YELLOW}Actions:{colors.RESET}     (select a torrent first)")
                    help_lines.append(f"             P=Pause/Resume  V=Verify  C=Category  E=Tags  T=Trackers  Q=QC  D=Delete")
                    help_lines.append(f"             Tab=Content tabs  M=Macro menu  Shift+1-9=direct macro")

                    help_lines.append(f"\n{colors.CYAN_BOLD}FILTER REFERENCE{colors.RESET}")
                    help_lines.append(f"{colors.YELLOW}f  Status:{colors.RESET}   Groups: downloading  seeding  paused  completed  error  checking  all")
                    help_lines.append(f"             Comma-list for multiple: seeding,paused")
                    help_lines.append(f"             Prefix ! to exclude:    !completed")
                    help_lines.append(f"{colors.YELLOW}c  Category:{colors.RESET} Exact category name  (case-insensitive)")
                    help_lines.append(f"             -  matches uncategorized torrents")
                    help_lines.append(f"             !name  excludes that category")
                    help_lines.append(f"{colors.YELLOW}#  Tag:{colors.RESET}      tagA              has tag tagA")
                    help_lines.append(f"             tagA,tagB         has tagA OR tagB")
                    help_lines.append(f"             tagA+tagB         has tagA AND tagB")
                    help_lines.append(f"             !tagA             does NOT have tagA")
                    help_lines.append(f"             !tagA+!tagB       has neither tagA nor tagB")
                    help_lines.append(f"{colors.YELLOW}l  Compound:{colors.RESET} Space-separated key=value pairs in one line:")
                    help_lines.append(f"             q=name  cat=category  tag=tagexpr  hash=abc  status=grp")
                    help_lines.append(f"             Example: q=ubuntu cat=linux status=seeding")
                    help_lines.append(f"{colors.YELLOW}x  Toggle:{colors.RESET}   Pause / resume all filters without clearing them")
                    help_lines.append(f"{colors.YELLOW}p  Presets:{colors.RESET}  Save/load named filter sets. At the prompt:")
                    help_lines.append(f"             1-9       load filter set from slot N")
                    help_lines.append(f"             s1-s9     save current filters to slot N")
                    help_lines.append(f"             Clearing a filter: press its key and leave blank")

                    help_lines.append(f"\n{colors.CYAN_BOLD}STATUS MAPPING TABLE{colors.RESET}")
                    help_lines.append(f"{'Code':<5} {'API Term':<20} {'Group/Description':<30}")
                    help_lines.append("-" * 60)
                    for m in STATUS_MAPPING:
                        help_lines.append(f"{m['code']:<5} {m['api']:<20} {m['desc']:<30}")

                    scroll_offset = 0
                    tty.setraw(fd)
                    need_redraw_help = True
                    
                    while True:
                        if NEED_RESIZE:
                            need_redraw_help = True
                            NEED_RESIZE = False

                        cols_term, rows_term = shutil.get_terminal_size()
                        view_height = max(5, rows_term - 2)
                        
                        if need_redraw_help:
                            # Clamp scroll offset
                            scroll_offset = max(0, min(scroll_offset, max(0, len(help_lines) - view_height)))
                            
                            # Build frame
                            frame = "\033[H\033[2J" # Clear and move to top
                            visible_lines = help_lines[scroll_offset : scroll_offset + view_height]
                            for line in visible_lines:
                                frame += line + "\r\n"
                            
                            # Render footer
                            progress = 100
                            if len(help_lines) > view_height:
                                progress = int((scroll_offset / (len(help_lines) - view_height)) * 100)
                            
                            footer_text = f"{colors.FG_SECONDARY}[Help] Scroll: ↑/↓ (or ' /), , . (page)  Exit: q Esc Enter  ({progress}%){colors.RESET}"
                            frame += f"\033[{rows_term};1H{footer_text}"
                            
                            sys.stdout.write(frame)
                            sys.stdout.flush()
                            need_redraw_help = False
                        
                        # Wait for input
                        r_in, _, _ = select.select([sys.stdin], [], [], 0.1)
                        if not r_in:
                            continue
                        
                        keys_in = read_input_queue()
                        exit_help = False
                        for k in keys_in:
                            if k in ("q", "Q", "\x1b", "\r", "\n", " "): 
                                exit_help = True
                                break
                            if k in ("'", "k"): 
                                if scroll_offset > 0:
                                    scroll_offset -= 1
                                    need_redraw_help = True
                            elif k in ("/", "j"): 
                                if scroll_offset < len(help_lines) - view_height:
                                    scroll_offset += 1
                                    need_redraw_help = True
                            elif k == ",": 
                                if scroll_offset > 0:
                                    scroll_offset = max(0, scroll_offset - (view_height // 2))
                                    need_redraw_help = True
                            elif k == ".": 
                                if scroll_offset < len(help_lines) - view_height:
                                    scroll_offset = min(len(help_lines) - view_height, scroll_offset + (view_height // 2))
                                    need_redraw_help = True
                        
                        if exit_help: break

                    have_full_draw = False
                    continue
                if key == "i":
                    # ── Cache Status Popup ──────────────────────────────────
                    # Fetch live status via cache agent --status
                    _ci = cache_info  # from last loop iteration
                    _cs_lines = []
                    _cs_lines.append(f"{colors.CYAN_BOLD}CACHE STATUS{colors.RESET}")
                    _cs_lines.append("")
                    if not _ci.get("enabled"):
                        _cs_lines.append(f"{colors.FG_TERTIARY}Shared cache: OFF  (using direct qbit API){colors.RESET}")
                    else:
                        _dot = f"{colors.GREEN}●{colors.RESET}" if _ci.get("daemon_running") else f"{colors.ERROR}○{colors.RESET}"
                        _path = str(_ci.get("base_path", "")).replace(str(Path.home()), "~")
                        _cs_lines.append(
                            f"{colors.FG_SECONDARY}Daemon:{colors.RESET}  {_dot}  "
                            f"{colors.FG_SECONDARY}Path:{colors.RESET} {colors.BLUE}{_path}{colors.RESET}"
                        )
                        _interval = _ci.get("interval_s")
                        _interval_s = f"{float(_interval):.1f}s" if _interval is not None else "unknown"
                        _items = _ci.get("items")
                        _items_s = str(_items) if _items is not None else "unknown"
                        _leases = _ci.get("active_leases", 0)
                        _age = _ci.get("cache_age_s")
                        _age_s = f"{float(_age):.1f}s" if _age is not None else "unknown"
                        _cs_lines.append(
                            f"{colors.FG_SECONDARY}Cache:{colors.RESET}   "
                            f"age {colors.YELLOW}{_age_s}{colors.RESET}  "
                            f"refresh every {colors.YELLOW}{_interval_s}{colors.RESET}  "
                            f"items {colors.CYAN}{_items_s}{colors.RESET}  "
                            f"leases {colors.FG_SECONDARY}{_leases}{colors.RESET}"
                        )
                        _hits = _ci.get("cache_hits", 0)
                        _direct = _ci.get("direct_hits", 0)
                        _total = _hits + _direct
                        _pct = f"{(_hits / _total * 100):.0f}%" if _total > 0 else "--"
                        _cs_lines.append("")
                        _cs_lines.append(f"{colors.FG_SECONDARY}Session request stats:{colors.RESET}")
                        _cs_lines.append(
                            f"  {colors.CYAN}↑ from cache{colors.RESET}  {colors.CYAN_BOLD}{_hits}{colors.RESET} reqs"
                            f"   {colors.BLUE}↓ direct qbit{colors.RESET}  {colors.BLUE_BOLD}{_direct}{colors.RESET} reqs"
                            f"   hit rate {colors.GREEN_BOLD}{_pct}{colors.RESET}"
                        )
                        _last_err = str(_ci.get("last_error") or "")
                        _cs_lines.append("")
                        if _last_err:
                            _cs_lines.append(f"{colors.FG_SECONDARY}Last error:{colors.RESET}  {colors.ERROR}{_last_err}{colors.RESET}")
                        else:
                            _cs_lines.append(f"{colors.FG_SECONDARY}Last error:{colors.RESET}  {colors.FG_TERTIARY}none{colors.RESET}")
                        # Live status from agent
                        _cs_lines.append("")
                        _cs_lines.append(f"{colors.FG_TERTIARY}Fetching live daemon status…{colors.RESET}")
                    _cs_lines.append("")
                    _cs_lines.append(f"{colors.FG_TERTIARY}[any key] dismiss{colors.RESET}")

                    # Render the popup (same pattern as help overlay)
                    _cols, _rows = shutil.get_terminal_size()
                    _frame = "\033[H\033[2J"
                    for _ln in _cs_lines:
                        _frame += _ln + "\r\n"
                    sys.stdout.write(_frame)
                    sys.stdout.flush()

                    # If cache enabled, fetch live status in background and re-render
                    if _ci.get("enabled"):
                        _cache_env = {**os.environ, "QBIT_URL": api_url, "QBIT_USER": username, "QBIT_PASS": password}
                        try:
                            _status_result = subprocess.run(
                                [sys.executable, str(args.cache_agent_cmd), "--status"],
                                env=_cache_env,
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            if _status_result.returncode == 0 and _status_result.stdout.strip():
                                try:
                                    _sd = json.loads(_status_result.stdout)
                                    # Rebuild lines with live data
                                    _cs_lines2 = []
                                    _cs_lines2.append(f"{colors.CYAN_BOLD}CACHE STATUS  (live){colors.RESET}")
                                    _cs_lines2.append("")
                                    _d_running = _sd.get("daemon_running", False)
                                    _d_pid = _sd.get("daemon_pid", 0)
                                    _ddot = f"{colors.GREEN}●{colors.RESET}" if _d_running else f"{colors.ERROR}○{colors.RESET}"
                                    _dpid_s = f"pid {_d_pid}" if _d_pid else "not running"
                                    _dpath = str(_sd.get("cache_file", "")).replace(str(Path.home()), "~")
                                    _cs_lines2.append(
                                        f"{colors.FG_SECONDARY}Daemon:{colors.RESET}  {_ddot} {_dpid_s}  "
                                        f"{colors.FG_SECONDARY}File:{colors.RESET} {colors.BLUE}{_dpath}{colors.RESET}"
                                    )
                                    _cage = _sd.get("cache_age_s")
                                    _cage_s = f"{float(_cage):.1f}s" if _cage is not None else "unknown"
                                    _meta = _sd.get("meta", {})
                                    _eff = _meta.get("effective_interval_s")
                                    _eff_s = f"{float(_eff):.1f}s" if _eff is not None else "unknown"
                                    _mitems = _meta.get("items")
                                    _mitems_s = str(_mitems) if _mitems is not None else "unknown"
                                    _al = _sd.get("active_lease_count", 0)
                                    _cs_lines2.append(
                                        f"{colors.FG_SECONDARY}Cache:{colors.RESET}   "
                                        f"age {colors.YELLOW}{_cage_s}{colors.RESET}  "
                                        f"refresh every {colors.YELLOW}{_eff_s}{colors.RESET}  "
                                        f"items {colors.CYAN}{_mitems_s}{colors.RESET}  "
                                        f"active leases {colors.FG_SECONDARY}{_al}{colors.RESET}"
                                    )
                                    _al_list = _sd.get("active_leases", [])
                                    if _al_list:
                                        _cs_lines2.append("")
                                        _cs_lines2.append(f"{colors.FG_SECONDARY}Active leases:{colors.RESET}")
                                        for _lease in _al_list[:6]:
                                            _lcid = _lease.get("client_id", "?")
                                            _lint = _lease.get("requested_interval_s", "?")
                                            _cs_lines2.append(f"  {colors.FG_TERTIARY}{_lcid}  interval {_lint}s{colors.RESET}")
                                    _cs_lines2.append("")
                                    _cs_lines2.append(f"{colors.FG_SECONDARY}Session request stats:{colors.RESET}")
                                    _hits2 = _ci.get("cache_hits", 0)
                                    _direct2 = _ci.get("direct_hits", 0)
                                    _total2 = _hits2 + _direct2
                                    _pct2 = f"{(_hits2 / _total2 * 100):.0f}%" if _total2 > 0 else "--"
                                    _cs_lines2.append(
                                        f"  {colors.CYAN}↑ from cache{colors.RESET}  {colors.CYAN_BOLD}{_hits2}{colors.RESET}"
                                        f"   {colors.BLUE}↓ direct qbit{colors.RESET}  {colors.BLUE_BOLD}{_direct2}{colors.RESET}"
                                        f"   hit rate {colors.GREEN_BOLD}{_pct2}{colors.RESET}"
                                    )
                                    _lerr = str(_meta.get("last_error") or "")
                                    _cs_lines2.append("")
                                    if _lerr:
                                        _cs_lines2.append(f"{colors.FG_SECONDARY}Last error:{colors.RESET}  {colors.ERROR}{_lerr}{colors.RESET}")
                                    else:
                                        _cs_lines2.append(f"{colors.FG_SECONDARY}Last error:{colors.RESET}  {colors.FG_TERTIARY}none{colors.RESET}")
                                    _cs_lines2.append("")
                                    _cs_lines2.append(f"{colors.FG_TERTIARY}[any key] dismiss{colors.RESET}")
                                    _frame2 = "\033[H\033[2J"
                                    for _ln2 in _cs_lines2:
                                        _frame2 += _ln2 + "\r\n"
                                    sys.stdout.write(_frame2)
                                    sys.stdout.flush()
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # Wait for any key
                    select.select([sys.stdin], [], [], 30)
                    read_input_queue()
                    have_full_draw = False
                    continue
                if key == "`":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[H\033[J")
                    capture_key_sequences()
                    tty.setraw(fd)
                    have_full_draw = False
                    continue
                if key == "~":
                    if selection_hash:
                        selection_hash = selection_name = None
                        set_banner("Selection cleared.")
                        have_full_draw = False
                    continue
                if key == "CTRL_TAB":
                    cycle_tabs(direction=1, exit_after_last=False)
                    continue
                if key == "SHIFT_TAB":
                    cycle_tabs(direction=-1, exit_after_last=False)
                    continue
                if key == "'":
                    if page_rows: focus_idx = max(0, focus_idx - 1)
                    continue
                if key == "/":
                    if page_rows: focus_idx = min(len(page_rows) - 1, focus_idx + 1)
                    continue
                if key in (" ", "\r", "\n"):
                    if page_rows and 0 <= focus_idx < len(page_rows):
                        focused = page_rows[focus_idx]
                        if selection_hash and focused.get("hash") == selection_hash:
                            selection_hash = selection_name = None
                            set_banner("Selection cleared.")
                            have_full_draw = False
                        else:
                            selection_hash = focused.get("hash")
                            selection_name = focused.get("name")
                            active_tab = 0 # Reset tab focus for new selection
                    continue
                if key == "\t":
                    cycle_tabs(direction=1, exit_after_last=True)
                    continue
                if key == "m":
                    show_mediainfo_inline = not show_mediainfo_inline
                    have_full_draw = False
                    continue
                if key == "z":
                    scope = "all"; page = 0; sort_index = 0; sort_desc = True; filters = []
                    show_tags = show_mediainfo_inline = show_full_hash = False
                    show_added = True; focus_idx = 0; in_tab_view = False; active_tab = 0
                    narrow_mode = terminal_width_raw() < FULL_TUI_MIN_WIDTH
                    narrow_mode_auto = True
                    selection_hash = selection_name = None
                    have_full_draw = False
                    continue
                if key == ",":
                    if page > 0: page -= 1; focus_idx = 0
                    have_full_draw = False
                    continue
                if key == ".":
                    if page < total_pages - 1: page += 1; focus_idx = 0
                    have_full_draw = False
                    continue
                if key == "a": scope = "all"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "w": scope = "downloading"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "u": scope = "uploading"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "v": scope = "paused"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "e": scope = "completed"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "g": scope = "error"; page = 0; focus_idx = 0; have_full_draw = False; continue
                if key == "s": sort_index = (sort_index + 1) % len(sort_fields); have_full_draw = False; continue
                if key == "o": sort_desc = not sort_desc; have_full_draw = False; continue
                if key == "t": show_tags = not show_tags; have_full_draw = False; continue
                if key == "d": show_added = not show_added; have_full_draw = False; continue
                if key == "h": show_full_hash = not show_full_hash; have_full_draw = False; continue
                if key in ("n", "N"):
                    narrow_mode_auto = False
                    narrow_mode = not narrow_mode
                    set_banner(f"Narrow mode {'ON' if narrow_mode else 'OFF'}")
                    have_full_draw = False
                    continue
                if key in "0123456789":
                    idx = int(key)
                    if idx < len(page_rows):
                        focus_idx = idx
                        selection_hash = page_rows[idx].get("hash")
                        selection_name = page_rows[idx].get("name")
                        active_tab = 0 # Reset tab focus for new selection
                    continue
                if key == "c":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    val = read_line("Category  (exact name  -=uncategorized  !name=exclude  blank=clear): ").strip()
                    filters = [f for f in filters if f.get("type") != "category"]
                    if val:
                        negate = False
                        if val.startswith("!"): negate = True; val = val[1:]
                        filters.append({"type": "category", "value": val, "enabled": True, "negate": negate})
                    tty.setraw(fd); have_full_draw = False; continue
                if key == "#":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    val = read_line("Tag  (tagA  tagA,tagB=OR  tagA+tagB=AND  !tagA=NOT  blank=clear): ").strip()
                    filters = [f for f in filters if f.get("type") != "tag"]
                    if val:
                        parsed = parse_tag_filter(val)
                        if parsed: parsed["enabled"] = True; filters.append(parsed)
                    tty.setraw(fd); have_full_draw = False; continue
                if key == "l":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    val = read_line("Compound  (q=name  cat=cat  tag=expr  hash=abc  status=grp  space-separated  blank=clear): ").strip()
                    if val: filters = parse_filter_line(val, filters)
                    tty.setraw(fd); have_full_draw = False; continue
                if key == "f":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    all_status_groups = sorted(STATUS_FILTER_MAP.keys())
                    all_api_terms = sorted(API_TERM_MAP.keys())
                    all_status_codes = sorted(list(set(m["code"].lower() for m in STATUS_MAPPING)))
                    current_status_filter_values = []
                    for f in filters:
                        if f["type"] == "status" and f.get("enabled", True):
                            current_status_filter_values.extend(f["values"])
                    _cur = ", ".join(current_status_filter_values) if current_status_filter_values else "none"
                    val = read_line(
                        f"Status  (downloading  seeding  paused  completed  error  checking  all"
                        f"  comma-list  !term=exclude  blank=clear)  current={_cur}: "
                    ).strip()
                    filters = [f for f in filters if f.get("type") != "status"]
                    if val:
                        negate = False
                        if val.startswith("!"):
                            negate = True
                            val = val[1:]
                        statuses = [s.strip().lower() for s in val.split(",") if s.strip()]
                        valid_terms = set(all_status_groups) | set(all_api_terms) | set(all_status_codes)
                        statuses = [s for s in statuses if s in valid_terms]
                        if statuses:
                            filters.append({"type": "status", "values": statuses, "enabled": True, "negate": negate})
                    tty.setraw(fd); have_full_draw = False; continue
                if key == "x":
                    if filters:
                        filters = [f for f in filters if not f.get("enabled", True)] if all(f.get("enabled", True) for f in filters) else [dict(f, enabled=True) for f in filters]
                    continue
                if key == "p":
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    tui_print("\nPresets (Slots):")
                    for s_idx in range(1, 10):
                        slot = presets.get(str(s_idx))
                        label = summarize_filters(restore_filters(slot)) if slot else "-"
                        tui_print(f"  {s_idx}: {label}")
                    val = read_line("\nSelect slot to load (1-9), or s[1-9] to save current: ").strip()
                    if val.isdigit() and val in presets:
                        filters = restore_filters(presets[val])
                        set_banner(f"Loaded slot {val}")
                    elif val.startswith("s") and val[1:].isdigit():
                        s_idx = val[1:]
                        presets[s_idx] = serialize_filters(filters)
                        save_presets(PRESET_FILE, presets)
                        set_banner(f"Saved current to slot {s_idx}")
                    tty.setraw(fd); have_full_draw = False; continue

                # === MACRO MENU ===
                if key == "M":
                    if not selection_hash:
                        set_banner("No hash selected")
                        continue

                    # Load macros from config
                    refresh_macros_if_changed(force=True)
                    macros = macros_global

                    if not macros:
                        set_banner(f"No macros configured ({macro_config_path})")
                        continue

                    # Limit to 9 slots
                    macros = macros[:9]

                    # Switch to cooked mode for readable input
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

                    # Display menu
                    tui_print(f"\nMacros for hash {selection_hash[:8]}:")
                    for idx, macro in enumerate(macros, start=1):
                        tui_print(f"  {idx}: {macro['desc']}")
                    tui_print("  [ESC] Cancel")
                    tui_flush()  # Flush menu display before prompting

                    # Get selection
                    choice = read_line("\nSelect macro (1-9): ").strip()

                    # Cancel on empty input or ESC
                    if not choice or choice.startswith("\x1b"):
                        # Empty Enter or ESC pressed - cancel silently
                        pass
                    elif choice.isdigit() and 1 <= int(choice) <= len(macros):
                        macro = macros[int(choice) - 1]
                        result = run_macro(macro, selection_hash)
                        set_banner(result, duration=4.0)
                    else:
                        set_banner("Invalid selection")

                    # Return to raw mode
                    tty.setraw(fd)
                    have_full_draw = False
                    continue

                # === DIRECT MACRO EXECUTION (Shift+1-9) ===
                if selection_hash and key in "!@#$%^&*(":
                    # Map shifted keys to macro numbers: ! → 1, @ → 2, etc.
                    shift_map = {"!": 1, "@": 2, "#": 3, "$": 4, "%": 5, "^": 6, "&": 7, "*": 8, "(": 9}
                    macro_idx = shift_map.get(key)
                    refresh_macros_if_changed(force=True)
                    macros_live = macros_global[:9]

                    if macro_idx:
                        if macro_idx <= len(macros_live):
                            macro = macros_live[macro_idx - 1]
                            result = run_macro(macro, selection_hash)
                            set_banner(result, duration=4.0)
                        else:
                            set_banner(f"Macro {macro_idx} not configured")
                    continue

                # Actions
                if selection_hash and key.upper() in "PVCETQD":
                    selected_item = next((r for r in page_rows if r.get("hash") == selection_hash), None)
                    if selected_item:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                        res = apply_action(opener, api_url, key.upper(), selected_item)
                        set_banner(f"Action {key.upper()}: {res}")
                        tty.setraw(fd); have_full_draw = False; continue

        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
