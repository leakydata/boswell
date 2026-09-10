"""Where the Boswell server listens, named once.

The port lived as the literal `8000` in two defaults -- the server's bind and
the MCP bridge's loopback URL -- which agree only for as long as somebody
remembers to change both. They are not in the same process and never fail
together: the MCP server is spawned by whatever is using it, so it does not
inherit the systemd unit's environment, and a port change made in one place
leaves every tool answering "connection refused" against a server that is
running perfectly well.

This project has already paid for that shape of bug once, with four separate
copies of a clip horizon.

8740 is deliberately not 8000: that range is what every other development
server reaches for first, and a recorder that has been running since Tuesday
should not be the thing that loses the coin toss. James Boswell was born in
1740, which is the only reason this number is memorable at all.
"""
import os

DEFAULT_PORT = 8740


def port():
    """The port to serve on, or to reach the server at."""
    try:
        return int(os.environ.get("BOSWELL_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def base_url():
    """The running server's address, honouring an explicit BOSWELL_URL.

    A remote or reverse-proxied server is addressed whole; everything else is
    loopback on the port above.
    """
    return os.environ.get("BOSWELL_URL") or f"http://127.0.0.1:{port()}"
