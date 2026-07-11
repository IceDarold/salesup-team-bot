"""Deepgram transcription with speaker diarization."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ru")
DEEPGRAM_TIMEOUT = int(os.getenv("DEEPGRAM_TIMEOUT", "900"))

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY environment variable is not set.")

    if _client is None:
        _client = httpx.Client(
            headers={"Authorization": f"Token {api_key}"},
            timeout=httpx.Timeout(DEEPGRAM_TIMEOUT, connect=20),
        )
    return _client


def transcribe_with_speakers(file_path: str, language: str | None = None) -> str:
    """Transcribe audio with speaker labels."""
    client = _get_client()
    content_type = _content_type(file_path)
    file_size = os.path.getsize(file_path)

    logger.info(
        "Sending %.1f MB to Deepgram (model=%s, diarize=true)",
        file_size / (1024 * 1024),
        DEEPGRAM_MODEL,
    )
    resp = client.post(
        DEEPGRAM_URL,
        content=_iter_file(file_path),
        headers={"Content-Type": content_type, "Content-Length": str(file_size)},
        params={
            "model": DEEPGRAM_MODEL,
            "language": language or DEEPGRAM_LANGUAGE,
            "diarize": "true",
            "smart_format": "true",
            "paragraphs": "true",
            "punctuate": "true",
        },
    )
    resp.raise_for_status()

    text = _extract_speaker_text(resp.json())
    logger.info("Deepgram transcription done - %d chars", len(text))
    return text


def _iter_file(file_path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _content_type(file_path: str) -> str:
    mime_map = {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".oga": "audio/ogg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
    }
    return mime_map.get(Path(file_path).suffix.lower(), "application/octet-stream")


def _extract_speaker_text(data: dict) -> str:
    alternative = (
        data.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
    )

    paragraphs = alternative.get("paragraphs", {}).get("paragraphs", [])
    if not paragraphs:
        return alternative.get("transcript", "").strip()

    lines = []
    for paragraph in paragraphs:
        speaker = paragraph.get("speaker", 0)
        sentences = paragraph.get("sentences", [])
        text = " ".join(s.get("text", "") for s in sentences).strip()
        if text:
            lines.append(f"[Спикер {speaker}] {text}")

    return "\n\n".join(lines).strip()
