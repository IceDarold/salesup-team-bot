"""Persist structured interview analysis to Notion."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
SCHEDULED_STATUS = "Sheduled"
INTERVIEWER_FEEDBACK_PROPERTY = "Interviewer feedback"
NOTION_MAX_RETRIES = int(os.getenv("NOTION_MAX_RETRIES", "3"))
NOTION_RETRY_BACKOFF_SECONDS = float(os.getenv("NOTION_RETRY_BACKOFF_SECONDS", "1.5"))
STATS_TIMEZONE = os.getenv("STATS_TIMEZONE", "Asia/Nicosia")

CONTACT_STATUS_ORDER = (
    "Новый",
    "Написали",
    "Ответил",
    "Согласился на интервью",
    "Интервью",
    "Показ",
    "Пилот",
    "Клиент",
    "Отказ",
    "No response",
)
AGREED_STATUSES = {"Согласился на интервью", "Интервью", "Показ", "Пилот", "Клиент"}
INTERVIEW_STATUSES = {"Интервью", "Показ", "Пилот", "Клиент"}

DEDUPE_TABLES = {
    "jtbd": {
        "label": "JTBD",
        "env": "NOTION_JTBD_DB_ID",
        "title_prop": "Job",
        "title_key": "job",
        "evidence_prop": "Evidence quote",
        "detail_props": ["Context", "Current solution", "Desired outcome", "Pain level", "Aich relevance", "Evidence quote"],
    },
    "pains": {
        "label": "Pains",
        "env": "NOTION_PAINS_DB_ID",
        "title_prop": "Pain",
        "title_key": "pain",
        "evidence_prop": "Evidence quote",
        "detail_props": ["Cause", "Consequence", "Current workaround", "Severity", "Can Aich help", "Evidence quote"],
    },
    "barriers": {
        "label": "Barriers",
        "env": "NOTION_BARRIERS_DB_ID",
        "title_prop": "Barrier",
        "title_key": "barrier",
        "evidence_prop": "Evidence quote",
        "detail_props": ["Category", "Severity", "How to reduce", "Evidence quote"],
    },
    "willingness_to_pay": {
        "label": "Willingness to Pay",
        "env": "NOTION_WTP_DB_ID",
        "title_prop": "Respondent label",
        "title_key": "respondent_label",
        "evidence_prop": "Evidence quote",
        "detail_props": [
            "Segment",
            "Role",
            "WTP status",
            "WTP strength",
            "Who pays",
            "Would pay for",
            "Payment conditions",
            "Payment objections",
            "Price mentioned",
            "Evidence quote",
            "Researcher comment",
        ],
    },
    "product_opportunities": {
        "label": "Product Opportunities",
        "env": "NOTION_OPPORTUNITIES_DB_ID",
        "title_prop": "Opportunity",
        "title_key": "opportunity",
        "evidence_prop": "",
        "detail_props": ["Problem solved", "Target segment", "Linked JTBD", "Linked Pains", "MVP test", "Confidence"],
    },
}


def find_team_member_by_telegram(user_id: int | str | None, username: str | None) -> dict | None:
    """Find a Notion Team Members page by Telegram user_id or username."""
    client = _NotionClient()
    db_id = os.getenv("NOTION_TEAM_MEMBERS_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_TEAM_MEMBERS_DB_ID environment variable is not set.")

    user_id_text = str(user_id).strip() if user_id else ""
    username_text = _normalize_username(username)
    filters = []
    if user_id_text:
        filters.append({"property": "Telegram user_id", "rich_text": {"equals": user_id_text}})
    if username_text:
        filters.append({"property": "Telegram username", "rich_text": {"equals": f"@{username_text}"}})
        filters.append({"property": "Telegram username", "rich_text": {"equals": username_text}})
    if not filters:
        return None

    payload = {"filter": filters[0] if len(filters) == 1 else {"or": filters}, "page_size": 1}
    response = client.query_database(db_id, payload)
    results = response.get("results", [])
    if not results:
        return None
    return _team_member_from_page(results[0])


def list_team_members() -> list[dict]:
    client = _NotionClient()
    db_id = os.getenv("NOTION_TEAM_MEMBERS_DB_ID")
    if not db_id:
        return []
    response = client.query_database(db_id, {"page_size": 100})
    return [_team_member_from_page(page) for page in response.get("results", [])]


def get_contact_stats(member_page_id: str | None = None) -> dict:
    """Return current and today contact funnel statistics.

    Today's activity is defined by the Contacts ``Последнее касание`` field.
    When ``member_page_id`` is omitted, the result covers the whole team.
    """
    db_id = os.getenv("NOTION_CONTACTS_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_CONTACTS_DB_ID environment variable is not set.")

    payload: dict = {"page_size": 100}
    if member_page_id:
        payload["filter"] = {
            "property": "Owner",
            "relation": {"contains": member_page_id},
        }

    response = _NotionClient().query_database_all(db_id, payload)
    contacts = [_contact_from_page(page) for page in response.get("results", [])]
    try:
        today = datetime.now(ZoneInfo(STATS_TIMEZONE)).date()
    except Exception:
        logger.warning("Invalid STATS_TIMEZONE=%r; falling back to local date", STATS_TIMEZONE)
        today = date.today()

    return {
        "date": today,
        "today": _contact_funnel(contact for contact in contacts if _is_on_date(contact["last_touch"], today)),
        "all": _contact_funnel(contacts),
    }


def get_contact_form_options() -> dict[str, list[str]]:
    """Return the currently configured segments and sources from Contacts."""
    db_id = _contacts_db_id()
    database = _NotionClient().retrieve_database(db_id)
    props = database.get("properties", {})
    segments = [item.get("name", "") for item in (props.get("Segment", {}).get("multi_select", {}).get("options", []))]
    sources = [item.get("name", "") for item in (props.get("Источник", {}).get("select", {}).get("options", []))]
    return {
        "segments": [item for item in segments if item],
        "sources": [item for item in sources if item],
    }


def create_contact(*, owner_id: str, name: str, contact: str, segment: str, source: str) -> str:
    """Create a new Contacts page assigned to the team member who added it."""
    properties = {
        "Name": _title(name),
        "Контакт": _rich_text(contact),
        "Owner": _relation(owner_id),
        "Status": {"status": {"name": "Новый"}},
        "Segment": {"multi_select": [{"name": segment}]},
        "Источник": {"select": {"name": source}},
    }
    response = _NotionClient().create_page(_contacts_db_id(), properties)
    return response.get("url") or ""


def find_contacts(
    *,
    member_page_id: str | None = None,
    query: str = "",
    status: str = "",
    segment: str = "",
    source: str = "",
    limit: int = 20,
) -> list[dict]:
    """Find Contacts by text and structured fields, optionally scoped to one owner."""
    payload: dict = {"page_size": 100}
    if member_page_id:
        payload["filter"] = {"property": "Owner", "relation": {"contains": member_page_id}}
    response = _NotionClient().query_database_all(_contacts_db_id(), payload)
    needle = query.casefold().strip()
    expected_status = status.casefold().strip()
    expected_segment = segment.casefold().strip()
    expected_source = source.casefold().strip()
    contacts = []
    for page in response.get("results", []):
        item = _contact_from_page(page)
        haystack = " ".join(
            [item["name"], item["contact"], item["status"], ", ".join(item["segments"]), item["source"]]
        ).casefold()
        if needle and needle not in haystack:
            continue
        if expected_status and item["status"].casefold() != expected_status:
            continue
        if expected_segment and expected_segment not in {value.casefold() for value in item["segments"]}:
            continue
        if expected_source and item["source"].casefold() != expected_source:
            continue
        contacts.append(item)
        if len(contacts) >= max(1, min(limit, 50)):
            break
    return contacts


def _contacts_db_id() -> str:
    db_id = os.getenv("NOTION_CONTACTS_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_CONTACTS_DB_ID environment variable is not set.")
    return db_id


def get_scheduled_interviews_for_member(member_page_id: str) -> list[dict]:
    client = _NotionClient()
    db_id = os.getenv("NOTION_INTERVIEWS_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_INTERVIEWS_DB_ID environment variable is not set.")
    response = client.query_database_all(
        db_id,
        {
            "filter": {
                "and": [
                    {"property": "Status", "status": {"equals": SCHEDULED_STATUS}},
                    {"property": "Owner", "relation": {"contains": member_page_id}},
                ]
            },
            "sorts": [{"property": "Meeting Date", "direction": "ascending"}],
            "page_size": 50,
        },
    )
    return [_interview_from_page(page) for page in response.get("results", [])]


def get_interview(interview_page_id: str) -> dict:
    client = _NotionClient()
    return _interview_from_page(client.retrieve_page(interview_page_id))


def update_interview_goal(interview_page_id: str, goal: str) -> None:
    """Update the interview goal if the Interviews database has a Goal/goal property."""
    goal = _text(goal)
    if not goal:
        return

    client = _NotionClient()
    page = client.retrieve_page(interview_page_id)
    props = page.get("properties", {})
    goal_property_name = _goal_property_name(props)
    if not goal_property_name:
        logger.warning("Interview page %s has no Goal/goal property", interview_page_id)
        return
    client.update_page(interview_page_id, {goal_property_name: _rich_text(goal)})


def update_interview_transcript(interview_page_id: str, transcript_url: str) -> None:
    transcript_url = _text(transcript_url)
    if not interview_page_id or not transcript_url:
        return
    _NotionClient().update_page(interview_page_id, {"Transcript": {"url": transcript_url}})


def update_interviewer_feedback_url(interview_page_id: str, feedback_url: str) -> None:
    feedback_url = _text(feedback_url)
    if not interview_page_id or not feedback_url:
        return
    client = _NotionClient()
    _ensure_interviewer_feedback_property(client)
    client.update_page(interview_page_id, {INTERVIEWER_FEEDBACK_PROPERTY: {"url": feedback_url}})


def save_analysis_to_notion(
    answers: dict,
    analysis: dict,
    *,
    transcript_url: str | None = None,
    report_url: str | None = None,
    dedupe_plan: dict | None = None,
) -> str:
    """Save structured analysis to Notion and return the Interview page URL."""
    client = _NotionClient()
    interview_page_id = answers.get("notion_interview_page_id")
    old_records = []
    if interview_page_id:
        _update_interview(client, interview_page_id, analysis, transcript_url, report_url)
        old_records = _existing_analysis_records(client, interview_page_id)
    else:
        interview_page_id = _create_interview(client, answers, analysis, transcript_url, report_url)

    if dedupe_plan:
        _save_analysis_with_dedupe(client, interview_page_id, analysis, dedupe_plan, answers)
    else:
        _create_jtbd(client, interview_page_id, analysis.get("jtbd", []))
        _create_pains(client, interview_page_id, analysis.get("pains", []))
        _create_barriers(client, interview_page_id, analysis.get("barriers", []))
        _create_wtp(client, interview_page_id, analysis.get("willingness_to_pay", {}))
        _create_opportunities(client, interview_page_id, analysis.get("product_opportunities", []))
    _archive_records(client, old_records, skip_page_ids=_dedupe_merge_targets(dedupe_plan))
    logger.info("Structured analysis saved to Notion interview page %s", interview_page_id)
    return f"https://www.notion.so/{interview_page_id.replace('-', '')}"


def build_dedupe_plan(analysis: dict, *, interview_page_id: str | None = None) -> dict:
    """Build merge/create decisions for all structured insight tables."""
    from insights import dedupe_notion_items

    client = _NotionClient()
    plan = {"tables": {}}
    for table_key, config in DEDUPE_TABLES.items():
        db_id = os.getenv(config["env"])
        new_items = _new_dedupe_items(table_key, analysis)
        if not db_id or not new_items:
            continue
        existing_items = _existing_dedupe_items(client, db_id, config)
        decisions = dedupe_notion_items(config["label"], new_items, existing_items)
        plan["tables"][table_key] = {
            "label": config["label"],
            "new_items": new_items,
            "existing_items": existing_items,
            "decisions": decisions,
        }
    return plan


def _existing_analysis_records(client, interview_page_id: str) -> list[dict]:
    related_databases = [
        ("NOTION_JTBD_DB_ID", "JTBD"),
        ("NOTION_PAINS_DB_ID", "Pains"),
        ("NOTION_BARRIERS_DB_ID", "Barriers"),
        ("NOTION_WTP_DB_ID", "Willingness to Pay"),
        ("NOTION_OPPORTUNITIES_DB_ID", "Product Opportunities"),
    ]
    records = []
    for env_name, label in related_databases:
        db_id = os.getenv(env_name)
        if not db_id:
            continue
        response = client.query_database_all(
            db_id,
            {
                "filter": {"property": "Interview", "relation": {"contains": interview_page_id}},
                "page_size": 100,
            },
        )
        for page in response.get("results", []):
            props = page.get("properties", {})
            interviews = _prop_relation(props.get("Interview"))
            records.append({"id": page["id"], "label": label, "relation_count": len(interviews)})
    return records


def _archive_records(client, records: list[dict], *, skip_page_ids: set[str] | None = None) -> None:
    skip_page_ids = skip_page_ids or set()
    archived_count = 0
    for record in records:
        if record["id"] in skip_page_ids:
            logger.info("Keeping old %s record %s because it is a dedupe merge target", record.get("label"), record["id"])
            continue
        if record.get("relation_count", 0) > 1:
            logger.info("Keeping shared old %s record %s because it is linked to multiple interviews", record.get("label"), record["id"])
            continue
        client.archive_page(record["id"])
        archived_count += 1
        logger.info("Archived old %s record %s", record.get("label"), record["id"])
    if archived_count:
        logger.info("Archived %d old structured analysis records", archived_count)


def _create_interview(
    client,
    answers: dict,
    analysis: dict,
    transcript_url: str | None,
    report_url: str | None,
) -> str:
    interview = analysis.get("interview", {})
    name = _text(answers.get("name")) or _text(interview.get("respondent_label")) or "Interview"
    segment = _map_segment(_text(answers.get("segment")) or _text(interview.get("segment")))
    comment = _join_parts(
        [
            _text(interview.get("summary")),
            f"SalesUp value fit: {_text(interview.get('aich_value_fit'))}",
            f"ICP fit: {_text(interview.get('icp_fit'))}",
            f"Report: {report_url}" if report_url else "",
        ]
    )

    properties = {
        "Name": _title(name),
        "Status": {"status": {"name": "Interviewed"}},
        "Comment": _rich_text(comment),
        "Meeting Date": {"date": {"start": date.today().isoformat()}},
    }
    if segment:
        properties["Segment"] = {"multi_select": [{"name": segment}]}
    if transcript_url:
        properties["Transcript"] = {"url": transcript_url}

    response = client.create_page(os.environ["NOTION_INTERVIEWS_DB_ID"], properties)
    return response["id"]


def _update_interview(
    client,
    interview_page_id: str,
    analysis: dict,
    transcript_url: str | None,
    report_url: str | None,
) -> None:
    interview = analysis.get("interview", {})
    properties = {
        "Status": {"status": {"name": "Interviewed"}},
        "Summary": _rich_text(interview.get("summary")),
    }
    if transcript_url:
        properties["Transcript"] = {"url": transcript_url}
    if report_url:
        properties["Telegra.ph report"] = {"url": report_url}

    client.update_page(interview_page_id, properties)


def _create_jtbd(client, interview_page_id: str, items: list[dict]) -> None:
    db_id = os.getenv("NOTION_JTBD_DB_ID")
    if not db_id:
        return
    for item in items:
        _create_jtbd_item(client, db_id, interview_page_id, item)


def _create_pains(client, interview_page_id: str, items: list[dict]) -> None:
    db_id = os.getenv("NOTION_PAINS_DB_ID")
    if not db_id:
        return
    for item in items:
        _create_pain_item(client, db_id, interview_page_id, item)


def _create_barriers(client, interview_page_id: str, items: list[dict]) -> None:
    db_id = os.getenv("NOTION_BARRIERS_DB_ID")
    if not db_id:
        return
    for item in items:
        _create_barrier_item(client, db_id, interview_page_id, item)


def _create_wtp(client, interview_page_id: str, item: dict) -> None:
    db_id = os.getenv("NOTION_WTP_DB_ID")
    if not db_id or not item:
        return
    _create_wtp_item(client, db_id, interview_page_id, item)


def _create_opportunities(client, interview_page_id: str, items: list[dict]) -> None:
    db_id = os.getenv("NOTION_OPPORTUNITIES_DB_ID")
    if not db_id:
        return
    for item in items:
        _create_opportunity_item(client, db_id, interview_page_id, item)


def _create_jtbd_item(client, db_id: str, interview_page_id: str, item: dict) -> None:
    client.create_page(
        db_id,
        {
            "Job": _title(item.get("job")),
            "Interview": _relation(interview_page_id),
            "Context": _rich_text(item.get("context")),
            "Current solution": _rich_text(item.get("current_solution")),
            "Desired outcome": _rich_text(item.get("desired_outcome")),
            "Pain level": _select(item.get("pain_level")),
            "Aich relevance": _select(item.get("aich_relevance")),
            "Evidence quote": _rich_text(item.get("evidence_quote")),
            "Confidence": _select(item.get("confidence")),
        },
    )


def _create_pain_item(client, db_id: str, interview_page_id: str, item: dict) -> None:
    client.create_page(
        db_id,
        {
            "Pain": _title(item.get("pain")),
            "Interview": _relation(interview_page_id),
            "Cause": _rich_text(item.get("cause")),
            "Consequence": _rich_text(item.get("consequence")),
            "Current workaround": _rich_text(item.get("current_workaround")),
            "Severity": _select(item.get("severity")),
            "Can Aich help": _select(item.get("can_aich_help")),
            "Evidence quote": _rich_text(item.get("evidence_quote")),
            "Confidence": _select(item.get("confidence")),
        },
    )


def _create_barrier_item(client, db_id: str, interview_page_id: str, item: dict) -> None:
    client.create_page(
        db_id,
        {
            "Barrier": _title(item.get("barrier")),
            "Interview": _relation(interview_page_id),
            "Category": _select(item.get("category")),
            "Severity": _select(item.get("severity")),
            "How to reduce": _rich_text(item.get("how_to_reduce")),
            "Evidence quote": _rich_text(item.get("evidence_quote")),
            "Confidence": _select(item.get("confidence")),
        },
    )


def _create_wtp_item(client, db_id: str, interview_page_id: str, item: dict) -> None:
    client.create_page(
        db_id,
        {
            "Respondent label": _title(item.get("respondent_label")),
            "Interview": _relation(interview_page_id),
            "Segment": _rich_text(item.get("segment")),
            "Role": _rich_text(item.get("role")),
            "WTP status": _select(item.get("wtp_status")),
            "WTP strength": _select(item.get("wtp_strength")),
            "Who pays": _select(item.get("who_pays")),
            "Would pay for": _rich_text(item.get("would_pay_for")),
            "Payment conditions": _rich_text(item.get("payment_conditions")),
            "Payment objections": _rich_text(item.get("payment_objections")),
            "Price mentioned": _rich_text(item.get("price_mentioned")),
            "Evidence quote": _rich_text(item.get("evidence_quote")),
            "Researcher comment": _rich_text(item.get("researcher_comment")),
            "Confidence": _select(item.get("confidence")),
        },
    )


def _create_opportunity_item(client, db_id: str, interview_page_id: str, item: dict) -> None:
    client.create_page(
        db_id,
        {
            "Opportunity": _title(item.get("opportunity")),
            "Interview": _relation(interview_page_id),
            "Problem solved": _rich_text(item.get("problem_solved")),
            "Target segment": _rich_text(item.get("target_segment")),
            "Linked JTBD": _rich_text(", ".join(item.get("linked_jtbd") or [])),
            "Linked Pains": _rich_text(", ".join(item.get("linked_pains") or [])),
            "MVP test": _rich_text(item.get("mvp_test")),
            "Confidence": _select(item.get("confidence")),
            "Status": _select("raw"),
        },
    )


def _save_analysis_with_dedupe(
    client,
    interview_page_id: str,
    analysis: dict,
    dedupe_plan: dict,
    answers: dict,
) -> None:
    for table_key, config in DEDUPE_TABLES.items():
        db_id = os.getenv(config["env"])
        if not db_id:
            continue
        items = _analysis_items(table_key, analysis)
        if not items:
            continue
        decisions = _decision_map((dedupe_plan.get("tables") or {}).get(table_key, {}))
        existing_by_id = _existing_map((dedupe_plan.get("tables") or {}).get(table_key, {}))
        for index, item in enumerate(items):
            temp_id = f"{table_key}:{index}"
            decision = decisions.get(temp_id) or {"decision": "create_new"}
            if decision.get("decision") == "merge_existing" and decision.get("existing_id"):
                existing = existing_by_id.get(decision["existing_id"])
                _merge_dedupe_record(
                    client,
                    table_key,
                    decision["existing_id"],
                    existing,
                    interview_page_id,
                    item,
                    answers,
                )
            else:
                _create_analysis_item(client, table_key, db_id, interview_page_id, item)


def _create_analysis_item(client, table_key: str, db_id: str, interview_page_id: str, item: dict) -> None:
    if table_key == "jtbd":
        _create_jtbd_item(client, db_id, interview_page_id, item)
    elif table_key == "pains":
        _create_pain_item(client, db_id, interview_page_id, item)
    elif table_key == "barriers":
        _create_barrier_item(client, db_id, interview_page_id, item)
    elif table_key == "willingness_to_pay":
        _create_wtp_item(client, db_id, interview_page_id, item)
    elif table_key == "product_opportunities":
        _create_opportunity_item(client, db_id, interview_page_id, item)


def _merge_dedupe_record(
    client,
    table_key: str,
    page_id: str,
    existing: dict | None,
    interview_page_id: str,
    item: dict,
    answers: dict,
) -> None:
    existing = existing or {}
    interview_ids = list(existing.get("interview_ids") or [])
    if interview_page_id not in interview_ids:
        interview_ids.append(interview_page_id)
    properties = {"Interview": _relation_many(interview_ids)}
    evidence_prop = DEDUPE_TABLES[table_key].get("evidence_prop")
    if evidence_prop:
        evidence = _merged_evidence(existing.get("evidence_quote"), item.get("evidence_quote"), answers)
        properties[evidence_prop] = _rich_text(evidence)
    client.update_page(page_id, properties)


def _merged_evidence(old_value: str | None, new_quote: str | None, answers: dict) -> str:
    old_text = _text(old_value)
    quote = _text(new_quote)
    if not quote:
        return old_text
    name = _text(answers.get("name")) or "новое интервью"
    addition = f"{name}: {quote}"
    if addition in old_text:
        return old_text
    return _join_parts([old_text, addition])


def _new_dedupe_items(table_key: str, analysis: dict) -> list[dict]:
    config = DEDUPE_TABLES[table_key]
    items = []
    for index, item in enumerate(_analysis_items(table_key, analysis)):
        title = _text(item.get(config["title_key"]))
        items.append(
            {
                "temp_id": f"{table_key}:{index}",
                "title": title,
                "content": item,
            }
        )
    return items


def _analysis_items(table_key: str, analysis: dict) -> list[dict]:
    if table_key == "willingness_to_pay":
        item = analysis.get("willingness_to_pay") or {}
        return [item] if item else []
    return list(analysis.get(table_key) or [])


def _existing_dedupe_items(client, db_id: str, config: dict) -> list[dict]:
    response = client.query_database_all(db_id, {"page_size": 100})
    items = []
    for page in response.get("results", []):
        props = page.get("properties", {})
        detail_values = []
        for prop_name in config.get("detail_props") or []:
            value = _prop_as_text(props.get(prop_name))
            if value:
                detail_values.append(f"{prop_name}: {value}")
        items.append(
            {
                "id": page["id"],
                "title": _prop_title(props.get(config["title_prop"])),
                "description": "\n".join(detail_values),
                "evidence_quote": _prop_text(props.get("Evidence quote")),
                "interview_ids": _prop_relation(props.get("Interview")),
            }
        )
    return items


def _decision_map(table_plan: dict) -> dict:
    return {item.get("temp_id"): item for item in table_plan.get("decisions") or [] if item.get("temp_id")}


def _existing_map(table_plan: dict) -> dict:
    return {item.get("id"): item for item in table_plan.get("existing_items") or [] if item.get("id")}


def _dedupe_merge_targets(dedupe_plan: dict | None) -> set[str]:
    if not dedupe_plan:
        return set()
    targets = set()
    for table in (dedupe_plan.get("tables") or {}).values():
        for decision in table.get("decisions") or []:
            if decision.get("decision") == "merge_existing" and decision.get("existing_id"):
                targets.add(decision["existing_id"])
    return targets


class _NotionClient:
    def __init__(self) -> None:
        self.token = os.getenv("NOTION_TOKEN")
        if not self.token:
            raise RuntimeError("NOTION_TOKEN environment variable is not set.")

    def create_page(self, database_id: str, properties: dict) -> dict:
        return self._request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": database_id},
                "properties": _compact_properties(properties),
            },
        )

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request(
            "PATCH",
            f"/pages/{page_id}",
            {"properties": _compact_properties(properties)},
        )

    def archive_page(self, page_id: str) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"archived": True})

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def query_database(self, database_id: str, body: dict) -> dict:
        return self._request("POST", f"/databases/{database_id}/query", body)

    def query_database_all(self, database_id: str, body: dict) -> dict:
        results = []
        next_cursor = None
        while True:
            page_body = dict(body)
            if next_cursor:
                page_body["start_cursor"] = next_cursor
            response = self.query_database(database_id, page_body)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                return {"results": results}
            next_cursor = response.get("next_cursor")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        last_error = None
        for attempt in range(1, NOTION_MAX_RETRIES + 1):
            request = urllib.request.Request(
                NOTION_API_BASE + path,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code < 500 and e.code != 429:
                    logger.exception("Notion API request failed: %s %s", method, path)
                    raise
            except Exception as e:
                last_error = e

            if attempt < NOTION_MAX_RETRIES:
                delay = NOTION_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Notion API request failed, retrying in %.1fs (%d/%d): %s %s: %s",
                    delay,
                    attempt,
                    NOTION_MAX_RETRIES,
                    method,
                    path,
                    last_error,
                )
                time.sleep(delay)

        logger.error("Notion API request failed after retries: %s %s: %s", method, path, last_error)
        raise last_error


def _compact_properties(properties: dict) -> dict:
    return {key: value for key, value in properties.items() if value is not None}


def _team_member_from_page(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "name": _prop_title(props.get("Name")),
        "telegram_username": _prop_text(props.get("Telegram username")),
        "telegram_user_id": _prop_text(props.get("Telegram user_id")),
    }


def _contact_from_page(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id": page.get("id", ""),
        "url": page.get("url", ""),
        "name": _prop_title(props.get("Name")),
        "contact": _prop_text(props.get("Контакт")),
        "status": _prop_status(props.get("Status")),
        "segments": _prop_multi_select(props.get("Segment")),
        "source": _prop_select(props.get("Источник")),
        "last_touch": _prop_date(props.get("Последнее касание")),
    }


def _is_on_date(value: str, expected: date) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() == expected
    except ValueError:
        return False


def _contact_funnel(contacts) -> dict:
    counts = {status: 0 for status in CONTACT_STATUS_ORDER}
    total = 0
    for contact in contacts:
        total += 1
        status = contact.get("status") or ""
        if status in counts:
            counts[status] += 1

    agreed = sum(counts[status] for status in AGREED_STATUSES)
    interviews = sum(counts[status] for status in INTERVIEW_STATUSES)
    return {
        "total": total,
        "statuses": counts,
        "agreed": agreed,
        "interviews": interviews,
        "agreement_conversion": _percent(agreed, total),
        "interview_conversion": _percent(interviews, total),
        "attendance": _percent(interviews, agreed),
    }


def _percent(numerator: int, denominator: int) -> int:
    return round(numerator / denominator * 100) if denominator else 0


def _interview_from_page(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "name": _prop_title(props.get("Name")),
        "status": _prop_status(props.get("Status")),
        "segment": _prop_multi_select(props.get("Segment")),
        "meeting_date": _prop_date(props.get("Meeting Date")),
        "transcript": _prop_url(props.get("Transcript")),
        "summary": _prop_text(props.get("Summary")),
        "goal": _prop_text(props.get("Goal")) or _prop_text(props.get("goal")),
        "telegra_ph_report": _prop_url(props.get("Telegra.ph report")),
        "interviewer_feedback": _prop_url(props.get(INTERVIEWER_FEEDBACK_PROPERTY)),
    }


def _title(value) -> dict:
    text = _truncate(_text(value) or "-")
    return {"title": [{"type": "text", "text": {"content": text}}]}


def _prop_title(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get("title", [])).strip()


def _prop_text(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get("rich_text", [])).strip()


def _prop_status(prop: dict | None) -> str:
    if not prop:
        return ""
    status = prop.get("status") or {}
    return status.get("name", "")


def _prop_multi_select(prop: dict | None) -> list[str]:
    if not prop:
        return []
    return [item.get("name", "") for item in prop.get("multi_select", []) if item.get("name")]


def _prop_date(prop: dict | None) -> str:
    if not prop:
        return ""
    value = prop.get("date") or {}
    return value.get("start", "")


def _prop_url(prop: dict | None) -> str:
    if not prop:
        return ""
    return prop.get("url") or ""


def _prop_relation(prop: dict | None) -> list[str]:
    if not prop:
        return []
    return [item.get("id", "") for item in prop.get("relation", []) if item.get("id")]


def _prop_select(prop: dict | None) -> str:
    if not prop:
        return ""
    value = prop.get("select") or prop.get("status") or {}
    return value.get("name", "")


def _prop_as_text(prop: dict | None) -> str:
    if not prop:
        return ""
    prop_type = prop.get("type")
    if prop_type == "title":
        return _prop_title(prop)
    if prop_type == "rich_text":
        return _prop_text(prop)
    if prop_type in {"select", "status"}:
        return _prop_select(prop)
    if prop_type == "multi_select":
        return ", ".join(item.get("name", "") for item in prop.get("multi_select", []) if item.get("name"))
    if prop_type == "url":
        return _prop_url(prop)
    if prop_type == "relation":
        return ", ".join(_prop_relation(prop))
    return ""


def _goal_property_name(props: dict) -> str:
    if "Goal" in props:
        return "Goal"
    if "goal" in props:
        return "goal"
    return ""


def _ensure_interviewer_feedback_property(client) -> None:
    db_id = os.getenv("NOTION_INTERVIEWS_DB_ID")
    if not db_id:
        raise RuntimeError("NOTION_INTERVIEWS_DB_ID environment variable is not set.")
    database = client._request("GET", f"/databases/{db_id}")
    if INTERVIEWER_FEEDBACK_PROPERTY in database.get("properties", {}):
        return
    client._request(
        "PATCH",
        f"/databases/{db_id}",
        {"properties": {INTERVIEWER_FEEDBACK_PROPERTY: {"url": {}}}},
    )


def _rich_text(value) -> dict | None:
    text = _truncate(_text(value), 1800)
    if not text:
        return None
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _select(value) -> dict | None:
    text = _text(value).strip().lower()
    if not text or text == "нет данных":
        return None
    return {"select": {"name": text}}


def _relation(page_id: str) -> dict:
    return {"relation": [{"id": page_id}]}


def _relation_many(page_ids: list[str]) -> dict:
    unique_ids = []
    for page_id in page_ids:
        if page_id and page_id not in unique_ids:
            unique_ids.append(page_id)
    return {"relation": [{"id": page_id} for page_id in unique_ids]}


def _map_segment(segment: str) -> str | None:
    normalized = segment.lower()
    if "репетитор" in normalized:
        return "Репетитор"
    if "метод" in normalized:
        return "Методист"
    if "директор" in normalized or "admin" in normalized:
        return "Директор школы"
    if "ib" in normalized:
        return "Учитель в IB школе"
    if "школ" in normalized or "учител" in normalized:
        return "Учитель в Российской школе"
    return None


def _join_parts(parts: list[str]) -> str:
    return "\n\n".join(part for part in parts if part)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(item) for item in value if _text(item))
    return str(value).strip()


def _normalize_username(username: str | None) -> str:
    value = _text(username)
    if value.startswith("@"):
        value = value[1:]
    return value.lower()


def _truncate(value: str, limit: int = 1900) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
