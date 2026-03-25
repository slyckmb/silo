# Silo Dashboard Handoff

## Focus
Continue improving `bin/silo-dashboard.py` (TUI). Current focus: stable, low-latency input handling (no arrow keys), refined keymap, selection UX, and MediaInfo caching/background refresh.

## Current State Summary
- File touched: `bin/silo-dashboard.py` only.
- Version bumped to `1.6.2`.
- Navigation keys now:
  - `,` page prev
  - `.` page next
  - `'` caret up
  - `/` caret down
  - `t` toggle tags
  - `l` filter line prompt
- Arrow keys are intended to be ignored (ESC sequences are swallowed in `get_key()`), but earlier behavior was inconsistent due to tmux splitting.
- Selection:
  - `Space` and `Enter` toggle selection (if focused item already selected, deselects).
  - `Esc` clears selection (or exits tab view).
- Tabs:
  - `Tab` cycles through tabs and turns tab view off after the last tab (cycle off).
  - `Ctrl-Tab` cycles to next available tab.
  - `T` also cycles available tabs (kept for now).
- New `z` hotkey resets to default startup view (page 1, sort newest added, clear filters, etc.).
- Selection highlight: bright blue background + white text (no reverse video).
- MediaInfo caching:
  - Cache-first reads via `get_mediainfo_summary_cached()`.
  - Background updater bootstraps page 1, page 2, and page n-1 on startup, then cycles through sorted hashes to fill cache.
- Minimal partial redraw:
  - List view uses cursor positioning to redraw only summary/banner/scope and list block.
  - Tab view still full redraw.

## Unresolved Issues
- Input lag persists sometimes; full redraw still occurs on tab view or certain state changes.
- Arrow key handling remains inconsistent due to tmux sending split ESC bytes; arrow support was removed intentionally.
- Paging/caret responsiveness can still feel sluggish in some conditions.

## Potential Improvements
- Move to non-blocking input loop that drains multiple queued keys per frame.
- Full switch to a buffered screen (curses-style) for reliable partial redraw and resize handling.
- Add a visible “last key” debug indicator (temporary) to validate key decoding without capture logs.
- Make MediaInfo background updates time-sliced and pausable when not on list view.

## Next Steps
1. Refactor `get_key` into a non-blocking `get_input_events` that drains the buffer and handles split escape sequences robustly.
2. Implement a non-blocking main loop using `select` to handle idle updates and user input.
3. Replace synchronous `mediainfo` calls in the render path with a "Loading..." placeholder and a background queue/tick processor.
4. Add terminal resize detection (SIGWINCH or polling) to trigger full redraws.
5. Verify keymap safety (arrows ignored) and UI responsiveness.

