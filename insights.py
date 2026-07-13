"""LLM analysis for Aich custdev interviews."""
from __future__ import annotations

import json
import logging
import os
import re
import time

from google_docs import AICH_RESEARCH_CONTEXT

logger = logging.getLogger(__name__)

INSIGHTS_BASE_URL = os.getenv("INSIGHTS_BASE_URL", "https://apinet.cloud/v1")
INSIGHTS_MODEL = os.getenv("INSIGHTS_MODEL", "claude-opus-4-8-max")
INSIGHTS_API_KEY = os.getenv("INSIGHTS_API_KEY")
INSIGHTS_TIMEOUT = int(os.getenv("INSIGHTS_TIMEOUT", "180"))
INSIGHTS_MAX_TOKENS = int(os.getenv("INSIGHTS_MAX_TOKENS", "8000"))
REPORT_MODEL = os.getenv("REPORT_MODEL", os.getenv("INSIGHTS_REPORT_MODEL", INSIGHTS_MODEL))
REPORT_MAX_TOKENS = int(os.getenv("REPORT_MAX_TOKENS", "4500"))
DEDUPE_MODEL = os.getenv("DEDUPE_MODEL", REPORT_MODEL)
DEDUPE_MAX_TOKENS = int(os.getenv("DEDUPE_MAX_TOKENS", "3500"))
DEDUPE_AUTO_MERGE_THRESHOLD = float(os.getenv("DEDUPE_AUTO_MERGE_THRESHOLD", "0.92"))
CONTACT_STATUS_MODEL = os.getenv("CONTACT_STATUS_MODEL", INSIGHTS_MODEL)
CONTACT_STATUS_MAX_TOKENS = int(os.getenv("CONTACT_STATUS_MAX_TOKENS", "700"))

REQUIRED_TOP_LEVEL_KEYS = {
    "interview",
    "jtbd",
    "pains",
    "barriers",
    "willingness_to_pay",
    "product_opportunities",
    "interviewer_feedback",
    "risks",
    "next_research_questions",
}


def analyze_interview(answers: dict, transcript: str) -> dict:
    """Extract structured custdev insights as validated JSON."""
    client = _client()
    started_at = time.time()
    logger.info(
        "Generating structured interview JSON with %s - transcript=%d chars, timeout=%ds, max_tokens=%d",
        INSIGHTS_MODEL,
        len(transcript),
        INSIGHTS_TIMEOUT,
        INSIGHTS_MAX_TOKENS,
    )

    completion = client.chat.completions.create(
        model=INSIGHTS_MODEL,
        messages=[
            {"role": "system", "content": _json_system_prompt()},
            {"role": "user", "content": _json_user_prompt(answers, transcript)},
        ],
        temperature=0.1,
        max_tokens=INSIGHTS_MAX_TOKENS,
    )
    result = completion.choices[0].message.content
    payload = _parse_json_response(result)
    _validate_payload(payload)

    logger.info(
        "Structured interview JSON generated in %.1fs - jtbd=%d pains=%d barriers=%d opportunities=%d",
        time.time() - started_at,
        len(payload.get("jtbd", [])),
        len(payload.get("pains", [])),
        len(payload.get("barriers", [])),
        len(payload.get("product_opportunities", [])),
    )
    return payload


