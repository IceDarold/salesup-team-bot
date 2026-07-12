"""Telegram bot entry point for interview transcription."""
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from telegram import BotCommand, MenuButtonCommands
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
    agent_message,
    archive_decision,
    archive_decision_text,
    artifact_decision,
    artifact_decision_text,
    cancel_interview,
    cancel_contact,
    contact_custom_segment,
    contact_custom_source,
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
    set_summary_chat,
    start,
    stats,
    summary_chat_status,
    choose_summary_chat,
)


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

TELEGRAM_READ_TIMEOUT = int(os.getenv("TELEGRAM_READ_TIMEOUT", "600"))
TELEGRAM_WRITE_TIMEOUT = int(os.getenv("TELEGRAM_WRITE_TIMEOUT", "600"))
TELEGRAM_CONNECT_TIMEOUT = int(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_POOL_TIMEOUT = int(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
PERSISTENCE_PATH = Path(os.getenv("BOT_PERSISTENCE_PATH", "data/bot-state.pickle"))

COMMANDS = [
    BotCommand("start", "Открыть бота"),
    BotCommand("new", "Новое интервью"),
    BotCommand("add_contact", "Добавить контакт"),
    BotCommand("transcript", "Только транскрипт в новый Google Doc"),
    BotCommand("stats", "Статистика по контактам"),
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
    app.add_handler(CommandHandler("add_member", admin_required(add_member_cmd)))
    app.add_handler(CommandHandler("members", admin_required(members_cmd)))
    app.add_handler(CommandHandler("remove_member", admin_required(remove_member_cmd)))
    app.add_handler(CommandHandler("set_summary_chat", admin_required(set_summary_chat)))
    app.add_handler(CommandHandler("summary_chat", admin_required(summary_chat_status)))
    app.add_handler(CallbackQueryHandler(admin_required(choose_summary_chat), pattern=r"^summary_chat:"))
    app.add_handler(ChatMemberHandler(remember_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, remember_group), group=10)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, member_required(agent_message)))

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
    try:
        await app.bot.set_my_commands(COMMANDS)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning("Failed to set commands menu: %s", e)


if __name__ == "__main__":
    main()
