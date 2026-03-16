# qbitui

Interactive terminal dashboard for [qBittorrent](https://www.qbittorrent.org/). Navigate, filter, and manage your torrents without leaving the terminal.

## Features

- **Zero dependencies** — pure Python 3 stdlib (optional `pyyaml` for config files)
- **Fast TUI** — paged list view with minimal redraws, tmux-friendly
- **Filter & sort** — live filter prompt, tab-based category views
- **Selection mode** — multi-select with bulk operations
- **MediaInfo** — optional background mediainfo cache with `--mediainfo`
- **Hotkey-driven** — no arrow keys required; works cleanly over SSH/mosh

## Requirements

- Python 3.9+
- qBittorrent with Web UI enabled
- `mediainfo` (optional, for media metadata column)

## Usage

```bash
# Basic — connects to localhost:8080
./bin/qbit-dashboard.py

# Custom host/port
./bin/qbit-dashboard.py --host 10.0.0.10 --port 9003

# With credentials
./bin/qbit-dashboard.py --username admin --password secret

# Enable mediainfo column
./bin/qbit-dashboard.py --mediainfo
```

### Config file (optional)

Place a `config/qbit-dashboard.yml` (or set `QBITTORRENT_CONFIG_FILE`) with:

```yaml
qbittorrent:
  api_url: http://localhost:9003
  username: admin
  password_file: /path/to/password.env
```

## Keymap

| Key | Action |
|-----|--------|
| `,` / `.` | Page prev / next |
| `'` / `/` | Cursor up / down |
| `l` | Filter prompt |
| `t` | Toggle tags column |
| `Tab` | Cycle category tabs |
| `Space` / `Enter` | Toggle selection |
| `Esc` | Clear selection / exit tab |
| `z` | Reset to default view |
| `q` | Quit |

## Install

```bash
git clone https://github.com/<you>/qbitui
cd qbitui
# Optional: symlink into PATH
ln -s "$PWD/bin/qbit-dashboard.py" ~/.local/bin/qbitui
```

## Shared Cache Mode

qbitui now uses `hashall` as the canonical shared qB cache implementation.
The local `bin/qbit-cache-agent.py` and `bin/qbit-cache-daemon.py` scripts are
thin wrappers so qbitui can share the same qB compatibility and cache contract
without maintaining a second daemon/client implementation.

### How it works

- `hashall` owns the actual daemon/client logic and qB version normalization.
- qbitui calls the local wrapper scripts, which delegate to hashall's cache
  agent and daemon.
- The shared cache lives at `~/.cache/hashall-qb/` by default.
- The wrappers expect a local `hashall` checkout at
  `/home/michael/dev/work/hashall`, or a `HASHALL_ROOT` override.
- The dashboard calls the agent subprocess instead of hitting the API directly
  when `--use-shared-cache` is enabled.
- If cache reads fail in shared-cache mode, the dashboard keeps its current
  snapshot and shows an explicit cache-health banner instead of silently
  falling back to hot direct polling.

### Example commands

```bash
# Enable shared cache mode (daemon auto-started if needed)
./bin/qbit-dashboard.py --use-shared-cache

# Tune freshness window
./bin/qbit-dashboard.py --use-shared-cache --cache-max-age 10 --cache-wait-fresh 3

# Disallow stale fallback (hard-fail if cache is too old)
./bin/qbit-dashboard.py --use-shared-cache --no-cache-allow-stale

# Point to a custom agent location
./bin/qbit-dashboard.py --use-shared-cache --cache-agent-cmd /usr/local/bin/qbit-cache-agent.py
```

### Troubleshooting with --cache-status

```bash
./bin/qbit-dashboard.py --use-shared-cache --cache-status
```

Prints a JSON object with daemon PID, running state, cache age, active leases,
and last fetch metadata. Useful for diagnosing stale cache or daemon startup issues.

### Migration notes

**Cross-repo cache alignment** (v1.13.0): qbitui no longer maintains its own qB
cache implementation. The local cache scripts now delegate to hashall's shared
cache tooling so qB version handling and cache behavior live in one place.
The default cache directory is `~/.cache/hashall-qb/`.

## Status

Actively used. Arrow key support intentionally removed (tmux ESC sequence conflicts). A non-blocking input loop and curses-style buffered screen are planned improvements.
