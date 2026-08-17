"""The runs index: what was delegated, and which session it produced.

`delegate.sh` appends one JSON line per run to ~/.delegate/runs.jsonl before
handing the task over. The agents themselves leave transcripts elsewhere —
opencode in SQLite, Claude Code in JSONL. Nothing else joins those two facts,
so this module is the join.

The ledger line is written BEFORE the agent starts, which is what makes the
join possible at all: the session this run produced is the first one to appear
in that directory at or after `started`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# A transcript touched within this many seconds counts as a live run. The
# ledger records the shell's parent pid, but that pid is recycled and tells
# you nothing once the shell exits — file mtime is the honest signal.
LIVE_WINDOW_S = 30


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


def _apply_session_to_run(run: Run, sessions: list) -> tuple[str, str]:
    """Pick the best session from a pre-fetched list and populate run stats.

    Among sessions whose ``created`` is truthy and ``>= run.started``, pick
    the one with the smallest ``created``.  Returns (platform, session_id) or
    ("", "") when nothing matches.

    NOTE: two runs in the same cwd started seconds apart will both resolve to
    the same earliest session.  Fixing that is out of scope here.
    """
    candidates = [s for s in sessions if s.created and s.created >= run.started]
    if not candidates:
        return "", ""
    best = min(candidates, key=lambda s: s.created)
    run.tokens_in = best.tokens_in
    run.tokens_out = best.tokens_out
    run.cost = best.cost
    return best.platform, best.id


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
    return _apply_session_to_run(run, sessions)


def resolve_sessions(runs: list[Run]) -> None:
    """Resolve all runs in one batch — one list_sessions call per distinct cwd.

    Much faster than calling resolve_session per run: on a ledger with 13
    runs across 3 cwds this issues 3 adapter queries instead of 13.
    """
    from collections import defaultdict
    from delegate_view import adapters

    by_cwd: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        by_cwd[r.cwd].append(r)

    session_cache: dict[str, list] = {}
    for cwd, cwd_runs in by_cwd.items():
        try:
            sessions = adapters.list_sessions(cwd=cwd)
        except Exception:
            sessions = []
        session_cache[cwd] = sessions

    for r in runs:
        r.platform, r.session_id = _apply_session_to_run(r, session_cache[r.cwd])


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
                try:
                    stat = os.stat(transcript)
                    size = stat.st_size
                    live = (now - stat.st_mtime) < LIVE_WINDOW_S
                except OSError:
                    size, live = 0, False

                runs.append(Run(
                    started=int(d.get("started") or 0),
                    prompt=d.get("prompt", ""),
                    transcript=transcript,
                    model=d.get("model", ""),
                    cwd=d.get("cwd", ""),
                    continued=bool(d.get("continued")),
                    live=live,
                    size=size,
                    prompt_text=_prompt_head(d.get("prompt", "")),
                    raw=d,
                ))
    except OSError:
        return []

    runs.sort(key=lambda r: r.started, reverse=True)

    if resolve:
        resolve_sessions(runs)
    return runs
