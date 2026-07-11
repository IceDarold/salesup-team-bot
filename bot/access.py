"""Team access control through Notion Team Members."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from functools import wraps
from typing import Awaitable, Callable, TypeVar

from telegram import Update, User
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)
ACCESS_CACHE_TTL_SECONDS = int(os.getenv("ACCESS_CACHE_TTL_SECONDS", "300"))

T = TypeVar("T")
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[T]]


def init_access_db() -> None:
    """Kept for startup compatibility. Access is stored in Notion, not SQLite."""


def member_required(handler: Handler[T]) -> Handler[T | int]:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> T | int:
        user = update.effective_user
        member = await get_notion_member(user, context)
        if not member and not is_admin(user):
            await deny_access(update)
            return ConversationHandler.END

        return await handler(update, context)

    return wrapped


def admin_required(handler: Handler[T]) -> Handler[T | None]:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> T | None:
        user = update.effective_user
        if not is_admin(user):
            await _reply(update, "Команда доступна только админам SalesUp.")
            return None

        return await handler(update, context)

    return wrapped


async def get_notion_member(
    user: User | None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    *,
    force: bool = False,
) -> dict | None:
    if not user:
        return None

    cache_key = "notion_team_member"
    cache_time_key = "notion_team_member_cached_at"
    if (
        context is not None
        and not force
        and context.user_data.get(cache_key)
        and time.time() - float(context.user_data.get(cache_time_key) or 0) < ACCESS_CACHE_TTL_SECONDS
    ):
        return context.user_data[cache_key]

    from notion_store import find_team_member_by_telegram

    member = await asyncio.to_thread(find_team_member_by_telegram, user.id, user.username)
    if context is not None and member:
        context.user_data[cache_key] = member
        context.user_data[cache_time_key] = time.time()
    elif context is not None:
        context.user_data.pop(cache_key, None)
        context.user_data.pop(cache_time_key, None)
    return member


def is_admin(user: User | None) -> bool:
    if not user:
        return False

    admin_ids = _env_set("BOT_ADMIN_IDS")
    if str(user.id) in admin_ids:
        return True

    username = normalize_username(user.username)
    admin_usernames = _env_set("BOT_ADMIN_USERNAMES")
    return bool(username and username in admin_usernames)


async def deny_access(update: Update) -> None:
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else "username не задан"
    user_id = str(user.id) if user else "-"
    await _reply(
        update,
        "Прости, у тебя нет доступа к этому боту.\n\n"
        "Это внутренний бот команды SalesUp. Добавь себя в Notion базу Team Members "
        "и заполни Telegram username / Telegram user_id.\n\n"
        f"Твой Telegram: {username}\n"
        f"Твой user_id: {user_id}",
    )


def normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    username = username.strip()
    if username.startswith("@"):
        username = username[1:]
    username = username.lower()
    return username or None


def _env_set(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {
        normalize_username(part) if not part.strip().isdigit() else part.strip()
        for part in raw.split(",")
        if part.strip()
    }


async def _reply(update: Update, text: str) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text)
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)
