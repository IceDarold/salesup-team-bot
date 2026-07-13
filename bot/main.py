"""Telegram bot entry point for interview transcription."""
import asyncio
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)


def load_dotenv() -> None:
    """Load simple KEY=VALUE pairs from .env for local development."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

from bot.access import admin_required, init_access_db, member_required  # noqa: E402
from bot.telegram_user import TelegramUserService  # noqa: E402
from bot.telegram_web import TelegramTwoFactorServer  # noqa: E402
from notion_store import find_contacts, get_contacts_with_next_step_on, list_team_members  # noqa: E402
from notion_store import get_contact_status_options  # noqa: E402
from insights import analyze_contact_status  # noqa: E402
from bot.handlers import (  # noqa: E402
    ARTIFACT_DECISION,
    ARCHIVE_DECISION,
    CUSTOM_PARTS_COUNT,
    CONTACT_CUSTOM_SEGMENT,
    CONTACT_CUSTOM_SOURCE,
    CONTACT_NAME,
    CONTACT_SEGMENT,
    CONTACT_SOURCE,
    CONTACT_VALUE,
    DEDUPE_MODE_DECISION,
    DEDUPE_REVIEW_DECISION,
    EXPERIENCE,
    FORMAT,
    HYPOTHESIS,
    INTERVIEW_AUDIO,
    INTERVIEW_LANGUAGE,
    DUPLICATE_DECISION,
    NAME,
    PARTS_COUNT,
    ROLE,
    SEGMENT,
    SUBJECT,
    add_member_cmd,
    add_contact,
    agent_action_callback,
    agent_message,
    archive_decision,
    archive_decision_text,
    artifact_decision,
    artifact_decision_text,
    cancel_interview,
    cancel_contact,
    contact_custom_segment,
    contact_custom_source,
    company_research_command,
    contact_status_suggestion_callback,
    contact_name,
    contact_segment,
    contact_source,
    contact_value,
    dedupe_mode_decision,
    dedupe_mode_text,
    dedupe_review_decision,
    dedupe_review_text,
    help_cmd,
    info,
    custom_parts_count,
    interview_audio,
    duplicate_decision_text,
    existing_transcript_decision,
    interview_experience,
    interview_format,
    interview_hypothesis,
    interview_language,
    interview_language_text,
    interview_name,
    interview_parts_count,
    interview_parts_count_text,
    interview_role,
    interview_selected,
    interviews_page,
    interview_segment,
    interview_subject,
    choose_interview_text,
    members_cmd,
    new_interview,
    new_transcript,
    remove_member_cmd,
    remember_bot_chat_member,
    remember_group,
    research_callback,
    research_command,
    research_document_handler,
    set_summary_chat,
    start,
    stats,
    summary_chat_status,
    telegram_account,
    telegram_archive_callback,
    telegram_delete,
    telegram_delete_all,
    telegram_export,
    telegram_privacy,
    choose_summary_chat,
)


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")
telegram_user_service: TelegramUserService | None = None
telegram_two_factor_server: TelegramTwoFactorServer | None = None

TELEGRAM_READ_TIMEOUT = int(os.getenv("TELEGRAM_READ_TIMEOUT", "600"))
TELEGRAM_WRITE_TIMEOUT = int(os.getenv("TELEGRAM_WRITE_TIMEOUT", "600"))
TELEGRAM_CONNECT_TIMEOUT = int(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_POOL_TIMEOUT = int(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
CONTACT_STATUS_MAX_MESSAGES = int(os.getenv("CONTACT_STATUS_MAX_MESSAGES", "500"))
NEXT_STEP_REMINDER_TIME = os.getenv("NEXT_STEP_REMINDER_TIME", "09:00")
NEXT_STEP_REMINDER_TIMEZONE = os.getenv("NEXT_STEP_REMINDER_TIMEZONE", "Asia/Nicosia")
PERSISTENCE_PATH = Path(os.getenv("BOT_PERSISTENCE_PATH", "data/bot-state.pickle"))

COMMANDS = [
    BotCommand("start", "Открыть бота"),
    BotCommand("new", "Новое интервью"),
    BotCommand("add_contact", "Добавить контакт"),
    BotCommand("transcript", "Только транскрипт в новый Google Doc"),
    BotCommand("stats", "Статистика по контактам"),
    BotCommand("telegram", "Подключить личный Telegram"),
    BotCommand("telegram_privacy", "Настройки архива переписки"),
    BotCommand("telegram_export", "Обновить архив контакта"),
    BotCommand("telegram_delete", "Удалить архив контакта"),
    BotCommand("telegram_delete_all", "Удалить все архивы"),
    BotCommand("research", "Пришли PDF с ресёрчем"),
    BotCommand("company_research", "Глубоко изучить компанию"),
    BotCommand("help", "Помощь"),
    BotCommand("info", "Статус"),
    BotCommand("cancel", "Отменить интервью"),
    BotCommand("add_member", "Добавить участника"),
    BotCommand("members", "Список участников"),
    BotCommand("remove_member", "Удалить участника"),
    BotCommand("set_summary_chat", "Настроить группу summary"),
    BotCommand("summary_chat", "Текущая группа summary"),
]


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        sys.exit(1)

    init_access_db()
    PERSISTENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    builder = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(setup_commands)
        .post_shutdown(shutdown)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .get_updates_read_timeout(60)
    )
    bot_api_base_url = os.getenv("TELEGRAM_BOT_API_BASE_URL")
    if bot_api_base_url:
        base_url = bot_api_base_url.rstrip("/")
        check_local_bot_api(token, base_url)
        builder = (
            builder.base_url(f"{base_url}/bot")
            .base_file_url(f"{base_url}/file/bot")
            .local_mode(True)
        )
        logger.info("Using local Telegram Bot API server: %s", base_url)

    app = builder.build()
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("new", member_required(new_interview)),
                CommandHandler("transcript", member_required(new_transcript)),
            ],
            states={
                NAME: [
                    CallbackQueryHandler(member_required(interview_selected), pattern=r"^interview:"),
                    CallbackQueryHandler(member_required(interviews_page), pattern=r"^interviews_page:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(choose_interview_text)),
                ],
                ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_role))],
                SEGMENT: [
                    CallbackQueryHandler(member_required(interview_segment), pattern=r"^seg:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_segment)),
                ],
                SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_subject))],
                FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_format))],
                EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_experience))],
                HYPOTHESIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_hypothesis))],
                INTERVIEW_LANGUAGE: [
                    CallbackQueryHandler(member_required(interview_language), pattern=r"^lang:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_language_text)),
                ],
                PARTS_COUNT: [
                    CallbackQueryHandler(member_required(interview_parts_count), pattern=r"^parts:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_parts_count_text)),
                ],
                CUSTOM_PARTS_COUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(custom_parts_count)),
                ],
                DUPLICATE_DECISION: [
                    CallbackQueryHandler(
                        member_required(existing_transcript_decision),
                        pattern=r"^existing_transcript:",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        member_required(duplicate_decision_text),
                    ),
                ],
                ARTIFACT_DECISION: [
                    CallbackQueryHandler(
                        member_required(artifact_decision),
                        pattern=r"^artifacts:",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        member_required(artifact_decision_text),
                    ),
                ],
                DEDUPE_MODE_DECISION: [
                    CallbackQueryHandler(member_required(dedupe_mode_decision), pattern=r"^dedupe_mode:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(dedupe_mode_text)),
                ],
                DEDUPE_REVIEW_DECISION: [
                    CallbackQueryHandler(member_required(dedupe_review_decision), pattern=r"^dedupe_review:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(dedupe_review_text)),
                ],
                ARCHIVE_DECISION: [
                    CallbackQueryHandler(member_required(archive_decision), pattern=r"^archive:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(archive_decision_text)),
                ],
                INTERVIEW_AUDIO: [
                    MessageHandler(
                        filters.VOICE
                        | filters.AUDIO
                        | filters.VIDEO
                        | filters.VIDEO_NOTE
                        | filters.Document.ALL,
                        member_required(interview_audio),
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://"),
                        member_required(interview_audio),
                    ),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(interview_audio)),
                ],
            },
            fallbacks=[
                CommandHandler("new", member_required(new_interview)),
                CommandHandler("transcript", member_required(new_transcript)),
                CommandHandler("cancel", member_required(cancel_interview)),
            ],
            name="interview_flow",
            persistent=True,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add_contact", member_required(add_contact))],
            states={
                CONTACT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(contact_name))],
                CONTACT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(contact_value))],
                CONTACT_SEGMENT: [CallbackQueryHandler(member_required(contact_segment), pattern=r"^contact_segment:")],
                CONTACT_CUSTOM_SEGMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(contact_custom_segment))
                ],
                CONTACT_SOURCE: [CallbackQueryHandler(member_required(contact_source), pattern=r"^contact_source:")],
                CONTACT_CUSTOM_SOURCE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(contact_custom_source))
                ],
            },
            fallbacks=[CommandHandler("cancel", member_required(cancel_contact))],
            name="contact_flow",
            persistent=True,
        )
    )

    app.add_handler(CommandHandler("start", member_required(start)))
    app.add_handler(CommandHandler(["help", "about"], member_required(help_cmd)))
    app.add_handler(CommandHandler("info", member_required(info)))
    app.add_handler(CommandHandler("stats", member_required(stats)))
    app.add_handler(CommandHandler("telegram", member_required(telegram_account)))
    app.add_handler(CommandHandler("telegram_privacy", member_required(telegram_privacy)))
    app.add_handler(CommandHandler("telegram_export", member_required(telegram_export)))
    app.add_handler(CommandHandler("telegram_delete", member_required(telegram_delete)))
    app.add_handler(CommandHandler("telegram_delete_all", member_required(telegram_delete_all)))
    app.add_handler(CommandHandler("research", member_required(research_command)))
    app.add_handler(CommandHandler("company_research", member_required(company_research_command)))
    app.add_handler(CommandHandler("add_member", admin_required(add_member_cmd)))
    app.add_handler(CommandHandler("members", admin_required(members_cmd)))
    app.add_handler(CommandHandler("remove_member", admin_required(remove_member_cmd)))
    app.add_handler(CommandHandler("set_summary_chat", admin_required(set_summary_chat)))
    app.add_handler(CommandHandler("summary_chat", admin_required(summary_chat_status)))
    app.add_handler(CallbackQueryHandler(admin_required(choose_summary_chat), pattern=r"^summary_chat:"))
    app.add_handler(CallbackQueryHandler(member_required(agent_action_callback), pattern=r"^agent_action:"))
    app.add_handler(CallbackQueryHandler(member_required(telegram_archive_callback), pattern=r"^archive:"))
    app.add_handler(CallbackQueryHandler(member_required(contact_status_suggestion_callback), pattern=r"^status_suggestion:"))
    app.add_handler(CallbackQueryHandler(member_required(research_callback), pattern=r"^research:"))
    app.add_handler(ChatMemberHandler(remember_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, remember_group), group=10)
    app.add_handler(MessageHandler(filters.Document.PDF | filters.Document.FileExtension("docx"), member_required(research_document_handler)), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(agent_message)))
    schedule_next_step_reminders(app)
    schedule_conversation_archives(app)

    logger.info("Interview bot starting")
    app.run_polling()


def check_local_bot_api(token: str, base_url: str) -> None:
    try:
        with urllib.request.urlopen(f"{base_url}/bot{token}/getMe", timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"unexpected status {response.status}")
    except (OSError, urllib.error.URLError, RuntimeError) as e:
        logger.error("Local Telegram Bot API server is not available at %s: %s", base_url, e)
        sys.exit(1)


async def setup_commands(app: Application) -> None:
    global telegram_user_service, telegram_two_factor_server
    try:
        await app.bot.set_my_commands(COMMANDS)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning("Failed to set commands menu: %s", e)
    service = telegram_user_service or TelegramUserService()
    telegram_user_service = service
    app._telegram_user_service = service
    server = TelegramTwoFactorServer(service, app.bot)
    telegram_two_factor_server = server
    try:
        await server.start()
    except Exception as e:
        logger.exception("Failed to start Telegram 2FA server: %s", e)


async def shutdown(app: Application) -> None:
    global telegram_user_service, telegram_two_factor_server
    server = telegram_two_factor_server
    if server:
        await server.stop()
    service = telegram_user_service
    if service:
        await service.close()
    telegram_two_factor_server = None
    telegram_user_service = None


def schedule_next_step_reminders(app: Application) -> None:
    try:
        hour, minute = (int(part) for part in NEXT_STEP_REMINDER_TIME.split(":", 1))
        scheduled_time = time(hour=hour, minute=minute, tzinfo=ZoneInfo(NEXT_STEP_REMINDER_TIMEZONE))
    except (ValueError, TypeError):
        logger.warning("Invalid NEXT_STEP_REMINDER_TIME=%r; using 09:00", NEXT_STEP_REMINDER_TIME)
        scheduled_time = time(hour=9, minute=0, tzinfo=ZoneInfo("Asia/Nicosia"))
    app.job_queue.run_daily(next_step_reminders_job, time=scheduled_time, name="next_step_reminders")


def schedule_conversation_archives(app: Application) -> None:
    interval = max(60, int(os.getenv("TELEGRAM_ARCHIVE_SYNC_SECONDS", "60")))
    app.job_queue.run_repeating(conversation_archives_job, interval=interval, first=15, name="conversation_archives")


async def conversation_archives_job(context) -> None:
    service = getattr(context.application, "_telegram_user_service", None)
    if service is None:
        return
    for member in await asyncio.to_thread(list_team_members):
        try:
            user_id = int(member.get("telegram_user_id") or "")
        except (TypeError, ValueError):
            continue
        if not service.archive_status(user_id)["enabled"]:
            continue
        try:
            contacts = await asyncio.to_thread(find_contacts, member_page_id=member.get("id"), limit=1000)
            sync_result = await service.sync_archive(user_id, contacts, member.get("name", ""))
            statuses = await asyncio.to_thread(get_contact_status_options)
            for contact in sync_result.get("changed_contacts", []):
                messages = service.contact_messages(user_id, contact["id"], limit=CONTACT_STATUS_MAX_MESSAGES)
                review = await asyncio.to_thread(analyze_contact_status, contact=contact, statuses=statuses, messages=messages)
                if not review.get("recommend_update"):
                    if review.get("next_action") or review.get("draft_message"):
                        await context.bot.send_message(
                            user_id,
                            f"Рекомендация по контакту: {contact.get('name')}\n\nСледующее действие: {review.get('next_action') or '—'}\nДата: {review.get('due_date') or '—'}\n\nЧерновик:\n{review.get('draft_message') or '—'}",
                        )
                    continue
                token = service.create_status_suggestion(user_id, contact["id"], contact.get("status", ""), review["suggested_status"], review.get("reason", ""), review.get("evidence", []))
                evidence = "\n".join(f"• {item}" for item in review.get("evidence", [])) or "—"
                await context.bot.send_message(
                    user_id,
                    f"Возможное обновление статуса\n\nКонтакт: {contact.get('name')}\nСейчас: {contact.get('status') or '—'}\nПредлагаю: {review['suggested_status']}\n\n{review.get('reason') or ''}\n\nСледующее действие: {review.get('next_action') or '—'}\nДата: {review.get('due_date') or '—'}\n\nЧерновик:\n{review.get('draft_message') or '—'}\n\nДоказательства:\n{evidence}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Обновить статус", callback_data=f"status_suggestion:apply:{token}"), InlineKeyboardButton("Оставить текущий", callback_data=f"status_suggestion:keep:{token}")]]),
                )
        except Exception:
            logger.exception("Unable to synchronize Telegram archive for user %s", user_id)


async def next_step_reminders_job(context) -> None:
    try:
        target_date = datetime.now(ZoneInfo(NEXT_STEP_REMINDER_TIMEZONE)).date()
        contacts = await asyncio.to_thread(get_contacts_with_next_step_on, target_date)
        members = await asyncio.to_thread(list_team_members)
    except Exception:
        logger.exception("Unable to load next-step reminders")
        return

    members_by_id = {member.get("id"): member for member in members}
    reminders: dict[int, list[dict]] = {}
    for contact in contacts:
        for owner_id in contact["owner_ids"]:
            member = members_by_id.get(owner_id) or {}
            try:
                user_id = int(member.get("telegram_user_id") or "")
            except (TypeError, ValueError):
                continue
            reminders.setdefault(user_id, []).append(contact)

    for user_id, items in reminders.items():
        lines = [f"☀️ <b>Следующие шаги на сегодня — {target_date.strftime('%d.%m')}</b>", ""]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. <b>{item['name']}</b> — {item['next_step']}")
        try:
            await context.bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
        except Exception:
            logger.exception("Unable to send next-step reminder to Telegram user %s", user_id)


if __name__ == "__main__":
    main()
