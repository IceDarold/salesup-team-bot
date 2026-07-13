# SalesUp Team Bot

Telegram bot for the SalesUp team's interviews, contacts, and candidates.

## Workflow

- Access is checked through the Notion `Team Members` database.
- `/new` shows the current user's `Interviews` with `Status = Sheduled` and `Owner = current member`.
- The bot reads `Goal` from the selected Notion interview. If it is empty, the bot asks for it and saves it back to Notion.
- The bot asks how many audio/video files the interview consists of: `1`, `2`, `3`, or a custom number.
- Audio/video parts are uploaded in order. For video, the bot extracts the audio track with `ffmpeg`, then transcribes parts sequentially with Deepgram and merges them into one transcript.
- The merged transcript is saved as a new Google Docs tab.
- The bot checks whether a transcript/report/feedback already exists and asks what to generate or regenerate.
- LLM analysis returns structured JSON for Notion and interviewer feedback.
- The readable report and interviewer feedback are published as separate Telegra.ph pages.
- Structured insights are saved into separate Notion databases.
- If a summary group is configured, the bot posts a short summary and report link there after successful report generation.

## Contacts

Use `/add_contact` to add a person to the `Contacts` database. The bot asks for
the name and contact, offers the current Notion segments and sources as inline
buttons, and lets the user add a new value when needed. New contacts are assigned
to the person who added them and start with the `Новый` status.

Every contact creation, status update, agent-sent Telegram message, and confirmed
manual contact action is written to the `Действия команды` Notion database when
`NOTION_TEAM_ACTIONS_DB_ID` is configured.

Every morning, the bot sends each team member a private reminder for their
contacts with a non-empty `Следующий шаг` and `Дата` equal to today. Configure
the schedule with `NEXT_STEP_REMINDER_TIME` (default `09:00`) and
`NEXT_STEP_REMINDER_TIMEZONE` (default `Asia/Nicosia`).

## SalesUp agent

Any ordinary text message is handled by a bounded LLM tool loop. It can answer
questions using personal data in a private chat and team data in a group: contact
statistics and contact search by name, status, segment, or source. The agent has
read-only access; creating or changing a contact remains an explicit bot flow.

It uses the official OpenAI API with `OPENAI_API_KEY` and `OPENAI_MODEL`.

## Personal Telegram accounts

`/telegram` lets a team member connect their personal Telegram account by QR code.
The encrypted session is stored separately for that Telegram user. The agent may
prepare an outgoing message, but sends it only after the account owner presses
the confirmation button.

After connecting, the bot asks for explicit consent to archive the complete
history of personal chats that can be matched unambiguously to that member's
Contacts records using the dedicated `Telegram` property (by `@username`, `t.me`
link, phone, or Telegram ID). Both
incoming and outgoing messages are stored in the runtime SQLite database and
appended to a dedicated tab per contact in `GOOGLE_CONVERSATION_DOC_ID`. The tab URL is stored
in the Contacts property `Переписка`, which the bot creates automatically when
needed. Groups, channels, bots, unmatched chats, and ambiguous matches are never
archived.

Telegram voice messages are downloaded during synchronization and transcribed with
OpenAI before being appended to the conversation tab. Set `OPENAI_API_KEY` and,
optionally, `OPENAI_TRANSCRIBE_MODEL` (default `gpt-4o-mini-transcribe`).

Use `/telegram_privacy` to view or grant consent, `/telegram_export <contact>`
to force a sync and get its Google Docs URL, `/telegram_delete <contact>` to
delete one archive, and `/telegram_delete_all` to delete all archives and disable
further synchronization. The periodic sync is configured by
`TELEGRAM_ARCHIVE_SYNC_SECONDS` (default `60`). A deleted contact stays excluded
from automatic re-import; `/telegram_export <contact>` explicitly enables it again.

When a synchronized chat contains new messages, the LLM compares the conversation
with the contact's current Notion status. A differing, evidence-backed status is
sent to the contact owner as a proposal; Notion changes only after they press
`Обновить статус`. The review uses up to `CONTACT_STATUS_MAX_MESSAGES` messages
(default `500`) and can use a separate `CONTACT_STATUS_MODEL`.

Use `/research` and then send a PDF or DOCX in a private chat to research potential
contacts. The bot extracts its text, validates candidates with public web search,
and returns cards with personalized outreach drafts. A message is sent only after
the owner presses `Отправить`.

Use `/company_research <URLs and free-text context>` for a source-grounded deep
company and vacancy analysis. It creates a durable background task rather than
blocking the bot: planning, iterative web research, an evidence ledger, strategy,
and an independent source check. The report covers history, founders, pains with
evidence, automation ideas, stakeholders, sales strategy, and outreach drafts;
it is saved as a separate tab in `GOOGLE_RESEARCH_DOC_ID`. Use
`/research_status <ID>`, `/research_cancel <ID>`, `/research_report <ID>`, and
`/research_refine <ID> <уточнение>` to manage it. The `salesup-research-worker`
systemd service processes queued jobs and resumes them after a bot restart.

## Scheduled personal Telegram messages

Use `/schedule_message` in a private chat. The bot asks for a recipient, message,
and date/time in `SCHEDULED_MESSAGES_TIMEZONE` (default `Asia/Tehran`). At the
scheduled time it asks for a final confirmation before sending through the linked
personal Telegram account. Use `/scheduled_messages` to inspect, edit, or cancel
future messages. The due-message check runs every 30 seconds by default.

Set `TELEGRAM_SESSION_ENCRYPTION_KEY` to a Fernet key. `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` are also required. For Telegram accounts protected by 2FA,
configure `TELEGRAM_2FA_WEB_BASE_URL` as a public HTTPS URL and proxy
`/telegram/2fa/*` to the local callback server on port `8094`.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with Telegram, Deepgram, LLM, Google Docs, and Notion credentials.

For `/stats`, add the `Contacts` database ID from the SalesUp workspace:

```bash
NOTION_CONTACTS_DB_ID=...
# Optional. Defaults to Asia/Nicosia.
STATS_TIMEZONE=Asia/Nicosia
```

`/stats` uses `Последнее касание` to determine activity for the current day. In a
private chat it shows the calling team member's statistics; in a group it shows
the combined team statistics.

Install `ffmpeg` on the machine that runs the bot if you want to process video uploads:

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

The Google service account must have edit access to the target Google Doc.

## Run

```bash
python -m bot.main
```

## Large Telegram Files

The public Telegram Bot API cannot download files over 20 MB. To handle large files sent directly to the bot, run a local Telegram Bot API server:

```bash
docker compose -f docker-compose.bot-api.yaml up -d
```

Then set:

```bash
TELEGRAM_BOT_API_BASE_URL=http://127.0.0.1:8081
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
```

Restart the bot after changing `.env`.

## Summary Group

Add the bot to a Telegram group. Then either:

- run `/set_summary_chat` in that group, or
- run `/summary_chat` in a private chat with the bot and choose a remembered group.

The selected group is persisted in `BOT_SETTINGS_PATH` and also cached by Telegram persistence.

## Notion Requirements

Required databases:

- `Team Members`
- `Interviews`
- `JTBD`
- `Pains`
- `Barriers`
- `Willingness to Pay`
- `Product Opportunities`

The `Interviews` database should include:

- `Name`
- `Status`
- `Owner`
- `Goal`
- `Transcript`
- `Summary`
- `Telegra.ph report`
- `Interviewer feedback`

## Notes

- The scheduled status is intentionally spelled `Sheduled`, matching the current Notion option.
- Runtime state is stored under `data/` and is ignored by git.
- Secrets are intentionally not stored in source files.
