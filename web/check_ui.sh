#!/usr/bin/env bash
# Pre-flight checks for the web UI.
#
# Each of these exists because the corresponding mistake shipped and broke the
# page silently:
#
#   1. a syntax error kills every handler, with nothing on the page to say why
#   2. a reference to an element that is not in the markup throws during setup
#      and aborts the rest of the script, taking unrelated features with it
#   3. a reference to a variable that is not in scope parses fine and throws
#      only when the line runs -- one in the websocket paint path threw on
#      every message and took the live device panel down without a trace
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HTML="$HERE/static/index.html"
fail=0

python3 - "$HTML" > /tmp/ui_script.js <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
print("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
PY

if command -v node >/dev/null 2>&1; then
  if node --check /tmp/ui_script.js 2>/tmp/ui_syntax.txt; then
    echo "  syntax:   ok"
  else
    echo "  syntax:   FAILED"; sed 's/^/    /' /tmp/ui_syntax.txt; fail=1
  fi
else
  echo "  syntax:   skipped (no node)"
fi

python3 "$HERE/ui_checks.py" "$HTML" /tmp/ui_script.js
rc=$?
[ $rc -ne 0 ] && fail=1
exit $fail
