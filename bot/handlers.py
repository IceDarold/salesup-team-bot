"""Telegram handlers for the interview-only workflow."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from bot.access import get_notion_member
from bot.agent import SalesUpAgent
from bot.agent_tools import execute_prepared_action
from bot.telegram_user import TelegramUserError
from notion_store import (
    create_contact,
    find_contacts,
    get_contact_form_options,
    get_contact_stats,
    update_contact_status,
    get_scheduled_interviews_for_member,
    list_team_members,
)
from transcriber import TRANSCRIBE_MODEL, transcribe
from sales_agent import research_company_brief, research_document
from google_docs import create_company_research_tab

logger = logging.getLogger(__name__)

START_TIME = datetime.now()
MAX_TELEGRAM_DOWNLOAD_MB = int(os.getenv("MAX_TELEGRAM_DOWNLOAD_MB", "20"))
MAX_URL_DOWNLOAD_MB = int(os.getenv("MAX_URL_DOWNLOAD_MB", "500"))
URL_DOWNLOAD_TIMEOUT = int(os.getenv("URL_DOWNLOAD_TIMEOUT", "300"))
TELEGRAM_LOCAL_MODE = bool(os.getenv("TELEGRAM_BOT_API_BASE_URL"))
TELEGRAM_FILE_TIMEOUT = int(os.getenv("TELEGRAM_FILE_TIMEOUT", "1800"))
VIDEO_EXTRACT_TIMEOUT = int(os.getenv("VIDEO_EXTRACT_TIMEOUT", "1800"))
INSIGHTS_PROGRESS_INTERVAL = int(os.getenv("INSIGHTS_PROGRESS_INTERVAL", "45"))
INTERVIEWS_PAGE_SIZE = int(os.getenv("INTERVIEWS_PAGE_SIZE", "8"))
SUMMARY_CHAT_ID_ENV = "SUMMARY_CHAT_ID"
SUMMARY_CHAT_ID_KEY = "summary_chat_id"
KNOWN_SUMMARY_GROUPS_KEY = "known_summary_groups"
SETTINGS_PATH = Path(os.getenv("BOT_SETTINGS_PATH", "data/settings.json"))
AGENT_PREPARED_ACTION_KEY = "agent_prepared_action"
AGENT_ACTION_PREFIX = "agent_action:"
ARCHIVE_CALLBACK_PREFIX = "archive:"
STATUS_SUGGESTION_PREFIX = "status_suggestion:"
RESEARCH_PREFIX = "research:"

(
    NAME,
    ROLE,
    SEGMENT,
    SUBJECT,
    FORMAT,
    EXPERIENCE,
    INTERVIEW_AUDIO,
    HYPOTHESIS,
    DUPLICATE_DECISION,
    ARTIFACT_DECISION,
    PARTS_COUNT,
    CUSTOM_PARTS_COUNT,
    INTERVIEW_LANGUAGE,
    ARCHIVE_DECISION,
    DEDUPE_MODE_DECISION,
    DEDUPE_REVIEW_DECISION,
) = range(16)

(
    CONTACT_NAME,
    CONTACT_VALUE,
    CONTACT_SEGMENT,
    CONTACT_CUSTOM_SEGMENT,
    CONTACT_SOURCE,
    CONTACT_CUSTOM_SOURCE,
) = range(100, 106)

SEGMENT_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("Школьный учитель", callback_data="seg:Школьный учитель")],
        [InlineKeyboardButton("Индивидуальный репетитор", callback_data="seg:Индивидуальный репетитор")],
        [InlineKeyboardButton("Онлайн-репетитор", callback_data="seg:Онлайн-репетитор")],
        [InlineKeyboardButton("Преподаватель онлайн-школы", callback_data="seg:Преподаватель онлайн-школы")],
        [InlineKeyboardButton("Методист", callback_data="seg:Методист")],
        [InlineKeyboardButton("Директор / Academic Manager", callback_data="seg:Директор / Academic Manager")],
        [InlineKeyboardButton("Другое", callback_data="seg:Другое")],
    ]
)

AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}

VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
    ".wmv",
}

LANGUAGE_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("Русский", callback_data="lang:ru"),
            InlineKeyboardButton("English", callback_data="lang:en"),
        ]
    ]
)


def _telegram_user_service(context: ContextTypes.DEFAULT_TYPE):
    service = getattr(context.application, "_telegram_user_service", None)
    if service is None:
        raise RuntimeError("Telegram user service is not initialized.")
    return service


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member = await get_notion_member(update.effective_user, context)
    display_name = (member or {}).get("name") or update.effective_user.first_name or "привет"
    await update.message.reply_text(
        f"<b>Привет, {display_name}. Я бот-помощник команды SalesUp.</b>\n\n"
        "Помогаю работать с интервью, контактами и кандидатами: принимаю аудио или видео, "
        "делаю транскрипт, извлекаю инсайты через LLM, публикую фидбэк в Telegra.ph "
        "и сохраняю транскрипт в Google Doc.\n\n"
        "Команды:\n"
        "/new - новое интервью\n"
        "/add_contact - добавить контакт\n"
        "/transcript - просто транскрипт в новый Google Doc\n"
        "/stats - статистика по контактам\n"
        "/info - статус бота\n"
        "/help - помощь\n"
        "/summary_chat - текущая группа summary\n"
        "/cancel - отменить текущую анкету",
        parse_mode="HTML",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>SalesUp team bot</b>\n\n"
        "1. /new - начать новое интервью\n"
        "1a. /add_contact - добавить новый контакт в Notion\n"
        "1b. /transcript - просто сделать транскрипт и получить ссылку на новый Google Doc\n"
        "1c. /stats - личная статистика; в группе — общая статистика команды\n"
        "1d. /telegram - подключить личный Telegram\n"
        "1e. /telegram_privacy - настройки архива переписки с контактами\n"
        "1f. /telegram_export <контакт> - обновить архив и получить ссылку\n"
        "1g. /telegram_delete <контакт> - удалить архив контакта\n"
        "1h. /telegram_delete_all - удалить все архивы\n"
        "2. Заполнить короткую анкету\n"
        "3. Отправить voice, audio, video, файл с аудио/видео или прямую ссылку\n"
        "4. Бот пришлёт ссылку на инсайты в Telegra.ph\n"
        "5. Бот создаст новую вкладку в Google Doc и сохранит туда транскрипт\n\n"
        "Админские команды:\n"
        "/add_member @username - добавить участника\n"
        "/members - список участников\n"
        "/remove_member @username - удалить участника\n"
        "/set_summary_chat - настроить текущую группу для summary\n"
        "/summary_chat - показать текущую группу summary",
        parse_mode="HTML",
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = datetime.now() - START_TIME
    members_count = len(await asyncio.to_thread(list_team_members))
    await update.message.reply_text(
        "<b>Статус</b>\n\n"
        f"Режим: <code>интервью -> Google Doc tab</code>\n"
        f"Telegram files: <code>{'local Bot API' if TELEGRAM_LOCAL_MODE else 'public Bot API'}</code>\n"
        f"Транскрибация: <code>{TRANSCRIBE_MODEL}</code>\n"
        f"Участников в базе: <code>{members_count}</code>\n"
        f"Uptime: <code>{str(uptime).split('.')[0]}</code>",
        parse_mode="HTML",
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a personal funnel in private chats and the team funnel in groups."""
    chat = update.effective_chat
    is_group = bool(chat and chat.type != "private")
    member = None if is_group else await get_notion_member(update.effective_user, context)

    try:
        data = await asyncio.to_thread(get_contact_stats, None if is_group else member["id"])
    except Exception:
        logger.exception("Unable to load contact statistics")
        await update.effective_message.reply_text(
            "Не удалось получить статистику из Notion. Попробуй ещё раз чуть позже."
        )
        return
    title = "Команда SalesUp" if is_group else (member or {}).get("name") or "твоя статистика"
    today = data["today"]
    overall = data["all"]
    today_statuses = today["statuses"]
    lines = [
        f"<b>📊 {html.escape(title)} — {data['date'].strftime('%d.%m')}</b>",
        "",
        "<b>Сегодня</b>",
        f"• Касаний: <b>{today['total']}</b>",
        f"• Написали: {today_statuses['Написали']} · Ответили: {today_statuses['Ответил']}",
        f"• Согласились: {today_statuses['Согласился на интервью']} · Интервью: {today_statuses['Интервью']}",
        f"• Отказы / без ответа: {today_statuses['Отказ']} / {today_statuses['No response']}",
        "",
        "<b>Всего</b>",
        f"• Контактов: <b>{overall['total']}</b>",
        f"• Согласились: {overall['agreed']} · Интервью+: {overall['interviews']}",
        f"• Конверсия в согласие: {overall['agreement_conversion']}%",
        f"• Конверсия в интервью: {overall['interview_conversion']}%",
        f"• Доходимость: {overall['attendance']}%",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an ordinary message through the bounded SalesUp agent loop."""
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return
    member = await get_notion_member(update.effective_user, context)
    if not member:
        await message.reply_text("Не нашёл тебя в базе Team Members.")
        return
    member = {**member, "telegram_user_id": update.effective_user.id}

    progress = await message.reply_text("Думаю…")
    chat = update.effective_chat
    try:
        result = await SalesUpAgent().run(
            text=text,
            member=member,
            is_group=bool(chat and chat.type != "private"),
            telegram_service=_telegram_user_service(context),
        )
    except Exception:
        logger.exception("SalesUp agent failed")
        await progress.edit_text("Не смог обработать запрос. Попробуй ещё раз немного позже.")
        return

    if result.prepared_action:
        token = secrets.token_urlsafe(8)
        context.user_data[AGENT_PREPARED_ACTION_KEY] = {
            "token": token,
            "telegram_user_id": update.effective_user.id,
            "action": result.prepared_action,
        }
        await progress.edit_text(
            result.text[:4000],
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f"{AGENT_ACTION_PREFIX}confirm:{token}"),
                        InlineKeyboardButton("Отменить", callback_data=f"{AGENT_ACTION_PREFIX}cancel:{token}"),
                    ]
                ]
            ),
        )
        return
    await progress.edit_text(result.text[:4000])


async def telegram_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Подключать личный Telegram можно только в личном чате с ботом.")
        return
    service = _telegram_user_service(context)
    user_id = update.effective_user.id
    status = service.status(user_id)
    if not status["configured"]:
        await update.effective_message.reply_text(
            "Подключение личного Telegram ещё не настроено на сервере. Нужны API ID, API Hash и ключ шифрования сессий."
        )
        return
    if status["connected"]:
        name = status["display_name"] or (f"@{status['username']}" if status["username"] else "аккаунт")
        archive = service.archive_status(user_id)
        state = "включено" if archive["enabled"] else "не включено"
        await update.effective_message.reply_text(f"Личный Telegram уже подключён: {name}. Архив переписки: {state}.")
        return
    try:
        login_url = await service.begin_qr_login(user_id)
    except TelegramUserError as exc:
        await update.effective_message.reply_text(f"Не удалось начать подключение: {exc}")
        return

    import qrcode

    image = qrcode.make(login_url)
    buffer = io.BytesIO()
    buffer.name = "telegram-login.png"
    image.save(buffer, format="PNG")
    buffer.seek(0)
    await update.effective_message.reply_photo(
        photo=buffer,
        caption="Открой Telegram → Настройки → Устройства → Подключить устройство и отсканируй QR-код. Жду до 90 секунд.",
    )
    asyncio.create_task(_finish_telegram_login(service, user_id, update.effective_message, context))


async def _finish_telegram_login(service, user_id: int, message, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = await service.complete_qr_login(user_id)
    except TelegramUserError as exc:
        await message.reply_text(f"Подключение не завершено: {exc}")
        return
    if result.get("requires_2fa"):
        await message.reply_text(
            "На аккаунте включена двухэтапная аутентификация. Открой защищённую ссылку и введи пароль:\n"
            f"{result['url']}"
        )
        return
    name = result.get("display_name") or (f"@{result.get('username')}" if result.get("username") else "аккаунт")
    await message.reply_text(
        f"Готово — личный Telegram подключён: {name}. Агент сможет предложить сообщение и отправить его только после твоего подтверждения."
    )
    await _ask_archive_consent(message)


async def _ask_archive_consent(message) -> None:
    await message.reply_text(
        "Разрешаете сохранять всю переписку с личными чатами, которые однозначно совпадают с вашими контактами в SalesUp?\n\n"
        "Будут импортированы все доступные входящие и исходящие сообщения, а новые будут синхронизироваться автоматически. "
        "Текст хранится в защищённой базе бота и во вкладке Google Doc контакта. Отключить и удалить архив можно командами /telegram_privacy, /telegram_delete и /telegram_delete_all.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Разрешить полный архив", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}consent:yes")],
            [InlineKeyboardButton("Не разрешать", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}consent:no")],
        ]),
    )


async def telegram_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _telegram_user_service(context)
    status = service.archive_status(update.effective_user.id)
    if status["enabled"]:
        await update.effective_message.reply_text(
            f"Архив включён. Сохранено сообщений: {status['messages']}; контактов: {status['contacts']}.\n"
            "Новые сообщения синхронизируются автоматически. Чтобы отключить и удалить всё, используй /telegram_delete_all."
        )
    else:
        await _ask_archive_consent(update.effective_message)


async def telegram_archive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, value = (query.data or "").split(":", 2)
    except ValueError:
        await query.edit_message_text("Не удалось распознать действие.")
        return
    service = _telegram_user_service(context)
    user_id = update.effective_user.id
    if action == "consent":
        if value != "yes":
            service.set_archive_consent(user_id, False)
            await query.edit_message_text("Архив переписки не включён. Подключение Telegram продолжает работать.")
            return
        service.set_archive_consent(user_id, True)
        await query.edit_message_text("Согласие сохранено. Импортирую всю доступную переписку с контактами в фоне; это может занять некоторое время.")
        member = await get_notion_member(update.effective_user, context)
        contacts = await asyncio.to_thread(find_contacts, member_page_id=(member or {}).get("id"), limit=1000)
        asyncio.create_task(service.sync_archive(user_id, contacts, (member or {}).get("name", "")))
        return
    if action == "delete" and value:
        await service.delete_contact_archive(user_id, value)
        await query.edit_message_text("Архив переписки контакта удалён из базы и Google Doc.")
        return
    if action == "delete_all" and value == "yes":
        count = await service.delete_all_archives(user_id)
        await query.edit_message_text(f"Удалены архивы: {count}. Автосинхронизация отключена.")
        return
    await query.edit_message_text("Действие отменено.")


async def contact_status_suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, decision, token = (query.data or "").split(":", 2)
    except ValueError:
        await query.edit_message_text("Не удалось распознать предложение.")
        return
    suggestion = _telegram_user_service(context).take_status_suggestion(token, update.effective_user.id)
    if not suggestion:
        await query.edit_message_text("Предложение уже обработано или истекло.")
        return
    if decision == "keep":
        _telegram_user_service(context).resolve_status_suggestion(token)
        await query.edit_message_text("Оставил текущий статус без изменений.")
        return
    if decision != "apply":
        await query.edit_message_text("Действие отменено.")
        return
    member = await get_notion_member(update.effective_user, context)
    try:
        current = next((item for item in await asyncio.to_thread(find_contacts, member_page_id=(member or {}).get("id"), limit=1000) if item.get("id") == suggestion["contact_id"]), None)
        if not current or current.get("status") != suggestion["expected_status"]:
            await query.edit_message_text("Статус уже изменился после предложения; ничего не менял.")
            _telegram_user_service(context).resolve_status_suggestion(token)
            return
        await asyncio.to_thread(update_contact_status, contact_id=suggestion["contact_id"], owner_id=(member or {})["id"], status=suggestion["suggested_status"], action_source="Анализ переписки")
    except Exception:
        logger.exception("Unable to apply conversation status suggestion")
        await query.edit_message_text("Не удалось обновить статус в Notion.")
        return
    _telegram_user_service(context).resolve_status_suggestion(token)
    await query.edit_message_text(f"Готово — статус обновлён на «{suggestion['suggested_status']}».")


async def research_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn an uploaded PDF or DOCX into reviewed, sendable outreach drafts."""
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("PDF-ресёрч доступен только в личном чате с ботом.")
        return
    document = update.effective_message.document
    suffix = Path(document.file_name or "").suffix.lower() if document else ""
    if suffix not in {".pdf", ".docx"}:
        return
    progress = await update.effective_message.reply_text("Изучаю документ и ищу релевантных людей в открытых источниках…")
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
            path = temp.name
        await (await document.get_file()).download_to_drive(path)
        candidates = await asyncio.to_thread(research_document, path)
    except Exception:
        logger.exception("PDF research failed")
        await progress.edit_text("Не удалось обработать документ. Проверь, что в нём есть текст, и попробуй ещё раз.")
        return
    finally:
        if path:
            with suppress(OSError):
                os.unlink(path)
    if not candidates:
        await progress.edit_text("Не нашёл подтверждённых кандидатов по этому материалу.")
        return
    token = secrets.token_urlsafe(8)
    context.user_data[f"research:{token}"] = candidates
    await progress.edit_text(f"Готово: нашёл {len(candidates)} кандидатов. Отправляю карточки для подтверждения.")
    for index, candidate in enumerate(candidates):
        await _send_research_candidate(update.effective_message, token, index, candidate)


async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Пришли сюда PDF или DOCX с ресёрчем потенциальных контактов. Я изучу его, проверю данные в открытых источниках и подготовлю карточки с персональными сообщениями.")


async def company_research_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Исследование компании доступно только в личном чате с ботом.")
        return
    request = " ".join(context.args).strip()
    if not request:
        await update.effective_message.reply_text("Использование: /company_research <ссылка на вакансию, сайт компании и любые вводные свободным текстом>")
        return
    progress = await update.effective_message.reply_text("Запускаю глубокое исследование: изучаю компанию, вакансию, людей, рынок и публичные источники. Это займёт несколько минут.")
    asyncio.create_task(_run_company_research(progress, request))


async def _run_company_research(progress, request: str) -> None:
    try:
        report = await asyncio.to_thread(research_company_brief, request)
        if not report:
            raise RuntimeError("Research model returned an empty report.")
        urls = re.findall(r"https?://[^\s]+", request)
        title = urllib.parse.urlparse(urls[0]).netloc if urls else "Компания"
        url = await asyncio.to_thread(create_company_research_tab, title, report)
    except Exception:
        logger.exception("Company research failed")
        await progress.edit_text("Не удалось завершить исследование. Проверь доступность OpenAI API и повтори запрос.")
        return
    await progress.edit_text(f"Исследование готово. Полный отчёт с источниками: {url}")


async def _send_research_candidate(message, token: str, index: int, candidate: dict) -> None:
    sources = "\n".join(f"• {item}" for item in candidate.get("sources", [])[:3]) or "—"
    text = (
        f"Кандидат: {candidate.get('name') or 'не указан'}\n"
        f"Компания / роль: {candidate.get('company') or '—'} · {candidate.get('role') or '—'}\n"
        f"Почему: {candidate.get('why') or '—'}\n\n"
        f"Черновик:\n{candidate.get('message') or '—'}\n\nИсточники:\n{sources}"
    )
    buttons = [[InlineKeyboardButton("✅ Отправить", callback_data=f"{RESEARCH_PREFIX}send:{token}:{index}"), InlineKeyboardButton("Пропустить", callback_data=f"{RESEARCH_PREFIX}skip:{token}:{index}")]]
    await message.reply_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def research_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, decision, token, index_text = (query.data or "").split(":", 3)
        candidate = (context.user_data.get(f"research:{token}") or [])[int(index_text)]
    except (ValueError, IndexError, TypeError):
        await query.edit_message_text("Карточка больше недоступна.")
        return
    if decision == "skip":
        await query.edit_message_text("Пропущено.")
        return
    recipient = str(candidate.get("telegram") or "").strip()
    text = str(candidate.get("message") or "").strip()
    if not recipient or not text:
        await query.edit_message_text("У кандидата нет подтверждённого Telegram-ника или черновика. Сообщение не отправлено.")
        return
    try:
        await _telegram_user_service(context).send_message(update.effective_user.id, recipient, text)
    except Exception:
        logger.exception("Unable to send research outreach")
        await query.edit_message_text("Не удалось отправить сообщение. Проверь подключение личного Telegram.")
        return
    await query.edit_message_text("Готово — сообщение отправлено от твоего имени.")


async def telegram_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Использование: /telegram_export <имя или часть имени контакта>")
        return
    member = await get_notion_member(update.effective_user, context)
    contacts = await asyncio.to_thread(find_contacts, member_page_id=(member or {}).get("id"), query=query, limit=2)
    if len(contacts) != 1:
        await update.effective_message.reply_text("Нужен один точно найденный контакт. Уточни имя.")
        return
    service = _telegram_user_service(context)
    if not service.archive_status(update.effective_user.id)["enabled"]:
        await update.effective_message.reply_text("Сначала включи архив через /telegram_privacy.")
        return
    await update.effective_message.reply_text("Синхронизирую и обновляю Google Doc…")
    service.allow_contact_archive(update.effective_user.id, contacts[0]["id"])
    await service.sync_archive(update.effective_user.id, contacts, (member or {}).get("name", ""))
    url = await service.export_contact(update.effective_user.id, contacts[0]["id"], (member or {}).get("name", ""))
    await update.effective_message.reply_text(url or "Для этого контакта пока не найден личный Telegram-чат.")


async def telegram_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text("Использование: /telegram_delete <имя или часть имени контакта>")
        return
    member = await get_notion_member(update.effective_user, context)
    contacts = await asyncio.to_thread(find_contacts, member_page_id=(member or {}).get("id"), query=query, limit=2)
    if len(contacts) != 1:
        await update.effective_message.reply_text("Нужен один точно найденный контакт. Уточни имя.")
        return
    await update.effective_message.reply_text(
        f"Удалить весь архив переписки с «{contacts[0]['name']}» из базы и Google Doc?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Удалить", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}delete:{contacts[0]['id']}")],
            [InlineKeyboardButton("Отмена", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}cancel:no")],
        ]),
    )


