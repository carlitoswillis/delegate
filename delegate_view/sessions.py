"""Every conversation worth showing, from every source, as one list.

The run list used to be built from two sources: the delegate.sh ledger, and
Claude Code SUBAGENT transcripts. Anything else you talked to was invisible.
On this machine that meant 19 ledger lines standing in for 63 opencode
sessions — every chat started directly with `opencode` was missing, which is
most of them, and the ones that were present were present because a shell
script happened to write them down.

So the sources are inverted here. The transcript stores ARE the record of what
happened; the ledger is extra knowledge ABOUT some of those conversations —
which task file was handed over, where the readable log went. This module
lists the stores and folds the ledger's knowledge in on top.

DE-DUPLICATION is therefore the whole job. A delegate.sh run and the opencode
session it produced are ONE conversation seen from two sides, and listing both
sides shows it twice. runs.py already resolves a ledger run to its
(platform, session_id); that pair is the identity everything merges on.
"""

from __future__ import annotations

import os
import time

from delegate_view.runs import Run, is_live, key_of, load_runs

# The listers this module knows about. "ledger" is delegate.sh's runs.jsonl;
# the other two are the platforms' own stores.
SOURCES = ("ledger", "opencode", "claude-code")

# Default cap per source when the caller gives no limit.
#
# opencode is uncapped because listing every session is one small SQL scan
# (0.5ms for 64 rows on a 140MB database) and capping it is what caused the
# bug this module exists to fix.
#
# Claude Code is capped because it is 4238 files on this machine, and listing
# one costs a bounded head-and-tail read: ~0.6ms each, so the full corpus is
# ~2.5 SECONDS. The TUI refreshes every 2 seconds. The cap is on the newest by
# mtime, which is the end of the list anybody scrolls to.
_DEFAULT_LIMITS = {
    "ledger": None,
    "opencode": None,
    "claude-code": 100,
}


def _limit_for(source: str, limit_per_source) -> int | None:
    """Resolve the cap for one source.

    Accepts None (per-source defaults), an int (the same cap everywhere), or a
    mapping of source name to cap. The mapping form exists because the sources
    are not alike: 100 Claude Code transcripts is a sane page, while 100
    opencode sessions would silently truncate a 63-session store the day it
    grows past the cap, and re-create the reported bug.
    """
    if limit_per_source is None:
        return _DEFAULT_LIMITS.get(source)
    if isinstance(limit_per_source, dict):
        return limit_per_source.get(source, _DEFAULT_LIMITS.get(source))
    return limit_per_source


def _run_from_session(s, source: str) -> Run:
    """One Session -> one Run, without inventing anything it does not know."""
    path = s.path or ""
    size = 0
    if path:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

    run = Run(
        started=s.created or s.updated,
        # No prompt FILE — nobody handed this conversation a task file. That
        # is not the same as having no prompt, and it is not what makes a
        # conversation a subagent's; see Run.is_subagent.
        prompt="",
        transcript=path,
        model=s.model,
        cwd=s.cwd,
        size=size,
        prompt_text=s.title or "",
        session_id=s.id,
        platform=s.platform,
        tokens_in=s.tokens_in,
        tokens_out=s.tokens_out,
        cost=s.cost,
        source=source,
        is_subagent=bool(s.parent_id),
        parent_id=s.parent_id or "",
        updated=s.updated,
    )
    run.live = is_live(run)
    return run


def _opencode_runs(limit: int | None) -> list[Run]:
    """Every opencode session, newest-updated first.

    Includes the @explore subagent sessions: they carry a parent_id, so they
    are marked rather than dropped. They are the agent-to-agent conversations,
    which is the most interesting thing in the store.
    """
    from delegate_view.adapters import opencode

    sessions = opencode.list_sessions()
    sessions.sort(key=lambda s: s.updated or s.created, reverse=True)
    if limit is not None:
        sessions = sessions[:limit]
    return [_run_from_session(s, "opencode") for s in sessions]


