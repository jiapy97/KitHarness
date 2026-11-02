import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from autoharness.lib import live_accounting as book


def state(used=100):
    return {"used_quota": used, "quota_per_unit": 500000, "display_currency": "CNY", "unlimited_key": True}


def grant(tmp_path):
    path = tmp_path / "grant.json"
    book.create(path, authorization="Sonnet 4.6，总预算 10 美元", baseline=state(), provider_identity={"provider": "fixed"})
    return path


def reserve(path, call_id="one", **kw):
    return book.reserve(path, call_id=call_id, stage="warmup", condition="original", allowance_usd=0.1,
                        prompt_sha256="public-hash", provider_identity={"provider": "fixed"}, current=state(), **kw)


def test_explicit_grant_never_overwrites_previous_session_history(tmp_path):
    path = grant(tmp_path)
    with pytest.raises(FileExistsError):
        book.create(path, authorization="retry", baseline=state(), provider_identity={})


def test_reservation_is_durable_before_launch_and_blocks_unreconciled_calls(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    with pytest.raises(ValueError, match="unfinished"):
        reserve(path, "second")
    assert json.loads(path.read_text())["reservations"][0]["status"] == "reserved"


def test_same_call_cannot_be_charged_twice_even_after_success(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0.01, current=state(5100), successful=True, receipt_sha256="receipt")
    with pytest.raises(ValueError):
        reserve(path)
    data = book.summary(path)
    assert data["cli_sessions"] == 1 and data["relay_normalized_units"] == 0.01
    assert data["cli_estimated_usd"] == 0.01


def test_concurrent_callers_cannot_bypass_single_inflight_rule(tmp_path):
    path = grant(tmp_path)

    def attempt(i):
        try:
            reserve(path, str(i))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 1


@pytest.mark.parametrize("cost", [None, float("nan"), -1])
def test_missing_or_invalid_costs_halt_without_claiming_zero_usage(tmp_path, cost):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=cost, current=state(120), successful=True, receipt_sha256="receipt")
    result = book.summary(path)
    assert result["halt_reason"] == "missing_model_cost_receipt" and result["cost_receipt_coverage"] == 0
    with pytest.raises(ValueError, match="halted"):
        reserve(path, "second")


def test_failed_sessions_keep_allocations_and_stop_further_calls(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0, current=state(), successful=False, receipt_sha256="receipt")
    data = json.loads(path.read_text())
    assert data["reservations"][0]["allowance_usd"] == 0.1
    assert data["halt_reason"] == "model_or_protocol_failure"


@pytest.mark.parametrize("change", [{"used_quota": 99}, {"quota_per_unit": 42}])
def test_quota_counter_reset_or_currency_unit_changes_fail_closed(tmp_path, change):
    path = grant(tmp_path)
    data = json.loads(path.read_text())
    with pytest.raises(ValueError):
        book.relative_quota(data, {**state(), **change})


def test_cli_underreporting_does_not_bypass_relay_consumption_guard(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0.001, current=state(4_000_100), successful=True, receipt_sha256="receipt")
    assert book.summary(path)["halt_reason"] == "relay_consumption_guard"


def test_allowance_overshoot_halts_even_below_total_budget(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0.11, current=state(), successful=True, receipt_sha256="receipt")
    assert book.summary(path)["halt_reason"] == "CLI_estimated_cost_guard"


def test_cumulative_reserved_budget_is_not_recycled(tmp_path):
    path = grant(tmp_path)
    data = json.loads(path.read_text())
    data["reservation_limit_usd"] = 0.15
    book.write(path, data)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0.01, current=state(), successful=True, receipt_sha256="receipt")
    with pytest.raises(ValueError, match="budget"):
        reserve(path, "second")


def test_documented_protocol_repair_does_not_refund_failed_charge(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0, current=state(), successful=False, receipt_sha256="error")
    book.continue_after_fix(path, reason="restore native CLI default system prompt")
    data = json.loads(path.read_text())
    assert data["halt_reason"] is None and len(data["reservations"]) == 1
    assert data["reservations"][0]["allowance_usd"] == 0.1
    reserve(path, "second")
    assert book.summary(path)["cli_sessions"] == 2


def test_cost_guard_cannot_be_cleared_as_a_protocol_repair(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0.5, current=state(), successful=True, receipt_sha256="cost")
    with pytest.raises(ValueError):
        book.continue_after_fix(path, reason="try again")


def test_explicit_deepseek_fallback_keeps_previous_failures_and_budget(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=0, current=state(), successful=False, receipt_sha256="error")
    baseline = {"meter_kind": "balance", "balances": {"CNY": 20}, "used_quota": 0, "quota_per_unit": 1, "display_currency": "CNY"}
    data = book.switch_to_deepseek(path, baseline=baseline, provider_identity={"provider": "deepseek"}, authorization="user requests local DeepSeek fallback")
    assert data["model"] == "deepseek-flash" and data["approved_budget_usd"] == 10
    assert len(data["reservations"]) == 1 and data["reservations"][0]["allowance_usd"] == 0.1
    assert book.relative_quota(data, {**baseline, "balances": {"CNY": 19.98}}) == pytest.approx(.02)


def test_deepseek_cost_estimate_uses_published_peak_rates():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert book.deepseek_peak_estimate(usage) == pytest.approx(1.506)
    assert book.deepseek_peak_estimate({"output_tokens": 1}) is None


def test_completed_experiment_cannot_spend_unused_capacity(tmp_path):
    path = grant(tmp_path)
    reserve(path)
    book.finish(path, call_id="one", estimated_usd=.01, current=state(), successful=True, receipt_sha256="receipt")
    book.close(path, evidence_sha256="verified-evidence")
    assert book.summary(path)["grant_status"] == "completed"
    with pytest.raises(ValueError, match="cannot be reused"):
        reserve(path, "unrelated-extra-call")
