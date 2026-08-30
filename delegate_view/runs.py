"""The runs index: what was delegated, and which session it produced.

`delegate.sh` appends one JSON line per run to ~/.delegate/runs.jsonl before
handing the task over. The agents themselves leave transcripts elsewhere —
opencode in SQLite, Claude Code in JSONL. Nothing else joins those two facts,
so this module is the join.

The ledger line is written BEFORE the agent starts, which is what makes the
join possible at all: the session this run produced is the first one to appear
in that directory at or after `started`.

This module covers ledger runs only. The unified list — ledger runs plus every
opencode and Claude Code conversation, de-duplicated — lives in sessions.py,
which builds on what is here.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from delegate_view.schema import norm_dir

# A transcript touched within this many seconds counts as recently active.
LIVE_WINDOW_S = 30

# ...but a delegate.sh transcript is judged by its closing marker first, and
# only falls back to mtime. See transcript_state() for the marker; this is the
# fallback window for a transcript that has NO marker yet.
#
# It is deliberately much wider than LIVE_WINDOW_S. A model that has been
# thinking quietly for 45 seconds writes nothing, and calling it dead was a
# real false negative. The window still has to close eventually, because a run
# killed with SIGKILL never gets to write its marker either, and a permanent
# spinner is a worse lie than a premature one. Five minutes covers the longest
# quiet turns observed and bounds the damage from a hard kill.
UNCLOSED_LIVE_WINDOW_S = 300

# How long after a ledger line a session may appear and still be that run's.
#
# Observed on real data: every one of the 19 recorded runs produced its session
# 0.6-1.6s after the ledger line. The window is generous next to that because a
# cold `opencode` start is slower than a warm one — but it must stay FINITE,
# because the alternative is what the old resolver did: a run with no session
# at all silently adopted the next unrelated chat someone started in the same
# directory five minutes later.
RESOLVE_WINDOW_MS = 60_000

# delegate.sh's transcript markers. The header is written before the agent
# starts; the END line is written by an EXIT/INT/TERM trap, so it survives a
# crash or a Ctrl-C and says which of those happened.
REPLY_MARKER = "===== REPLY ====="
END_MARKER = "===== END "
_END_RE = re.compile(r"^===== END .*? — (.+?) =====\s*$")

# How much of a transcript's end to read when checking those markers. One
# seek and one small read, not a parse — the TUI does this every refresh.
_TAIL_BYTES = 8192

# end_reason values this module synthesizes for a run that never got far
# enough to write a marker of its own.
REASON_MISSING = "no transcript"
REASON_EMPTY = "no reply"

# An END marker whose reason starts with one of these means the run did not
# finish cleanly. `failed` also carries an exit code — "failed (exit 3)" — so
# the match is on the first word.
_FAILED_REASONS = ("failed", "interrupted", "terminated")


@dataclass
class Run:
    started: int  # epoch millis
    prompt: str
    transcript: str
    model: str
    cwd: str
    continued: bool = False
    live: bool = False
    size: int = 0
    prompt_text: str = ""
    session_id: str = ""
    platform: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    # Which listing produced this run: "ledger", "opencode" or "claude-code".
    # `platform` says where the CONVERSATION lives and is empty until a ledger
    # run resolves; `source` says which lister put the row on screen and is
    # always set. Display rules key off source — only a ledger run has a
    # delegate.sh transcript with markers in it.
    source: str = "ledger"

    # Whether this conversation was started by another agent rather than by a
    # person. Was previously inferred from `not run.prompt`, which is wrong now
    # that a chat you started yourself in opencode also has no prompt file and
    # would be mislabelled as an agent-to-agent conversation.
    is_subagent: bool = False

    # The conversation that spawned this one, when it is a subagent.
    parent_id: str = ""

    # Last activity, epoch millis. The liveness signal for a conversation that
    # is not backed by a file — an opencode session lives in a shared SQLite
    # database whose mtime says only that *something* changed.
    updated: int = 0

    # A run that is over and did not produce a conversation. NOT hidden: the
    # ledger is append-only on purpose ("if the run dies, the record of what
    # was asked survives"), so a failure is marked, never dropped.
    failed: bool = False

    # Why it ended, for display: the END marker's own words ("completed",
    # "failed (exit 3)", "interrupted", "terminated"), or one of the
    # REASON_* strings above when there was no marker to read. Empty means
    # unknown — an old transcript, or a run still going.
    end_reason: str = ""

    raw: dict = field(default_factory=dict)


def default_ledger_path() -> Path:
    env = os.environ.get("DELEGATE_LEDGER")
    if env:
        return Path(env)
    return Path.home() / ".delegate" / "runs.jsonl"


def tail(path: str | Path, n: int = 40) -> list[str]:
    """Last n lines, read from the end.

    Transcripts run to hundreds of KB and the TUI re-reads on every refresh,
    so this seeks backwards in chunks rather than reading the whole file.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size == 0:
        return []

    chunk = 8192
    data = b""
    with open(path, "rb") as fh:
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[-n:]


