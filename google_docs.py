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
DOC_ID = os.getenv("GOOGLE_DOC_ID", "1Y4GFJFYwktciQpmkdteBqgqiTLbqCM8kNICaB50a3-U")
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


def _create_tab(service, title: str) -> str:
    response = _execute_google_request(
        service.documents().batchUpdate(
            documentId=DOC_ID,
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
    _verify_tab_exists(service, tab_id, title)
    return tab_id


def _unique_tab_title(service, base_title: str) -> str:
    existing_titles = _existing_tab_titles(service)
    if base_title not in existing_titles:
        return base_title

    for index in range(1, 1000):
        suffix = f" ({index})"
        candidate = f"{base_title[: 100 - len(suffix)]}{suffix}"
        if candidate not in existing_titles:
            return candidate

    raise RuntimeError(f"Could not create a unique Google Docs tab title for {base_title!r}")


def _existing_tab_titles(service) -> set[str]:
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
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


def _insert_tab_content(service, tab_id: str, content: str) -> None:
    title_end = 1 + _utf16_len(content.splitlines()[0])
    _execute_google_request(
        service.documents().batchUpdate(
        documentId=DOC_ID,
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


def _delete_tab(service, tab_id: str) -> None:
    _execute_google_request(
        service.documents().batchUpdate(
        documentId=DOC_ID,
        body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
        )
    )


def _verify_tab_exists(service, tab_id: str, title: str) -> None:
    doc = _execute_google_request(
        service.documents().get(documentId=DOC_ID, includeTabsContent=True)
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
