"""Interview transcription routing."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "deepgram-nova3")


def transcribe(file_path: str, model_name: str | None = None, language: str | None = None) -> str:
    """Transcribe an interview audio file."""
    model = model_name or TRANSCRIBE_MODEL
    if model != "deepgram-nova3":
        raise ValueError(f"Unsupported transcription model for interview workflow: {model}")

    from deepgram import transcribe_with_speakers

    return transcribe_with_speakers(file_path, language=language)
