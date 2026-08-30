"""`delegate serve` — the transcripts, on your phone.

Everything printed here is aimed at one moment: you are at the keyboard, the
phone is in your other hand, and you need exactly one string to get from here
to there.  So the URL is printed WITH the token in it, ready to copy, and the
warning about what that URL is capable of is printed right next to it rather
than in a README nobody opens at that moment.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from delegate_view.web import auth, server
from delegate_view.web.tunnel import Tunnel, TunnelError


def _say(text: str = "") -> None:
    """Print, flushed.

    stdout is block-buffered the moment this is piped into a file or a pager,
    and the one line that must never sit in a buffer is the URL — someone
    staring at an empty terminal while the server is already up concludes it
    is broken and kills it.
    """
    print(text, flush=True)


def _url(base: str, token: str) -> str:
    return f"{base}/?t={token}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delegate serve",
        description="Serve agent transcripts as a mobile web page.",
    )
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT,
                        help=f"local port (default {server.DEFAULT_PORT}); "
                             "the next free one is used if it is taken")
    parser.add_argument("--tunnel", action="store_true",
                        help="publish through a cloudflared quick tunnel so "
                             "the page is reachable off your network")
    parser.add_argument("--ledger", dest="ledger_path", default=None,
                        help="path to the ledger file")
    parser.add_argument("--limit", type=int, default=None,
                        help="how many recent conversations to list per source")
    parser.add_argument("--token-file", dest="token_file", default=None,
                        help="where the access token is stored "
                             "(default ~/.delegate/web-token)")
    parser.add_argument("--quiet", action="store_true",
                        help="do not log requests")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    token = auth.load_or_create_token(args.token_file)
    token_path = args.token_file or auth.default_token_path()

    try:
        httpd = server.make_server(
            args.port, token=token, ledger_path=args.ledger_path,
            limit_per_source=args.limit, quiet=args.quiet,
        )
    except OSError as exc:
        print(f"delegate serve: could not bind a port: {exc}",
              file=sys.stderr, flush=True)
        return 1

    port = httpd.server_address[1]
    server.serve_forever_in_thread(httpd)
    server.wait_until_ready(port)

    local = _url(f"http://127.0.0.1:{port}", token)

    _say()
    _say("  delegate serve")
    _say()
    _say(f"  on this mac   {local}")
    _say(f"  token file    {token_path}")

    tunnel: Tunnel | None = None
    if args.tunnel:
        _say()
        _say("  starting cloudflared…")
        try:
            tunnel = Tunnel(port)
            tunnel.start()
            public = tunnel.wait_for_url()
            _say(f"  on your phone {_url(public, token)}")
            _say()
            _say("  That link is public and carries your access token —")
            _say("  treat it like a password. It stops working when this")
            _say("  command exits; the token itself does not change.")
        except TunnelError as exc:
            if tunnel is not None:
                tunnel.stop()
                tunnel = None
            _say()
            print(f"  {exc}".replace("\n", "\n  "), file=sys.stderr, flush=True)

    _say()
    _say("  ctrl-c to stop")
    _say()

    # A plain Event().wait() rather than a sleep loop, so ctrl-c is instant and
    # the cleanup below always runs — a leaked cloudflared is a public route
    # into this machine that outlives the thing it was routing to.
    stop = threading.Event()

    def _handle(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel is not None:
            tunnel.stop()
        httpd.shutdown()
        httpd.server_close()
        _say("stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
