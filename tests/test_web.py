"""Tests for the web UI.

The ones that matter here are the security tests, and they are written against
a real socket rather than against the handler's methods.  Auth, path handling
and escaping are all properties of what goes over the wire — a test that calls
`_static("../../etc/passwd")` directly proves the function is careful, not that
the server is, and it is the server that will be on the internet.

Nothing in this file starts a cloudflared tunnel.  Doing so would publish the
machine's transcripts to a public hostname in order to assert a regex, so the
parsing is tested against captured output instead and the process handling is
tested with a fake `Popen`.
"""

from __future__ import annotations

import http.client
import threading
import time

import pytest

from delegate_view.runs import Run
from delegate_view.schema import Event, Session
from delegate_view.web import auth, blocks, data, pages, server, tunnel

TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz0123456789"


# ── fixtures ────────────────────────────────────────────────────────────

def make_run(**kw) -> Run:
    base = dict(started=1_700_000_000_000, prompt="/tmp/tasks/ship-it.md",
                transcript="/tmp/tasks/ship-it-transcript.log",
                model="opencode/big-pickle", cwd="/tmp/proj",
                session_id="ses_abc", platform="opencode")
    base.update(kw)
    return Run(**base)


def make_session(events=None) -> Session:
    return Session(
        id="ses_abc", platform="opencode", title="Ship it",
        cwd="/tmp/proj", model="opencode/big-pickle",
        created=1_700_000_000_000, updated=1_700_000_100_000,
        events=events or [],
    )


class Client:
    """A tiny HTTP client that never follows redirects and never keeps state.

    Both of those are deliberate: the auth flow IS a redirect plus a cookie,
    and a client that quietly handled either would test nothing.
    """

    def __init__(self, port: int) -> None:
        self.port = port

    def get(self, path: str, *, token: str | None = None,
            cookie: str | None = None, headers: dict | None = None,
            method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = f"{auth.COOKIE_NAME}={cookie}"
        if token:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}t={token}"
        # Ask for identity so assertions can read the body as text without
        # decompressing; the gzip path gets its own test.
        hdrs.setdefault("Accept-Encoding", "identity")
        try:
            conn.request(method, path, headers=hdrs)
            res = conn.getresponse()
            body = res.read().decode("utf-8", "replace")
            return res.status, dict(res.getheaders()), body
        finally:
            conn.close()


@pytest.fixture
def web(request):
    """A live server on a random port, backed by whatever runs the test wants."""
    runs = getattr(request, "param", None)
    if runs is None:
        runs = [make_run()]
    session = make_session()

    index = data.RunIndex(fetch=lambda *_: list(runs), ttl=0.0)
    cache = data.ConversationCache(
        build=lambda run, key, cap=None: data.build_conversation(run, key, cap=cap)
    )

    # The adapter is patched per-test where a conversation body matters; the
    # default here is a session with no events.
    httpd = server.DelegateWeb(("127.0.0.1", 0), server.Handler,
                               token=TOKEN, index=index, cache=cache, quiet=True)
    # A short poll interval so shutdown() returns promptly: the default 0.5s
    # is paid by every test that uses this fixture and dominates the suite.
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    try:
        yield Client(httpd.server_address[1]), httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
    del session


# ── auth ────────────────────────────────────────────────────────────────

def test_no_token_is_rejected(web):
    client, _ = web
    status, _, body = client.get("/")
    assert status == 401
    # The 401 must not leak what is behind it: no run titles, no paths.
    assert "ship-it" not in body


def test_wrong_token_is_rejected(web):
    client, _ = web
    status, _, _ = client.get("/", token="not-the-token")
    assert status == 401


def test_prefix_of_token_is_rejected(web):
    """A near-miss must fail exactly like a wild guess."""
    client, _ = web
    status, _, _ = client.get("/", token=TOKEN[:-1])
    assert status == 401
    status, _, _ = client.get("/", token=TOKEN + "x")
    assert status == 401


