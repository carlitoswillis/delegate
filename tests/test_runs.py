"""Tests for delegate_view.runs — bulk resolution and load_runs."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate_view.runs import Run, load_runs, resolve_session, resolve_sessions


def _make_run(started: int, cwd: str, **kw) -> Run:
    defaults = dict(
        started=started, prompt="p.py", transcript="/tmp/t",
        model="m", cwd=cwd,
    )
    defaults.update(kw)
    return Run(**defaults)


def _write_ledger(path: Path, entries: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestResolveSessionsSingleCwd(unittest.TestCase):
    """resolve_sessions issues ONE list_sessions call for runs sharing a cwd."""

    def test_same_cwd_one_call(self):
        runs = [_make_run(started=1000 * i, cwd="/proj") for i in range(5)]
        call_count = 0

        def fake_list_sessions(cwd=None):
            nonlocal call_count
            call_count += 1
            return []

        with patch("delegate_view.adapters.list_sessions",
                    side_effect=fake_list_sessions):
            resolve_sessions(runs)

        self.assertEqual(call_count, 1,
                         f"Expected 1 call, got {call_count}")


class TestResolveSessionsDistinctCwds(unittest.TestCase):
    """resolve_sessions issues one call PER distinct cwd."""

    def test_two_cwds_two_calls(self):
        runs = [
            _make_run(started=100, cwd="/proj/a"),
            _make_run(started=200, cwd="/proj/a"),
            _make_run(started=300, cwd="/proj/b"),
        ]
        call_count = 0

        def fake_list_sessions(cwd=None):
            nonlocal call_count
            call_count += 1
            return []

        with patch("delegate_view.adapters.list_sessions",
                    side_effect=fake_list_sessions):
            resolve_sessions(runs)

        self.assertEqual(call_count, 2,
                         f"Expected 2 calls, got {call_count}")


class TestLoadRunsUsesBulkResolver(unittest.TestCase):
    """load_runs(resolve=True) calls list_sessions once per distinct cwd."""

    def test_ledger_same_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "transcript.txt"
            transcript.write_text("hello")
            entries = [
                {"started": i * 1000, "prompt": f"t{i}", "transcript": str(transcript),
                 "cwd": "/proj", "model": "m"}
                for i in range(5)
            ]
            _write_ledger(ledger, entries)

            call_count = 0

            def fake_list_sessions(cwd=None):
                nonlocal call_count
                call_count += 1
                return []

            with patch("delegate_view.adapters.list_sessions",
                        side_effect=fake_list_sessions):
                runs = load_runs(ledger_path=ledger, resolve=True)

            self.assertEqual(len(runs), 5)
            self.assertEqual(call_count, 1,
                             f"Expected 1 call, got {call_count}")

    def test_ledger_two_cwds(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "transcript.txt"
            transcript.write_text("hello")
            entries = [
                {"started": 1000, "prompt": "t1", "transcript": str(transcript),
                 "cwd": "/proj/a", "model": "m"},
                {"started": 2000, "prompt": "t2", "transcript": str(transcript),
                 "cwd": "/proj/a", "model": "m"},
                {"started": 3000, "prompt": "t3", "transcript": str(transcript),
                 "cwd": "/proj/b", "model": "m"},
            ]
            _write_ledger(ledger, entries)

            call_count = 0

            def fake_list_sessions(cwd=None):
                nonlocal call_count
                call_count += 1
                return []

            with patch("delegate_view.adapters.list_sessions",
                        side_effect=fake_list_sessions):
                runs = load_runs(ledger_path=ledger, resolve=True)

            self.assertEqual(len(runs), 3)
            self.assertEqual(call_count, 2,
                             f"Expected 2 calls, got {call_count}")


class TestBulkResolutionActuallyStoresTheResult(unittest.TestCase):
    """The batching must still WRITE what it resolves back onto the runs.

    Regression guard. When resolve_sessions was first written it computed the
    match correctly and then dropped it on the floor — the return value of the
    per-run helper was never assigned to r.platform / r.session_id. Every
    existing test still passed, because they all counted adapter CALLS or
    checked the single-run wrapper's return value, and none of them asserted
    that a run came out of the batch path with a session attached. The result
    was a resolver that went 840x faster by resolving nothing.

    A speed test that does not also assert the answer is a test for the wrong
    thing, so these assert the answer.
    """

    def _sessions(self):
        from delegate_view.schema import Session
        return [
            Session(id="early", platform="opencode", title="t", cwd="/proj",
                    model="m", created=2000, tokens_in=11, tokens_out=22,
                    cost=0.5),
            Session(id="later", platform="opencode", title="t", cwd="/proj",
                    model="m", created=9000),
        ]

    def test_resolve_sessions_populates_platform_and_id(self):
        runs = [_make_run(started=1000, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].platform, "opencode")
        self.assertEqual(runs[0].session_id, "early")

    def test_resolve_sessions_populates_token_and_cost_stats(self):
        runs = [_make_run(started=1000, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].tokens_in, 11)
        self.assertEqual(runs[0].tokens_out, 22)
        self.assertEqual(runs[0].cost, 0.5)

    def test_no_match_leaves_the_run_unresolved_not_wrong(self):
        # A run started after every known session resolves to nothing.
        runs = [_make_run(started=99999, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].platform, "")
        self.assertEqual(runs[0].session_id, "")

    def test_load_runs_end_to_end_attaches_sessions(self):
        # The path the TUI actually takes.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "t.log"
            transcript.write_text("hi")
            _write_ledger(ledger, [{
                "started": 1000, "prompt": str(transcript),
                "transcript": str(transcript), "model": "m", "cwd": "/proj",
            }])
            with patch("delegate_view.adapters.list_sessions",
                       return_value=self._sessions()):
                runs = load_runs(ledger_path=ledger, resolve=True)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].session_id, "early",
                         "load_runs(resolve=True) must attach the session")
        self.assertEqual(runs[0].platform, "opencode")


class TestResolveSessionStillWorks(unittest.TestCase):
    """resolve_session(run) remains a functional single-run interface."""

    def test_returns_match(self):
        from delegate_view.schema import Session
        run = _make_run(started=1000, cwd="/proj")
        fake_sessions = [
            Session(id="s1", platform="opencode", title="t", cwd="/proj",
                    model="m", created=2000),
        ]
        with patch("delegate_view.adapters.list_sessions",
                    return_value=fake_sessions):
            platform, sid = resolve_session(run)
        self.assertEqual(platform, "opencode")
        self.assertEqual(sid, "s1")
        self.assertEqual(run.tokens_in, 0)



# ── one run, one session ────────────────────────────────────────────────

def _session(sid, created, *, platform="opencode", cwd="/proj", parent=None,
             updated=0, tokens_in=0, tokens_out=0, cost=0.0):
    from delegate_view.schema import Session
    return Session(id=sid, platform=platform, title="t", cwd=cwd, model="m",
                   parent_id=parent, created=created, updated=updated or created,
                   tokens_in=tokens_in, tokens_out=tokens_out, cost=cost)


class TestOneRunOneSession(unittest.TestCase):
    """Two runs seconds apart must not both claim the same session.

    The old resolver picked, per run and with no memory, the earliest session
    created at or after that run.  Two runs in one directory a few seconds
    apart therefore resolved to the SAME session: the list showed one
    conversation twice and the other run's conversation not at all.  Confirmed
    on the real ledger, where four pairs collided.
    """

    def test_two_runs_get_two_sessions(self):
        runs = [_make_run(started=1000, cwd="/proj"),
                _make_run(started=2000, cwd="/proj")]
        sessions = [_session("first", 1500), _session("second", 2500)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions(runs)
        self.assertEqual([r.session_id for r in runs], ["first", "second"])

    def test_a_session_is_never_claimed_twice(self):
        runs = [_make_run(started=1000, cwd="/proj"),
                _make_run(started=1100, cwd="/proj"),
                _make_run(started=1200, cwd="/proj")]
        sessions = [_session("only", 1300)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions(runs)
        claimed = [r.session_id for r in runs if r.session_id]
        self.assertEqual(claimed, ["only"])
        self.assertEqual(len(claimed), len(set(claimed)))

    def test_the_nearest_preceding_run_wins(self):
        """A run that produced nothing must not steal the next run's session.

        `selection-indicator.md` and `fast-subagent-listing.md` were delegated
        79 seconds apart.  Only the second produced a session — 0.6s after its
        ledger line — and the session's own title says so.  Handing it to the
        earlier run (the run-first walk) is wrong by 79 seconds and shifts
        every later pairing too.
        """
        early = _make_run(started=1000, cwd="/proj")   # produced nothing
        later = _make_run(started=80_000, cwd="/proj")
        sessions = [_session("s", 80_600)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions([early, later])
        self.assertEqual(later.session_id, "s")
        self.assertEqual(early.session_id, "")

    def test_a_much_later_session_is_not_claimed(self):
        """An unrelated chat started later in the same directory is not mine."""
        from delegate_view.runs import RESOLVE_WINDOW_MS
        run = _make_run(started=1000, cwd="/proj")
        sessions = [_session("unrelated", 1000 + RESOLVE_WINDOW_MS + 1)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions([run])
        self.assertEqual(run.session_id, "")

    def test_subagent_sessions_are_not_claimable(self):
        """A run's @explore subagents are separate sessions, not the run's own."""
        run = _make_run(started=1000, cwd="/proj")
        other = _make_run(started=6000, cwd="/proj")
        sessions = [_session("top", 1500),
                    _session("child", 6500, parent="top")]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions([run, other])
        self.assertEqual(run.session_id, "top")
        self.assertEqual(other.session_id, "")

    def test_resolution_is_repeatable(self):
        """Re-resolving the same runs does not accumulate stale claims."""
        runs = [_make_run(started=1000, cwd="/proj"),
                _make_run(started=2000, cwd="/proj")]
        sessions = [_session("first", 1500), _session("second", 2500)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions(runs)
            resolve_sessions(runs)
        self.assertEqual([r.session_id for r in runs], ["first", "second"])

    def test_stats_follow_the_claim(self):
        runs = [_make_run(started=1000, cwd="/proj"),
                _make_run(started=2000, cwd="/proj")]
        sessions = [_session("first", 1500, tokens_in=1, tokens_out=2, cost=0.1),
                    _session("second", 2500, tokens_in=30, tokens_out=40, cost=0.9)]
        with patch("delegate_view.adapters.list_sessions", return_value=sessions):
            resolve_sessions(runs)
        self.assertEqual((runs[0].tokens_in, runs[0].cost), (1, 0.1))
        self.assertEqual((runs[1].tokens_in, runs[1].cost), (30, 0.9))


class TestCwdNormalization(unittest.TestCase):
    """The ledger's cwd and the store's directory are spelled differently."""

    def test_logical_and_physical_paths_group_together(self):
        """`/tmp/x` in the ledger is `/private/tmp/x` in the database on macOS."""
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)

            runs = [_make_run(started=1000, cwd=str(link)),
                    _make_run(started=2000, cwd=str(real))]
            calls = []

            def fake(cwd=None):
                calls.append(cwd)
                return [_session("a", 1500, cwd=str(real)),
                        _session("b", 2500, cwd=str(real))]

            with patch("delegate_view.adapters.list_sessions", side_effect=fake):
                resolve_sessions(runs)

            self.assertEqual(len(calls), 1,
                             "two spellings of one directory is one query")
            self.assertEqual([r.session_id for r in runs], ["a", "b"])


# ── transcript state ────────────────────────────────────────────────────

class TestTranscriptState(unittest.TestCase):
    """delegate.sh's markers say whether a run replied and how it ended."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def _write(self, text: str) -> str:
        path = self._tmp / "t.log"
        path.write_text(text)
        return str(path)

    def test_missing_file(self):
        from delegate_view.runs import REASON_MISSING, transcript_state
        has_reply, reason, size, _ = transcript_state(str(self._tmp / "nope.log"))
        self.assertFalse(has_reply)
        self.assertEqual(reason, REASON_MISSING)
        self.assertEqual(size, 0)

    def test_header_only_transcript_has_no_reply(self):
        """The `watcn` phantom: a header and nothing else, 63 bytes."""
        from delegate_view.runs import REASON_EMPTY, transcript_state
        path = self._write("\n===== SENT 2026-08-16 21:00:00 — opencode/m =====\n\n")
        has_reply, reason, _size, _ = transcript_state(path)
        self.assertFalse(has_reply)
        self.assertEqual(reason, REASON_EMPTY)

    def test_reply_with_content(self):
        from delegate_view.runs import transcript_state
        path = self._write("===== SENT x — m =====\n\ntask\n"
                           "\n===== REPLY =====\n\nthe answer\n")
        has_reply, reason, _size, _ = transcript_state(path)
        self.assertTrue(has_reply)
        self.assertEqual(reason, "")

    def test_empty_reply_section(self):
        from delegate_view.runs import REASON_EMPTY, transcript_state
        path = self._write("===== SENT x — m =====\n\ntask\n"
                           "\n===== REPLY =====\n\n")
        has_reply, reason, _size, _ = transcript_state(path)
        self.assertFalse(has_reply)
        self.assertEqual(reason, REASON_EMPTY)

    def test_end_markers_are_read(self):
        from delegate_view.runs import transcript_state
        for marker, expected in (
            ("completed", "completed"),
            ("failed (exit 3)", "failed (exit 3)"),
            ("interrupted", "interrupted"),
            ("terminated", "terminated"),
        ):
            path = self._write(
                "===== SENT x — m =====\n\ntask\n\n===== REPLY =====\n\nhi\n"
                f"\n===== END 2026-08-17 21:51:30 — {marker} =====\n")
            _has_reply, reason, _size, _ = transcript_state(path)
            self.assertEqual(reason, expected)

    def test_reply_after_the_end_marker_of_a_previous_run(self):
        """A `-c` continuation appends a second exchange to the same file."""
        from delegate_view.runs import transcript_state
        path = self._write(
            "===== SENT a — m =====\n\nq1\n\n===== REPLY =====\n\na1\n"
            "\n===== END 2026-08-17 21:00:00 — completed =====\n"
            "\n===== SENT b — m =====\n\nq2\n\n===== REPLY =====\n\na2\n"
            "\n===== END 2026-08-17 21:10:00 — interrupted =====\n")
        has_reply, reason, _size, _ = transcript_state(path)
        self.assertTrue(has_reply)
        self.assertEqual(reason, "interrupted", "the LAST marker is the state")

    def test_long_transcript_whose_header_scrolled_out(self):
        from delegate_view.runs import transcript_state
        path = self._write("===== SENT x — m =====\n\n===== REPLY =====\n\n"
                           + ("filler line\n" * 5000))
        has_reply, reason, _size, _ = transcript_state(path)
        self.assertTrue(has_reply)
        self.assertEqual(reason, "")


