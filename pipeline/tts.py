"""Text-to-speech via Piper (en_US-hfc_male-medium)."""

import subprocess
import logging
from pathlib import Path

log = logging.getLogger("video-narrator")

MODEL_PATH = Path(__file__).parent.parent / "models" / "en_US-hfc_male-medium.onnx"


class TTSError(Exception):
    pass


def synthesize(script: str, out_wav: Path):
    if not MODEL_PATH.exists():
        raise TTSError(f"Piper voice model not found at {MODEL_PATH}. Check Dockerfile download step.")

    # Flatten scene breaks into natural sentence pauses for continuous narration.
    text = " ".join(line.strip() for line in script.splitlines() if line.strip())

    cmd = [
        "piper",
        "--model", str(MODEL_PATH),
        "--output_file", str(out_wav),
        "--length_scale", "1.05",   # slightly slower than default -> sounds less rushed
        "--noise_scale", "0.55",
        "--noise_w", "0.7",
    ]
    try:
        result = subprocess.run(
            cmd, input=text.encode("utf-8"),
            capture_output=True, timeout=600,
        )
        if result.returncode != 0:
            raise TTSError(f"Piper failed: {result.stderr.decode(errors='ignore')[:500]}")
        if not out_wav.exists() or out_wav.stat().st_size == 0:
            raise TTSError("Piper produced no audio output.")
    except subprocess.TimeoutExpired:
        raise TTSError("Narration generation timed out.")
    except FileNotFoundError:
        raise TTSError("Piper executable not found. Check installation in the Docker image.")
