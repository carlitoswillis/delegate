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

import os
import sys
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


# ── directory identity ──────────────────────────────────────────────────

def norm_dir(path: str) -> str:
    """A comparable identity for a working directory.

    Two records that mean the same directory routinely spell it differently,
    and an exact string compare then silently resolves nothing:

    * `delegate.sh` records `$PWD`, the shell's LOGICAL path, while opencode
      stores the PHYSICAL one.  On macOS `/tmp` is a symlink to `/private/tmp`,
      so a run started in `/tmp/x` lands in the ledger as `/tmp/x` and in the
      DB as `/private/tmp/x`.  realpath() collapses that.
    * The default macOS filesystem is case-INSENSITIVE, so a `cd
      ~/workspace/termdeck` records a different string than the
      `~/workspace/Termdeck` the tool wrote, for the same directory.  realpath
      does not fix case (it only follows symlinks), so the case fold does.

    The case fold is wrong on a case-SENSITIVE volume, where `Foo` and `foo`
    really are two directories.  That configuration is opt-in and rare on the
    platforms this runs on, and the cost of getting it wrong there (two
    unrelated directories treated as one, in a read-only viewer) is far
    smaller than the cost of the default case being broken for everyone.
    """
    if not path:
        return ""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        real = path
    real = real.rstrip("/") or "/"
    if sys.platform in ("darwin", "win32"):
        return real.casefold()
    return real
