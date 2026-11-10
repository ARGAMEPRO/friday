#!/usr/bin/env bash
# F.R.I.D.A.Y. launcher for macOS and Linux.
# Usage: ./run.sh [--port 8765] [--no-browser]
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${FRIDAY_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  echo
  echo "  Python 3.10+ не найден."
  echo "  macOS:  brew install python    (или скачай с python.org)"
  echo "  Debian: sudo apt install python3"
  echo
  exit 1
fi

echo
echo "  F.R.I.D.A.Y. — Female Replacement Intelligent Digital Assistant Youth"
echo "  Локальный ассистент: голос, память, правки, живой разговор."
echo "  Платформа: $(uname -s) · Python: $("$PYTHON" -V 2>&1)"
echo "  Закрой это окно (Ctrl+C), чтобы выключить Пятницу."
echo

exec "$PYTHON" server.py "$@"
