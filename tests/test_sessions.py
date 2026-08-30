"""Tests for delegate_view.sessions — the unified, de-duplicated run list.

Every source is faked. Nothing here reads ~/.delegate, ~/.claude or the
opencode database.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate_view.runs import RESOLVE_WINDOW_MS, Run
from delegate_view.schema import Session
from delegate_view.sessions import SOURCES, all_runs

# Epoch millis, so timestamps are spaced the way real ones are. A session
# created within RESOLVE_WINDOW_MS of a ledger line is that line's session;
# FAR is comfortably outside it, so those pairings stay unrelated.
T = 1_700_000_000_000
FAR = RESOLVE_WINDOW_MS * 10


def _session(sid, platform="opencode", *, created=T, updated=T + 2000,
             cwd="/proj", title="t", parent=None, tokens_in=0, tokens_out=0,
             cost=0.0, path="") -> Session:
    return Session(id=sid, platform=platform, title=title, cwd=cwd,
                   model="prov/model", parent_id=parent, created=created,
                   updated=updated, tokens_in=tokens_in, tokens_out=tokens_out,
                   cost=cost, path=path)


def _write_ledger(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class _Base(unittest.TestCase):
    """Runs all_runs() against fake adapters."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def _ledger(self, entries: list[dict]) -> Path:
        path = self._tmp / "runs.jsonl"
        _write_ledger(path, entries)
        return path

    def _entry(self, started=T, name="task", cwd="/proj", **kw):
        prompt = self._tmp / f"{name}.md"
        prompt.write_text("do the thing\n")
        log = self._tmp / f"{name}-transcript.log"
        log.write_text("\n===== REPLY =====\n\nsure\n"
                       "\n===== END 2026-08-17 21:51:30 — completed =====\n")
        d = {"started": started, "prompt": str(prompt), "transcript": str(log),
             "model": "opencode/big-pickle", "cwd": cwd}
        d.update(kw)
        return d

    def _run(self, *, ledger=None, opencode=(), claude_code=(), **kw):
        """all_runs() with each adapter's list_sessions faked."""
        def fake_oc(db_path=None, cwd=None):
            if cwd is None:
                return list(opencode)
            return [s for s in opencode if s.cwd == cwd]

        def fake_cc(db_path=None, cwd=None, limit=None, subagents_only=False):
            out = list(claude_code)
            if cwd is not None:
                out = [s for s in out if s.cwd == cwd]
            if limit is not None:
                out = out[:limit]
            return out

        with patch("delegate_view.adapters.opencode.list_sessions", fake_oc), \
             patch("delegate_view.adapters.claude_code.list_sessions", fake_cc):
            return all_runs(ledger_path=ledger, **kw)


