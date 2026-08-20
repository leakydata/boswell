I want you to investigate and prototype an alternative architecture for this project:

https://github.com/leakydata/boswell

The goal is to determine whether the existing WhisperX -> pyannote -> text LLM pipeline can be replaced, fully or partially, with Google's newest audio-capable Gemma 4 model by feeding recorded audio directly to Gemma 4.

IMPORTANT SAFETY REQUIREMENT:

Do NOT modify, overwrite, delete, or restructure my existing working Boswell installation.

Before making any code changes:

1. Inspect the current repository and understand its architecture.
2. Check the current git status.
3. Preserve all current work, including uncommitted work.
4. Create a separate experimental git branch named something similar to:

experiment/gemma4-native-audio

5. Preferably create a separate git worktree in a sibling directory such as:

../boswell-gemma4

The original Boswell directory must remain usable and unchanged.

If a git worktree is inappropriate for some reason, create a completely separate experimental copy instead, but do not modify the original project.

Do not perform destructive git operations such as reset --hard, clean -fd, force checkout, or anything else that could destroy existing work.

## FIRST: RESEARCH THE CURRENT GEMMA 4 IMPLEMENTATION

Before writing the prototype, verify from current official Google/Hugging Face documentation:

- Which Gemma 4 models actually support audio input.
- Whether Gemma 4 12B Unified is the appropriate model for this use.
- Whether E2B or E4B also support audio.
- Exact audio input requirements.
- Sampling rate requirements.
- Maximum audio duration per request.
- Whether raw waveform input is supported through Hugging Face Transformers.
- Which Transformers version is required.
- Expected GPU VRAM requirements.
- Whether quantized inference is supported.
- Whether Gemma 4 can perform:
  - speech recognition/transcription
  - speaker diarization
  - speaker-change detection
  - semantic understanding of audio
  - structured JSON extraction
- Any limitations in using Gemma 4 for continuous conversational audio.

Do not blindly assume previous claims about Gemma 4 are correct. Verify them against the current documentation and tell me if anything differs.

## SECOND: UNDERSTAND BOSWELL

Inspect Boswell and identify exactly where the existing pipeline performs:

- BLE audio capture
- ADPCM decoding
- WAV/audio creation
- voice activity detection
- WhisperX transcription
- word timestamps
- pyannote diarization
- speaker embedding generation
- persistent speaker identification
- conversation segmentation
- LLM analysis
- summaries
- facts/memories
- task extraction
- web/API handling
- database/storage

Produce a concise architecture map showing the relevant files, functions, classes, and data flow.

Do not rewrite anything yet.

## THIRD: DESIGN AN EXPERIMENTAL GEMMA 4 PIPELINE

I want to test a pipeline conceptually like:

wearable audio
    |
    v
existing Boswell audio capture/decoding
    |
    v
short audio chunk
    |
    v
Gemma 4 native audio
    |
    +--> transcript
    +--> speaker turns
    +--> semantic analysis
    +--> summary
    +--> facts
    +--> tasks
    +--> entities
    +--> important dates/times
    |
    v
Boswell storage/UI

The first prototype should run LOCALLY on an NVIDIA CUDA GPU.

Do not implement cloud deployment yet.

Keep this experiment as isolated from the original pipeline as reasonably possible.

I would prefer a structure such as:

boswell-gemma4/
    gemma4_experiment/
        __init__.py
        config.py
        audio.py
        model.py
        prompts.py
        schemas.py
        pipeline.py
        benchmark.py
        cli.py
        README.md

You may adapt this structure if the existing Boswell architecture suggests a better design.

## FOURTH: DO NOT REMOVE WHISPERX OR PYANNOTE

The existing pipeline must remain intact.

Implement Gemma 4 as an alternative backend or experimental pipeline.

I want to be able to compare:

A. Existing Boswell:
audio -> WhisperX -> pyannote -> LLM

versus

B. Experimental:
audio -> Gemma 4

versus potentially

C. Hybrid:
audio -> Gemma 4
      -> separate speaker embedding model for persistent identity

The existing system should remain runnable without Gemma 4.

Use configuration or command-line switches rather than replacing the old implementation.

For example, something conceptually like:

--pipeline legacy

and:

--pipeline gemma4

Do not force this exact syntax if something cleaner fits the codebase.

## FIFTH: STRUCTURED GEMMA OUTPUT

Create a strict structured output schema.

I want Gemma to return data equivalent to something like:

