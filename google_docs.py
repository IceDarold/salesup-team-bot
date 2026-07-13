"""Save interview transcripts into a new Google Docs tab."""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

DEFAULT_CREDS_PATH = Path(__file__).parent / ".secrets" / "google-credentials.json"
DEFAULT_OAUTH_CLIENT_PATH = Path(__file__).parent / ".secrets" / "google-oauth-client.json"
DEFAULT_OAUTH_TOKEN_PATH = Path(__file__).parent / ".secrets" / "google-oauth-token.json"
DOC_ID = os.getenv("GOOGLE_DOC_ID", "1wDR5jAx7Y8rQetmdY1WyHSvtyZYKM0n-qdWEuSLVe_s")
CONVERSATION_DOC_ID = os.getenv("GOOGLE_CONVERSATION_DOC_ID", "1zs57P-lBgM8oBOajx2SfbbKwjrbrdfJk1GZAKrYdO8Q")
RESEARCH_DOC_ID = os.getenv("GOOGLE_RESEARCH_DOC_ID", "1wUY-eeFboYPRjAEg5N6kytRF5Y6VDPS8aw49_A3Qm2M")
CREDS_PATH = Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_CREDS_PATH)))
OAUTH_CLIENT_PATH = Path(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", str(DEFAULT_OAUTH_CLIENT_PATH)))
OAUTH_TOKEN_PATH = Path(os.getenv("GOOGLE_OAUTH_TOKEN", str(DEFAULT_OAUTH_TOKEN_PATH)))
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
GOOGLE_API_MAX_RETRIES = int(os.getenv("GOOGLE_API_MAX_RETRIES", "3"))
GOOGLE_API_RETRY_BACKOFF_SECONDS = float(os.getenv("GOOGLE_API_RETRY_BACKOFF_SECONDS", "1.5"))
TRANSCRIPT_FOLDER_ID = os.getenv("GOOGLE_TRANSCRIPT_FOLDER_ID", "")
TRANSCRIPT_DOC_PERMISSION = os.getenv("TRANSCRIPT_DOC_PERMISSION", "reader")

_docs_service = None
_drive_service = None
_oauth_docs_service = None
_oauth_drive_service = None

AICH_RESEARCH_CONTEXT = (
    "Aich - AI-продукт, который анализирует урок и выдаёт фидбэк учителю. "
    "Исследуем, насколько учителям полезен такой AI-фидбэк, есть ли реальная "
    "потребность и готовность платить, а также какие другие сильные боли есть "
    "в работе учителя."
)


def _get_service():
    global _docs_service
    if _docs_service is not None:
        return _docs_service

    if not DOC_ID:
        raise RuntimeError("GOOGLE_DOC_ID environment variable is not set.")
    if not CREDS_PATH.exists():
        raise RuntimeError(f"Google credentials file not found: {CREDS_PATH}")

    creds = service_account.Credentials.from_service_account_file(
        str(CREDS_PATH), scopes=SCOPES
    )
    _docs_service = build("docs", "v1", credentials=creds)
    return _docs_service


def _get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    if not CREDS_PATH.exists():
        raise RuntimeError(f"Google credentials file not found: {CREDS_PATH}")

    creds = service_account.Credentials.from_service_account_file(
        str(CREDS_PATH), scopes=SCOPES
    )
    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


def _get_oauth_credentials() -> Credentials:
    if not OAUTH_TOKEN_PATH.exists():
        raise RuntimeError(
            f"Google OAuth token not found: {OAUTH_TOKEN_PATH}. "
            "Run the local OAuth setup before using /transcript."
        )

    creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        OAUTH_TOKEN_PATH.write_text(creds.to_json())
    if not creds.valid:
        raise RuntimeError(
            f"Google OAuth token is invalid: {OAUTH_TOKEN_PATH}. "
            "Run the local OAuth setup again."
        )
    return creds


def _get_oauth_docs_service():
    global _oauth_docs_service
    if _oauth_docs_service is None:
        _oauth_docs_service = build("docs", "v1", credentials=_get_oauth_credentials())
    return _oauth_docs_service


def _get_oauth_drive_service():
    global _oauth_drive_service
    if _oauth_drive_service is None:
        _oauth_drive_service = build("drive", "v3", credentials=_get_oauth_credentials())
    return _oauth_drive_service


def add_interview(answers: dict, transcript: str) -> str:
    """Create a new document tab and write one interview into it."""
    service = _get_service()
    tab_title = _unique_tab_title(service, _tab_title(answers))
    content = _build_content(answers, transcript)
    tab_id = None

    try:
        tab_id = _create_tab(service, tab_title)
        _insert_tab_content(service, tab_id, content)
        _verify_tab_content(service, tab_id, content)
        logger.info("Interview saved to Google Doc tab %s (%s)", tab_title, tab_id)
        return f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab_id}"
    except HttpError as e:
        logger.error("Google Docs API error: %s", e)
        if tab_id:
            with suppress(Exception):
                _delete_tab(service, tab_id)
        raise
    except Exception:
        if tab_id:
            with suppress(Exception):
                _delete_tab(service, tab_id)
        raise