async def telegram_delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Удалить все архивы переписки из базы и Google Docs, а также отключить синхронизацию? Это нельзя отменить.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Удалить всё", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}delete_all:yes")],
            [InlineKeyboardButton("Отмена", callback_data=f"{ARCHIVE_CALLBACK_PREFIX}cancel:no")],
        ]),
    )


async def agent_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, decision, token = (query.data or "").split(":", 2)
    except ValueError:
        await query.edit_message_text("Не удалось распознать действие.")
        return

    pending = context.user_data.get(AGENT_PREPARED_ACTION_KEY) or {}
    if pending.get("token") != token or pending.get("telegram_user_id") != update.effective_user.id:
        await query.edit_message_text("Это действие больше недоступно.")
        return
    action = pending.get("action") or {}
    try:
        expired = datetime.fromisoformat(str(action.get("expires_at") or "")) <= datetime.now(timezone.utc)
    except ValueError:
        expired = True
    if expired:
        context.user_data.pop(AGENT_PREPARED_ACTION_KEY, None)
        await query.edit_message_text("Срок подтверждения истёк. Отправь запрос ещё раз.")
        return
    if decision == "cancel":
        context.user_data.pop(AGENT_PREPARED_ACTION_KEY, None)
        await query.edit_message_text("Действие отменено.")
        return
    if decision != "confirm":
        await query.edit_message_text("Неизвестное действие.")
        return

    await query.edit_message_text("Сохраняю в Notion…")
    try:
        url = await execute_prepared_action(action, _telegram_user_service(context))
    except Exception:
        logger.exception("Unable to execute confirmed agent action")
        await query.edit_message_text("Не удалось выполнить действие в Notion. Ничего не изменено.")
        return

    context.user_data.pop(AGENT_PREPARED_ACTION_KEY, None)
    if action.get("kind") == "record_team_action":
        text = "Готово — действие записано в журнал команды."
    elif action.get("kind") == "send_telegram_message":
        text = "Готово — сообщение отправлено от твоего имени."
    elif action.get("kind") == "update_contact_status":
        text = "Готово — статус контакта обновлён в Notion."
    else:
        text = "Готово — контакт добавлен в Notion со статусом «Новый»."
    if url:
        text += f"\n{url}"
    await query.edit_message_text(text)