class TestLiveness(unittest.TestCase):
    """Three sources, three honest signals."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def _log(self, text: str) -> str:
        path = self._tmp / "t.log"
        path.write_text(text)
        return str(path)

    def test_open_transcript_is_live_past_the_old_mtime_window(self):
        """A model thinking quietly for a minute is not dead."""
        from delegate_view.runs import LIVE_WINDOW_S, is_live
        path = self._log("===== REPLY =====\n\nthinking...\n")
        quiet = time.time() - (LIVE_WINDOW_S * 3)
        os.utime(path, (quiet, quiet))
        run = _make_run(started=1000, cwd="/proj", transcript=path)
        self.assertTrue(is_live(run))

    def test_closed_transcript_is_never_live(self):
        from delegate_view.runs import is_live, transcript_state
        path = self._log("===== REPLY =====\n\nhi\n"
                         "\n===== END 2026-08-17 21:51:30 — completed =====\n")
        _h, reason, _s, _m = transcript_state(path)
        run = _make_run(started=1000, cwd="/proj", transcript=path,
                        end_reason=reason)
        self.assertFalse(is_live(run))

    def test_ancient_unclosed_transcript_is_not_live(self):
        """Transcripts written before the END marker existed must not all light up."""
        from delegate_view.runs import UNCLOSED_LIVE_WINDOW_S, is_live
        path = self._log("===== REPLY =====\n\nold output\n")
        old = time.time() - (UNCLOSED_LIVE_WINDOW_S + 60)
        os.utime(path, (old, old))
        run = _make_run(started=1000, cwd="/proj", transcript=path)
        self.assertFalse(is_live(run))

    def test_opencode_session_uses_updated(self):
        from delegate_view.runs import LIVE_WINDOW_S, Run, is_live
        now_ms = int(time.time() * 1000)
        fresh = Run(started=now_ms, prompt="", transcript="", model="m",
                    cwd="/proj", source="opencode", updated=now_ms)
        stale = Run(started=now_ms, prompt="", transcript="", model="m",
                    cwd="/proj", source="opencode",
                    updated=now_ms - (LIVE_WINDOW_S + 10) * 1000)
        self.assertTrue(is_live(fresh))
        self.assertFalse(is_live(stale))

    def test_run_with_nothing_to_check_is_not_live(self):
        from delegate_view.runs import Run, is_live
        self.assertFalse(is_live(Run(started=0, prompt="", transcript="",
                                     model="m", cwd="/proj")))


class TestFailedRuns(unittest.TestCase):
    """Phantom and crashed runs are MARKED, never hidden.

    The ledger is append-only on purpose: if a run dies, the record of what was
    asked has to survive.  Dropping the row would defeat that, so the row stays
    and carries the reason.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())

    def _load(self, entries):
        ledger = self._tmp / "runs.jsonl"
        _write_ledger(ledger, entries)
        with patch("delegate_view.adapters.list_sessions", return_value=[]):
            return load_runs(ledger_path=ledger)

    def _entry(self, name, body=None, started=1000, fresh=False):
        """A ledger entry whose transcript is finished unless `fresh`.

        Finished means back-dated: a transcript still being appended to is
        live, and a live run is never called failed — it may simply not have
        replied YET.  Only `fresh=True` keeps the just-written mtime.
        """
        from delegate_view.runs import UNCLOSED_LIVE_WINDOW_S
        prompt = self._tmp / f"{name}.md"
        prompt.write_text("task")
        log = self._tmp / f"{name}.log"
        if body is not None:
            log.write_text(body)
            if not fresh:
                old = time.time() - (UNCLOSED_LIVE_WINDOW_S + 60)
                os.utime(log, (old, old))
        return {"started": started, "prompt": str(prompt),
                "transcript": str(log), "model": "m", "cwd": "/proj"}

    def test_missing_transcript_is_failed(self):
        from delegate_view.runs import REASON_MISSING
        run = self._load([self._entry("gone")])[0]
        self.assertTrue(run.failed)
        self.assertEqual(run.end_reason, REASON_MISSING)

    def test_header_only_transcript_is_failed(self):
        from delegate_view.runs import REASON_EMPTY
        run = self._load([self._entry(
            "watcn", "\n===== SENT 2026-08-16 21:00 — opencode/m =====\n\n")])[0]
        self.assertTrue(run.failed)
        self.assertEqual(run.end_reason, REASON_EMPTY)

    def test_a_normal_run_is_not_failed(self):
        run = self._load([self._entry(
            "ok", "===== REPLY =====\n\nthe answer\n"
                  "\n===== END 2026-08-17 21:51:30 — completed =====\n")])[0]
        self.assertFalse(run.failed)
        self.assertEqual(run.end_reason, "completed")

    def test_nonzero_exit_is_failed(self):
        run = self._load([self._entry(
            "boom", "===== REPLY =====\n\npartial\n"
                    "\n===== END 2026-08-17 21:51:30 — failed (exit 3) =====\n")])[0]
        self.assertTrue(run.failed)
        self.assertEqual(run.end_reason, "failed (exit 3)")

    def test_interrupted_and_terminated_are_failed(self):
        for word in ("interrupted", "terminated"):
            run = self._load([self._entry(
                word, "===== REPLY =====\n\nhalf an answer\n"
                      f"\n===== END 2026-08-17 21:51:30 — {word} =====\n")])[0]
            self.assertTrue(run.failed)
            self.assertEqual(run.end_reason, word)

    def test_a_run_that_just_started_is_not_called_failed(self):
        now_ms = int(time.time() * 1000)
        run = self._load([self._entry("new", "", started=now_ms,
                                       fresh=True)])[0]
        self.assertFalse(run.failed)

    def test_a_resolved_run_survives_a_lost_transcript(self):
        """The conversation is still readable, so this is not a failure."""
        ledger = self._tmp / "runs.jsonl"
        _write_ledger(ledger, [self._entry("gone")])
        with patch("delegate_view.adapters.list_sessions",
                   return_value=[_session("s", 1500)]):
            run = load_runs(ledger_path=ledger)[0]
        self.assertEqual(run.session_id, "s")
        self.assertFalse(run.failed)
        self.assertEqual(run.end_reason, "")


class TestLoadRunsShape(unittest.TestCase):
    """Every ledger run says where it came from."""

    def test_source_is_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            log = Path(tmp) / "t.log"
            log.write_text("===== REPLY =====\n\nhi\n")
            _write_ledger(ledger, [{
                "started": 1000, "prompt": str(log), "transcript": str(log),
                "model": "m", "cwd": "/proj",
            }])
            with patch("delegate_view.adapters.list_sessions", return_value=[]):
                run = load_runs(ledger_path=ledger)[0]
        self.assertEqual(run.source, "ledger")
        self.assertFalse(run.is_subagent)


if __name__ == "__main__":
    unittest.main()
