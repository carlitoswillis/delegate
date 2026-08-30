"""Keyboard and mouse decoding, as pure functions over raw key codes.

Two pieces of input refuse to work through curses on macOS and are decoded
here by hand instead.

1. **The scroll wheel.** macOS ships ncurses 6.0 (2015-08-08). That build
   defines BUTTON1 through BUTTON4 and no BUTTON5, so `curses.BUTTON5_PRESSED`
   raises AttributeError — which is precisely what crashed the old wheel
   handler on every scroll-down. Guarding the constant would not have been
   enough: on that same build `getmouse()` reports `bstate=0` for wheel events,
   so the wheel is invisible through the curses mouse API whichever way you
   ask. We turn on SGR mouse reporting ourselves and read the escape sequence,
   where wheel-up and wheel-down are ordinary button codes 64 and 65.

2. **Mouse motion.** The old code enabled `ESC[?1003h`, which asks the terminal
   to report every pointer *movement*. Moving the mouse across the window then
   floods `getch()` with hundreds of events per second, and those events
   compete with keystrokes in the same queue — the input lag was not the render
   loop, it was this.

   The fix is not to give up motion entirely, because dragging a scroll bar
   needs it. `?1002` is the middle setting: the terminal reports motion **only
   while a button is held**. Idle mouse movement stays silent, and a drag still
   arrives as a stream of motion reports. That is exactly the trade we want.

Everything below is a pure function or a small state machine over ints, so the
whole input path is testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Terminal mouse mode ─────────────────────────────────────────────────
#
# 1002 = report button press/release AND motion *while a button is held*.
#        Not 1000 (no motion at all, so scroll bars cannot be dragged) and
#        emphatically not 1003 (motion always, which floods the input queue).
# 1006 = SGR encoding: coordinates as decimal text rather than as bytes offset
#        by 32, which is what makes columns past 223 addressable at all.
MOUSE_ON = "\033[?1002h\033[?1006h"
MOUSE_OFF = "\033[?1006l\033[?1002l"

# SGR button codes. The low two bits are the button; bit 5 (32) marks a motion
# report, and bit 6 (64) marks the wheel. So wheel-up is 64, wheel-down 65,
# a left-button press 0, and dragging with the left button held is 0|32 = 32.
_WHEEL_UP = 64
_WHEEL_DOWN = 65
_MOTION = 32
_BUTTON_MASK = 0b11

ESC = 27


@dataclass(frozen=True)
class MouseEvent:
    """One decoded mouse report. Coordinates are 0-based screen cells."""

    button: int
    x: int
    y: int
    pressed: bool  # False for a release ('m' terminator)

    @property
    def is_wheel_up(self) -> bool:
        return self.button == _WHEEL_UP

    @property
    def is_wheel_down(self) -> bool:
        return self.button == _WHEEL_DOWN

    @property
    def is_wheel(self) -> bool:
        return self.button in (_WHEEL_UP, _WHEEL_DOWN)

    @property
    def is_motion(self) -> bool:
        """A drag report: the pointer moved with a button held down."""
        return not self.is_wheel and bool(self.button & _MOTION)

    @property
    def is_left(self) -> bool:
        """Left button, whether this is the initial press or a drag step."""
        return not self.is_wheel and (self.button & _BUTTON_MASK) == 0

    @property
    def is_press(self) -> bool:
        """The initial button-down, as opposed to a drag step or a release."""
        return self.pressed and not self.is_wheel and not self.is_motion


def parse_sgr_mouse(seq: str) -> MouseEvent | None:
    """Decode one SGR mouse report body, or None if it is not one.

    `seq` is the text between the `ESC[<` introducer and the final byte,
    with the final byte included: ``"0;12;34M"``. The terminator is ``M`` for
    a press and ``m`` for a release.

    Returns None rather than raising on anything malformed. A garbled mouse
    report should cost you one scroll tick, not the frame.
    """
    if not seq or seq[-1] not in ("M", "m"):
        return None
    pressed = seq[-1] == "M"
    body = seq[:-1]
    parts = body.split(";")
    if len(parts) != 3:
        return None
    try:
        button, col, row = (int(p) for p in parts)
    except ValueError:
        return None
    # SGR reports 1-based coordinates; the rest of the TUI is 0-based.
    return MouseEvent(button=button, x=max(0, col - 1), y=max(0, row - 1),
                      pressed=pressed)


class InputDecoder:
    """Turns the raw `getch()` byte stream into keys and mouse events.

    curses hands us one int at a time. A mouse report arrives as the multi-byte
    run ``ESC [ < 0 ; 1 2 ; 3 4 M``, so we buffer from the ESC until the
    terminating letter and decode the whole thing at once.

    `feed(code)` returns either None (byte consumed, nothing complete yet) or a
    ``(kind, value)`` pair where kind is ``"key"`` (value is the int keycode)
    or ``"mouse"`` (value is a MouseEvent). When one byte completes more than
    one event — an ESC that turned out not to introduce a mouse report, plus
    the key that followed it — the extras queue up and come out of the next
    `feed()` calls, so **no byte is ever dropped**. Dropping is the tempting
    shortcut here and it is wrong: the byte after a bare ESC is a real
    keystroke, and swallowing it makes Esc-then-anything feel dead.

    A partial sequence that never terminates would otherwise swallow every
    subsequent keystroke, so the buffer is capped and flushed back as a plain
    ESC when it overruns — a stray ESC is recoverable, a wedged input path is
    not.

    Two things here exist because of what ESC *means* upstream: in the list
    view it quits, and in a conversation it goes back. That makes ESC the most
    destructive byte in the stream, and both of the following were firing it
    when the user had not pressed it.

    * **An unknown CSI sequence is swallowed, not replayed.** curses with
      keypad(True) has already translated every escape sequence it recognises,
      so anything still arriving as ``ESC [ …`` is one curses has no name for:
      a focus-in/out report (``ESC[I`` / ``ESC[O``), a bracketed-paste marker
      (``ESC[200~``), a cursor-position reply. Terminals emit those unbidden.
      Replaying the bytes emitted a leading ESC and quit the app the moment
      the window took focus. CSI has a grammar — parameter and intermediate
      bytes, then one final byte in 0x40..0x7E — so we follow it to the end
      and emit nothing. Ignoring a sequence we cannot name is the only reading
      of it that cannot be wrong.

    * **A lone ESC is emitted on the first idle tick.** Held in the buffer
      waiting for a byte that never comes, it made the Esc key do nothing at
      all until the user pressed something else. `feed(-1)` — the caller's
      "getch timed out" signal — is the evidence that nothing followed, which
      is exactly the question ESCDELAY answers for curses' own decoder.
    """

    _MAX_SEQ = 32

    def __init__(self) -> None:
        self._buf: list[int] = []
        self._pending: list[tuple[str, object]] = []
        self._csi = False    # inside an unknown CSI we are swallowing whole

    def reset(self) -> None:
        self._buf = []
        self._pending = []
        self._csi = False

    def pending(self) -> int:
        """How many decoded events are queued behind the last return."""
        return len(self._pending)

    def _abandon(self, code: int | None, *, replay: bool) -> tuple[str, object]:
        """Give up on the buffered sequence and emit the ESC that started it.

        `replay` decides what happens to the bytes we had already eaten, and
        the two cases genuinely differ:

        * **ESC followed by something that is not a mouse introducer** — the
          user pressed Esc and then typed. Those bytes are real keystrokes and
          get replayed in order, because dropping them makes Esc-then-anything
          feel like a dead key.

        * **A body that overran 32 bytes** — no real SGR report is that long,
          so this is line noise or a truncated paste. Replaying thirty-two
          digit keys into the UI would be worse than silence, so it is dropped.
        """
        tail = self._buf[1:]
        self._buf = []
        self._csi = False
        if replay:
            for c in tail:
                self._pending.append(("key", c))
            if code is not None:
                self._pending.append(("key", code))
        return ("key", ESC)

    def feed(self, code: int) -> tuple[str, object] | None:
        # Anything already decoded comes out first, and a byte arriving while
        # the backlog drains goes to the back of it. Order in == order out.
        if self._pending:
            if code >= 0:
                self._pending.append(("key", code))
            return self._pending.pop(0)

        if code < 0:
            return self._idle()

        if not self._buf:
            if code == ESC:
                self._buf = [code]
                return None
            return ("key", code)

        # Swallowing a CSI curses could not name: run to its final byte.
        if self._csi:
            self._buf.append(code)
            if 0x40 <= code <= 0x7E:
                self._buf = []
                self._csi = False
            elif len(self._buf) > self._MAX_SEQ:
                self._buf = []
                self._csi = False
            return None

        # Still deciding whether this is a mouse report at all.
        if len(self._buf) == 1:
            if code != ord("["):
                return self._abandon(code, replay=True)
            self._buf.append(code)
            return None
        if len(self._buf) == 2:
            if code != ord("<"):
                # Not a mouse report, but still a CSI. Swallow it whole rather
                # than replaying an ESC that would quit the app.
                self._csi = True
                self._buf.append(code)
                if 0x40 <= code <= 0x7E:
                    self._buf = []
                    self._csi = False
                return None
            self._buf.append(code)
            return None

        # Inside the body: digits and semicolons until M or m.
        if code in (ord("M"), ord("m")):
            seq = "".join(chr(c) for c in self._buf[3:]) + chr(code)
            self._buf = []
            ev = parse_sgr_mouse(seq)
            return ("mouse", ev) if ev is not None else None

        if not (ord("0") <= code <= ord("9") or code == ord(";")):
            return self._abandon(code, replay=True)

        self._buf.append(code)
        if len(self._buf) > self._MAX_SEQ:
            return self._abandon(None, replay=False)

        return None

    def _idle(self) -> tuple[str, object] | None:
        """Nothing arrived this tick. Resolve whatever is still buffered.

        A lone ESC is the Esc key and comes out now; anything longer is a
        sequence that was cut off mid-flight and is dropped, because emitting
        its head would mean emitting an ESC the user never pressed.
        """
        if not self._buf:
            return None
        if self._buf == [ESC]:
            self._buf = []
            return ("key", ESC)
        self._buf = []
        self._csi = False
        return None
