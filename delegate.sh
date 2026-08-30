#!/usr/bin/env bash
#
# scripts/delegate.sh — hand a task to another model, keep ONE readable transcript.
#
#   ./scripts/delegate.sh task.md               # new session
#   ./scripts/delegate.sh -c followup.md        # continue the last session
#   DELEGATE_MODEL=opencode/some-model ./scripts/delegate.sh task.md
#
# WHY THIS EXISTS: `opencode run "$(cat task.md)" > out.log` captures only the
# model's half of the conversation. The prompt sits in one file and the reply in
# another, so reading back what was actually asked means opening two things and
# interleaving them by hand — and if the prompt was a heredoc or a scratch file
# that got cleaned up, the question is simply gone while the answer survives.
# Appending the prompt INTO the log first makes one file the whole exchange, and
# makes `tail -f` show both sides as they happen.
#
# The transcript is append-only and defaults to sitting beside the prompt, so a
# -c follow-up lands in the same file as the exchange it continues — which is
# usually what you want to read as one story.
#
# NOTE ON --auto: this approves tool use without prompting, because a
# backgrounded run has nobody to answer the prompt. Review the diff afterwards;
# `git status` before delegating is the cheap way to know what changed.
set -euo pipefail

CONT=""; [ "${1:-}" = "-c" ] && { CONT="-c"; shift; }
PROMPT="${1:?usage: delegate.sh [-c] <prompt-file> [transcript-file]}"
LOG="${2:-${PROMPT%.*}-transcript.log}"
MODEL="${DELEGATE_MODEL:-opencode/big-pickle}"

# VALIDATE BEFORE WRITING ANYTHING. Everything below this line appends to
# durable, append-only state — the ledger and the transcript — and none of it
# can be taken back. A mistyped prompt filename used to sail past here: the
# ledger line was written, the transcript header was written, and only then did
# `cat "$PROMPT"` fail, leaving a permanent phantom run in the list with a
# transcript containing nothing but a header. `watcn-transcript.log` in this
# repo is exactly that, sitting next to the `watch-transcript.log` it was a
# typo of. Checking first costs one stat and keeps a typo from becoming a
# permanent entry in an append-only record.
if [ ! -f "$PROMPT" ]; then
  echo "delegate: no such prompt file: $PROMPT" >&2
  exit 1
fi
if [ ! -r "$PROMPT" ]; then
  echo "delegate: prompt file is not readable: $PROMPT" >&2
  exit 1
fi
if [ ! -s "$PROMPT" ]; then
  echo "delegate: prompt file is empty: $PROMPT" >&2
  exit 1
fi

# The transcript defaults to sitting beside the prompt, but an explicit second
# argument can point anywhere — including a directory that does not exist yet.
# Failing here beats failing after the ledger line is already committed.
LOG_DIR="$(dirname "$LOG")"
if [ ! -d "$LOG_DIR" ]; then
  echo "delegate: transcript directory does not exist: $LOG_DIR" >&2
  exit 1
fi

# opencode is what actually runs the task. Discovering it is missing only after
# the ledger and the prompt header are written produces the same phantom run as
# a mistyped prompt file.
if ! command -v opencode >/dev/null 2>&1; then
  echo "delegate: opencode is not on your PATH" >&2
  echo "  install it first: https://opencode.ai" >&2
  exit 127
fi

# THE LEDGER: one JSON line per run, appended before the run starts.
#
# Without this there is no record that a delegation happened at all — the
# opencode session lands in a SQLite row keyed by working directory, and
# matching it back to the task you handed over means eyeballing timestamps.
# `started` is what lets a viewer resolve the session afterwards: the run is
# the newest session in `cwd` created at or after that moment.
#
# Written BEFORE the run for the same reason the prompt is appended before
# the reply — if the run dies, the record of what was asked survives.
LEDGER="${DELEGATE_LEDGER:-$HOME/.delegate/runs.jsonl}"
mkdir -p "$(dirname "$LEDGER")"

# python3 does the quoting; hand-rolled JSON breaks on the first quote or
# backslash in a path, and paths here are user-controlled.
python3 - "$PROMPT" "$LOG" "$MODEL" "$PWD" "${CONT:-new}" >> "$LEDGER" <<'PY'
import json, os, sys, time
prompt, log, model, cwd, cont = sys.argv[1:6]
print(json.dumps({
    "started": int(time.time() * 1000),
    "prompt": os.path.abspath(prompt),
    "transcript": os.path.abspath(log),
    "model": model,
    "cwd": cwd,
    "continued": cont == "-c",
    "pid": os.getppid(),
}))
PY

{ printf '\n===== SENT %s — %s =====\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODEL"
  cat "$PROMPT"
  printf '\n===== REPLY =====\n\n'; } >> "$LOG"

echo "transcript: $LOG" >&2
echo "ledger:     $LEDGER" >&2

# CLOSE THE TRANSCRIPT, ALWAYS. The prompt is appended before the run so the
# question survives a crash; the same reasoning applies to the ending. Without
# a closing marker a transcript that stops mid-sentence is ambiguous — you
# cannot tell a model that finished from one that was killed, ran out of quota,
# or is still writing. The trap fires on normal exit and on interrupt alike, so
# every transcript ends by saying how it ended.
#
# It also gives readers a liveness signal that does not depend on guessing from
# mtime: a transcript with no END marker and a recent mtime is genuinely still
# running, where mtime alone calls a model that has been thinking quietly for a
# minute dead.
finish() {
  # $? must be read first — every command below overwrites it. An explicit
  # argument wins, because a signal handler knows its own status where $? in
  # that context reports whatever the interrupted command happened to leave.
  status=$?
  [ -n "${1:-}" ] && status="$1"

  # Disarm before doing anything else. A signal trap that calls `exit` re-enters
  # through the EXIT trap and writes a second, contradictory END marker — the
  # interrupted case ended up stamped "completed" underneath its own
  # "interrupted" line. Clearing all three traps makes finish run exactly once
  # no matter which one got here first.
  trap - EXIT INT TERM

  case "$status" in
    0)   printf '\n===== END %s — completed =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" ;;
    130) printf '\n===== END %s — interrupted =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" ;;
    143) printf '\n===== END %s — terminated =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" ;;
    *)   printf '\n===== END %s — failed (exit %s) =====\n' \
           "$(date '+%Y-%m-%d %H:%M:%S')" "$status" ;;
  esac >> "$LOG"
  exit "$status"
}
trap 'finish' EXIT
trap 'finish 130' INT
trap 'finish 143' TERM

# `set -e` would abort before the trap could record a non-zero exit, so the
# status is captured explicitly and handed to the trap instead.
set +e
opencode run ${CONT:+$CONT} -m "$MODEL" --auto "$(cat "$PROMPT")" >> "$LOG" 2>&1
exit $?
