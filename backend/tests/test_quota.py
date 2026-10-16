from sashmo.quota import normalize_quota


def test_only_reported_weekly_window_is_visible():
    account = {"account": {"type": "chatgpt", "planType": "example-plan"}}
    response = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 46, "windowDurationMins": 10080, "resetsAt": 1700000000},
                "secondary": None,
            }
        },
        "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 99}},
        "rateLimitResetCredits": {"availableCount": 3},
    }
    result = normalize_quota(account, response)
    assert result["plan_reported"] == "example-plan"
    assert result["reset_credits_available"] == 3
    assert result["buckets"][0]["windows"] == [{
        "slot": "primary",
        "remaining_percent": 54,
        "duration_mins": 10080,
        "resets_at_local": result["buckets"][0]["windows"][0]["resets_at_local"],
    }]
    assert len(result["buckets"][0]["windows"]) == 1


def test_legacy_fallback_and_missing_fields():
    result = normalize_quota({}, {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 120}}})
    assert result["buckets"][0]["windows"][0]["remaining_percent"] == 0
    assert result["buckets"][0]["windows"][0]["resets_at_local"] is None
    assert result["reset_credits_available"] is None


def test_missing_windows_are_not_fabricated():
    result = normalize_quota({}, {"rateLimitsByLimitId": {"codex": {"primary": None, "secondary": None}}})
    assert result["buckets"][0]["windows"] == []
