"""Semantic style names to curses attributes.

Callers name a *role* — "model.anthropic", "live", "border" — never a colour.
That indirection is what lets the palette move in one place instead of being
chased through render code, and it is why the style strings in render.Span are
worth keeping symbolic rather than storing attributes directly.

A style name that is not in the table resolves to 0 rather than raising. A typo
should cost you a colour, not a frame: this runs inside a repaint loop where an
exception means the screen stops updating, and a grey word is a far cheaper
failure than a frozen TUI.

`Theme(color=False)` resolves everything to 0 and touches curses not at all,
which is what makes the render layer testable in a process with no terminal.
"""

from __future__ import annotations

# Style name -> (color number or None, attribute name or None)
#
# Colours carried over from the original `_curses_main` init_pair block so the
# look does not regress. The numbers above 16 are xterm-256 indices.
_PALETTE: dict[str, tuple[int | None, str | None]] = {
    "": (None, None),
    "default": (None, None),

    # Chrome
    "head": (None, "bold"),
    "dim": (241, None),
    "border": (238, None),
    "bar": (241, None),
    "bar.thumb": (245, None),

    # List rows
    "selected": (None, "reverse"),
    "selected.dim": (None, "reverse"),
    "live": (51, "bold"),
    "idle": (241, None),
    "age": (243, None),
    "cost": (34, None),
    "tokens": (38, None),

    # Conversation roles
    "user": (34, None),
    "assistant": (75, None),
    "tool": (170, None),
    "reasoning": (241, None),
    "error": (203, None),
    "speaker": (None, "bold"),

    # Role edges — the coloured left rule in the flat layout
    "edge.user": (34, None),
    "edge.assistant": (75, None),
    "edge.tool": (170, None),

    # Models
    "model.anthropic": (209, None),
    "model.openai": (34, None),
    "model.google": (75, None),
    "model.other": (221, None),
}

# Substring -> model style. Ordered longest-first at lookup so "o1" does not
# shadow a longer match.
_MODEL_MATCHES = (
    ("claude", "model.anthropic"),
    ("opus", "model.anthropic"),
    ("sonnet", "model.anthropic"),
    ("haiku", "model.anthropic"),
    ("gpt", "model.openai"),
    ("o1", "model.openai"),
    ("o3", "model.openai"),
    ("o4", "model.openai"),
    ("gemini", "model.google"),
)


def style_for_model(model: str) -> str:
    """Which model.* style a model string gets. Case-insensitive."""
    m = (model or "").lower()
    for substr, style in _MODEL_MATCHES:
        if substr in m:
            return style
    return "model.other"


class Theme:
    """Resolves style names to curses attributes.

    `start()` allocates the colour pairs. It is separate from __init__ so a
    Theme can be constructed before curses is initialised — and so tests can
    construct one that never calls curses at all.
    """

    def __init__(self, *, color: bool = True) -> None:
        self._color = color
        self._attrs: dict[str, int] = {}
        self._started = False

    def start(self) -> None:
        """Allocate colour pairs. Idempotent, and safe without colour support."""
        if self._started or not self._color:
            self._started = True
            return
        self._started = True

        try:
            import curses
        except Exception:
            self._color = False
            return

        try:
            if not curses.has_colors():
                self._color = False
                return
            curses.start_color()
            curses.use_default_colors()
        except Exception:
            self._color = False
            return

        pair = 1
        for name, (color, attr_name) in _PALETTE.items():
            value = 0
            if color is not None:
                try:
                    curses.init_pair(pair, color, -1)
                    value = curses.color_pair(pair)
                    pair += 1
                except Exception:
                    value = 0
            if attr_name:
                value |= getattr(curses, "A_" + attr_name.upper(), 0)
            self._attrs[name] = value

    def attr(self, style: str) -> int:
        """Curses attribute for a style name. Unknown names resolve to 0."""
        if not self._color or not style:
            return 0
        return self._attrs.get(style, 0)
