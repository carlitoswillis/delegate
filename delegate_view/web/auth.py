"""The shared secret that stands between your transcripts and the internet.

A Cloudflare quick tunnel publishes a guessable-shaped hostname to anyone who
asks, and the transcripts behind it contain source code, file paths, and
whatever a task file happened to say.  There is no login here and there should
not be one, so the entire security model is a single high-entropy token
checked on every single request.

Three decisions worth the words:

1. The token is generated once and persisted, not derived and not rotated per
   boot.  A URL you texted to your phone has to keep working after the server
   restarts, otherwise the tunnel is unusable for the one thing it is for.

2. The file is written 0600 and created with those bits from the start (not
   chmod'ed after), because a token that spent even a millisecond
   world-readable on a shared machine is a token you have to assume leaked.

3. Comparison is constant-time.  The attack is not sophisticated — it is a
   script hammering the tunnel hostname — but a byte-at-a-time compare over a
   network that leaks timing is a real way to walk a token out of a server,
   and hmac.compare_digest costs nothing to use.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

# 32 bytes of urandom, urlsafe-encoded to ~43 characters.  Long enough that
# online guessing is hopeless, short enough to survive being pasted into a
# phone's address bar or a message to yourself.
TOKEN_BYTES = 32

# The query parameter names accepted on a URL.  `t` is what gets printed and
# texted; `token` is spelled out for anyone typing it by hand.
QUERY_PARAMS = ("t", "token")

COOKIE_NAME = "delegate_token"


def default_token_path() -> Path:
    """Where the token lives.  Overridable so tests never touch ~/.delegate."""
    env = os.environ.get("DELEGATE_WEB_TOKEN")
    if env:
        return Path(env)
    return Path.home() / ".delegate" / "web-token"


def load_or_create_token(path: str | Path | None = None) -> str:
    """The token for this machine, creating and persisting one on first run.

    An existing-but-empty or whitespace-only file is treated as absent and
    replaced.  A zero-byte token file is the shape a crashed first run leaves
    behind, and inheriting it would mean serving with an empty secret — which
    would compare equal to an empty query parameter and let everyone in.
    """
    target = Path(path) if path else default_token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(TOKEN_BYTES)
    target.parent.mkdir(parents=True, exist_ok=True)
    # os.open with 0600 rather than write_text + chmod: the mode has to be in
    # place at creation, not applied to a file that already exists on disk.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time token check that never trusts its inputs to be strings.

    Returns False for None, for the empty string, and for a server that
    somehow has no token — the failure mode of an empty `expected` must be
    "nobody gets in", never "everybody gets in".
    """
    if not expected or not provided:
        return False
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(provided, expected)


def cookie_header(token: str, *, secure: bool, max_age: int = 30 * 24 * 3600) -> str:
    """The Set-Cookie value that keeps navigation clean after the first click.

    HttpOnly because no script here needs to read it and a stored-XSS in a
    transcript should not be able to exfiltrate the token.  SameSite=Lax so a
    cross-site request cannot ride the cookie, while a link tapped from a
    messaging app still arrives authenticated.

    `secure` is passed in rather than assumed: over the tunnel this is https
    and the flag is correct, but on plain http://127.0.0.1 a Secure cookie is
    silently discarded and every page would 401 forever.
    """
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def token_from_cookies(header: str | None) -> str | None:
    """Pull our cookie out of a raw Cookie header, ignoring everything else."""
    if not header:
        return None
    for chunk in header.split(";"):
        name, _, value = chunk.partition("=")
        if name.strip() == COOKIE_NAME:
            return value.strip()
    return None
