"""Persistent Piper worker: loads the voice once, synthesizes many lines.

Runs under Python 3.12 (the interpreter piper-tts supports); the server itself
is on 3.13+, so this little sidecar keeps synthesis at ~0.3 s per phrase.
Protocol: one JSON per line on stdin {"text": str, "out": str}; one JSON per
line on stdout {"ok": bool, "error": str}.
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

from piper import PiperVoice


def main() -> None:
    model = Path(sys.argv[1])
    voice = PiperVoice.load(str(model))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            text = (request.get("text") or "").strip()
            target = Path(request["out"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(target), "wb") as handle:
                voice.synthesize_wav(text, handle)
            reply = {"ok": True}
        except Exception as exc:  # keep the worker alive
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
