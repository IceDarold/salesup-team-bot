"""Encrypted, QR-based personal Telegram connections for SalesUp members."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
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
            return self._save(telegram_user_id, pending.client, account)
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
            return self._save(telegram_user_id, pending.client, account)
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


def two_factor_html(message: str = "") -> str:
    notice = f"<p>{escape(message)}</p>" if message else ""
    return f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>SalesUp Telegram</title><h2>Подтверждение Telegram</h2>{notice}<form method=post><input type=password name=password autocomplete=current-password autofocus required placeholder='Пароль 2FA'><button type=submit>Подтвердить</button></form>"
