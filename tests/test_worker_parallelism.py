"""More than one clip in flight, without two tensors through one model.

Half a clip's cost is not on the GPU. Measured over eight clips: ASR,
diarization and sound tagging take 1.36s, and the rest of `_process` --
naming voices, writing the transcript, three indexes -- takes another 1.38s,
during which the card does nothing. Sampling `nvidia-smi` through a real
backlog agreed: idle on eleven of twenty samples.

What these guard is the shape of the fix, because the tempting version of it
is wrong: running two inferences through one WhisperX or pyannote object is
not something either promises to survive. The overlap wanted is one clip's
tail against another's inference.
"""
import os
import sys
import threading

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))


def _src():
    return open(os.path.join(HERE, "..", "web", "pipeline.py")).read()


def test_more_than_one_worker_by_default():
    import pipeline
    assert pipeline.WORKERS >= 2


def test_the_count_can_be_set_and_is_bounded():
    import importlib
    import pipeline
    old = os.environ.get("BOSWELL_WORKERS")
    try:
        for val, want in (("1", 1), ("4", 4), ("99", 8), ("nonsense", 2)):
            os.environ["BOSWELL_WORKERS"] = val
            importlib.reload(pipeline)
            assert pipeline.WORKERS == want, f"{val} gave {pipeline.WORKERS}"
    finally:
        os.environ.pop("BOSWELL_WORKERS", None)
        if old is not None:
            os.environ["BOSWELL_WORKERS"] = old
        importlib.reload(pipeline)


def test_every_model_call_is_serialised():
    """The whole reason a second thread is safe."""
    src = _src()
    body = src[src.index("    def _process(self, clip):"):]
    body = body[:body.index("\n    def ", 10)]
    for call in ("self._words(path, audio)",
                 "self._diar(audio, return_embeddings=True)",
                 "self.tag_sounds(audio)"):
        i = body.index(call)
        before = body[max(0, i - 200):i]
        assert "with self._gpu" in before, (
            f"{call} runs without the GPU lock -- two threads could push "
            f"tensors through one model object at once")


def test_loading_is_guarded():
    """Two threads starting together would both find the models unloaded and
    both load them: twice the VRAM, twenty seconds each."""
    src = _src()
    i = src.index("    def _load(self):")
    assert "self._loadlock" in src[i:i + 400]


def test_the_load_lock_is_not_taken_twice():
    """`_load` calls into the helper loader, and threading.Lock is not
    reentrant -- taking it again would deadlock the first clip of every run.
    The internal calls must use the unlocked form."""
    src = _src()
    body = src[src.index("    def _load_locked(self):"):]
    body = body[:body.index("    def _load_helpers(self):")]
    assert "self._load_helpers()" not in body, (
        "an internal call takes the load lock a second time")
    assert "self._load_helpers_locked()" in body


def test_queue_arithmetic_counts_everything_in_flight():
    import pipeline
    w = pipeline.Worker.__new__(pipeline.Worker)
    import queue as _q
    w.q, w._queued, w._qlock = _q.Queue(), set(), threading.Lock()
    w._busy = set()
    w.notify = lambda *a, **k: None
    w.submit("a.wav")
    w.submit("b.wav")
    w._busy.add("c.wav")
    with w._qlock:
        depth = len(w._queued) + len(w._busy)
    assert depth == 3, "work in flight must count as work outstanding"


def test_a_clip_in_flight_is_not_queued_again():
    import pipeline
    w = pipeline.Worker.__new__(pipeline.Worker)
    import queue as _q
    w.q, w._queued, w._qlock = _q.Queue(), set(), threading.Lock()
    w._busy = {"a.wav"}
    w.notify = lambda *a, **k: None
    assert w.submit("a.wav") is False
    assert w.is_running("a.wav") is True


def test_busy_still_reads_and_writes_like_one_clip():
    """Eight callers were written when there could only be one."""
    import pipeline
    w = pipeline.Worker.__new__(pipeline.Worker)
    w._busy = set()
    assert w.busy is None
    w.busy = "a.wav"
    assert w.busy == "a.wav" and w.is_running("a.wav")
    w.busy = None
    assert w.busy is None and w._busy == set()
