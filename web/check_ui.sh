#!/usr/bin/env bash
# Pre-flight checks for the web UI.
#
# A syntax error in the inline script silently kills every handler on the
# page, so this started as a `node --check`. That was not enough: a *runtime*
# reference to an element that does not exist throws during setup and aborts
# the rest of the script, which takes out unrelated features -- a missing
# filter control once broke the waveform, the play button, the Back button
# and the export flow all at once, with nothing wrong in any of them.
set -u
HTML="$(dirname "$0")/static/index.html"
fail=0

# 1. Does the inline script parse at all?
python3 - "$HTML" > /tmp/ui_script.js <<'PY'
import re, sys
html = open(sys.argv[1]).read()
print("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
PY
if command -v node >/dev/null 2>&1; then
  if node --check /tmp/ui_script.js 2>/tmp/ui_syntax.txt; then
    echo "  syntax: ok"
  else
    echo "  syntax: FAILED"; sed 's/^/    /' /tmp/ui_syntax.txt; fail=1
  fi
else
  echo "  syntax: skipped (no node)"
fi

# 2. Does every element the script reaches for actually exist?
python3 - "$HTML" <<'PY'
import re, sys
html = open(sys.argv[1]).read()
script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
ids = set(re.findall(r'id="([^"]+)"', html))
# Only literal lookups can be checked; computed ones are skipped by design.
used = set(re.findall(r'\$\("([^"]+)"\)', script))
used |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
# Elements the script builds itself are legitimately absent from the markup.
made = set(re.findall(r'\.id\s*=\s*"([^"]+)"', script))
# A lookup guarded with ?. is handling absence on purpose.
guarded = set(re.findall(r'getElementById\("([^"]+)"\)\?\.', script))
guarded |= set(re.findall(r'\$\("([^"]+)"\)\?\.', script))
missing = sorted(used - ids - made - guarded)
if missing:
    print("  elements: MISSING " + ", ".join("#" + m for m in missing))
    sys.exit(1)
print(f"  elements: ok ({len(used)} referenced, all present)")
PY
[ $? -ne 0 ] && fail=1

exit $fail
