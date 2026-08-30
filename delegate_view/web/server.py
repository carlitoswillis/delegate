"""The HTTP server: routing, auth enforcement, and conditional GETs.

Standard library only, on purpose.  The whole value of this feature is being
able to type one command on a machine you have not set up and read your
transcripts from your phone; a requirements.txt would defeat it.

Three things about the shape of this file.

**Auth runs before routing.**  Not as a decorator on some handlers, not inside
each route — the very first thing `do_GET` does.  An unauthenticated request
to a real conversation URL and one to a made-up path get byte-identical 401s,
so the tunnel hostname leaks nothing about what is behind it, not even whether
a given session exists.

**The URL never becomes a path.**  Conversations are addressed by a key that
is looked up in the run list; static files are served from a fixed whitelist.
Nothing here concatenates a request string onto a directory, which is the only
way to be sure about path traversal in a service whose whole job is reading
files by path.

**Fragments are conditional.**  A phone polling a live run asks every few
seconds; the ETag turns the steady state into a 304 with no body.
"""

from __future__ import annotations

import gzip
import hashlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from delegate_view.web import auth, pages
from delegate_view.web.data import (
    DEFAULT_EVENT_CAP,
    ConversationCache,
    RunIndex,
    run_key,
    valid_key,
)

DEFAULT_PORT = 8787

# Static files are served from this fixed map and no other way.  A dict of
# allowed names is not a check that can be got around by encoding tricks: a
# path that is not a key here simply has no file behind it.
STATIC = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Sent on every response.  These matter more than usual here: the page renders
# text that an agent wrote, and an agent writes HTML and JavaScript as a matter
# of routine.  Escaping in pages.py is the defence; this is the second one.
SECURITY_HEADERS = {
    # No external anything.  If a transcript ever did smuggle a tag past the
    # escaping, it still could not phone home or load a remote script.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # A tunnel hostname is public. Keep transcripts out of shared caches.
    "Cache-Control": "private, no-store",
}

# Below this a compressed response is not worth the CPU or the extra header.
GZIP_MIN = 1400

# Compressible types. Everything this serves is text; the list exists so a
# future binary asset does not silently get gzipped twice.
GZIP_TYPES = ("text/html", "text/css", "text/javascript", "application/json")


