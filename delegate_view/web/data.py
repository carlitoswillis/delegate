"""Runs and conversations for the web UI, cached hard enough to poll against.

Two jobs.

The first is getting the unified run list.  `delegate_view.sessions.all_runs`
is the intended source, but this package must not be dead in the water when
that module is absent or changes shape underneath it, so the import is
defensive and there is a full fallback built from the adapters directly.  The
fallback is not a stub: it produces the same union (ledger runs + every
opencode session + recent Claude Code sessions), because a viewer that
silently shows a third of your conversations is worse than one that fails.

The second is caching.  Every page hit and every poll would otherwise re-scan
~4200 Claude Code transcripts and re-parse a multi-megabyte JSONL.  A phone
polling a live conversation every few seconds turns that into a permanent
background load on the machine, so both layers are cached: the run list on a
short clock, and a parsed conversation on the *fingerprint* of the file
behind it, so a session that has not been touched is never parsed twice.

Identity deserves a note.  A conversation is addressed by an opaque key, and
the key is looked up in the run list rather than decoded into a filesystem
path.  That is the single design decision that makes path traversal
structurally impossible here: no request-supplied string is ever joined onto
a directory.  A key that is not in the list is a 404, full stop.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field

from delegate_view import adapters, views
from delegate_view.runs import LIVE_WINDOW_S, Run, load_runs, tail
from delegate_view.web.blocks import Block, blocks_for_session, blocks_for_raw

# How long a run list is reused before it is rebuilt.  Matches the TUI's
# refresh interval: long enough that a phone polling the list costs nothing,
# short enough that a run started at the desk shows up by the time you look.
RUN_TTL_S = 2.0

# Claude Code has thousands of transcripts and peeking all of them costs
# seconds.  This bounds the peek, not the display, exactly as the TUI does.
DEFAULT_LIMIT = 60

# Conversations are capped by default: a long Claude Code session is tens of
# thousands of events, and rendering all of them produces a page a phone
# takes seconds to lay out and cannot scroll usefully.  The tail is the part
# you want anyway — you are checking on a run, not auditing it.
DEFAULT_EVENT_CAP = 400

# Keys are matched against the run list, never against the filesystem, but
# the pattern still rejects obvious junk early so a malformed URL never
# reaches the lookup path at all.
KEY_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,200}$")


def run_key(run: Run) -> str:
    """Stable, opaque, URL-safe identity for a run.

    A resolved session keys on (platform, id), which survives the run list
    being rebuilt, re-sorted, or merged differently.  An unresolved ledger run
    has no session yet, so it keys on a hash of its transcript path and start
    time — hashed rather than embedded because the path is a real filesystem
    path and putting it in a URL invites exactly the confusion between "id"
    and "path" this module exists to prevent.
    """
    platform = getattr(run, "platform", "") or ""
    sid = getattr(run, "session_id", "") or ""
    if platform and sid:
        return f"{platform}:{sid}"
    seed = f"{getattr(run, 'transcript', '')}|{getattr(run, 'started', 0)}"
    return "t:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def valid_key(key: str) -> bool:
    return bool(key) and bool(KEY_RE.match(key))


# ── the run list ────────────────────────────────────────────────────────

def _session_to_run(session, now_ms: int) -> Run:
    """A Session from an adapter, in the Run shape the list renders."""
    started = session.created or session.updated
    return Run(
        started=started,
        prompt="",  # not a delegate.sh run, so there is no task file
        transcript=session.path,
        model=session.model,
        cwd=session.cwd,
        live=(now_ms - session.updated) < (LIVE_WINDOW_S * 1000),
        size=0,
        prompt_text=session.title,
        session_id=session.id,
        platform=session.platform,
        tokens_in=session.tokens_in,
        tokens_out=session.tokens_out,
        cost=session.cost,
    )


def _fallback_runs(ledger_path=None, limit_per_source=None) -> list[Run]:
    """The union of every source, built without delegate_view.sessions.

    Claude Code is listed through its adapter module directly rather than
    through `adapters.list_sessions`, because the registry entry point takes
    no limit and an unbounded call peeks all ~4200 transcripts — several
    seconds per page load.  `subagents.py` reaches for the same module for
    the same reason, so this is the established shape rather than a new hole
    in the abstraction.
    """
    limit = DEFAULT_LIMIT if limit_per_source is None else limit_per_source
    now_ms = int(time.time() * 1000)

    out: list[Run] = []
    seen: set[tuple[str, str]] = set()

    def add(run: Run) -> None:
        platform = run.platform or ""
        sid = run.session_id or ""
        if platform and sid:
            if (platform, sid) in seen:
                return
            seen.add((platform, sid))
        out.append(run)

    # Ledger runs go in first so that when a delegation and its session are
    # the same conversation, the entry that survives is the one that knows
    # which task file produced it.
    try:
        for run in load_runs(ledger_path=ledger_path):
            add(run)
    except Exception:
        pass

    try:
        for session in adapters.list_sessions(platform="opencode"):
            add(_session_to_run(session, now_ms))
    except Exception:
        pass

    try:
        from delegate_view.adapters import claude_code

        for session in claude_code.list_sessions(limit=limit):
            add(_session_to_run(session, now_ms))
    except Exception:
        pass

    out.sort(key=lambda r: r.started, reverse=True)
    return out


def fetch_runs(ledger_path=None, limit_per_source=None) -> list[Run]:
    """The unified run list, from delegate_view.sessions when it is there.

    That module is being written alongside this one, so every failure mode of
    it — missing, missing the function, a different signature, raising — has
    to land somewhere that still shows the user their transcripts.
    """
    try:
        from delegate_view.sessions import all_runs
    except Exception:
        return _fallback_runs(ledger_path, limit_per_source)

    try:
        return list(all_runs(ledger_path=ledger_path,
                             limit_per_source=limit_per_source))
    except TypeError:
        # Signature drift while the contract settles: try the bare call
        # before giving up on the real source.
        try:
            return list(all_runs())
        except Exception:
            return _fallback_runs(ledger_path, limit_per_source)
    except Exception:
        return _fallback_runs(ledger_path, limit_per_source)


class RunIndex:
    """The run list, refreshed on a clock and addressable by key.

    Cached rather than loaded per request because the list page, the list
    fragment poll, and every conversation lookup all need it, and rebuilding
    it three times for one page view is the difference between a UI that
    feels instant on a phone and one that does not.
    """

    def __init__(self, ledger_path=None, limit_per_source=None,
                 ttl: float = RUN_TTL_S, *, fetch=None) -> None:
        self._ledger_path = ledger_path
        self._limit = limit_per_source
        self._ttl = ttl
        self._fetch = fetch or fetch_runs
        self._lock = threading.Lock()
        self._runs: list[Run] = []
        self._at: float = 0.0

    def runs(self, *, force: bool = False) -> list[Run]:
        with self._lock:
            fresh = (time.time() - self._at) < self._ttl
            if self._runs and fresh and not force:
                return list(self._runs)
        # Fetch outside the lock: it touches sqlite and thousands of files,
        # and holding the lock across it would serialize every request behind
        # the slowest one.
        runs = self._fetch(self._ledger_path, self._limit)
        with self._lock:
            self._runs = runs
            self._at = time.time()
            return list(runs)

    def find(self, key: str) -> Run | None:
        """The run with this key, or None.  Never touches the filesystem.

        A miss retries once against a forced refresh, because the honest
        reason a key is missing is usually that the cached list predates the
        conversation someone just opened from another device.
        """
        if not valid_key(key):
            return None
        for run in self.runs():
            if run_key(run) == key:
                return run
        for run in self.runs(force=True):
            if run_key(run) == key:
                return run
        return None


# ── one conversation ────────────────────────────────────────────────────

@dataclass
class Conversation:
    key: str
    title: str
    run: Run
    blocks: list[Block] = field(default_factory=list)
    total_events: int = 0
    shown_events: int = 0
    live: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    model: str = ""
    platform: str = ""
    cwd: str = ""
    updated: int = 0
    failed: bool = False
    end_reason: str = ""
    error: str = ""

    @property
    def truncated(self) -> bool:
        return self.shown_events < self.total_events


def fingerprint(run: Run) -> str:
    """A cheap value that changes exactly when the conversation changed.

    File-backed sessions (Claude Code, and delegate.sh transcripts) get mtime
    and size, which is the same signal the TUI uses for liveness and costs one
    stat.  opencode keeps sessions in a shared SQLite database where a file
    mtime tells you only that *something* changed, so those fall back to the
    per-session counters the listing already carries.
    """
    path = getattr(run, "transcript", "") or ""
    if path:
        try:
            st = os.stat(path)
            return f"f:{int(st.st_mtime_ns)}:{st.st_size}"
        except OSError:
            pass
    return (f"s:{getattr(run, 'updated', 0)}:{getattr(run, 'started', 0)}"
            f":{getattr(run, 'tokens_out', 0)}:{getattr(run, 'cost', 0.0)}"
            f":{getattr(run, 'size', 0)}")


def build_conversation(run: Run, key: str, *, cap: int | None = DEFAULT_EVENT_CAP
                       ) -> Conversation:
    """Load and shape one conversation for display.

    Mirrors the TUI's `_load_body`: a resolved session goes through the
    adapter, and anything unresolved falls back to the tail of the raw
    transcript file.  That fallback matters more here than it looks — a run
    delegated seconds ago has no session to resolve yet, and it is precisely
    the run you picked up your phone to check on.
    """
    title = views.run_title(run)[0] or "conversation"
    platform = getattr(run, "platform", "") or ""
    sid = getattr(run, "session_id", "") or ""
    # `is_subagent` is authoritative when the run list provides it. The old
    # inference — no prompt file means an agent spawned it — mislabels a chat
    # you started yourself in opencode, which also has no prompt file. Keep the
    # fallback only for run objects that predate the field.
    is_subagent = getattr(run, "is_subagent", None)
    if is_subagent is None:
        is_subagent = not getattr(run, "prompt", "")

    conv = Conversation(
        key=key,
        title=title,
        run=run,
        live=bool(getattr(run, "live", False)),
        tokens_in=getattr(run, "tokens_in", 0),
        tokens_out=getattr(run, "tokens_out", 0),
        cost=getattr(run, "cost", 0.0),
        model=getattr(run, "model", "") or "",
        platform=platform,
        cwd=getattr(run, "cwd", "") or "",
        updated=getattr(run, "updated", 0) or getattr(run, "started", 0),
        failed=bool(getattr(run, "failed", False)),
        end_reason=getattr(run, "end_reason", "") or "",
    )

    if platform and sid:
        try:
            session = adapters.load_session(platform, sid)
        except Exception as exc:  # unreadable store, mid-write JSONL, bad id
            conv.error = f"could not load this conversation ({type(exc).__name__})"
            return conv
        events = list(getattr(session, "events", []))
        conv.total_events = len(events)
        if cap is not None and len(events) > cap:
            events = events[-cap:]
        conv.shown_events = len(events)
        conv.model = conv.model or session.model
        conv.tokens_in = session.tokens_in or conv.tokens_in
        conv.tokens_out = session.tokens_out or conv.tokens_out
        conv.cost = session.cost or conv.cost
        conv.cwd = conv.cwd or session.cwd
        conv.updated = session.updated or conv.updated
        conv.blocks = blocks_for_session(
            events, model=conv.model, platform=platform,
            is_subagent=is_subagent,
        )
        return conv

    raw = tail(getattr(run, "transcript", ""), 500)
    conv.total_events = len(raw)
    conv.shown_events = len(raw)
    conv.blocks = blocks_for_raw(raw)
    return conv


class ConversationCache:
    """Parsed conversations, keyed by run key and invalidated by fingerprint.

    A phone polling a live conversation asks for it every few seconds.  Without
    this, each poll re-parses the whole JSONL — and a Claude Code session
    routinely runs to megabytes.  With it, a poll against an unchanged file
    costs one stat.

    Bounded to a handful of entries because a parsed session holds every event
    in memory and this process is also running someone's TUI.
    """

    def __init__(self, size: int = 6, *, build=None) -> None:
        self._size = size
        self._build = build or build_conversation
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, int | None, Conversation]] = {}
        self._order: list[str] = []

    def get(self, run: Run, key: str, *, cap: int | None = DEFAULT_EVENT_CAP
            ) -> Conversation:
        fp = fingerprint(run)
        with self._lock:
            hit = self._entries.get(key)
            if hit and hit[0] == fp and hit[1] == cap:
                return hit[2]

        conv = self._build(run, key, cap=cap)

        with self._lock:
            self._entries[key] = (fp, cap, conv)
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            while len(self._order) > self._size:
                self._entries.pop(self._order.pop(0), None)
        return conv
