"""One chat call, whichever model is answering it.

The agent that writes notes, facts and tags was wired straight to Ollama on
localhost. That is the right default -- it is free, it is private, and nothing
leaves the machine -- and it is not a choice everybody can make: a small local
model needs a GPU to be quick, and the machines this project is now aimed at
often have neither the card nor a 20B model sitting on disk.

So the same loop can talk to an OpenAI-compatible endpoint instead. OpenRouter
speaks that protocol too, which is one adapter for both.

Claude is the third, and it is not that protocol. The Anthropic API keeps the
system prompt out of the message list, describes a tool by `input_schema`
rather than `function.parameters`, and answers with a list of content blocks
instead of a string beside a list of calls. That is a real translation rather
than a header swap, and it lives in `_for_claude` and `_from_claude` below so
the agent loop keeps seeing exactly one message shape.

One thing there is worth knowing about. When Claude thinks, the reasoning
comes back as a block of its own, and a following turn in the same tool-use
exchange has to carry those blocks back unchanged or the request is refused.
Reconstructing them from the flattened Ollama shape is impossible -- the
signature is gone -- so the raw blocks ride along on the returned message
under `_claude` and are sent back verbatim. The agent loop appends whatever
it is given and never looks inside, which is why this works without touching
it.

What differs between them is smaller than it looks. Both take `messages` and
`tools`, and the tool schemas this project already uses are in OpenAI's
`{"type": "function", "function": {...}}` shape, so they pass through
untouched. The one real difference is that OpenAI tracks a tool call by an id
and requires the result to quote it, while Ollama matches on order and name.
Getting that wrong does not fail loudly -- the model simply stops calling
tools -- so it is handled here and the agent loop never sees it.
"""

import json
import urllib.error
import urllib.request

import secrets_store

OLLAMA = "http://localhost:11434/api/chat"
ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions",
                   "OPENROUTER_API_KEY"),
}
CLAUDE_KEY = "ANTHROPIC_API_KEY"
# Everything the agent may be pointed at. `ENDPOINTS` is no longer the whole
# list, so anything validating a backend against it would silently refuse
# Claude.
BACKENDS = ("local",) + tuple(ENDPOINTS) + ("anthropic",)

# The model list is short on purpose: these are the ones worth pointing at a
# transcript, cheapest last. Opus reads a conversation the way a person would
# -- it notices that a commitment was walked back two lines later -- and the
# archive is small enough that the difference costs cents a day. Haiku is
# there for somebody running this over a much larger archive who wants tags
# more than judgement.
CLAUDE_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
CLAUDE_DEFAULT = "claude-opus-5"
# What to offer on OpenRouter, best value first.
#
# Reviewing a conversation is a reading job with a fixed ~2,800-token prefix
# of system prompt and tool schemas, and the transcript after it. That shape
# rewards a cheap long-context model far more than it rewards a frontier one:
# measured per-token at the time of writing, deepseek-v4-pro is $0.96/$1.91
# per million against Opus 5's $5/$25 -- about five times cheaper to read and
# thirteen times cheaper to write, which takes a conversation from roughly
# five cents to one, and the whole archive from ten dollars to two. It carries
# a million tokens of context and does tool calling, which is all this loop
# asks of a model.
#
# The Claude entries stay because they are the better reader when a
# conversation is worth it, and because a lot of people arriving at this
# project have an OpenRouter key and no Anthropic one.
OPENROUTER_MODELS = ("deepseek/deepseek-v4-pro",
                     "anthropic/claude-opus-5", "anthropic/claude-sonnet-5",
                     "anthropic/claude-haiku-4.5")
# The old name, kept pointing at the Claude half so nothing that meant
# "Claude through OpenRouter" silently starts meaning something else.
CLAUDE_VIA_OPENROUTER = tuple(m for m in OPENROUTER_MODELS
                              if m.startswith("anthropic/"))
# Room for a full round of tool calls. Thinking is billed and counted inside
# this, so it is not the size of the answer.
CLAUDE_MAX_TOKENS = 16000


