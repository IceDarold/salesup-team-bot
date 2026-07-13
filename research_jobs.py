"""Durable queue and evidence store for long-running company research."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchJobStore:
    """SQLite-backed jobs. A lease makes the worker safe to restart or duplicate."""

    def __init__(self, path: str | None = None) -> None:
        db_path = Path(path or os.getenv("RESEARCH_JOBS_DB_PATH", "data/research-jobs.sqlite3"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_jobs (
                    id TEXT PRIMARY KEY, telegram_user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    progress_message_id INTEGER, request TEXT NOT NULL, refinement TEXT NOT NULL DEFAULT '',
                    contact_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '', google_url TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', source_count INTEGER NOT NULL DEFAULT 0,
                    iteration INTEGER NOT NULL DEFAULT 0, max_iterations INTEGER NOT NULL DEFAULT 6,
                    max_sources INTEGER NOT NULL DEFAULT 40, max_minutes INTEGER NOT NULL DEFAULT 20,
                    lease_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_research_jobs_queue ON research_jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS research_suggestions (
                    contact_id TEXT NOT NULL, telegram_user_id INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(contact_id, telegram_user_id)
                );
                CREATE TABLE IF NOT EXISTS outreach_plans (
                    contact_id TEXT PRIMARY KEY, research_job_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'research_ready', plan_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '', relevance REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, UNIQUE(job_id, url)
                );
                CREATE TABLE IF NOT EXISTS research_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, claim TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'hypothesis',
                    category TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_events_job ON research_events(job_id, id);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(research_jobs)").fetchall()}
            if "contact_id" not in columns:
                db.execute("ALTER TABLE research_jobs ADD COLUMN contact_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_research_jobs_contact ON research_jobs(contact_id, created_at)")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with self._db:
                yield self._db

    def create(self, *, telegram_user_id: int, chat_id: int, request: str, progress_message_id: int | None, contact_id: str = "") -> dict:
        job_id = uuid.uuid4().hex[:10]
        now = _now()
        with self._tx() as db:
            db.execute(
                """INSERT INTO research_jobs (id, telegram_user_id, chat_id, progress_message_id, request, contact_id, status, stage,
                   created_at, updated_at, max_iterations, max_sources, max_minutes)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', 'В очереди', ?, ?, ?, ?, ?)""",
                (job_id, telegram_user_id, chat_id, progress_message_id, request, contact_id,
                 now, now, int(os.getenv("DEEP_RESEARCH_MAX_ITERATIONS", "6")),
                 int(os.getenv("DEEP_RESEARCH_MAX_SOURCES", "40")), int(os.getenv("DEEP_RESEARCH_MAX_MINUTES", "20"))),
            )
        return self.get(job_id) or {}

    def has_suggestion(self, contact_id: str, telegram_user_id: int) -> bool:
        with self._lock:
            return bool(self._db.execute("SELECT 1 FROM research_suggestions WHERE contact_id=? AND telegram_user_id=?", (contact_id, telegram_user_id)).fetchone())

    def create_suggestion(self, contact_id: str, telegram_user_id: int) -> bool:
        now = _now()
        with self._tx() as db:
            result = db.execute("INSERT OR IGNORE INTO research_suggestions VALUES (?, ?, 'pending', ?, ?)", (contact_id, telegram_user_id, now, now))
        return bool(result.rowcount)

    def resolve_suggestion(self, contact_id: str, telegram_user_id: int, status: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE research_suggestions SET status=?, updated_at=? WHERE contact_id=? AND telegram_user_id=?", (status, _now(), contact_id, telegram_user_id))

    def latest_for_contact(self, contact_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM research_jobs WHERE contact_id=? AND status='completed' AND report != '' ORDER BY completed_at DESC LIMIT 1", (contact_id,)).fetchone()
        return dict(row) if row else None

    def save_outreach_plan(self, contact_id: str, research_job_id: str, plan: dict) -> None:
        now = _now()
        with self._tx() as db:
            db.execute(
                """INSERT INTO outreach_plans(contact_id,research_job_id,status,plan_json,created_at,updated_at)
                VALUES (?,?,'research_ready',?,?,?) ON CONFLICT(contact_id) DO UPDATE SET
                research_job_id=excluded.research_job_id,status=excluded.status,plan_json=excluded.plan_json,updated_at=excluded.updated_at""",
                (contact_id, research_job_id, json.dumps(plan, ensure_ascii=False), now, now),
            )

    def outreach_plan(self, contact_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM outreach_plans WHERE contact_id=?", (contact_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["plan"] = json.loads(item.pop("plan_json") or "{}")
        return item

    def get(self, job_id: str, telegram_user_id: int | None = None) -> dict | None:
        sql = "SELECT * FROM research_jobs WHERE id = ?"
        params: tuple = (job_id,)
        if telegram_user_id is not None:
            sql += " AND telegram_user_id = ?"
            params = (job_id, telegram_user_id)
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def claim_next(self, worker_id: str, lease_seconds: int = 300) -> dict | None:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._tx() as db:
            row = db.execute(
                """SELECT * FROM research_jobs WHERE status IN ('queued', 'planning', 'collecting', 'analyzing', 'reviewing')
                   AND (lease_until IS NULL OR lease_until < ?) ORDER BY created_at LIMIT 1""", (now.isoformat(),)
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE research_jobs SET lease_until=?, updated_at=?, started_at=COALESCE(started_at, ?) WHERE id=?",
                       (expires, now.isoformat(), now.isoformat(), row["id"]))
            claimed = db.execute("SELECT * FROM research_jobs WHERE id=?", (row["id"],)).fetchone()
        return dict(claimed) if claimed else None

    def update(self, job_id: str, *, status: str | None = None, stage: str | None = None,
               progress: int | None = None, detail: str | None = None, error: str | None = None,
               source_count: int | None = None, iteration: int | None = None, report: str | None = None,
               google_url: str | None = None, release_lease: bool = False) -> None:
        values: dict[str, object] = {"updated_at": _now()}
        for key, value in {"status": status, "stage": stage, "progress": progress, "detail": detail, "error": error,
                           "source_count": source_count, "iteration": iteration, "report": report, "google_url": google_url}.items():
            if value is not None:
                values[key] = value
        if status in TERMINAL:
            values["completed_at"] = _now()
            release_lease = True
        if release_lease:
            values["lease_until"] = None
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._tx() as db:
            db.execute(f"UPDATE research_jobs SET {assignments} WHERE id=?", (*values.values(), job_id))

    def cancel(self, job_id: str, telegram_user_id: int) -> bool:
        with self._tx() as db:
            result = db.execute("UPDATE research_jobs SET status='cancelled', stage='Отменено', detail='Отменено пользователем', lease_until=NULL, updated_at=?, completed_at=? WHERE id=? AND telegram_user_id=? AND status NOT IN ('completed','failed','cancelled')",
                                (_now(), _now(), job_id, telegram_user_id))
        return bool(result.rowcount)

    def refine(self, job_id: str, telegram_user_id: int, refinement: str) -> bool:
        with self._tx() as db:
            result = db.execute("UPDATE research_jobs SET refinement=?, status='queued', stage='Уточнение поставлено в очередь', progress=0, detail='', error='', report='', google_url='', iteration=0, lease_until=NULL, updated_at=?, completed_at=NULL WHERE id=? AND telegram_user_id=? AND status IN ('completed','failed','cancelled')",
                                (refinement, _now(), job_id, telegram_user_id))
        return bool(result.rowcount)

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get(job_id)
        return not job or job["status"] == "cancelled"

    def replace_evidence(self, job_id: str, sources: list[dict], claims: list[dict]) -> None:
        now = _now()
        with self._tx() as db:
            db.execute("DELETE FROM research_sources WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM research_claims WHERE job_id=?", (job_id,))
            for source in sources:
                url = str(source.get("url") or "").strip()
                if url:
                    db.execute("INSERT OR IGNORE INTO research_sources (job_id,url,title,excerpt,source_type,published_at,relevance,created_at) VALUES (?,?,?,?,?,?,?,?)",
                               (job_id, url, str(source.get("title") or ""), str(source.get("excerpt") or source.get("evidence") or ""),
                                str(source.get("source_type") or ""), str(source.get("published_at") or ""), float(source.get("relevance") or 0), now))
            for claim in claims:
                db.execute("INSERT INTO research_claims (job_id,claim,evidence,url,confidence,category,created_at) VALUES (?,?,?,?,?,?,?)",
                           (job_id, str(claim.get("claim") or ""), str(claim.get("evidence") or ""), str(claim.get("url") or ""),
                            str(claim.get("confidence") or "hypothesis"), str(claim.get("category") or ""), now))

    def add_event(self, job_id: str, kind: str, title: str, body: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO research_events (job_id, kind, title, body, created_at) VALUES (?, ?, ?, ?, ?)",
                       (job_id, kind, title, body, _now()))

    def events(self, job_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT kind,title,body,created_at FROM research_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, max(1, min(limit, 500))),
            ).fetchall()][::-1]

    def sources(self, job_id: str) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT url,title,excerpt,source_type,published_at,relevance FROM research_sources WHERE job_id=? ORDER BY relevance DESC, id", (job_id,)).fetchall()]

    def claims(self, job_id: str) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT claim,evidence,url,confidence,category FROM research_claims WHERE job_id=? ORDER BY id", (job_id,)).fetchall()]
