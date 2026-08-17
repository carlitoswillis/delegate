"""Adapter registry.

Every adapter module exposes the same three functions:

    default_db_path() -> Path
    list_sessions(db_path=None, cwd=None) -> list[Session]   # events NOT loaded
    load_session(session_id, db_path=None) -> Session        # events loaded

The UI must go through this module and never import a platform adapter
directly. Adding gemini or codex later is then a one-line change here, and
nothing in the UI has to learn a new name.

Adapters are expected to be missing or broken on any given machine — not
everyone has all four CLIs installed, and a store can be absent, empty, or a
version we have not seen. A platform that raises must not take the whole
viewer down with it, so discovery below degrades to skipping that platform.
"""

from __future__ import annotations

import importlib

from delegate_view.schema import Session

# platform name -> module under delegate_view.adapters
REGISTRY = {
    "opencode": "delegate_view.adapters.opencode",
    "claude-code": "delegate_view.adapters.claude_code",
}


def available() -> list[str]:
    """Platforms whose adapter imports AND whose store actually exists."""
    out = []
    for name, mod_path in REGISTRY.items():
        try:
            mod = importlib.import_module(mod_path)
            if mod.default_db_path().exists():
                out.append(name)
        except Exception:
            continue
    return out


def _load(name: str):
    return importlib.import_module(REGISTRY[name])


def list_sessions(platform: str | None = None,
                  cwd: str | None = None) -> list[Session]:
    """Sessions across every available platform, newest-updated first.

    A platform that blows up is skipped rather than fatal: one unreadable
    store should not hide the other three.
    """
    names = [platform] if platform else available()
    out: list[Session] = []
    for name in names:
        try:
            out.extend(_load(name).list_sessions(cwd=cwd))
        except Exception:
            continue
    out.sort(key=lambda s: s.updated, reverse=True)
    return out


def load_session(platform: str, session_id: str) -> Session:
    """Full session, events included. Raises KeyError if unknown."""
    if platform not in REGISTRY:
        raise KeyError(platform)
    return _load(platform).load_session(session_id)
