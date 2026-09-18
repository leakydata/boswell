"""Colours you can read, in the theme you actually chose.

Two faults found by review 2026-09-17:

The dark palette existed only inside `@media (prefers-color-scheme: dark)`.
Choosing Dark set `color-scheme` -- native controls and scrollbars went dark
-- and left every colour on the page light, so the toggle appeared to do
almost nothing unless your OS was already dark.

And three text/background pairs were below 4.5:1, on styles carrying
timestamps, durations and action labels: light faint text 2.68:1, dark faint
3.67:1, and the primary button's white label on the dark theme's pale teal
accent, 2.44:1.
"""
import os
import re

HERE = os.path.dirname(__file__)
UI = os.path.join(HERE, "..", "web", "static", "index.html")


def _css():
    return open(UI).read()


def _vars(block):
    return dict(re.findall(r"--([a-z-]+)\s*:\s*(#[0-9A-Fa-f]{6})", block))


def _lum(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _theme(which):
    s = _css()
    if which == "light":
        blk = s[s.index(":root{"):s.index("@media (prefers-color-scheme:dark)")]
    else:
        i = s.index(":root[data-theme=dark]{")
        blk = s[i:s.index("}", i)]
    return _vars(blk)


def test_choosing_dark_actually_changes_the_colours():
    """Not just `color-scheme`: the palette has to come with it."""
    dark = _theme("dark")
    for token in ("paper", "surface", "ink", "accent", "faint"):
        assert token in dark, (
            f"--{token} is not defined for an explicitly chosen dark theme, "
            f"so it falls back to the light value")


def test_the_two_dark_definitions_agree():
    """One palette reached two ways. If they drift, the theme depends on
    which route you came in by."""
    s = _css()
    i = s.index("@media (prefers-color-scheme:dark)")
    media = _vars(s[i:s.index("}}", i)])
    explicit = _theme("dark")
    assert media == explicit, "the media-query and data-theme palettes differ"


def test_small_text_is_readable_in_both_themes():
    for name in ("light", "dark"):
        v = _theme(name)
        for bg in ("surface", "paper"):
            r = _ratio(v["faint"], v[bg])
            assert r >= 4.5, (
                f"{name} --faint on --{bg} is {r:.2f}:1; it carries "
                f"timestamps and durations")


def test_the_primary_button_label_is_readable_on_its_own_accent():
    """White is right on the light accent and wrong on the dark one, which is
    why the foreground is a token rather than a literal."""
    assert "color:var(--on-accent)" in _css(), \
        "the primary button hard-codes its text colour again"
    for name in ("light", "dark"):
        v = _theme(name)
        r = _ratio(v["on-accent"], v["accent"])
        assert r >= 4.5, f"{name} primary button text is {r:.2f}:1"
