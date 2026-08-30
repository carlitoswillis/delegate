"""The web face of delegate — the same transcripts, readable from a phone.

The TUI is the right tool at a desk and the wrong one everywhere else: it
needs a terminal, a keyboard, and a machine you are sitting at.  This package
serves the same normalized Session/Event data as ordinary HTML so a
delegated run can be read from a phone over a Cloudflare tunnel.

Deliberately standard library only.  This has to start on a stock macOS
python3 with no virtualenv and no pip install, because the moment reading a
transcript from your phone requires a dependency install it stops happening.

Layout:
    auth.py    the shared secret, and constant-time checking of it
    data.py    runs and conversations, cached, from the adapters
    blocks.py  Session/Event -> display blocks the templates can render
    pages.py   blocks -> escaped HTML
    server.py  routing, auth enforcement, ETag polling
    tunnel.py  cloudflared supervision and URL parsing
"""
