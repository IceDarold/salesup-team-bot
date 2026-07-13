"""Evidence-based sales recommendations and PDF prospect research."""
from __future__ import annotations

import json
import os


def _client():
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return OpenAI(api_key=key)


def _json(value: str) -> dict:
    value = (value or "").strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(value)


def research_document(path: str) -> list[dict]:
    if path.lower().endswith(".docx"):
        from docx import Document
        text = "\n".join(paragraph.text for paragraph in Document(path).paragraphs)[:120000]
    else:
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)[:120000]
    if not text.strip():
        raise ValueError("В документе не удалось извлечь текст; для скана нужен OCR.")
    prompt = """Изучи материал, выполни web search по потенциальным B2B-контактам и верни только JSON: {\"candidates\":[{\"name\":\"\",\"telegram\":\"\",\"company\":\"\",\"role\":\"\",\"why\":\"\",\"sources\":[\"\"],\"message\":\"\"}]}. Не выдумывай контакты, Telegram или источники. Максимум 10 кандидатов. Сообщение — персонализированный короткий первый outreach на русском."""
    response = _client().responses.create(
        model=os.getenv("SALES_RESEARCH_MODEL", "gpt-5.6-terra"),
        tools=[{"type": "web_search"}],
        input=f"{prompt}\n\nPDF:\n{text}",
    )
    payload = _json(response.output_text)
    return [item for item in payload.get("candidates", []) if isinstance(item, dict)][:10]


def recommend_next_action(contact: dict, messages: list[dict]) -> dict:
    transcript = "\n".join(f"{'Менеджер' if m.get('outgoing') else 'Контакт'}: {m.get('text') or m.get('media')}" for m in messages[-500:])
    prompt = {"contact": contact, "conversation": transcript, "task": "Верни JSON {next_action, due_date_iso_or_empty, rationale, draft_message, evidence:[...]}. Не предлагай отправку, если данных недостаточно. Следующее действие и черновик должны быть конкретными, на русском, с опорой на evidence."}
    result = _client().chat.completions.create(model=os.getenv("SALES_ACTION_MODEL", "gpt-5.6-terra"), messages=[{"role":"system","content":"Ты осторожный sales-ассистент. Только JSON."},{"role":"user","content":json.dumps(prompt, ensure_ascii=False)}], temperature=0.2)
    return _json(result.choices[0].message.content)


def research_company_brief(request: str) -> str:
    """Run a multi-angle, source-grounded company and vacancy research task."""
    prompt = """Ты senior B2B research и sales strategist. Пользователь дал ссылки и свободный контекст о компании/вакансии.
Проведи максимально глубокий web research: официальный сайт, вакансия, продукт, рынок, новости, основатель и команда, история, клиенты, конкуренты, публичные интервью, отзывы и технологический стек. Не утверждай, что нашёл всё в интернете; явно отмечай пробелы.

Верни подробный отчёт на русском в Markdown со следующими разделами:
1. Executive summary.
2. Компания и история: факты, основатель, продукт, рынок, динамика.
3. Разбор вакансии: обязанности, KPI, скрытые сигналы, что это говорит о задачах бизнеса.
4. Подтверждённые боли и возможности автоматизации: каждая боль = наблюдение, доказательство/цитата, ссылка-источник, степень уверенности, конкретная идея автоматизации.
5. ICP и карта стейкхолдеров: кому писать, роль каждого, порядок контактов.
6. Стратегия продажи: гипотеза ценности, персонализация, каналы, последовательность касаний на 30 дней, возражения и ответы.
7. Три варианта первого сообщения: короткое, экспертное, value-first.
8. Риски и что нужно проверить в следующем разговоре.
9. Список источников с прямыми URL.

Каждый факт о компании или человеке подтверждай ссылкой рядом с утверждением. Не выдумывай личные контакты, цифры, клиентов или источники. Используй несколько поисковых запросов и сначала первичные источники."""
    response = _client().responses.create(
        model=os.getenv("COMPANY_RESEARCH_MODEL", "gpt-5.6-terra"),
        tools=[{"type": "web_search"}],
        input=f"{prompt}\n\nЗапрос пользователя:\n{request}",
    )
    return str(response.output_text or "").strip()
