"""Encrypted, QR-based personal Telegram connections for SalesUp members."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession


class TelegramUserError(RuntimeError):
    pass


@dataclass
class _PendingLogin:
    client: TelegramClient
    login: object
    two_factor_token: str = ""
    expires_at: datetime | None = None


class TelegramUserService:
    """Connect a user's Telegram account and send only confirmed messages."""

    def __init__(self) -> None:
        self.api_id = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
        self.api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        self._fernet = _fernet(os.getenv("TELEGRAM_SESSION_ENCRYPTION_KEY", ""))
        self.two_factor_url = os.getenv("TELEGRAM_2FA_WEB_BASE_URL", "").rstrip("/")
        self.voice_transcription_max_bytes = int(os.getenv("TELEGRAM_VOICE_TRANSCRIPTION_MAX_MB", "25")) * 1024 * 1024
        path = Path(os.getenv("TELEGRAM_USER_DB_PATH", "data/telegram-users.sqlite3"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._pending: dict[int, _PendingLogin] = {}
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_user_connections (
                telegram_user_id INTEGER PRIMARY KEY, session_encrypted TEXT NOT NULL,
                account_id INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'connected',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_archive_preferences (
                telegram_user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                consented_at TEXT, consent_version TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_archive_conversations (
                telegram_user_id INTEGER NOT NULL, contact_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                contact_name TEXT NOT NULL, google_tab_id TEXT NOT NULL DEFAULT '', google_url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (telegram_user_id, contact_id, chat_id))"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_archived_messages (
                telegram_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                contact_id TEXT NOT NULL, sent_at TEXT NOT NULL, outgoing INTEGER NOT NULL,
                sender_id INTEGER, text TEXT NOT NULL DEFAULT '', media TEXT NOT NULL DEFAULT '', exported_at TEXT,
                PRIMARY KEY (telegram_user_id, chat_id, message_id))"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_archive_exclusions (
                telegram_user_id INTEGER NOT NULL, contact_id TEXT NOT NULL, excluded_at TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, contact_id))"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_status_suggestions (
                token TEXT PRIMARY KEY, telegram_user_id INTEGER NOT NULL, contact_id TEXT NOT NULL,
                expected_status TEXT NOT NULL, suggested_status TEXT NOT NULL, reason TEXT NOT NULL,
                evidence TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT)"""
            )

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self._fernet)

    def status(self, telegram_user_id: int) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, username, display_name, status FROM telegram_user_connections WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return {
            "configured": self.configured,
            "connected": bool(row and row["status"] == "connected"),
            "username": str(row["username"]) if row else "",
            "display_name": str(row["display_name"]) if row else "",
        }

    def archive_status(self, telegram_user_id: int) -> dict:
        with self._lock:
            preference = self._db.execute(
                "SELECT enabled, consented_at FROM telegram_archive_preferences WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            counts = self._db.execute(
                "SELECT COUNT(*) AS messages, COUNT(DISTINCT contact_id) AS contacts FROM telegram_archived_messages WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return {
            "enabled": bool(preference and preference["enabled"]),
            "consented_at": str(preference["consented_at"] or "") if preference else "",
            "messages": int(counts["messages"] or 0),
            "contacts": int(counts["contacts"] or 0),
        }

    def set_archive_consent(self, telegram_user_id: int, enabled: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        version = os.getenv("TELEGRAM_ARCHIVE_CONSENT_VERSION", "2026-07-12")
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO telegram_archive_preferences (telegram_user_id, enabled, consented_at, consent_version, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET
                enabled=excluded.enabled, consented_at=excluded.consented_at, consent_version=excluded.consent_version,
                updated_at=excluded.updated_at""",
                (telegram_user_id, int(enabled), now if enabled else None, version, now),
            )

    async def sync_archive(self, telegram_user_id: int, contacts: list[dict], owner_name: str = "") -> dict:
        """Import the complete history for matched direct-contact chats and export new rows."""
        if not self.archive_status(telegram_user_id)["enabled"]:
            return {"contacts": 0, "messages": 0}
        matches = 0
        saved = 0
        changed: dict[str, dict] = {}
        async with self._client(telegram_user_id) as client:
            async for dialog in client.iter_dialogs():
                if not dialog.is_user or getattr(dialog.entity, "bot", False):
                    continue
                contact = _match_contact(contacts, dialog.entity)
                if not contact or self._is_excluded(telegram_user_id, str(contact["id"])):
                    continue
                matches += 1
                chat_id = int(dialog.id)
                self._ensure_conversation(telegram_user_id, contact, chat_id)
                last_id = self._last_message_id(telegram_user_id, chat_id)
                async for message in client.iter_messages(dialog.entity, min_id=last_id, reverse=True):
                    if message.action:
                        continue
                    text = await self._message_text(client, message)
                    if self._save_message(telegram_user_id, chat_id, contact["id"], message, text=text):
                        saved += 1
                        changed[str(contact["id"])] = contact
                if await self._backfill_voice_messages(client, telegram_user_id, chat_id, str(contact["id"]), dialog.entity):
                    changed[str(contact["id"])] = contact
        exported = await self._export_pending(telegram_user_id, owner_name)
        return {"contacts": matches, "messages": saved, "exported": exported, "changed_contacts": list(changed.values())}

    def contact_messages(self, telegram_user_id: int, contact_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT sent_at, outgoing, text, media FROM telegram_archived_messages WHERE telegram_user_id = ? AND contact_id = ? ORDER BY sent_at DESC, message_id DESC LIMIT ?",
                (telegram_user_id, contact_id, max(1, min(limit, 2000))),
            ).fetchall()][::-1]

    def create_status_suggestion(self, telegram_user_id: int, contact_id: str, expected: str, suggested: str, reason: str, evidence: list[str]) -> str:
        token = secrets.token_urlsafe(8)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO telegram_status_suggestions VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (token, telegram_user_id, contact_id, expected, suggested, reason, json.dumps(evidence, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
        return token

    def take_status_suggestion(self, token: str, telegram_user_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM telegram_status_suggestions WHERE token = ? AND telegram_user_id = ? AND decided_at IS NULL", (token, telegram_user_id)).fetchone()
        return dict(row) if row else None

    def resolve_status_suggestion(self, token: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE telegram_status_suggestions SET decided_at = ? WHERE token = ?", (datetime.now(timezone.utc).isoformat(), token))

    async def export_contact(self, telegram_user_id: int, contact_id: str, owner_name: str = "") -> str:
        await self._export_pending(telegram_user_id, owner_name, contact_id=contact_id)
        with self._lock:
            row = self._db.execute(
                "SELECT google_url FROM telegram_archive_conversations WHERE telegram_user_id = ? AND contact_id = ? AND google_url != '' LIMIT 1",
                (telegram_user_id, contact_id),
            ).fetchone()
        return str(row["google_url"]) if row else ""

    def allow_contact_archive(self, telegram_user_id: int, contact_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM telegram_archive_exclusions WHERE telegram_user_id = ? AND contact_id = ?",
                (telegram_user_id, contact_id),
            )

    async def delete_contact_archive(self, telegram_user_id: int, contact_id: str) -> bool:
        with self._lock:
            rows = self._db.execute(
                "SELECT google_tab_id FROM telegram_archive_conversations WHERE telegram_user_id = ? AND contact_id = ?",
                (telegram_user_id, contact_id),
            ).fetchall()
        from google_docs import delete_conversation_tab_by_id
        from notion_store import clear_contact_conversation_url
        for row in rows:
            if row["google_tab_id"]:
                await asyncio.to_thread(delete_conversation_tab_by_id, str(row["google_tab_id"]))
        with self._lock, self._db:
            self._db.execute("DELETE FROM telegram_archived_messages WHERE telegram_user_id = ? AND contact_id = ?", (telegram_user_id, contact_id))
            self._db.execute("DELETE FROM telegram_archive_conversations WHERE telegram_user_id = ? AND contact_id = ?", (telegram_user_id, contact_id))
            self._db.execute(
                "INSERT OR REPLACE INTO telegram_archive_exclusions (telegram_user_id, contact_id, excluded_at) VALUES (?, ?, ?)",
                (telegram_user_id, contact_id, datetime.now(timezone.utc).isoformat()),
            )
        await asyncio.to_thread(clear_contact_conversation_url, contact_id)
        return bool(rows)

    async def delete_all_archives(self, telegram_user_id: int) -> int:
        with self._lock:
            contact_ids = [row["contact_id"] for row in self._db.execute(
                "SELECT DISTINCT contact_id FROM telegram_archive_conversations WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchall()]
        for contact_id in contact_ids:
            await self.delete_contact_archive(telegram_user_id, str(contact_id))
        self.set_archive_consent(telegram_user_id, False)
        return len(contact_ids)

    async def begin_qr_login(self, telegram_user_id: int) -> str:
        self._require_configured()
        await self._discard_pending(telegram_user_id)
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        await client.connect()
        try:
            login = await client.qr_login()
        except Exception:
            await client.disconnect()
            raise
        self._pending[telegram_user_id] = _PendingLogin(client=client, login=login)
        return str(login.url)

    async def complete_qr_login(self, telegram_user_id: int, timeout_seconds: int = 90) -> dict:
        pending = self._pending.get(telegram_user_id)
        if not pending:
            raise TelegramUserError("QR-код истёк. Запусти /telegram ещё раз.")
        try:
            account = await pending.login.wait(timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self._discard_pending(telegram_user_id)
            raise TelegramUserError("QR-код истёк. Запусти /telegram ещё раз.") from exc
        except SessionPasswordNeededError as exc:
            if not self._two_factor_configured():
                await self._discard_pending(telegram_user_id)
                raise TelegramUserError("На аккаунте включена 2FA. HTTPS-страница для неё ещё не настроена.") from exc
            token = secrets.token_urlsafe(32)
            pending.two_factor_token = token
            pending.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            return {"requires_2fa": True, "url": f"{self.two_factor_url}/telegram/2fa/{token}"}
        try:
            result = self._save(telegram_user_id, pending.client, account)
            result["telegram_user_id"] = telegram_user_id
            return result
        finally:
            await self._discard_pending(telegram_user_id)

    async def complete_two_factor_login(self, token: str, password: str) -> dict:
        telegram_user_id, pending = self._pending_for_token(token)
        if not password:
            raise TelegramUserError("Введи пароль двухэтапной аутентификации.")
        try:
            account = await pending.client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise TelegramUserError("Неверный пароль.") from exc
        try:
            result = self._save(telegram_user_id, pending.client, account)
            result["telegram_user_id"] = telegram_user_id
            return result
        finally:
            await self._discard_pending(telegram_user_id)

    async def send_message(self, telegram_user_id: int, recipient: str, text: str) -> int:
        if not recipient.strip() or not text.strip():
            raise TelegramUserError("Не указан получатель или текст сообщения.")
        async with self._client(telegram_user_id) as client:
            message = await client.send_message(_recipient(recipient), text.strip())
        return int(message.id)

    async def close(self) -> None:
        for user_id in list(self._pending):
            await self._discard_pending(user_id)
        self._db.close()

    def _ensure_conversation(self, telegram_user_id: int, contact: dict, chat_id: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO telegram_archive_conversations (telegram_user_id, contact_id, chat_id, contact_name)
                VALUES (?, ?, ?, ?) ON CONFLICT(telegram_user_id, contact_id, chat_id) DO UPDATE SET contact_name=excluded.contact_name""",
                (telegram_user_id, str(contact["id"]), chat_id, str(contact.get("name") or "Контакт")),
            )

    def _is_excluded(self, telegram_user_id: int, contact_id: str) -> bool:
        with self._lock:
            return bool(self._db.execute(
                "SELECT 1 FROM telegram_archive_exclusions WHERE telegram_user_id = ? AND contact_id = ?",
                (telegram_user_id, contact_id),
            ).fetchone())

    def _last_message_id(self, telegram_user_id: int, chat_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(message_id) AS message_id FROM telegram_archived_messages WHERE telegram_user_id = ? AND chat_id = ?",
                (telegram_user_id, chat_id),
            ).fetchone()
        return int(row["message_id"] or 0)

    async def _message_text(self, client, message) -> str:
        text = str(message.raw_text or "").strip()
        if not getattr(message, "voice", None):
            return text
        size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        if size > self.voice_transcription_max_bytes:
            return text
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp:
                path = temp.name
            await client.download_media(message, file=path)
            from voice_transcription import transcribe_telegram_voice
            transcript = await asyncio.to_thread(transcribe_telegram_voice, path)
            return f"Транскрипция голосового:\n{transcript}" if transcript else text
        except Exception:
            logger.exception("Unable to transcribe Telegram voice message %s", getattr(message, "id", ""))
            return text
        finally:
            if path:
                with suppress(OSError):
                    os.unlink(path)

    async def _backfill_voice_messages(self, client, telegram_user_id: int, chat_id: int, contact_id: str, entity) -> bool:
        with self._lock:
            rows = self._db.execute(
                "SELECT message_id FROM telegram_archived_messages WHERE telegram_user_id = ? AND chat_id = ? AND contact_id = ? AND media = '[Голосовое сообщение]' AND text = ''",
                (telegram_user_id, chat_id, contact_id),
            ).fetchall()
        updated = False
        for row in rows:
            message = await client.get_messages(entity, ids=int(row["message_id"]))
            if not message:
                continue
            text = await self._message_text(client, message)
            if not text:
                continue
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE telegram_archived_messages SET text = ?, exported_at = NULL WHERE telegram_user_id = ? AND chat_id = ? AND message_id = ?",
                    (text, telegram_user_id, chat_id, int(row["message_id"])),
                )
            updated = True
        return updated

    def _save_message(self, telegram_user_id: int, chat_id: int, contact_id: str, message, *, text: str = "") -> bool:
        media = _media_label(message)
        sent_at = getattr(message, "date", datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO telegram_archived_messages
                (telegram_user_id, chat_id, message_id, contact_id, sent_at, outgoing, sender_id, text, media)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (telegram_user_id, chat_id, int(message.id), contact_id, sent_at, int(bool(message.out)),
                 int(message.sender_id) if message.sender_id else None, text, media),
            )
        return bool(cursor.rowcount)

    async def _export_pending(self, telegram_user_id: int, owner_name: str, contact_id: str | None = None) -> int:
        from google_docs import append_conversation_messages, create_conversation_tab
        from notion_store import update_contact_conversation_url
        with self._lock:
            query = "SELECT DISTINCT contact_id FROM telegram_archived_messages WHERE telegram_user_id = ? AND exported_at IS NULL"
            params: tuple = (telegram_user_id,)
            if contact_id:
                query += " AND contact_id = ?"
                params = (telegram_user_id, contact_id)
            contact_ids = [str(row["contact_id"]) for row in self._db.execute(query, params).fetchall()]
        exported = 0
        for current_contact_id in contact_ids:
            with self._lock:
                conversation = self._db.execute(
                    "SELECT contact_name, google_tab_id, google_url FROM telegram_archive_conversations WHERE telegram_user_id = ? AND contact_id = ? LIMIT 1",
                    (telegram_user_id, current_contact_id),
                ).fetchone()
                messages = [dict(row) for row in self._db.execute(
                    "SELECT chat_id, message_id, sent_at, outgoing, text, media FROM telegram_archived_messages WHERE telegram_user_id = ? AND contact_id = ? AND exported_at IS NULL ORDER BY sent_at, chat_id, message_id",
                    (telegram_user_id, current_contact_id),
                ).fetchall()]
            if not conversation or not messages:
                continue
            tab_id, url = str(conversation["google_tab_id"]), str(conversation["google_url"])
            if not tab_id:
                created = await asyncio.to_thread(create_conversation_tab, str(conversation["contact_name"]), owner_name)
                tab_id, url = created["tab_id"], created["url"]
                with self._lock, self._db:
                    self._db.execute(
                        "UPDATE telegram_archive_conversations SET google_tab_id = ?, google_url = ? WHERE telegram_user_id = ? AND contact_id = ?",
                        (tab_id, url, telegram_user_id, current_contact_id),
                    )
            await asyncio.to_thread(append_conversation_messages, tab_id, messages)
            await asyncio.to_thread(update_contact_conversation_url, current_contact_id, url)
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE telegram_archived_messages SET exported_at = ? WHERE telegram_user_id = ? AND contact_id = ? AND exported_at IS NULL",
                    (datetime.now(timezone.utc).isoformat(), telegram_user_id, current_contact_id),
                )
            exported += len(messages)
        return exported

    async def _discard_pending(self, telegram_user_id: int) -> None:
        pending = self._pending.pop(telegram_user_id, None)
        if pending:
            await pending.client.disconnect()

    def _save(self, telegram_user_id: int, client: TelegramClient, account: object) -> dict:
        session = client.session.save()
        display_name = " ".join(
            part for part in [str(getattr(account, "first_name", "") or ""), str(getattr(account, "last_name", "") or "")]
            if part
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO telegram_user_connections
                (telegram_user_id, session_encrypted, account_id, username, display_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'connected', ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET session_encrypted=excluded.session_encrypted,
                account_id=excluded.account_id, username=excluded.username, display_name=excluded.display_name,
                status='connected', updated_at=excluded.updated_at""",
                (telegram_user_id, self._encrypt(session), int(account.id), str(getattr(account, "username", "") or ""), display_name, now, now),
            )
        return {"username": str(getattr(account, "username", "") or ""), "display_name": display_name}

    def _client(self, telegram_user_id: int) -> TelegramClient:
        self._require_configured()
        with self._lock:
            row = self._db.execute(
                "SELECT session_encrypted FROM telegram_user_connections WHERE telegram_user_id = ? AND status = 'connected'",
                (telegram_user_id,),
            ).fetchone()
        if not row:
            raise TelegramUserError("Личный Telegram не подключён. Открой /telegram.")
        return TelegramClient(StringSession(self._decrypt(str(row["session_encrypted"]))), self.api_id, self.api_hash)

    def _pending_for_token(self, token: str) -> tuple[int, _PendingLogin]:
        for user_id, pending in self._pending.items():
            if secrets.compare_digest(token, pending.two_factor_token) and pending.expires_at and pending.expires_at > datetime.now(timezone.utc):
                return user_id, pending
        raise TelegramUserError("Ссылка недействительна или истекла.")

    def _two_factor_configured(self) -> bool:
        parsed = urlparse(self.two_factor_url)
        return parsed.scheme == "https" and bool(parsed.netloc)

    def _require_configured(self) -> None:
        if not self.configured:
            raise TelegramUserError("Нужны TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_SESSION_ENCRYPTION_KEY.")

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(json.dumps({"session": value}).encode()).decode()

    def _decrypt(self, value: str) -> str:
        try:
            return json.loads(self._fernet.decrypt(value.encode()).decode())["session"]
        except (InvalidToken, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise TelegramUserError("Не удалось прочитать защищённую Telegram-сессию.") from exc


def _fernet(value: str) -> Fernet | None:
    try:
        return Fernet(value.encode()) if value else None
    except ValueError:
        return None


def _recipient(value: str) -> str:
    value = value.strip()
    if "t.me/" in value:
        value = "@" + value.rstrip("/").rsplit("/", 1)[-1]
    return value


def _match_contact(contacts: list[dict], entity) -> dict | None:
    """Match a direct Telegram peer only when one Contacts entry matches it."""
    username = str(getattr(entity, "username", "") or "").casefold().lstrip("@")
    phone = _digits(str(getattr(entity, "phone", "") or ""))
    peer_id = str(getattr(entity, "id", "") or "")
    matched = []
    for contact in contacts:
        value = str(contact.get("telegram") or "").casefold().strip()
        contact_username = value.rsplit("t.me/", 1)[-1].strip("/@ ") if "t.me/" in value else value.lstrip("@")
        contact_phone = _digits(value)
        if username and contact_username == username:
            matched.append(contact)
        elif phone and len(phone) >= 7 and contact_phone == phone:
            matched.append(contact)
        elif peer_id and value == peer_id:
            matched.append(contact)
    return matched[0] if len(matched) == 1 else None


def _digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def _media_label(message) -> str:
    if not getattr(message, "media", None):
        return ""
    if getattr(message, "photo", None):
        return "[Фото]"
    if getattr(message, "video", None):
        return "[Видео]"
    if getattr(message, "voice", None):
        return "[Голосовое сообщение]"
    if getattr(message, "audio", None):
        return "[Аудио]"
    document = getattr(message, "document", None)
    if document:
        return "[Файл]"
    return "[Вложение]"


def two_factor_html(message: str = "") -> str:
    notice = f"<p>{escape(message)}</p>" if message else ""
    return f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>SalesUp Telegram</title><h2>Подтверждение Telegram</h2>{notice}<form method=post><input type=password name=password autocomplete=current-password autofocus required placeholder='Пароль 2FA'><button type=submit>Подтвердить</button></form>"