class TestEverySourceAppears(_Base):
    """The list is no longer ledger + subagents; it is every conversation."""

    def test_all_three_sources(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        runs = self._run(
            ledger=ledger,
            opencode=[_session("oc-1", created=T + FAR, updated=T + FAR)],
            claude_code=[_session("cc-1", "claude-code", created=T + 2 * FAR,
                                  updated=T + 2 * FAR)],
        )
        self.assertEqual({r.source for r in runs},
                         {"ledger", "opencode", "claude-code"})
        self.assertEqual(len(runs), 3)

    def test_direct_opencode_chat_is_not_dropped(self):
        """The reported bug: chats started outside delegate.sh were invisible."""
        ledger = self._ledger([])
        runs = self._run(
            ledger=ledger,
            opencode=[_session(f"oc-{i}", created=T + FAR * i,
                                updated=T + FAR * i) for i in range(1, 11)],
        )
        self.assertEqual(len(runs), 10)

    def test_sources_constant_matches_what_is_loaded(self):
        self.assertEqual(SOURCES, ("ledger", "opencode", "claude-code"))


class TestDeduplication(_Base):
    """A ledger run and the session it produced are ONE conversation."""

    def test_resolved_run_appears_once(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        session = _session("oc-1", created=T + 1500, updated=T + 5000,
                           tokens_in=11, tokens_out=22, cost=0.5)
        runs = self._run(ledger=ledger, opencode=[session])

        self.assertEqual(len(runs), 1, "the run and its session merged")
        run = runs[0]
        self.assertEqual(run.session_id, "oc-1")

    def test_merge_keeps_ledger_metadata_and_session_stats(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        session = _session("oc-1", created=T + 1500, updated=T + 5000,
                           title="Session Title",
                           tokens_in=11, tokens_out=22, cost=0.5)
        run = self._run(ledger=ledger, opencode=[session])[0]

        # Ledger side: the task file, its text, the readable transcript.
        self.assertTrue(run.prompt.endswith("a.md"))
        self.assertIn("do the thing", run.prompt_text)
        self.assertTrue(run.transcript.endswith("a-transcript.log"))
        self.assertEqual(run.source, "ledger")
        # Session side: the statistics the ledger never had.
        self.assertEqual((run.tokens_in, run.tokens_out, run.cost), (11, 22, 0.5))
        self.assertEqual(run.updated, T + 5000)

    def test_unrelated_session_is_not_merged_away(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        runs = self._run(
            ledger=ledger,
            opencode=[_session("oc-1", created=T + 1500, updated=T + 5000),
                      _session("oc-2", created=T + FAR, updated=T + FAR)],
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual({r.session_id for r in runs}, {"oc-1", "oc-2"})

    def test_unresolved_run_keeps_its_own_row(self):
        """A ledger run with no session must not collide with another."""
        ledger = self._ledger([
            self._entry(started=T, name="a"),
            self._entry(started=T + FAR, name="b"),
        ])
        runs = self._run(ledger=ledger)
        self.assertEqual(len(runs), 2)

    def test_continuation_and_its_own_session_both_survive(self):
        """Two ledger lines against one prompt file are two runs, not one.

        `fast-subagent-listing.md` really was delegated twice in the user's
        ledger. Each produced its own session, and de-dup must not fold them
        together just because the prompt path matches.
        """
        entry_a = self._entry(started=T, name="same")
        entry_b = dict(entry_a, started=T + FAR, continued=True)
        ledger = self._ledger([entry_a, entry_b])
        runs = self._run(
            ledger=ledger,
            opencode=[_session("oc-1", created=T + 1500, updated=T + 2000),
                      _session("oc-2", created=T + FAR + 1500,
                               updated=T + FAR + 2000)],
        )
        self.assertEqual(len(runs), 2)
        self.assertEqual({r.session_id for r in runs}, {"oc-1", "oc-2"})


class TestOrdering(_Base):
    def test_newest_first(self):
        ledger = self._ledger([self._entry(started=T + FAR, name="a")])
        runs = self._run(
            ledger=ledger,
            opencode=[_session("oc-1", created=T + 2 * FAR, updated=T + 2 * FAR)],
            claude_code=[_session("cc-1", "claude-code", created=T, updated=T)],
        )
        self.assertEqual([r.started for r in runs],
                         [T + 2 * FAR, T + FAR, T])


class TestSubagentMarking(_Base):
    """is_subagent comes from the parent pointer, never from a missing prompt."""

    def test_parent_id_marks_subagent(self):
        runs = self._run(
            ledger=self._ledger([]),
            opencode=[_session("child", created=T + FAR, parent="parent"),
                      _session("parent", created=T)],
        )
        by_id = {r.session_id: r for r in runs}
        self.assertTrue(by_id["child"].is_subagent)
        self.assertEqual(by_id["child"].parent_id, "parent")
        self.assertFalse(by_id["parent"].is_subagent)

    def test_direct_chat_is_not_a_subagent(self):
        """A chat you started yourself has no prompt file and no parent."""
        run = self._run(ledger=self._ledger([]),
                        opencode=[_session("oc-1")])[0]
        self.assertEqual(run.prompt, "")
        self.assertFalse(run.is_subagent)

    def test_ledger_run_is_never_a_subagent(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        run = self._run(ledger=ledger,
                        opencode=[_session("oc-1", created=T + 1500)])[0]
        self.assertFalse(run.is_subagent)


class TestIncludeFilter(_Base):
    def test_include_narrows_sources(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        kw = dict(ledger=ledger,
                  opencode=[_session("oc-1", created=T + FAR, updated=T + FAR)],
                  claude_code=[_session("cc-1", "claude-code",
                                        created=T + 2 * FAR)])

        only_oc = self._run(include={"opencode"}, **kw)
        self.assertEqual([r.source for r in only_oc], ["opencode"])

        no_cc = self._run(include=["ledger", "opencode"], **kw)
        self.assertEqual({r.source for r in no_cc}, {"ledger", "opencode"})

        self.assertEqual(self._run(include=[], **kw), [])


class TestLimits(_Base):
    def test_int_limit_applies_to_every_source(self):
        runs = self._run(
            ledger=self._ledger([]),
            opencode=[_session(f"oc-{i}", created=T + FAR * i,
                                updated=T + FAR * i) for i in range(1, 6)],
            claude_code=[_session(f"cc-{i}", "claude-code", created=T + FAR * i,
                                  updated=T + FAR * i) for i in range(1, 6)],
            limit_per_source=2,
        )
        self.assertEqual(len(runs), 4)

    def test_mapping_limit_is_per_source(self):
        runs = self._run(
            ledger=self._ledger([]),
            opencode=[_session(f"oc-{i}", created=T + FAR * i,
                                updated=T + FAR * i) for i in range(1, 6)],
            claude_code=[_session(f"cc-{i}", "claude-code", created=T + FAR * i,
                                  updated=T + FAR * i) for i in range(1, 6)],
            limit_per_source={"claude-code": 1},
        )
        self.assertEqual(sum(1 for r in runs if r.source == "opencode"), 5)
        self.assertEqual(sum(1 for r in runs if r.source == "claude-code"), 1)

    def test_limit_keeps_the_newest(self):
        runs = self._run(
            ledger=self._ledger([]),
            opencode=[_session("old", created=T, updated=T),
                      _session("new", created=T + FAR, updated=T + FAR)],
            limit_per_source={"opencode": 1},
        )
        self.assertEqual([r.session_id for r in runs], ["new"])

    def test_default_leaves_opencode_uncapped(self):
        """The reported bug in one assertion: no silent truncation."""
        runs = self._run(
            ledger=self._ledger([]),
            opencode=[_session(f"oc-{i}", created=T + i, updated=T + i)
                      for i in range(1, 200)],
        )
        self.assertEqual(len(runs), 199)


class TestErrorIsolation(_Base):
    """One broken source must never suppress the others."""

    def _boom(self, *a, **kw):
        raise RuntimeError("store is on fire")

    def test_opencode_failure_keeps_ledger_and_claude_code(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        with patch("delegate_view.adapters.opencode.list_sessions", self._boom), \
             patch("delegate_view.adapters.claude_code.list_sessions",
                   lambda **kw: [_session("cc-1", "claude-code",
                                          created=T + FAR)]):
            runs = all_runs(ledger_path=ledger)
        self.assertEqual({r.source for r in runs}, {"ledger", "claude-code"})

    def test_claude_code_failure_keeps_the_rest(self):
        ledger = self._ledger([self._entry(started=T, name="a")])
        with patch("delegate_view.adapters.claude_code.list_sessions", self._boom), \
             patch("delegate_view.adapters.opencode.list_sessions",
                   lambda **kw: [_session("oc-1", created=T + FAR)]):
            runs = all_runs(ledger_path=ledger)
        self.assertEqual({r.source for r in runs}, {"ledger", "opencode"})

    def test_ledger_failure_keeps_the_sessions(self):
        with patch("delegate_view.sessions.load_runs", self._boom), \
             patch("delegate_view.adapters.opencode.list_sessions",
                   lambda **kw: [_session("oc-1")]), \
             patch("delegate_view.adapters.claude_code.list_sessions",
                   lambda **kw: []):
            runs = all_runs(ledger_path=None)
        self.assertEqual([r.session_id for r in runs], ["oc-1"])

    def test_missing_ledger_is_not_an_error(self):
        runs = self._run(ledger=self._tmp / "nope.jsonl",
                         opencode=[_session("oc-1")])
        self.assertEqual(len(runs), 1)


class TestRunShape(_Base):
    """What a session-derived Run carries, for the TUI and the web UI."""

    def test_fields_from_session(self):
        s = _session("oc-1", created=T, updated=T + 2000, title="Some Chat",
                     cwd="/proj", tokens_in=5, tokens_out=6, cost=0.25)
        run = self._run(ledger=self._ledger([]), opencode=[s])[0]
        self.assertEqual(run.started, T)
        self.assertEqual(run.updated, T + 2000)
        self.assertEqual(run.prompt_text, "Some Chat")
        self.assertEqual(run.platform, "opencode")
        self.assertEqual(run.cwd, "/proj")
        self.assertEqual((run.tokens_in, run.tokens_out, run.cost), (5, 6, 0.25))
        self.assertFalse(run.failed)

    def test_started_falls_back_to_updated(self):
        run = self._run(ledger=self._ledger([]),
                        opencode=[_session("oc-1", created=0,
                                            updated=T + 7000)])[0]
        self.assertEqual(run.started, T + 7000)

    def test_returns_run_objects(self):
        run = self._run(ledger=self._ledger([]), opencode=[_session("oc-1")])[0]
        self.assertIsInstance(run, Run)


if __name__ == "__main__":
    unittest.main()