def analyze_contact_status(*, contact: dict, statuses: list[str], messages: list[dict]) -> dict:
    """Return a conservative, evidence-backed status recommendation for one conversation."""
    if not INSIGHTS_API_KEY:
        raise RuntimeError("INSIGHTS_API_KEY is not set.")
    transcript = "\n".join(
        f"[{item.get('sent_at', '')}] {'Менеджер' if item.get('outgoing') else 'Контакт'}: {item.get('text') or item.get('media') or '[без текста]'}"
        for item in messages
    )
    prompt = {
        "contact": {"name": contact.get("name"), "current_status": contact.get("status")},
        "allowed_statuses": statuses,
        "conversation": transcript,
        "task": "Определи актуальный статус только при явных доказательствах в переписке. Если данных недостаточно или текущий статус верен, верни recommend_update=false. Не делай предположений. Верни JSON: recommend_update (bool), suggested_status (строка из allowed_statuses или пустая), reason (кратко), evidence (до 3 коротких цитат).",
    }
    completion = _client().chat.completions.create(
        model=CONTACT_STATUS_MODEL,
        messages=[{"role": "system", "content": "Ты аккуратный CRM-аналитик. Отвечай только валидным JSON."}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        temperature=0,
        max_tokens=CONTACT_STATUS_MAX_TOKENS,
    )
    result = _parse_json_response(completion.choices[0].message.content)
    suggested = str(result.get("suggested_status") or "")
    if suggested not in statuses or suggested == contact.get("status"):
        result["recommend_update"] = False
    result["suggested_status"] = suggested
    result["reason"] = str(result.get("reason") or "")[:700]
    result["evidence"] = [str(item)[:500] for item in (result.get("evidence") or [])[:3]]
    return result


def generate_report(analysis: dict) -> str:
    """Generate a readable research report from structured JSON."""
    client = _client()
    started_at = time.time()
    logger.info("Generating readable report with %s from structured JSON", REPORT_MODEL)
    public_analysis = _public_report_payload(analysis)
    completion = client.chat.completions.create(
        model=REPORT_MODEL,
        messages=[
            {"role": "system", "content": _report_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(public_analysis, ensure_ascii=False, indent=2),
            },
        ],
        temperature=0.2,
        max_tokens=REPORT_MAX_TOKENS,
    )
    result = completion.choices[0].message.content
    if not result or not result.strip():
        raise RuntimeError("LLM returned an empty report response.")

    logger.info("Readable report generated in %.1fs - %d chars", time.time() - started_at, len(result))
    return result.strip()


def generate_interviewer_feedback_report(analysis: dict) -> str:
    """Build a private readable report for the interviewer from structured feedback JSON."""
    interview = analysis.get("interview") or {}
    feedback = analysis.get("interviewer_feedback") or {}
    respondent = _clean_report_value(interview.get("respondent_label")) or "респондент"
    hypothesis = _clean_report_value(interview.get("hypothesis"))
    overall_quality = _clean_report_value(feedback.get("overall_quality")) or "unknown"

    lines = [
        "### Фидбэк интервьюеру",
        f"Интервью: {respondent}",
        f"Общая оценка: {overall_quality}",
    ]
    if hypothesis and hypothesis != "нет данных":
        lines.append(f"Цель: {hypothesis}")

    _append_string_list(lines, "#### Что получилось хорошо", feedback.get("what_went_well"))
    _append_string_list(lines, "#### Что улучшить", feedback.get("what_to_improve"))
    _append_issue_list(
        lines,
        "#### Наводящие вопросы",
        feedback.get("leading_questions"),
        fields=[
            ("issue", "Проблема"),
            ("evidence_quote", "Цитата"),
            ("better_version", "Лучше спросить"),
        ],
    )
    _append_issue_list(
        lines,
        "#### Упущенные follow-up",
        feedback.get("missed_follow_ups"),
        fields=[
            ("signal", "Сигнал"),
            ("evidence_quote", "Цитата"),
            ("suggested_follow_up", "Что спросить"),
        ],
    )
    _append_string_list(
        lines,
        "#### Рекомендации на следующее интервью",
        feedback.get("next_interview_recommendations"),
    )
    return "\n".join(lines).strip()


def dedupe_notion_items(table_name: str, new_items: list[dict], existing_items: list[dict]) -> list[dict]:
    """Ask the LLM whether new Notion insight rows should be merged or created."""
    if not new_items:
        return []
    if not existing_items:
        return [
            {
                "temp_id": item.get("temp_id"),
                "decision": "create_new",
                "existing_id": "",
                "confidence": 1.0,
                "reason": "No existing records in this table.",
            }
            for item in new_items
        ]

    client = _client()
    completion = client.chat.completions.create(
        model=DEDUPE_MODEL,
        messages=[
            {"role": "system", "content": _dedupe_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "table": table_name,
                        "new_items": new_items,
                        "existing_items": existing_items,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0,
        max_tokens=DEDUPE_MAX_TOKENS,
    )
    payload = _parse_json_response(completion.choices[0].message.content)
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise RuntimeError("Dedupe LLM JSON must contain decisions list.")

    existing_ids = {str(item.get("id") or "") for item in existing_items}
    new_ids = {str(item.get("temp_id") or "") for item in new_items}
    normalized = []
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        temp_id = str(decision.get("temp_id") or "")
        if temp_id not in new_ids or temp_id in seen:
            continue
        seen.add(temp_id)
        action = str(decision.get("decision") or "").strip().lower()
        if action not in {"merge_existing", "create_new", "needs_review"}:
            action = "needs_review"
        existing_id = str(decision.get("existing_id") or decision.get("candidate_id") or "")
        if existing_id and existing_id not in existing_ids:
            existing_id = ""
        if action == "merge_existing" and not existing_id:
            action = "create_new"
        try:
            confidence = float(decision.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        if confidence < DEDUPE_AUTO_MERGE_THRESHOLD and action == "merge_existing":
            action = "needs_review"
        normalized.append(
            {
                "temp_id": temp_id,
                "decision": action,
                "existing_id": existing_id,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": _clean_report_value(decision.get("reason")),
            }
        )

    for item in new_items:
        temp_id = str(item.get("temp_id") or "")
        if temp_id and temp_id not in seen:
            normalized.append(
                {
                    "temp_id": temp_id,
                    "decision": "needs_review",
                    "existing_id": "",
                    "confidence": 0.0,
                    "reason": "LLM did not return a decision for this item.",
                }
            )
    return normalized


def _client():
    if not INSIGHTS_API_KEY:
        raise RuntimeError("INSIGHTS_API_KEY environment variable is not set.")

    from openai import OpenAI

    return OpenAI(
        base_url=INSIGHTS_BASE_URL,
        api_key=INSIGHTS_API_KEY,
        timeout=INSIGHTS_TIMEOUT,
    )


def _json_system_prompt() -> str:
    return (
        "Ты senior product researcher. Твоя задача - извлечь из custdev-интервью "
        "строго структурированные данные для Notion. Пиши по-русски. "
        "Используй только транскрипт и данные респондента. Не выдумывай факты. "
        "Каждый важный вывод подтверждай evidence_quote. Верни только валидный JSON "
        "без markdown, без пояснений и без ```."
    )


def _json_user_prompt(answers: dict, transcript: str) -> str:
    return f"""
Контекст продукта:
{AICH_RESEARCH_CONTEXT}

Главные исследовательские вопросы:
1. Нужен ли учителям AI, который анализирует урок и выдаёт фидбэк?
2. В чём конкретная польза или бесполезность такого фидбэка для учителя?
3. Готов ли респондент реально платить за такой продукт? За что именно и при каких условиях?
4. Какие барьеры внедрения: доверие к AI, время, приватность, привычки, администрация, деньги?
5. Какие другие боли в работе респондента могут быть сильнее, чем фидбэк по уроку?
6. Какие продуктовые возможности стоит проверить дальше?

Данные респондента:
Имя: {_value(answers, "name")}
Роль: {_value(answers, "role")}
Сегмент: {_value(answers, "segment")}
Предмет / направление: {_value(answers, "subject")}
Формат занятий: {_value(answers, "format")}
Опыт: {_value(answers, "experience")}
Цель / гипотеза интервью: {_value(answers, "hypothesis")}

Верни JSON строго такой формы:
{{
  "interview": {{
    "respondent_label": "string",
    "role": "string",
    "segment": "string",
    "subject": "string",
    "format": "string",
    "experience": "string",
    "hypothesis": "string",
    "summary": "5-8 предложений; обязательно встроить 2-4 короткие прямые цитаты респондента в кавычках",
    "aich_value_fit": "yes|maybe|no|unknown",
    "icp_fit": "high|medium|low|unknown",
    "confidence": "high|medium|low"
  }},
  "jtbd": [
    {{
      "job": "string",
      "context": "string",
      "current_solution": "string",
      "desired_outcome": "string",
      "pain_level": "high|medium|low",
      "aich_relevance": "high|medium|low",
      "evidence_quote": "string",
      "confidence": "high|medium|low"
    }}
  ],
  "pains": [
    {{
      "pain": "string",
      "cause": "string",
      "consequence": "string",
      "current_workaround": "string",
      "severity": "high|medium|low",
      "can_aich_help": "yes|partly|no",
      "evidence_quote": "string",
      "confidence": "high|medium|low"
    }}
  ],
  "barriers": [
    {{
      "barrier": "string",
      "category": "trust|privacy|habit|quality|money|workflow|tech|expertise|other",
      "severity": "high|medium|low",
      "how_to_reduce": "string",
      "evidence_quote": "string",
      "confidence": "high|medium|low"
    }}
  ],
  "willingness_to_pay": {{
    "respondent_label": "string",
    "segment": "string",
    "role": "string",
    "wtp_status": "yes|maybe|no|unknown|conditional",
    "wtp_strength": "strong|medium|weak",
    "who_pays": "teacher|school|parent|online_school|unknown|other",
    "would_pay_for": "string",
    "payment_conditions": "string",
    "payment_objections": "string",
    "price_mentioned": "string",
    "evidence_quote": "string",
    "researcher_comment": "string",
    "confidence": "high|medium|low"
  }},
  "product_opportunities": [
    {{
      "opportunity": "string",
      "problem_solved": "string",
      "target_segment": "string",
      "linked_jtbd": ["string"],
      "linked_pains": ["string"],
      "mvp_test": "string",
      "confidence": "high|medium|low"
    }}
  ],
  "interviewer_feedback": {{
    "overall_quality": "strong|ok|weak|unknown",
    "what_went_well": ["string"],
    "what_to_improve": ["string"],
    "leading_questions": [
      {{
        "issue": "string",
        "evidence_quote": "string",
        "better_version": "string"
      }}
    ],
    "missed_follow_ups": [
      {{
        "signal": "string",
        "evidence_quote": "string",
        "suggested_follow_up": "string"
      }}
    ],
    "next_interview_recommendations": ["string"]
  }},
  "risks": [
    {{
      "risk": "string",
      "severity": "high|medium|low",
      "evidence_quote": "string",
      "mitigation": "string"
    }}
  ],
  "next_research_questions": [
    {{
      "question": "string",
      "why_it_matters": "string",
      "priority": "high|medium|low"
    }}
  ]
}}

Правила:
- Если данных нет, пиши "нет данных", но сохраняй ключи.
- Не добавляй отдельную таблицу Quotes: цитаты должны быть внутри evidence_quote.
- Делай записи атомарными. Одна запись = один конкретный user need, pain, barrier или product opportunity.
- Не создавай broad umbrella-записи, в которых смешаны несколько смыслов через "и", "а также", "понять X и улучшить Y".
- Если в транскрипте есть составной вывод, разложи его на несколько отдельных записей.
- Пример для JTBD: "понять, прошёл ли урок успешно, и понял ли ученик материал, и что улучшить" нужно разделить минимум на:
  1) "Понять, понял ли ученик материал";
  2) "Оценить качество/структуру проведённого урока";
  3) "Определить, что изменить или улучшить на следующем уроке".
- Для JTBD не смешивай в одной записи: проверку понимания ученика, оценку качества урока, планирование next steps, отчёт родителям, трекинг прогресса, подбор материалов и генерацию домашки.
- Для Pains не смешивай симптом, причину и отдельную операционную рутину, если из них можно сделать разные записи.
- Для Product Opportunities не смешивай разные фичи в одну "AI feedback" запись: метрики вовлечённости, summary по ученику, планировщик программы, домашка, privacy layer и фидбэк по структуре урока - отдельные opportunities.
- Summary должен быть насыщен evidence: чаще цитируй респондента короткими фрагментами, а не только пересказывай.
- Для каждого JTBD/pain/barrier/risk/WTP вывода старайся выбирать максимально конкретную короткую цитату, а не общий пересказ.
- Для списков верни 3-7 самых важных элементов, не больше.
- В interviewer_feedback оцени качество проведения интервью, а не респондента. Отмечай наводящие вопросы, упущенные follow-up и конкретные улучшения для следующего интервью.
- JSON должен парситься стандартным json.loads.

Транскрипт:
{transcript}
""".strip()


def _report_system_prompt() -> str:
    return (
        "Ты product researcher. На входе тебе дают JSON с анализом интервью. "
        "Сделай читаемый отчёт на русском для команды Aich. Используй только данные из JSON. "
        "Не добавляй новых фактов. Сохрани цитаты и используй их часто: почти каждый важный вывод "
        "должен сопровождаться короткой прямой цитатой из evidence_quote, если она есть. "
        "Не включай фидбэк интервьюеру: "
        "этот отчёт предназначен для команды и должен содержать только исследовательские инсайты. "
        "Верни markdown-текст без JSON. "
        "Разделы оформляй markdown-заголовками уровня ###, подразделы - ####. "
        "Не используй markdown-таблицы: Telegra.ph плохо поддерживает таблицы. "
        "Если нужно сравнение, используй обычные списки."
    )


def _dedupe_system_prompt() -> str:
    return (
        "Ты помогаешь дедуплицировать research insights в Notion. "
        "На входе новые записи и существующие записи одной таблицы. "
        "Для каждой новой записи реши: merge_existing, create_new или needs_review. "
        "Главное правило: похожая тема НЕ является дублем. "
        "merge_existing выбирай только если совпадает один и тот же конкретный user need: "
        "тот же actor, тот же context, та же причина, тот же desired outcome и продуктовый смысл. "
        "Новая запись может быть частным примером существующей только если существующая запись уже явно покрывает этот частный случай "
        "и сама не является слишком широкой umbrella-записью. "
        "Не мержи атомарную запись в broad umbrella-record, если umbrella смешивает несколько смыслов. "
        "В таком случае выбирай create_new или needs_review и укажи, что существующую запись лучше расщепить. "
        "create_new выбирай, если записи отличаются хотя бы по одному важному измерению: "
        "контекст использования, причина боли, пользовательский сегмент, текущий workaround, desired outcome, барьер внедрения, "
        "или конкретная продуктовая фича. "
        "Для Product Opportunities не объединяй фичи только потому, что обе относятся к AI feedback; "
        "merge допустим только если это фактически одна и та же фича для одного сценария. "
        "Для Barriers не объединяй разные типы недоверия: privacy/control, AI accuracy, admin misuse, habit/time, money/free alternatives - это разные барьеры. "
        "Для JTBD не объединяй tracking progress, checking understanding, parent reporting, lesson reflection, homework generation и material selection, если outcome разный. "
        "Для Willingness to Pay почти всегда create_new: одна строка должна соответствовать одному респонденту/интервью. "
        "needs_review выбирай при любом сомнении или если совпадает только общая тема. "
        "Ставь confidence >= 0.92 только для почти точных дублей; 0.75-0.91 для похожих, но требующих review; <=0.74 для слабого сходства. "
        "Верни только валидный JSON без markdown: "
        "{\"decisions\":[{\"temp_id\":\"...\",\"decision\":\"merge_existing|create_new|needs_review\","
        "\"existing_id\":\"...\",\"confidence\":0.0,\"reason\":\"short reason\"}]}. "
        "existing_id должен быть id одной из existing_items или пустой строкой."
    )


def _parse_json_response(raw: str | None) -> dict:
    if not raw or not raw.strip():
        raise RuntimeError("LLM returned an empty JSON response.")

    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM returned invalid JSON: {e}") from e

    if not isinstance(payload, dict):
        raise RuntimeError("LLM JSON root must be an object.")
    return payload


def _validate_payload(payload: dict) -> None:
    missing = REQUIRED_TOP_LEVEL_KEYS - set(payload)
    if missing:
        raise RuntimeError(f"LLM JSON missing keys: {', '.join(sorted(missing))}")

    if not isinstance(payload.get("interview"), dict):
        raise RuntimeError("LLM JSON field interview must be an object.")
    if not isinstance(payload.get("willingness_to_pay"), dict):
        raise RuntimeError("LLM JSON field willingness_to_pay must be an object.")

    for key in ["jtbd", "pains", "barriers", "product_opportunities", "risks", "next_research_questions"]:
        if not isinstance(payload.get(key), list):
            raise RuntimeError(f"LLM JSON field {key} must be a list.")

    if not isinstance(payload.get("interviewer_feedback"), dict):
        raise RuntimeError("LLM JSON field interviewer_feedback must be an object.")


def _value(answers: dict, key: str) -> str:
    value = str(answers.get(key) or "").strip()
    return value or "-"


def _public_report_payload(analysis: dict) -> dict:
    payload = dict(analysis)
    payload.pop("interviewer_feedback", None)
    return payload


def _append_string_list(lines: list[str], title: str, values) -> None:
    clean_values = [_clean_report_value(value) for value in values or []]
    clean_values = [value for value in clean_values if value and value != "нет данных"]
    if not clean_values:
        return
    lines.extend(["", title])
    lines.extend(f"- {value}" for value in clean_values)


def _append_issue_list(lines: list[str], title: str, values, fields: list[tuple[str, str]]) -> None:
    clean_items = [item for item in values or [] if isinstance(item, dict)]
    if not clean_items:
        return
    lines.extend(["", title])
    for index, item in enumerate(clean_items, start=1):
        lines.append(f"- Эпизод {index}")
        for key, label in fields:
            value = _clean_report_value(item.get(key))
            if value and value != "нет данных":
                lines.append(f"- {label}: {value}")


def _clean_report_value(value) -> str:
    return str(value or "").strip()
