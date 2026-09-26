"""Word-level alignment of the generated narration.

NOTE ON A DELIBERATE SUBSTITUTION:
WhisperX was requested, but its PyTorch + forced-alignment model stack
does not fit reliably inside Render's free-tier 512MB RAM (it routinely
OOMs on CPU-only free instances). faster-whisper (CTranslate2, int8)
gives the same word-level timestamps with a much smaller memory
footprint and is the realistic choice for this hosting tier. If you
upgrade to a paid Render plan with more RAM, WhisperX can be swapped in
here with the same output shape (list of {word, start, end}).
"""

import os
import logging
from pathlib import Path

log = logging.getLogger("video-narrator")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")

_model = None


class AlignError(Exception):
    pass


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("Loading faster-whisper model: %s", WHISPER_MODEL)
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def get_word_timestamps(wav_path: Path):
    try:
        model = _load_model()
        segments, _info = model.transcribe(
            str(wav_path), language="en", word_timestamps=True, vad_filter=True,
        )
        words = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
        if not words:
            raise AlignError("Alignment produced no words. Check the narration audio.")
        return words
    except AlignError:
        raise
    except Exception as e:
        raise AlignError(f"Alignment failed: {e}")
