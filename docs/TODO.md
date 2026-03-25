# silo Project Roadmap: TODOs and Proposals

This document tracks the immediate requirements and future architectural directions for the `silo` downloader dashboard suite.

## Mandatory Tasks (Must-Do)
These items are required to resolve existing bugs, restore full functionality, or complete the recent refactor.

- ✅ **Bypass Logic for Cache Scripts:** Implement "Optimistic Auth Bypass" in `bin/silo-cache-agent.py` and `bin/silo-cache-daemon.py` (which wrap `hashall` scripts) to prevent background tasks from triggering new IP bans.
1.  **Verify Connection Recovery:** Confirm that `silo-dashboard.py` successfully connects and performs actions (e.g., Pause) now that qBittorrent has been restarted.

## Proposals (Future Enhancements)
These items are strategic improvements to expand the scope and usability of the project.

1.  **Unified `silo` Entry Point:** Update the root `silo` shim to act as a proper CLI dispatcher (e.g., `silo qbit`, `silo sab`, `silo rtorrent`) rather than just a pointer to the qBittorrent view.
2.  **rTorrent Integration:** Implement a dashboard view for rTorrent to fulfill the project's stated goal of being a multi-downloader "silo."
3.  **Consolidated Configuration:** Move toward a single `config/silo.yml` that handles connection details and presets for all supported downloaders.
4.  **Advanced SABnzbd Error Handling:** Port the "ban detection" and connection-reset logic from the qBittorrent dashboard to the SABnzbd view for increased robustness.

---

## Completed Tasks
- ✅ **Refactor to silo:** Renamed all primary entry points and shared utilities (`qbit-*` → `silo-*`).
- ✅ **Backward Compatibility:** Created symlink-based shims for legacy `qbit-*` paths.
- ✅ **Fix SABnzbd Config Loading:** Corrected the bug in `bin/silo-sabnzbd.py:read_api_url_from_config`.
- ✅ **Cache Header Alignment:** Dashboard now supports both `SILO_MI_V2` and legacy `QBITUI_MI_V2` headers.
- ✅ **Naming Consistency:** Renamed `sabnzbd-dashboard.py` to `silo-sabnzbd.py`.
- ✅ **Migration Documentation:** Created `docs/migration/REFACTOR-TO-SILO.md`.
