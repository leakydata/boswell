#!/usr/bin/env python3
"""
Transcribe a WAV with WhisperX on the 4090, optionally with diarization.

Diarization needs a HuggingFace token AND manual license acceptance on both
  https://huggingface.co/pyannote/speaker-diarization-3.1
  https://huggingface.co/pyannote/segmentation-3.0
Pass --hf-token or set HF_TOKEN. Without it you still get a transcript,
just no speaker labels.

Usage:
    uv run host/transcribe.py data/test.wav
    uv run host/transcribe.py data/test.wav --diarize
"""

import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--language", default="en")
    ap.add_argument("--diarize", action="store_true")
    ap.add_argument("--diar-model", default="pyannote/speaker-diarization-3.1",
                    help="whisperx now defaults to speaker-diarization-community-1, "
                         "which is separately gated; 3.1 is the open one")
    ap.add_argument("--min-speakers", type=int, default=None)
    ap.add_argument("--max-speakers", type=int, default=None)
    ap.add_argument("--save-embeddings", default=None,
                    help="write per-speaker embeddings to this .npz (Phase 5 enrollment)")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"))
    args = ap.parse_args()

    import whisperx

    print(f"loading {args.model} on {args.device} ...")
    model = whisperx.load_model(
        args.model, args.device, compute_type=args.compute_type, language=args.language
    )

    audio = whisperx.load_audio(args.wav)
    print(f"audio: {len(audio) / 16000:.2f}s")

    result = model.transcribe(audio, batch_size=16)

    # Forced alignment gives word-level timestamps, which is what makes
    # speaker attribution land on the right words later.
    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=args.device
    )
    result = whisperx.align(
        result["segments"], align_model, metadata, audio, args.device
    )

    if args.diarize:
        if not args.hf_token:
            print("\n! --diarize needs --hf-token or HF_TOKEN; skipping.", file=sys.stderr)
        else:
            # API note: this whisperx takes `token=`, not `use_auth_token=`.
            diarize_model = whisperx.diarize.DiarizationPipeline(
                model_name=args.diar_model, token=args.hf_token, device=args.device
            )
            want_emb = args.save_embeddings is not None
            out = diarize_model(
                audio,
                min_speakers=args.min_speakers,
                max_speakers=args.max_speakers,
                return_embeddings=want_emb,
            )
            if want_emb:
                diar_df, embeddings = out
                if embeddings:
                    import numpy as np
                    np.savez(args.save_embeddings,
                             **{k: np.asarray(v) for k, v in embeddings.items()})
                    print(f"  saved {len(embeddings)} speaker embeddings -> "
                          f"{args.save_embeddings}")
            else:
                diar_df = out
            result = whisperx.assign_word_speakers(diar_df, result)

    print("\n" + "=" * 60)
    for seg in result["segments"]:
        who = seg.get("speaker", "")
        tag = f"[{who}] " if who else ""
        print(f"{seg['start']:7.2f}  {tag}{seg['text'].strip()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
