"""Styled text as data: spans in, spans out.

The previous renderer carried style inside the string, as `"<user>hi</user>"`,
and stripped the markers at paint time. Every operation on such a string loses
the thing it is carrying, and all three of the bugs that shipped were the same
bug wearing different clothes:

* The close tag never closed. The pattern was `</?(\\w+)>` and the code then
  asked `if tag.startswith("/")` — but the slash sits *outside* the capture
  group, so `</user>` captured `"user"` and pushed another style. The stack
  only grew, and colour bled forward down the screen.
* Only one cell per span was coloured, because the paint loop passed a literal
  `1` as the length to `chgat`.
* Wrapping called `_strip_tags` and then `.split()`, which threw away every
  style and collapsed runs of spaces, so indented code came back flat and grey.

The fix is not a better parser. It is to stop encoding style in the text at
all. A `Line` is a list of `Span`, each a run of characters with one style
name. Slicing, padding and wrapping move spans around; they cannot lose a
style, because the style is not written in the thing being cut.

Everything here is pure except `paint`, which is the single point of contact
with curses and imports it lazily so this module stays importable with no
terminal attached.
"""

from __future__ import annotations

from dataclasses import dataclass

ELLIPSIS = "…"


@dataclass(frozen=True)
class Span:
    """A run of characters sharing one style.

    `text` is plain text and never contains markup — that invariant is the
    whole design. `style` is a semantic name resolved by theme.Theme, or ""
    for the terminal default.
    """

    text: str
    style: str = ""


Line = list[Span]


def span(text: str, style: str = "") -> Span:
    return Span(text, style)


def line_of(*parts) -> Line:
    """Build a Line from Spans and bare strings.

    Empty spans are dropped. They are invisible but they make every downstream
    length assertion ambiguous, and they turn "is this line blank?" into a
    question with two different answers.
    """
    out: Line = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, Span):
            if p.text:
                out.append(p)
        elif isinstance(p, (list, tuple)):
            for sub in p:
                out.extend(line_of(sub))
        elif p:
            out.append(Span(str(p)))
    return out


def text_of(line: Line) -> str:
    return "".join(s.text for s in line)


def width_of(line: Line) -> int:
    return sum(len(s.text) for s in line)


def styles_of(line: Line) -> list[tuple[str, str]]:
    """Flatten to one (char, style) pair per character.

    Only used by tests, but it is the honest way to state the invariant this
    module exists to keep, so it lives with the code rather than beside it.
    """
    out: list[tuple[str, str]] = []
    for s in line:
        for ch in s.text:
            out.append((ch, s.style))
    return out


def truncate(line: Line, width: int, ellipsis: str = ELLIPSIS) -> Line:
    """Clip to `width` characters, marking the cut with an ellipsis.

    The ellipsis inherits the style of the span it lands in, so a truncated
    coloured field does not end in a stray default-coloured character.
    """
    if width <= 0:
        return []
    if width_of(line) <= width:
        return list(line)
    if width == 1:
        first = line[0].style if line else ""
        return [Span(ellipsis, first)]

    keep = width - len(ellipsis)
    out: Line = []
    used = 0
    last_style = ""
    for s in line:
        if used >= keep:
            break
        room = keep - used
        chunk = s.text[:room]
        if chunk:
            out.append(Span(chunk, s.style))
            used += len(chunk)
            last_style = s.style
    out.append(Span(ellipsis, last_style))
    return out


def truncate_left(line: Line, width: int, ellipsis: str = ELLIPSIS) -> Line:
    """Clip from the front, keeping the tail — for paths, where the filename
    is the part worth seeing."""
    if width <= 0:
        return []
    total = width_of(line)
    if total <= width:
        return list(line)
    if width == 1:
        return [Span(ellipsis, line[0].style if line else "")]

    keep = width - len(ellipsis)
    drop = total - keep
    out: Line = [Span(ellipsis, line[0].style if line else "")]
    seen = 0
    for s in line:
        if seen + len(s.text) <= drop:
            seen += len(s.text)
            continue
        start = max(0, drop - seen)
        chunk = s.text[start:]
        seen += len(s.text)
        if chunk:
            out.append(Span(chunk, s.style))
    return out


