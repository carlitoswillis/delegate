"""The refreshing run store — keeps the TUI's run list up to date.

The TUI used to load runs once at launch and never again, which meant new
delegations were invisible and live/frozen status was frozen too.  This
module owns a background thread that re-reads the ledger and subagent list
on a short interval and merges the results into a single list the TUI can
cheaply snapshot every frame.

Merging is the key design choice.  The TUI holds a reference to the selected
Run and keys its conversation cache off it.  Replacing the entire list every
2 seconds would invalidate that cache and re-read every open transcript from
disk — turning a liveness feature into a performance bug.  Instead we
identify each run by a stable key and update the existing object in place.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from delegate_view.runs import LIVE_WINDOW_S, Run


def key_of(run: Run) -> tuple:
    """Stable identity for a run across refreshes.

    When a run has a resolved session we key on (platform, session_id); when
    it does not (ledger-only, not yet resolved) we fall back to
    (transcript, started).  Either way the key is hashable and unique enough
    for the merge dictionary.
    """
    if run.platform and run.session_id:
        return (run.platform, run.session_id)
    return (run.transcript, run.started)


class RunStore:
    """A thread-safe, periodically refreshing list of runs.

    The store is the single source of truth for the TUI's run list.  Call
    ``start()`` to begin background refresh and ``snapshot()`` each frame to
    get the current list without blocking.

    ``stop()`` signals the background thread to exit and returns promptly.
    It is idempotent and safe to call before ``start()``.
    """

    def __init__(
        self,
        ledger_path: str | None = None,
        subagent_limit: int = 25,
        include_subagents: bool = True,
        interval: float = 2.0,
        *,
        _load_runs: Callable | None = None,
        _load_subagent_runs: Callable | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._subagent_limit = subagent_limit
        self._include_subagents = include_subagents
        self._interval = interval

        # Pluggable loaders so tests never touch ~/.delegate or ~/.claude.
        # When not overridden, import lazily to avoid import-time side effects
        # (the real loaders stat ~ paths that may not exist).
        self._load_runs_fn = _load_runs
        self._load_subagent_runs_fn = _load_subagent_runs

        self._runs: list[Run] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: str = ""
        self._started = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Begin background refreshing.  Returns immediately.

        Snapshot() will return [] until the first refresh cycle completes,
        so the TUI opens instantly — no blocking on the first paint.
        """
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="run-store-refresh"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and return promptly.

        Must be idempotent: safe to call twice, safe to call without start().
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 0.5)
            self._thread = None

    def snapshot(self) -> list[Run]:
        """Current runs, newest first.  Cheap; safe to call every frame."""
        with self._lock:
            return list(self._runs)

    def status(self) -> str:
        """Short human string for the header while loading, else \"\"."""
        return self._status

    def index_of(self, key: tuple) -> int | None:
        """Return the index of a run by its key, or None if not found."""
        with self._lock:
            for i, r in enumerate(self._runs):
                if key_of(r) == key:
                    return i
            return None

    # -- internal ----------------------------------------------------------

    def _refresh(self) -> None:
        """One full refresh cycle: re-read sources and merge.

        Errors in one source must not prevent the other from appearing.
        A cycle that raises must not kill the thread — the next cycle should
        still run.
        """
        new_runs: list[Run] = []
        new_runs.extend(self._safe_load_ledger())
        if self._include_subagents:
            new_runs.extend(self._safe_load_subagents())

        # Build a lookup of incoming runs by key.
        incoming: dict[tuple, Run] = {}
        for r in new_runs:
            incoming[key_of(r)] = r

        with self._lock:
            existing = {key_of(r): r for r in self._runs}
            merged_keys: list[tuple] = []
            merged: list[Run] = []

            # Walk the union of keys, preserving the order of existing runs
            # first, then appending new ones.  This keeps the list stable
            # across refreshes.
            seen: set[tuple] = set()
            for r in self._runs:
                k = key_of(r)
                if k in incoming:
                    # Update the existing object in place so the TUI's
                    # reference stays valid and the conversation cache
                    # is not invalidated.
                    src = incoming[k]
                    r.live = src.live
                    r.size = src.size
                    r.tokens_in = src.tokens_in
                    r.tokens_out = src.tokens_out
                    r.cost = src.cost
                    r.prompt_text = src.prompt_text or r.prompt_text
                    r.model = src.model or r.model
                    r.cwd = src.cwd or r.cwd
                    r.transcript = src.transcript or r.transcript
                    r.session_id = src.session_id or r.session_id
                    r.platform = src.platform or r.platform
                    # Recompute liveness from the current mtime of the
                    # transcript file, not from whatever the source said.
                    r.live = _is_live(r)
                merged.append(r)
                merged_keys.append(k)
                seen.add(k)

            # Append runs that are genuinely new.
            for k, r in incoming.items():
                if k not in seen:
                    r.live = _is_live(r)
                    merged.append(r)

            # Runs that vanish from the source are kept, not dropped.

            # Sort newest started first.  Sort is stable, so ties preserve
            # insertion order, which is the merge order above.
            merged.sort(key=lambda r: r.started, reverse=True)

            self._runs = merged

    def _safe_load_ledger(self) -> list[Run]:
        """Load from the ledger, returning [] on any error."""
        try:
            fn = self._load_runs_fn
            if fn is None:
                from delegate_view.runs import load_runs
                fn = load_runs
            return fn(ledger_path=self._ledger_path)
        except Exception:
            return []

    def _safe_load_subagents(self) -> list[Run]:
        """Load subagent runs, returning [] on any error."""
        try:
            fn = self._load_subagent_runs_fn
            if fn is None:
                from delegate_view.subagents import load_subagent_runs
                fn = load_subagent_runs
            return fn(limit=self._subagent_limit)
        except Exception:
            return []

    def _run_loop(self) -> None:
        """Background loop that refreshes on an interval."""
        # Do one immediate refresh so the list is populated quickly.
        self._refresh()
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._interval)
            if self._stop_event.is_set():
                break
            self._refresh()
        self._status = ""


def _is_live(run: Run) -> bool:
    """Recompute liveness from the current mtime of run.transcript.

    A run with no transcript path is never live — the ledger's mtime is
    meaningless because the ledger is rewritten by every new delegation.
    """
    transcript = run.transcript
    if not transcript:
        return False
    try:
        stat = os.stat(transcript)
        return (time.time() - stat.st_mtime) < LIVE_WINDOW_S
    except OSError:
        return False