def _prompt_head(path: str, limit: int = 200) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


# ── transcript state ────────────────────────────────────────────────────

def transcript_state(path: str) -> tuple[bool, str, int, float]:
    """(has_reply, end_reason, size, mtime) for a delegate.sh transcript.

    One bounded read of the file's tail answers both questions the run list
    needs: did the agent say anything, and did the run close itself.

    `has_reply` is what separates a real run from a phantom. A mistyped prompt
    filename used to leave a transcript containing nothing but the header —
    `watcn-transcript.log` in this repo, 63 bytes, sitting next to the
    `watch-transcript.log` it was a typo of. Those look like ordinary runs in
    the list and open to nothing.

    Reading only the tail means the REPLY marker may have scrolled out of the
    window — which is itself the answer: a transcript with more than a
    windowful of text after its header plainly has a reply in it.

    Returns end_reason "" when there is no END marker: either the run is still
    going, or the transcript predates delegate.sh writing one.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return False, REASON_MISSING, 0, 0.0

    size = stat.st_size
    if size == 0:
        return False, REASON_EMPTY, 0, stat.st_mtime

    try:
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            window = fh.read().decode("utf-8", "replace")
    except OSError:
        return False, REASON_MISSING, size, stat.st_mtime

    end_reason = ""
    end_at = window.rfind(END_MARKER)
    if end_at != -1:
        m = _END_RE.match(window[end_at:].splitlines()[0])
        if m:
            end_reason = m.group(1).strip()

    reply_at = window.rfind(REPLY_MARKER)
    if reply_at != -1:
        body = window[reply_at + len(REPLY_MARKER):]
        if end_at > reply_at:
            body = window[reply_at + len(REPLY_MARKER):end_at]
        has_reply = bool(body.strip())
    else:
        # No header in the window at all: everything here is content that came
        # after one, unless the whole file fit in the window and simply has no
        # header — which is a transcript delegate.sh never got to write.
        has_reply = size > _TAIL_BYTES

    if not has_reply and not end_reason:
        end_reason = REASON_EMPTY
    return has_reply, end_reason, size, stat.st_mtime


def _reason_is_failure(end_reason: str) -> bool:
    if not end_reason:
        return False
    if end_reason in (REASON_MISSING, REASON_EMPTY):
        return True
    return end_reason.split()[0] in _FAILED_REASONS


def is_live(run: Run, now: float | None = None) -> bool:
    """Whether this conversation is still being written.

    Three different signals, because the three sources leave three different
    kinds of evidence:

    * A ledger run has a delegate.sh transcript, and since delegate.sh closes
      every transcript with an END marker the marker is the honest answer.
      No marker means still running — bounded by mtime so a hard-killed run
      does not spin forever, and so the transcripts written before the marker
      existed do not all light up at once.
    * A Claude Code session is one JSONL file appended as it goes, so mtime is
      the signal.
    * An opencode session is rows in a shared database with no file of its
      own; `updated` is the only thing that moves.
    """
    now = time.time() if now is None else now

    if run.source == "ledger":
        if not run.transcript:
            return False
        try:
            mtime = os.stat(run.transcript).st_mtime
        except OSError:
            return False
        if run.end_reason and run.end_reason not in (REASON_MISSING, REASON_EMPTY):
            return False  # the run said how it ended; it is not still going
        return (now - mtime) < UNCLOSED_LIVE_WINDOW_S

    if run.transcript:
        try:
            mtime = os.stat(run.transcript).st_mtime
        except OSError:
            return False
        return (now - mtime) < LIVE_WINDOW_S

    if run.updated:
        return (now - run.updated / 1000.0) < LIVE_WINDOW_S
    return False


def key_of(run: Run) -> tuple:
    """Stable identity for a run across refreshes and across sources.

    This is what de-duplication and the TUI's conversation cache both key on,
    which is why it lives next to Run rather than in either of them.

    A resolved run is identified by the conversation it points at
    (platform, session_id) — that is the same pair whether the row came from
    the ledger or straight from the platform's store, so the two collapse into
    one. An unresolved ledger run has no conversation to name, so it falls
    back to (transcript, started): unique per line, and never equal to a
    (platform, session_id) pair.
    """
    if run.platform and run.session_id:
        return (run.platform, run.session_id)
    return (run.transcript, run.started)


# ── resolution: which session did this run produce? ─────────────────────

def _claimable(sessions: list) -> list:
    """Sessions a delegate.sh run could plausibly have created.

    A run starts ONE top-level conversation. The @explore subagents that
    conversation spawns are separate sessions in the same directory, created
    seconds later — close enough to be mistaken for the next run's session,
    so they are excluded from the pool rather than left to be miscounted.
    """
    return sorted(
        (s for s in sessions if s.created and not s.parent_id),
        key=lambda s: s.created,
    )


def _assign(runs: list[Run], sessions: list) -> None:
    """Match runs to sessions one-to-one, in time order.

    The old rule was "each run takes the earliest session created after it",
    applied per run and with no memory. Two runs seconds apart therefore both
    claimed the SAME session, and the list showed one conversation twice while
    the other run showed none.

    This walks SESSIONS in creation order and gives each one to the nearest
    unclaimed run that preceded it, which is the direction that survives the
    case the run-first walk gets wrong: a run that produced no session at all.
    Walking runs first, that run swallows the next run's session and every
    later pairing shifts by one — which is exactly what happened to
    `selection-indicator.md`, whose session never existed and which therefore
    stole `fast-subagent-listing.md`'s.

    Claiming is bounded by RESOLVE_WINDOW_MS so that a run with no session
    stays unresolved instead of adopting an unrelated chat started later in
    the same directory.
    """
    for r in runs:
        r.platform, r.session_id = "", ""

    unclaimed = sorted(runs, key=lambda r: r.started)
    for s in _claimable(sessions):
        best = None
        for r in unclaimed:  # ascending started
            if r.started > s.created:
                break
            if (s.created - r.started) <= RESOLVE_WINDOW_MS:
                best = r  # a later run is a nearer one; keep looking
        if best is None:
            continue
        unclaimed.remove(best)
        best.platform, best.session_id = s.platform, s.id
        best.tokens_in = s.tokens_in
        best.tokens_out = s.tokens_out
        best.cost = s.cost
        best.updated = s.updated or best.updated


def resolve_session(run: Run) -> tuple[str, str]:
    """(platform, session_id) for the session this run produced, else ("", "").

    Also populates run.tokens_in, run.tokens_out, run.cost from the session
    when available (OpenCode populates these in list_sessions; Claude Code
    does not).
    """
    try:
        from delegate_view import adapters
        sessions = adapters.list_sessions(cwd=run.cwd)
    except Exception:
        return "", ""
    _assign([run], sessions)
    return run.platform, run.session_id


def resolve_sessions(runs: list[Run]) -> None:
    """Resolve all runs in one batch — one list_sessions call per distinct cwd.

    Much faster than calling resolve_session per run: on a ledger with 13
    runs across 3 cwds this issues 3 adapter queries instead of 13.

    Grouping is by NORMALIZED directory, so runs recorded as `/tmp/x` and
    `/private/tmp/x` share one query and one pool of sessions to claim from.
    """
    from collections import defaultdict
    from delegate_view import adapters

    by_cwd: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        by_cwd[norm_dir(r.cwd)].append(r)

    for cwd_runs in by_cwd.values():
        try:
            # Any spelling of the directory does — the adapters normalize.
            sessions = adapters.list_sessions(cwd=cwd_runs[0].cwd)
        except Exception:
            sessions = []
        _assign(cwd_runs, sessions)


def load_runs(ledger_path: Path | None = None,
              resolve: bool = True) -> list[Run]:
    """Every recorded run, newest first.

    A missing ledger is normal (nothing delegated yet) and returns []. A
    malformed line is skipped: a run killed mid-write leaves a partial line,
    and one bad line must not hide every other run.
    """
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    if not path.exists():
        return []

    now = time.time()
    runs: list[Run] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict) or "started" not in d:
                    continue

                transcript = d.get("transcript", "")
                has_reply, end_reason, size, _mtime = transcript_state(transcript)

                run = Run(
                    started=int(d.get("started") or 0),
                    prompt=d.get("prompt", ""),
                    transcript=transcript,
                    model=d.get("model", ""),
                    cwd=d.get("cwd", ""),
                    continued=bool(d.get("continued")),
                    size=size,
                    prompt_text=_prompt_head(d.get("prompt", "")),
                    source="ledger",
                    end_reason=end_reason,
                    raw=d,
                )
                run.live = is_live(run, now)
                runs.append(run)
    except OSError:
        return []

    runs.sort(key=lambda r: r.started, reverse=True)

    if resolve:
        resolve_sessions(runs)

    _mark_failures(runs, now)
    return runs


def _mark_failures(runs: list[Run], now: float) -> None:
    """Flag runs that ended without producing a conversation.

    Two ways a run is a failure, and both need the whole picture rather than
    the transcript alone:

    * It closed with a marker saying it failed, was interrupted, or was
      terminated.
    * It has no transcript, or a transcript with nothing after the header, AND
      it never resolved to a session — the phantom left behind when the prompt
      file did not exist. The session check matters because a run whose
      transcript was moved or deleted is still readable through its session.

    A run that started moments ago has not written its reply yet, so nothing
    is marked until it is old enough to have failed rather than just begun.
    """
    for r in runs:
        if r.source != "ledger":
            continue
        if r.live:
            continue
        if now - (r.started / 1000.0) < LIVE_WINDOW_S:
            continue
        if r.end_reason in (REASON_MISSING, REASON_EMPTY) and r.session_id:
            # The conversation survived even though the transcript did not.
            r.end_reason = ""
            continue
        r.failed = _reason_is_failure(r.end_reason)
