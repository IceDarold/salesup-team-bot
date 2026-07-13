"""Background worker for durable deep-company research jobs.

Run with ``python -m research_worker`` under a separate systemd service.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from google_docs import create_company_research_tab
from research_jobs import ResearchJobStore
from notion_store import get_contact, update_contact_research_state, update_contact_research_url
from sales_agent import deep_company_research, start_usage_tracking, stop_usage_tracking, usage_snapshot
from outreach import ask_user_for_research_context, build_outreach_plan, quick_qualify

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("research-worker")
POLL_SECONDS = max(2, int(os.getenv("RESEARCH_WORKER_POLL_SECONDS", "5")))


def _telegram(method: str, payload: dict) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25):
            pass
    except Exception as exc:  # The job itself must not fail because progress delivery failed.
        logger.warning("Telegram %s failed: %s", method, exc)


def _notify_progress(job: dict) -> None:
    message_id = job.get("progress_message_id")
    if not message_id:
        return
    text = (f"Исследование <code>{html.escape(str(job['id']))}</code>\n"
            f"{html.escape(str(job.get('stage') or 'В работе'))}: {html.escape(str(job.get('detail') or ''))}\n"
            f"Прогресс: {int(job.get('progress') or 0)}% · источников: {int(job.get('source_count') or 0)}\n\n"
            f"/research_status {job['id']} · /research_cancel {job['id']}")
    _telegram("editMessageText", {"chat_id": job["chat_id"], "message_id": message_id, "text": text[:4000], "parse_mode": "HTML"})


def _trace(store: ResearchJobStore, job: dict, kind: str, title: str, body: str) -> None:
    """Persist and expose every research step as a separate Telegram message."""
    body = (body or "").strip()
    store.add_event(str(job["id"]), kind, title, body)
    icon = {"tool_call": "🔧", "tool_result": "📥", "model": "🧠"}.get(kind, "•")
    text = f"{icon} <b>{html.escape(title)}</b>\n\n<pre>{html.escape(body[:3300])}</pre>"
    _telegram("sendMessage", {"chat_id": job["chat_id"], "text": text[:3900], "parse_mode": "HTML"})


def _title(request: str) -> str:
    for word in request.split():
        if word.startswith(("http://", "https://")):
            return urllib.parse.urlparse(word).netloc or "Компания"
    return "Компания"


def _outreach_plan(report: dict, sources: list[dict], claims: list[dict], qualification: dict) -> dict:
    """Structured, auditable state for the later message/critic stages."""
    evidence = [
        {"claim": item.get("claim", ""), "evidence": item.get("evidence", ""), "url": item.get("url", ""),
         "confidence": item.get("confidence", "hypothesis"), "category": item.get("category", "")}
        for item in claims
    ]
    facts = report.get("company_facts") or []
    signals = report.get("vacancy_signals") or []
    pains = report.get("pains") or []
    return {
        "qualification": qualification,
        "evidence_ledger": evidence,
        "process_map": [],
        "hypotheses": pains[:4],
        "selected_angle": {},
        "contact_strategy": {},
        "message_strategy": {"status": "pending_human_review"},
        "facts": facts[:5], "signals": signals[:5],
        "sources": [{"url": item.get("url", ""), "title": item.get("title", "")} for item in sources],
        "risks": ["Не использовать hypothesis как подтверждённый факт в сообщении."],
    }


def _communication_mode(job: dict) -> dict:
    """Choose outreach stage from the actual archived dialogue, never from assumption."""
    if not job.get("contact_id"):
        return {"mode": "research_only", "reason": "Research запущен вне Contacts."}
    contact = get_contact(str(job["contact_id"]))
    status = str(contact.get("status") or "")
    if status in {"Отказ", "Интервью", "Показ", "Пилот", "Клиент"}:
        return {"mode": "no_cold_outreach", "reason": f"Текущий статус контакта: {status}."}
    path = os.getenv("TELEGRAM_USER_DB_PATH", "data/telegram-users.sqlite3")
    try:
        db = sqlite3.connect(path)
        rows = db.execute("SELECT outgoing FROM telegram_archived_messages WHERE telegram_user_id=? AND contact_id=? ORDER BY sent_at", (int(job["telegram_user_id"]), str(job["contact_id"]))).fetchall()
        db.close()
    except Exception:
        rows = []
    if any(not bool(row[0]) for row in rows):
        return {"mode": "reply_analysis", "reason": "В переписке уже есть входящий ответ контакта."}
    if any(bool(row[0]) for row in rows):
        return {"mode": "next_followup", "reason": "Первое исходящее сообщение уже отправлено."}
    return {"mode": "first_message", "reason": "Исходящих сообщений в архиве нет."}


def _needs_more_context(report: dict, sources: list[dict]) -> bool:
    """Ask only after a genuine search failed to establish a usable lead."""
    if not sources:
        return True
    facts = report.get("company_facts") or []
    signals = report.get("vacancy_signals") or []
    return not facts and not signals


def _clarification_request(job: dict, report: dict) -> str:
    """Turn evidence gaps into a short, actionable human question."""
    gaps = [str(item).strip() for item in (report.get("gaps") or []) if str(item).strip()][:2]
    known = str(job.get("request") or "")
    hints = []
    if "сайт компании: не указан" in known:
        hints.append("сайт компании")
    if "вакансия/триггер: не указан" in known:
        hints.append("ссылку на вакансию или иной триггер")
    if "Компания: не указана" in known:
        hints.append("точное название компании или город")
    requested = ", ".join(hints) or "сайт, вакансию, ссылку на компанию или пару слов о бизнесе"
    gap_text = f" Не удалось проверить: {'; '.join(gaps)}." if gaps else ""
    return f"Я уже попробовал поиск по доступным данным, включая имя контакта.{gap_text} Пришли, пожалуйста, {requested} — и я продолжу тот же research."


def _usage_line(usage: dict, seconds: float) -> str:
    input_tokens, output_tokens = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    in_rate = float(os.getenv("RESEARCH_INPUT_USD_PER_M_TOKENS", "0") or 0)
    out_rate = float(os.getenv("RESEARCH_OUTPUT_USD_PER_M_TOKENS", "0") or 0)
    cost = input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate
    cost_text = f"${cost:.4f}" if in_rate or out_rate else "не настроена"
    return f"Время: {seconds:.0f} сек. · токены: {input_tokens:,} input / {output_tokens:,} output · стоимость: {cost_text}"


def run_job(store: ResearchJobStore, job: dict) -> None:
    job_id = str(job["id"])
    started = time.monotonic()
    usage_token = start_usage_tracking()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=int(job["max_minutes"]))
    last_notify = 0.0

    def progress(stage: str, percent: int, detail: str) -> None:
        nonlocal last_notify
        if datetime.now(timezone.utc) >= deadline:
            raise TimeoutError("Превышен лимит времени исследования.")
        iteration_match = re.search(r"Итерация\s+(\d+)", detail)
        store.update(job_id, status=stage, stage={
            "planning": "Планирование", "collecting": "Сбор источников", "analyzing": "Анализ", "reviewing": "Проверка",
        }.get(stage, stage), progress=percent, detail=detail,
                     iteration=int(iteration_match.group(1)) if iteration_match else None)
        current = store.get(job_id)
        if current and (time.monotonic() - last_notify > 12 or percent >= 90):
            _notify_progress(current)
            last_notify = time.monotonic()

    try:
        progress("planning", 3, "Быстро проверяю соответствие ICP и наличие триггера.")
        qualification = quick_qualify(str(job["request"]))
        if str(qualification.get("decision")) == "reject":
            plan = {"qualification": qualification, "evidence_ledger": [], "hypotheses": [], "selected_angle": {},
                    "contact_strategy": {}, "first_message": {}, "critic": {"rewrite_required": True, "blockers": ["Лид отклонён на быстрой квалификации."]}}
            if job.get("contact_id"):
                store.save_outreach_plan(str(job["contact_id"]), job_id, plan)
                update_contact_research_state(str(job["contact_id"]), "Done")
            store.update(job_id, status="completed", stage="Лид отклонён", progress=100, detail=str(qualification.get("reason") or "Не рекомендую писать."), report=json.dumps({"qualification": qualification}, ensure_ascii=False))
            _telegram("sendMessage", {"chat_id": job["chat_id"], "text": f"По быстрой квалификации: <b>не рекомендую писать</b>.\n{html.escape(str(qualification.get('reason') or 'Недостаточно оснований.'))}", "parse_mode": "HTML"})
            return
        report, sources, claims = deep_company_research(
            str(job["request"]), max_iterations=int(job["max_iterations"]), max_sources=int(job["max_sources"]),
            refinement=str(job.get("refinement") or ""), progress=progress, cancelled=lambda: store.is_cancelled(job_id),
            trace=lambda kind, title, body: _trace(store, job, kind, title, body),
        )
        if store.is_cancelled(job_id):
            return
        if _needs_more_context(report, sources):
            store.replace_evidence(job_id, sources, claims)
            store.update(job_id, status="waiting_input", stage="Нужны уточнения", progress=100,
                         detail="Поиск выполнен, но недостаточно данных, чтобы надёжно продолжить.",
                         report=json.dumps(report, ensure_ascii=False), source_count=len(sources), release_lease=True)
            buttons = json.dumps({"inline_keyboard": [[{"text": "➕ Добавить данные", "callback_data": f"research_input:provide:{job_id}"}]]})
            try:
                ask = ask_user_for_research_context(str(job.get("request") or ""), report)
                question = str(ask.get("question") or "").strip()
                why = str(ask.get("why") or "").strip()
                expected = str(ask.get("expected") or "").strip()
                text = question + (f"\n\nЗачем: {why}" if why else "") + (f"\nПодойдёт: {expected}" if expected else "")
            except Exception:
                logger.exception("Agent ask_user failed; using deterministic clarification")
                text = _clarification_request(job, report)
            _telegram("sendMessage", {"chat_id": job["chat_id"], "text": text, "reply_markup": buttons})
            return
        store.replace_evidence(job_id, sources, claims)
        plan = _outreach_plan(report, sources, claims, qualification)
        try:
            plan.update(build_outreach_plan(report, sources, claims, qualification))
        except Exception:
            logger.exception("Outreach strategy generation failed for research %s", job_id)
            plan["critic"] = {"rewrite_required": True, "blockers": ["Не удалось безопасно сгенерировать стратегию."]}
        communication = _communication_mode(job)
        plan["communication_state"] = communication
        if communication["mode"] != "first_message":
            plan["first_message"] = {}
        if job.get("contact_id"):
            store.save_outreach_plan(str(job["contact_id"]), job_id, plan)
        store.update(job_id, status="analyzing", stage="Публикация", progress=96, detail="Сохраняю доказательный отчёт в Google Docs.", source_count=len(sources), report=json.dumps(report, ensure_ascii=False))
        _notify_progress(store.get(job_id) or job)
        url = create_company_research_tab(_title(str(job["request"])), report)
        usage = usage_snapshot()
        elapsed = time.monotonic() - started
        store.update(job_id, status="completed", stage="Готово", progress=100, detail="Отчёт готов.", source_count=len(sources), google_url=url, usage=usage, duration_seconds=elapsed)
        if job.get("contact_id"):
            update_contact_research_url(str(job["contact_id"]), url)
        final = store.get(job_id) or job
        _notify_progress(final)
        draft = str((plan.get("first_message") or {}).get("recommended") or "")
        critic = plan.get("critic") or {}
        mode = (plan.get("communication_state") or {}).get("mode", "research_only")
        verdict = "можно проверить и отправить" if mode == "first_message" and draft and not critic.get("rewrite_required") else "первое сообщение не предлагается"
        next_text = {"next_followup": "Первое касание уже есть: research будет использован для следующего follow-up.", "reply_analysis": "Контакт уже ответил: автоматические касания не предлагаются, нужен разбор ответа.", "no_cold_outreach": "Контакт уже не в стадии холодного outreach."}.get(mode, "")
        _telegram("sendMessage", {"chat_id": job["chat_id"], "text": f"Исследование <code>{job_id}</code> готово.\nОтчёт: {html.escape(url)}\n{_usage_line(usage, elapsed)}\n\n<b>Outreach verdict:</b> {verdict}\n{html.escape(next_text)}\n{('Черновик:\n' + html.escape(draft)) if draft else ''}\n\n/research_report {job_id}", "parse_mode": "HTML"})
    except InterruptedError:
        # Cancellation was persisted by the command; only release any active lease.
        store.update(job_id, release_lease=True)
        _notify_progress(store.get(job_id) or job)
    except TimeoutError as exc:
        store.update(job_id, status="failed", stage="Превышен лимит", progress=100, detail=str(exc), error=str(exc))
        if job.get("contact_id"):
            try:
                update_contact_research_state(str(job["contact_id"]), "Failed")
            except Exception:
                logger.exception("Unable to mark timed out research as failed")
        _notify_progress(store.get(job_id) or job)
    except Exception as exc:
        logger.exception("Research job %s failed", job_id)
        store.update(job_id, status="failed", stage="Ошибка", progress=100, detail="Не удалось завершить исследование.", error=str(exc))
        if job.get("contact_id"):
            try:
                update_contact_research_state(str(job["contact_id"]), "Failed")
            except Exception:
                logger.exception("Unable to mark Contact research as failed")
        _notify_progress(store.get(job_id) or job)
        if "insufficient permissions" in str(exc).lower() or "authentication" in str(exc).lower() or "api key" in str(exc).lower():
            _telegram("sendMessage", {"chat_id": job["chat_id"], "text": "Research не запущен: у OpenAI API-ключа нет нужного доступа. Обновите OPENAI_API_KEY на сервере и повторите задачу."})
    finally:
        stop_usage_tracking(usage_token)


def main() -> None:
    store = ResearchJobStore()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    logger.info("Research worker started: %s", worker_id)
    while True:
        job = store.claim_next(worker_id)
        if not job:
            time.sleep(POLL_SECONDS)
            continue
        if job["status"] == "cancelled":
            continue
        run_job(store, job)


if __name__ == "__main__":
    main()