def test_unauthenticated_404_and_200_are_indistinguishable(web):
    """Auth runs before routing, so probing cannot map the URL space."""
    client, _ = web
    real = client.get("/")
    missing = client.get("/definitely-not-a-page")
    conv = client.get("/c/opencode:ses_abc")
    assert real[0] == missing[0] == conv[0] == 401
    assert real[2] == missing[2] == conv[2]


def test_static_assets_require_the_token_too(web):
    client, _ = web
    assert client.get("/static/style.css")[0] == 401
    assert client.get("/static/app.js")[0] == 401
    assert client.get("/static/style.css", cookie=TOKEN)[0] == 200


def test_query_token_sets_cookie_and_redirects(web):
    client, _ = web
    status, headers, _ = client.get("/", token=TOKEN)
    assert status == 303
    assert headers["Location"] == "/"
    assert f"{auth.COOKIE_NAME}={TOKEN}" in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Lax" in headers["Set-Cookie"]


def test_redirect_reencodes_what_it_keeps(web):
    """Kept query values go back out percent-encoded.

    parse_qs DECODED them, and pasting decoded text into a Location header
    would let a %0d%0a in the query become a CRLF in the response head.
    """
    client, _ = web
    status, headers, _ = client.get("/c/opencode:ses_abc?x=a%0d%0ab",
                                    token=TOKEN)
    assert status == 303
    location = headers["Location"]
    assert "\r" not in location and "\n" not in location
    assert location.lower().startswith("/c/opencode:ses_abc?x=a%0d%0ab")


def test_redirect_keeps_other_query_params(web):
    client, _ = web
    status, headers, _ = client.get("/c/opencode:ses_abc?all=1", token=TOKEN)
    assert status == 303
    assert headers["Location"] == "/c/opencode:ses_abc?all=1"


def test_secure_flag_follows_the_forwarded_protocol(web):
    """cloudflared terminates TLS, so the header is the only evidence."""
    client, _ = web
    _, plain, _ = client.get("/", token=TOKEN)
    assert "Secure" not in plain["Set-Cookie"]
    _, tls, _ = client.get("/", token=TOKEN,
                           headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in tls["Set-Cookie"]


def test_cookie_authenticates_subsequent_requests(web):
    client, _ = web
    status, _, body = client.get("/", cookie=TOKEN)
    assert status == 200
    assert "ship-it" in body


def test_token_matches_is_strict():
    assert auth.token_matches("abc", "abc")
    assert not auth.token_matches("abc", "abd")
    assert not auth.token_matches(None, "abc")
    assert not auth.token_matches("", "abc")
    # An empty server-side token must lock everyone out, not let everyone in.
    assert not auth.token_matches("", "")
    assert not auth.token_matches("anything", "")


def test_token_is_persisted_and_reused(tmp_path):
    path = tmp_path / "web-token"
    first = auth.load_or_create_token(path)
    assert auth.load_or_create_token(path) == first
    assert len(first) > 30
    # 0600 or the token is readable by every account on the machine.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_empty_token_file_is_replaced(tmp_path):
    """A crashed first run leaves a zero-byte file; inheriting it is fatal."""
    path = tmp_path / "web-token"
    path.write_text("   \n")
    token = auth.load_or_create_token(path)
    assert token.strip()
    assert len(token) > 30


def test_cookie_parsing_ignores_other_cookies():
    header = f"other=1; {auth.COOKIE_NAME}=abc123; third=x"
    assert auth.token_from_cookies(header) == "abc123"
    assert auth.token_from_cookies("other=1") is None
    assert auth.token_from_cookies(None) is None


# ── path traversal ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/static/../../auth.py",
    "/static/../../../../etc/passwd",
    "/static/..%2f..%2fauth.py",
    "/static/%2e%2e/%2e%2e/auth.py",
    "/static//etc/passwd",
    "/static/subdir/style.css",
])
def test_static_traversal_is_refused(web, path):
    """Static files come from a fixed map, so a path is either a key or a 404."""
    client, _ = web
    status, _, body = client.get(path, cookie=TOKEN)
    assert status == 404
    assert "root:" not in body
    assert "load_or_create_token" not in body


