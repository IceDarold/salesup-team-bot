"""Telegram bot entry point for interview transcription."""
import asyncio
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
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
from notion_store import find_contacts, get_contacts_with_next_step_on, list_team_members, list_followups_for_contacts, stop_contact_followups, update_contact_research_state, update_followup  # noqa: E402
from followups import FollowupSuggestionStore, generate_adaptive_followup, generate_followup_sequence  # noqa: E402
from notion_store import get_contact_status_options  # noqa: E402
from insights import analyze_contact_status  # noqa: E402
from research_jobs import ResearchJobStore  # noqa: E402
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
    SCHEDULE_DATE,
    SCHEDULE_EDIT_VALUE,
    SCHEDULE_HOUR,
    SCHEDULE_MINUTE,
    SCHEDULE_RECIPIENT,
    SCHEDULE_TEXT,
    FOLLOWUP_EDIT_TEXT,
    RESEARCH_LINK_VALUE,
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
    research_cancel_command,
    research_document_handler,
    research_refine_command,
    research_proposal_callback,
    research_contact_callback,
    research_link_entry,
    research_link_value,
    research_report_command,
    research_status_command,
    outreach_stats_command,
    schedule_message_command,
    scheduled_callback,
    scheduled_cancel_flow,
    scheduled_edit_entry,
    scheduled_edit_value,
    scheduled_date_callback,
    scheduled_hour_callback,
    scheduled_minute_callback,
    scheduled_messages_command,
    scheduled_recipient,
    scheduled_text,
    followup_callback,
    followup_edit_entry,
    followup_edit_value,
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
    BotCommand("research_status", "Статус глубокого research"),
    BotCommand("research_cancel", "Отменить глубокий research"),
    BotCommand("research_report", "Открыть отчёт research"),
    BotCommand("research_refine", "Уточнить завершённый research"),
    BotCommand("outreach_stats", "Аналитика outreach"),
    BotCommand("schedule_message", "Запланировать личное сообщение"),
    BotCommand("scheduled_messages", "Запланированные сообщения"),
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
            entry_points=[CallbackQueryHandler(member_required(research_link_entry), pattern=r"^research_proposal:attach:")],
            states={RESEARCH_LINK_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(research_link_value))]},
            fallbacks=[CommandHandler("cancel", member_required(scheduled_cancel_flow))],
            name="research_link_flow", persistent=True,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(member_required(followup_edit_entry), pattern=r"^followup:edit:[^:]+:[123]$")],
            states={FOLLOWUP_EDIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(followup_edit_value))]},
            fallbacks=[CommandHandler("cancel", member_required(scheduled_cancel_flow))],
            name="followup_edit_flow",
            persistent=True,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("schedule_message", member_required(schedule_message_command)),
                CallbackQueryHandler(member_required(scheduled_edit_entry), pattern=r"^scheduled:edit:(time|recipient|text):"),
            ],
            states={
                SCHEDULE_RECIPIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(scheduled_recipient))],
                SCHEDULE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(scheduled_text))],
                SCHEDULE_DATE: [CallbackQueryHandler(member_required(scheduled_date_callback), pattern=r"^scheduled:date:")],
                SCHEDULE_HOUR: [CallbackQueryHandler(member_required(scheduled_hour_callback), pattern=r"^scheduled:hour:")],
                SCHEDULE_MINUTE: [CallbackQueryHandler(member_required(scheduled_minute_callback), pattern=r"^scheduled:minute:")],
                SCHEDULE_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(scheduled_edit_value))],
            },
            fallbacks=[CommandHandler("cancel", member_required(scheduled_cancel_flow))],
            name="scheduled_message_flow",
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
    app.add_handler(CommandHandler("research_status", member_required(research_status_command)))
    app.add_handler(CommandHandler("research_cancel", member_required(research_cancel_command)))
    app.add_handler(CommandHandler("research_report", member_required(research_report_command)))
    app.add_handler(CommandHandler("research_refine", member_required(research_refine_command)))
    app.add_handler(CommandHandler("outreach_stats", member_required(outreach_stats_command)))
    app.add_handler(CommandHandler("scheduled_messages", member_required(scheduled_messages_command)))
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
    app.add_handler(CallbackQueryHandler(member_required(research_contact_callback), pattern=r"^research_contact:"))
    app.add_handler(CallbackQueryHandler(member_required(research_proposal_callback), pattern=r"^research_proposal:"))
    app.add_handler(CallbackQueryHandler(member_required(scheduled_callback), pattern=r"^scheduled:"))
    app.add_handler(CallbackQueryHandler(member_required(followup_callback), pattern=r"^followup:"))
    app.add_handler(ChatMemberHandler(remember_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, remember_group), group=10)
    app.add_handler(MessageHandler(filters.Document.PDF | filters.Document.FileExtension("docx"), member_required(research_document_handler)), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(agent_message)))
    schedule_next_step_reminders(app)
    schedule_conversation_archives(app)
    schedule_scheduled_messages(app)
    schedule_followup_suggestions(app)
    schedule_research_suggestions(app)

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


