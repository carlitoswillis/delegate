"""Adapter that reads Claude Code's JSONL transcripts.

Claude Code writes one .jsonl per session under
~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl, one JSON object per
line, appended as the conversation happens.

Two things shape this adapter:

1. A tool call and its result live in DIFFERENT records — the `tool_use` block
   is in an assistant message, the matching `tool_result` arrives later inside
   a *user* message. The schema wants them as one Event, so calls are indexed
   by id and back-filled when the result shows up.

2. There are ~4000 transcripts on a working machine. list_sessions() must not
   parse them; it reads a bounded head and tail of each file instead. Only
   load_session() does a full parse.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from delegate_view.schema import Event, Session

# Records that are bookkeeping rather than conversation.
_SKIP_TYPES = {
    "attachment", "mode", "permission-mode", "ai-title", "last-prompt",
    "file-history-delta", "file-history-snapshot", "queue-operation", "system",
}

_HEAD_LINES = 40  # enough to reach the first real user turn
_TAIL_BYTES = 65536  # enough to catch the latest ai-title


def default_db_path() -> Path:
    return Path.home() / ".claude" / "projects"


def _ts(value: str | None) -> int:
    """ISO-8601 -> epoch millis. 0 when absent or unparseable."""
    if not value:
        return 0
    try:
        # stored as e.g. 2026-08-16T19:25:03.123Z
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _text_of(content) -> str:
    """tool_result.content is sometimes a string, sometimes a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b.get("text", "") or "")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    return ""


def _iter_files(root: Path):
    """Every transcript, including the nested subagent ones.

    Layouts in the wild:
        <project>/<session>.jsonl                                  top-level
        <project>/<parent>/subagents/agent-<id>.jsonl              subagent
        <project>/<parent>/subagents/workflows/wf_<id>/*.jsonl     workflow

    A plain */*.jsonl glob sees only the first and silently drops ~1100 files
    on this machine — which happen to be the agent-to-agent conversations,
    the most interesting thing here.
    """
    return root.rglob("*.jsonl")


def _parent_of(path: Path, root: Path) -> str | None:
    """Parent session uuid for a subagent transcript, else None.

    The uuid is the directory immediately under the project slug, and it is
    only a parent when a `subagents` component follows it.
    """
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return None
    # rel[0] is the project slug; rel[1] is the parent uuid for nested files
    if len(rel) >= 3 and "subagents" in rel:
        return rel[1]
    return None


def _peek(path: Path) -> dict:
    """Cheap metadata read: bounded head for cwd/model, bounded tail for title.

    Deliberately avoids parsing the whole file — at ~4000 transcripts a full
    parse per row turns listing sessions into reading the entire corpus.
    """
    info = {"cwd": "", "model": "", "title": "", "created": 0, "session_id": ""}

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not info["session_id"]:
                    info["session_id"] = d.get("sessionId") or d.get("session_id") or ""
                if not info["cwd"] and d.get("cwd"):
                    info["cwd"] = d["cwd"]
                if not info["created"]:
                    info["created"] = _ts(d.get("timestamp"))
                if not info["model"]:
                    m = (d.get("message") or {}).get("model")
                    if m:
                        info["model"] = m
                if not info["title"] and d.get("type") == "user" and not d.get("isMeta"):
                    txt = _text_of((d.get("message") or {}).get("content"))
                    if txt:
                        info["title"] = txt.strip().splitlines()[0][:80]
    except OSError:
        return info

    # The newest ai-title wins; it sits near the end of the file.
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the partial line
            for raw in fh:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if d.get("type") == "ai-title" and d.get("aiTitle"):
                    info["title"] = d["aiTitle"]
    except OSError:
        pass

    return info


