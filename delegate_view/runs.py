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


def resolve_session(run: Run) -> tuple[str, str]:
    """(platform, session_id) for the session this run produced, else ("", "").

    Picks the OLDEST session in the run's directory created at or after the
    ledger timestamp. Newest-first would pick up a later, unrelated run in the
    same directory — which is exactly what happens when you delegate twice.
    """
    # Imported lazily: a broken adapter should degrade the session link, not
    # stop the ledger from listing at all.
    try:
        from delegate_view import adapters
        sessions = adapters.list_sessions(cwd=run.cwd)
    except Exception:
        return "", ""

    candidates = [s for s in sessions if s.created and s.created >= run.started]
    if not candidates:
        return "", ""
    best = min(candidates, key=lambda s: s.created)
    return best.platform, best.id


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
        for r in runs:
            r.platform, r.session_id = resolve_session(r)
    return runs