{
  "transcript": "...",
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 4.2,
      "text": "..."
    }
  ],
  "summary": "...",
  "facts": [],
  "tasks": [],
  "commitments": [],
  "people": [],
  "organizations": [],
  "places": [],
  "dates_and_times": [],
  "questions": [],
  "follow_up_items": [],
  "notable_audio_events": []
}

Use Pydantic models if that fits the existing project.

Gemma output is untrusted model output, so validate and recover gracefully from malformed JSON.

Do not allow bad model output to crash the recording pipeline.

## SIXTH: AUDIO HANDLING

Reuse Boswell's existing audio decoding where practical.

Gemma should receive audio in whatever exact waveform format its current official implementation requires.

If Boswell's BLE stream uses compressed ADPCM:

- preserve the original compressed audio where useful
- decode only when necessary for Gemma
- avoid unnecessarily writing giant temporary WAV files
- use in-memory audio processing when practical

Create a clean function conceptually similar to:

analyze_audio(audio_samples, sample_rate) -> GemmaAnalysis

The Gemma-specific code should not need to know anything about BLE.

This separation is important because eventually I may replace the Linux BLE receiver with an Android BLE gateway that uploads audio to a cloud service.

## SEVENTH: TEST MULTIPLE GEMMA MODELS IF POSSIBLE

Make model selection configurable.

I want to be able to benchmark available audio-capable Gemma 4 models, especially:

- the smallest useful audio model
- Gemma 4 E4B if audio-capable
- Gemma 4 12B Unified if audio-capable

Do not hard-code a model unless necessary.

Something like:

GEMMA_MODEL=...

should be easy to change.

If model names in this prompt are inaccurate, use the actual current model identifiers from Google's documentation.

## EIGHTH: CREATE A SIMPLE STANDALONE TEST FIRST

Before integrating deeply into Boswell, create a standalone command-line test.

Example behavior:

python -m gemma4_experiment.cli recording.wav

It should:

1. Load the audio.
2. Convert/resample it as required.
3. Send it through Gemma 4.
4. Print the structured result.
5. Save the JSON output next to the source recording or in a results directory.
6. Report:
   - model loading time
   - audio duration
   - inference time
   - real-time factor
   - peak GPU VRAM if practical
   - tokens generated if available

I want to know whether this architecture is actually practical before it becomes entangled with the rest of Boswell.

## NINTH: BUILD A COMPARISON/BENCHMARK TOOL

Create a benchmark that can run the same Boswell recording through:

LEGACY:
WhisperX + pyannote

and:

GEMMA:
Gemma 4 native audio

Then compare output.

At minimum save:

legacy_result.json
gemma4_result.json

And a readable comparison report.

Compare:

- transcription accuracy qualitatively
- missed words
- hallucinated words
- speaker-change accuracy
- number of speakers
- timestamps
- names
- dates
- task extraction
- semantic summary quality
- GPU memory
- processing time
- real-time factor

If a proper objective metric cannot be calculated without human ground truth, say so rather than inventing one.

## TENTH: SPEAKER DIARIZATION VS SPEAKER IDENTITY

Be very careful about this distinction.

"Diarization" means:

SPEAKER_00 said this.
SPEAKER_01 said that.

"Persistent speaker identification" means:

SPEAKER_00 is Nathan.
SPEAKER_01 is Dan.

Even if Gemma can perform diarization, do not assume it can reliably identify the same human across unrelated recordings.

Inspect Boswell's current speaker-embedding/voiceprint implementation.

Determine whether we should retain the existing embedding system while allowing Gemma to perform transcription and semantic analysis.

A likely hybrid architecture may be:

audio
   |
   +--> Gemma 4
   |       transcript
   |       speaker segmentation
   |       semantic understanding
   |
   +--> speaker embedding model
           persistent voice identity

Investigate whether this is technically reasonable.

## ELEVENTH: HANDLE THE 30-SECOND OR OTHER AUDIO LIMIT INTELLIGENTLY

Verify Gemma's current maximum audio duration.

If there is a short input limit, design chunking with overlap.

For example:

chunk 1: 0-30 sec
chunk 2: 27-57 sec
chunk 3: 54-84 sec

But determine an appropriate overlap experimentally.

The merging system must avoid duplicating words or tasks from the overlap.

Maintain absolute timestamps across chunks if possible.

Also consider voice-activity-based chunking rather than blindly cutting speech in the middle of sentences.

Do not implement an overly complex solution until the basic standalone test works.

## TWELFTH: PROMPT ENGINEERING

Create a system/instruction prompt optimized for Gemma's audio capabilities.

The model should:

