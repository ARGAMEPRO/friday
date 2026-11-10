"""Persistent Silero Russian TTS worker (runs on python3.12 with torch)."""
import json
import sys
import wave

import torch

MODEL_PATH = sys.argv[1]
SPEAKER = sys.argv[2] if len(sys.argv) > 2 else "baya"

model = torch.package.PackageImporter(MODEL_PATH).load_pickle("tts_models", "model")
model.to(torch.device("cpu"))
print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        request = json.loads(line)
        text = (request.get("text") or "").strip()
        out = request.get("out") or ""
        if not text or not out:
            print(json.dumps({"ok": False, "error": "bad request"}), flush=True)
            continue
        audio = model.apply_tts(text=text, speaker=SPEAKER, sample_rate=48000)
        data = (audio.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
        with wave.open(out, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(data)
        print(json.dumps({"ok": True}), flush=True)
    except Exception as exc:  # noqa: BLE001 - report and keep serving
        print(json.dumps({"ok": False, "error": str(exc)[:200]}), flush=True)
