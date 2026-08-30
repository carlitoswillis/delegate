"""The screens, as pure functions from state to styled lines.

Every function here returns `list[render.Line]` and touches neither curses nor
the filesystem, so a screen can be asserted on as data. The curses loop in
watch.py only paints what these return.

The layout is deliberately flat: almost no box drawing, a coloured left rule
instead of a full border, and vertical space used to separate things rather
than lines. Boxes cost two columns and two rows per block and spend them on
chrome; on an 80-column terminal showing a transcript that is most of the
budget. Space groups just as well and reads calmer.
"""

from __future__ import annotations

from delegate_view.render import (
    Line,
    Span,
    blank,
    hstack,
    line_of,
    pad,
    truncate,
    width_of,
    wrap,
)
from delegate_view.scrollbar import render_column
from delegate_view.speakers import exchange_header, short_model
from delegate_view.theme import style_for_model

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# The coloured rule that marks who is speaking, in place of a box.
RULE = "▎"

# Rows a run occupies in the list: a title line and a meta line.
ROWS_PER_RUN = 2

# Below this width the split view stops being readable and the conversation
# takes the whole screen instead.
SPLIT_MIN_WIDTH = 100

# Columns render_convo spends on margin and scroll bar. wrap_body must use
# the same number or wrapping and scrolling disagree.
CONVO_INSET = 4


# ── small formatters ────────────────────────────────────────────────────