async def add_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    member = await get_notion_member(update.effective_user, context, force=True)
    if not member:
        await update.effective_message.reply_text("Не нашёл тебя в базе Team Members.")
        return ConversationHandler.END

    context.user_data.pop("new_contact", None)
    context.user_data["new_contact"] = {"owner_id": member["id"]}
    await update.effective_message.reply_text("<b>1/4</b> Как зовут человека или компанию?", parse_mode="HTML")
    return CONTACT_NAME


async def contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text or "").strip()
    if not name:
        await update.effective_message.reply_text("Напиши имя или название контакта.")
        return CONTACT_NAME
    context.user_data["new_contact"]["name"] = name
    await update.effective_message.reply_text(
        "<b>2/4</b> Укажи контакт: телефон, @username, email или ссылку.", parse_mode="HTML"
    )
    return CONTACT_VALUE


async def contact_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = (update.effective_message.text or "").strip()
    if not contact:
        await update.effective_message.reply_text("Укажи способ связи с человеком.")
        return CONTACT_VALUE
    context.user_data["new_contact"]["contact"] = contact
    return await _ask_contact_segment(update.effective_message, context)


async def contact_segment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "new":
        await query.message.reply_text("<b>3/4</b> Напиши новый сегмент.", parse_mode="HTML")
        return CONTACT_CUSTOM_SEGMENT

    segments = context.user_data.get("contact_segments") or []
    if not value.isdigit() or int(value) >= len(segments):
        await query.message.reply_text("Выбери сегмент кнопкой ниже.")
        return CONTACT_SEGMENT
    context.user_data["new_contact"]["segment"] = segments[int(value)]
    return await _ask_contact_source(query.message, context)


async def contact_custom_segment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    segment = (update.effective_message.text or "").strip()
    if not segment:
        await update.effective_message.reply_text("Напиши название нового сегмента.")
        return CONTACT_CUSTOM_SEGMENT
    context.user_data["new_contact"]["segment"] = segment
    return await _ask_contact_source(update.effective_message, context)


async def contact_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "new":
        await query.message.reply_text("<b>4/4</b> Напиши новый источник.", parse_mode="HTML")
        return CONTACT_CUSTOM_SOURCE

    sources = context.user_data.get("contact_sources") or []
    if not value.isdigit() or int(value) >= len(sources):
        await query.message.reply_text("Выбери источник кнопкой ниже.")
        return CONTACT_SOURCE
    context.user_data["new_contact"]["source"] = sources[int(value)]
    return await _save_contact(query.message, context)


async def contact_custom_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    source = (update.effective_message.text or "").strip()
    if not source:
        await update.effective_message.reply_text("Напиши название нового источника.")
        return CONTACT_CUSTOM_SOURCE
    context.user_data["new_contact"]["source"] = source
    return await _save_contact(update.effective_message, context)


async def cancel_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_contact", None)
    context.user_data.pop("contact_segments", None)
    context.user_data.pop("contact_sources", None)
    await update.effective_message.reply_text("Добавление контакта отменено.")
    return ConversationHandler.END


async def _ask_contact_segment(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        options = await asyncio.to_thread(get_contact_form_options)
    except Exception:
        logger.exception("Unable to load Contact segments")
        await message.reply_text("Не удалось загрузить сегменты из Notion. Попробуй ещё раз позже.")
        return ConversationHandler.END
    context.user_data["contact_segments"] = options["segments"]
    buttons = [[InlineKeyboardButton(item, callback_data=f"contact_segment:{index}")] for index, item in enumerate(options["segments"])]
    buttons.append([InlineKeyboardButton("➕ Новый сегмент", callback_data="contact_segment:new")])
    await message.reply_text("<b>3/4</b> Выбери сегмент:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    return CONTACT_SEGMENT


async def _ask_contact_source(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        options = await asyncio.to_thread(get_contact_form_options)
    except Exception:
        logger.exception("Unable to load Contact sources")
        await message.reply_text("Не удалось загрузить источники из Notion. Попробуй ещё раз позже.")
        return ConversationHandler.END
    context.user_data["contact_sources"] = options["sources"]
    buttons = [[InlineKeyboardButton(item, callback_data=f"contact_source:{index}")] for index, item in enumerate(options["sources"])]
    buttons.append([InlineKeyboardButton("➕ Новый источник", callback_data="contact_source:new")])
    await message.reply_text("<b>4/4</b> Выбери источник:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    return CONTACT_SOURCE


async def _save_contact(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    payload = context.user_data.get("new_contact") or {}
    try:
        url = await asyncio.to_thread(create_contact, **payload)
    except Exception:
        logger.exception("Unable to create Contact in Notion")
        await message.reply_text("Не удалось добавить контакт в Notion. Попробуй ещё раз позже.")
        return ConversationHandler.END

    context.user_data.pop("new_contact", None)
    context.user_data.pop("contact_segments", None)
    context.user_data.pop("contact_sources", None)
    reply = "Готово — контакт добавлен в Notion со статусом «Новый»."
    if url:
        reply += f"\n{url}"
    await message.reply_text(reply)
    return ConversationHandler.END


async def add_member_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Участники теперь управляются только в Notion.\n\n"
        "Открой базу Team Members и добавь строку с Telegram username и Telegram user_id."
    )


async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    members = await asyncio.to_thread(list_team_members)
    if not members:
        await update.message.reply_text("В базе пока нет участников.")
        return

    lines = ["<b>Участники SalesUp bot</b>"]
    for member in members:
        username = member.get("telegram_username") or "-"
        user_id = member.get("telegram_user_id") or "-"
        display_name = member.get("name") or ""
        lines.append(f"{username} | <code>{user_id}</code> {display_name}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def set_summary_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    if chat.type == "private":
        await update.message.reply_text(
            "Эту команду нужно вызвать в группе, куда бот должен присылать summary встреч."
        )
        return

    _remember_group_chat(context, chat)
    context.bot_data[SUMMARY_CHAT_ID_KEY] = chat.id
    _save_setting(SUMMARY_CHAT_ID_KEY, chat.id)
    await _send_summary_chat_welcome(context, chat.id)


async def summary_chat_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type != "private":
        _remember_group_chat(context, chat)

    chat_id = _summary_chat_id(context)
    groups = await _refresh_known_summary_groups(context)
    if chat and chat.type == "private" and groups:
        buttons = []
        for group in groups:
            marker = "✓ " if str(group["id"]) == str(chat_id or "") else ""
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{marker}{group['title']}",
                        callback_data=f"summary_chat:{group['id']}",
                    )
                ]
            )
        current = _group_title_by_id(groups, chat_id) if chat_id else ""
        text = "Выбери группу для summary встреч:"
        if current:
            text += f"\n\nСейчас выбрана: <b>{html.escape(current)}</b>"
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if not chat_id:
        await update.message.reply_text(
            "Группа для summary не настроена. Добавь бота в нужную группу и вызови там /summary_chat "
            "или /set_summary_chat, чтобы бот её запомнил."
        )
        return

    title = _group_title_by_id(groups, chat_id)
    if title:
        await update.message.reply_text(
            f"Summary-группа настроена: <b>{html.escape(title)}</b>\n<code>{chat_id}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"Summary-группа настроена: <code>{chat_id}</code>", parse_mode="HTML")


async def choose_summary_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.data.split(":", 1)[1]
    groups = await _refresh_known_summary_groups(context)
    group = next((item for item in groups if str(item["id"]) == chat_id), None)
    if not group:
        await query.edit_message_text(
            "Я больше не вижу эту группу среди доступных. Возможно, бота удалили из неё. "
            "Добавь бота обратно и вызови /summary_chat в нужной группе."
        )
        return

    context.bot_data[SUMMARY_CHAT_ID_KEY] = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    _save_setting(SUMMARY_CHAT_ID_KEY, context.bot_data[SUMMARY_CHAT_ID_KEY])
    await query.edit_message_text(
        f"Готово. Summary встреч будет уходить в группу: <b>{html.escape(group['title'])}</b>.",
        parse_mode="HTML",
    )
    await _send_summary_chat_welcome(context, context.bot_data[SUMMARY_CHAT_ID_KEY])


async def remember_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat and chat.type != "private":
        _remember_group_chat(context, chat)


async def remember_bot_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_member = update.my_chat_member
    if not chat_member:
        return
    chat = chat_member.chat
    if not chat or chat.type == "private":
        return

    new_status = getattr(chat_member.new_chat_member, "status", "")
    if new_status in {"left", "kicked"}:
        _forget_group_chat(context, chat.id)
    else:
        _remember_group_chat(context, chat)


async def remove_member_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Удаление участников теперь делается только в Notion базе Team Members."
    )


async def new_interview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_interview_context(context)
    member = await get_notion_member(update.effective_user, context, force=True)
    if not member:
        await update.message.reply_text(
            "Я не нашёл тебя в Notion базе Team Members.\n\n"
            f"Твой Telegram: @{update.effective_user.username or '-'}\n"
            f"Твой user_id: {update.effective_user.id}\n\n"
            "Добавь эти данные в Team Members и попробуй /new ещё раз."
        )
        return ConversationHandler.END

    context.user_data["current_team_member"] = member
    interviews = await asyncio.to_thread(get_scheduled_interviews_for_member, member["id"])
    if not interviews:
        await update.message.reply_text(
            "У тебя нет интервью со статусом Sheduled в Notion.\n\n"
            "Сначала добавь контакт в Interviews, поставь Status = Sheduled и Owner = себя."
        )
        return ConversationHandler.END

    context.user_data["scheduled_interviews"] = {item["id"]: item for item in interviews}
    context.user_data["scheduled_interviews_order"] = [item["id"] for item in interviews]
    await _send_interviews_page(update.message, context, page=0)
    return NAME


async def new_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_interview_context(context)
    context.user_data["transcript_only"] = True
    context.user_data["interview"] = {
        "name": f"Транскрипт {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "role": "-",
        "segment": "-",
        "subject": "-",
        "format": "-",
        "experience": "-",
        "hypothesis": "plain transcript",
    }
    await update.message.reply_text(
        "Сделаю только транскрипт и создам отдельный новый Google Doc.\n\n"
        "Сначала выбери язык интервью."
    )
    await _ask_interview_language(update.message)
    return INTERVIEW_LANGUAGE


async def interviews_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    await _send_interviews_page(query.message, context, page=page, edit=True)
    return NAME


async def interview_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    interview_id = query.data.split(":", 1)[1]
    interview = context.user_data.get("scheduled_interviews", {}).get(interview_id)
    if not interview:
        from notion_store import get_interview

        interview = await asyncio.to_thread(get_interview, interview_id)

    segment = ", ".join(interview.get("segment") or [])
    member = context.user_data.get("current_team_member") or {}
    context.user_data["interview"] = {
        "notion_interview_page_id": interview["id"],
        "name": interview.get("name") or "-",
        "segment": segment or "-",
        "role": "-",
        "subject": "-",
        "format": "-",
        "experience": "-",
        "hypothesis": _clean_answer(interview.get("goal") or "", limit=1000),
        "interviewer_name": member.get("name") or "-",
        "interviewer_telegram": member.get("telegram_username") or "-",
    }
    goal = context.user_data["interview"]["hypothesis"]
    await query.edit_message_text(
        "<b>Интервью выбрано</b>\n\n"
        f"Респондент: {interview.get('name') or '-'}\n"
        f"Дата: {interview.get('meeting_date') or '-'}\n"
        f"Сегмент: {segment or '-'}\n"
        f"Goal: {goal or '-'}",
        parse_mode="HTML",
    )
    existing = await _find_existing_transcript(interview)
    if existing and existing.get("source") == "check_failed":
        await query.message.reply_text(
            "Не смог проверить, есть ли уже транскрипт в Google Doc. "
            "Чтобы не создать дубль, остановил обработку. Попробуй /new ещё раз позже."
        )
        _clear_interview_context(context)
        return ConversationHandler.END
    if existing:
        context.user_data["existing_transcript"] = existing
        await query.message.reply_text(
            "Я нашёл уже существующий транскрипт для этого интервью:\n"
            f"{existing['url']}\n\n"
            "Что делаем?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Использовать старый",
                            callback_data="existing_transcript:use",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Перегенерировать",
                            callback_data="existing_transcript:regenerate",
                        )
                    ],
                ]
            ),
        )
        return DUPLICATE_DECISION

    if goal:
        await _ask_interview_language(query.message)
        return INTERVIEW_LANGUAGE

    context.user_data["goal_was_missing"] = True
    await _ask_goal(query.message)
    return HYPOTHESIS


