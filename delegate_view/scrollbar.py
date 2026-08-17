"""Scroll bar geometry, and the inverse mapping that makes it draggable.

The old TUI drew a scroll bar and stopped there. Drawing needs only the
forward map — given a scroll offset, which rows of the track are the thumb.
Dragging needs the *inverse*: given the row the pointer is on, what scroll
offset does that mean. Without it the bar is a picture of a control rather
than a control, which is what "I can't drag the scroll bar" comes down to.

Both directions live here as pure functions over ints so the interaction is
testable without a terminal, and so the drawing code and the hit-testing code
cannot drift apart — they are two halves of one mapping, and a UI where the
thumb lands somewhere other than where you dropped it is exactly what happens
when those halves are derived separately.
"""

from __future__ import annotations

from dataclasses import dataclass

THUMB = "█"  # █
TRACK = "│"  # │


@dataclass(frozen=True)
class Thumb:
    """Where the thumb sits within a track of `height` rows, 0-based."""

    start: int
    size: int

    @property
    def end(self) -> int:
        """One past the last row of the thumb."""
        return self.start + self.size

    def covers(self, row: int) -> bool:
        return self.start <= row < self.end


def max_scroll(total: int, height: int) -> int:
    """The largest useful scroll offset: past this you are scrolling blank."""
    return max(0, total - height)


def thumb_for(scroll: int, total: int, height: int) -> Thumb | None:
    """Thumb position for a scroll offset, or None when no bar is needed.

    Returns None when the content already fits, because a full-height thumb
    is just noise — there is nowhere to scroll to.
    """
    if height <= 0 or total <= height:
        return None

    # Proportional, but never smaller than one row: a thumb you cannot see is
    # a thumb you cannot grab, however long the content is.
    size = max(1, round(height * height / total))
    size = min(size, height)

    span = height - size
    limit = max_scroll(total, height)
    if span <= 0 or limit <= 0:
        return Thumb(start=0, size=size)

    scroll = min(max(scroll, 0), limit)
    start = round(span * scroll / limit)
    start = min(max(start, 0), span)
    return Thumb(start=start, size=size)


def scroll_for_thumb_top(top: int, total: int, height: int) -> int:
    """Inverse of `thumb_for`: the scroll offset that puts the thumb at `top`.

    This is what a drag calls on every motion event. Clamped at both ends so
    dragging past the track does not run the offset off into nowhere.
    """
    if height <= 0 or total <= height:
        return 0

    size = max(1, round(height * height / total))
    size = min(size, height)
    span = height - size
    limit = max_scroll(total, height)
    if span <= 0 or limit <= 0:
        return 0

    top = min(max(top, 0), span)
    return min(max(round(top * limit / span), 0), limit)


def scroll_for_click(row: int, total: int, height: int) -> int:
    """Scroll offset for a click at `row` on the track.

    The thumb centres on the pointer, which is what every scroll bar does and
    what makes a click on empty track feel like "take me roughly there"
    rather than a page-step of unclear size.
    """
    if height <= 0 or total <= height:
        return 0
    size = max(1, round(height * height / total))
    size = min(size, height)
    return scroll_for_thumb_top(row - size // 2, total, height)


def grab_offset(row: int, thumb: Thumb | None) -> int:
    """How far down the thumb the user grabbed it.

    Kept so a drag moves the thumb *relative* to where it was picked up. Snap
    the thumb's top to the pointer instead and the bar jumps under your cursor
    the instant you press, which reads as a bug even though the scrolling that
    follows is correct.
    """
    if thumb is None or not thumb.covers(row):
        return 0
    return row - thumb.start


def render_column(scroll: int, total: int, height: int) -> list[str]:
    """The bar as one character per row, top to bottom.

    Returns track characters when the content fits, so the column keeps its
    width and the layout does not shift the moment a conversation grows past
    one screen.
    """
    if height <= 0:
        return []
    thumb = thumb_for(scroll, total, height)
    if thumb is None:
        return [" "] * height
    return [THUMB if thumb.covers(r) else TRACK for r in range(height)]
