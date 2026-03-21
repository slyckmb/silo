# Migration: qbitui to silo

This repository has been refactored from **qbitui** to **silo**.

## What Changed
- The main application entry point is now `silo-dashboard.py`.
- Shared utilities are now under `silo_hashall_shared.py`.
- All internal references have been updated.

## Backward Compatibility
To prevent breaking external dependencies (bash aliases, cron jobs, other repositories), **shims** (symbolic links) have been created at the original file locations:
- `bin/qbit-dashboard.py` -> `bin/silo-dashboard.py`
- `bin/qbit-cache-agent.py` -> `bin/silo-cache-agent.py`
- `bin/qbit-cache-daemon.py` -> `bin/silo-cache-daemon.py`

## Required Action
All external agents and scripts should update their configurations to use the new `silo-*` naming convention.

## Sunset Warning
These shims are **DEPRECATED** and will be removed in a future release. Please update your references to the canonical `silo-*` paths as soon as possible.
