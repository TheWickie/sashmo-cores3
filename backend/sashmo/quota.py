"""Normalize Codex App Server usage without inventing missing limits."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any


def _reset_local(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _window(slot: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not isfinite(used):
        return None
    duration = value.get("windowDurationMins")
    return {
        "slot": slot,
        "remaining_percent": round(max(0.0, min(100.0, 100.0 - used))),
        "duration_mins": duration if isinstance(duration, int) and duration > 0 else None,
        "resets_at_local": _reset_local(value.get("resetsAt")),
    }


def normalize_quota(account: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Return actual reported buckets, with the codex bucket first when present."""
    by_id = response.get("rateLimitsByLimitId")
    snapshots: list[tuple[str, dict[str, Any]]] = []
    if isinstance(by_id, dict) and by_id:
        for limit_id, snapshot in by_id.items():
            if isinstance(snapshot, dict):
                snapshots.append((str(limit_id), snapshot))
    elif isinstance(response.get("rateLimits"), dict):
        legacy = response["rateLimits"]
        snapshots.append((legacy.get("limitId") or "codex", legacy))
    snapshots.sort(key=lambda item: (item[0] != "codex", item[0]))

    buckets = []
    for limit_id, snapshot in snapshots:
        windows = [w for slot in ("primary", "secondary") if (w := _window(slot, snapshot.get(slot)))]
        buckets.append({
            "id": limit_id,
            "name": snapshot.get("limitName") if isinstance(snapshot.get("limitName"), str) else None,
            "windows": windows,
            "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
        })

    account_record = account.get("account") if isinstance(account.get("account"), dict) else {}
    plan = account_record.get("planType") or next(
        (snap.get("planType") for _, snap in snapshots if snap.get("planType")), None
    )
    reset_credits = response.get("rateLimitResetCredits")
    reset_count = reset_credits.get("availableCount") if isinstance(reset_credits, dict) else None
    if not isinstance(reset_count, int) or reset_count < 0:
        reset_count = None

    return {
        "plan_reported": plan,
        "account_type": account_record.get("type"),
        "primary_bucket_id": "codex" if any(b["id"] == "codex" for b in buckets) else None,
        "buckets": buckets,
        "reset_credits_available": reset_count,
        "ordinary_usage_allowed": response.get("ordinaryUsageAllowed"),
    }
