"""AI follow-up suggestions and their durable approval state."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = os.getenv("SCHEDULED_MESSAGES_TIMEZONE", "Europe/Moscow")


class FollowupSuggestionStore:
    def __init__(self) -> None:
        path = Path(os.getenv("FOLLOWUP_SUGGESTIONS_DB_PATH", "data/followup-suggestions.sqlite3"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS followup_suggestions (
                token TEXT PRIMARY KEY, contact_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, decided_at TEXT)""")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_pending ON followup_suggestions(contact_id, status)")

    def create(self, contact_id: str, owner_id: str, telegram_user_id: int, payload: dict) -> dict | None:
        token = secrets.token_urlsafe(8)
        now = datetime.now().isoformat()
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO followup_suggestions VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)",
                                (token, contact_id, owner_id, telegram_user_id, json.dumps(payload, ensure_ascii=False), now))
        except sqlite3.IntegrityError:
            return None
        return self.get(token, telegram_user_id)

    def get(self, token: str, telegram_user_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM followup_suggestions WHERE token=? AND telegram_user_id=? AND status='pending'", (token, telegram_user_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def has_pending(self, contact_id: str, telegram_user_id: int) -> bool:
        with self.lock:
            return bool(self.db.execute(
                "SELECT 1 FROM followup_suggestions WHERE contact_id=? AND telegram_user_id=? AND status='pending'",
                (contact_id, telegram_user_id),
            ).fetchone())

    def has_suggestion(self, contact_id: str, telegram_user_id: int) -> bool:
        """A rejection is deliberate: do not spam the owner with the same proposal."""
        with self.lock:
            return bool(self.db.execute(
                "SELECT 1 FROM followup_suggestions WHERE contact_id=? AND telegram_user_id=?",
                (contact_id, telegram_user_id),
            ).fetchone())

    def update_payload(self, token: str, telegram_user_id: int, payload: dict) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE followup_suggestions SET payload=? WHERE token=? AND telegram_user_id=? AND status='pending'", (json.dumps(payload, ensure_ascii=False), token, telegram_user_id))

    def resolve(self, token: str, telegram_user_id: int, status: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE followup_suggestions SET status=?, decided_at=? WHERE token=? AND telegram_user_id=? AND status='pending'", (status, datetime.now().isoformat(), token, telegram_user_id))


def generate_followup_sequence(contact: dict, messages: list[dict], research: dict | None = None) -> dict:
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    transcript = "\n".join(f"{'Менеджер' if item.get('outgoing') else 'Контакт'}: {item.get('text') or item.get('media') or ''}" for item in messages[-60:])
    prompt = {
        "contact": {key: contact.get(key) for key in ("name", "contact", "telegram", "status", "segments", "source")},
        "conversation": transcript,
        "research": research or {},
        "task": "Сгенерируй 3 коротких персонализированных follow-up сообщения на русском. Используй только подтверждённые факты из research; гипотезы формулируй осторожно. Не выдумывай факты. Каждое следующее касание должно добавлять новый факт, вопрос или пользу: уточнение, новый angle, маленькая ценность, корректный breakup. Верни только JSON {messages:[{text,reason}]}. Если исходного касания нет, первое сообщение должно быть аккуратным началом диалога.",
    }
    response = OpenAI(api_key=key).chat.completions.create(
        model=os.getenv("FOLLOWUP_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-terra")),
        messages=[{"role": "system", "content": "Ты аккуратный sales-ассистент. Только JSON."}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
    )
    raw = (response.choices[0].message.content or "").strip().removeprefix("```json").removesuffix("```").strip()
    payload = json.loads(raw)
    messages = [item for item in payload.get("messages", []) if isinstance(item, dict) and str(item.get("text") or "").strip()][:3]
    if len(messages) != 3:
        raise RuntimeError("Follow-up model did not return three messages.")
    zone = ZoneInfo(TIMEZONE)
    base = datetime.now(zone).replace(hour=int(os.getenv("FOLLOWUP_DEFAULT_HOUR", "11")), minute=0, second=0, microsecond=0)
    if base <= datetime.now(zone):
        base += timedelta(days=1)
    for index, item in enumerate(messages, start=1):
        item["sequence"] = index
        item["scheduled_at"] = (base + timedelta(days=2 * index)).isoformat()
    return {"messages": messages}


def generate_adaptive_followup(contact: dict, messages: list[dict], research: dict, direction: str, previous_text: str) -> str:
    """Refresh one touch immediately before human confirmation, using live history."""
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    transcript = "\n".join(f"{'Менеджер' if item.get('outgoing') else 'Контакт'}: {item.get('text') or item.get('media') or ''}" for item in messages[-80:])
    prompt = {"contact": contact, "research": research, "conversation": transcript, "planned_direction": direction,
              "previous_draft": previous_text,
              "task": "Перепиши только следующий follow-up на русском с учётом всей истории. Не отправляй, если был ответ контакта. Добавь новый факт/вопрос/пользу, не повторяй предыдущие касания, используй только подтверждённые факты. Верни JSON {text:'', should_send:true|false, reason:''}."}
    response = OpenAI(api_key=key).chat.completions.create(model=os.getenv("FOLLOWUP_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-terra")), messages=[{"role":"system","content":"Ты осторожный outreach-ассистент. Только JSON."},{"role":"user","content":json.dumps(prompt, ensure_ascii=False)}])
    raw = (response.choices[0].message.content or "").strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(raw)
    if not result.get("should_send") or not str(result.get("text") or "").strip():
        raise RuntimeError("Adaptive follow-up decided not to send.")
    return str(result["text"]).strip()
