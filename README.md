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

## SalesUp agent

Any ordinary text message is handled by a bounded LLM tool loop. It can answer
questions using personal data in a private chat and team data in a group: contact
statistics and contact search by name, status, segment, or source. The agent has
read-only access; creating or changing a contact remains an explicit bot flow.

It uses `AGENT_API_KEY`, `AGENT_BASE_URL`, and `AGENT_MODEL` when set, otherwise
falls back to the existing `INSIGHTS_*` settings.

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
