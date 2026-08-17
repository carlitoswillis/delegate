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
opencode run ${CONT:+$CONT} -m "$MODEL" --auto "$(cat "$PROMPT")" >> "$LOG" 2>&1