class Unavailable(RuntimeError):
    """The model could not be reached or refused. The caller decides whether
    that is fatal; for the agent it never is -- it writes nothing this round
    and tries again on the next conversation."""


def available(backend):
    if backend == "local":
        return True          # decided by whether Ollama answers, not by a key
    if backend == "anthropic":
        return bool(secrets_store.get(CLAUDE_KEY))
    spec = ENDPOINTS.get(backend)
    return bool(spec and secrets_store.get(spec[1]))


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.load(e).get("error") or {}).get("message", "")
        except Exception:
            pass
        raise Unavailable(f"{e.code}" + (f": {detail[:140]}" if detail else ""))
    except Exception as e:
        raise Unavailable(f"{type(e).__name__}: {str(e)[:140]}")


def chat(backend, model, messages, tools, timeout=600):
    """One turn. Returns Ollama's message shape whatever answered.

        {"role": "assistant", "content": str, "tool_calls": [...]}

    Ollama's shape rather than OpenAI's because that is what the agent loop
    already consumes, and a loop that has been correct for months is not
    worth rewriting to suit a second provider.
    """
    if backend == "anthropic":
        return _claude(model, messages, tools, timeout)

    if backend == "local":
        d = _post(OLLAMA, {"model": model, "messages": messages,
                           "tools": tools, "stream": False,
                           "options": {"temperature": 0.2}}, {}, timeout)
        return d.get("message") or {}

    spec = ENDPOINTS.get(backend)
    if not spec:
        raise Unavailable(f"unknown backend {backend!r}")
    url, key_name = spec
    key = secrets_store.get(key_name)
    if not key:
        raise Unavailable(f"no key for {backend} — Settings → API keys")

    d = _post(url, {"model": model, "messages": _for_openai(messages),
                    "tools": tools, "temperature": 0.2},
              {"Authorization": f"Bearer {key}"}, timeout)
    try:
        msg = d["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise Unavailable("no answer in the response")
    return {"role": "assistant", "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or []}


def _for_openai(messages):
    """Give tool results the id of the call they answer.

    Ollama matches a result to its call by position and name. OpenAI matches
    by `tool_call_id`, and a result without one is rejected or ignored -- the
    model just stops calling tools, which reads as a model that decided not
    to rather than a protocol mistake.
    """
    out, pending = [], []
    for m in messages:
        if m.get("role") == "tool":
            call_id = pending.pop(0) if pending else None
            m = dict(m)
            if call_id:
                m["tool_call_id"] = call_id
            # OpenAI has no use for it and some gateways reject unknown keys.
            m.pop("name", None)
            out.append(m)
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for i, c in enumerate(m["tool_calls"]):
                fn = c.get("function", {})
                cid = c.get("id") or f"call_{len(out)}_{i}"
                pending.append(cid)
                args = fn.get("arguments")
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name"),
                    # They want a JSON string; Ollama hands back a dict.
                    "arguments": args if isinstance(args, str)
                                 else json.dumps(args or {})}})
            m = dict(m, tool_calls=calls)
            m.setdefault("content", m.get("content") or "")
        out.append(m)
    return out


# ---- Claude ---------------------------------------------------------------
#
# Three shapes have to line up: what the agent loop speaks (Ollama's), what
# the Anthropic API takes, and what it gives back. Each direction is one
# function below, and none of them knows anything about this project.