async def existing_transcript_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":", 1)[1]
    existing = context.user_data.get("existing_transcript") or {}

    if decision == "use":
        transcript = existing.get("transcript") or ""
        if not transcript.strip():
            await query.edit_message_text(
                "Ссылку на старый транскрипт нашёл, но не смог прочитать текст из Google Doc. "
                "Нужно перегенерировать."
            )
            context.user_data["use_existing_transcript"] = False
        else:
            context.user_data["use_existing_transcript"] = True
            await query.edit_message_text(
                "Ок, используем старый транскрипт. Сейчас задам последний вопрос."
            )
    else:
        context.user_data["use_existing_transcript"] = False
        context.user_data["regenerating_transcript"] = True
        await query.edit_message_text("Ок, перегенерируем транскрипт из нового аудио или видео.")

    answers = context.user_data.get("interview") or {}
    if answers.get("hypothesis"):
        if context.user_data.get("use_existing_transcript"):
            return await _process_existing_transcript(query.message, context)
        await _ask_interview_language(query.message)
        return INTERVIEW_LANGUAGE

    context.user_data["goal_was_missing"] = True
    await _ask_goal(query.message)
    return HYPOTHESIS


async def duplicate_decision_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери вариант кнопкой: использовать старый или перегенерировать.")
    return DUPLICATE_DECISION


async def _ask_goal(msg) -> None:
    await msg.reply_text(
        "<b>Какая цель этого интервью?</b>\n\n"
        "Поле Goal в Notion не заполнено. Напиши, что хотели проверить или понять в этом разговоре. "
        "Например: понять, нужен ли учителям AI-фидбэк по уроку; проверить готовность платить; "
        "найти сильные боли в текущей работе.",
        parse_mode="HTML",
    )


async def choose_interview_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери интервью кнопкой из списка выше.")
    return NAME


async def interview_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["name"] = _clean_answer(update.message.text)
    await update.message.reply_text("<b>Роль респондента:</b>", parse_mode="HTML")
    return ROLE


async def interview_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["role"] = _clean_answer(update.message.text)
    await update.message.reply_text(
        "<b>Сегмент:</b>",
        parse_mode="HTML",
        reply_markup=SEGMENT_KB,
    )
    return SEGMENT


async def interview_segment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        segment = query.data.split(":", 1)[1]
        await query.edit_message_text(f"<b>Сегмент:</b> {segment}", parse_mode="HTML")
        msg = query.message
    else:
        segment = _clean_answer(update.message.text)
        msg = update.message

    context.user_data["interview"]["segment"] = segment
    await msg.reply_text("<b>Предмет / направление:</b>", parse_mode="HTML")
    return SUBJECT


async def interview_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["subject"] = _clean_answer(update.message.text)
    await update.message.reply_text("<b>Формат занятий:</b>", parse_mode="HTML")
    return FORMAT


async def interview_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["format"] = _clean_answer(update.message.text)
    await update.message.reply_text("<b>Опыт:</b>", parse_mode="HTML")
    return EXPERIENCE


async def interview_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["experience"] = _clean_answer(update.message.text)
    await update.message.reply_text(
        "<b>Какая цель этого интервью?</b>\n\n"
        "Например: понять, нужен ли учителям AI-фидбэк по уроку; проверить готовность платить; "
        "найти сильные боли в текущей работе.",
        parse_mode="HTML",
    )
    return HYPOTHESIS


async def interview_hypothesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["interview"]["hypothesis"] = _clean_answer(update.message.text, limit=1000)
    answers = context.user_data["interview"]
    if context.user_data.pop("goal_was_missing", False):
        await _save_goal_to_notion(update.message, answers)

    if context.user_data.get("use_existing_transcript"):
        return await _process_existing_transcript(update.message, context)

    await _ask_interview_language(update.message)
    return INTERVIEW_LANGUAGE


async def _ask_interview_language(msg) -> None:
    await msg.reply_text(
        "<b>На каком языке интервью?</b>",
        parse_mode="HTML",
        reply_markup=LANGUAGE_KB,
    )


async def interview_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    language = query.data.split(":", 1)[1]
    if language not in {"ru", "en"}:
        await query.edit_message_text("Выбери язык кнопкой: ru или en.")
        return INTERVIEW_LANGUAGE

    answers = context.user_data.get("interview") or {}
    answers["language"] = language
    label = "русский" if language == "ru" else "английский"
    await query.edit_message_text(f"Ок, язык интервью: {label}.")
    await _ask_parts_count(query.message, answers)
    return PARTS_COUNT


async def interview_language_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip().lower()
    aliases = {
        "ru": "ru",
        "русский": "ru",
        "russian": "ru",
        "en": "en",
        "english": "en",
        "английский": "en",
    }
    language = aliases.get(raw)
    if not language:
        await update.message.reply_text("Выбери язык кнопкой или напиши ru / en.")
        return INTERVIEW_LANGUAGE

    answers = context.user_data.get("interview") or {}
    answers["language"] = language
    await _ask_parts_count(update.message, answers)
    return PARTS_COUNT


async def interview_parts_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "more":
        await query.edit_message_text(
            "Из скольки файлов состоит запись? Напиши число от 1 до 20."
        )
        return CUSTOM_PARTS_COUNT

    expected_parts = int(value)
    await query.edit_message_text(f"Ок, запись состоит из {expected_parts} файл(ов).")
    await _set_expected_audio_parts(query.message, context, expected_parts)
    return INTERVIEW_AUDIO


async def interview_parts_count_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери количество файлов кнопкой.")
    return PARTS_COUNT


async def custom_parts_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await update.message.reply_text("Нужно отправить именно число. Например: 4")
        return CUSTOM_PARTS_COUNT

    expected_parts = int(raw)
    if expected_parts < 1 or expected_parts > 20:
        await update.message.reply_text("Число должно быть от 1 до 20.")
        return CUSTOM_PARTS_COUNT

    await _set_expected_audio_parts(update.message, context, expected_parts)
    return INTERVIEW_AUDIO


async def archive_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    decision = query.data.split(":", 1)[1]
    archived_tab = context.user_data.get("archived_old_transcript_tab") or {}

    delete_tab_id = archived_tab.get("delete_tab_id") or archived_tab.get("tab_id")
    if decision == "delete" and delete_tab_id:
        await query.edit_message_text("Удаляю старую архивную вкладку и сохраняю новую транскрипцию...")
        try:
            from google_docs import delete_tab_by_id

            await asyncio.to_thread(delete_tab_by_id, delete_tab_id)
            context.user_data["old_transcript_deleted"] = True
        except Exception as e:
            logger.exception("Failed to delete archived Google Doc tab")
            await query.message.reply_text(
                "Не смог удалить старую архивную вкладку, но продолжу и сохраню новую транскрипцию.\n"
                f"Ошибка: {e}"
            )
    else:
        await query.edit_message_text("Оставляю старую вкладку архивной. Сохраняю новую транскрипцию...")

    answers = context.user_data.get("interview") or {}
    transcript = context.user_data.get("pending_regenerated_transcript") or ""
    if not answers or not transcript:
        await query.message.reply_text("Не нашёл новую транскрипцию в состоянии бота. Начни заново через /new.")
        _clear_interview_context(context)
        return ConversationHandler.END

    try:
        doc_url = await _ensure_transcript_saved(query.message, answers, transcript, progress_msg=query.message)
    except Exception as e:
        logger.exception("Failed to save regenerated transcript after archive decision")
        await query.message.reply_text(
            "Старую вкладку уже обработал, но новую транскрипцию не смог сохранить в Google Doc:\n"
            f"{e}\n\n"
            "Лучше повторить /new."
        )
        _clear_interview_context(context)
        return ConversationHandler.END

    context.user_data.pop("pending_regenerated_transcript", None)
    return await _ask_artifact_decision(query.message, context, answers, transcript, doc_url)


async def archive_decision_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери кнопкой: удалить старую вкладку или оставить архивной.")
    return ARCHIVE_DECISION