- transcribe accurately
- never invent speech it cannot hear
- label uncertain portions explicitly
- distinguish speakers when possible
- preserve important technical terminology
- capture numbers accurately
- capture names accurately
- recognize commands/tasks
- identify commitments
- identify deadlines
- summarize conservatively
- not infer facts unsupported by the audio
- produce strict machine-readable output

Since Boswell may record technical conversations, discourage the model from "correcting" unusual terminology merely because it sounds unfamiliar.

## THIRTEENTH: PRIVACY AND LOCAL-FIRST DESIGN

Do not upload recordings anywhere during this experiment.

Run Gemma locally.

Do not silently call Google APIs, Hugging Face inference APIs, OpenAI APIs, or any other hosted inference service.

Downloading model weights from an official model repository is acceptable.

If authentication or a Hugging Face token is needed to download the model, explain what is needed rather than embedding credentials.

Never commit tokens, API keys, audio recordings, model weights, or secrets to git.

Update .gitignore inside the experimental branch/worktree as appropriate.

## FOURTEENTH: DEPENDENCIES

Do not unnecessarily destroy or modify Boswell's current Python environment.

Prefer an isolated environment for the experiment.

Create an appropriate requirements file or pyproject dependency group specifically for Gemma 4.

Document:

- Python version
- PyTorch version
- CUDA version
- Transformers version
- Accelerate version
- audio dependencies
- optional quantization dependencies

Before installing a package that could significantly alter the existing environment, explain why.

If feasible, create a separate virtualenv/conda environment instead.

## FIFTEENTH: GPU MEMORY OPTIONS

I want the prototype to be suitable eventually for inexpensive rented GPUs.

Investigate and support, where practical:

- bf16
- fp16
- 8-bit quantization
- 4-bit quantization

Do not assume quantization works correctly with Gemma 4's audio path. Verify it.

Report approximate VRAM requirements for each model/configuration tested.

## SIXTEENTH: FAILURE FALLBACK

Eventually I may want this behavior:

Gemma succeeds:
    use Gemma result

Gemma fails:
    fall back to WhisperX/pyannote

Design the experiment so that adding this later is straightforward.

Do not make the prototype more complicated than necessary just to implement fallback immediately.

## SEVENTEENTH: DOCUMENT EVERYTHING

Create:

gemma4_experiment/README.md

Explain:

- what was added
- why it was added
- exact setup commands
- how to download/access the model
- how to run a single WAV test
- how to run the benchmark
- how to change models
- how to switch precision/quantization
- expected VRAM
- current limitations
- what remains experimental
- what parts of Boswell remain untouched

Also create:

gemma4_experiment/FINDINGS.md

Record what you learn while testing.

Include both positive and negative findings.

If Gemma performs worse than WhisperX for this project, say so clearly.

## WORK ORDER

Proceed in this order:

PHASE 1
Inspect Boswell and research the actual current Gemma 4 audio API and capabilities.

PHASE 2
Report your findings and proposed integration points.

PHASE 3
Create the isolated branch/worktree.

PHASE 4
Implement the smallest possible standalone Gemma 4 audio test.

PHASE 5
Run it against one existing Boswell recording if a suitable recording is available locally.

PHASE 6
Fix errors and confirm inference works.

PHASE 7
Add structured output.

PHASE 8
Create the comparison benchmark.

PHASE 9
Only after all of that, consider wiring the experimental backend into Boswell itself.

Do not perform a large refactor upfront.

Get one WAV file successfully processed by Gemma first.

## IMPORTANT DEVELOPMENT STYLE

- Read existing code before changing it.
- Reuse existing Boswell utilities where sensible.
- Do not duplicate major functionality unnecessarily.
- Keep the experiment modular.
- Use type hints.
- Use logging rather than excessive print statements in library code.
- Handle CUDA out-of-memory errors gracefully.
- Validate paths and input audio.
- Do not silently discard exceptions.
- Keep Windows/Linux portability where practical.
- Do not add Unicode characters to source code.
- Prefer clear Python over clever abstraction.
- Do not rewrite unrelated parts of Boswell.
- Keep commits small and logically grouped.

At the end, give me:

1. Your verified findings about Gemma 4 audio support.
2. The architecture of the current Boswell audio pipeline.
3. Exactly which files you created or changed.
4. The commands needed to run the experiment.
5. Which Gemma model you recommend for Boswell.
6. Approximate GPU VRAM required.
7. Inference speed relative to audio duration.
8. Whether Gemma can realistically replace WhisperX.
9. Whether Gemma can realistically replace pyannote.
10. Whether persistent speaker embeddings should remain.
11. Any major problems or limitations you discovered.
12. Your recommendation for the next step.

Most importantly: preserve the original working Boswell system. This is an experiment, not a migration.