from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.temporal import BEIJING

DAY = "2026-10-29"
NOW = datetime(2026, 10, 29, 12, tzinfo=BEIJING).timestamp()


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "usage.sqlite3", clock=lambda: NOW)
    yield result
    result.close()


def reserve(store, scope="qq:g1", day=DAY, **kwargs):
    options = dict(
        persona_id="cocoa",
        provider_id="provider",
        purpose="interaction",
        estimated_input=100,
        output_limit=50,
        global_calls=20,
        group_calls=10,
        global_tokens=10000,
        group_tokens=5000,
    )
    options.update(kwargs)
    return store.reserve_learning(day, scope, **options)


def test_reservation_is_persistent_and_prior_call_budgets_are_respected(store):
    assert store.reserve_call_budget(DAY, "qq:g1", 20, 10)
    assert reserve(store, group_calls=1) is None
    assert store.budget_used(DAY, "*") == 1
    assert store.token_budget_used(DAY) == 0
    report = store.usage_report(DAY)
    assert report["today"]["calls"] == report["today"]["legacy_calls"] == 1
    assert report["today"]["unknown_calls"] == 1
    assert report["today"]["reported_tokens"] == 0
    assert report["by_group"][0]["legacy_calls"] == 1


def test_global_and_group_tokens_are_reserved_together_or_not_at_all(store):
    first = reserve(store, global_tokens=200)
    assert first
    assert reserve(store, scope="qq:g2", global_tokens=200) is None
    assert store.budget_used(DAY, "qq:g2") == 0
    assert store.token_budget_used(DAY, "qq:g2") == 0
    assert store.token_budget_used(DAY) == 150


def test_only_never_started_reservation_can_be_released(store):
    first = reserve(store)
    assert store.release_learning(first)
    assert not store.release_learning(first)
    assert store.budget_used(DAY, "*") == store.token_budget_used(DAY) == 0
    second = reserve(store)
    assert store.begin_learning(second)
    assert not store.begin_learning(second)
    assert not store.release_learning(second)
    assert store.budget_used(DAY, "*") == 1


def test_reported_success_reconciles_tokens_and_duplicate_settlement_is_ignored(store):
    first = reserve(store)
    store.begin_learning(first)
    assert store.settle_learning(
        first, "success", {"input_tokens": 20, "output_tokens": 10}, accepted=2
    )
    assert not store.settle_learning(first, "success", {"total_tokens": 0})
    report = store.usage_report(DAY)
    assert report["today"]["reported_tokens"] == report["today"]["budget_tokens"] == 30
    assert report["today"]["accepted"] == 2
    assert report["today"]["estimated_tokens"] == report["today"]["pending_calls"] == 0


@pytest.mark.parametrize("status", ["success", "error", "cancelled", "discarded"])
def test_missing_usage_preserves_conservative_occupation(store, status):
    reservation = reserve(store)
    store.begin_learning(reservation)
    store.settle_learning(reservation, status)
    report = store.usage_report(DAY)
    assert (
        report["today"]["budget_tokens"] == report["today"]["estimated_tokens"] == 150
    )
    assert report["today"]["missing_usage_calls"] == 1
    assert report["recent"][0]["total_tokens"] is None


def test_zero_reported_usage_differs_from_missing(store):
    reservation = reserve(store)
    store.begin_learning(reservation)
    store.settle_learning(
        reservation, "success", {"input_tokens": 0, "output_tokens": 0}
    )
    report = store.usage_report(DAY)
    assert report["today"]["reported_calls"] == 1
    assert (
        report["today"]["budget_tokens"] == report["today"]["missing_usage_calls"] == 0
    )
    assert report["recent"][0]["total_tokens"] == 0


def test_provider_ignoring_output_cap_cannot_hide_actual_overrun(store):
    reservation = reserve(store, global_tokens=200)
    store.begin_learning(reservation)
    store.settle_learning(
        reservation, "success", {"input_tokens": 100, "output_tokens": 300}
    )
    assert store.token_budget_used(DAY) == 400
    assert reserve(store, global_tokens=200) is None


