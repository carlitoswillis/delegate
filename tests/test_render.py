"""Tests for the span render layer.

The invariant under test throughout: **no operation loses a style**. That is
the one thing the string-with-markup renderer could not do, and every colour
bug it shipped was a consequence.
"""

from __future__ import annotations

import unittest

from delegate_view.render import (
    Span,
    blank,
    hstack,
    line_of,
    pad,
    slice_line,
    span,
    styles_of,
    text_of,
    truncate,
    truncate_left,
    width_of,
    wrap,
)
from delegate_view.theme import Theme, style_for_model


def L():
    """A multi-style sample line."""
    return [Span("hello ", "user"), Span("brave ", "tool"), Span("world", "err")]


class BasicsTests(unittest.TestCase):
    def test_width_matches_text_length(self):
        self.assertEqual(width_of(L()), len(text_of(L())))

    def test_line_of_drops_empty_spans(self):
        got = line_of(Span("a", "x"), Span("", "y"), "", "b")
        self.assertEqual(text_of(got), "ab")
        self.assertEqual(len(got), 2)

    def test_line_of_accepts_bare_strings(self):
        self.assertEqual(text_of(line_of("a", "b")), "ab")

    def test_blank_and_hstack(self):
        self.assertEqual(text_of(blank(3)), "   ")
        self.assertEqual(text_of(hstack(L(), blank(2), L())),
                         text_of(L()) + "  " + text_of(L()))


class SliceTests(unittest.TestCase):
    def test_slice_preserves_styles(self):
        got = slice_line(L(), 2, 9)
        self.assertEqual(text_of(got), "llo bra")
        self.assertEqual(styles_of(got), styles_of(L())[2:9])

    def test_empty_and_reversed_slices(self):
        self.assertEqual(slice_line(L(), 5, 5), [])
        self.assertEqual(slice_line(L(), 9, 2), [])


class TruncateTests(unittest.TestCase):
    def test_short_line_is_unchanged(self):
        self.assertEqual(text_of(truncate(L(), 100)), text_of(L()))

    def test_result_never_exceeds_width(self):
        for w in range(0, 20):
            self.assertLessEqual(width_of(truncate(L(), w)), w, w)

    def test_zero_and_one(self):
        self.assertEqual(truncate(L(), 0), [])
        self.assertEqual(width_of(truncate(L(), 1)), 1)
        self.assertEqual(text_of(truncate(L(), 1)), "…")

    def test_ellipsis_inherits_the_style_it_lands_in(self):
        got = truncate(L(), 5)          # cuts inside the "user" span
        self.assertEqual(got[-1].text, "…")
        self.assertEqual(got[-1].style, "user")

    def test_truncate_left_keeps_the_tail(self):
        path = [Span("/Users/x/very/long/path/", "dim"),
                Span("file.py", "head")]
        got = truncate_left(path, 12)
        self.assertLessEqual(width_of(got), 12)
        self.assertTrue(text_of(got).endswith("file.py"))
        self.assertTrue(text_of(got).startswith("…"))

    def test_truncate_left_short_line_unchanged(self):
        self.assertEqual(text_of(truncate_left(L(), 100)), text_of(L()))


class PadTests(unittest.TestCase):
    def test_pad_reaches_exact_width(self):
        self.assertEqual(width_of(pad(L(), 40)), 40)

    def test_padding_carries_the_requested_style(self):
        got = pad([Span("hi", "user")], 6, style="selected")
        self.assertEqual(got[-1].style, "selected")
        self.assertEqual(got[-1].text, "    ")

    def test_pad_clips_an_overwide_line_without_ellipsis(self):
        got = pad(L(), 5)
        self.assertEqual(width_of(got), 5)
        self.assertNotIn("…", text_of(got))

    def test_pad_zero(self):
        self.assertEqual(pad(L(), 0), [])