def relative_age(now_ms: int, then_ms: int) -> str:
    secs = max(0, int(now_ms - then_ms) // 1000)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def format_tokens(n: int) -> str:
    if n <= 0:
        return ""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        v = n / 1_000
        return f"{v:.1f}K" if v < 10 else f"{v:.0f}K"
    v = n / 1_000_000
    return f"{v:.1f}M" if v < 10 else f"{v:.0f}M"


def format_cost(c: float) -> str:
    return f"${c:.2f}" if c and c > 0 else ""


def task_name(path: str) -> str:
    """'/Users/me/proj/tasks/live-refresh.md' -> 'live-refresh'.

    The directories are the same for every run in a project and the extension
    is always the same, so showing either spends width on the one part of the
    string that carries no information. What distinguishes one delegation from
    another is the stem, and on an 80-column row that is all there is room for.
    """
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name


def run_title(run) -> tuple[str, bool]:
    """(title, is_path) for a run.

    A ledger run is named by the task file it was handed — reduced to that
    file's stem. A subagent has no file and is named by the first thing it was
    told, which is prose and truncates from the other end, so which kind this
    is has to travel with the string.
    """
    prompt = getattr(run, "prompt", "") or ""
    if prompt:
        return task_name(prompt), True
    return (getattr(run, "prompt_text", "") or "").strip(), False


# ── list view ───────────────────────────────────────────────────────────

def _run_rows(run, *, selected: bool, width: int, now_ms: int,
              spin_frame: int, expanded: bool) -> list[Line]:
    """The two lines one run occupies."""
    title, is_path = run_title(run)
    model = getattr(run, "model", "") or ""
    model_short = short_model(model)

    if run.live:
        marker = Span(SPINNER[spin_frame % len(SPINNER)], "live")
    elif getattr(run, "failed", False):
        # Marked, never hidden: the ledger is append-only so the record of
        # what was asked survives a run that died, and the web list already
        # flags these — the terminal list should not read cleaner than
        # reality.
        marker = Span("✗", "error")
    else:
        marker = Span("·", "idle")

    gutter = Span("▸ " if selected else "  ", "selected" if selected else "")

    # Title line: marker, title on the left, model on the right.
    model_w = min(len(model_short), max(0, width // 3))
    left_w = max(4, width - model_w - 8)
    # Both kinds now truncate from the end: a task name and a prose title both
    # say what they are in their opening words.
    title_line = truncate(line_of(Span(title or "(untitled)", "head")), left_w)

    head = line_of(gutter, marker, Span(" "), title_line)
    head = pad(head, width - model_w - 2, "selected" if selected else "")
    head = hstack(head,
                  line_of(Span(model_short, style_for_model(model))),
                  blank(2))

    # Meta line: age, tokens, cost — the detail that does not need to be read
    # on every row, indented under the title so the eye can skip it.
    parts: list[str] = [relative_age(now_ms, run.started) + " ago"]
    tokens = getattr(run, "tokens_in", 0) + getattr(run, "tokens_out", 0)
    if expanded:
        if tokens:
            parts.append(format_tokens(tokens) + " tok")
        cost = format_cost(getattr(run, "cost", 0.0))
        if cost:
            parts.append(cost)
        cwd = getattr(run, "cwd", "") or ""
        if cwd:
            parts.append(cwd.rsplit("/", 1)[-1])
    else:
        if tokens:
            parts.append(format_tokens(tokens) + " tok")
        cost = format_cost(getattr(run, "cost", 0.0))
        if cost:
            parts.append(cost)

    if getattr(run, "failed", False) and getattr(run, "end_reason", ""):
        parts.append(run.end_reason)

    meta = line_of(Span("     "), Span(" · ".join(parts), "age"))
    meta = pad(truncate(meta, width), width)
    return [head, meta]


def render_list(runs, selected: int, width: int, height: int, now_ms: int,
                *, scroll: int = 0, expanded: bool = False,
                spin_frame: int = 0, status: str = "") -> list[Line]:
    """The landing screen: one flat list, two rows per run."""
    lines: list[Line] = []

    live = sum(1 for r in runs if r.live)
    bits = [f"{len(runs)} run{'s' if len(runs) != 1 else ''}"]
    if live:
        bits.append(f"{live} live")
    if status:
        bits.append(status)

    header = line_of(Span("  delegate", "head"))
    right = line_of(Span(" · ".join(bits), "dim"))
    gap = max(1, width - width_of(header) - width_of(right) - 2)
    lines.append(hstack(header, blank(gap), right, blank(2)))
    lines.append([])

    body_h = max(1, height - 4)

    if not runs:
        lines.append(line_of(Span("  no runs yet — try: delegate run task.md",
                                  "dim")))
        while len(lines) < height - 2:
            lines.append([])
        lines.append(line_of(Span("  " + "─" * max(0, width - 4), "border")))
        lines.append(_footer(width, "list"))
        return lines

    rows: list[Line] = []
    for i, run in enumerate(runs):
        rows.extend(_run_rows(run, selected=(i == selected),
                              width=width - 2, now_ms=now_ms,
                              spin_frame=spin_frame, expanded=expanded))
        rows.append([])

    scroll = list_scroll_for(selected, scroll, height)

    bar = render_column(scroll, len(rows), body_h)
    visible = rows[scroll: scroll + body_h]
    for i in range(body_h):
        row = visible[i] if i < len(visible) else []
        row = pad(row, width - 1)
        lines.append(hstack(row, line_of(Span(bar[i] if i < len(bar) else " ",
                                              "bar.thumb"))))

    lines.append(line_of(Span("  " + "─" * max(0, width - 4), "border")))
    lines.append(_footer(width, "list"))
    return lines


def list_body_height(height: int) -> int:
    """Rows of the list actually shown, excluding header and footer."""
    return max(1, height - 4)


def list_scroll_for(selected: int, scroll: int, height: int) -> int:
    """Where the list is scrolled to, given the selection and previous scroll.

    Exported rather than kept private because the click handler needs the same
    answer. The renderer used to work this out internally while the loop kept
    its own copy that never changed, so once the list was scrolled a click
    landed on whatever run happened to be that many rows from the top — the
    right row on screen, the wrong run underneath it. One function, one answer.
    """
    per_item = ROWS_PER_RUN + 1
    view_h = list_body_height(height)
    item_top = selected * per_item
    if item_top < scroll:
        return item_top
    if item_top + per_item > scroll + view_h:
        return max(0, item_top + per_item - view_h)
    return max(0, scroll)


def _footer(width: int, view: str) -> Line:
    if view == "list":
        text = "  ↑↓ move   ↵ open   tab detail   r refresh   q quit"
    else:
        text = "  ↑↓ scroll   esc back   g/G top/bottom   q quit"
    return pad(line_of(Span(text, "dim")), width)


# ── conversation view ───────────────────────────────────────────────────

def session_blocks(session, *, platform: str = "", model: str = "",
                   is_subagent: bool = False) -> list[Line]:
    """A session as flat, styled lines — speaker headers and ruled bodies.

    Speakers are named by `speakers.exchange_header` rather than by their raw
    role, because "user" in a delegated transcript is not the person reading
    it. See that module for why that matters more than it looks.
    """
    model = model or getattr(session, "model", "") or ""
    platform = platform or getattr(session, "platform", "") or ""

    out: list[Line] = []
    last_speaker = None

    for ev in getattr(session, "events", []):
        if ev.kind == "text":
            who = exchange_header(ev.role, model=model, platform=platform,
                                  is_subagent=is_subagent)
            style = "user" if ev.role == "user" else "assistant"
            edge = "edge.user" if ev.role == "user" else "edge.assistant"
            if who != last_speaker:
                out.append([])
                out.append(line_of(Span("  "), Span(who, "speaker")))
                last_speaker = who
            body = ev.text.replace("\r\n", "\n").replace("\r", "\n")
            for para in body.split("\n"):
                out.append(line_of(Span("  "), Span(RULE, edge),
                                   Span(" "), Span(para, style)))

        elif ev.kind == "reasoning":
            text = ev.text[:200] + ("…" if len(ev.text) > 200 else "")
            for ln in text.split("\n"):
                out.append(line_of(Span("  "), Span(RULE, "reasoning"),
                                   Span(" "), Span(ln, "reasoning")))

        elif ev.kind == "tool_call":
            status = ev.tool_status or ""
            if status == "error":
                icon, istyle = "✗", "error"
            elif status == "pending":
                icon, istyle = "⋯", "dim"
            else:
                icon, istyle = "✓", "cost"
            ms = f"{ev.tool_ms}ms" if ev.tool_ms is not None else "pending"
            out.append(line_of(
                Span("  "), Span(RULE, "edge.tool"), Span(" "),
                Span(icon, istyle), Span(" "),
                Span(ev.tool_name or "tool", "tool"),
                Span(f"  {ms}", "age"),
            ))
            if ev.tool_output:
                shown = ev.tool_output.splitlines()[:8]
                for ln in shown:
                    out.append(line_of(Span("  "), Span(RULE, "edge.tool"),
                                       Span("   "), Span(ln, "dim")))
                extra = len(ev.tool_output.splitlines()) - len(shown)
                if extra > 0:
                    out.append(line_of(
                        Span("  "), Span(RULE, "edge.tool"),
                        Span(f"   … {extra} more lines", "dim")))
            last_speaker = None

        elif ev.kind == "patch":
            files = ", ".join(ev.raw.get("files", []) or [])
            out.append(line_of(Span("  "), Span(RULE, "edge.tool"),
                               Span(" ± "), Span(files or "patch", "tool")))
            last_speaker = None

        elif ev.kind == "compaction":
            out.append([])
            out.append(line_of(Span("  ── context compacted ──", "dim")))
            last_speaker = None

    return out


def split_rule_prefix(line: Line) -> tuple[Line, Line]:
    """Separate a leading `  ▎ ` rule from the content it marks.

    Wrapping has to be applied to the content alone. Wrap the whole line and
    the rule is treated as a word: continuation rows come back without it, the
    coloured edge turns into a dashed one, and a long paragraph stops looking
    like a single block — which is the entire job the rule was doing.
    """
    for i, s in enumerate(line):
        if s.text == RULE:
            return line[: i + 1], line[i + 1:]
    return [], line


def wrap_all(lines: list[Line], width: int) -> list[Line]:
    """Wrap every line to `width`, keeping the ruled prefix on continuations."""
    out: list[Line] = []
    for ln in lines:
        if not ln:
            out.append([])
            continue
        prefix, content = split_rule_prefix(ln)
        if not prefix:
            out.extend(wrap(ln, width, subsequent_indent="  "))
            continue
        pw = width_of(prefix)
        pieces = wrap(content, max(1, width - pw), subsequent_indent="  ")
        for piece in pieces:
            out.append(hstack(prefix, piece))
    return out


def wrap_body(body: list[Line], width: int) -> list[Line]:
    """Wrap a conversation to the width it will be shown at.

    Callers must wrap **before** doing scroll arithmetic, and pass the result
    to `render_convo`. Wrapping inside the renderer is what caused "it will
    not scroll to the bottom": the loop sized its maximum scroll from the
    unwrapped block count while the screen was showing wrapped lines, so the
    last screenful of a long conversation was unreachable. One line of prose
    becomes four on a narrow terminal, and any code that holds those two
    numbers apart will disagree with itself.
    """
    return wrap_all(body, max(1, width - CONVO_INSET))


def render_convo(title: str, wrapped: list[Line], scroll: int,
                 width: int, height: int) -> list[Line]:
    """Full-screen conversation. `wrapped` must come from `wrap_body`."""
    lines: list[Line] = []
    lines.append(pad(line_of(Span("  "), Span(title, "head")), width))
    lines.append([])

    body_h = convo_body_height(height)

    max_scroll = max(0, len(wrapped) - body_h)
    scroll = min(max(scroll, 0), max_scroll)

    bar = render_column(scroll, len(wrapped), body_h)
    visible = wrapped[scroll: scroll + body_h]
    for i in range(body_h):
        row = pad(visible[i] if i < len(visible) else [], width - 1)
        lines.append(hstack(row, line_of(
            Span(bar[i] if i < len(bar) else " ", "bar.thumb"))))

    lines.append(line_of(Span("  " + "─" * max(0, width - 4), "border")))
    lines.append(_footer(width, "convo"))
    return lines


def render_split(runs, selected: int, body: list[Line], title: str,
                 width: int, height: int, now_ms: int, *,
                 scroll: int = 0, list_scroll: int = 0, expanded: bool = False,
                 spin_frame: int = 0, status: str = "") -> list[Line]:
    """List on the left, conversation on the right."""
    list_w = max(34, width * 38 // 100)
    convo_w = width - list_w - 1

    left = render_list(runs, selected, list_w, height, now_ms,
                       scroll=list_scroll, expanded=expanded,
                       spin_frame=spin_frame, status=status)
    right = render_convo(title, body, scroll, convo_w, height)

    out: list[Line] = []
    for i in range(height):
        l_row = pad(left[i] if i < len(left) else [], list_w)
        r_row = pad(right[i] if i < len(right) else [], convo_w)
        out.append(hstack(l_row, line_of(Span("│", "border")), r_row))
    return out


def convo_body_height(height: int) -> int:
    """Rows of conversation actually shown — the scroll maths needs this and
    it must match render_convo exactly or dragging lands in the wrong place."""
    return max(1, height - 4)
