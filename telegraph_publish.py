"""Publish interview insights to Telegra.ph."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegra.ph"
ENV_PATH = Path(__file__).resolve().parent / ".env"
TELEGRAPH_AUTHOR_NAME = os.getenv("TELEGRAPH_AUTHOR_NAME", "Aich Team")
TELEGRAPH_SHORT_NAME = os.getenv("TELEGRAPH_SHORT_NAME", "Aich")
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN")
MAX_CONTENT_BYTES = 60_000
TELEGRAPH_MAX_RETRIES = int(os.getenv("TELEGRAPH_MAX_RETRIES", "3"))
TELEGRAPH_RETRY_BACKOFF_SECONDS = float(os.getenv("TELEGRAPH_RETRY_BACKOFF_SECONDS", "1.5"))


def publish_insights(answers: dict, insights: str) -> str:
    """Create a Telegra.ph page with interview insights and return its URL."""
    return _publish_page(_page_title(answers, "Инсайты Aich"), insights)


def publish_interviewer_feedback(answers: dict, feedback: str) -> str:
    """Create a separate Telegra.ph page with private interviewer feedback."""
    return _publish_page(_page_title(answers, "Фидбэк интервьюеру Aich"), feedback)


def _publish_page(title: str, text: str) -> str:
    access_token = _access_token()
    content = _markdownish_to_nodes(text)
    truncated = False

    while _content_size(content) > MAX_CONTENT_BYTES and len(content) > 1:
        content.pop()
        truncated = True
    if _content_size(content) > MAX_CONTENT_BYTES:
        content = [{"tag": "p", "children": [text[:20_000]]}]
        truncated = True

    if truncated:
        content.append(
            {
                "tag": "p",
                "children": [
                    "Отчёт был сокращён из-за лимита Telegra.ph. Полная структурированная версия сохранена в Notion."
                ],
            }
        )

    response = _post(
        "createPage",
        {
            "access_token": access_token,
            "title": title,
            "author_name": TELEGRAPH_AUTHOR_NAME,
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        },
    )
    url = response.get("url")
    if not url:
        raise RuntimeError(f"Telegra.ph did not return page URL: {response}")

    logger.info("Interview insights published to Telegra.ph: %s", url)
    return url


def _create_account() -> str:
    global TELEGRAPH_ACCESS_TOKEN
    response = _post(
        "createAccount",
        {
            "short_name": TELEGRAPH_SHORT_NAME,
            "author_name": TELEGRAPH_AUTHOR_NAME,
        },
    )
    access_token = response.get("access_token")
    if not access_token:
        raise RuntimeError(f"Telegra.ph did not return access_token: {response}")

    _save_access_token(access_token)
    TELEGRAPH_ACCESS_TOKEN = access_token
    return access_token


def _access_token() -> str:
    return TELEGRAPH_ACCESS_TOKEN or _create_account()


def _post(method: str, data: dict) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    last_error = None
    for attempt in range(1, TELEGRAPH_MAX_RETRIES + 1):
        request = urllib.request.Request(f"{API_BASE}/{method}", data=encoded, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if not payload.get("ok"):
                raise RuntimeError(f"Telegra.ph {method} failed: {payload.get('error')}")
            return payload.get("result", {})
        except Exception as e:
            last_error = e
            if attempt < TELEGRAPH_MAX_RETRIES:
                delay = TELEGRAPH_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Telegra.ph %s failed, retrying in %.1fs (%d/%d): %s",
                    method,
                    delay,
                    attempt,
                    TELEGRAPH_MAX_RETRIES,
                    e,
                )
                time.sleep(delay)

    raise last_error


def _save_access_token(access_token: str) -> None:
    if not ENV_PATH.exists():
        return

    lines = ENV_PATH.read_text().splitlines()
    if any(line.startswith("TELEGRAPH_ACCESS_TOKEN=") for line in lines):
        return

    with ENV_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\nTELEGRAPH_ACCESS_TOKEN={access_token}\n")


def _page_title(answers: dict, prefix: str) -> str:
    name = str(answers.get("name") or "Интервью").strip()
    title = re.sub(r"\s+", " ", f"{prefix} - {name}").strip()
    return title[:256] or prefix


def _markdownish_to_nodes(text: str) -> list[dict]:
    nodes: list[dict] = []
    unordered_items: list[str] = []
    ordered_items: list[str] = []
    quote_lines: list[str] = []
    table_lines: list[str] = []

    def flush_unordered() -> None:
        nonlocal unordered_items
        if not unordered_items:
            return
        nodes.append(
            {
                "tag": "ul",
                "children": [
                    {"tag": "li", "children": _inline_nodes(item)}
                    for item in unordered_items
                ],
            }
        )
        unordered_items = []

    def flush_ordered() -> None:
        nonlocal ordered_items
        if not ordered_items:
            return
        nodes.append(
            {
                "tag": "ol",
                "children": [
                    {"tag": "li", "children": _inline_nodes(item)}
                    for item in ordered_items
                ],
            }
        )
        ordered_items = []

    def flush_quotes() -> None:
        nonlocal quote_lines
        if not quote_lines:
            return
        nodes.append({"tag": "blockquote", "children": _inline_nodes("\n".join(quote_lines))})
        quote_lines = []

    def flush_table() -> None:
        nonlocal table_lines
        if not table_lines:
            return
        nodes.append({"tag": "pre", "children": [_format_markdown_table(table_lines)]})
        table_lines = []

    def flush_blocks() -> None:
        flush_unordered()
        flush_ordered()
        flush_quotes()
        flush_table()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_blocks()
            continue

        if _is_table_line(line):
            flush_unordered()
            flush_ordered()
            flush_quotes()
            table_lines.append(line)
            continue

        flush_table()

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            flush_unordered()
            flush_ordered()
            flush_quotes()
            tag = "h4" if len(heading_match.group(1)) >= 4 else "h3"
            nodes.append({"tag": tag, "children": _inline_nodes(heading_match.group(2))})
            continue

        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            flush_ordered()
            flush_quotes()
            unordered_items.append(bullet_match.group(1))
            continue

        ordered_match = re.match(r"^\d+\.\s+(.+)$", line)
        if ordered_match:
            flush_unordered()
            flush_quotes()
            ordered_items.append(ordered_match.group(1))
            continue

        quote_match = re.match(r"^>\s*(.+)$", line)
        if quote_match:
            flush_unordered()
            flush_ordered()
            quote_lines.append(quote_match.group(1))
            continue

        flush_blocks()
        if line == "---":
            nodes.append({"tag": "hr"})
        else:
            nodes.append({"tag": "p", "children": _inline_nodes(line)})

    flush_blocks()
    return nodes or [{"tag": "p", "children": ["Инсайты не сгенерированы."]}]


def _inline_nodes(text: str) -> list:
    nodes: list = []
    pattern = re.compile(
        r"(\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`|\*([^*]+)\*)"
    )
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            nodes.append(text[pos : match.start()])
        if match.group(2) and match.group(3):
            nodes.append({"tag": "a", "attrs": {"href": match.group(3)}, "children": [match.group(2)]})
        elif match.group(4) or match.group(5):
            nodes.append({"tag": "strong", "children": [match.group(4) or match.group(5)]})
        elif match.group(6):
            nodes.append({"tag": "code", "children": [match.group(6)]})
        elif match.group(7):
            nodes.append({"tag": "em", "children": [match.group(7)]})
        pos = match.end()
    if pos < len(text):
        nodes.append(text[pos:])
    return nodes or [""]


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _format_markdown_table(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append(cells)

    if not rows:
        return "\n".join(lines)

    column_count = max(len(row) for row in rows)
    widths = [0] * column_count
    for row in rows:
        for index in range(column_count):
            value = row[index] if index < len(row) else ""
            widths[index] = max(widths[index], len(value))

    formatted = []
    for row_index, row in enumerate(rows):
        padded = [
            (row[index] if index < len(row) else "").ljust(widths[index])
            for index in range(column_count)
        ]
        formatted.append(" | ".join(padded).rstrip())
        if row_index == 0 and len(rows) > 1:
            formatted.append("-+-".join("-" * width for width in widths).rstrip())
    return "\n".join(formatted)


def _content_size(content: list[dict]) -> int:
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