def add_transcript_document(title: str, transcript: str, language: str | None = None) -> str:
    """Create a new Google Doc document with a plain transcript."""
    service = _get_oauth_docs_service()
    doc_title = _sanitize_title(title)[:100]
    drive = _get_oauth_drive_service()
    document = _execute_google_request(
        drive.files().create(
            body={
                "name": doc_title,
                "mimeType": "application/vnd.google-apps.document",
                **({"parents": [TRANSCRIPT_FOLDER_ID]} if TRANSCRIPT_FOLDER_ID else {}),
            },
            fields="id",
            supportsAllDrives=True,
        )
    )
    document_id = document.get("id")
    if not document_id:
        raise RuntimeError(f"Google Docs did not return documentId: {document}")

    content = _build_transcript_content(doc_title, transcript, language)
    _execute_google_request(
        service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": content,
                        }
                    },
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": 1,
                                "endIndex": 1 + _utf16_len(content.splitlines()[0]),
                            },
                            "textStyle": {
                                "bold": True,
                                "fontSize": {"magnitude": 14, "unit": "PT"},
                            },
                            "fields": "bold,fontSize",
                        }
                    },
                ]
            },
        )
    )
    _verify_document_content(service, document_id, content)
    _share_document_by_link(document_id)
    logger.info("Plain transcript saved to new Google Doc %s (%s)", doc_title, document_id)
    return f"https://docs.google.com/document/d/{document_id}/edit"


# Backward-compatible name for callers that only need "plain transcript".
add_transcript = add_transcript_document


def find_interview_by_name(name: str) -> dict | None:
    """Find an existing Google Docs tab by interview respondent name."""
    service = _get_service()
    base_title = _sanitize_title(name)[:100]
    if not base_title or base_title == "-":
        return None

    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
    )
    matches = []
    for tab in doc.get("tabs", []):
        properties = tab.get("tabProperties", {})
        title = properties.get("title", "")
        tab_id = properties.get("tabId")
        if not tab_id:
            continue
        suffix_index = _matching_title_index(title, base_title)
        if suffix_index is None:
            continue
        matches.append(
            {
                "title": title,
                "tab_id": tab_id,
                "suffix_index": suffix_index,
                "url": f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab_id}",
                "transcript": _extract_transcript_text(_tab_text_from_tab(tab)),
            }
        )

    if not matches:
        return None
    return sorted(matches, key=lambda item: item["suffix_index"])[-1]


def get_tab_info_from_url(url: str) -> dict | None:
    """Return Google Docs tab metadata and transcript for a tab URL."""
    tab_match = re.search(r"[?&]tab=([^&#]+)", url)
    if not tab_match:
        return None
    tab_id = tab_match.group(1)
    service = _get_service()
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
    )
    for tab in doc.get("tabs", []):
        properties = tab.get("tabProperties", {})
        if properties.get("tabId") != tab_id:
            continue
        text = _tab_text_from_tab(tab)
        return {
            "title": properties.get("title") or "",
            "tab_id": tab_id,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab_id}",
            "transcript": _extract_transcript_text(text),
        }
    return None


def read_transcript_from_url(url: str) -> str | None:
    tab_match = re.search(r"[?&]tab=([^&#]+)", url)
    if not tab_match:
        return None
    service = _get_service()
    text = _read_tab_text(service, tab_match.group(1))
    return _extract_transcript_text(text)


