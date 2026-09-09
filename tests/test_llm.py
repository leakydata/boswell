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


# ---- Claude ---------------------------------------------------------------
#
# The Anthropic API is not the OpenAI protocol wearing a different hostname,
# so unlike OpenRouter it needed a real translation. These pin the three
# places that translation can be wrong without failing loudly.


def test_claude_is_a_backend_the_server_will_accept():
    # `ENDPOINTS` stopped being the list of backends when Claude arrived, and
    # anything still validating against it refuses Claude with "unknown
    # backend" -- a message that sends you looking in the wrong file.
    assert "anthropic" in llm.BACKENDS
    assert "anthropic" not in llm.ENDPOINTS
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert "tuple(llm.ENDPOINTS)" not in src


def test_the_system_prompt_is_lifted_out_of_the_messages():
    # It is a parameter there, not a message. Left in the list it is either
    # rejected or read as something the user said.
    system, msgs = llm._for_claude([
        {"role": "system", "content": "you review transcripts"},
        {"role": "user", "content": "here is one"}])
    assert system == "you review transcripts"
    assert msgs == [{"role": "user", "content": "here is one"}]


def test_a_tool_result_quotes_the_call_it_answers():
    """Ollama pairs a result to its call by order; Claude pairs by id, and a
    result whose id does not match is an error rather than a mismatch you can
    see in the output."""
    system, msgs = llm._for_claude([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "toolu_1", "function":
                         {"name": "tag_topics", "arguments": {"topics": ["x"]}}}]},
        {"role": "tool", "name": "tag_topics", "content": '{"ok": true}'}])
    assert msgs[-1]["role"] == "user"
    block = msgs[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_1"


def test_results_for_one_turn_arrive_in_one_message():
    # Every result answering a single assistant turn has to be in the same
    # user message. Split across two, the second has no call left to answer.
    _, msgs = llm._for_claude([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "add_note", "arguments": {}}},
            {"id": "b", "function": {"name": "tag_topics", "arguments": {}}}]},
        {"role": "tool", "name": "add_note", "content": "{}"},
        {"role": "tool", "name": "tag_topics", "content": "{}"}])
    assert len(msgs) == 3, "the two results were sent as separate turns"
    assert [b["tool_use_id"] for b in msgs[-1]["content"]] == ["a", "b"]


def test_thinking_is_replayed_rather_than_rebuilt():
    """A following turn in a tool-use exchange has to carry the thinking
    blocks back unchanged, signature included. Nothing in the flattened
    message shape can reconstruct one, so the raw blocks ride along on the
    message and are sent back verbatim."""
    raw = [{"type": "thinking", "thinking": "", "signature": "sig..."},
           {"type": "tool_use", "id": "toolu_1", "name": "add_note", "input": {}}]
    _, msgs = llm._for_claude([
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "_claude": raw,
         "tool_calls": [{"id": "toolu_1",
                         "function": {"name": "add_note", "arguments": {}}}]}])
    assert msgs[-1]["content"] is raw


def test_an_empty_assistant_turn_is_dropped_not_sent():
    # A message with no content at all is rejected, and there was nothing in
    # it to replay.
    _, msgs = llm._for_claude([{"role": "user", "content": "u"},
                               {"role": "assistant", "content": ""}])
    assert msgs == [{"role": "user", "content": "u"}]


def test_a_tool_schema_survives_the_rename():
    t = {"type": "function", "function": {
        "name": "add_task", "description": "save one",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
    got = llm._claude_tool(t)
    assert got["name"] == "add_task"
    assert got["input_schema"] == t["function"]["parameters"]
    assert "parameters" not in got


def test_a_decline_is_reported_not_routed_around():
    """Server-side fallbacks would rerun a refused review on another model
    inside the same call. For an archive of somebody's own conversations that
    is the wrong trade: what was recorded, and by which model, is the point.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "web", "llm.py")).read()
    fn = src[src.index("def _claude("):]
    fn = fn[:fn.index("\ndef ")]
    assert 'r.stop_reason == "refusal"' in fn
    assert "fallbacks=" not in fn


def test_claude_needs_a_key_like_any_other_hosted_backend():
    import secrets_store, tempfile
    secrets_store.PATH = os.path.join(tempfile.mkdtemp(), "secrets.json")
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert llm.available("anthropic") is False
        secrets_store.set_key("ANTHROPIC_API_KEY", "sk-ant-x")
        assert llm.available("anthropic") is True
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
