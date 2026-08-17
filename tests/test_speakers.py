"""Tests for speaker naming.

The one thing these guard is that a machine-written task brief is never
attributed to the human. Everything else here is cosmetic; that part is not.
"""

from __future__ import annotations

import unittest

from delegate_view.speakers import (
    delegator_name,
    exchange_header,
    short_model,
    speaker_for,
)


class ShortModelTests(unittest.TestCase):
    def test_strips_provider_prefix(self):
        self.assertEqual(short_model("opencode/big-pickle"), "big-pickle")
        self.assertEqual(short_model("anthropic/claude-opus-5"), "claude-opus-5")

    def test_bare_name_is_left_alone(self):
        self.assertEqual(short_model("big-pickle"), "big-pickle")

    def test_empty_stays_empty(self):
        self.assertEqual(short_model(""), "")


class DelegatorNameTests(unittest.TestCase):
    def test_ledger_run_was_sent_by_you(self):
        self.assertEqual(delegator_name("opencode"), "you")

    def test_subagent_was_sent_by_claude_not_you(self):
        # The point of the module: a subagent prompt is not something the
        # human typed, so it must not be attributed to them.
        self.assertEqual(delegator_name("claude-code", is_subagent=True),
                         "claude")
        self.assertNotEqual(delegator_name("claude-code", is_subagent=True),
                            "you")

    def test_unknown_platform_gets_a_neutral_name(self):
        self.assertEqual(delegator_name("gemini-cli"), "delegator")


class SpeakerForTests(unittest.TestCase):
    def test_assistant_turn_is_named_after_the_model(self):
        self.assertEqual(
            speaker_for("assistant", model="opencode/big-pickle",
                        platform="opencode"),
            "big-pickle",
        )

    def test_assistant_with_no_model_falls_back(self):
        self.assertEqual(
            speaker_for("assistant", model="", platform="opencode"), "agent")

    def test_user_turn_is_the_delegator_never_the_word_user(self):
        got = speaker_for("user", model="opencode/big-pickle",
                          platform="opencode")
        self.assertEqual(got, "you")
        self.assertNotEqual(got, "user")


class ExchangeHeaderTests(unittest.TestCase):
    def test_prompt_side_shows_direction(self):
        self.assertEqual(
            exchange_header("user", model="opencode/big-pickle",
                            platform="opencode"),
            "you → big-pickle",
        )

    def test_subagent_prompt_shows_claude_as_the_sender(self):
        self.assertEqual(
            exchange_header("user", model="claude-sonnet-5",
                            platform="claude-code", is_subagent=True),
            "claude → claude-sonnet-5",
        )

    def test_reply_side_is_just_the_model(self):
        self.assertEqual(
            exchange_header("assistant", model="opencode/big-pickle",
                            platform="opencode"),
            "big-pickle",
        )


if __name__ == "__main__":
    unittest.main()