def archive_tab(tab_id: str, current_title: str | None = None) -> dict:
    """Archive an existing Google Docs tab.

    The Docs API currently exposes tab title updates, but the endpoint can return
    transient 500s. If renaming fails, create an archived copy so the old content
    is still preserved under a *_archived tab before the caller decides whether
    to delete the original tab.
    """
    service = _get_service()
    title = current_title or _tab_title_by_id(service, tab_id)
    archived_title = _unique_archive_title(service, title, tab_id)
    try:
        _execute_google_request(
            service.documents().batchUpdate(
                documentId=DOC_ID,
                body={
                    "requests": [
                        {
                            "updateDocumentTabProperties": {
                                "tabProperties": {
                                    "tabId": tab_id,
                                    "title": archived_title,
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            )
        )
        _verify_tab_exists(service, tab_id, archived_title)
        logger.info("Google Doc tab archived by rename: %s -> %s (%s)", title, archived_title, tab_id)
        return {
            "tab_id": tab_id,
            "delete_tab_id": tab_id,
            "title": archived_title,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={tab_id}",
            "mode": "renamed",
        }
    except HttpError as e:
        logger.warning("Google Docs tab rename failed, creating archived copy instead: %s", e)
        archived_tab_id = _copy_tab_text(service, tab_id, archived_title)
        return {
            "tab_id": archived_tab_id,
            "delete_tab_id": tab_id,
            "original_tab_id": tab_id,
            "title": archived_title,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit?tab={archived_tab_id}",
            "mode": "copied",
        }


def delete_tab_by_id(tab_id: str) -> None:
    """Delete an existing Google Docs tab."""
    service = _get_service()
    _delete_tab(service, tab_id)
    logger.info("Google Doc tab deleted: %s", tab_id)


def create_conversation_tab(contact_name: str, owner_name: str) -> dict:
    """Create a tab for a contact's Telegram archive in the configured document."""
    service = _get_service()
    title = _unique_tab_title(service, _sanitize_title(f"Переписка — {contact_name}")[:100], CONVERSATION_DOC_ID)
    tab_id = _create_tab(service, title, CONVERSATION_DOC_ID)
    header = "\n".join(
        [
            title,
            "",
            f"Ответственный: {owner_name or '-'}",
            f"Создано: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "Сообщения Telegram",
            "",
        ]
    )
    try:
        _insert_tab_content(service, tab_id, header, CONVERSATION_DOC_ID)
        return {"tab_id": tab_id, "url": f"https://docs.google.com/document/d/{CONVERSATION_DOC_ID}/edit?tab={tab_id}"}
    except Exception:
        with suppress(Exception):
            _delete_tab(service, tab_id, CONVERSATION_DOC_ID)
        raise


def append_conversation_messages(tab_id: str, messages: list[dict]) -> None:
    """Append an ordered batch of archived Telegram messages to a conversation tab."""
    if not messages:
        return
    lines = []
    for message in messages:
        created_at = str(message.get("sent_at") or "").replace("T", " ").replace("+00:00", " UTC")
        author = "Менеджер" if message.get("outgoing") else "Контакт"
        text = str(message.get("text") or "").strip() or "[без текста]"
        media = str(message.get("media") or "").strip()
        lines.extend([f"[{created_at}] {author}", text, media] if media else [f"[{created_at}] {author}", text])
        lines.append("")
    _append_tab_content(_get_service(), tab_id, "\n".join(lines) + "\n", CONVERSATION_DOC_ID)


def delete_conversation_tab_by_id(tab_id: str) -> None:
    _delete_tab(_get_service(), tab_id, CONVERSATION_DOC_ID)


def create_company_research_tab(title: str, report: dict | str) -> str:
    """Save a structured company research report as a formatted Google Docs tab."""
    service = _get_service()
    tab_title = _unique_tab_title(service, _sanitize_title(f"Research — {title}")[:100], RESEARCH_DOC_ID)
    tab_id = _create_tab(service, tab_title, RESEARCH_DOC_ID)
    try:
        if isinstance(report, dict):
            _insert_research_report(service, tab_id, tab_title, report)
        else:  # Compatibility with historical Markdown reports.
            _insert_tab_content(service, tab_id, f"{tab_title}\n\n{report.strip()}\n", RESEARCH_DOC_ID)
        return f"https://docs.google.com/document/d/{RESEARCH_DOC_ID}/edit?tab={tab_id}"
    except Exception:
        with suppress(Exception):
            _delete_tab(service, tab_id, RESEARCH_DOC_ID)
        raise


def _insert_research_report(service, tab_id: str, title: str, report: dict) -> None:
    """Render structured research into native Docs headings, bullets, callouts and links."""
    content, headings, bullets, hypothesis_ranges = _research_report_content(title, report)
    requests = [{"insertText": {"endOfSegmentLocation": {"tabId": tab_id}, "text": content}}]
    for start, end, style in headings:
        requests.append({
            "updateParagraphStyle": {
                "range": {"tabId": tab_id, "startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType",
            }
        })
    for start, end in bullets:
        requests.append({
            "createParagraphBullets": {
                "range": {"tabId": tab_id, "startIndex": start, "endIndex": end},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })
    for start, end in hypothesis_ranges:
        requests.append({
            "updateTextStyle": {
                "range": {"tabId": tab_id, "startIndex": start, "endIndex": end},
                "textStyle": {"backgroundColor": {"color": {"rgbColor": {"red": 1, "green": 0.95, "blue": 0.75}}}},
                "fields": "backgroundColor",
            }
        })
    source_urls = {str(item.get("id")): str(item.get("url")) for item in report.get("sources", []) if item.get("id") and item.get("url")}
    for match in re.finditer(r"\[(S\d+)\]", content):
        url = source_urls.get(match.group(1))
        if url:
            start = 1 + _utf16_len(content[:match.start()])
            end = start + _utf16_len(match.group(0))
            requests.append({
                "updateTextStyle": {
                    "range": {"tabId": tab_id, "startIndex": start, "endIndex": end},
                    "textStyle": {"link": {"url": url}, "foregroundColor": {"color": {"rgbColor": {"red": 0.1, "green": 0.35, "blue": 0.8}}}},
                    "fields": "link,foregroundColor",
                }
            })
    for start in range(0, len(requests), 80):
        _execute_google_request(service.documents().batchUpdate(documentId=RESEARCH_DOC_ID, body={"requests": requests[start:start + 80]}))


def _research_report_content(title: str, report: dict) -> tuple[str, list[tuple[int, int, str]], list[tuple[int, int]], list[tuple[int, int]]]:
    lines: list[str] = []
    headings: list[tuple[int, int, str]] = []
    bullets: list[tuple[int, int]] = []
    hypotheses: list[tuple[int, int]] = []

    def position() -> int:
        return 1 + _utf16_len("\n".join(lines)) + (1 if lines else 0)

    def paragraph(text: str = "") -> tuple[int, int]:
        start = position()
        lines.append(str(text).strip())
        return start, start + _utf16_len(str(text).strip())

    def heading(text: str, level: str) -> None:
        start, end = paragraph(text)
        headings.append((start, end + 1, level))

    def bullet_items(items: list[str]) -> None:
        if not items:
            return
        start = position()
        for item in items:
            paragraph(item)
        bullets.append((start, position()))

    def citations(item: dict) -> str:
        ids = [str(value) for value in item.get("source_ids", []) if str(value)]
        return " " + " ".join(f"[{source_id}]" for source_id in ids) if ids else ""

    heading(title, "TITLE")
    paragraph(f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    paragraph("Легенда: жёлтая подсветка — гипотеза, которую нужно проверить на discovery.")
    paragraph()

    heading("Что делать сейчас", "HEADING_1")
    brief = report.get("sales_brief") or {}
    bullet_items([str(item) for item in brief.get("signals", []) if str(item)])
    if brief.get("buyer"):
        paragraph(f"Кому писать: {brief['buyer']}")
    if brief.get("value_proposition"):
        paragraph(f"Ценность: {brief['value_proposition']}")
    if brief.get("cta"):
        paragraph(f"Первый CTA: {brief['cta']}")
    if report.get("executive_summary"):
        paragraph(report["executive_summary"])
    paragraph()

    def evidence_section(title_text: str, items: list[dict], main_key: str, secondary_key: str | None = None) -> None:
        if not items:
            return
        heading(title_text, "HEADING_1")
        for item in items:
            text = str(item.get(main_key) or "").strip()
            if not text:
                continue
            confidence = str(item.get("confidence") or "hypothesis")
            prefix = "Гипотеза: " if confidence == "hypothesis" else "Факт: "
            start, end = paragraph(prefix + text + citations(item))
            if confidence == "hypothesis":
                hypotheses.append((start, end))
            evidence = str(item.get("evidence") or "").strip()
            if evidence:
                paragraph("Доказательство: " + evidence)
            if secondary_key and item.get(secondary_key):
                paragraph("Рекомендация: " + str(item[secondary_key]))
            if item.get("priority"):
                paragraph("Приоритет: " + str(item["priority"]))
            paragraph()

    evidence_section("Компания и подтверждённые факты", report.get("company_facts") or [], "fact")
    evidence_section("Сигналы вакансии", report.get("vacancy_signals") or [], "signal", "why_it_matters")
    evidence_section("Боли и автоматизация", report.get("pains") or [], "pain", "automation")

    stakeholders = report.get("stakeholders") or []
    if stakeholders:
        heading("Кому писать", "HEADING_1")
        for item in stakeholders:
            paragraph(f"{item.get('priority') or '—'}. {item.get('role') or 'Роль'} — {item.get('motivation') or ''}")
            if item.get("cta"):
                paragraph("CTA: " + str(item["cta"]))
        paragraph()

    touchpoints = report.get("touchpoints") or []
    if touchpoints:
        heading("Последовательность касаний", "HEADING_1")
        bullet_items([f"День {item.get('day') or '—'} · {item.get('channel') or '—'}: {item.get('action') or ''}" for item in touchpoints])
        paragraph()

    messages = report.get("messages") or []
    if messages:
        heading("Готовые первые сообщения", "HEADING_1")
        for item in messages:
            heading(str(item.get("label") or "Вариант"), "HEADING_2")
            paragraph(str(item.get("text") or ""))
            paragraph()

    for heading_text, key in [("Риски", "risks"), ("Что проверить на discovery", "discovery_questions"), ("Пробелы исследования", "gaps")]:
        values = [str(item) for item in report.get(key, []) if str(item)]
        if values:
            heading(heading_text, "HEADING_1")
            bullet_items(values)
            paragraph()

    sources = report.get("sources") or []
    if sources:
        heading("Реестр источников", "HEADING_1")
        for source in sources:
            source_id = str(source.get("id") or "")
            paragraph(f"[{source_id}] {source.get('title') or source.get('url') or 'Источник'}")
            if source.get("excerpt"):
                paragraph(str(source["excerpt"]))
            paragraph(str(source.get("url") or ""))
            paragraph()
    return "\n".join(lines) + "\n", headings, bullets, hypotheses


def _share_document_by_link(document_id: str) -> None:
    if TRANSCRIPT_DOC_PERMISSION not in {"reader", "commenter", "writer"}:
        raise RuntimeError(
            "TRANSCRIPT_DOC_PERMISSION must be one of: reader, commenter, writer"
        )
    drive = _get_oauth_drive_service()
    _execute_google_request(
        drive.permissions().create(
            fileId=document_id,
            body={
                "type": "anyone",
                "role": TRANSCRIPT_DOC_PERMISSION,
            },
            fields="id",
            supportsAllDrives=True,
        )
    )


def _copy_tab_text(service, source_tab_id: str, title: str) -> str:
    content = _read_tab_text(service, source_tab_id).strip()
    if not content:
        content = f"Архив старой вкладки: {title}\n"
    archived_tab_id = None
    try:
        archived_tab_id = _create_tab(service, title)
        _insert_tab_content(service, archived_tab_id, content + "\n")
        _verify_tab_content(service, archived_tab_id, content + "\n")
        logger.info("Google Doc tab archived by copy: %s -> %s", source_tab_id, archived_tab_id)
        return archived_tab_id
    except Exception:
        if archived_tab_id:
            with suppress(Exception):
                _delete_tab(service, archived_tab_id)
        raise


def _create_tab(service, title: str, document_id: str = DOC_ID) -> str:
    response = _execute_google_request(
        service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "addDocumentTab": {
                            "tabProperties": {
                                "title": title,
                            }
                        }
                    }
                ]
            },
        )
    )
    replies = response.get("replies", [])
    tab_properties = replies[0].get("addDocumentTab", {}).get("tabProperties", {})
    tab_id = tab_properties.get("tabId")
    if not tab_id:
        raise RuntimeError(f"Google Docs did not return tabId: {response}")
    _verify_tab_exists(service, tab_id, title, document_id)
    return tab_id


def _unique_tab_title(service, base_title: str, document_id: str = DOC_ID) -> str:
    existing_titles = _existing_tab_titles(service, document_id)
    if base_title not in existing_titles:
        return base_title

    for index in range(1, 1000):
        suffix = f" ({index})"
        candidate = f"{base_title[: 100 - len(suffix)]}{suffix}"
        if candidate not in existing_titles:
            return candidate

    raise RuntimeError(f"Could not create a unique Google Docs tab title for {base_title!r}")


def _existing_tab_titles(service, document_id: str = DOC_ID) -> set[str]:
    doc = _execute_google_request(
        service.documents().get(documentId=document_id, includeTabsContent=True)
    )
    return {
        properties.get("title", "")
        for tab in doc.get("tabs", [])
        for properties in [tab.get("tabProperties", {})]
        if properties.get("title")
    }


def _unique_archive_title(service, title: str, tab_id: str) -> str:
    existing_titles = _existing_tab_titles_by_id(service)
    base = f"{_sanitize_title(title)[:91]}_archived"
    if base not in existing_titles or existing_titles.get(base) == tab_id:
        return base

    for index in range(1, 1000):
        suffix = f" ({index})"
        candidate = f"{base[: 100 - len(suffix)]}{suffix}"
        if candidate not in existing_titles or existing_titles.get(candidate) == tab_id:
            return candidate

    raise RuntimeError(f"Could not create a unique archived tab title for {title!r}")


def _existing_tab_titles_by_id(service) -> dict[str, str]:
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
    )
    result = {}
    for tab in doc.get("tabs", []):
        properties = tab.get("tabProperties", {})
        title = properties.get("title")
        tab_id = properties.get("tabId")
        if title and tab_id:
            result[title] = tab_id
    return result


def _tab_title_by_id(service, tab_id: str) -> str:
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
    )
    for tab in doc.get("tabs", []):
        properties = tab.get("tabProperties", {})
        if properties.get("tabId") == tab_id:
            return properties.get("title") or "-"
    raise RuntimeError(f"Tab was not found while reading title: {tab_id}")


def _matching_title_index(title: str, base_title: str) -> int | None:
    if title == base_title:
        return 0
    match = re.fullmatch(r"(.+)\s+\((\d+)\)", title)
    if match and match.group(1) == base_title:
        return int(match.group(2))
    return None


def _insert_tab_content(service, tab_id: str, content: str, document_id: str = DOC_ID) -> None:
    title_end = 1 + _utf16_len(content.splitlines()[0])
    _execute_google_request(
        service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "endOfSegmentLocation": {"tabId": tab_id},
                        "text": content,
                    }
                },
                {
                    "updateTextStyle": {
                        "range": {"tabId": tab_id, "startIndex": 1, "endIndex": title_end},
                        "textStyle": {
                            "bold": True,
                            "fontSize": {"magnitude": 14, "unit": "PT"},
                        },
                        "fields": "bold,fontSize",
                    }
                },
            ]
        },
        )
    )