def _claude(model, messages, tools, timeout):
    """One turn on the Anthropic API, answered in Ollama's message shape."""
    try:
        import anthropic
    except ImportError:
        raise Unavailable("the anthropic package is not installed "
                          "(pip install anthropic)")

    key = secrets_store.get(CLAUDE_KEY)
    if not key:
        raise Unavailable("no key for anthropic — Settings → API keys")

    client = anthropic.Anthropic(api_key=key, timeout=float(timeout))
    system, convo = _for_claude(messages)
    try:
        r = client.messages.create(
            model=model or CLAUDE_DEFAULT,
            max_tokens=CLAUDE_MAX_TOKENS,
            # The tools and the system prompt are identical on every review
            # and the transcript is not, so the breakpoint goes at the end of
            # the stable part. Caching is a prefix match and the order on the
            # wire is tools, then system, then messages -- so one mark here
            # covers both of the things that repeat.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}] if system else None,
            messages=convo,
            tools=[_claude_tool(t) for t in tools] or None,
            # Deciding what in an hour of talk is worth keeping, and whether
            # it is already recorded, is the kind of judgement this is for.
            thinking={"type": "adaptive"},
        )
    except anthropic.APIStatusError as e:
        raise Unavailable(f"{e.status_code}: {str(getattr(e, 'message', e))[:140]}")
    except anthropic.APIConnectionError as e:
        raise Unavailable(f"{type(e).__name__}: {str(e)[:140]}")

    # A decline is reported rather than routed around. Server-side fallbacks
    # would rerun this on another model inside the same call, and an archive
    # of somebody's own conversations is the wrong place for a silent change
    # of author: what was recorded, and by which model, is the whole point.
    if r.stop_reason == "refusal":
        cat = getattr(r.stop_details, "category", None) if r.stop_details else None
        raise Unavailable(f"declined this transcript ({cat or 'no category given'})")
    return _from_claude(r)


def _claude_tool(t):
    """An OpenAI function schema as a Claude tool. Same fields, other names."""
    fn = t.get("function", t)
    return {"name": fn.get("name"),
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters")
                            or {"type": "object", "properties": {}}}


def _from_claude(r):
    """A Claude response as the message the agent loop already consumes.

    `_claude` carries the raw blocks. They are the thinking blocks and their
    signatures, which cannot be rebuilt from anything else here and which the
    next request in a tool-use exchange has to send back unchanged.
    """
    text, calls = [], []
    for b in r.content:
        if b.type == "text":
            text.append(b.text)
        elif b.type == "tool_use":
            calls.append({"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": b.input}})
    return {"role": "assistant", "content": "".join(text),
            "tool_calls": calls,
            "_claude": [b.model_dump() for b in r.content]}


def _for_claude(messages):
    """The loop's messages as (system, messages) for the Anthropic API.

    Three differences to absorb:

    - The system prompt is a parameter there, not a message. Every system
      message is lifted out and joined.
    - A tool result is a block inside a *user* message quoting the id of the
      call it answers -- and every result for one assistant turn has to be in
      the same message, so consecutive results are gathered rather than sent
      one per turn. Ollama pairs by order, so the ids are recovered the same
      way `_for_openai` does it.
    - An assistant turn that came from Claude is replayed from its raw blocks
      when they are there, which keeps thinking intact.
    """
    system, out, pending = [], [], []
    for m in messages:
        role = m.get("role")

        if role == "system":
            if m.get("content"):
                system.append(m["content"])
            continue

        if role == "tool":
            cid = pending.pop(0) if pending else None
            block = {"type": "tool_result",
                     "tool_use_id": cid or "unknown",
                     "content": m.get("content") or ""}
            last = out[-1] if out else None
            if (last and last["role"] == "user"
                    and isinstance(last["content"], list)
                    and last["content"][0].get("type") == "tool_result"):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            for i, c in enumerate(m.get("tool_calls") or []):
                pending.append(c.get("id") or f"call_{len(out)}_{i}")
            raw = m.get("_claude")
            if raw:
                out.append({"role": "assistant", "content": raw})
                continue
            blocks = []
            if (m.get("content") or "").strip():
                blocks.append({"type": "text", "text": m["content"]})
            for i, c in enumerate(m.get("tool_calls") or []):
                fn = c.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                blocks.append({"type": "tool_use",
                               "id": c.get("id") or f"call_{len(out)}_{i}",
                               "name": fn.get("name"),
                               "input": args or {}})
            # An assistant turn with nothing in it is rejected, and there is
            # nothing to replay anyway.
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        out.append({"role": "user", "content": m.get("content") or ""})

    return "\n\n".join(system), out
