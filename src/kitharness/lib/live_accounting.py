"""Shared, durable API experiment accounting. No credential persistence or inference."""
import json
import math
import time
import urllib.request
from pathlib import Path

from kitharness.lib import atomic, live_auth, lock

MODEL = "claude-sonnet-4-6"
SUPPORTED_MODELS = (MODEL, "deepseek-flash")
STAGES = ("pilot", "warmup", "reflector", "curator", "evaluation")


def write(path, data):
    atomic.write_text(Path(path), json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n")


def number(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def _get(config, path):
    headers = {"Accept": "application/json", "User-Agent": "claude-cli/2.1.312 (external, cli)",
               "Authorization": "Bearer " + config.env[config.credential_key]}
    base = "https://api.deepseek.com" if config.source == "deepseek_project" else config.base_url
    request = urllib.request.Request(base + path, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), live_auth._NoRedirect())
    fetch = opener.open
    try:
        with fetch(request, timeout=10) as response:
            raw = response.read(live_auth.MAX_BYTES + 1)
        if len(raw) > live_auth.MAX_BYTES:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        if config.source == "deepseek_project":
            return data
        if not isinstance(data.get("data"), dict):
            raise ValueError
        return data["data"]
    except (OSError, ValueError):
        raise ValueError("relay accounting endpoint unavailable; no further model calls") from None


def relay_state(config):
    """Only quota and public unit metadata; never return token names/account data."""
    if config.source == "deepseek_project":
        data = _get(config, "/user/balance")
        balances = {}
        for row in data.get("balance_infos", []):
            if row.get("currency") in ("CNY", "USD"):
                try:
                    value = float(row["total_balance"])
                except (ValueError, TypeError, KeyError):
                    raise ValueError("invalid DeepSeek balance response") from None
                if not number(value):
                    raise ValueError("invalid DeepSeek balance")
                balances[row["currency"]] = value
        if data.get("is_available") is not True or not balances:
            raise ValueError("DeepSeek account is not available")
        return {"meter_kind": "balance", "balances": balances, "used_quota": 0, "quota_per_unit": 1,
                "display_currency": next(iter(balances)) if len(balances) == 1 else "mixed", "unlimited_key": False}
    usage = _get(config, "/api/usage/token/")
    status = _get(config, "/api/status")
    used, unit = usage.get("total_used"), status.get("quota_per_unit")
    if not number(used) or not number(unit, positive=True):
        raise ValueError("invalid relay quota counters")
    return {"used_quota": used, "quota_per_unit": unit,
            "display_currency": status.get("quota_display_type") if status.get("quota_display_type") in ("USD", "CNY") else "unknown",
            "display_exchange_rate": status.get("usd_exchange_rate") if number(status.get("usd_exchange_rate"), positive=True) else None,
            "unlimited_key": usage.get("unlimited_quota") is True}


def create(path, *, authorization, baseline, provider_identity, max_sessions=48):
    """Only call after explicit user authorization; never overwrite a grant."""
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        if path.exists():
            raise FileExistsError("API grant already exists")
        if not authorization or not number(baseline.get("used_quota")) or not number(baseline.get("quota_per_unit"), positive=True):
            raise ValueError("explicit authorization and provider counters required")
        data = {"version": 1, "model": MODEL, "billing_mode": "api_relay", "authorization": authorization,
                "approved_budget_usd": 10.0, "reservation_limit_usd": 9.0,
                "relay_normalized_unit_limit": 8.0, "max_sessions": max_sessions,
                "provider_identity": provider_identity, "baseline": baseline,
                "reservations": [], "halt_reason": None}
        write(path, data)
        return data


def _validate(data):
    if (data.get("model") not in SUPPORTED_MODELS or data.get("billing_mode") != "api_relay"
            or data.get("approved_budget_usd") != 10.0 or not data.get("authorization")
            or not isinstance(data.get("reservations"), list)):
        raise ValueError("invalid explicit API grant")
    if data.get("status") == "completed":
        raise ValueError("completed API grant cannot be reused")
    if data.get("halt_reason"):
        raise ValueError("experiment halted: " + data["halt_reason"])


def relative_quota(data, current):
    baseline = data["baseline"]
    carried = data.get("carried_relay_units", 0)
    if baseline.get("meter_kind") == "balance":
        if current.get("meter_kind") != "balance" or set(current.get("balances", {})) != set(baseline["balances"]):
            raise ValueError("DeepSeek balance currencies changed")
        deltas = [baseline["balances"][key] - current["balances"][key] for key in baseline["balances"]]
        if any(delta < -1e-8 for delta in deltas):
            raise ValueError("account balance increased; explicit meter reconciliation required")
        return carried + max(0, sum(deltas))
    if (current.get("quota_per_unit") != baseline["quota_per_unit"]
            or not number(current.get("used_quota")) or current["used_quota"] < baseline["used_quota"]):
        raise ValueError("relay accounting units changed or counter went backwards")
    return carried + (current["used_quota"] - baseline["used_quota"]) / baseline["quota_per_unit"]


def reserve(path, *, call_id, stage, condition, allowance_usd, prompt_sha256, provider_identity, current):
    """Charge before launch; failed attempts keep both their slot and allocation."""
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        data = json.loads(path.read_text(encoding="utf-8"))
        _validate(data)
        rows = data["reservations"]
        if provider_identity != data["provider_identity"] or stage not in STAGES or condition not in ("no_learning", "original", "kitharness"):
            raise ValueError("unexpected provider, stage or condition")
        if (not number(allowance_usd, positive=True) or len(rows) >= data["max_sessions"]
                or any(row["call_id"] == call_id for row in rows)
                or sum(row["allowance_usd"] for row in rows) + allowance_usd > data["reservation_limit_usd"] + 1e-9):
            raise ValueError("API session/allocation budget exhausted or duplicate call")
        if any(row["status"] == "reserved" for row in rows):
            raise ValueError("unfinished model reservation; reconcile before any further calls")
        quota = relative_quota(data, current)
        if quota >= data["relay_normalized_unit_limit"]:
            raise ValueError("relay consumption limit reached")
        row = {"number": len(rows) + 1, "call_id": call_id, "stage": stage, "condition": condition,
               "model": data["model"], "allowance_usd": allowance_usd, "prompt_sha256": prompt_sha256,
               "relay_units_before": quota, "reserved_at_unix": time.time(), "status": "reserved"}
        rows.append(row)
        write(path, data)
        return dict(row)


def finish(path, *, call_id, estimated_usd, current, successful, receipt_sha256, estimate_basis="native_cli"):
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        data = json.loads(path.read_text(encoding="utf-8"))
        _validate(data)
        row = next((item for item in data["reservations"] if item["call_id"] == call_id), None)
        if row is None or row["status"] != "reserved":
            raise ValueError("unknown or already reconciled model call")
        quota = relative_quota(data, current)
        row.update(status="completed" if successful else "failed", estimated_usd=estimated_usd if number(estimated_usd) else None,
                   relay_units_after=quota, relay_units_delta=quota - row["relay_units_before"], receipt_sha256=receipt_sha256,
                   estimate_basis=estimate_basis)
        total = sum(item.get("estimated_usd") or 0 for item in data["reservations"])
        if not number(estimated_usd):
            data["halt_reason"] = "missing_model_cost_receipt"
        elif estimated_usd > row["allowance_usd"] or total >= data["reservation_limit_usd"]:
            data["halt_reason"] = "CLI_estimated_cost_guard"
        elif quota >= data["relay_normalized_unit_limit"]:
            data["halt_reason"] = "relay_consumption_guard"
        elif not successful:
            data["halt_reason"] = "model_or_protocol_failure"
        write(path, data)
        return data


def summary(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["reservations"]
    known = [row["estimated_usd"] for row in rows if number(row.get("estimated_usd"))]
    native = [row["estimated_usd"] for row in rows if number(row.get("estimated_usd")) and row.get("estimate_basis", "native_cli") == "native_cli"]
    return {"model": data["model"], "approved_budget_usd": data["approved_budget_usd"],
            "grant_status": data.get("status", "active"),
            "cli_sessions": len(rows), "cli_estimated_usd": sum(native) if native else None,
            "accounted_estimated_usd": sum(known) if known else None,
            "cost_receipt_coverage": len(known), "relay_normalized_units": max((r.get("relay_units_after", r["relay_units_before"]) for r in rows), default=0),
            "relay_display_currency": data["baseline"]["display_currency"],
            "relay_attribution": "shared key; consumption includes any concurrent external usage",
            "stages": {stage: {"sessions": sum(row["stage"] == stage for row in rows),
                               "estimated_usd": sum(row.get("estimated_usd") or 0 for row in rows if row["stage"] == stage)} for stage in STAGES},
            "halt_reason": data["halt_reason"],
            "limit_scope": "controller allocations, CLI estimated-cost guard and post-request relay counters; not a provider-side hard credit limit"}


def continue_after_fix(path, *, reason):
    """A documented implementation repair, with all failed charges retained.

Never clears cost/unknown-usage guards, changes the grant, or refunds a slot.
"""
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["reservations"]
        if (not reason or data.get("halt_reason") != "model_or_protocol_failure"
                or any(row["status"] == "reserved" or not number(row.get("estimated_usd")) for row in rows)):
            raise ValueError("this halt cannot be released by an implementation fix")
        data.setdefault("repairs", []).append({"reason": reason, "after_sessions": len(rows), "at_unix": time.time()})
        data["halt_reason"] = None
        write(path, data)
        return data


def switch_to_deepseek(path, *, baseline, provider_identity, authorization):
    """Explicit user fallback; preserve all previous sessions and allocations."""
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not authorization or data.get("halt_reason") != "model_or_protocol_failure"
                or any(row["status"] == "reserved" or not number(row.get("estimated_usd")) for row in data["reservations"])
                or baseline.get("meter_kind") != "balance"):
            raise ValueError("cannot rebind this budget to DeepSeek")
        carry = max((r.get("relay_units_after", 0) for r in data["reservations"]), default=0)
        data.setdefault("provider_segments", []).append({"model": data["model"], "provider_identity": data["provider_identity"],
                                                         "relay_units_at_switch": carry, "sessions_at_switch": len(data["reservations"])})
        data.update(model="deepseek-flash", provider_identity=provider_identity, baseline=baseline,
                    carried_relay_units=carry, halt_reason=None, fallback_authorization=authorization)
        write(path, data)
        return data


def deepseek_peak_estimate(usage):
    """Conservative USD estimate at published peak Flash rates, 2026-11-02.

Official prices per 1M: uncached input .30, cached input .006, output 1.20.
This deliberately does not assume the 50% off-peak discount.
"""
    fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    if not isinstance(usage, dict) or any(type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0 for key in fields):
        return None
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    return ((usage["input_tokens"] + usage.get("cache_creation_input_tokens", 0)) * .30
            + usage.get("cache_read_input_tokens", 0) * .006 + usage["output_tokens"] * 1.20) / 1_000_000


def close(path, *, evidence_sha256):
    """Seal a finished experiment; unused capacity is not permission for new work."""
    path = Path(path)
    with lock.file_lock(path.with_suffix(".lock")):
        data = json.loads(path.read_text(encoding="utf-8"))
        _validate(data)
        if (not evidence_sha256 or not data["reservations"]
                or any(row["status"] == "reserved" for row in data["reservations"])):
            raise ValueError("cannot close an unfinished experiment")
        data.update(status="completed", completed_at_unix=time.time(), evidence_sha256=evidence_sha256)
        write(path, data)
        return data