@pytest.mark.parametrize("path", [
    "/c/../../../etc/passwd",
    "/c/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/c//etc/passwd",
    "/c/" + "/Users/me/.ssh/id_rsa",
    "/f/c/../../etc/passwd",
])
def test_conversation_key_is_never_a_path(web, path):
    """Keys are looked up in the run list; nothing is joined onto a directory."""
    client, _ = web
    status, _, body = client.get(path, cookie=TOKEN)
    assert status == 404
    assert "root:" not in body


def test_key_validation_rejects_separators():
    assert data.valid_key("opencode:ses_abc")
    assert data.valid_key("claude-code:agent-1234")
    assert not data.valid_key("../etc/passwd")
    assert not data.valid_key("a/b")
    assert not data.valid_key("")
    assert not data.valid_key("x" * 500)


def test_unknown_key_is_404_not_an_error(web):
    client, _ = web
    assert client.get("/c/opencode:nope", cookie=TOKEN)[0] == 404


# ── escaping ────────────────────────────────────────────────────────────

HOSTILE = '<script>alert("xss")</script> & <img src=x onerror=alert(1)>'


def test_escape_helper_covers_quotes():
    out = pages.e(HOSTILE)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&quot;" in out
    assert "&amp;" in out


def test_transcript_text_is_escaped_in_a_turn():
    block = blocks.Block(kind="turn", role="assistant", speaker="big-pickle",
                         chunks=[blocks.Chunk(kind="prose", text=HOSTILE)])
    html = pages.conversation_fragment(
        data.Conversation(key="k", title="t", run=make_run(), blocks=[block]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_tool_output_and_args_are_escaped():
    block = blocks.Block(kind="tool", name="Bash", summary=HOSTILE,
                         status="completed", ms=12,
                         args=[("cmd", HOSTILE)], output=HOSTILE)
    html = pages.conversation_fragment(
        data.Conversation(key="k", title="t", run=make_run(), blocks=[block]))
    assert "<script>" not in html
    # The payload survives as text — that is the point — but never as a tag.
    assert "<img" not in html
    assert html.count("&lt;script&gt;") >= 3


def test_hostile_title_cannot_break_out_of_an_attribute():
    """A session title lands in href= and data-search=, not just in text."""
    run = make_run(prompt="", prompt_text='" onmouseover="alert(1)')
    html = pages.list_fragment([run], ["opencode:ses_abc"])
    assert 'onmouseover="alert(1)"' not in html
    assert "&quot;" in html


def test_hostile_tool_name_is_escaped():
    block = blocks.Block(kind="tool", name='<b onload="x">', status="error")
    html = pages.conversation_fragment(
        data.Conversation(key="k", title="t", run=make_run(), blocks=[block]))
    assert "<b onload" not in html


def test_end_to_end_escaping_through_the_server(web, monkeypatch):
    """The real path: adapter -> blocks -> pages -> socket."""
    session = make_session([
        Event(ts=1, role="assistant", kind="tool_call", tool_name="Bash",
              tool_input={"command": HOSTILE}, tool_output=HOSTILE,
              tool_status="completed", tool_ms=5),
    ])
    monkeypatch.setattr("delegate_view.adapters.load_session",
                        lambda platform, sid: session)
    client, _ = web
    status, _, body = client.get("/c/opencode:ses_abc", cookie=TOKEN)
    assert status == 200
    # The only script tag on the page is the one the layout puts there.
    assert body.count("<script") == 1
    assert 'src="/static/app.js"' in body
    assert "&lt;script&gt;" in body


# ── listing ─────────────────────────────────────────────────────────────

def test_list_shows_runs_newest_first(web):
    client, _ = web
    status, _, body = client.get("/", cookie=TOKEN)
    assert status == 200
    assert "ship-it" in body
    assert "big-pickle" in body
    assert "1 conversation" in body


@pytest.mark.parametrize("web", [[
    make_run(session_id="a", live=True, prompt="/t/live.md"),
    make_run(session_id="b", live=False, prompt="/t/done.md"),
]], indirect=True)
def test_live_runs_are_marked(web):
    client, _ = web
    _, _, body = client.get("/", cookie=TOKEN)
    assert "2 conversations · 1 live" in body
    assert 'class="run live"' in body
    assert 'class="dot live"' in body


@pytest.mark.parametrize("web", [[]], indirect=True)
def test_empty_list_says_so(web):
    client, _ = web
    _, _, body = client.get("/", cookie=TOKEN)
    assert "No conversations yet" in body


def test_failed_run_is_marked_not_hidden():
    run = make_run(failed=True, end_reason="failed (exit 3)")
    html = pages.list_fragment([run], ["opencode:ses_abc"])
    assert "failed (exit 3)" in html
    assert 'class="dot failed"' in html


def test_run_key_is_stable_and_url_safe():
    run = make_run()
    assert data.run_key(run) == "opencode:ses_abc"
    assert data.run_key(run) == data.run_key(make_run())
    unresolved = make_run(platform="", session_id="")
    key = data.run_key(unresolved)
    assert key.startswith("t:")
    assert data.valid_key(key)
    # An unresolved run must not put its filesystem path in the URL.
    assert "/tmp" not in key


def test_run_index_caches_and_can_be_forced():
    calls = []

    def fetch(ledger, limit):
        calls.append(1)
        return [make_run()]

    index = data.RunIndex(fetch=fetch, ttl=60.0)
    index.runs()
    index.runs()
    assert len(calls) == 1
    index.runs(force=True)
    assert len(calls) == 2
    assert index.find("opencode:ses_abc") is not None
    assert index.find("nope") is None


def test_fallback_runs_is_used_when_sessions_module_is_missing(monkeypatch):
    """The contract module may not exist yet; the viewer still has to work."""
    monkeypatch.setattr(data, "_fallback_runs",
                        lambda ledger, limit: ["sentinel"])
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "delegate_view.sessions":
            raise ImportError("not built yet")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert data.fetch_runs() == ["sentinel"]


# ── conversation rendering ──────────────────────────────────────────────

def _render(events, **kw):
    session = make_session(events)
    conv = data.Conversation(
        key="k", title="Ship it", run=make_run(),
        blocks=blocks.blocks_for_session(session.events,
                                         model="opencode/big-pickle",
                                         platform="opencode", **kw),
    )
    return pages.conversation_fragment(conv)


def test_kinds_render_distinctly():
    html = _render([
        Event(ts=1, role="user", kind="text", text="do the thing"),
        Event(ts=2, role="assistant", kind="reasoning", text="thinking hard"),
        Event(ts=3, role="assistant", kind="text", text="done"),
        Event(ts=4, role="assistant", kind="tool_call", tool_name="Read",
              tool_input={"file_path": "/tmp/x.py"}, tool_output="print(1)",
              tool_status="completed", tool_ms=7),
        Event(ts=5, role="assistant", kind="patch", raw={"files": ["a.py"]}),
        Event(ts=6, role="assistant", kind="compaction"),
    ])
    assert 'class="block turn user"' in html
    assert 'class="block turn assistant"' in html
    assert 'class="block reasoning"' in html
    assert 'class="block tool ok"' in html
    assert 'class="block patch"' in html
    assert 'class="block compaction"' in html
    assert "do the thing" in html
    assert "thinking hard" in html
    assert "/tmp/x.py" in html
    assert "a.py" in html


def test_speakers_name_the_parties_not_the_roles():
    html = _render([Event(ts=1, role="user", kind="text", text="go")])
    assert "you → big-pickle" in html
    assert ">user<" not in html


def test_tool_status_drives_the_class():
    err = _render([Event(ts=1, role="assistant", kind="tool_call",
                         tool_name="Bash", tool_status="error",
                         tool_output="boom", tool_ms=1)])
    assert 'class="block tool err"' in err
    pending = _render([Event(ts=1, role="assistant", kind="tool_call",
                             tool_name="Bash", tool_status="pending")])
    assert 'class="block tool pending"' in pending
    assert "pending" in pending


def test_consecutive_same_speaker_text_merges_into_one_turn():
    html = _render([
        Event(ts=1, role="assistant", kind="text", text="first"),
        Event(ts=2, role="assistant", kind="text", text="second"),
    ])
    assert html.count('class="block turn assistant"') == 1
    assert "first" in html and "second" in html


def test_a_tool_call_breaks_the_turn():
    html = _render([
        Event(ts=1, role="assistant", kind="text", text="before"),
        Event(ts=2, role="assistant", kind="tool_call", tool_name="Bash",
              tool_status="completed", tool_ms=1),
        Event(ts=3, role="assistant", kind="text", text="after"),
    ])
    assert html.count('class="block turn assistant"') == 2


def test_code_fences_become_their_own_scrollable_block():
    html = _render([Event(ts=1, role="assistant", kind="text",
                          text="see:\n```python\nx = 1 < 2\n```\ndone")])
    assert '<div class="code">' in html
    assert "x = 1 &lt; 2" in html
    assert '<span class="lang">python</span>' in html


def test_fence_splitting_handles_an_unterminated_fence():
    chunks = blocks.split_fences("intro\n```\nnever closed")
    assert [c.kind for c in chunks] == ["prose", "code"]
    assert chunks[1].text == "never closed"


def test_fence_info_string_is_not_trusted_into_markup():
    chunks = blocks.split_fences('```<script>\nx\n```')
    assert chunks[0].lang == ""


def test_tool_output_is_clipped():
    text, clipped = blocks.clip_output("line\n" * 5000)
    assert clipped
    assert len(text.splitlines()) == blocks.MAX_OUTPUT_LINES


def test_long_tool_arguments_are_clipped():
    args = blocks.tool_args({"content": "x" * 50_000})
    assert len(args[0][1]) < blocks.MAX_ARG_CHARS + 100
    assert "truncated" in args[0][1]


def test_tool_summary_picks_the_useful_argument():
    assert blocks.tool_summary("Bash", {"command": "pytest -q",
                                        "timeout": 1}) == "pytest -q"
    assert blocks.tool_summary("Read", {"file_path": "/a/b.py"}) == "/a/b.py"
    assert blocks.tool_summary("Unknown", {"query": "hi"}) == "hi"
    assert blocks.tool_summary("Bash", {}) == ""


def test_long_conversations_are_capped_with_a_link_to_the_rest(monkeypatch):
    events = [Event(ts=i, role="assistant", kind="text", text=f"m{i}")
              for i in range(data.DEFAULT_EVENT_CAP + 50)]
    monkeypatch.setattr("delegate_view.adapters.load_session",
                        lambda platform, sid: make_session(events))
    conv = data.build_conversation(make_run(), "opencode:ses_abc")
    assert conv.truncated
    assert conv.shown_events == data.DEFAULT_EVENT_CAP
    html = pages.conversation_fragment(conv)
    assert "show all" in html
    # The tail is what is kept: you want the newest, not the oldest.
    assert f"m{data.DEFAULT_EVENT_CAP + 49}" in html


def test_show_all_survives_the_live_poll():
    """A live conversation opened with ?all=1 must poll its fragment with
    ?all=1 too, or the first refresh swaps the full transcript back out for
    the capped tail underneath the reader."""
    conv = data.Conversation(key="k", title="t", run=make_run(), live=True)
    assert 'data-fragment="/f/c/k?all=1"' in pages.conversation_page(
        conv, all_events=True)
    assert 'data-fragment="/f/c/k"' in pages.conversation_page(conv)


def test_unloadable_session_reports_instead_of_500(monkeypatch, web):
    def boom(platform, sid):
        raise RuntimeError("mid-write json")

    monkeypatch.setattr("delegate_view.adapters.load_session", boom)
    client, _ = web
    status, _, body = client.get("/c/opencode:ses_abc", cookie=TOKEN)
    assert status == 200
    assert "could not load" in body
    assert "mid-write" not in body  # no internals leaked to the page


def test_unresolved_run_falls_back_to_the_raw_transcript(tmp_path):
    log = tmp_path / "t.log"
    log.write_text("first\n<script>bad</script>\nlast\n")
    run = make_run(platform="", session_id="", transcript=str(log))
    conv = data.build_conversation(run, "t:abc")
    assert conv.blocks and conv.blocks[0].kind == "raw"
    html = pages.conversation_fragment(conv)
    assert "&lt;script&gt;" in html
    assert "<script>bad" not in html


# ── HTTP behaviour ──────────────────────────────────────────────────────

def test_fragment_is_conditional(web):
    client, _ = web
    status, headers, body = client.get("/f/runs", cookie=TOKEN)
    assert status == 200
    tag = headers["ETag"]
    assert body
    status, _, body2 = client.get("/f/runs", cookie=TOKEN,
                                  headers={"If-None-Match": tag})
    assert status == 304
    assert body2 == ""


def test_weak_etag_is_accepted(web):
    client, _ = web
    _, headers, _ = client.get("/f/runs", cookie=TOKEN)
    status, _, _ = client.get("/f/runs", cookie=TOKEN,
                              headers={"If-None-Match": "W/" + headers["ETag"]})
    assert status == 304


def test_security_headers_are_present(web):
    client, _ = web
    _, headers, _ = client.get("/", cookie=TOKEN)
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert "no-store" in headers["Cache-Control"]
    # No Python version advertised to the internet.
    assert headers["Server"] == "delegate"


def test_post_is_refused(web):
    client, _ = web
    status, _, _ = client.get("/", cookie=TOKEN, method="POST")
    assert status == 405


def test_gzip_is_used_for_large_bodies(web):
    client, _ = web
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
    conn.request("GET", "/static/style.css",
                 headers={"Cookie": f"{auth.COOKIE_NAME}={TOKEN}",
                          "Accept-Encoding": "gzip"})
    res = conn.getresponse()
    body = res.read()
    assert res.getheader("Content-Encoding") == "gzip"
    import gzip as _gzip
    assert b"--bg" in _gzip.decompress(body)
    conn.close()


def test_server_binds_loopback_only(web):
    _, httpd = web
    assert httpd.server_address[0] == "127.0.0.1"


def test_page_declares_a_mobile_viewport(web):
    client, _ = web
    _, _, body = client.get("/", cookie=TOKEN)
    assert 'name="viewport"' in body
    assert "width=device-width" in body
    assert "viewport-fit=cover" in body
    assert 'content="light dark"' in body


# ── cloudflared ─────────────────────────────────────────────────────────

# Captured from a real `cloudflared tunnel --url http://127.0.0.1:8787`.
CLOUDFLARED_OUTPUT = """\
2026-08-17T22:41:03Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out. However, be aware that these account-less Tunnels have no uptime guarantee. If you intend to use Tunnels in production you should use a pre-created named tunnel by following: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps
2026-08-17T22:41:03Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-08-17T22:41:05Z INF +--------------------------------------------------------------------------------------------+
2026-08-17T22:41:05Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):   |
2026-08-17T22:41:05Z INF |  https://frost-marble-vessel-hazard.trycloudflare.com                                       |
2026-08-17T22:41:05Z INF +--------------------------------------------------------------------------------------------+
2026-08-17T22:41:05Z INF Registered tunnel connection connIndex=0 location=sea01
"""


def test_tunnel_url_is_parsed_from_real_output():
    assert (tunnel.parse_tunnel_url(CLOUDFLARED_OUTPUT)
            == "https://frost-marble-vessel-hazard.trycloudflare.com")


def test_documentation_link_is_not_mistaken_for_the_tunnel():
    """The banner's first line contains a cloudflare.com URL that is not it."""
    first_line = CLOUDFLARED_OUTPUT.splitlines()[0]
    assert tunnel.parse_tunnel_url(first_line) is None


def test_no_url_yet_returns_none():
    assert tunnel.parse_tunnel_url("") is None
    assert tunnel.parse_tunnel_url("INF Requesting new quick Tunnel...") is None


class FakeProc:
    """Enough of Popen to drive Tunnel without running anything."""

    def __init__(self, lines, exit_code=None):
        self.stdout = iter(lines)
        self.returncode = exit_code
        self.terminated = False
        self.killed = False
        self._alive = exit_code is None

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self._alive = False


def test_tunnel_reports_the_url_it_saw():
    proc = FakeProc(CLOUDFLARED_OUTPUT.splitlines(True))
    tun = tunnel.Tunnel(8787, binary="/bin/true", spawn=lambda argv: proc)
    tun.start()
    assert tun.wait_for_url(timeout=3) == \
        "https://frost-marble-vessel-hazard.trycloudflare.com"
    tun.stop()
    assert proc.terminated


def test_tunnel_passes_the_local_port_and_disables_autoupdate():
    seen = {}

    def spawn(argv):
        seen["argv"] = argv
        return FakeProc(CLOUDFLARED_OUTPUT.splitlines(True))

    tun = tunnel.Tunnel(9123, binary="/bin/true", spawn=spawn)
    tun.start()
    tun.wait_for_url(timeout=3)
    tun.stop()
    assert "--url" in seen["argv"]
    assert "http://127.0.0.1:9123" in seen["argv"]
    assert "--no-autoupdate" in seen["argv"]


def test_tunnel_that_dies_explains_itself():
    proc = FakeProc(["ERR failed to connect\n"], exit_code=1)
    tun = tunnel.Tunnel(8787, binary="/bin/true", spawn=lambda argv: proc)
    tun.start()
    time.sleep(0.05)
    with pytest.raises(tunnel.TunnelError) as exc:
        tun.wait_for_url(timeout=0.2)
    assert "exited with status 1" in str(exc.value)
    assert "failed to connect" in str(exc.value)


def test_tunnel_timeout_is_not_silent():
    proc = FakeProc([])
    tun = tunnel.Tunnel(8787, binary="/bin/true", spawn=lambda argv: proc)
    tun.start()
    with pytest.raises(tunnel.TunnelError) as exc:
        tun.wait_for_url(timeout=0.1)
    assert "did not report a tunnel URL" in str(exc.value)
    tun.stop()


def test_missing_cloudflared_gives_install_instructions(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    with pytest.raises(tunnel.TunnelError) as exc:
        tunnel.find_cloudflared()
    assert "brew install cloudflared" in str(exc.value)


def test_stop_is_idempotent_and_safe_before_start():
    tun = tunnel.Tunnel(8787, binary="/bin/true", spawn=lambda argv: None)
    tun.stop()  # never started
    proc = FakeProc([])
    tun2 = tunnel.Tunnel(8787, binary="/bin/true", spawn=lambda argv: proc)
    tun2.start()
    tun2.stop()
    tun2.stop()
    assert proc.terminated


# ── the CLI ─────────────────────────────────────────────────────────────

def test_cli_parses_the_flags():
    from delegate_view.web.__main__ import build_parser

    args = build_parser().parse_args(["--port", "9000", "--tunnel",
                                      "--limit", "10"])
    assert args.port == 9000
    assert args.tunnel is True
    assert args.limit == 10
    assert build_parser().parse_args([]).tunnel is False


def test_make_server_walks_past_a_busy_port(tmp_path):
    first = server.make_server(0, token=TOKEN, quiet=True)
    port = first.server_address[1]
    try:
        second = server.make_server(port, token=TOKEN, quiet=True, tries=5)
        try:
            assert second.server_address[1] != port
            assert second.server_address[0] == "127.0.0.1"
        finally:
            second.server_close()
    finally:
        first.server_close()
