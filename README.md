# delegate

Hand a task to another model and keep **one** readable transcript of it.

```sh
./delegate.sh task.md               # new session
./delegate.sh -c followup.md        # continue the last session
DELEGATE_MODEL=opencode/some-model ./delegate.sh task.md
```

## The problem it solves

```sh
opencode run "$(cat task.md)" > out.log
```

captures only the model's half. The prompt lives in one file and the reply in
another, so reading back what was actually *asked* means opening two things and
interleaving them by hand — and when the prompt was a heredoc or a scratch file
since cleaned up, the question is simply gone while the answer survives. That
asymmetry is how a delegated change ends up in a repo with no record of what it
was told to do.

`delegate.sh` appends the prompt into the log first, then appends the reply to
the same log. One file is the whole exchange, and `tail -f` shows both sides as
they happen.

The transcript defaults to sitting beside its prompt (`task.md` →
`task-transcript.log`) and is append-only, so a `-c` follow-up lands in the same
file as the exchange it continues — which is usually how you want to read it
back.

## Deliberately not a viewer

opencode already stores every session: `opencode session list`,
`opencode export <id>`, and its TUI renders them properly. The gap was never
that the data was missing, only that the two halves were never in one place.
Anything larger than this rebuilds what already exists.

Two things worth knowing if you go looking in the TUI instead:

- Sessions are scoped to the directory you launch from. Run `opencode` from the
  project directory or you will get a different project's session list.
- While a run is in flight the stored session is mid-write, so `opencode export`
  can return truncated JSON. The live log is the reliable read during a run.

## `--auto`

The script passes `--auto`, which approves tool use without prompting, because a
backgrounded run has nobody to answer the prompt. **Review the diff afterwards** —
`git status` before delegating is the cheap way to know what changed.

## Where it came from

Written inside a job-search repo during a session that delegated three tasks to
`opencode/big-pickle` and had to reconstruct, twice, what had actually been
asked. It does not belong to that project — nothing here touches its database or
its pipeline — so it moved out.

Two things that session learned about delegated work, worth keeping next to the
tool that makes it easier:

1. **Read the diff, not the report.** Reports came back confident and specific
   about things that were not true — a cleanup path described as handled when
   nothing handled it, and a cited incident figure that appeared nowhere in the
   repo.
2. **Delegated tests cover what the model built, not what it got wrong.** One
   13-check battery passed green across a draft that queued unscored records for
   an irreversible action. The tests were real; they simply pointed away from
   the defect.
