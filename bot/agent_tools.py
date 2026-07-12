"""Model-visible SalesUp tools with code-owned safety policies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from notion_store import create_contact, find_contacts, get_contact_stats


ToolHandler = Callable[["AgentToolContext", dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolPolicy:
    toolset: str
    risk: str = "read"
    confirmation: str = "never"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    progress_label: str
    execute: ToolHandler
    policy: ToolPolicy

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class AgentToolContext:
    member: dict
    is_group: bool

    @property
    def scope(self) -> str:
        return "team" if self.is_group else "personal"

    @property
    def owner_id(self) -> str | None:
        return None if self.is_group else self.member.get("id")


class ToolCatalog:
    """Expose only the toolsets that the agent has explicitly loaded."""

    def __init__(self) -> None:
        self._tools = {tool.name: tool for tool in _TOOLS}

    def tools(self, active_toolsets: set[str]) -> dict[str, ToolSpec]:
        active = set(active_toolsets) | {"core"}
        return {name: tool for name, tool in self._tools.items() if tool.policy.toolset in active}

    def list_toolsets(self) -> dict[str, str]:
        return {
            "contacts": "поиск контактов и подготовка добавления нового контакта с подтверждением",
        }


def execute_tool(tool: ToolSpec, context: AgentToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = tool.execute(context, arguments)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(result, dict):
        return {"ok": False, "error": "Инструмент вернул некорректный результат."}
    return result


def execute_prepared_action(action: dict[str, Any]) -> str:
    """Run a previously confirmed write action. Never call this from the model loop."""
    if action.get("kind") != "create_contact":
        raise ValueError("Неизвестное подготовленное действие.")
    return create_contact(**(action.get("payload") or {}))


def prepared_action_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()


def _stats(context: AgentToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
    result = get_contact_stats(context.owner_id)
    return {"ok": True, "scope": context.scope, "date": result["date"].isoformat(), **_stats_payload(result)}


def _search_contacts(context: AgentToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    contacts = find_contacts(
        member_page_id=context.owner_id,
        query=str(arguments.get("query") or ""),
        status=str(arguments.get("status") or ""),
        segment=str(arguments.get("segment") or ""),
        source=str(arguments.get("source") or ""),
        limit=int(arguments.get("limit") or 10),
    )
    return {"ok": True, "scope": context.scope, "contacts": contacts, "count": len(contacts)}


def _prepare_create_contact(context: AgentToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    if context.is_group:
        return {"ok": False, "error": "Добавлять контакты через агента можно только в личном чате."}
    required = {key: str(arguments.get(key) or "").strip() for key in ("name", "contact", "segment", "source")}
    missing = [key for key, value in required.items() if not value]
    if missing:
        return {"ok": False, "error": f"Не хватает данных: {', '.join(missing)}."}
    action = {
        "kind": "create_contact",
        "payload": {"owner_id": context.member["id"], **required},
        "expires_at": prepared_action_expiry(),
    }
    return {"ok": True, "terminal": True, "prepared_action": action}


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


_EMPTY_OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}
_CONTACT_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Свободный текст для поиска."},
        "status": {"type": "string"},
        "segment": {"type": "string"},
        "source": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "additionalProperties": False,
}
_CREATE_CONTACT_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "contact": {"type": "string", "description": "Телефон, @username, email или ссылка."},
        "segment": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["name", "contact", "segment", "source"],
    "additionalProperties": False,
}
_TOOLS = (
    ToolSpec(
        name="get_contact_stats",
        description="Получить статистику воронки контактов за сегодня и за всё время.",
        parameters=_EMPTY_OBJECT,
        progress_label="Считаю статистику…",
        execute=_stats,
        policy=ToolPolicy(toolset="core"),
    ),
    ToolSpec(
        name="load_toolset",
        description="Подключить специализированный набор инструментов. Доступен набор contacts.",
        parameters={
            "type": "object",
            "properties": {"toolset": {"type": "string", "enum": ["contacts"]}},
            "required": ["toolset"],
            "additionalProperties": False,
        },
        progress_label="Подключаю инструменты…",
        execute=lambda _context, arguments: {"ok": True, "loaded_toolset": str(arguments.get("toolset") or "")},
        policy=ToolPolicy(toolset="core"),
    ),
    ToolSpec(
        name="search_contacts",
        description="Найти контакты по имени, контакту, сегменту, источнику или статусу.",
        parameters=_CONTACT_SEARCH_PARAMETERS,
        progress_label="Ищу контакты…",
        execute=_search_contacts,
        policy=ToolPolicy(toolset="contacts"),
    ),
    ToolSpec(
        name="prepare_create_contact",
        description="Подготовить добавление нового контакта. Запись появится в Notion только после подтверждения пользователя.",
        parameters=_CREATE_CONTACT_PARAMETERS,
        progress_label="Готовлю контакт…",
        execute=_prepare_create_contact,
        policy=ToolPolicy(toolset="contacts", risk="write", confirmation="required"),
    ),
)
