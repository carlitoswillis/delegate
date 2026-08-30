"""Blocks -> HTML.  Every string that came from a transcript is escaped here.

This is the only module that produces markup, which is the point: transcripts
are full of code, angle brackets, and — routinely — literal `<script>` tags,
because agents write and read HTML all day.  A single unescaped tool output is
stored XSS against a page that holds the session cookie for the whole viewer.
So there is exactly one way to put transcript text on a page, `e()`, and
nothing in this file interpolates a value without it.

The layout is mobile-first in the strict sense: the phone rendering is the
design, and the wide screen is the same design with more room, not a desktop
layout squeezed down.  That is why there is no sidebar, no table, and no
two-pane split — the TUI already has the desk covered.
"""

from __future__ import annotations

import time
from html import escape
from urllib.parse import quote

from delegate_view import views
from delegate_view.speakers import short_model
from delegate_view.web.blocks import REASONING_TEASER, Block, Chunk

__all__ = [
    "e", "layout", "list_page", "list_fragment", "conversation_page",
    "conversation_fragment", "error_page", "unauthorized_page",
]


def e(value) -> str:
    """Escape anything for HTML, quotes included.

    quote=True is not optional: these strings land in attribute values as well
    as text nodes (a tool name in a title=, a session key in an href=), and
    the difference between the two escaping modes is the difference between a
    safe page and an attribute-injection.
    """
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _href(path: str) -> str:
    """A same-origin URL with the path component percent-encoded.

    Keys are validated upstream, but a URL built by string concatenation is
    the classic way a validated value stops being validated a month later.
    """
    return e(quote(path, safe="/:"))


# ── page shell ──────────────────────────────────────────────────────────

def layout(title: str, body: str, *, header: str = "", body_class: str = "") -> str:
    """The document every page shares.

    viewport-fit=cover plus the safe-area padding in the stylesheet is what
    keeps the sticky header out from under an iPhone's notch and the last line
    of a transcript out from under the home indicator.  color-scheme tells the
    browser to render form controls and scrollbars in the right theme, which
    is the difference between a dark page and a dark page with a white
    scrollbar down the side of it.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="theme-color" content="#12141a" '
        'media="(prefers-color-scheme: dark)">\n'
        '<meta name="theme-color" content="#f7f7f8" '
        'media="(prefers-color-scheme: light)">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{e(title)}</title>\n"
        '<link rel="stylesheet" href="/static/style.css">\n'
        "</head>\n"
        f'<body class="{e(body_class)}">\n'
        f"{header}"
        f"{body}"
        '<script src="/static/app.js" defer></script>\n'
        "</body>\n</html>\n"
    )


# ── the run list ────────────────────────────────────────────────────────

def _run_card(run, key: str, now_ms: int) -> str:
    title = views.run_title(run)[0] or "(untitled)"
    model = short_model(getattr(run, "model", "") or "")
    live = bool(getattr(run, "live", False))
    # Age is measured from last activity, not from the start. On a phone the
    # question is always "is this still moving", and a run that began nine
    # hours ago but wrote a line a minute ago should not read as nine hours old.
    when = getattr(run, "updated", 0) or getattr(run, "started", 0) or 0

    bits: list[str] = []
    if when:
        bits.append(views.relative_age(now_ms, when) + " ago")
    tokens = getattr(run, "tokens_in", 0) + getattr(run, "tokens_out", 0)
    if tokens:
        bits.append(views.format_tokens(tokens) + " tok")
    cost = views.format_cost(getattr(run, "cost", 0.0))
    if cost:
        bits.append(cost)
    cwd = getattr(run, "cwd", "") or ""
    if cwd:
        bits.append(cwd.rsplit("/", 1)[-1])

    failed = bool(getattr(run, "failed", False))
    reason = getattr(run, "end_reason", "") or ""
    if failed and reason:
        # A failed run is marked, never hidden: the ledger is append-only so
        # that the record of what was ASKED survives a run that died, and a
        # list that quietly drops those throws away the thing worth keeping.
        bits.append(reason)

    if live:
        dot = '<span class="dot live" aria-label="live"></span>'
    elif failed:
        dot = '<span class="dot failed" aria-label="failed"></span>'
    else:
        dot = '<span class="dot"></span>'
    chip = f'<span class="chip">{e(model)}</span>' if model else ""

    # data-search carries a lowercased haystack so the filter box can work
    # without a round trip; it is escaped like everything else.
    haystack = " ".join([title, model, cwd, getattr(run, "platform", "") or ""])

    return (
        f'<li class="run{" live" if live else ""}{" failed" if failed else ""}" '
        f'data-search="{e(haystack.lower())}">'
        f'<a class="run-link" href="{_href("/c/" + key)}">'
        f'<span class="run-head">{dot}<span class="run-title">{e(title)}</span></span>'
        f'<span class="run-meta">{chip}'
        f'<span class="run-bits">{e(" · ".join(bits))}</span></span>'
        "</a></li>"
    )


def list_fragment(runs, keys, now_ms: int | None = None) -> str:
    """Just the list, so a poll can replace it without reloading the page."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if not runs:
        return ('<p class="empty">No conversations yet.<br>'
                'Try <code>delegate run task.md</code>.</p>')
    cards = "".join(_run_card(run, key, now_ms)
                    for run, key in zip(runs, keys))
    return f'<ul class="runs">{cards}</ul>'