def _claude_code_runs(limit: int | None) -> list[Run]:
    """Recent Claude Code transcripts, top-level conversations and subagents.

    Top-level sessions are included for the same reason direct opencode chats
    are: excluding them would mean the list shows every conversation you had
    with one tool and only the delegated half of the other. The adapter's
    `limit` is applied while sorting paths by mtime, BEFORE any file is
    opened, so the cap is what keeps this cheap.
    """
    from delegate_view.adapters import claude_code

    sessions = claude_code.list_sessions(limit=limit)
    return [_run_from_session(s, "claude-code") for s in sessions]


_LOADERS = {
    "opencode": _opencode_runs,
    "claude-code": _claude_code_runs,
}


def _fold_session_into_run(run: Run, other: Run) -> None:
    """Merge a directly-listed session into the ledger run that produced it.

    The ledger run keeps everything the session cannot know — which task file
    was sent, its text, where the readable transcript is — and takes the
    session's live statistics, which the ledger never had. Nothing is
    overwritten with an empty value: a session with no title must not blank
    out the prompt text the ledger read off disk.
    """
    run.tokens_in = other.tokens_in or run.tokens_in
    run.tokens_out = other.tokens_out or run.tokens_out
    run.cost = other.cost or run.cost
    run.updated = max(run.updated, other.updated)
    run.model = run.model or other.model
    run.cwd = run.cwd or other.cwd
    run.parent_id = run.parent_id or other.parent_id
    run.prompt_text = run.prompt_text or other.prompt_text
    # A ledger run is a delegation, never a subagent — a person typed the
    # command. Its liveness comes from its own transcript's END marker, which
    # is a better signal than the session's timestamps, so it is left alone.


def all_runs(ledger_path=None, limit_per_source=None, include=None) -> list[Run]:
    """The unified, de-duplicated, newest-first list of conversations.

    Sources are "ledger", "opencode" and "claude-code" (see SOURCES).
    `include` narrows to a subset of those names; None means all of them.

    `limit_per_source` caps how many each source contributes, newest first:
    None for the per-source defaults, an int for one cap everywhere, or a
    {source: cap} mapping.

    One source failing never suppresses another. A missing store, a locked
    database, a corrupt transcript — each is contained to its own source and
    the rest of the list still appears, because the alternative is a viewer
    that shows nothing at all the first time an adapter meets a version it
    does not recognise.
    """
    wanted = set(include) if include is not None else set(SOURCES)

    ledger_runs: list[Run] = []
    if "ledger" in wanted:
        try:
            ledger_runs = load_runs(ledger_path=ledger_path)
            cap = _limit_for("ledger", limit_per_source)
            if cap is not None:
                ledger_runs = ledger_runs[:cap]
        except Exception:
            ledger_runs = []

    # The ledger goes in first so that a run and the session it produced merge
    # INTO the ledger row, keeping the richer metadata.
    out: list[Run] = []
    by_key: dict[tuple, Run] = {}
    for r in ledger_runs:
        k = key_of(r)
        if k in by_key:
            # Two ledger lines that resolved to one session — a `-c`
            # continuation, before the one-to-one resolver made that
            # impossible. Keep the newer line; both wrote to the same
            # transcript, so nothing is lost from the reading.
            continue
        by_key[k] = r
        out.append(r)

    for source in SOURCES:
        if source == "ledger" or source not in wanted:
            continue
        try:
            runs = _LOADERS[source](_limit_for(source, limit_per_source))
        except Exception:
            continue
        for r in runs:
            k = key_of(r)
            existing = by_key.get(k)
            if existing is not None:
                _fold_session_into_run(existing, r)
                continue
            by_key[k] = r
            out.append(r)

    out.sort(key=lambda r: r.started, reverse=True)
    return out


def refresh_liveness(runs: list[Run], now: float | None = None) -> None:
    """Recompute `live` for an existing list, in place.

    Cheap enough to call every refresh: a stat per file-backed run and nothing
    at all for the database-backed ones.
    """
    now = time.time() if now is None else now
    for r in runs:
        r.live = is_live(r, now)