class WrapStylePreservationTests(unittest.TestCase):
    """The property the old renderer could not hold."""

    def test_every_surviving_character_keeps_its_style(self):
        line = [Span("alpha beta ", "user"),
                Span("gamma delta ", "tool"),
                Span("epsilon zeta", "err")]
        original = styles_of(line)
        out = wrap(line, 10)

        # Rebuild the (char, style) stream from the wrapped output and check it
        # is a subsequence of the input stream, in order. Characters lost at
        # wrap points are allowed; a character with a CHANGED style is not.
        produced = [p for ln in out for p in styles_of(ln)]
        i = 0
        for pair in produced:
            while i < len(original) and original[i] != pair:
                i += 1
            self.assertLess(i, len(original),
                            f"{pair!r} is not in the original stream in order")
            i += 1

    def test_no_wrapped_line_exceeds_width(self):
        line = [Span("some reasonably long text that needs wrapping", "user")]
        for w in (5, 8, 13, 20):
            for out in wrap(line, w):
                self.assertLessEqual(width_of(out), w, (w, text_of(out)))


class WrapBehaviourTests(unittest.TestCase):
    def test_leading_indentation_is_preserved(self):
        line = [Span("    indented code here", "tool")]
        out = wrap(line, 12)
        self.assertTrue(text_of(out[0]).startswith("    "))

    def test_subsequent_indent_applies_only_to_continuations(self):
        line = [Span("aaa bbb ccc ddd eee", "user")]
        out = wrap(line, 8, subsequent_indent="  ")
        self.assertFalse(text_of(out[0]).startswith("  "))
        for ln in out[1:]:
            self.assertTrue(text_of(ln).startswith("  "), text_of(ln))

    def test_long_word_is_hard_broken(self):
        line = [Span("x" * 50, "user")]
        out = wrap(line, 10)
        self.assertGreater(len(out), 1)
        for ln in out:
            self.assertLessEqual(width_of(ln), 10)
        self.assertEqual("".join(text_of(l) for l in out), "x" * 50)

    def test_interior_spacing_is_not_collapsed(self):
        line = [Span("col1      col2", "tool")]
        out = wrap(line, 40)
        self.assertEqual(text_of(out[0]), "col1      col2")

    def test_blank_and_whitespace_lines_survive(self):
        self.assertEqual(len(wrap([], 10)), 1)
        self.assertEqual(len(wrap([Span("   ", "dim")], 10)), 1)

    def test_degenerate_widths_terminate(self):
        line = [Span("hello world this is text", "user")]
        for w in (1, 0, -5):
            out = wrap(line, w)          # must not hang
            self.assertGreaterEqual(len(out), 1)

    def test_width_one_terminates_and_covers_the_text(self):
        out = wrap([Span("abc def", "user")], 1)
        self.assertGreaterEqual(len(out), 1)
        for ln in out:
            self.assertLessEqual(width_of(ln), 1)


class ThemeTests(unittest.TestCase):
    def test_colorless_theme_is_all_zero(self):
        t = Theme(color=False)
        t.start()
        for name in ("", "user", "live", "model.anthropic", "nonsense"):
            self.assertEqual(t.attr(name), 0, name)

    def test_unknown_style_does_not_raise(self):
        t = Theme(color=False)
        t.start()
        self.assertEqual(t.attr("no.such.style"), 0)

    def test_start_is_idempotent(self):
        t = Theme(color=False)
        t.start()
        t.start()
        self.assertEqual(t.attr("user"), 0)

    def test_attr_before_start_does_not_raise(self):
        self.assertEqual(Theme(color=False).attr("user"), 0)


class StyleForModelTests(unittest.TestCase):
    def test_known_families(self):
        self.assertEqual(style_for_model("claude-opus-5"), "model.anthropic")
        self.assertEqual(style_for_model("anthropic/claude-sonnet-5"),
                         "model.anthropic")
        self.assertEqual(style_for_model("gpt-4o"), "model.openai")
        self.assertEqual(style_for_model("o3-mini"), "model.openai")
        self.assertEqual(style_for_model("gemini-2.0"), "model.google")

    def test_unknown_and_empty_fall_back(self):
        self.assertEqual(style_for_model("big-pickle"), "model.other")
        self.assertEqual(style_for_model(""), "model.other")

    def test_case_insensitive(self):
        self.assertEqual(style_for_model("CLAUDE-OPUS"), "model.anthropic")


if __name__ == "__main__":
    unittest.main()