def pad(line: Line, width: int, style: str = "") -> Line:
    """Extend with spaces to exactly `width`, or hard-clip if already wider.

    `style` is applied to the padding, which matters for a selected row: the
    highlight has to run the full width of the screen, and it only does that
    if the trailing blanks carry it too.
    """
    if width <= 0:
        return []
    cur = width_of(line)
    if cur == width:
        return list(line)
    if cur > width:
        # Layout clipping, not display shortening — no ellipsis here.
        out: Line = []
        used = 0
        for s in line:
            if used >= width:
                break
            chunk = s.text[: width - used]
            if chunk:
                out.append(Span(chunk, s.style))
                used += len(chunk)
        return out
    return list(line) + [Span(" " * (width - cur), style)]


def blank(width: int, style: str = "") -> Line:
    return [Span(" " * width, style)] if width > 0 else []


def hstack(*parts: Line) -> Line:
    """Concatenate lines into one row. How the split view joins its columns."""
    out: Line = []
    for p in parts:
        out.extend(s for s in p if s.text)
    return out


def slice_line(line: Line, start: int, end: int) -> Line:
    """Character slice that keeps styles — the primitive wrapping is built on."""
    if end <= start:
        return []
    out: Line = []
    pos = 0
    for s in line:
        s_end = pos + len(s.text)
        if s_end > start and pos < end:
            chunk = s.text[max(0, start - pos): max(0, end - pos)]
            if chunk:
                out.append(Span(chunk, s.style))
        pos = s_end
        if pos >= end:
            break
    return out


def _leading_space(text: str) -> str:
    return text[: len(text) - len(text.lstrip(" \t"))]


def wrap(line: Line, width: int, subsequent_indent: str = "") -> list[Line]:
    """Word-wrap, preserving styles, indentation and interior spacing.

    Three things this does that the old wrapper did not:

    * Styles survive a break. A span split across the boundary becomes two
      spans with the same style.
    * Leading whitespace is kept, and re-applied to continuation lines when
      the caller asks. Transcripts are full of indented code and flattening it
      loses the only structure that code has on a terminal.
    * Runs of interior spaces are kept, because `.split()` on a line of a diff
      or a table destroys the alignment that makes it readable.

    A word longer than `width` is hard-broken rather than allowed to overflow.
    """
    if width <= 0:
        return [list(line)]

    text = text_of(line)
    if not text:
        return [list(line)]
    if not text.strip():
        return [list(line)]

    indent = _leading_space(text)
    if len(indent) >= width:
        indent = ""

    out: list[Line] = []
    pos = 0
    n = len(text)
    first = True

    while pos < n:
        cur_indent = indent if first else subsequent_indent
        if len(cur_indent) >= width:
            cur_indent = ""
        avail = width - len(cur_indent)
        if avail <= 0:
            avail = width

        if first:
            # The indent is part of the text, so it is already accounted for.
            seg_start = pos
            limit = pos + width
        else:
            seg_start = pos
            limit = pos + avail

        if limit >= n:
            chunk_end = n
        else:
            # Prefer to break at the last space inside the window — but never
            # inside the leading indentation. Breaking there produces a first
            # line of pure whitespace and shunts the text down a row, which is
            # what "preserve indentation" must not mean.
            window = text[seg_start:limit]
            floor = len(indent) if first else 0
            brk = window.rfind(" ", floor)
            if brk <= floor:
                chunk_end = limit          # hard break: no usable space
            else:
                chunk_end = seg_start + brk

        if chunk_end <= seg_start:
            chunk_end = min(n, seg_start + max(1, avail))

        piece = slice_line(line, seg_start, chunk_end)
        if first:
            out.append(piece)
        else:
            out.append(line_of(Span(cur_indent), *piece) if cur_indent else piece)

        pos = chunk_end
        # Swallow the single space we broke on, but not a whole run of them.
        if pos < n and text[pos] == " ":
            pos += 1
        first = False

    return out or [list(line)]


def paint(win, y: int, x: int, line: Line, theme) -> None:
    """Write one Line to a curses window, one addstr per span.

    The whole span gets its attribute in a single call — the old code passed a
    length of 1 to `chgat` and coloured exactly one cell per style change.

    Writing the very last cell of a window raises `curses.error` even though
    the character lands. The old code dodged that by never painting the bottom
    row at all, which is why the status bar was invisible. Here the error is
    caught and ignored so the last row still paints.
    """
    import curses

    try:
        max_y, max_x = win.getmaxyx()
    except curses.error:
        return
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return

    col = x
    for s in line:
        if col >= max_x:
            break
        chunk = s.text[: max_x - col]
        if not chunk:
            continue
        attr = 0
        try:
            attr = theme.attr(s.style)
        except Exception:
            attr = 0
        try:
            win.addstr(y, col, chunk, attr)
        except curses.error:
            # Expected on the bottom-right cell; the text is drawn regardless.
            pass
        col += len(chunk)