async def interview_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    answers = context.user_data.get("interview")
    if not answers:
        await msg.reply_text("Анкета не найдена. Начни заново через /new.")
        return ConversationHandler.END

    tmp_path = None
    try:
        expected_parts = int(context.user_data.get("audio_expected_parts") or 1)
        audio_parts = context.user_data.setdefault("audio_parts", [])
        part_number = len(audio_parts) + 1
        progress_msg = await msg.reply_text(
            f"Получаю файл {part_number}/{expected_parts}. Для больших файлов это может занять несколько минут."
        )
        tmp_path = await _save_audio_input(update, context, progress_msg=progress_msg)
        if not tmp_path:
            await _edit_progress(
                progress_msg,
                f"Отправь файл {part_number}/{expected_parts}: voice, audio/video-файл или прямую ссылку.",
            )
            return INTERVIEW_AUDIO

        file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        audio_parts.append(tmp_path)
        tmp_path = None
        await _edit_progress(progress_msg, f"Файл {part_number}/{expected_parts} получен ({file_size_mb:.1f} MB).")

        if len(audio_parts) < expected_parts:
            await _ask_next_audio_part(msg, context)
            return INTERVIEW_AUDIO

        await _edit_progress(
            progress_msg,
            f"Все {expected_parts} файл(ов) получены. Начинаю транскрибацию по порядку."
        )
        t0 = time.time()
        transcript = await _transcribe_audio_parts(progress_msg, audio_parts, answers.get("language"))
        elapsed = time.time() - t0

        if not transcript.strip():
            _cleanup_audio_parts(context)
            await _edit_progress(progress_msg, "Не удалось распознать речь. Попробуй другую запись.")
            await _ask_parts_count(msg, answers)
            return PARTS_COUNT

        await _edit_progress(progress_msg, f"Транскрипция готова за {elapsed:.1f}s.")

        if context.user_data.get("transcript_only"):
            await _edit_progress(progress_msg, "Транскрипция готова. Создаю новый Google Doc...")
            from google_docs import add_transcript_document

            title = answers.get("name") or f"Транскрипт {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            doc_url = await asyncio.to_thread(
                add_transcript_document,
                title,
                transcript,
                answers.get("language"),
            )
            _cleanup_audio_parts(context)
            await _edit_progress(
                progress_msg,
                f"Готово. Транскрипт сохранён в новом Google Doc:\n{doc_url}",
            )
            _clear_interview_context(context)
            return ConversationHandler.END

        if context.user_data.get("regenerating_transcript"):
            archive_state = await _archive_old_tab_before_rewrite(
                msg,
                context,
                answers,
                transcript,
                progress_msg=progress_msg,
            )
            if archive_state is not None:
                _cleanup_audio_parts(context)
                return archive_state

        doc_url = await _ensure_transcript_saved(msg, answers, transcript, progress_msg=progress_msg)
        _cleanup_audio_parts(context)
        return await _ask_artifact_decision(msg, context, answers, transcript, doc_url)
    except Exception as e:
        logger.exception("Interview processing failed")
        _cleanup_audio_parts(context)
        await msg.reply_text(
            "Не получилось обработать интервью:\n"
            f"{e}\n\n"
            "Аудиочасти сброшены, анкета сохранена. Можно заново выбрать количество файлов или /cancel."
        )
        await _ask_parts_count(msg, answers)
        return PARTS_COUNT
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def cancel_interview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_interview_context(context)
    await update.message.reply_text("Интервью отменено.")
    return ConversationHandler.END


def _clean_answer(text: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _interview_button_title(interview: dict) -> str:
    name = interview.get("name") or "Без имени"
    date_value = interview.get("meeting_date") or "без даты"
    segment = ", ".join(interview.get("segment") or []) or "без сегмента"
    goal_marker = "goal ok" if (interview.get("goal") or "").strip() else "без goal"
    title = f"{name} · {date_value} · {segment} · {goal_marker}"
    return title[:64]


async def _ask_parts_count(msg, answers: dict) -> None:
    language = answers.get("language") or "-"
    if answers.get("hypothesis") == "plain transcript":
        await msg.reply_text(
            f"Язык: {language}\n\n"
            "Из скольки файлов состоит запись?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("1", callback_data="parts:1"),
                        InlineKeyboardButton("2", callback_data="parts:2"),
                        InlineKeyboardButton("3", callback_data="parts:3"),
                    ],
                    [InlineKeyboardButton("Больше", callback_data="parts:more")],
                ]
            ),
        )
        return

    await msg.reply_text(
        "Анкета заполнена.\n\n"
        f"Имя: {answers.get('name')}\n"
        f"Роль: {answers.get('role')}\n"
        f"Сегмент: {answers.get('segment')}\n"
        f"Предмет: {answers.get('subject')}\n"
        f"Формат: {answers.get('format')}\n"
        f"Опыт: {answers.get('experience')}\n\n"
        f"Цель: {answers.get('hypothesis')}\n\n"
        f"Язык: {language}\n\n"
        "Из скольки файлов состоит запись интервью?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("1", callback_data="parts:1"),
                    InlineKeyboardButton("2", callback_data="parts:2"),
                    InlineKeyboardButton("3", callback_data="parts:3"),
                ],
                [InlineKeyboardButton("Больше", callback_data="parts:more")],
            ]
        ),
    )


async def _set_expected_audio_parts(msg, context: ContextTypes.DEFAULT_TYPE, expected_parts: int) -> None:
    _cleanup_audio_parts(context)
    context.user_data["audio_expected_parts"] = expected_parts
    context.user_data["audio_parts"] = []
    await _ask_next_audio_part(msg, context)


