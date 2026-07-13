"""OpenAI transcription for Telegram voice messages."""
from __future__ import annotations

import os


def transcribe_telegram_voice(file_path: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    from openai import OpenAI

    with open(file_path, "rb") as audio:
        response = OpenAI(api_key=api_key).audio.transcriptions.create(
            model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
            file=audio,
        )
    return str(response.text or "").strip()
