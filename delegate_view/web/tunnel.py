"""cloudflared supervision: get a public https URL, and give it back on exit.

A quick tunnel is the right tool for this: no Cloudflare account, no DNS, no
port forwarding, no VPN, and TLS terminated for you — which matters because
the token is in the URL of the first request and sending that over plain http
would hand it to every network between the phone and here.

What a quick tunnel is NOT is private.  The hostname is random, but it is a
real public hostname reachable by anyone who learns it, which is exactly why
nothing in this package will serve a byte without the token.

Two things this module is careful about:

**Parsing is a pure function.**  `parse_tunnel_url` takes text and returns a
URL, so the interesting logic can be tested against captured cloudflared
output without ever starting a tunnel — a test that started one would publish
the user's transcripts to the internet to assert a regex.

**The child dies with the parent.**  A leaked cloudflared keeps a public route
to a local server open after the user thinks they closed it.  Cleanup runs
from a context manager, from an atexit hook, and escalates to SIGKILL, because
the failure mode of "it usually gets cleaned up" is an unnoticed open door.
"""

from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import threading
import time

# Matches only the quick-tunnel hostname.  cloudflared's banner also prints
# a developers.cloudflare.com documentation link, and a looser pattern happily
# hands that back as your tunnel URL.
_URL_RE = re.compile(r"https://[a-zA-Z0-9][a-zA-Z0-9.-]*\.trycloudflare\.com")

INSTALL_HINT = (
    "cloudflared is not installed.\n"
    "\n"
    "  brew install cloudflared\n"
    "\n"
    "Or download it from:\n"
    "  https://developers.cloudflare.com/cloudflare-one/connections/"
    "connect-networks/downloads/\n"
    "\n"
    "Then run `delegate serve --tunnel` again. Without --tunnel the server\n"
    "still works on this machine, at the local URL printed above."
)


class TunnelError(RuntimeError):
    """cloudflared is missing, failed to start, or never produced a URL."""


def parse_tunnel_url(text: str) -> str | None:
    """The quick-tunnel URL from cloudflared's output, or None.

    cloudflared prints its logs to stderr and boxes the URL inside an ASCII
    banner whose borders are on their own lines, so this scans for the
    hostname rather than trying to understand the layout — the banner has
    changed shape across releases and the hostname has not.
    """
    if not text:
        return None
    match = _URL_RE.search(text)
    return match.group(0) if match else None


def find_cloudflared() -> str:
    path = shutil.which("cloudflared")
    if not path:
        raise TunnelError(INSTALL_HINT)
    return path


class Tunnel:
    """A running `cloudflared tunnel --url http://127.0.0.1:<port>`.

    Use as a context manager so the child is reaped on every exit path,
    including the KeyboardInterrupt that is how this program normally ends.
    """

    def __init__(self, port: int, *, binary: str | None = None,
                 spawn=None) -> None:
        self.port = port
        self._binary = binary
        self._spawn = spawn or self._default_spawn
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.log: list[str] = []
        self._lock = threading.Lock()
        self._found = threading.Event()
        self._reader: threading.Thread | None = None

    def _default_spawn(self, argv: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # cloudflared logs to stderr; merge them
            text=True,
            bufsize=1,
            # Its own process group, so a Ctrl-C in the terminal goes to us
            # first and we get to shut it down deliberately instead of racing
            # the signal.
            start_new_session=True,
        )

    def start(self) -> None:
        binary = self._binary or find_cloudflared()
        argv = [
            binary, "tunnel",
            "--no-autoupdate",  # never let it restart itself mid-session
            "--url", f"http://127.0.0.1:{self.port}",
        ]
        try:
            self.proc = self._spawn(argv)
        except OSError as exc:
            raise TunnelError(f"could not start cloudflared: {exc}") from exc

        atexit.register(self.stop)
        self._reader = threading.Thread(target=self._read, daemon=True,
                                        name="cloudflared-log")
        self._reader.start()

    def _read(self) -> None:
        stream = getattr(self.proc, "stdout", None)
        if stream is None:
            return
        for line in stream:
            with self._lock:
                # Bounded: cloudflared is chatty and this process may run for
                # hours. Keeping the head is what matters — the URL and any
                # startup failure are both in the first few lines.
                if len(self.log) < 200:
                    self.log.append(line.rstrip("\n"))
            if self.url is None:
                found = parse_tunnel_url(line)
                if found:
                    self.url = found
                    self._found.set()

    def wait_for_url(self, timeout: float = 30.0) -> str:
        """Block until the URL appears, or explain why it never will."""
        if self._found.wait(timeout) and self.url:
            return self.url
        proc = self.proc
        if proc is not None and proc.poll() is not None:
            tail = "\n".join(self.log[-8:])
            raise TunnelError(
                f"cloudflared exited with status {proc.returncode}.\n{tail}")
        raise TunnelError(
            f"cloudflared did not report a tunnel URL within {timeout:.0f}s. "
            "Check your network, or run without --tunnel to use the local URL.")

    def stop(self) -> None:
        """Terminate, then kill.  Idempotent and safe from atexit."""
        proc = self.proc
        if proc is None:
            return
        self.proc = None
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            proc.kill()
        except OSError:
            pass

    def __enter__(self) -> "Tunnel":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
