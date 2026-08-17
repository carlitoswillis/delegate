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

{ printf '\n===== SENT %s — %s =====\n\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODEL"
  cat "$PROMPT"
  printf '\n===== REPLY =====\n\n'; } >> "$LOG"

echo "transcript: $LOG" >&2
opencode run ${CONT:+$CONT} -m "$MODEL" --auto "$(cat "$PROMPT")" >> "$LOG" 2>&1
