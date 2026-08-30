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

## Reading it from your phone

`delegate watch` needs a terminal. `delegate serve` gives you the same
transcripts as an ordinary web page, built for a phone screen:

```sh
delegate serve              # local only: http://127.0.0.1:8787
delegate serve --tunnel     # ...also reachable from anywhere
```

Both print a URL with an access token already in it. Copy that one string to
your phone and you are in — there is no login, no account, and nothing to set
up on the other end.

```
  delegate serve

  on this mac   http://127.0.0.1:8787/?t=Kx9…
  on your phone https://frost-marble-vessel-hazard.trycloudflare.com/?t=Kx9…
```

`--tunnel` shells out to [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
(`brew install cloudflared`) and runs a *quick tunnel*: a throwaway public
hostname, TLS terminated by Cloudflare, no account and no DNS. The tunnel dies
when you stop the command; the token does not change.

The page lists every conversation newest-first with live ones marked, and
renders a single conversation with text, reasoning, tool calls and patches as
distinct things rather than as one wall of log. Tool calls are collapsed by
default — a phone screen holds about fifteen lines and a single file read can
return four hundred. A live conversation refreshes itself every few seconds.

### How the access token works

- Generated once on first run and stored in `~/.delegate/web-token`, mode
  `0600`. It survives restarts, because a URL you texted to yourself has to
  keep working tomorrow.
- Required on **every** request, static assets included. The check runs before
  any routing, so an unauthenticated request to a real conversation and one to
  a made-up path get identical 401s and the tunnel hostname leaks nothing —
  not even whether a given session exists.
- Accepted as `?t=…`, which is then traded for an `HttpOnly` cookie and
  redirected away, so the token stops appearing in your browser history and in
  the URL bar of any page you might screenshot or share.
- Compared in constant time.

To rotate it, delete `~/.delegate/web-token` and restart. Every old link stops
working immediately.

### What to know before you tunnel

A quick tunnel hostname is **public**. It is unguessable, not secret, and the
link you copy to your phone carries your token — treat it like a password.
Anyone holding the full URL can read every transcript on this machine, which
includes source code, file paths, and whatever your task files said.

Mitigations that are already in place: the server binds `127.0.0.1` only, with
no flag to change that, so nothing but the tunnel can reach it; there is no
`POST` surface, so nothing on the page can change anything; transcript content
is HTML-escaped everywhere and served under a `default-src 'none'` CSP,
because agents write `<script>` tags all day and an unescaped tool output
would be stored XSS; and conversations are addressed by an opaque key looked
up in the run list, never by a path, so no request string is ever joined onto
a directory.

What is *not* protected: anyone who obtains the URL, including the token. Stop
the command when you are done.

### Notes

- Standard library only — no `pip install`, nothing to vendor.
- `--port N` if 8787 is taken (it walks forward to the next free port anyway).
- Long conversations render their last 400 events with a link to the rest, so
  a 10,000-event session does not become a page your phone cannot lay out.

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
