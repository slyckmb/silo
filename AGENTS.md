# AGENTS — silo

**Global defaults**: `~/.agent/AGENTS_GLOBAL.md`

## Quick Start

1. Read BASELINE_PATH (per-chat baseline in `.agent/baselines/`) first if present.
2. Read this file (repo-specific rules).
3. Read `~/.agent/AGENTS_GLOBAL.md` for cross-repo defaults.

## Baseline Protocol

- Read BASELINE_PATH (per-chat baseline in `.agent/baselines/<chat_id>-baseline.md`) and treat its key=value facts as authoritative.
- Do **not** ask the user "were things dirty before?" — use the baseline.
- Baseline reflects repo state at session start; re-check `git status` before committing.
- Do not commit, delete files, or modify `.gitignore` without explicit approval.

## Global Defaults

See `~/.agent/AGENTS_GLOBAL.md` for cross-repo defaults (environment detection,
path conventions, branching, venv usage, safety rules).
