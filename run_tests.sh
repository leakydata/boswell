#!/usr/bin/env bash
# Everything that can be checked without the board.
set -u
cd "$(dirname "$0")"
fail=0

echo "web interface:"
bash web/check_ui.sh || fail=1

echo
echo "python syntax:"
if python3 -m py_compile web/*.py host/*.py 2>/tmp/pycompile.txt; then
  echo "  ok"
else
  sed 's/^/  /' /tmp/pycompile.txt; fail=1
fi

echo
echo "sequence and storage:"
uv run python -m pytest tests/ -q 2>&1 | tail -3 || fail=1

exit $fail