def _append_tab_content(service, tab_id: str, content: str, document_id: str = DOC_ID) -> None:
    _execute_google_request(
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"endOfSegmentLocation": {"tabId": tab_id}, "text": content}}]},
        )
    )


def _delete_tab(service, tab_id: str, document_id: str = DOC_ID) -> None:
    _execute_google_request(
        service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
        )
    )


def _verify_tab_exists(service, tab_id: str, title: str, document_id: str = DOC_ID) -> None:
    doc = _execute_google_request(
        service.documents().get(documentId=document_id, includeTabsContent=True)
    )
    tabs = doc.get("tabs", [])
    for tab in tabs:
        properties = tab.get("tabProperties", {})
        if properties.get("tabId") == tab_id:
            actual_title = properties.get("title")
            if actual_title != title:
                raise RuntimeError(
                    f"Created tab title mismatch: expected {title!r}, got {actual_title!r}"
                )
            return
    raise RuntimeError(f"Created tab was not found in Google Doc: {tab_id}")


def _verify_tab_content(service, tab_id: str, expected_content: str) -> None:
    tab_text = _read_tab_text(service, tab_id)
    first_line = expected_content.splitlines()[0]
    transcript_start = expected_content.split("Транскрипция", 1)[-1].strip()[:200]
    if first_line not in tab_text or (transcript_start and transcript_start not in tab_text):
        raise RuntimeError(
            "Google Docs write verification failed: transcript was not found "
            f"in the created tab {tab_id}."
        )


