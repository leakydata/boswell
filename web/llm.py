"""One chat call, whichever model is answering it.

The agent that writes notes, facts and tags was wired straight to Ollama on
localhost. That is the right default -- it is free, it is private, and nothing
leaves the machine -- and it is not a choice everybody can make: a small local
model needs a GPU to be quick, and the machines this project is now aimed at
often have neither the card nor a 20B model sitting on disk.

So the same loop can talk to an OpenAI-compatible endpoint instead. OpenRouter
speaks that protocol too, which is one adapter for both.

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


class Unavailable(RuntimeError):
    """The model could not be reached or refused. The caller decides whether
    that is fatal; for the agent it never is -- it writes nothing this round
    and tries again on the next conversation."""


def available(backend):
    if backend == "local":
        return True          # decided by whether Ollama answers, not by a key
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