def list_page(runs, keys, now_ms: int | None = None) -> str:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    live = sum(1 for r in runs if getattr(r, "live", False))
    count = f"{len(runs)} conversation{'s' if len(runs) != 1 else ''}"
    if live:
        count += f" · {live} live"

    header = (
        '<header class="top">'
        '<div class="top-row">'
        '<span class="brand">delegate</span>'
        f'<span class="count">{e(count)}</span>'
        "</div>"
        '<input class="filter" id="filter" type="search" '
        'placeholder="Filter conversations" autocomplete="off" '
        'autocapitalize="off" spellcheck="false" enterkeyhint="search">'
        "</header>"
    )
    body = (
        '<main id="list" class="list" data-fragment="/f/runs" data-poll="6">'
        f"{list_fragment(runs, keys, now_ms)}"
        "</main>"
    )
    return layout("delegate", body, header=header, body_class="page-list")


# ── one conversation ────────────────────────────────────────────────────

def _chunk_html(chunk: Chunk) -> str:
    if chunk.kind == "code":
        lang = f'<span class="lang">{e(chunk.lang)}</span>' if chunk.lang else ""
        return (f'<div class="code">{lang}'
                f"<pre><code>{e(chunk.text)}</code></pre></div>")
    return f'<p class="prose">{e(chunk.text)}</p>'


def _turn_html(block: Block) -> str:
    side = "user" if block.role == "user" else "assistant"
    body = "".join(_chunk_html(c) for c in block.chunks)
    return (f'<section class="block turn {side}">'
            f'<div class="speaker">{e(block.speaker)}</div>'
            f'<div class="turn-body">{body}</div>'
            "</section>")


def _reasoning_html(block: Block, idx: int) -> str:
    teaser = block.text.strip().replace("\n", " ")
    if len(teaser) > REASONING_TEASER:
        teaser = teaser[:REASONING_TEASER] + "…"
    return (f'<details class="block reasoning" data-id="r{idx}">'
            f'<summary><span class="tag">thinking</span>'
            f'<span class="teaser">{e(teaser)}</span></summary>'
            f'<pre class="reasoning-body">{e(block.text)}</pre>'
            "</details>")


def _tool_html(block: Block, idx: int) -> str:
    """A tool call, collapsed.

    Collapsed by default because a phone screen holds about fifteen lines and
    a single Read call can return four hundred.  The summary line keeps the
    part you scan for — which tool, on what, did it work — and the body is one
    tap away.  Expanding is where the args and the output live, each in its
    own horizontally scrollable box so neither can widen the page.
    """
    status = block.status or "completed"
    if status == "error":
        icon, cls = "✗", "err"
    elif status == "pending":
        icon, cls = "⋯", "pending"
    else:
        icon, cls = "✓", "ok"
    took = f"{block.ms} ms" if block.ms is not None else "pending"

    args = ""
    if block.args:
        rows = "".join(
            f'<div class="arg"><div class="arg-k">{e(k)}</div>'
            f'<div class="arg-v"><pre>{e(v)}</pre></div></div>'
            for k, v in block.args
        )
        args = f'<div class="args">{rows}</div>'

    output = ""
    if block.output:
        note = ('<div class="clip">output truncated</div>'
                if block.output_clipped else "")
        output = (f'<div class="out"><div class="out-k">output</div>'
                  f"<pre>{e(block.output)}</pre>{note}</div>")
    elif status != "pending":
        output = '<div class="out empty-out">no output</div>'

    summary_txt = (f'<span class="tool-arg">{e(block.summary)}</span>'
                   if block.summary else "")

    return (
        f'<details class="block tool {cls}" data-id="t{idx}">'
        f'<summary><span class="ico">{icon}</span>'
        f'<span class="tool-name">{e(block.name)}</span>'
        f"{summary_txt}"
        f'<span class="took">{e(took)}</span></summary>'
        f'<div class="tool-body">{args}{output}</div>'
        "</details>"
    )