def _verify_document_content(service, document_id: str, expected_content: str) -> None:
    doc = _execute_google_request(
        service.documents().get(documentId=document_id)
    )
    parts = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph", {})
        for paragraph_element in paragraph.get("elements", []):
            parts.append(paragraph_element.get("textRun", {}).get("content", ""))
    text = "".join(parts)
    first_line = expected_content.splitlines()[0]
    transcript_start = expected_content.split("Транскрипция", 1)[-1].strip()[:200]
    if first_line not in text or (transcript_start and transcript_start not in text):
        raise RuntimeError(
            "Google Docs write verification failed: transcript was not found "
            f"in the created document {document_id}."
        )


def _read_tab_text(service, tab_id: str) -> str:
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
    )
    for tab in doc.get("tabs", []):
        if tab.get("tabProperties", {}).get("tabId") != tab_id:
            continue

        return _tab_text_from_tab(tab)

    raise RuntimeError(f"Tab was not found while reading content: {tab_id}")


def _tab_text_from_tab(tab: dict) -> str:
    body = tab.get("documentTab", {}).get("body", {})
    parts = []
    for element in body.get("content", []):
        paragraph = element.get("paragraph", {})
        for paragraph_element in paragraph.get("elements", []):
            parts.append(paragraph_element.get("textRun", {}).get("content", ""))
    return "".join(parts)


