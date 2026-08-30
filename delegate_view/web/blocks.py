"""Events -> display blocks.  The web equivalent of views.session_blocks.

Not a reuse of views.py, deliberately.  That module answers a different
question: it flattens a session into styled *lines*, already wrapped to a
terminal width, with a one-column rule standing in for structure because a
terminal has no other way to draw a box.  A browser has structure natively,
and a phone needs it — a tool call that can be collapsed, output that scrolls
in its own box, a code fence that keeps its shape while the prose around it
reflows.  Flattening to lines first and then trying to re-derive blocks from
them would throw away exactly the information the HTML needs.

What is shared is the naming: `speakers.exchange_header` decides who is
talking, so a delegated turn reads "you -> big-pickle" here for the same
reason it does in the TUI.  See that module for why the raw role labels are
actively misleading in a delegated transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from delegate_view.speakers import exchange_header

# Tool output can be enormous (a full file read, a 10k-line test log).  The
# page keeps a bounded head so a single runaway tool call cannot turn one
# conversation into a 50MB response to a phone on cellular data.
MAX_OUTPUT_LINES = 200
MAX_OUTPUT_CHARS = 20_000

# Reasoning is shown in full but collapsed; this only bounds the teaser.
REASONING_TEASER = 160

# One argument's worth of text. A Write call carries the entire file it wrote,
# and a page with three of those in it is a megabyte of HTML for a phone to
# download and lay out. The head of the value tells you what the call did;
# the whole file does not tell you more, and the file itself is on disk.
MAX_ARG_CHARS = 4_000

# Which argument of a tool call is worth putting in the collapsed summary,
# in preference order per tool.  Seeing "Bash: pytest tests/ -q" in the
# summary is most of the value of the call; seeing "Bash" is none of it.
_SUMMARY_KEYS = {
    "bash": ("command", "description"),
    "read": ("file_path", "path", "notebook_path"),
    "write": ("file_path", "path"),
    "edit": ("file_path", "path"),
    "multiedit": ("file_path", "path"),
    "notebookedit": ("notebook_path", "path"),
    "grep": ("pattern",),
    "glob": ("pattern",),
    "task": ("description", "subagent_type"),
    "webfetch": ("url",),
    "websearch": ("query",),
    "todowrite": (),
}

_GENERIC_KEYS = ("command", "file_path", "path", "pattern", "query", "url",
                 "description", "prompt")


@dataclass
class Chunk:
    """A run of prose or a fenced code block inside one turn."""
    kind: str  # "prose" | "code"
    text: str
    lang: str = ""


@dataclass
class Block:
    """One rendered unit of a conversation."""
    kind: str  # "turn" | "reasoning" | "tool" | "patch" | "compaction" | "raw"
    role: str = ""
    speaker: str = ""
    chunks: list[Chunk] = field(default_factory=list)
    text: str = ""
    name: str = ""
    summary: str = ""
    status: str = ""
    ms: int | None = None
    args: list[tuple[str, str]] = field(default_factory=list)
    output: str = ""
    output_clipped: bool = False
    files: list[str] = field(default_factory=list)
    ts: int = 0


def split_fences(text: str) -> list[Chunk]:
    """Prose and ```fenced``` code, kept apart.

    The only markdown this renderer understands, and the only one it needs to.
    Prose reflows to the screen width; code must not — a wrapped diff or a
    wrapped stack trace is unreadable, and on a phone everything wraps unless
    you say otherwise.  Keeping the two as separate chunks is what lets the
    page scroll code sideways inside its own box while the page itself never
    scrolls sideways at all.

    Nothing here interprets the content: an unterminated fence closes at the
    end of the turn, and the fence marker never becomes markup.  Escaping
    happens downstream in pages.py, on every chunk, without exception.
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    chunks: list[Chunk] = []
    buf: list[str] = []
    lang = ""
    in_code = False

    def flush(kind: str, language: str = "") -> None:
        if not buf:
            return
        body = "\n".join(buf)
        if kind == "prose" and not body.strip():
            buf.clear()
            return
        chunks.append(Chunk(kind=kind, text=body, lang=language))
        buf.clear()

    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if in_code:
                flush("code", lang)
                in_code = False
                lang = ""
            else:
                flush("prose")
                in_code = True
                # Only a bare word is treated as a language; anything else is
                # decoration and is dropped rather than trusted into markup.
                info = stripped[3:].strip()
                lang = info if info.isalnum() else ""
            continue
        buf.append(line)

    flush("code" if in_code else "prose", lang)
    return chunks