def _patch_html(block: Block) -> str:
    files = ", ".join(block.files) or "patch"
    return (f'<section class="block patch">'
            f'<span class="ico">±</span><span class="files">{e(files)}</span>'
            "</section>")


def _block_html(block: Block, idx: int) -> str:
    if block.kind == "turn":
        return _turn_html(block)
    if block.kind == "reasoning":
        return _reasoning_html(block, idx)
    if block.kind == "tool":
        return _tool_html(block, idx)
    if block.kind == "patch":
        return _patch_html(block)
    if block.kind == "compaction":
        return '<div class="block compaction"><span>context compacted</span></div>'
    if block.kind == "raw":
        return (f'<section class="block raw"><div class="speaker">raw transcript'
                f'</div><pre>{e(block.text)}</pre></section>')
    return ""


def conversation_fragment(conv) -> str:
    if conv.error:
        return f'<p class="empty">{e(conv.error)}</p>'
    if not conv.blocks:
        return '<p class="empty">Nothing recorded in this conversation yet.</p>'

    note = ""
    if conv.truncated:
        note = (f'<div class="notice">Showing the last {conv.shown_events:,} '
                f'of {conv.total_events:,} events · '
                f'<a href="{_href("/c/" + conv.key)}?all=1">show all</a></div>')

    blocks = "".join(_block_html(b, i) for i, b in enumerate(conv.blocks))
    return note + blocks


def conversation_page(conv, *, all_events: bool = False) -> str:
    now_ms = int(time.time() * 1000)

    chips: list[str] = []
    model = short_model(conv.model)
    if model:
        chips.append(f'<span class="chip">{e(model)}</span>')
    if conv.platform:
        chips.append(f'<span class="chip subtle">{e(conv.platform)}</span>')
    if conv.updated:
        chips.append('<span class="chip subtle">'
                     f'{e(views.relative_age(now_ms, conv.updated))} ago</span>')
    tokens = conv.tokens_in + conv.tokens_out
    if tokens:
        chips.append('<span class="chip subtle">'
                     f'{e(views.format_tokens(tokens))} tok</span>')
    cost = views.format_cost(conv.cost)
    if cost:
        chips.append(f'<span class="chip subtle">{e(cost)}</span>')
    if conv.cwd:
        chips.append('<span class="chip subtle">'
                     f'{e(conv.cwd.rsplit("/", 1)[-1])}</span>')
    if conv.live:
        chips.insert(0, '<span class="chip livechip">live</span>')
    elif conv.failed:
        chips.insert(0, '<span class="chip failchip">'
                        f'{e(conv.end_reason or "failed")}</span>')

    header = (
        '<header class="top convtop">'
        '<div class="top-row">'
        '<a class="back" href="/" aria-label="Back to conversations">‹</a>'
        f'<span class="conv-title">{e(conv.title)}</span>'
        "</div>"
        f'<div class="chips">{"".join(chips)}</div>'
        "</header>"
    )

    # data-live drives the poll: a finished conversation is not going to
    # change, and polling it forever would be a phone waking a laptop's disk
    # every few seconds for nothing.
    #
    # The poll URL repeats the page's own ?all=1. Without it, the first
    # refresh of a live conversation opened with "show all" would swap the
    # full transcript back out for the capped tail underneath the reader.
    fragment_url = _href("/f/c/" + conv.key) + ("?all=1" if all_events else "")
    body = (
        f'<main id="conv" class="conv" data-fragment="{fragment_url}" '
        f'data-poll="{4 if conv.live else 0}" '
        f'data-live="{"1" if conv.live else "0"}">'
        f"{conversation_fragment(conv)}"
        "</main>"
        '<button class="to-bottom" id="to-bottom" type="button" '
        'aria-label="Jump to latest">↓</button>'
    )
    return layout(conv.title or "conversation", body, header=header,
                  body_class="page-conv")


# ── failure pages ───────────────────────────────────────────────────────

def error_page(status: int, message: str) -> str:
    body = (f'<main class="mid"><h1>{status}</h1><p>{e(message)}</p>'
            '<p><a class="btn" href="/">Back to conversations</a></p></main>')
    return layout(f"{status}", body, body_class="page-error")


def unauthorized_page() -> str:
    """The 401 body.

    Says nothing about what exists behind it: no path, no run count, no
    version.  An unauthenticated request cannot tell a real conversation URL
    from a made-up one, which is the whole point of checking auth before
    routing rather than after.
    """
    body = ('<main class="mid"><h1>401</h1>'
            "<p>This page needs its access link.</p>"
            "<p class=\"hint\">Open the full URL you were given — the one ending "
            "in <code>?t=…</code>. If you are seeing this after following such "
            "a link, your browser rejected the session cookie; use the full "
            "link each time.</p></main>")
    return layout("401", body, body_class="page-error")
