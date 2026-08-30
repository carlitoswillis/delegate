"""The refreshing run store — keeps the TUI's run list up to date.

The TUI used to load runs once at launch and never again, which meant new
delegations were invisible and live/frozen status was frozen too.  This
module owns a background thread that re-reads the sources on a short interval
and merges the results into a single list the TUI can cheaply snapshot every
frame.

Merging is the key design choice.  The TUI holds a reference to the selected
Run and keys its conversation cache off it.  Replacing the entire list every
2 seconds would invalidate that cache and re-read every open transcript from
disk — turning a liveness feature into a performance bug.  Instead we
identify each run by a stable key and update the existing object in place.

What gets loaded is no longer this module's business.  sessions.all_runs()
owns the source list and the de-duplication; the store owns refreshing,
merging in place, and never letting either kill the thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from delegate_view.runs import LIVE_WINDOW_S, Run, is_live, key_of  # noqa: F401

# key_of and LIVE_WINDOW_S are re-exported: they moved to runs.py, next to the
# Run they describe, but this is where the rest of the code already imports
# them from and the identity rule is the same one either way.


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
        _all_runs: Callable | None = None,
        _load_runs: Callable | None = None,
        _load_subagent_runs: Callable | None = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._subagent_limit = subagent_limit
        self._include_subagents = include_subagents
        self._interval = interval

        # Pluggable loaders so tests never touch ~/.delegate, ~/.claude or the
        # opencode database.  Production passes none of these and gets
        # sessions.all_runs(); the legacy pair is still honoured because a
        # loader is the seam every test in test_store.py is written against,
        # and each one is loaded inside its own try/except either way.
        self._all_runs_fn = _all_runs
        self._load_runs_fn = _load_runs
        self._load_subagent_runs_fn = _load_subagent_runs

        self._runs: list[Run] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Shown in the header until the first refresh lands. Without it an
        # empty list is ambiguous — "nothing delegated yet" and "still reading
        # four thousand transcripts" render identically, and the honest answer
        # for the first two seconds is the second one.
        self._status: str = "loading…"
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

    def _sources(self) -> list[Callable[[], list[Run]]]:
        """The zero-argument loaders this refresh should call.

        Production is one source — sessions.all_runs() already unifies and
        de-duplicates across the ledger, opencode and Claude Code.  Splitting
        it back into two calls here would just re-introduce the double-listing
        it exists to prevent.
        """
        if self._load_runs_fn is not None or self._load_subagent_runs_fn is not None:
            out: list[Callable[[], list[Run]]] = []
            if self._load_runs_fn is not None:
                out.append(lambda: self._load_runs_fn(ledger_path=self._ledger_path))
            if self._load_subagent_runs_fn is not None:
                out.append(lambda: self._load_subagent_runs_fn(
                    limit=self._subagent_limit))
            return out

        fn = self._all_runs_fn
        if fn is None:
            from delegate_view.sessions import all_runs
            fn = all_runs

        # The Claude Code corpus is the only source big enough to need a cap,
        # and --subagent-limit is the knob that already exists for it.  The
        # opencode store is left uncapped: capping it is the bug this work
        # was for.
        return [lambda: fn(
            ledger_path=self._ledger_path,
            limit_per_source={"claude-code": self._subagent_limit},
        )]

    def _load(self) -> list[Run]:
        """Every incoming run for this cycle.

        Each source is isolated: one raising must not hide the others, which
        is the whole reason this is a loop over callables and not one call.
        """
        out: list[Run] = []
        for source in self._sources():
            try:
                out.extend(source())
            except Exception:
                continue
        return out

    def _refresh(self) -> None:
        """One full refresh cycle: re-read sources and merge.

        Errors in one source must not prevent the other from appearing.
        A cycle that raises must not kill the thread — the next cycle should
        still run.
        """
        new_runs = self._load()

        # `include_subagents=False` hides agent-to-agent conversations, not a
        # whole platform.  It used to mean "do not load Claude Code at all",
        # which as a side effect also hid every direct chat there — the same
        # coverage gap this store was fixed for, one flag down.
        if not self._include_subagents:
            new_runs = [r for r in new_runs if not r.is_subagent]

        # Build a lookup of incoming runs by key.
        incoming: dict[tuple, Run] = {}
        for r in new_runs:
            incoming[key_of(r)] = r

        with self._lock:
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
                    _update_in_place(r, incoming[k])
                merged.append(r)
                seen.add(k)

            # Append runs that are genuinely new.
            for k, r in incoming.items():
                if k not in seen:
                    r.live = is_live(r)
                    merged.append(r)

            # Runs that vanish from the source are kept, not dropped.

            # Sort newest started first.  Sort is stable, so ties preserve
            # insertion order, which is the merge order above.
            merged.sort(key=lambda r: r.started, reverse=True)

            self._runs = merged

    def _run_loop(self) -> None:
        """Background loop that refreshes on an interval."""
        # Do one immediate refresh so the list is populated quickly.
        try:
            self._refresh()
        except Exception:
            pass
        finally:
            # Cleared even if the refresh blew up: a header stuck on
            # "loading…" over a list that will never load is a worse lie
            # than an empty list.
            self._status = ""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._interval)
            if self._stop_event.is_set():
                break
            try:
                self._refresh()
            except Exception:
                # A cycle that raises must not kill the thread; the next
                # cycle should still run. The sources are individually
                # guarded, but the merge is not, and a dead refresh thread
                # is a list that silently stops updating forever.
                continue


def _update_in_place(run: Run, src: Run) -> None:
    """Copy this cycle's facts onto the Run object the TUI already holds.

    Field by field rather than by replacing the object, because the TUI keys
    its conversation cache on the object it is holding; swapping it would
    re-read every open transcript from disk on every refresh.

    Values that can legitimately go back to zero — stats, flags, the reason a
    run ended — are copied straight across.  Values that are only ever
    discovered, like a session id or a prompt, are never overwritten with
    nothing: a source that momentarily fails to resolve must not erase what
    an earlier cycle already learned.
    """
    run.size = src.size
    run.tokens_in = src.tokens_in
    run.tokens_out = src.tokens_out
    run.cost = src.cost
    run.failed = src.failed
    run.end_reason = src.end_reason
    run.is_subagent = src.is_subagent
    run.updated = max(run.updated, src.updated)
    run.prompt_text = src.prompt_text or run.prompt_text
    run.model = src.model or run.model
    run.cwd = src.cwd or run.cwd
    run.transcript = src.transcript or run.transcript
    run.session_id = src.session_id or run.session_id
    run.platform = src.platform or run.platform
    run.parent_id = src.parent_id or run.parent_id
    run.source = src.source or run.source
    # Recompute liveness from the current state of the transcript, not from
    # whatever the source said when it built its list.
    run.live = is_live(run)


def _is_live(run: Run) -> bool:
    """Deprecated alias for runs.is_live, kept for callers that imported it."""
    return is_live(run)