async def _ask_next_audio_part(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    expected_parts = int(context.user_data.get("audio_expected_parts") or 1)
    current_part = len(context.user_data.get("audio_parts") or []) + 1
    await msg.reply_text(
        f"Отправь часть {current_part}/{expected_parts} в правильном порядке: "
        "voice, audio/video-файл или прямую ссылку на аудио/видео."
    )


async def _transcribe_audio_parts(progress_msg, paths: list[str], language: str | None) -> str:
    transcripts = []
    total = len(paths)
    for index, path in enumerate(paths, start=1):
        language_label = language or "default"
        await _edit_progress(progress_msg, f"Транскрибирую часть {index}/{total} (язык: {language_label})...")
        part_transcript = await asyncio.to_thread(transcribe, path, None, language)
        if part_transcript.strip():
            if total > 1:
                transcripts.append(f"## Часть {index}\n\n{part_transcript.strip()}")
            else:
                transcripts.append(part_transcript.strip())
    return "\n\n".join(transcripts).strip()


def _cleanup_audio_parts(context: ContextTypes.DEFAULT_TYPE) -> None:
    for path in context.user_data.get("audio_parts") or []:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError:
            logger.warning("Failed to remove temp audio part: %s", path)
    context.user_data.pop("audio_parts", None)
    context.user_data.pop("audio_expected_parts", None)


async def _save_goal_to_notion(msg, answers: dict) -> None:
    interview_page_id = answers.get("notion_interview_page_id")
    goal = answers.get("hypothesis")
    if not interview_page_id or not goal:
        return
    try:
        from notion_store import update_interview_goal

        await asyncio.to_thread(update_interview_goal, interview_page_id, goal)
    except Exception:
        logger.exception("Failed to save interview goal to Notion")
        await msg.reply_text(
            "Цель принял, но не смог сохранить её в поле Goal в Notion. "
            "Продолжаю обработку интервью."
        )


async def _process_existing_transcript(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data.get("interview") or {}
    existing = context.user_data.get("existing_transcript") or {}
    transcript = existing.get("transcript") or ""
    doc_url = existing.get("url")
    if not transcript.strip() or not doc_url:
        await msg.reply_text(
            "Не удалось использовать старый транскрипт. Отправь аудио или видео, чтобы перегенерировать."
        )
        context.user_data["use_existing_transcript"] = False
        return INTERVIEW_AUDIO

    await msg.reply_text("Использую старый транскрипт. Проверяю, какие отчёты уже есть.")
    return await _ask_artifact_decision(msg, context, answers, transcript, doc_url)


async def _edit_progress(progress_msg, text: str) -> None:
    if not progress_msg:
        return
    try:
        await progress_msg.edit_text(text)
    except Exception as e:
        message = str(e).lower()
        if "message is not modified" in message:
            return
        logger.debug("Failed to edit progress message: %s", e)


def _html_link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _clear_interview_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    _cleanup_audio_parts(context)
    for key in [
        "interview",
        "existing_transcript",
        "use_existing_transcript",
        "goal_was_missing",
        "ready_transcript",
        "ready_doc_url",
        "existing_report_url",
        "existing_feedback_url",
        "current_team_member",
        "transcript_only",
        "regenerating_transcript",
        "pending_regenerated_transcript",
        "archived_old_transcript_tab",
        "old_transcript_deleted",
        "pending_notion_pipeline",
        "pending_dedupe_plan",
        "pending_dedupe_review_items",
        "pending_dedupe_review_index",
        "pending_dedupe_mode",
    ]:
        context.user_data.pop(key, None)


async def _reply_long_text(msg, text: str, chunk_size: int = 3500) -> None:
    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, chunk_size)
        if split_at < chunk_size // 2:
            split_at = remaining.rfind("\n", 0, chunk_size)
        if split_at < chunk_size // 2:
            split_at = chunk_size

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    for index, chunk in enumerate(chunks, start=1):
        prefix = f"Часть {index}/{len(chunks)}\n\n" if len(chunks) > 1 else ""
        await msg.reply_text(prefix + chunk)


async def _send_interviews_page(msg, context: ContextTypes.DEFAULT_TYPE, page: int, edit: bool = False) -> None:
    order = context.user_data.get("scheduled_interviews_order") or []
    interviews = context.user_data.get("scheduled_interviews") or {}
    total = len(order)
    total_pages = max(1, (total + INTERVIEWS_PAGE_SIZE - 1) // INTERVIEWS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * INTERVIEWS_PAGE_SIZE
    end = start + INTERVIEWS_PAGE_SIZE

    buttons = []
    for interview_id in order[start:end]:
        item = interviews.get(interview_id)
        if not item:
            continue
        buttons.append(
            [InlineKeyboardButton(_interview_button_title(item), callback_data=f"interview:{interview_id}")]
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Назад", callback_data=f"interviews_page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Вперёд ›", callback_data=f"interviews_page:{page + 1}"))
    if nav:
        buttons.append(nav)

    text = (
        "<b>Выбери интервью, которое нужно обработать:</b>\n\n"
        f"Показаны {start + 1}-{min(end, total)} из {total}. Страница {page + 1}/{total_pages}."
    )
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _find_existing_transcript(interview: dict) -> dict | None:
    transcript_url = interview.get("transcript")
    if transcript_url:
        from google_docs import get_tab_info_from_url, read_transcript_from_url

        try:
            tab_info = await asyncio.to_thread(get_tab_info_from_url, transcript_url)
            transcript = (tab_info or {}).get("transcript")
            if transcript is None:
                transcript = await asyncio.to_thread(read_transcript_from_url, transcript_url)
        except Exception:
            logger.exception("Failed to read existing transcript from Notion URL")
            tab_info = None
            transcript = ""
        return {
            "source": "notion",
            "url": transcript_url,
            "transcript": transcript or "",
            "tab_id": (tab_info or {}).get("tab_id"),
            "title": (tab_info or {}).get("title"),
        }

    from google_docs import find_interview_by_name

    try:
        existing = await asyncio.to_thread(find_interview_by_name, interview.get("name") or "")
    except Exception as e:
        logger.exception("Failed to search existing transcript in Google Doc")
        return {"source": "check_failed", "error": str(e)}
    if not existing:
        return None
    return {
        "source": "google_doc",
        "url": existing["url"],
        "transcript": existing.get("transcript") or "",
        "tab_id": existing.get("tab_id"),
        "title": existing.get("title"),
    }


async def _archive_old_tab_before_rewrite(
    msg,
    context: ContextTypes.DEFAULT_TYPE,
    answers: dict,
    transcript: str,
    *,
    progress_msg=None,
) -> int | None:
    existing = context.user_data.get("existing_transcript") or {}
    tab_id = existing.get("tab_id")
    if not tab_id:
        await _edit_progress(
            progress_msg,
            "Транскрипция готова. Не нашёл tabId старой вкладки, поэтому сохраню новую без архивирования старой.",
        )
        return None

    await _edit_progress(progress_msg, "Транскрипция готова. Архивирую старую вкладку...")
    try:
        from google_docs import archive_tab

        archived = await asyncio.to_thread(archive_tab, tab_id, existing.get("title"))
    except Exception as e:
        logger.exception("Failed to archive old Google Doc tab before regenerated transcript save")
        await msg.reply_text(
            "Новая транскрипция готова, но не смог переименовать старую вкладку в _archived.\n"
            f"Ошибка: {e}\n\n"
            "Чтобы не создать путаницу в Google Doc, остановил сохранение. Можно повторить /new позже."
        )
        _clear_interview_context(context)
        return ConversationHandler.END

    context.user_data["pending_regenerated_transcript"] = transcript
    context.user_data["archived_old_transcript_tab"] = archived
    archived_action = (
        "Старая вкладка переименована в архивную"
        if archived.get("mode") == "renamed"
        else "Создана архивная копия старой вкладки"
    )
    await msg.reply_text(
        f"{archived_action}:\n"
        f"{_html_link(archived['title'], archived['url'])}\n\n"
        "Удалить старую вкладку перед созданием новой?",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Удалить старую", callback_data="archive:delete")],
                [InlineKeyboardButton("Оставить архивной", callback_data="archive:keep")],
            ]
        ),
    )
    return ARCHIVE_DECISION


async def _ensure_transcript_saved(
    msg,
    answers: dict,
    transcript: str,
    doc_url: str | None = None,
    progress_msg=None,
) -> str:
    if not doc_url:
        if progress_msg:
            await _edit_progress(progress_msg, "Сохраняю транскрипт в Google Doc...")
        else:
            await msg.reply_text("Сохраняю транскрипт в Google Doc...")
        from google_docs import add_interview

        doc_url = await asyncio.to_thread(add_interview, answers, transcript)

    interview_page_id = answers.get("notion_interview_page_id")
    if interview_page_id:
        try:
            from notion_store import update_interview_transcript

            await asyncio.to_thread(update_interview_transcript, interview_page_id, doc_url)
        except Exception:
            logger.exception("Failed to save transcript URL to Notion")
    return doc_url


async def _ask_artifact_decision(
    msg,
    context: ContextTypes.DEFAULT_TYPE,
    answers: dict,
    transcript: str,
    doc_url: str,
) -> int:
    existing_report_url = ""
    existing_feedback_url = ""
    interview_page_id = answers.get("notion_interview_page_id")
    if interview_page_id:
        try:
            from notion_store import get_interview

            interview = await asyncio.to_thread(get_interview, interview_page_id)
            existing_report_url = interview.get("telegra_ph_report") or ""
            existing_feedback_url = interview.get("interviewer_feedback") or ""
        except Exception:
            logger.exception("Failed to check existing interview artifacts in Notion")

    context.user_data["ready_transcript"] = transcript
    context.user_data["ready_doc_url"] = doc_url
    context.user_data["existing_report_url"] = existing_report_url
    context.user_data["existing_feedback_url"] = existing_feedback_url

    lines = ["Транскрипт сохранён.", f"Транскрипт: {_html_link('Google Doc', doc_url)}"]
    if existing_report_url:
        lines.append(f"Уже есть анализ интервью: {_html_link('Telegra.ph', existing_report_url)}")
    if existing_feedback_url:
        lines.append(f"Уже есть фидбэк интервьюеру: {_html_link('Telegra.ph', existing_feedback_url)}")

    missing_report = not existing_report_url
    missing_feedback = not existing_feedback_url

    buttons = []
    if missing_report and missing_feedback:
        lines.append(
            "\nАнализа и фидбэка ещё нет. Фидбэк считается из того же JSON-анализа, "
            "поэтому обычно лучше делать оба сразу. Что сделать?"
        )
        buttons = [
            [InlineKeyboardButton("Сделать анализ и фидбэк", callback_data="artifacts:1:1")],
            [InlineKeyboardButton("Только анализ", callback_data="artifacts:1:0")],
            [InlineKeyboardButton("Только фидбэк", callback_data="artifacts:0:1")],
            [InlineKeyboardButton("Ничего", callback_data="artifacts:0:0")],
        ]
    elif missing_report:
        lines.append("\nАнализа интервью ещё нет. Фидбэк уже есть. Что сделать?")
        buttons = [
            [InlineKeyboardButton("Сделать анализ", callback_data="artifacts:1:0")],
            [InlineKeyboardButton("Переделать анализ и фидбэк", callback_data="artifacts:1:1")],
            [InlineKeyboardButton("Не делать", callback_data="artifacts:0:0")],
        ]
    elif missing_feedback:
        lines.append(
            "\nФидбэка интервьюеру ещё нет. Он считается из того же JSON-анализа; "
            "можно только сделать фидбэк или заодно переделать анализ."
        )
        buttons = [
            [InlineKeyboardButton("Сделать фидбэк", callback_data="artifacts:0:1")],
            [InlineKeyboardButton("Переделать анализ и сделать фидбэк", callback_data="artifacts:1:1")],
            [InlineKeyboardButton("Не делать", callback_data="artifacts:0:0")],
        ]
    else:
        lines.append("\nАнализ и фидбэк уже есть. Можно переделать любой из них.")
        buttons = [
            [InlineKeyboardButton("Переделать анализ и фидбэк", callback_data="artifacts:1:1")],
            [InlineKeyboardButton("Переделать анализ", callback_data="artifacts:1:0")],
            [InlineKeyboardButton("Переделать фидбэк", callback_data="artifacts:0:1")],
            [InlineKeyboardButton("Ничего", callback_data="artifacts:0:0")],
        ]

    await msg.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ARTIFACT_DECISION


async def artifact_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, do_report_raw, do_feedback_raw = query.data.split(":")
    do_report = do_report_raw == "1"
    do_feedback = do_feedback_raw == "1"
    await query.edit_message_text("Принял. Запускаю выбранную обработку." if (do_report or do_feedback) else "Ок, ничего не генерирую.")

    if not do_report and not do_feedback:
        doc_url = context.user_data.get("ready_doc_url") or ""
        lines = ["<b>Готово.</b>"]
        if doc_url:
            lines.append(f"Транскрипт: {_html_link('Google Doc', doc_url)}")
        existing_report_url = context.user_data.get("existing_report_url") or ""
        existing_feedback_url = context.user_data.get("existing_feedback_url") or ""
        if existing_report_url:
            lines.append(f"Анализ интервью: {_html_link('Telegra.ph', existing_report_url)}")
        if existing_feedback_url:
            lines.append(f"Фидбэк интервьюеру: {_html_link('Telegra.ph', existing_feedback_url)}")
        await query.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        _clear_interview_context(context)
        return ConversationHandler.END

    answers = context.user_data.get("interview") or {}
    transcript = context.user_data.get("ready_transcript") or ""
    doc_url = context.user_data.get("ready_doc_url") or ""
    if not answers or not transcript or not doc_url:
        await query.message.reply_text("Не нашёл сохранённый транскрипт в состоянии бота. Начни заново через /new.")
        _clear_interview_context(context)
        return ConversationHandler.END

    next_state = await _process_ready_transcript(
        query.message,
        context,
        answers,
        transcript,
        doc_url,
        progress_msg=query.message,
        do_report=do_report,
        do_feedback=do_feedback,
    )
    if next_state is not None:
        return next_state
    _clear_interview_context(context)
    return ConversationHandler.END


async def artifact_decision_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери действие кнопкой.")
    return ARTIFACT_DECISION


async def _process_ready_transcript(
    msg,
    context: ContextTypes.DEFAULT_TYPE,
    answers: dict,
    transcript: str,
    doc_url: str,
    *,
    progress_msg=None,
    do_report: bool,
    do_feedback: bool,
) -> int | None:
    report_url = None
    feedback_url = None
    notion_url = None
    pipeline_failed = False
    try:
        await _edit_progress(progress_msg or msg, "Запускаю JSON-анализ интервью...")
        analysis = await _analyze_with_progress(progress_msg or msg, answers, transcript)
        await _edit_progress(progress_msg or msg, "JSON-анализ готов.")
        from insights import generate_interviewer_feedback_report, generate_report

        if do_report:
            await _edit_progress(progress_msg or msg, "Формирую readable report...")
            report = await asyncio.to_thread(generate_report, analysis)
            try:
                from telegraph_publish import publish_insights

                await _edit_progress(progress_msg or msg, "Публикую анализ интервью в Telegra.ph...")
                report_url = await asyncio.to_thread(publish_insights, answers, report)
            except Exception as e:
                logger.exception("Telegra.ph publishing failed")
                pipeline_failed = True
                await _edit_progress(
                    progress_msg or msg,
                    "Не получилось опубликовать инсайты в Telegra.ph. "
                    "Полный отчёт в чат не отправляю.\n\n"
                    f"Ошибка: {e}"
                )

        if do_feedback:
            feedback_report = generate_interviewer_feedback_report(analysis)
            try:
                from telegraph_publish import publish_interviewer_feedback

                await _edit_progress(progress_msg or msg, "Публикую личный фидбэк интервьюеру в Telegra.ph...")
                feedback_url = await asyncio.to_thread(publish_interviewer_feedback, answers, feedback_report)
            except Exception as e:
                logger.exception("Interviewer feedback Telegra.ph publishing failed")
                pipeline_failed = True
                await _edit_progress(
                    progress_msg or msg,
                    "Не получилось опубликовать личный фидбэк в Telegra.ph. "
                    "Текст фидбэка в чат не отправляю.\n\n"
                    f"Ошибка: {e}"
                )

            if feedback_url:
                try:
                    from notion_store import update_interviewer_feedback_url

                    await _edit_progress(progress_msg or msg, "Сохраняю ссылку на фидбэк в Notion...")
                    await asyncio.to_thread(
                        update_interviewer_feedback_url,
                        answers.get("notion_interview_page_id"),
                        feedback_url,
                    )
                except Exception as e:
                    logger.exception("Failed to save interviewer feedback URL to Notion")
                    pipeline_failed = True
                    await _edit_progress(
                        progress_msg or msg,
                        "Фидбэк опубликован в Telegra.ph, но ссылку не удалось сохранить в Notion. "
                        "Ссылка будет в итоговом сообщении.\n\n"
                        f"Ошибка: {e}"
                    )

        if do_report:
            context.user_data["pending_notion_pipeline"] = {
                "answers": answers,
                "analysis": analysis,
                "doc_url": doc_url,
                "report_url": report_url,
                "feedback_url": feedback_url,
                "pipeline_failed": pipeline_failed,
            }
            return await _ask_dedupe_mode(msg, progress_msg or msg)
    except Exception as e:
        logger.exception("Interview insights/Notion pipeline failed")
        pipeline_failed = True
        await _edit_progress(
            progress_msg or msg,
            "LLM/Notion pipeline не выполнен, но транскрипт сохранён в Google Doc.\n\n"
            f"Ошибка: {e}"
        )

    await _send_processing_done(msg, progress_msg or msg, doc_url, report_url, feedback_url, notion_url, pipeline_failed)
    return None


async def _send_processing_done(
    msg,
    progress_msg,
    doc_url: str,
    report_url: str | None,
    feedback_url: str | None,
    notion_url: str | None,
    pipeline_failed: bool,
) -> None:
    await _edit_progress(progress_msg, "Обработка завершена." if not pipeline_failed else "Обработка завершена частично.")
    done_lines = [
        "<b>Готово.</b>" if not pipeline_failed else "<b>Частично готово.</b>",
        f"Транскрипт: {_html_link('Google Doc', doc_url)}",
    ]
    if report_url:
        done_lines.append(f"Анализ интервью: {_html_link('Telegra.ph', report_url)}")
    if feedback_url:
        done_lines.append(f"Фидбэк интервьюеру: {_html_link('Telegra.ph', feedback_url)}")
    if notion_url:
        done_lines.append(f"Notion: {_html_link('страница интервью', notion_url)}")
    await msg.reply_text("\n".join(done_lines), parse_mode="HTML", disable_web_page_preview=True)


async def _ask_dedupe_mode(msg, progress_msg) -> int:
    await _edit_progress(progress_msg, "Анализ готов. Перед сохранением в Notion выбери режим дедупликации.")
    await msg.reply_text(
        "<b>Сейчас буду сохранять инсайты в Notion.</b>\n\n"
        "Выбери режим дедупликации:\n"
        "Auto - бот сам объединит уверенные дубли, спорные создаст отдельно.\n"
        "Review uncertain - бот спросит только спорные случаи.\n"
        "Manual - бот спросит все случаи, где есть похожая существующая запись.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Auto", callback_data="dedupe_mode:auto")],
                [InlineKeyboardButton("Review uncertain", callback_data="dedupe_mode:review_uncertain")],
                [InlineKeyboardButton("Manual", callback_data="dedupe_mode:manual")],
            ]
        ),
    )
    return DEDUPE_MODE_DECISION


