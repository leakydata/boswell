"""API keys, kept in one place and never handed back out.

Until now the only key this project needed lived in `.env`, which is fine for
the person who wrote the code and a wall for everybody else: it means finding
a dotfile, knowing the variable's name, and restarting a service. For somebody
who has just installed this to get their Omi recordings back, that is the
point they stop.

So keys can also be entered in the interface and are kept here.

Two rules this file exists to enforce:

**A value goes in and never comes out.** Nothing here returns a secret to the
browser -- the API reports only whether a key is set, where it came from, and
its last four characters, which is enough to tell two keys apart and useless
to anyone who intercepts it.

**The environment wins.** A key exported in `.env` or by systemd is what the
process is actually using, and it cannot be overridden from a web page without
the interface lying about what is in force. Where both exist the environment
is used and the interface says so, rather than silently preferring the value
somebody typed most recently.
"""

import json
import os
import stat

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
PATH = os.path.join(DATA, "secrets.json")

# The keys the interface offers. Anything not listed here cannot be written,
# so a stray POST cannot turn this into a general-purpose file writer.
KEYS = {
    "HF_TOKEN": {
        "label": "Hugging Face",
        "help": "Needed for speaker diarization. Without it every recording "
                "is transcribed with nobody in it, and no voice can be named.",
        "url": "https://huggingface.co/settings/tokens",
    },
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic (Claude)",
        "help": "Lets Claude read finished conversations and write the tasks, "
                "facts, dates and topic tags -- the reviewing this project "
                "otherwise does with a 20B model on your own card. Needed "
                "only if you have no GPU, or want the better reader.",
        "url": "https://console.anthropic.com/settings/keys",
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI",
        "help": "Not used yet. For transcription on a machine with no GPU, "
                "and for notes and tags.",
        "url": "https://platform.openai.com/api-keys",
    },
    "DEEPGRAM_API_KEY": {
        "label": "Deepgram",
        "help": "Not used yet. Transcription only.",
        "url": "https://console.deepgram.com/",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "help": "Not used yet. One key for many models, for notes and tags.",
        "url": "https://openrouter.ai/keys",
    },
    "GROQ_API_KEY": {
        "label": "Groq",
        "help": "Not used yet. Transcription, on a free tier at the time of "
                "writing -- worth a look on a machine with no GPU, where the "
                "local pipeline runs below real time and the backlog never "
                "closes.",
        "url": "https://console.groq.com/keys",
    },
}


def _read():
    try:
        with open(PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write(d):
    atomicio.write_json(PATH, d, indent=2)
    try:
        # Owner only. A file of API keys readable by every account on the
        # machine is a different kind of file from the rest of the archive.
        os.chmod(PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get(name):
    """The value in force: the environment first, then what was entered."""
    return os.environ.get(name) or _read().get(name) or None


def set_key(name, value):
    if name not in KEYS:
        raise ValueError(f"{name} is not a key this program uses")
    d = _read()
    value = (value or "").strip()
    if value:
        d[name] = value
    else:
        d.pop(name, None)          # empty means forget it
    _write(d)
    return bool(value)


def status():
    """What is set and where it came from. Never the values themselves."""
    stored = _read()
    out = []
    for name, meta in KEYS.items():
        env, saved = os.environ.get(name), stored.get(name)
        value = env or saved
        out.append({
            "name": name, "label": meta["label"], "help": meta["help"],
            "url": meta.get("url"),
            "set": bool(value),
            # Which of the two is actually in use, because a stale line in
            # .env silently beating what somebody just typed is exactly the
            # confusion this field exists to prevent.
            "source": "environment" if env else ("saved" if saved else None),
            "also_saved": bool(env and saved),
            # Enough to tell two keys apart and no use to anyone else.
            "hint": ("…" + value[-4:]) if value and len(value) > 8 else None,
        })
    return out