def _extract_transcript_text(tab_text: str) -> str:
    if "Транскрипция" not in tab_text:
        return tab_text.strip()
    return tab_text.split("Транскрипция", 1)[1].strip()


def _build_content(answers: dict, transcript: str) -> str:
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"Интервью - {_value(answers, 'name')}",
        "",
        f"Дата сохранения: {created_at}",
        f"Имя: {_value(answers, 'name')}",
        f"Роль: {_value(answers, 'role')}",
        f"Сегмент: {_value(answers, 'segment')}",
        f"Предмет / направление: {_value(answers, 'subject')}",
        f"Формат занятий: {_value(answers, 'format')}",
        f"Опыт: {_value(answers, 'experience')}",
        f"Гипотеза интервью: {_value(answers, 'hypothesis')}",
        "",
        "Контекст исследования",
        AICH_RESEARCH_CONTEXT,
        "",
        "Транскрипция",
        "",
        transcript.strip(),
        "",
    ]
    return "\n".join(lines)


def _build_transcript_content(title: str, transcript: str, language: str | None) -> str:
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        title,
        "",
        f"Дата сохранения: {created_at}",
        f"Язык: {language or '-'}",
        "",
        "Транскрипция",
        "",
        transcript.strip(),
        "",
    ]
    return "\n".join(lines)


def _tab_title(answers: dict) -> str:
    name = _sanitize_title(_value(answers, "name"))
    return name[:100]


def _sanitize_title(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "-"


def _value(answers: dict, key: str) -> str:
    value = str(answers.get(key) or "").strip()
    return value or "-"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _execute_google_request(request):
    last_error = None
    for attempt in range(1, GOOGLE_API_MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as e:
            last_error = e
            status = getattr(e.resp, "status", 0)
            if status < 500 and status != 429:
                raise
        except Exception as e:
            last_error = e

        if attempt < GOOGLE_API_MAX_RETRIES:
            delay = GOOGLE_API_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Google Docs API request failed, retrying in %.1fs (%d/%d): %s",
                delay,
                attempt,
                GOOGLE_API_MAX_RETRIES,
                last_error,
            )
            time.sleep(delay)
    raise last_error
