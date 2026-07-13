"""Evidence-based sales recommendations and PDF prospect research."""
from __future__ import annotations

import json
import os
from typing import Callable


def _client():
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return OpenAI(api_key=key)


def _json(value: str) -> dict:
    value = (value or "").strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(value)


def _json_response(prompt: str, *, model: str, tools: list[dict] | None = None) -> dict:
    """Ask for machine-readable output while tolerating fenced JSON from a model."""
    kwargs: dict = {"model": model, "input": prompt}
    if tools:
        kwargs["tools"] = tools
    response = _client().responses.create(**kwargs)
    try:
        return _json(str(response.output_text or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Research model returned invalid structured data.") from exc


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


def deep_company_research(
    request: str,
    *,
    max_iterations: int,
    max_sources: int,
    progress: Callable[[str, int, str], None],
    cancelled: Callable[[], bool],
    trace: Callable[[str, str, str], None],
    refinement: str = "",
) -> tuple[dict, list[dict], list[dict]]:
    """Run a bounded plan/search/evidence/synthesis/critique research loop.

    The caller persists the returned ledger. This function intentionally has no
    database knowledge, making it safe to resume a queued job in another process.
    """
    model = os.getenv("COMPANY_RESEARCH_MODEL", "gpt-5.6-terra")
    scope = f"Запрос: {request}\nУточнение пользователя: {refinement or 'нет'}"
    progress("planning", 8, "Формирую план исследования и список проверяемых вопросов.")
    trace("model", "План исследования — вызов", "Модель формирует вопросы и поисковые запросы по исходным ссылкам и контексту.")
    plan = _json_response(
        """Ты планировщик B2B-исследований. Верни ТОЛЬКО JSON вида
{"questions":["..."],"queries":["..."],"success_criteria":["..."]}.
Составь до 10 точных поисковых запросов: компания, продукт, вакансия, основатели,
рынок, новости, клиенты, конкуренты, стек и доказуемые сигналы проблем. Не ищи пока.
""" + scope,
        model=model,
    )
    trace("model", "План исследования — результат", _preview_json(plan))
    queries = [str(q) for q in plan.get("queries", []) if str(q).strip()][:10]
    if not queries:
        queries = [request]
    all_sources: dict[str, dict] = {}
    all_claims: list[dict] = []
    gaps: list[str] = []
    for iteration in range(max(1, max_iterations)):
        if cancelled():
            raise InterruptedError("Research cancelled")
        batch = queries[:6]
        queries = queries[6:]
        if not batch:
            break
        progress("collecting", min(15 + iteration * 10, 65), f"Итерация {iteration + 1}: проверяю открытые источники.")
        trace("tool_call", f"web_search — вызов #{iteration + 1}", "Поисковые запросы:\n" + "\n".join(f"• {query}" for query in batch))
        ledger = _json_response(
            """Ты фактчекер B2B-исследования. Используй web search и верни ТОЛЬКО JSON:
{"sources":[{"url":"https://...","title":"","excerpt":"короткий факт или цитата","source_type":"official|news|job|social|review","published_at":"","relevance":0.0}],
"claims":[{"claim":"проверяемый факт или осторожная гипотеза","evidence":"короткий фрагмент","url":"https://...","confidence":"high|medium|hypothesis","category":"company|vacancy|founder|market|pain|stakeholder"}],
"next_queries":["..."],"gaps":["..."]}.
Каждый claim с confidence high/medium обязан иметь URL из sources. Не выдумывай URL,
контакты или цифры. Предпочитай первичные источники. Максимум 12 источников и 20 claims.

Контекст исследования:
""" + scope + "\nПлановые вопросы:\n" + json.dumps(plan.get("questions", []), ensure_ascii=False) + "\nПоисковые запросы:\n" + json.dumps(batch, ensure_ascii=False),
            model=model,
            tools=[{"type": "web_search"}],
        )
        trace("tool_result", f"web_search — результат #{iteration + 1}", _search_preview(ledger))
        for source in ledger.get("sources", []):
            if isinstance(source, dict) and str(source.get("url") or "").startswith(("http://", "https://")):
                all_sources[str(source["url"])] = source
        valid_urls = set(all_sources)
        for claim in ledger.get("claims", []):
            if not isinstance(claim, dict) or not str(claim.get("claim") or "").strip():
                continue
            confidence = str(claim.get("confidence") or "hypothesis")
            if confidence in {"high", "medium"} and str(claim.get("url") or "") not in valid_urls:
                claim["confidence"] = "hypothesis"
                claim["evidence"] = "Источник не прошёл проверку; требует подтверждения."
                claim["url"] = ""
            all_claims.append(claim)
        gaps = [str(g) for g in ledger.get("gaps", []) if str(g).strip()]
        if len(all_sources) >= max_sources:
            break
        queries.extend(str(q) for q in ledger.get("next_queries", []) if str(q).strip())
        queries = list(dict.fromkeys(queries))[:12]
        if not queries:
            break
    sources = list(all_sources.values())[:max_sources]
    source_catalog = _source_catalog(sources)
    source_urls = {str(item.get("url") or "") for item in sources}
    claims = [claim for claim in all_claims if str(claim.get("url") or "") in source_urls or str(claim.get("confidence") or "") == "hypothesis"]
    if cancelled():
        raise InterruptedError("Research cancelled")
    progress("analyzing", 75, f"Собрано {len(sources)} источников; строю стратегию на доказательной базе.")
    trace("model", "Синтез стратегии — вызов", f"Передаю модели {len(sources)} источников и {len(claims)} утверждений.")
    source_by_url = {item["url"]: item["id"] for item in source_catalog}
    report_claims = [{**claim, "source_id": source_by_url.get(str(claim.get("url") or ""), "")} for claim in claims]
    dossier = json.dumps({"sources": source_catalog, "claims": report_claims, "gaps": gaps}, ensure_ascii=False)[:110000]
    draft = _json_response(
        _report_prompt(scope, dossier), model=model,
    )
    trace("model", "Синтез стратегии — результат", _preview_json(draft))
    if cancelled():
        raise InterruptedError("Research cancelled")
    progress("reviewing", 90, "Проверяю отчёт: убираю неподтверждённые утверждения.")
    trace("model", "Критическая проверка — вызов", f"Проверяю отчёт по {len(source_urls)} разрешённым URL.")
    report = _json_response(
        _review_prompt(source_catalog, draft), model=model,
    )
    report = _normalize_report(report, source_catalog)
    trace("model", "Критическая проверка — результат", _preview_json(report))
    return report, sources, claims


def _source_catalog(sources: list[dict]) -> list[dict]:
    """Give sources stable, short citation identifiers for the model and renderer."""
    catalog = []
    for index, source in enumerate(sources, start=1):
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        catalog.append({
            "id": f"S{index}", "title": str(source.get("title") or url), "url": url,
            "excerpt": str(source.get("excerpt") or ""), "type": str(source.get("source_type") or ""),
            "published_at": str(source.get("published_at") or ""),
        })
    return catalog


def _report_prompt(scope: str, dossier: str) -> str:
    return """Ты senior B2B researcher и sales strategist. Работай ТОЛЬКО с реестром ниже.
Верни ТОЛЬКО валидный JSON без Markdown и без URL вне `source_ids`.

Схема:
{"executive_summary":"", "sales_brief":{"signals":[""],"buyer":"","value_proposition":"","cta":""},
"company_facts":[{"fact":"","evidence":"","source_ids":["S1"],"confidence":"high|medium|hypothesis"}],
"vacancy_signals":[{"signal":"","why_it_matters":"","source_ids":["S1"],"confidence":"high|medium|hypothesis"}],
"pains":[{"pain":"","evidence":"","automation":"","priority":"P1|P2|P3","source_ids":["S1"],"confidence":"high|medium|hypothesis"}],
"stakeholders":[{"role":"","motivation":"","cta":"","priority":1}],
"touchpoints":[{"day":"1","channel":"","action":""}],
"messages":[{"label":"Короткое","text":""}],
"risks":[""],"discovery_questions":[""],"gaps":[""]}.

Правила: максимум 5 company_facts, 5 vacancy_signals, 7 pains, 5 stakeholders, 8 touchpoints,
3 messages, 8 вопросов. Первые блоки должны быть пригодны для sales-brief на две страницы.
Не повторяй тезисы. Любое утверждение без source_ids — только confidence=hypothesis.

""" + scope + "\n\nРеестр доказательств:\n" + dossier


def _review_prompt(source_catalog: list[dict], draft: dict) -> str:
    allowed = ", ".join(item["id"] for item in source_catalog)
    return """Ты независимый фактчекер. Верни ТОЛЬКО исправленный JSON в той же схеме.
Разрешены только source_ids: """ + allowed + """.
Для high/medium оставляй только факты с подходящим source_id. Иначе меняй confidence на hypothesis
и явно пиши в evidence, что это нужно проверить. Удали повторы, неподтверждённые цифры и личные
контакты. Не добавляй поля и не превращай JSON в Markdown.

Черновик:\n""" + json.dumps(draft, ensure_ascii=False)


def _normalize_report(report: dict, source_catalog: list[dict]) -> dict:
    allowed = {item["id"] for item in source_catalog}
    report = report if isinstance(report, dict) else {}
    result = {
        "executive_summary": str(report.get("executive_summary") or ""),
        "sales_brief": report.get("sales_brief") if isinstance(report.get("sales_brief"), dict) else {},
        "company_facts": _normalize_evidence_items(report.get("company_facts"), allowed)[:5],
        "vacancy_signals": _normalize_evidence_items(report.get("vacancy_signals"), allowed)[:5],
        "pains": _normalize_evidence_items(report.get("pains"), allowed)[:7],
        "stakeholders": _dict_list(report.get("stakeholders"))[:5],
        "touchpoints": _dict_list(report.get("touchpoints"))[:8],
        "messages": _dict_list(report.get("messages"))[:3],
        "risks": _text_list(report.get("risks"))[:8],
        "discovery_questions": _text_list(report.get("discovery_questions"))[:8],
        "gaps": _text_list(report.get("gaps"))[:8],
        "sources": source_catalog,
    }
    brief = result["sales_brief"]
    result["sales_brief"] = {
        "signals": _text_list(brief.get("signals"))[:3], "buyer": str(brief.get("buyer") or ""),
        "value_proposition": str(brief.get("value_proposition") or ""), "cta": str(brief.get("cta") or ""),
    }
    return result


def _normalize_evidence_items(value, allowed: set[str]) -> list[dict]:
    items = _dict_list(value)
    normalized = []
    for item in items:
        ids = [str(source_id) for source_id in item.get("source_ids", []) if str(source_id) in allowed]
        confidence = str(item.get("confidence") or "hypothesis").lower()
        if confidence not in {"high", "medium", "hypothesis"}:
            confidence = "hypothesis"
        if confidence in {"high", "medium"} and not ids:
            confidence = "hypothesis"
            item["evidence"] = "Требует подтверждения источником."
        normalized.append({**item, "source_ids": ids, "confidence": confidence})
    return normalized


def _dict_list(value) -> list[dict]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _text_list(value) -> list[str]:
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _preview(value: str, limit: int = 1800) -> str:
    value = " ".join((value or "").split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _preview_json(value: dict) -> str:
    return _preview(json.dumps(value, ensure_ascii=False, indent=2))


def _search_preview(ledger: dict) -> str:
    sources = [item for item in ledger.get("sources", []) if isinstance(item, dict)][:5]
    if not sources:
        return _preview_json({"gaps": ledger.get("gaps", []), "next_queries": ledger.get("next_queries", [])})
    lines = [f"Найдено источников: {len(ledger.get('sources', []))}; claims: {len(ledger.get('claims', []))}."]
    for source in sources:
        lines.append(f"• {source.get('title') or 'Без названия'}\n{source.get('url') or 'URL не указан'}\n{_preview(str(source.get('excerpt') or ''), 320)}")
    if ledger.get("gaps"):
        lines.append("Пробелы: " + "; ".join(str(item) for item in ledger["gaps"][:3]))
    return _preview("\n".join(lines))
