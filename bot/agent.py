"""SalesUp's small, tool-calling agent for ordinary Telegram messages."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from notion_store import find_contacts, get_contact_stats


class SalesUpAgent:
    """Run a bounded LLM/tool loop without giving the model write access."""

    def __init__(self, *, max_steps: int = 5) -> None:
        self.max_steps = max_steps
        self.api_key = os.getenv("AGENT_API_KEY") or os.getenv("INSIGHTS_API_KEY", "")
        self.base_url = os.getenv("AGENT_BASE_URL") or os.getenv("INSIGHTS_BASE_URL", "https://apinet.cloud/v1")
        self.model = os.getenv("AGENT_MODEL") or os.getenv("INSIGHTS_MODEL", "claude-opus-4-8-max")
        self.timeout = int(os.getenv("AGENT_TIMEOUT", "60"))

    async def run(self, *, text: str, member: dict, is_group: bool) -> str:
        if not self.api_key:
            return "Агент пока не настроен: не задан AGENT_API_KEY или INSIGHTS_API_KEY."

        from openai import OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(is_group)},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": text,
                        "user": {"name": member.get("name", ""), "id": member.get("id", "")},
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        for _ in range(self.max_steps):
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=self.model,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                return (message.content or "Не смог подготовить ответ. Попробуй сформулировать иначе.").strip()

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments},
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = await asyncio.to_thread(
                    _execute_tool,
                    call.function.name,
                    arguments,
                    member,
                    is_group,
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)},
                )

        return "Не смог закончить запрос за один проход. Уточни, пожалуйста, что именно нужно найти."


def _execute_tool(name: str, arguments: dict, member: dict, is_group: bool) -> dict:
    scope = "team" if is_group else str(arguments.get("scope") or "personal")
    owner_id = None if scope == "team" else member.get("id")
    if not owner_id and scope != "team":
        return {"ok": False, "error": "Не удалось определить участника команды."}

    if name == "get_contact_stats":
        result = get_contact_stats(owner_id)
        return {"ok": True, "scope": scope, "date": result["date"].isoformat(), **_stats_payload(result)}
    if name == "search_contacts":
        contacts = find_contacts(
            member_page_id=owner_id,
            query=str(arguments.get("query") or ""),
            status=str(arguments.get("status") or ""),
            segment=str(arguments.get("segment") or ""),
            source=str(arguments.get("source") or ""),
            limit=int(arguments.get("limit") or 10),
        )
        return {"ok": True, "scope": scope, "contacts": contacts, "count": len(contacts)}
    return {"ok": False, "error": f"Неизвестный инструмент: {name}"}


def _stats_payload(data: dict) -> dict:
    def funnel(item: dict) -> dict:
        return {
            "contacts": item["total"],
            "statuses": item["statuses"],
            "agreed": item["agreed"],
            "interviews": item["interviews"],
            "agreement_conversion": item["agreement_conversion"],
            "interview_conversion": item["interview_conversion"],
            "attendance": item["attendance"],
        }

    return {"today": funnel(data["today"]), "all": funnel(data["all"])}


def _system_prompt(is_group: bool) -> str:
    scope = "командные данные" if is_group else "данные текущего пользователя"
    return f"""Ты помощник команды SalesUp. Отвечай по-русски, кратко и по делу.
У тебя есть доступ к {scope} в Notion через инструменты. Для чисел, статусов,
контактов и воронки всегда сначала вызывай инструмент, не придумывай данные.
В этой версии у тебя нет прав на запись: если пользователь хочет добавить контакт,
объясни, что нужно запустить /add_contact. Не заявляй, что выполнил действие,
которое не выполнил. Не раскрывай технические детали, токены или внутренние ID."""


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_contact_stats",
            "description": "Получить статистику воронки контактов за сегодня и за всё время.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["personal", "team"]},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Найти контакты по имени, контакту, сегменту, источнику или статусу.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Свободный текст для поиска."},
                    "status": {"type": "string"},
                    "segment": {"type": "string"},
                    "source": {"type": "string"},
                    "scope": {"type": "string", "enum": ["personal", "team"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]
