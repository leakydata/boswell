#!/usr/bin/env python3
"""
Phase 6 — turn a speaker-labelled transcript into actions via a local LLM.

Pipeline:  wav -> whisperx (+diarize) -> resolve names from speakers.npz
           -> chunk on speaker turns / pauses -> local LLM with tools
           -> append notes / tasks / events / facts under data/agent/

Chunking deliberately follows conversation structure rather than a wall clock:
a chunk ends at a long pause or when it exceeds --chunk-seconds, so the model
sees whole exchanges instead of sentences cut in half.

Usage:
    uv run host/agent.py data/vad_sample.wav
    uv run host/agent.py --transcript data/vad_sample.json --dry-run
"""

import argparse
import json
import os
import sys

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools_impl import REGISTRY, SCHEMAS

OLLAMA = "http://localhost:11434/api/chat"
DB_PATH = "data/speakers.npz"
MATCH_THRESHOLD = 0.60

SYSTEM = """You are a personal assistant reviewing a transcript of a real \
conversation captured by a wearable microphone. Speakers are labelled by name \
where known, or SPEAKER_xx where not.

Extract only what is genuinely useful:
- action items someone committed to  -> add_task
- meetings/deadlines mentioned       -> add_calendar_event
- durable facts about people/projects-> remember_fact
- important context worth keeping    -> add_note

Rules:
- Call tools ONLY for things actually said. Never invent details.
- Skip smalltalk. If nothing is worth saving, save nothing and say so.
- Attribute owners by the speaker name shown in the transcript.
- Do not save the same item twice."""


def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def resolve_names(embeddings):
    """Map SPEAKER_xx -> enrolled name where confident."""
    if not embeddings or not os.path.exists(DB_PATH):
        return {}
    db = np.load(DB_PATH)
    out = {}
    for spk, vec in embeddings.items():
        v = unit(vec)
        best_name, best = None, -1.0
        for name in db.files:
            s = float(v @ unit(db[name]))
            if s > best:
                best_name, best = name, s
        if best >= MATCH_THRESHOLD:
            out[spk] = best_name
    return out


def transcribe(wav, args):
    import whisperx

    model = whisperx.load_model(args.asr_model, args.device,
                                compute_type=args.compute_type, language="en")
    audio = whisperx.load_audio(wav)
    res = model.transcribe(audio, batch_size=16)

    am, meta = whisperx.load_align_model(language_code=res["language"],
                                         device=args.device)
    res = whisperx.align(res["segments"], am, meta, audio, args.device)

    names = {}
    if args.hf_token:
        dia = whisperx.diarize.DiarizationPipeline(
            model_name=args.diar_model, token=args.hf_token, device=args.device)
        df, emb = dia(audio, return_embeddings=True)
        res = whisperx.assign_word_speakers(df, res)
        names = resolve_names(emb or {})
    return res["segments"], names


def chunk(segments, max_seconds, gap_seconds):
    """Split at long pauses, or when a chunk gets too long."""
    chunks, cur = [], []
    for seg in segments:
        if cur:
            gap = seg["start"] - cur[-1]["end"]
            span = seg["end"] - cur[0]["start"]
            if gap >= gap_seconds or span >= max_seconds:
                chunks.append(cur)
                cur = []
        cur.append(seg)
    if cur:
        chunks.append(cur)
    return chunks


def render(segs, names):
    lines = []
    for s in segs:
        spk = s.get("speaker", "")
        who = names.get(spk, spk) if spk else "UNKNOWN"
        lines.append(f"[{s['start']:.0f}s] {who}: {s['text'].strip()}")
    return "\n".join(lines)


def run_agent(text, args):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Transcript excerpt:\n\n{text}"}]
    actions, said = [], ""

    for _ in range(args.max_steps):
        r = requests.post(OLLAMA, json={
            "model": args.model, "messages": messages,
            "tools": SCHEMAS, "stream": False,
            "options": {"temperature": args.temperature},
        }, timeout=args.timeout)
        r.raise_for_status()
        msg = r.json().get("message", {})
        messages.append(msg)
        said = msg.get("content") or said

        calls = msg.get("tool_calls") or []
        if not calls:
            break

        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name")
            raw = fn.get("arguments", {})
            a = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if name not in REGISTRY:
                result = {"ok": False, "error": f"unknown tool {name}"}
            elif args.dry_run:
                result = {"ok": True, "dry_run": True}
            else:
                try:
                    result = REGISTRY[name](**a)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            actions.append((name, a, result))
            messages.append({"role": "tool", "name": name,
                             "content": json.dumps(result)})
    return actions, said


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?")
    ap.add_argument("--transcript", help="reuse a saved .json instead of re-running ASR")
    ap.add_argument("--save-transcript", default=None)
    ap.add_argument("--model", default="glm-4.7-flash:latest")
    ap.add_argument("--asr-model", default="large-v3")
    ap.add_argument("--diar-model", default="pyannote/speaker-diarization-3.1")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"))
    ap.add_argument("--chunk-seconds", type=float, default=180.0)
    ap.add_argument("--gap-seconds", type=float, default=3.0)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.transcript:
        d = json.load(open(args.transcript))
        segments, names = d["segments"], d.get("names", {})
    elif args.wav:
        segments, names = transcribe(args.wav, args)
        if args.save_transcript:
            json.dump({"segments": segments, "names": names},
                      open(args.save_transcript, "w"), indent=2, default=str)
    else:
        ap.error("need a wav or --transcript")

    if names:
        print("identified speakers: " +
              ", ".join(f"{k}->{v}" for k, v in names.items()))

    chunks = chunk(segments, args.chunk_seconds, args.gap_seconds)
    print(f"{len(segments)} segments -> {len(chunks)} chunk(s), model={args.model}"
          + ("  [DRY RUN]" if args.dry_run else ""))

    total = 0
    for i, ch in enumerate(chunks, 1):
        text = render(ch, names)
        print(f"\n--- chunk {i}/{len(chunks)}  "
              f"({ch[0]['start']:.0f}s-{ch[-1]['end']:.0f}s, {len(ch)} segs) ---")
        try:
            actions, said = run_agent(text, args)
        except Exception as e:
            print(f"  agent error: {e}", file=sys.stderr)
            continue
        if not actions:
            print(f"  no actions. model said: {said.strip()[:200]}")
        for name, a, res in actions:
            ok = "ok" if res.get("ok") else "FAIL"
            detail = a.get("title") or a.get("text") or a.get("subject") or ""
            print(f"  [{ok}] {name}: {detail}")
            total += 1

    print(f"\n{total} action(s) total.")
    if not args.dry_run and total:
        print(f"stored under data/agent/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