def schedule_scheduled_messages(app: Application) -> None:
    interval = max(15, int(os.getenv("SCHEDULED_MESSAGE_CHECK_SECONDS", "30")))
    app.job_queue.run_repeating(scheduled_messages_job, interval=interval, first=10, name="scheduled_messages")


def schedule_followup_suggestions(app: Application) -> None:
    interval = max(900, int(os.getenv("FOLLOWUP_SCAN_SECONDS", "3600")))
    app.job_queue.run_repeating(followup_suggestions_job, interval=interval, first=45, name="followup_suggestions")


def schedule_research_suggestions(app: Application) -> None:
    interval = max(900, int(os.getenv("OUTREACH_RESEARCH_SCAN_SECONDS", "3600")))
    app.job_queue.run_repeating(research_suggestions_job, interval=interval, first=60, name="research_suggestions")


async def research_suggestions_job(context) -> None:
    """Offer company research for new, untouched Contacts without a research URL."""
    statuses = {item.strip() for item in os.getenv("OUTREACH_RESEARCH_CONTACT_STATUSES", "Новый").split(",") if item.strip()}
    store = ResearchJobStore()
    for member in await asyncio.to_thread(list_team_members):
        try:
            user_id = int(member.get("telegram_user_id") or "")
        except (ValueError, TypeError):
            continue
        try:
            contacts = await asyncio.to_thread(find_contacts, member_page_id=member.get("id"), limit=1000)
            for contact in contacts:
                contact_id = str(contact.get("id") or "")
                research_status = contact.get("research_status") or "Not started"
                revisit_raw = str(contact.get("research_revisit_at") or "")
                revisit_due = False
                if research_status == "Later" and revisit_raw:
                    try:
                        revisit_due = datetime.fromisoformat(revisit_raw.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
                    except ValueError:
                        revisit_due = False
                eligible = research_status == "Not started" or revisit_due
                if not contact_id or contact.get("research_url") or not eligible or store.has_active_for_contact(contact_id):
                    continue
                if research_status == "Later" and revisit_due:
                    await asyncio.to_thread(update_contact_research_state, contact_id, "Not started")
                if store.has_suggestion(contact_id, user_id) and research_status != "Later":
                    continue
                if not await asyncio.to_thread(store.create_suggestion, contact_id, user_id):
                    # A previous decision exists; reset only when the scheduled
                    # "later" date has arrived.
                    if not revisit_due:
                        continue
                text = (
                    f"<b>Контакт без research: {html.escape(str(contact.get('name') or 'без имени'))}</b>\n\n"
                    "Для качественного outreach сначала стоит провести research компании: проверить ICP, триггер, процесс, "
                    "гипотезы и подходящего ЛПР. Запустить?"
                )
                await asyncio.to_thread(update_contact_research_state, contact_id, "Proposed")
                buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Провести research", callback_data=f"research_proposal:start:{contact_id}")], [InlineKeyboardButton("📎 Прикрепить готовый research", callback_data=f"research_proposal:attach:{contact_id}")], [InlineKeyboardButton("Вернуться позже", callback_data=f"research_proposal:later:{contact_id}")], [InlineKeyboardButton("Не делать research", callback_data=f"research_proposal:skip:{contact_id}")]])
                await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=buttons)
        except Exception:
            logger.exception("Unable to propose company research for member %s", member.get("id"))


