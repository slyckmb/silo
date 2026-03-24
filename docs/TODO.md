# silo Project Roadmap: TODOs and Proposals

This document tracks the immediate requirements and future architectural directions for the `silo` downloader dashboard suite.

## Mandatory Tasks (Must-Do)
These items are required to resolve existing bugs, restore full functionality, or complete the recent refactor.

1.  **Verify Connection Recovery:** Confirm that `silo-dashboard.py` successfully connects and performs actions (e.g., Pause) once the user restarts qBittorrent (clearing the active IP ban).
2.  **Fix SABnzbd Config Loading:** Correct the indentation bug in `bin/sabnzbd-dashboard.py:read_api_url_from_config` where the `try/except` block may fail to return the intended API URL.
3.  **Cache Header Alignment:** The `SILO_MI_V2` header in `silo-dashboard.py` has diverged from the `hashall` agent's expected `QBITUI_MI_V2`. This must be aligned (or the agent updated) to restore MediaInfo columns in the TUI.
4.  **Bypass Logic for Cache Scripts:** Implement "Optimistic Auth Bypass" in `silo-cache-agent.py` and `silo-cache-daemon.py` (which wrap `hashall` scripts) to prevent background tasks from triggering new IP bans.
5.  **Naming Consistency:** Rename `bin/sabnzbd-dashboard.py` to `bin/silo-sabnzbd.py` to match the project's new naming convention.

---

## Proposals (Future Enhancements)
These items are strategic improvements to expand the scope and usability of the project.

1.  **Unified `silo` Entry Point:** Update the root `silo` shim to act as a proper CLI dispatcher (e.g., `silo qbit`, `silo sab`, `silo rtorrent`) rather than just a pointer to the qBittorrent view.
2.  **rTorrent Integration:** Implement a dashboard view for rTorrent to fulfill the project's stated goal of being a multi-downloader "silo."
3.  **Consolidated Configuration:** Move toward a single `config/silo.yml` that handles connection details and presets for all supported downloaders.
4.  **Advanced SABnzbd Error Handling:** Port the "ban detection" and connection-reset logic from the qBittorrent dashboard to the SABnzbd view for increased robustness.
