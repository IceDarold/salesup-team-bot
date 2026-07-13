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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from google_docs import create_company_research_tab
from research_jobs import ResearchJobStore
from sales_agent import deep_company_research

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


def run_job(store: ResearchJobStore, job: dict) -> None:
    job_id = str(job["id"])
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
        report, sources, claims = deep_company_research(
            str(job["request"]), max_iterations=int(job["max_iterations"]), max_sources=int(job["max_sources"]),
            refinement=str(job.get("refinement") or ""), progress=progress, cancelled=lambda: store.is_cancelled(job_id),
            trace=lambda kind, title, body: _trace(store, job, kind, title, body),
        )
        if store.is_cancelled(job_id):
            return
        store.replace_evidence(job_id, sources, claims)
        store.update(job_id, status="analyzing", stage="Публикация", progress=96, detail="Сохраняю доказательный отчёт в Google Docs.", source_count=len(sources), report=json.dumps(report, ensure_ascii=False))
        _notify_progress(store.get(job_id) or job)
        url = create_company_research_tab(_title(str(job["request"])), report)
        store.update(job_id, status="completed", stage="Готово", progress=100, detail="Отчёт готов.", source_count=len(sources), google_url=url)
        final = store.get(job_id) or job
        _notify_progress(final)
        _telegram("sendMessage", {"chat_id": job["chat_id"], "text": f"Исследование <code>{job_id}</code> готово.\nОтчёт с источниками: {html.escape(url)}\n\n/research_report {job_id}", "parse_mode": "HTML"})
    except InterruptedError:
        # Cancellation was persisted by the command; only release any active lease.
        store.update(job_id, release_lease=True)
        _notify_progress(store.get(job_id) or job)
    except TimeoutError as exc:
        store.update(job_id, status="failed", stage="Превышен лимит", progress=100, detail=str(exc), error=str(exc))
        _notify_progress(store.get(job_id) or job)
    except Exception as exc:
        logger.exception("Research job %s failed", job_id)
        store.update(job_id, status="failed", stage="Ошибка", progress=100, detail="Не удалось завершить исследование.", error=str(exc))
        _notify_progress(store.get(job_id) or job)


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
