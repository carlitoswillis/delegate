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

import unicodedata
from dataclasses import dataclass

ELLIPSIS = "…"

# Characters that occupy no cell at all: combining marks stack on the previous
# glyph, and the format characters (ZWJ, ZWNJ, the bidi controls, the BOM) are
# instructions to the shaper rather than things to draw.
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u200e\u200f\ufeff")

# What a control character is drawn as. Transcripts carry raw ANSI colour
# codes, NULs and BELs, and curses renders those in caret notation — two cells
# for one character, which is exactly the kind of silent disagreement between
# "how long is this string" and "how much screen does it take" that the rest of
# this module exists to avoid. One character in, one cell out, always.
_CONTROL_GLYPH = "·"
_CONTROL_MAP = {c: _CONTROL_GLYPH for c in range(0x20)}
_CONTROL_MAP[0x7F] = _CONTROL_GLYPH
_CONTROL_MAP[0x9B] = _CONTROL_GLYPH


def char_width(ch: str) -> int:
    """Cells one character occupies on a terminal: 0, 1 or 2.

    Everything above measures text in *cells*, not in characters, and the two
    are the same number only for the ASCII the layout was originally written
    against. A CJK or emoji transcript makes them differ by a factor of two,
    and every consequence of that is a rendering bug: `pad` stops filling the
    row, `truncate` clips at the wrong column, `wrap` breaks a line long after
    it has run off the edge, and `paint` writes past the last column so curses
    wraps the overflow onto the row below and eats it.

    East Asian "Ambiguous" is counted as one cell. Every glyph this UI draws
    for itself — the rule, the box lines, the thumb, the ellipsis — is
    Ambiguous, and they are all one cell wide in the terminals people run this
    in. Counting them as two would corrupt the layout in the ordinary case to
    be right in the rare one.
    """
    if not ch:
        return 0
    o = ord(ch)
    if o < 0x20 or o == 0x7F:
        return 1                       # drawn as _CONTROL_GLYPH
    if o < 0x7F:
        return 1                       # the ASCII fast path
    if ch in _ZERO_WIDTH or unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: str) -> int:
    """Cells a string occupies. The ASCII case stays a plain len()."""
    if text.isascii():
        return len(text)
    return sum(char_width(c) for c in text)


def fit(text: str, cells: int, start: int = 0) -> int:
    """Index one past the last character of `text[start:]` fitting in `cells`.

    Never splits a double-width character: a half-drawn glyph is not a cheaper
    failure than a missing one, and curses would refuse to draw it anyway.
    """
    if cells <= 0:
        return start
    used = 0
    i = start
    n = len(text)
    while i < n:
        w = char_width(text[i])
        if used + w > cells:
            break
        used += w
        i += 1
    return i


def printable(text: str) -> str:
    """Swap control characters for a placeholder, one character for one.

    Applied at paint time only. Doing it here rather than at the source keeps
    every length already computed upstream valid, because the substitution
    preserves both the character count and the cell count.
    """
    return text.translate(_CONTROL_MAP)


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
    """Cells the line occupies — not characters. See `char_width`."""
    return sum(cell_width(s.text) for s in line)


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

    keep = width - cell_width(ellipsis)
    out: Line = []
    used = 0
    last_style = ""
    for s in line:
        if used >= keep:
            break
        chunk = s.text[: fit(s.text, keep - used)]
        if chunk:
            out.append(Span(chunk, s.style))
            used += cell_width(chunk)
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

    keep = width - cell_width(ellipsis)
    drop = total - keep
    out: Line = [Span(ellipsis, line[0].style if line else "")]
    seen = 0
    for s in line:
        w = cell_width(s.text)
        if seen + w <= drop:
            seen += w
            continue
        # `fit` from the front tells us how many characters make up the cells
        # being dropped, which is the only way to cut a mixed-width span at a
        # cell boundary without landing inside a glyph. When the boundary lands
        # mid-glyph we drop that glyph too: one cell of slack is invisible,
        # one cell of overflow is not.
        want = max(0, drop - seen)
        cut = fit(s.text, want)
        if cut < len(s.text) and cell_width(s.text[:cut]) < want:
            cut += 1
        chunk = s.text[cut:]
        seen += w
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
            chunk = s.text[: fit(s.text, width - used)]
            if chunk:
                out.append(Span(chunk, s.style))
                used += cell_width(chunk)
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

        # `limit` is a character index but the budget is in cells, and `fit`
        # is the conversion. Counting characters here is what let a line of
        # CJK run to twice the width of the column it was wrapped for.
        if first:
            # The indent is part of the text, so it is already accounted for.
            seg_start = pos
            limit = fit(text, width, pos)
        else:
            seg_start = pos
            limit = fit(text, avail, pos)

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
            # A single character wider than the whole column. Emit it alone
            # rather than looping forever on a window nothing fits into.
            chunk_end = min(n, seg_start + 1)

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
        # Clip by cells, not by characters. A span of CJK or emoji clipped by
        # character count is twice as wide as the room left, and curses does
        # not stop at the margin — it wraps the overflow onto the next row and
        # then the next row's own text overwrites part of it, which is what
        # turned an emoji-heavy transcript into shredded columns.
        chunk = printable(s.text[: fit(s.text, max_x - col)])
        if not chunk:
            continue
        attr = 0
        try:
            attr = theme.attr(s.style)
        except Exception:
            attr = 0
        try:
            win.addstr(y, col, chunk, attr)
        except (curses.error, ValueError, UnicodeError):
            # Expected on the bottom-right cell; the text is drawn regardless.
            # ValueError is the embedded-NUL case and UnicodeError a character
            # the terminal's encoding cannot carry — neither is worth losing
            # the rest of the frame over.
            pass
        col += cell_width(chunk)
