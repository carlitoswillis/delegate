"""Tests for scroll bar geometry and its inverse.

The property that matters is the round trip: drawing and dragging must agree.
If they disagree the thumb lands somewhere other than where you dropped it,
which is the classic scroll-bar bug and is invisible to any test that only
checks the drawing half.
"""

from __future__ import annotations

import unittest

from delegate_view.scrollbar import (
    Thumb,
    grab_offset,
    max_scroll,
    render_column,
    scroll_for_click,
    scroll_for_thumb_top,
    thumb_for,
)


class NoBarNeededTests(unittest.TestCase):
    def test_content_that_fits_has_no_thumb(self):
        self.assertIsNone(thumb_for(0, total=10, height=10))
        self.assertIsNone(thumb_for(0, total=3, height=10))

    def test_degenerate_heights_do_not_raise(self):
        self.assertIsNone(thumb_for(0, total=100, height=0))
        self.assertIsNone(thumb_for(0, total=100, height=-5))
        self.assertEqual(render_column(0, total=100, height=0), [])

    def test_no_scrolling_possible_means_zero_offset(self):
        self.assertEqual(scroll_for_thumb_top(5, total=10, height=10), 0)
        self.assertEqual(scroll_for_click(5, total=10, height=10), 0)
        self.assertEqual(max_scroll(10, 10), 0)


class ThumbPositionTests(unittest.TestCase):
    def test_thumb_is_at_the_top_when_unscrolled(self):
        t = thumb_for(0, total=100, height=10)
        self.assertEqual(t.start, 0)

    def test_thumb_is_at_the_bottom_at_max_scroll(self):
        total, height = 100, 10
        t = thumb_for(max_scroll(total, height), total, height)
        self.assertEqual(t.end, height,
                         "thumb must reach the bottom of the track")

    def test_thumb_is_never_smaller_than_one_row(self):
        # A thumb you cannot see is a thumb you cannot grab.
        t = thumb_for(0, total=100_000, height=10)
        self.assertGreaterEqual(t.size, 1)

    def test_thumb_never_exceeds_the_track(self):
        for total in (11, 20, 100, 5000):
            for scroll in (0, 3, total):
                t = thumb_for(scroll, total, height=10)
                if t is None:
                    continue
                self.assertGreaterEqual(t.start, 0)
                self.assertLessEqual(t.end, 10, (total, scroll))

    def test_scrolling_down_never_moves_the_thumb_up(self):
        total, height = 250, 12
        last = -1
        for scroll in range(0, max_scroll(total, height) + 1):
            t = thumb_for(scroll, total, height)
            self.assertGreaterEqual(t.start, last)
            last = t.start

    def test_out_of_range_scroll_is_clamped_not_wrapped(self):
        total, height = 100, 10
        self.assertEqual(thumb_for(-50, total, height).start, 0)
        self.assertEqual(thumb_for(9999, total, height).end, height)


class RoundTripTests(unittest.TestCase):
    """Draw then drag: the two directions must agree."""

    def test_thumb_top_round_trips_back_to_the_same_scroll(self):
        total, height = 300, 15
        limit = max_scroll(total, height)
        for scroll in range(0, limit + 1, 7):
            top = thumb_for(scroll, total, height).start
            back = scroll_for_thumb_top(top, total, height)
            # Rounding to integer rows loses precision, so the requirement is
            # that it lands close, not that it is exact — but it must never
            # drift far enough to feel like the content jumped.
            rows_per_step = max(1, limit // max(1, height))
            self.assertLessEqual(abs(back - scroll), rows_per_step + 1,
                                 f"scroll={scroll} top={top} back={back}")

    def test_dragging_to_the_bottom_reaches_max_scroll(self):
        total, height = 500, 20
        self.assertEqual(scroll_for_thumb_top(999, total, height),
                         max_scroll(total, height))

    def test_dragging_above_the_top_reaches_zero(self):
        self.assertEqual(scroll_for_thumb_top(-999, total=500, height=20), 0)

    def test_drag_is_monotonic(self):
        total, height = 400, 16
        last = -1
        for top in range(0, height + 4):
            s = scroll_for_thumb_top(top, total, height)
            self.assertGreaterEqual(s, last)
            last = s


class ClickTests(unittest.TestCase):
    def test_click_centres_the_thumb_on_the_pointer(self):
        total, height = 400, 20
        row = 10
        scroll = scroll_for_click(row, total, height)
        t = thumb_for(scroll, total, height)
        centre = t.start + t.size // 2
        self.assertLessEqual(abs(centre - row), 1,
                             f"thumb centred at {centre}, clicked {row}")

    def test_click_at_the_very_bottom_goes_to_the_end(self):
        total, height = 400, 20
        self.assertEqual(scroll_for_click(height - 1, total, height),
                         max_scroll(total, height))


class GrabOffsetTests(unittest.TestCase):
    def test_offset_is_measured_from_the_thumb_top(self):
        t = Thumb(start=4, size=5)
        self.assertEqual(grab_offset(4, t), 0)
        self.assertEqual(grab_offset(6, t), 2)

    def test_grabbing_off_the_thumb_is_zero(self):
        t = Thumb(start=4, size=5)
        self.assertEqual(grab_offset(0, t), 0)
        self.assertEqual(grab_offset(99, t), 0)
        self.assertEqual(grab_offset(3, None), 0)

    def test_grab_then_drag_keeps_the_thumb_under_the_pointer(self):
        # Press in the middle of the thumb, move down 3 rows: the thumb should
        # follow by 3 rows, not snap its top to the pointer.
        total, height = 300, 15
        scroll = 60
        t = thumb_for(scroll, total, height)
        grab_row = t.start + t.size // 2
        off = grab_offset(grab_row, t)
        new_scroll = scroll_for_thumb_top(grab_row + 3 - off, total, height)
        moved = thumb_for(new_scroll, total, height)
        self.assertLessEqual(abs((moved.start - t.start) - 3), 1)


class RenderColumnTests(unittest.TestCase):
    def test_column_is_exactly_height_rows(self):
        for height in (1, 5, 40):
            self.assertEqual(len(render_column(0, 500, height)), height)

    def test_fitting_content_renders_blank_not_a_full_thumb(self):
        self.assertEqual(render_column(0, total=5, height=10), [" "] * 10)

    def test_thumb_rows_match_thumb_for(self):
        total, height, scroll = 200, 12, 40
        col = render_column(scroll, total, height)
        t = thumb_for(scroll, total, height)
        filled = [i for i, c in enumerate(col) if c != "│"]
        self.assertEqual(filled, list(range(t.start, t.end)))


if __name__ == "__main__":
    unittest.main()
