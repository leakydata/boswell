"""Where the server listens, and the fact that everything agrees about it.

The port was a literal in two defaults that are never in the same process:
the server's bind and the MCP bridge's loopback. They fail apart silently --
a tool reports "connection refused" against a server that is running
perfectly well -- so what is worth testing is not the number but that there
is only one of it.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))
sys.path.insert(0, os.path.join(HERE, "..", "host"))

import netcfg


def test_not_the_port_every_other_dev_server_wants():
    """8000 is contested; a recorder running since Tuesday should not have to
    win a coin toss against whatever was started this afternoon."""
    assert netcfg.DEFAULT_PORT != 8000
    assert netcfg.DEFAULT_PORT not in (3000, 5000, 8080, 8888, 9000)


def test_the_environment_wins():
    old = os.environ.get("BOSWELL_PORT")
    try:
        os.environ["BOSWELL_PORT"] = "9999"
        assert netcfg.port() == 9999
        assert netcfg.base_url() == "http://127.0.0.1:9999"
    finally:
        os.environ.pop("BOSWELL_PORT", None)
        if old is not None:
            os.environ["BOSWELL_PORT"] = old


def test_nonsense_in_the_environment_does_not_take_the_server_down():
    old = os.environ.get("BOSWELL_PORT")
    try:
        os.environ["BOSWELL_PORT"] = "notaport"
        assert netcfg.port() == netcfg.DEFAULT_PORT
        os.environ["BOSWELL_PORT"] = ""
        assert netcfg.port() == netcfg.DEFAULT_PORT
    finally:
        os.environ.pop("BOSWELL_PORT", None)
        if old is not None:
            os.environ["BOSWELL_PORT"] = old


def test_a_whole_url_addresses_a_server_somewhere_else():
    old = os.environ.get("BOSWELL_URL")
    try:
        os.environ["BOSWELL_URL"] = "http://10.0.0.19:1234"
        assert netcfg.base_url() == "http://10.0.0.19:1234"
    finally:
        os.environ.pop("BOSWELL_URL", None)
        if old is not None:
            os.environ["BOSWELL_URL"] = old


def test_nobody_kept_their_own_copy_of_the_port():
    """The bug this module exists to prevent, asserted directly."""
    root = os.path.join(HERE, "..")
    for rel in ("web/server.py", "host/boswell_mcp.py"):
        with open(os.path.join(root, rel)) as f:
            body = [ln for ln in f if not ln.lstrip().startswith("#")]
        text = "".join(body)
        assert "BOSWELL_PORT" not in text, (
            f"{rel} reads BOSWELL_PORT itself instead of asking netcfg")
        assert '"8000"' not in text and "'8000'" not in text, (
            f"{rel} still carries a literal port")
