"""Read-only reconciliation of organization spend from the OpenAI Cost API."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone


_COSTS_URL = "https://api.openai.com/v1/organization/costs"


def today_cost() -> dict | None:
    """Return the organization-wide actual OpenAI spend since UTC midnight.

    This cannot identify one research job: OpenAI's Cost API aggregates billing
    by time bucket. It is deliberately kept separate from the per-job estimate.
    """
    admin_key = os.getenv("OPENAI_ADMIN_API_KEY", "").strip()
    if not admin_key:
        return None
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    params = urllib.parse.urlencode({
        "start_time": int(start.timestamp()),
        "bucket_width": "1d",
        "limit": 1,
    })
    request = urllib.request.Request(
        f"{_COSTS_URL}?{params}",
        headers={"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    amount = 0.0
    currency = "usd"
    for bucket in payload.get("data", []):
        for result in bucket.get("results", []):
            value = (result.get("amount") or {})
            amount += float(value.get("value", 0) or 0)
            currency = str(value.get("currency") or currency).lower()
    return {"amount": amount, "currency": currency, "period": "today_utc"}