def list_sessions(db_path: Path | None = None,
                  cwd: str | None = None) -> list[Session]:
    root = db_path or default_db_path()
    if not root.exists():
        return []

    out: list[Session] = []
    for path in _iter_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        meta = _peek(path)
        if cwd is not None and meta["cwd"] != cwd:
            continue
        parent = _parent_of(path, root)
        # Subagent files carry the PARENT's sessionId in their records, so
        # trusting sessionId there would collapse every subagent onto its
        # parent's id. The filename is the only unique handle.
        sid = path.stem if parent else (meta["session_id"] or path.stem)
        out.append(Session(
            id=sid,
            platform="claude-code",
            title=meta["title"] or path.stem,
            cwd=meta["cwd"],
            parent_id=parent,
            model=meta["model"],
            created=meta["created"] or int(stat.st_mtime * 1000),
            updated=int(stat.st_mtime * 1000),
        ))
    out.sort(key=lambda s: s.updated, reverse=True)
    return out


def _find_file(session_id: str, root: Path) -> Path | None:
    """Locate a transcript by id at any nesting depth.

    Checks the shallow path first so the common case does not pay for a full
    recursive walk of ~4000 files.
    """
    direct = list(root.glob(f"*/{session_id}.jsonl"))
    if direct:
        return direct[0]
    for path in root.rglob(f"{session_id}.jsonl"):
        return path
    return None


def load_session(session_id: str, db_path: Path | None = None) -> Session:
    root = db_path or default_db_path()
    path = _find_file(session_id, root)
    if path is None:
        raise KeyError(session_id)

    meta = _peek(path)
    session = Session(
        id=session_id,
        platform="claude-code",
        title=meta["title"] or path.stem,
        cwd=meta["cwd"],
        model=meta["model"],
        created=meta["created"],
        updated=int(path.stat().st_mtime * 1000),
    )

    events: list[Event] = []
    pending: dict[str, Event] = {}  # tool_use id -> its Event, awaiting result
    tok_in = tok_out = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = d.get("type")
            if rtype in _SKIP_TYPES or rtype not in ("user", "assistant"):
                continue
            # Injected context, not something the human typed.
            if d.get("isMeta"):
                continue

            ts = _ts(d.get("timestamp"))
            role = "assistant" if rtype == "assistant" else "user"
            msg = d.get("message") or {}

            usage = msg.get("usage") or {}
            # `input_tokens` counts only the uncached remainder. With prompt
            # caching on it is near-zero for most turns, so summing it alone
            # reports a 200k-token session as having sent ~800 tokens. Cache
            # reads and writes were still input.
            tok_in += ((usage.get("input_tokens") or 0)
                       + (usage.get("cache_read_input_tokens") or 0)
                       + (usage.get("cache_creation_input_tokens") or 0))
            tok_out += usage.get("output_tokens") or 0
            if not session.model and msg.get("model"):
                session.model = msg["model"]

            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    events.append(Event(ts=ts, role=role, kind="text", text=content))
                continue
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    if (block.get("text") or "").strip():
                        events.append(Event(ts=ts, role=role, kind="text",
                                            text=block["text"], raw=block))

                elif btype == "thinking":
                    txt = block.get("thinking") or block.get("text") or ""
                    if txt.strip():
                        events.append(Event(ts=ts, role=role, kind="reasoning",
                                            text=txt, raw=block))

                elif btype == "tool_use":
                    ev = Event(ts=ts, role=role, kind="tool_call",
                               tool_name=block.get("name", ""),
                               tool_input=block.get("input") or {},
                               tool_status="pending", raw=block)
                    events.append(ev)
                    if block.get("id"):
                        pending[block["id"]] = ev

                elif btype == "tool_result":
                    # Back-fill the call this answers; the result rides in on a
                    # user message and is not an event of its own.
                    ev = pending.pop(block.get("tool_use_id", ""), None)
                    if ev is None:
                        continue
                    ev.tool_output = _text_of(block.get("content"))
                    ev.tool_status = "error" if block.get("is_error") else "completed"
                    if ts and ev.ts:
                        ev.tool_ms = max(0, ts - ev.ts)

    session.events = events
    session.tokens_in = tok_in
    session.tokens_out = tok_out
    return session
