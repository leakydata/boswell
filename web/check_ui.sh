#!/usr/bin/env bash
# Syntax-check the inline JavaScript in the UI. A parse error kills the whole
# script silently -- every control stops responding with nothing in the page to
# indicate why -- so this runs before the server is restarted.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HTML="$HERE/static/index.html"
TMP="$(mktemp /tmp/boswell_ui_XXXX.js)"
python3 - "$HTML" "$TMP" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
open(sys.argv[2], "w", encoding="utf-8").write(
    "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
PY
if command -v node >/dev/null 2>&1; then
  node --check "$TMP" && echo "UI javascript: OK" || { echo "UI javascript: SYNTAX ERROR" >&2; rm -f "$TMP"; exit 1; }
else
  echo "node not found; skipping UI syntax check" >&2
fi
rm -f "$TMP"