def clip_output(text: str) -> tuple[str, bool]:
    """Bounded tool output, plus whether anything was dropped."""
    if not text:
        return "", False
    clipped = False
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS]
        clipped = True
    lines = text.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        lines = lines[:MAX_OUTPUT_LINES]
        clipped = True
    return "\n".join(lines), clipped


def _stringify(value) -> str:
    if isinstance(value, str):
        return value[:MAX_ARG_CHARS] + ("\n… truncated" if len(value) > MAX_ARG_CHARS else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = repr(value)
    return text[:MAX_ARG_CHARS] + ("\n… truncated" if len(text) > MAX_ARG_CHARS else "")


def tool_summary(name: str, tool_input: dict) -> str:
    """The one argument worth showing next to the tool name when collapsed."""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    keys = _SUMMARY_KEYS.get((name or "").lower(), None)
    if keys is None:
        keys = _GENERIC_KEYS
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().split("\n")[0]
            return first[:160]
    return ""


def tool_args(tool_input: dict) -> list[tuple[str, str]]:
    """Every argument as (name, text), longest values last.

    Order matters on a narrow screen: `file_path` next to a 400-line `content`
    is unreadable if the content came first, and the short scalar arguments
    are the ones you are actually scanning for.
    """
    if not isinstance(tool_input, dict):
        return []
    pairs = [(str(k), _stringify(v)) for k, v in tool_input.items()]
    pairs.sort(key=lambda kv: len(kv[1]))
    return pairs


def blocks_for_session(events, *, model: str = "", platform: str = "",
                       is_subagent: bool = False) -> list[Block]:
    """A session's events as display blocks, consecutive turns merged.

    Merging consecutive same-speaker text events into one turn is what stops
    a page from becoming a wall of repeated name headers: platforms split a
    single reply across several text parts for reasons that have nothing to do
    with how it should read.
    """
    out: list[Block] = []
    current: Block | None = None

    for ev in events:
        kind = getattr(ev, "kind", "")

        if kind == "text":
            speaker = exchange_header(ev.role, model=model, platform=platform,
                                      is_subagent=is_subagent)
            if current is None or current.speaker != speaker:
                current = Block(kind="turn", role=ev.role, speaker=speaker,
                                ts=getattr(ev, "ts", 0))
                out.append(current)
            current.chunks.extend(split_fences(ev.text))
            continue

        # Anything that is not text ends the current turn: a tool call between
        # two paragraphs really is a break in the speaking, and continuing the
        # same bubble across it misrepresents the order things happened in.
        current = None

        if kind == "reasoning":
            out.append(Block(kind="reasoning", role=ev.role,
                             text=(ev.text or "").strip(),
                             ts=getattr(ev, "ts", 0)))
        elif kind == "tool_call":
            output, clipped = clip_output(ev.tool_output or "")
            out.append(Block(
                kind="tool", role=ev.role,
                name=ev.tool_name or "tool",
                summary=tool_summary(ev.tool_name, ev.tool_input),
                status=ev.tool_status or "",
                ms=ev.tool_ms,
                args=tool_args(ev.tool_input),
                output=output,
                output_clipped=clipped,
                ts=getattr(ev, "ts", 0),
            ))
        elif kind == "patch":
            raw = getattr(ev, "raw", {}) or {}
            files = [str(f) for f in (raw.get("files") or [])]
            out.append(Block(kind="patch", role=ev.role, files=files,
                             ts=getattr(ev, "ts", 0)))
        elif kind == "compaction":
            out.append(Block(kind="compaction", role=ev.role,
                             ts=getattr(ev, "ts", 0)))

    return [b for b in out if b.kind != "turn" or b.chunks]


def blocks_for_raw(lines) -> list[Block]:
    """The fallback view: a transcript file with no session behind it yet.

    A delegation that started thirty seconds ago has no resolved session, and
    it is the single most likely thing someone opens this on a phone to see.
    Showing the tail of the log as preformatted text is the honest answer.
    """
    if not lines:
        return []
    return [Block(kind="raw", text="\n".join(lines))]
