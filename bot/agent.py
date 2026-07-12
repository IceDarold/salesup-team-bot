"""Bounded SalesUp tool-calling loop for ordinary Telegram messages."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from bot.agent_tools import AgentToolContext, ToolCatalog, execute_tool


@dataclass(frozen=True)
class AgentRunResult:
    text: str
    prepared_action: dict[str, Any] | None = None


class SalesUpAgent:
    def __init__(self, *, max_steps: int = 6) -> None:
        self.max_steps = max_steps
        self.api_key = os.getenv("AGENT_API_KEY") or os.getenv("INSIGHTS_API_KEY", "")
        self.base_url = os.getenv("AGENT_BASE_URL") or os.getenv("INSIGHTS_BASE_URL", "https://apinet.cloud/v1")
        self.model = os.getenv("AGENT_MODEL") or os.getenv("INSIGHTS_MODEL", "claude-opus-4-8-max")
        self.timeout = int(os.getenv("AGENT_TIMEOUT", "60"))
        self.catalog = ToolCatalog()

    async def run(self, *, text: str, member: dict, is_group: bool) -> AgentRunResult:
        if not self.api_key:
            return AgentRunResult("Агент пока не настроен: не задан AGENT_API_KEY или INSIGHTS_API_KEY.")

        from openai import OpenAI

        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
        tool_context = AgentToolContext(member=member, is_group=is_group)
        active_toolsets = {"core"}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(is_group, self.catalog.list_toolsets())},
            {
                "role": "user",
                "content": json.dumps(
                    {"message": text, "user": {"name": member.get("name", ""), "id": member.get("id", "")}},
                    ensure_ascii=False,
                ),
            },
        ]

        for _ in range(self.max_steps):
            tools = self.catalog.tools(active_toolsets)
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=self.model,
                messages=messages,
                tools=[tool.openai_schema() for tool in tools.values()],
                tool_choice="auto",
                temperature=0.2,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            if not tool_calls:
                return AgentRunResult((message.content or "Не смог подготовить ответ. Попробуй сформулировать иначе.").strip())

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
                tool = tools.get(call.function.name)
                result = (
                    execute_tool(tool, tool_context, arguments)
                    if tool is not None
                    else {"ok": False, "error": f"Недоступный инструмент: {call.function.name}"}
                )
                prepared_action = result.get("prepared_action")
                if prepared_action:
                    return AgentRunResult(_prepared_action_text(prepared_action), prepared_action=prepared_action)

                loaded_toolset = str(result.get("loaded_toolset") or "")
                if result.get("ok") and loaded_toolset in self.catalog.list_toolsets():
                    active_toolsets.add(loaded_toolset)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)},
                )

        return AgentRunResult("Не смог закончить запрос за один проход. Уточни, пожалуйста, что именно нужно найти.")


def _prepared_action_text(action: dict[str, Any]) -> str:
    payload = action.get("payload") or {}
    if action.get("kind") == "create_contact":
        return (
            "Добавить контакт в Notion?\n\n"
            f"Имя: {payload.get('name')}\n"
            f"Контакт: {payload.get('contact')}\n"
            f"Сегмент: {payload.get('segment')}\n"
            f"Источник: {payload.get('source')}"
        )
    if action.get("kind") == "update_contact_status":
        payload = action.get("payload") or {}
        return (
            "Изменить статус контакта в Notion?\n\n"
            f"Контакт: {action.get('contact_name')}\n"
            f"Текущий статус: {action.get('current_status')}\n"
            f"Новый статус: {payload.get('status')}"
        )
    return "Подтвердить подготовленное действие?"


def _system_prompt(is_group: bool, toolsets: dict[str, str]) -> str:
    scope = "командные данные" if is_group else "данные текущего пользователя"
    available = "; ".join(f"{name}: {description}" for name, description in toolsets.items())
    return f"""Ты помощник команды SalesUp. Отвечай по-русски, кратко и по делу.
У тебя есть доступ к {scope} в Notion через инструменты. Для чисел, статусов,
контактов и воронки всегда сначала вызывай инструмент, не придумывай данные.
В начале доступны только базовые инструменты. Для специализированной задачи сначала
вызови load_toolset. Доступные наборы: {available}.
Инструменты с записью не меняют Notion сами: они готовят действие, а код покажет
пользователю кнопки подтверждения. Никогда не утверждай, что запись создана до
подтверждения. Не раскрывай технические детали, токены или внутренние ID."""
