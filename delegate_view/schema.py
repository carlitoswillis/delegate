"""The one shape every platform gets normalized into.

Adapters live in delegate_view/adapters/ and each export the same three
functions: list_sessions(), load_session(), default_db_path(). Everything
downstream — the CLI renderer, the viewer — only ever sees Session and Event,
never a platform's native format.

Kept deliberately small. A field earns its place by being something you
actually want to SEE in a transcript, not by existing in some platform's
schema. Platform-specific extras survive in Event.raw for anyone who needs
them, without forcing every other adapter to invent a value.
"""

from dataclasses import dataclass, field

# Event.kind values. Adapters must emit one of these and nothing else.
KINDS = ("text", "reasoning", "tool_call", "patch", "compaction")

ROLES = ("user", "assistant")


@dataclass
class Event:
    """One thing that happened in a conversation, in display order.

    A tool call and its result are ONE event, not two. Platforms disagree on
    whether those are separate records (Claude Code splits them across
    messages; opencode nests the result inside the call's state), and joining
    them in each adapter is the only way the viewer can render a call next to
    what it returned without re-deriving the pairing per platform.
    """

    ts: int  # epoch millis
    role: str  # one of ROLES
    kind: str  # one of KINDS

    # text / reasoning. NOT patch or compaction: opencode stores a patch as
    # {"hash":…, "files":[…]} with the diff living in a git object, and a
    # compaction as a {"tail_start_id":…} marker. Neither carries prose, so
    # both leave this empty and keep their payload in `raw`.
    text: str = ""

    # tool_call only
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ""
    tool_status: str = ""  # "completed" | "error" | "pending"
    tool_ms: int | None = None  # None when the call has not finished

    # the untouched platform payload, for anything this schema drops
    raw: dict = field(default_factory=dict)


@dataclass
class Session:
    """One conversation with one agent.

    parent_id is what makes subagent runs readable: opencode files an
    @explore subagent as its own session pointing at its caller, and Claude
    Code marks the same idea with sidechain entries. Normalizing both to a
    parent pointer lets the viewer nest them instead of showing a stray
    conversation with no visible cause.

    path is the liveness signal: mtime on that file is how the viewer knows
    a conversation is still being written.  Left empty for platforms where a
    session is not one file (opencode keeps sessions in a shared SQLite DB,
    so an mtime there tells you only that *something* changed).
    """

    id: str
    platform: str  # "opencode" | "claude-code" | "codex" | "delegate-log"
    title: str
    cwd: str
    model: str  # "provider/model"
    agent: str = ""
    parent_id: str | None = None
    created: int = 0  # epoch millis
    updated: int = 0  # epoch millis
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    path: str = ""  # transcript file backing this session, when it is a file
    events: list[Event] = field(default_factory=list)
