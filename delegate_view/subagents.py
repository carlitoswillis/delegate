"""Claude Code subagent conversations surfaced as Run objects.

Claude Code marks spawned subagents with a parent_id pointing at the caller.
A session with a truthy parent_id IS a subagent conversation. This module
turns those into the same Run shape the TUI already renders.
"""

from __future__ import annotations

import time

from delegate_view.adapters import list_sessions
from delegate_view.runs import Run, LIVE_WINDOW_S


def load_subagent_runs(limit: int = 50, since_ms: int | None = None) -> list[Run]:
    """Recent Claude Code subagent conversations, as Run objects, newest first."""
    try:
        sessions = list_sessions(platform="claude-code")
    except Exception:
        return []

    now_ms = int(time.time() * 1000)
    out: list[Run] = []
    for s in sessions:
        if not s.parent_id:
            continue
        if since_ms is not None and s.updated < since_ms:
            continue
        started = s.created if s.created else s.updated
        live = (now_ms - s.updated) < (LIVE_WINDOW_S * 1000)
        out.append(Run(
            started=started,
            prompt="",
            transcript="",
            model=s.model,
            cwd=s.cwd,
            continued=False,
            live=live,
            size=0,
            prompt_text=s.title,
            session_id=s.id,
            platform="claude-code",
        ))

    out.sort(key=lambda r: r.started, reverse=True)
    if limit:
        out = out[:limit]
    return out