async def scheduled_messages_job(context) -> None:
    service = getattr(context.application, "_telegram_user_service", None)
    if service is None:
        return
    due = await asyncio.to_thread(service.claim_due_scheduled_messages)
    for item in due:
        token = str(item["token"])
        try:
            item = await _refresh_due_followup(item, service)
            if item.get("notion_followup_id"):
                await asyncio.to_thread(update_followup, followup_id=str(item["notion_followup_id"]), status="Ждёт подтверждения отправки", text=str(item.get("text") or ""))
            when = datetime.fromisoformat(str(item["scheduled_at"])).astimezone(ZoneInfo(os.getenv("SCHEDULED_MESSAGES_TIMEZONE", "Europe/Moscow")))
            text = (
                f"<b>Время отправки пришло</b>\n\n"
                f"Контакт: <code>{html.escape(str(item['recipient']))}</code>\n"
                f"Запланировано: <b>{when.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
                f"Сообщение:\n{html.escape(str(item['text'])[:3000])}\n\n"
                "Отправить через ваш личный Telegram?"
            )
            await context.bot.send_message(
                chat_id=int(item["telegram_user_id"]), text=text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отправить", callback_data=f"scheduled:send:{token}"), InlineKeyboardButton("Не отправлять", callback_data=f"scheduled:decline:{token}")], [InlineKeyboardButton("Изменить", callback_data=f"scheduled:open:{token}")]]),
            )
        except Exception:
            logger.exception("Unable to request confirmation for scheduled message %s", token)


async def _refresh_due_followup(item: dict, service) -> dict:
    """Use fresh history immediately before confirmation; never block a human review on a refresh failure."""
    contact_id = str(item.get("contact_id") or "")
    if not contact_id or not item.get("notion_followup_id"):
        return item
    try:
        member = next((x for x in await asyncio.to_thread(list_team_members) if str(x.get("telegram_user_id") or "") == str(item["telegram_user_id"])), None)
        if not member:
            return item
        contacts = await asyncio.to_thread(find_contacts, member_page_id=member["id"], limit=1000)
        contact = next((x for x in contacts if str(x.get("id")) == contact_id), None)
        if not contact or contact.get("status") not in {"Новый", "Написали", "No response"} or not contact.get("research_url"):
            return item
        messages = service.contact_messages(int(item["telegram_user_id"]), contact_id, limit=80)
        if any(not bool(message.get("outgoing")) for message in messages[-5:]):
            return item
        research_job = await asyncio.to_thread(ResearchJobStore().latest_for_contact, contact_id)
        research = json.loads(str(research_job.get("report") or "{}")) if research_job else {}
        text = await asyncio.to_thread(generate_adaptive_followup, contact, messages, research, "Добавить новый полезный угол или вопрос", str(item["text"]))
        updated = service.update_scheduled_message(str(item["token"]), int(item["telegram_user_id"]), text=text)
        return updated or item
    except Exception:
        logger.info("Could not refresh follow-up %s; using approved draft", item.get("token"), exc_info=True)
        return item


def _followup_recipient(contact: dict) -> str:
    candidate = str(contact.get("telegram") or contact.get("contact") or "").strip()
    if candidate.startswith("@") or candidate.startswith("https://t.me/") or candidate.startswith("http://t.me/") or candidate.lstrip("-").isdigit():
        return candidate
    return ""


def _new_enough_contact(contact: dict) -> bool:
    days = max(1, int(os.getenv("FOLLOWUP_CONTACT_LOOKBACK_DAYS", "14")))
    raw = str(contact.get("created_at") or "")
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return created >= datetime.now(created.tzinfo) - timedelta(days=days)


