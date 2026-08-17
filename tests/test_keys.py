"""Tests for the hand-rolled mouse/key decoding.

The whole point of delegate_view.keys is that the input path is decodable
without a terminal, so none of these touch curses.
"""

from __future__ import annotations

import unittest

from delegate_view.keys import (
    ESC,
    InputDecoder,
    MouseEvent,
    parse_sgr_mouse,
)


def _feed_str(dec: InputDecoder, text: str) -> list:
    """Feed every char of `text` and collect whatever the decoder emits."""
    out = []
    for ch in text:
        got = dec.feed(ord(ch))
        if got is not None:
            out.append(got)
    return out


class ParseSgrMouseTests(unittest.TestCase):
    def test_wheel_up_and_down(self):
        up = parse_sgr_mouse("64;10;5M")
        self.assertIsNotNone(up)
        self.assertTrue(up.is_wheel_up)
        self.assertFalse(up.is_wheel_down)
        self.assertTrue(up.is_wheel)

        down = parse_sgr_mouse("65;10;5M")
        self.assertTrue(down.is_wheel_down)
        self.assertFalse(down.is_wheel_up)

    def test_coordinates_are_zero_based(self):
        # SGR reports 1-based; the TUI is 0-based.
        ev = parse_sgr_mouse("0;1;1M")
        self.assertEqual((ev.x, ev.y), (0, 0))
        ev = parse_sgr_mouse("0;12;34M")
        self.assertEqual((ev.x, ev.y), (11, 33))

    def test_press_versus_release(self):
        self.assertTrue(parse_sgr_mouse("0;5;5M").pressed)
        self.assertFalse(parse_sgr_mouse("0;5;5m").pressed)

    def test_malformed_returns_none_never_raises(self):
        for bad in ("", "M", "m", "0;5M", "0;5;5", "a;b;cM",
                    "0;5;5;7M", ";;M", "0;;5M"):
            self.assertIsNone(parse_sgr_mouse(bad), bad)

    def test_negative_coordinates_clamp_to_zero(self):
        ev = parse_sgr_mouse("0;0;0M")
        self.assertEqual((ev.x, ev.y), (0, 0))


class InputDecoderTests(unittest.TestCase):
    def test_plain_keys_pass_straight_through(self):
        dec = InputDecoder()
        self.assertEqual(dec.feed(ord("j")), ("key", ord("j")))
        self.assertEqual(dec.feed(ord("k")), ("key", ord("k")))

    def test_negative_getch_is_ignored(self):
        # getch() returns -1 on timeout; it must not disturb a partial buffer.
        dec = InputDecoder()
        self.assertIsNone(dec.feed(-1))

    def test_full_mouse_sequence_decodes(self):
        dec = InputDecoder()
        got = _feed_str(dec, "\033[<65;10;5M")
        self.assertEqual(len(got), 1)
        kind, ev = got[0]
        self.assertEqual(kind, "mouse")
        self.assertTrue(ev.is_wheel_down)
        self.assertEqual((ev.x, ev.y), (9, 4))

    def test_keys_after_a_mouse_sequence_still_work(self):
        # The regression that matters: a mouse report must not swallow the
        # keystrokes that follow it.
        dec = InputDecoder()
        got = _feed_str(dec, "\033[<64;1;1Mq")
        self.assertEqual(got[0][0], "mouse")
        self.assertEqual(got[-1], ("key", ord("q")))

    def test_two_mouse_sequences_back_to_back(self):
        dec = InputDecoder()
        got = _feed_str(dec, "\033[<65;1;1M\033[<65;1;2M")
        self.assertEqual([k for k, _ in got], ["mouse", "mouse"])
        self.assertEqual(got[1][1].y, 1)

    def test_bare_escape_then_key_yields_both_in_order(self):
        # Esc means "back"/"quit" in the TUI, so it has to survive — and so
        # does the key typed right after it. Dropping the follow-on byte is
        # what makes Esc-then-anything feel like a dead key.
        dec = InputDecoder()
        got = _feed_str(dec, "\033q")
        got += [x for x in (dec.feed(-1),) if x is not None]
        self.assertEqual(got, [("key", ESC), ("key", ord("q"))])

    def test_non_mouse_escape_sequence_replays_its_bytes(self):
        dec = InputDecoder()
        got = _feed_str(dec, "\033[A")     # a bare arrow-key sequence
        while (x := dec.feed(-1)) is not None:
            got.append(x)
        self.assertEqual(got, [("key", ESC), ("key", ord("[")), ("key", ord("A"))])
        self.assertEqual(dec.feed(ord("j")), ("key", ord("j")))

    def test_runaway_sequence_is_dropped_not_replayed(self):
        # A 32+ byte "mouse body" is line noise, not typing. Replaying it
        # would fire dozens of phantom keypresses into the UI.
        dec = InputDecoder()
        # Feed exactly enough to trip the 32-byte cap and no more, so the only
        # thing that can come out is whatever the abandon path emits.
        got = _feed_str(dec, "\033[<" + "1" * 30)
        while (x := dec.feed(-1)) is not None:
            got.append(x)
        self.assertEqual(got, [("key", ESC)],
                         "buffered noise should be dropped, not replayed")
        self.assertEqual(dec.feed(ord("q")), ("key", ord("q")))

    def test_reset_clears_a_partial_sequence(self):
        dec = InputDecoder()
        _feed_str(dec, "\033[<65;1")
        dec.reset()
        self.assertEqual(dec.feed(ord("j")), ("key", ord("j")))


if __name__ == "__main__":
    unittest.main()