def _wants_all(query: dict) -> bool:
    """Whether the request asked for the uncapped conversation."""
    return (query.get("all") or [""])[0] == "1"


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _etag_matches(header: str | None, tag: str) -> bool:
    """If-None-Match, handling the list form and weak validators."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == tag or candidate == "*":
            return True
    return False


class DelegateWeb(ThreadingHTTPServer):
    """The server, carrying the shared state its handlers read."""

    daemon_threads = True
    # Bounded queue: this is a single-user service behind a tunnel, and an
    # unbounded backlog only converts a burst into a slower burst.
    request_queue_size = 32

    def __init__(self, addr, handler, *, token: str, index: RunIndex,
                 cache: ConversationCache, quiet: bool = False) -> None:
        super().__init__(addr, handler)
        self.token = token
        self.index = index
        self.cache = cache
        self.quiet = quiet


class Handler(BaseHTTPRequestHandler):
    server_version = "delegate"
    sys_version = ""  # do not advertise the Python version to the internet
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args) -> None:
        """One compact line, with the query string stripped.

        The default logger writes the full request line, and the request line
        of the very first visit contains the access token.  Printing that to a
        terminal — and into whatever scrollback or log file is capturing it —
        would defeat the point of redirecting the token out of the URL bar.
        """
        if getattr(self.server, "quiet", False):
            return
        path = urlparse(self.path).path
        sys.stderr.write(f"{self.command} {path} {args[1] if len(args) > 1 else ''}\n")

    def version_string(self) -> str:
        return "delegate"

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        # Compression is not a nicety here. A 400-event Claude Code session
        # renders to ~200KB of HTML, and the reader is on a phone on cellular
        # at the far end of a tunnel; gzip takes that to roughly a tenth. It is
        # applied last, after the ETag, so the validator identifies the
        # resource rather than the encoding of one particular response.
        encoding = None
        if (len(body) >= GZIP_MIN and self._accepts_gzip()
                and any(ctype.startswith(t) for t in GZIP_TYPES)):
            body = gzip.compress(body, 6)
            encoding = "gzip"

        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A phone that locked its screen mid-transfer. Normal, not an error.
            self.close_connection = True

    def _html(self, status: int, html: str,
              extra: dict[str, str] | None = None) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _not_found(self) -> None:
        self._html(404, pages.error_page(404, "No such page."))

    # -- auth -------------------------------------------------------------

    def _is_https(self) -> bool:
        """Whether the client's leg of the connection was TLS.

        cloudflared terminates TLS and forwards plain http to us, so the only
        evidence is the header it sets.  Getting this wrong in the safe
        direction (assuming http) merely drops the Secure flag; getting it
        wrong the other way sets Secure on a cookie the browser then refuses
        to store over plain http, and every page 401s.
        """
        proto = self.headers.get("X-Forwarded-Proto", "")
        return proto.split(",")[0].strip().lower() == "https"

    def _authenticate(self, query: dict) -> tuple[bool, str | None]:
        """(ok, token_from_query).

        A token in the query authenticates this request AND earns a cookie; a
        cookie authenticates quietly.  Both go through the same constant-time
        compare.
        """
        expected = self.server.token
        for name in auth.QUERY_PARAMS:
            values = query.get(name) or []
            if values and auth.token_matches(values[0], expected):
                return True, values[0]
        cookie = auth.token_from_cookies(self.headers.get("Cookie"))
        if auth.token_matches(cookie, expected):
            return True, None
        return False, None

    # -- routing ----------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        ok, from_query = self._authenticate(query)
        if not ok:
            # Identical response for every unauthenticated request, whatever
            # the path was, so a probe cannot map what exists behind here.
            self._html(401, pages.unauthorized_page())
            return

        if from_query is not None:
            # Trade the token in the URL for a cookie and bounce to the clean
            # path. Two reasons beyond tidiness: the token stops appearing in
            # browser history and in anything that screenshots the URL bar,
            # and a link the user shares by copying the address bar of a page
            # they navigated to no longer carries their credential.
            self._redirect_clean(parsed, query, from_query)
            return

        path = parsed.path
        if path == "/":
            self._list_page()
        elif path == "/f/runs":
            self._list_fragment()
        elif path.startswith("/c/"):
            self._conversation(path[3:], query)
        elif path.startswith("/f/c/"):
            self._conversation_fragment(path[5:], query)
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._not_found()

    def do_POST(self) -> None:
        # Nothing here mutates anything, so there is no POST surface at all —
        # and therefore no CSRF surface either.
        self._send(405, b"method not allowed", "text/plain; charset=utf-8",
                   {"Allow": "GET, HEAD"})

    def _redirect_clean(self, parsed, query: dict, token: str) -> None:
        keep = {k: v for k, v in query.items() if k not in auth.QUERY_PARAMS}
        # Re-encoded, never re-joined: parse_qs already DECODED these values,
        # and pasting decoded text into a Location header is how a %0d%0a in
        # the query becomes a CRLF in the response head.
        rest = urlencode([(k, v) for k, vs in keep.items() for v in vs])
        target = parsed.path or "/"
        if rest:
            target += "?" + rest
        self._send(
            303, b"", "text/plain; charset=utf-8",
            {
                "Location": target,
                "Set-Cookie": auth.cookie_header(token, secure=self._is_https()),
            },
        )

    # -- pages ------------------------------------------------------------

    def _runs_and_keys(self):
        runs = self.server.index.runs()
        return runs, [run_key(r) for r in runs]

    def _list_page(self) -> None:
        runs, keys = self._runs_and_keys()
        self._html(200, pages.list_page(runs, keys))

    def _list_fragment(self) -> None:
        runs, keys = self._runs_and_keys()
        self._fragment(pages.list_fragment(runs, keys))

    def _conversation(self, key: str, query: dict) -> None:
        conv = self._load(key, query)
        if conv is None:
            self._not_found()
            return
        self._html(200, pages.conversation_page(conv,
                                                all_events=_wants_all(query)))

    def _conversation_fragment(self, key: str, query: dict) -> None:
        conv = self._load(key, query)
        if conv is None:
            self._not_found()
            return
        self._fragment(pages.conversation_fragment(conv))

    def _load(self, key: str, query: dict):
        if not valid_key(key):
            return None
        run = self.server.index.find(key)
        if run is None:
            return None
        cap = None if _wants_all(query) else DEFAULT_EVENT_CAP
        return self.server.cache.get(run, key, cap=cap)

    def _fragment(self, html: str) -> None:
        body = html.encode("utf-8")
        tag = etag_for(body)
        if _etag_matches(self.headers.get("If-None-Match"), tag):
            self._send(304, b"", "text/html; charset=utf-8", {"ETag": tag})
            return
        self._send(200, body, "text/html; charset=utf-8", {"ETag": tag})

    def _static(self, name: str) -> None:
        ctype = STATIC.get(name)
        if ctype is None:
            # Includes every traversal attempt: "../../etc/passwd" is simply
            # not a key in the map, so there is nothing to normalize and
            # nothing to get wrong.
            self._not_found()
            return
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            self._not_found()
            return
        tag = etag_for(body)
        if _etag_matches(self.headers.get("If-None-Match"), tag):
            self._send(304, b"", ctype, {"ETag": tag})
            return
        self._send(200, body, ctype, {"ETag": tag})


# ── lifecycle ───────────────────────────────────────────────────────────

def make_server(port: int = DEFAULT_PORT, *, token: str,
                ledger_path=None, limit_per_source=None,
                quiet: bool = False, tries: int = 10) -> DelegateWeb:
    """A server bound to 127.0.0.1, walking forward if the port is taken.

    Loopback only, always, with no option to change it.  The security model is
    "nothing but the tunnel can reach this", and an interface flag is the one
    line of config that would quietly turn a token-protected local service into
    an open one on a coffee shop network.  If you want it reachable, use the
    tunnel — that path at least terminates TLS.

    Walking the port forward matters because the common case is a second
    `delegate serve` while the first is still running, and failing with
    EADDRINUSE sends people hunting for a pid instead of reading a transcript.
    """
    index = RunIndex(ledger_path=ledger_path, limit_per_source=limit_per_source)
    cache = ConversationCache()
    last: OSError | None = None
    for offset in range(max(1, tries)):
        try:
            return DelegateWeb(("127.0.0.1", port + offset), Handler,
                               token=token, index=index, cache=cache,
                               quiet=quiet)
        except OSError as exc:
            last = exc
            continue
    raise last  # type: ignore[misc]


def serve_forever_in_thread(server: DelegateWeb) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="delegate-web")
    thread.start()
    return thread


def wait_until_ready(port: int, timeout: float = 5.0) -> bool:
    """Block until the port accepts a connection, so callers can order output.

    Used by the tunnel path: starting cloudflared against a socket that is not
    listening yet produces a tunnel that 502s for its first few seconds, which
    reads as "this is broken" on a phone.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False
