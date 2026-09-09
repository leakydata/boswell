#!/usr/bin/env python3
"""
Boswell from a shell: every MCP tool, as a command.

The MCP server is the right way in for a model that has an MCP client. Not
everything does, and a shell is the one interface that is always there -- so
this exposes exactly the same tools over argv, with no second implementation
to drift out of step. It reflects over the server's own registry, which means
a tool added there is a command here the same day, with the same name, the
same arguments and the same description.

    uv run host/boswell_cli.py                       # what there is
    uv run host/boswell_cli.py unreviewed_conversations
    uv run host/boswell_cli.py search --query "the roof" --limit 5
    uv run host/boswell_cli.py record_fact --subject Nathan \\
        --fact "prefers ..." --clips omi_1788907536.wav

Answers are JSON on stdout, so `| jq` works and so does reading it directly.
A tool that reports a failure exits non-zero, because a shell caller that
cannot tell "nothing matched" from "the server is not running" will treat
both as an empty result.
"""

import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import boswell_mcp                                   # noqa: E402


def _tools():
    return {t.name: t for t in asyncio.run(boswell_mcp.server.list_tools())}


def _coerce(raw, spec):
    """argv is strings; the tool wants what its schema says.

    A list arrives as repeated flags or as one comma-separated value, because
    both are things a person types and neither is worth a stack trace.
    """
    kind = (spec or {}).get("type")
    if isinstance(kind, list):                 # e.g. ["string", "null"]
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "integer":
        return int(raw)
    if kind == "number":
        return float(raw)
    if kind == "boolean":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "array":
        if isinstance(raw, list):
            flat = raw
        else:
            flat = [raw]
        out = []
        for v in flat:
            out.extend(p.strip() for p in str(v).split(",") if p.strip())
        return out
    return raw


def _describe(tools):
    width = max(len(n) for n in tools)
    lines = ["Boswell tools. Run one with:  boswell_cli.py <tool> [--arg value]", ""]
    for name in sorted(tools):
        d = (tools[name].description or "").split(".")[0]
        lines.append(f"  {name:<{width}}  {d}")
    lines += ["", "Add --help after a tool name for its arguments."]
    return "\n".join(lines)


def _parser_for(name, tool):
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    ap = argparse.ArgumentParser(prog=f"boswell_cli.py {name}",
                                 description=tool.description)
    for arg, spec in props.items():
        kind = (spec or {}).get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        ap.add_argument(f"--{arg}",
                        action="append" if kind == "array" else "store",
                        required=arg in required,
                        help=(spec or {}).get("description")
                             or f"{kind or 'string'}"
                                + ("" if arg in required else " (optional)"))
    return ap, props


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    tools = _tools()

    if not argv or argv[0] in ("-h", "--help", "help", "list"):
        print(_describe(tools))
        return 0

    name = argv[0]
    if name not in tools:
        near = [n for n in tools if name in n or n in name]
        print(f"no tool {name!r}." + (f" Did you mean: {', '.join(sorted(near))}?"
                                      if near else " Run with no arguments for the list."),
              file=sys.stderr)
        return 2

    ap, props = _parser_for(name, tools[name])
    ns = ap.parse_args(argv[1:])
    args = {}
    for arg, spec in props.items():
        v = getattr(ns, arg, None)
        if v is None:
            continue
        args[arg] = _coerce(v, spec)

    res = asyncio.run(boswell_mcp.server.call_tool(name, args))
    text = "\n".join(b.text for b in (res.content or []) if getattr(b, "text", None))
    print(text)

    # A failure has to be visible to `&&`, not only readable. Tools here
    # report trouble as data -- {"ok": false} or an "error" key -- rather than
    # by raising, which is right for a model reading the answer and invisible
    # to a shell that only sees an exit code.
    try:
        parsed = json.loads(text)
    except ValueError:
        return 0
    rows = parsed if isinstance(parsed, list) else [parsed]
    for r in rows:
        if isinstance(r, dict) and (r.get("ok") is False or r.get("error")):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