async def dedupe_mode_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    if mode not in {"auto", "review_uncertain", "manual"}:
        await query.edit_message_text("Не понял режим. Выбери кнопкой.")
        return DEDUPE_MODE_DECISION

    context.user_data["pending_dedupe_mode"] = mode
    await query.edit_message_text("Читаю текущие записи Notion и строю dedupe-план...")
    try:
        pipeline = context.user_data.get("pending_notion_pipeline") or {}
        from notion_store import build_dedupe_plan

        plan = await asyncio.to_thread(
            build_dedupe_plan,
            pipeline.get("analysis") or {},
            interview_page_id=(pipeline.get("answers") or {}).get("notion_interview_page_id"),
        )
    except Exception as e:
        logger.exception("Failed to build Notion dedupe plan")
        await query.edit_message_text(
            "Не смог построить dedupe-план. Инсайты пока не сохранены в Notion.\n\n"
            f"Ошибка: {e}"
        )
        _clear_interview_context(context)
        return ConversationHandler.END

    _prepare_plan_for_mode(plan, mode)
    context.user_data["pending_dedupe_plan"] = plan
    review_items = _dedupe_review_items(plan, mode)
    context.user_data["pending_dedupe_review_items"] = review_items
    context.user_data["pending_dedupe_review_index"] = 0
    if review_items:
        await _ask_next_dedupe_review(query.message, context)
        return DEDUPE_REVIEW_DECISION

    return await _save_pending_notion_pipeline(query.message, context)


async def dedupe_mode_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери режим дедупликации кнопкой.")
    return DEDUPE_MODE_DECISION


async def dedupe_review_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action not in {"merge", "create"}:
        await query.edit_message_text("Не понял действие. Выбери кнопкой.")
        return DEDUPE_REVIEW_DECISION

    review_items = context.user_data.get("pending_dedupe_review_items") or []
    index = int(context.user_data.get("pending_dedupe_review_index") or 0)
    if index >= len(review_items):
        return await _save_pending_notion_pipeline(query.message, context)

    item = review_items[index]
    plan = context.user_data.get("pending_dedupe_plan") or {}
    _apply_dedupe_review_choice(plan, item, action)
    context.user_data["pending_dedupe_plan"] = plan
    context.user_data["pending_dedupe_review_index"] = index + 1
    await query.edit_message_text("Принял: объединяю." if action == "merge" else "Принял: создаю отдельно.")

    if index + 1 < len(review_items):
        await _ask_next_dedupe_review(query.message, context)
        return DEDUPE_REVIEW_DECISION
    return await _save_pending_notion_pipeline(query.message, context)


async def dedupe_review_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Выбери: объединить или создать отдельно.")
    return DEDUPE_REVIEW_DECISION


async def _ask_next_dedupe_review(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    review_items = context.user_data.get("pending_dedupe_review_items") or []
    index = int(context.user_data.get("pending_dedupe_review_index") or 0)
    item = review_items[index]
    new_item = item.get("new_item") or {}
    existing = item.get("existing_item") or {}
    decision = item.get("decision") or {}
    text = (
        f"<b>Dedupe review {index + 1}/{len(review_items)}</b>\n"
        f"Таблица: {html.escape(item.get('label') or item.get('table_key') or '-')}\n\n"
        f"<b>Новая запись:</b>\n{html.escape(_clean_answer(new_item.get('title') or '-', 900))}\n\n"
        f"<b>Похожая существующая:</b>\n{html.escape(_clean_answer(existing.get('title') or '-', 900))}\n\n"
        f"<b>Причина LLM:</b>\n{html.escape(_clean_answer(decision.get('reason') or '-', 900))}"
    )
    await msg.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Объединить", callback_data="dedupe_review:merge")],
                [InlineKeyboardButton("Создать отдельно", callback_data="dedupe_review:create")],
            ]
        ),
    )


async def _save_pending_notion_pipeline(msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    pipeline = context.user_data.get("pending_notion_pipeline") or {}
    answers = pipeline.get("answers") or {}
    analysis = pipeline.get("analysis") or {}
    doc_url = pipeline.get("doc_url") or ""
    report_url = pipeline.get("report_url")
    feedback_url = pipeline.get("feedback_url")
    pipeline_failed = bool(pipeline.get("pipeline_failed"))
    notion_url = None
    try:
        await msg.reply_text("Сохраняю инсайты в Notion с учётом дедупликации...")
        from notion_store import save_analysis_to_notion

        notion_url = await asyncio.to_thread(
            save_analysis_to_notion,
            answers,
            analysis,
            transcript_url=doc_url,
            report_url=report_url,
            dedupe_plan=context.user_data.get("pending_dedupe_plan") or {},
        )
        await _send_group_summary(context, answers, analysis, report_url, notion_url)
    except Exception as e:
        logger.exception("Failed to save deduped analysis to Notion")
        pipeline_failed = True
        await msg.reply_text(
            "Не смог сохранить dedupe-анализ в Notion. Telegra.ph и транскрипт остаются доступными.\n\n"
            f"Ошибка: {e}"
        )

    await _send_processing_done(msg, msg, doc_url, report_url, feedback_url, notion_url, pipeline_failed)
    _clear_interview_context(context)
    return ConversationHandler.END


def _prepare_plan_for_mode(plan: dict, mode: str) -> None:
    if mode != "auto":
        return
    for table in (plan.get("tables") or {}).values():
        for decision in table.get("decisions") or []:
            if decision.get("decision") == "needs_review":
                decision["decision"] = "create_new"


def _dedupe_review_items(plan: dict, mode: str) -> list[dict]:
    if mode == "auto":
        return []
    review_items = []
    for table_key, table in (plan.get("tables") or {}).items():
        existing_by_id = {item.get("id"): item for item in table.get("existing_items") or []}
        new_by_id = {item.get("temp_id"): item for item in table.get("new_items") or []}
        for index, decision in enumerate(table.get("decisions") or []):
            existing_id = decision.get("existing_id")
            should_review = decision.get("decision") == "needs_review"
            if mode == "manual":
                should_review = bool(existing_id)
            if not should_review or not existing_id:
                continue
            review_items.append(
                {
                    "table_key": table_key,
                    "label": table.get("label"),
                    "decision_index": index,
                    "decision": decision,
                    "new_item": new_by_id.get(decision.get("temp_id")) or {},
                    "existing_item": existing_by_id.get(existing_id) or {},
                }
            )
    return review_items


def _apply_dedupe_review_choice(plan: dict, review_item: dict, action: str) -> None:
    table = (plan.get("tables") or {}).get(review_item.get("table_key")) or {}
    decisions = table.get("decisions") or []
    index = review_item.get("decision_index")
    if not isinstance(index, int) or index < 0 or index >= len(decisions):
        return
    decisions[index]["decision"] = "merge_existing" if action == "merge" else "create_new"


async def _send_group_summary(
    context: ContextTypes.DEFAULT_TYPE,
    answers: dict,
    analysis: dict,
    report_url: str | None,
    notion_url: str | None,
) -> None:
    chat_id = _summary_chat_id(context)
    if not chat_id or not report_url:
        return

    interview = analysis.get("interview") or {}
    name = _clean_answer(answers.get("name") or interview.get("respondent_label") or "интервью")
    summary = _clean_answer(interview.get("summary") or "Summary не сгенерировано.", limit=1800)
    fit = _clean_answer(interview.get("aich_value_fit") or "-", limit=50)
    icp_fit = _clean_answer(interview.get("icp_fit") or "-", limit=50)
    interviewer = _format_interviewer_for_group(answers)
    quotes = _collect_group_summary_quotes(analysis, limit=3)
    lines = [
        f"<b>Проанализировано интервью: {html.escape(name)}</b>",
        f"Интервьюер: {html.escape(interviewer)}",
        "",
        html.escape(summary),
        "",
    ]
    if quotes:
        lines.extend(["<b>Цитаты:</b>"])
        for quote in quotes:
            lines.append(f"• “{html.escape(quote)}”")
        lines.append("")

    lines.extend(
        [
            f"SalesUp value fit: <code>{html.escape(fit)}</code>",
            f"ICP fit: <code>{html.escape(icp_fit)}</code>",
            "",
            f"Подробный разбор: {_html_link('Telegra.ph', report_url)}",
        ]
    )
    if notion_url:
        lines.append(f"Notion: {_html_link('страница интервью', notion_url)}")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to send interview summary to group chat %s", chat_id)


def _format_interviewer_for_group(answers: dict) -> str:
    name = _clean_answer(answers.get("interviewer_name") or "-", limit=100)
    username = _clean_answer(answers.get("interviewer_telegram") or "-", limit=100)
    if username and username != "-" and not username.startswith("@"):
        username = f"@{username}"
    if name and name != "-" and username and username != "-":
        return f"{name} ({username})"
    if name and name != "-":
        return name
    if username and username != "-":
        return username
    return "не указан"


def _collect_group_summary_quotes(analysis: dict, limit: int = 3) -> list[str]:
    quotes = []

    def add(value) -> None:
        quote = _clean_answer(value or "", limit=260)
        if not quote or quote == "нет данных":
            return
        normalized = quote.casefold()
        if any(item.casefold() == normalized for item in quotes):
            return
        quotes.append(quote)

    wtp = analysis.get("willingness_to_pay") or {}
    add(wtp.get("evidence_quote"))
    for section in ("pains", "jtbd", "barriers", "risks"):
        for item in analysis.get(section) or []:
            if len(quotes) >= limit:
                return quotes
            if isinstance(item, dict):
                add(item.get("evidence_quote"))
    return quotes[:limit]


def _summary_chat_id(context: ContextTypes.DEFAULT_TYPE) -> int | str | None:
    chat_id = context.bot_data.get(SUMMARY_CHAT_ID_KEY)
    if chat_id:
        return chat_id
    stored_chat_id = _load_settings().get(SUMMARY_CHAT_ID_KEY)
    if stored_chat_id:
        context.bot_data[SUMMARY_CHAT_ID_KEY] = stored_chat_id
        return stored_chat_id
    env_chat_id = os.getenv(SUMMARY_CHAT_ID_ENV)
    if not env_chat_id:
        return None
    return int(env_chat_id) if env_chat_id.lstrip("-").isdigit() else env_chat_id


def _remember_group_chat(context: ContextTypes.DEFAULT_TYPE, chat) -> None:
    groups = dict(context.bot_data.get(KNOWN_SUMMARY_GROUPS_KEY) or {})
    title = chat.title or chat.full_name or str(chat.id)
    groups[str(chat.id)] = {
        "id": chat.id,
        "title": title,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    context.bot_data[KNOWN_SUMMARY_GROUPS_KEY] = groups


def _forget_group_chat(context: ContextTypes.DEFAULT_TYPE, chat_id) -> None:
    groups = dict(context.bot_data.get(KNOWN_SUMMARY_GROUPS_KEY) or {})
    groups.pop(str(chat_id), None)
    context.bot_data[KNOWN_SUMMARY_GROUPS_KEY] = groups
    if str(context.bot_data.get(SUMMARY_CHAT_ID_KEY) or "") == str(chat_id):
        context.bot_data.pop(SUMMARY_CHAT_ID_KEY, None)
        _save_setting(SUMMARY_CHAT_ID_KEY, None)


def _known_summary_groups(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    groups = context.bot_data.get(KNOWN_SUMMARY_GROUPS_KEY) or {}
    return sorted(groups.values(), key=lambda item: item.get("title") or "")


async def _refresh_known_summary_groups(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    groups = _known_summary_groups(context)
    if not groups:
        return []

    bot = await context.bot.get_me()
    for group in list(groups):
        chat_id = group.get("id")
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            if member.status in {"left", "kicked"}:
                _forget_group_chat(context, chat_id)
        except Exception:
            logger.info("Forgetting inaccessible summary group %s", chat_id)
            _forget_group_chat(context, chat_id)
    return _known_summary_groups(context)


def _group_title_by_id(groups: list[dict], chat_id) -> str:
    for group in groups:
        if str(group.get("id")) == str(chat_id):
            return group.get("title") or ""
    return ""


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read bot settings from %s", SETTINGS_PATH)
        return {}


def _save_setting(key: str, value) -> None:
    settings = _load_settings()
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
    tmp_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, SETTINGS_PATH)


async def _send_summary_chat_welcome(context: ContextTypes.DEFAULT_TYPE, chat_id) -> None:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Эта группа настроена для summary встреч SalesUp.\n\n"
                "После каждого проанализированного интервью я буду присылать сюда короткое summary "
                "и ссылку на подробный разбор."
            ),
        )
    except Exception:
        logger.exception("Failed to send summary chat welcome to %s", chat_id)


async def _analyze_with_progress(msg, answers: dict, transcript: str) -> str:
    from insights import analyze_interview

    task = asyncio.create_task(asyncio.to_thread(analyze_interview, answers, transcript))
    progress_messages = [
        "JSON-анализ ещё идёт. Большое интервью может обрабатываться несколько минут.",
        "LLM всё ещё извлекает структурированные инсайты. Транскрипт уже сохранён, бот не завис.",
        "Продолжаю анализ. Если LLM не ответит вовремя, транскрипт всё равно останется в Google Doc.",
    ]
    message_index = 0

    while not task.done():
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=INSIGHTS_PROGRESS_INTERVAL,
            )
        except asyncio.TimeoutError:
            await _edit_progress(msg, progress_messages[message_index % len(progress_messages)])
            message_index += 1

    return await task