def test_pending_survives_reopen_as_unknown_charge(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    original = Store(path, clock=lambda: NOW)
    reserved = reserve(original)
    original.begin_learning(reserved)
    original.close()
    reopened = Store(path, clock=lambda: NOW)
    try:
        report = reopened.usage_report(DAY)
        assert report["today"]["pending_calls"] == report["today"]["unknown_calls"] == 1
        assert report["today"]["reserved_tokens"] == 150
        assert reserve(reopened, global_tokens=150) is None
    finally:
        reopened.close()


def test_two_connections_cannot_overreserve_either_counter(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    stores = [Store(path) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda n: reserve(
                        stores[n % 2], scope=f"g{n % 2}", global_tokens=450
                    ),
                    range(30),
                )
            )
        assert sum(value is not None for value in results) == 3
        assert stores[0].budget_used(DAY, "*") == 3
        assert stores[0].token_budget_used(DAY) == 450
    finally:
        for item in stores:
            item.close()


def test_report_filters_scope_and_keeps_global_budget_visible(store):
    for scope, tokens in [("g1", 20), ("g2", 40)]:
        reservation = reserve(store, scope=scope)
        store.begin_learning(reservation)
        store.settle_learning(reservation, "success", {"total_tokens": tokens})
    report = store.usage_report(DAY, scope_key="g1")
    assert report["today"]["reported_tokens"] == 20
    assert report["global_token_budget_used"] == 60
    assert {row["scope_key"] for row in report["recent"]} == {"g1"}


def test_metadata_does_not_store_prompt_or_raw_error_text(store):
    reservation = reserve(
        store,
        metadata={
            "sample_count": 3,
            "phase": "low_price",
            "guidance_hash": "ab" * 32,
            "prompt": "PRIVATE_MESSAGE",
            "response": "PRIVATE_RESPONSE",
        },
    )
    store.begin_learning(reservation)
    store.settle_learning(
        reservation,
        "error",
        error_type="Secret key: 123",
        metadata={"schedule_reason": "low_window", "raw": "SECRET"},
    )
    row = store.usage_report(DAY)["recent"][0]
    assert row["error_type"] == ""
    assert row["metadata"] == {
        "sample_count": 3,
        "phase": "low_price",
        "guidance_hash": "ab" * 32,
        "schedule_reason": "low_window",
    }
    assert "PRIVATE" not in str(row)


def test_activity_excludes_future_and_forgotten_sources(store):
    for index, age in enumerate((100, 400, -100)):
        source = dict(
            id=str(index),
            persona_id="cocoa",
            platform_id="qq",
            bot_id="999",
            group_id="g1",
            user_id=str(1000 + index),
            text="source",
            scope_key="g1",
            purpose="observation",
            sent_at=NOW - age,
        )
        store.add_source(source)
    store.forget("cocoa", "1001")
    activity = store.activity("g1", window_seconds=600, now=NOW)
    assert activity == {
        "recent_count": 1,
        "last_sent_at": NOW - 100,
        "pending_count": 1,
        "oldest_pending_at": NOW - 100,
    }


def test_config_invalidation_persists_new_epoch(store):
    before = store.epoch()
    assert store.invalidate() == before + 1
    assert store.epoch() == before + 1


def test_trusted_final_guard_can_release_begun_but_never_sent_work(store):
    reservation = reserve(store)
    store.begin_learning(reservation)
    assert not store.release_learning(reservation)
    assert store.release_learning(reservation, not_sent=True)
    assert not store.release_learning(reservation, not_sent=True)
    assert store.budget_used(DAY, "*") == store.token_budget_used(DAY) == 0
    row = store.usage_report(DAY)["recent"][0]
    assert row["status"] == "released"
    assert row["total_tokens"] is None
    assert not store.settle_learning(reservation, "success", {"total_tokens": 100})


def test_group_report_is_today_while_trend_and_totals_cover_range(store):
    for day, scope, total in [
        ("2026-10-28", "g1", 100),
        ("2026-10-28", "g2", 200),
        (DAY, "g1", 30),
    ]:
        reservation = reserve(store, scope=scope, day=day)
        store.begin_learning(reservation)
        store.settle_learning(reservation, "success", {"total_tokens": total})
    report = store.usage_report(DAY, days=7)
    assert report["totals"]["reported_tokens"] == 330
    assert report["today"]["reported_tokens"] == 30
    assert sum(row["reported_tokens"] for row in report["by_day"]) == 330
    assert [
        (row["scope_key"], row["reported_tokens"], row["calls"])
        for row in report["by_group"]
    ] == [("g1", 30, 1)]
