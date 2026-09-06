"""The agent's model, wherever it runs.

Ollama on this machine is the default and stays it: free, private, nothing
leaves. But a small local model needs a card to be quick, and the machines
this is now aimed at often have neither that nor a 20B model on disk.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import llm


def test_a_tool_result_quotes_the_call_it_answers():
    """OpenAI matches a result to its call by id. Ollama matches by order and
    name, so the loop never had to carry one -- and a result without an id is
    ignored, which reads as a model that chose not to call tools rather than
    as a protocol mistake."""
    out = llm._for_openai([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "record_note", "arguments": {"title": "x"}}}]},
        {"role": "tool", "name": "record_note", "content": "{}"},
    ])
    call_id = out[1]["tool_calls"][0]["id"]
    assert call_id, "the call was not given an id"
    assert out[2]["tool_call_id"] == call_id, "the result does not quote it"


def test_arguments_reach_openai_as_a_string():
    # Ollama hands back a dict; they want JSON text.
    out = llm._for_openai([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": {"a": 1}}}]}])
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str) and json.loads(args) == {"a": 1}


def test_a_string_of_arguments_is_left_alone():
    out = llm._for_openai([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": '{"a": 1}'}}]}])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


def test_two_calls_in_one_turn_keep_their_own_results():
    out = llm._for_openai([
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "a", "arguments": {}}},
            {"function": {"name": "b", "arguments": {}}}]},
        {"role": "tool", "name": "a", "content": "1"},
        {"role": "tool", "name": "b", "content": "2"},
    ])
    ids = [c["id"] for c in out[0]["tool_calls"]]
    assert [out[1]["tool_call_id"], out[2]["tool_call_id"]] == ids


def test_plain_messages_pass_through():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert llm._for_openai(msgs) == msgs


def test_local_needs_no_key_and_the_others_do():
    import secrets_store, tempfile
    secrets_store.PATH = os.path.join(tempfile.mkdtemp(), "secrets.json")
    saved = {k: os.environ.pop(k, None)
             for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY")}
    try:
        assert llm.available("local") is True
        assert llm.available("openai") is False
        assert llm.available("openrouter") is False
        secrets_store.set_key("OPENROUTER_API_KEY", "sk-x")
        assert llm.available("openrouter") is True
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_openrouter_speaks_the_same_protocol():
    # One adapter for both is the whole reason to prefer this shape.
    assert "openrouter" in llm.ENDPOINTS
    assert llm.ENDPOINTS["openrouter"][0].endswith("/chat/completions")


def test_a_model_that_cannot_be_reached_is_not_fatal():
    # The conversation is already transcribed and searchable. What is missed
    # is a round of notes, and the next conversation tries again.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "agent_runner.py")).read()
    assert "except llm.Unavailable" in src
    assert "break" in src[src.index("except llm.Unavailable"):][:400]