def _has_telegram_audio(msg) -> bool:
    return bool(msg.voice or msg.audio or msg.video or msg.video_note or msg.document)


async def _save_audio_input(update: Update, context: ContextTypes.DEFAULT_TYPE, progress_msg=None) -> str | None:
    msg = update.message
    if msg.voice:
        return await _download_telegram_file(context, msg.voice.file_id, ".ogg", msg.voice.file_size)

    if msg.audio:
        audio = msg.audio
        file_name = getattr(audio, "file_name", None) or "interview.ogg"
        suffix = Path(file_name).suffix.lower() or ".ogg"
        if suffix not in AUDIO_EXTENSIONS:
            suffix = ".ogg"
        return await _download_telegram_file(context, audio.file_id, suffix, audio.file_size)

    if msg.video:
        video = msg.video
        file_name = getattr(video, "file_name", None) or "interview.mp4"
        suffix = Path(file_name).suffix.lower() or ".mp4"
        if suffix not in VIDEO_EXTENSIONS:
            suffix = ".mp4"
        video_path = await _download_telegram_file(context, video.file_id, suffix, video.file_size)
        await _edit_progress(progress_msg, "Видео получено. Извлекаю аудиодорожку...")
        return await asyncio.to_thread(_extract_audio_from_video, video_path)

    if msg.video_note:
        video_path = await _download_telegram_file(
            context,
            msg.video_note.file_id,
            ".mp4",
            msg.video_note.file_size,
        )
        await _edit_progress(progress_msg, "Видео получено. Извлекаю аудиодорожку...")
        return await asyncio.to_thread(_extract_audio_from_video, video_path)

    if msg.document:
        file_name = msg.document.file_name or "interview"
        suffix = Path(file_name).suffix.lower()
        mime_type = msg.document.mime_type or ""
        is_audio = suffix in AUDIO_EXTENSIONS or mime_type.startswith("audio/")
        is_video = suffix in VIDEO_EXTENSIONS or mime_type.startswith("video/")
        if not is_audio and not is_video:
            raise ValueError("Это не похоже на аудио или видео. Отправь voice, audio, video или файл с аудио/видео.")
        if is_video:
            if suffix not in VIDEO_EXTENSIONS:
                suffix = ".mp4"
            video_path = await _download_telegram_file(
                context, msg.document.file_id, suffix, msg.document.file_size
            )
            await _edit_progress(progress_msg, "Видео получено. Извлекаю аудиодорожку...")
            return await asyncio.to_thread(_extract_audio_from_video, video_path)
        if suffix not in AUDIO_EXTENSIONS:
            suffix = ".ogg"
        return await _download_telegram_file(
            context, msg.document.file_id, suffix, msg.document.file_size
        )

    if msg.text:
        urls = re.findall(r"https?://[^\s]+", msg.text)
        if urls:
            await _edit_progress(progress_msg, "Скачиваю файл по ссылке...")
            path = await asyncio.to_thread(_download_url, urls[0])
            if _path_looks_like_video(path):
                await _edit_progress(progress_msg, "Видео скачано. Извлекаю аудиодорожку...")
                return await asyncio.to_thread(_extract_audio_from_video, path)
            return path

    return None


async def _download_telegram_file(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    suffix: str,
    file_size: int | None,
) -> str:
    if (
        not TELEGRAM_LOCAL_MODE
        and file_size
        and file_size > MAX_TELEGRAM_DOWNLOAD_MB * 1024 * 1024
    ):
        raise ValueError(
            f"Файл больше {MAX_TELEGRAM_DOWNLOAD_MB} MB. "
            "Для таких файлов нужен локальный Telegram Bot API server "
            "или прямая ссылка на аудио внутри этого интервью."
        )

    telegram_file = await context.bot.get_file(
        file_id,
        read_timeout=TELEGRAM_FILE_TIMEOUT,
        write_timeout=TELEGRAM_FILE_TIMEOUT,
        connect_timeout=30,
        pool_timeout=30,
    )
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    await telegram_file.download_to_drive(
        tmp.name,
        read_timeout=TELEGRAM_FILE_TIMEOUT,
        write_timeout=TELEGRAM_FILE_TIMEOUT,
        connect_timeout=30,
        pool_timeout=30,
    )
    return tmp.name


def _download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Поддерживаются только http/https ссылки.")

    _reject_private_host(parsed.hostname)
    suffix = Path(parsed.path).suffix.lower()

    opener = urllib.request.build_opener(_SafeRedirectHandler)
    req = urllib.request.Request(url, headers={"User-Agent": "InterviewBot/1.0"})
    max_bytes = MAX_URL_DOWNLOAD_MB * 1024 * 1024
    downloaded = 0
    tmp_path = ""
    try:
        with opener.open(req, timeout=URL_DOWNLOAD_TIMEOUT) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if suffix not in AUDIO_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
                if content_type.startswith("video/"):
                    suffix = ".mp4"
                else:
                    suffix = ".mp3"
            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length and content_length > max_bytes:
                raise ValueError(f"Файл больше {MAX_URL_DOWNLOAD_MB} MB.")

            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp_path = tmp.name
            tmp.close()
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ValueError(f"Файл больше {MAX_URL_DOWNLOAD_MB} MB.")
                    f.write(chunk)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    if downloaded == 0:
        os.unlink(tmp_path)
        raise ValueError("Ссылка вернула пустой файл.")

    return tmp_path


def _path_looks_like_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _extract_audio_from_video(video_path: str) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "На сервере не установлен ffmpeg, поэтому я не могу извлечь аудио из видео. "
            "Нужно установить ffmpeg и повторить обработку."
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    audio_path = tmp.name
    tmp.close()
    try:
        command = [
            ffmpeg,
            "-y",
            "-i",
            video_path,
            "-map",
            "0:a:0",
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "4",
            audio_path,
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VIDEO_EXTRACT_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or "").strip().splitlines()
            detail = error[-1] if error else "ffmpeg failed"
            raise RuntimeError(f"Не удалось извлечь аудио из видео: {detail}")
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise RuntimeError("Не удалось извлечь аудио из видео: аудиодорожка пустая.")
        return audio_path
    except Exception:
        if os.path.exists(audio_path):
            os.unlink(audio_path)
        raise
    finally:
        if os.path.exists(video_path):
            os.unlink(video_path)


def _reject_private_host(hostname: str) -> None:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"Не удалось разрешить host ссылки: {hostname}") from e

    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError("Ссылки на локальные или приватные адреса запрещены.")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Редирект ведёт на неподдерживаемую ссылку.")
        _reject_private_host(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
