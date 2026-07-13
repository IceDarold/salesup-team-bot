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


def research_pdf(path: str) -> list[dict]:
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)[:120000]
    if not text.strip():
        raise ValueError("В PDF не удалось извлечь текст; нужен текстовый PDF или OCR.")
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
