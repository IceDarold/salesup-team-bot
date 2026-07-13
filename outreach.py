"""Grounded qualification, strategy and message criticism for cold outreach."""
from __future__ import annotations

import json
import os

from sales_agent import _json_response


MODEL = os.getenv("OUTREACH_MODEL", os.getenv("COMPANY_RESEARCH_MODEL", "gpt-5.6-terra"))


def quick_qualify(request: str) -> dict:
    """Fast, no-search gate. It may reject only when evidence is unambiguous."""
    return _json_response(
        '''Ты квалифицируешь B2B-лид до глубокого исследования. Ничего не ищи и не выдумывай.
Верни только JSON: {"decision":"research|defer|reject","reason":"","icp_score":0,
"trigger_score":0,"feasibility_score":0,"known_facts":[""],"missing":[""]}.
reject — только если лид явно не подходит или нет компании/повода; defer — если данных мало;
research — если есть разумный повод проверить. Оценки 0..5.\n\nВводные:\n''' + request,
        model=MODEL,
    )


def build_outreach_plan(report: dict, sources: list[dict], claims: list[dict], qualification: dict) -> dict:
    """Select a single evidence-backed strategy and audit its first message."""
    ledger = [{"claim": x.get("claim", ""), "evidence": x.get("evidence", ""), "url": x.get("url", ""),
               "confidence": x.get("confidence", "hypothesis"), "category": x.get("category", "")} for x in claims]
    prompt = {
        "qualification": qualification, "research": report, "evidence_ledger": ledger,
        "task": '''Собери outreach plan. Используй только evidence_ledger и исследование. Верни только JSON:
{"process_map":[""],"hypotheses":[{"fact":"","process":"","problem":"","consequence":"","first_step":"","question":"","confidence":"high|medium|hypothesis","score":0,"falsifier":""}],
"selected_angle":{"hypothesis_index":0,"why":""},"contact_strategy":{"primary_role":"","backup_role":"","channel":"","why":""},
"first_message":{"recommended":"","soft":"","direct":"","cta_type":"interest|call"},
"followup_directions":["","", ""],"risks":[""],
"critic":{"score":0,"checks":{"facts_grounded":true,"one_angle":true,"one_cta":true,"natural":true,"role_relevant":true},"blockers":[""],"rewrite_required":false}}.
Нужно 2–4 гипотезы, выбрать ровно одну. Первое сообщение: 40–80 слов, 3–4 предложения, один проверяемый повод,
одна осторожная гипотеза, один лёгкий CTA, без ссылок/вложений. Если доказательств недостаточно — critic обязан
поставить rewrite_required=true и запретить отправку.''',
    }
    return _json_response(json.dumps(prompt, ensure_ascii=False), model=MODEL)


def ask_user_for_research_context(request: str, report: dict) -> dict:
    """The research agent's bounded ask_user tool: one factual question only."""
    payload = {"request": request, "gaps": report.get("gaps") or [], "facts": report.get("company_facts") or [], "signals": report.get("vacancy_signals") or []}
    return _json_response(
        """Ты вызываешь инструмент ask_user в процессе B2B research. Поиск уже был выполнен, но контекста недостаточно.
Спроси РОВНО один короткий вопрос на русском, который сильнее всего разблокирует проверяемое исследование.
Проси только публичный деловой контекст: сайт, ссылку на вакансию, название компании, город, описание продукта.
Не проси личные данные, не утверждай непроверенные факты. Верни только JSON:
{"question":"","why":"","expected":""}.\n\nКонтекст:\n""" + json.dumps(payload, ensure_ascii=False),
        model=MODEL,
    )