async def followup_suggestions_job(context) -> None:
    """Propose a three-touch sequence for fresh Contacts that have no active follow-ups."""
    service = getattr(context.application, "_telegram_user_service", None)
    if service is None or not os.getenv("NOTION_FOLLOW_UPS_DB_ID"):
        return
    store = FollowupSuggestionStore()
    eligible = {item.strip() for item in os.getenv("FOLLOWUP_CONTACT_STATUSES", "Новый,Написали,No response").split(",") if item.strip()}
    for member in await asyncio.to_thread(list_team_members):
        try:
            user_id = int(member.get("telegram_user_id") or "")
        except (TypeError, ValueError):
            continue
        if not service.status(user_id).get("connected"):
            continue
        try:
            contacts = await asyncio.to_thread(find_contacts, member_page_id=member["id"], limit=1000)
            existing = await asyncio.to_thread(list_followups_for_contacts, [str(item["id"]) for item in contacts])
            for contact in contacts:
                contact_id = str(contact["id"])
                active = [item for item in existing.get(contact_id, []) if item.get("status") in {"Черновик", "На согласовании", "Запланировано", "Ждёт подтверждения отправки", "Отправляется"}]
                if contact.get("status") not in eligible:
                    if active:
                        await asyncio.to_thread(stop_contact_followups, contact_id, "Статус контакта больше не требует follow-up")
                        await asyncio.to_thread(service.cancel_scheduled_messages_for_contact, user_id, contact_id)
                    continue
                # Only contacts without an existing chain are eligible. This avoids
                # adding three more touches to a manually planned sequence.
                # Outreach follows research: without a canonical research link there
                # is no evidence base for a safe, personalized sequence.
                if active or not _new_enough_contact(contact) or not contact.get("research_url"):
                    continue
                recipient = _followup_recipient(contact)
                if not recipient or store.has_suggestion(contact_id, user_id):
                    continue
                history = service.contact_messages(user_id, contact_id, limit=60)
                # Do not restart an outreach sequence after a reply, and do not
                # label a first contact as a follow-up. Research worker proposes
                # the first message; this flow starts only after it was sent.
                if any(not bool(item.get("outgoing")) for item in history):
                    continue
                if not any(bool(item.get("outgoing")) for item in history):
                    continue
                try:
                    research_job = await asyncio.to_thread(ResearchJobStore().latest_for_contact, contact_id)
                    research = json.loads(str(research_job.get("report") or "{}")) if research_job else {}
                    if not research and contact.get("research_url"):
                        from google_docs import read_research_document
                        research = {"external_research": await asyncio.to_thread(read_research_document, str(contact["research_url"]))}
                    payload = await asyncio.to_thread(generate_followup_sequence, contact, history, research)
                except Exception as exc:
                    logger.exception("Unable to generate follow-ups for contact %s", contact_id)
                    # Do not silently leave the owner waiting when the provider
                    # itself is unavailable or the key has expired.
                    if any(marker in str(exc).lower() for marker in ("insufficient permissions", "authentication", "api key")):
                        try:
                            await context.bot.send_message(chat_id=user_id, text="Не удалось подготовить follow-up: у OPENAI_API_KEY нет нужного доступа. Обновите ключ на сервере — бот попробует снова.")
                        except Exception:
                            logger.exception("Unable to notify about follow-up generation failure")
                    continue
                payload.update({"recipient": recipient, "contact_name": contact.get("name") or ""})
                suggestion = store.create(contact_id, str(member["id"]), user_id, payload)
                if not suggestion:
                    continue
                from bot.handlers import _followup_proposal_buttons, _followup_proposal_text
                await context.bot.send_message(chat_id=user_id, text=_followup_proposal_text(payload["contact_name"], payload), parse_mode="HTML", reply_markup=_followup_proposal_buttons(suggestion["token"]))
        except Exception:
            logger.exception("Unable to prepare follow-up suggestions for member %s", member.get("id"))


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
            # A real inbound reply ends the automated sequence before any LLM
            # classification: never send another follow-up over a reply.
            for contact in sync_result.get("inbound_contacts", []):
                contact_id = str(contact.get("id") or "")
                if not contact_id:
                    continue
                stopped_ids = await asyncio.to_thread(service.cancel_scheduled_messages_for_contact, user_id, contact_id)
                await asyncio.to_thread(stop_contact_followups, contact_id, "Получен ответ контакта")
                await asyncio.to_thread(ResearchJobStore().record_outreach_event, contact_id, "inbound_reply", {"cancelled_followups": len(stopped_ids)})
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
