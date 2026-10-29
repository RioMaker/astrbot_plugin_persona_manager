from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.temporal import BEIJING


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING).timestamp()


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "memory.sqlite3", clock=lambda: ts("2026-10-29T12:00"))
    yield result
    result.close()


def source(
    identity="m1",
    *,
    user="123",
    group="g1",
    purpose="interaction",
    sent="2026-10-28T10:00",
    text="我想练幽玄，牢谱",
):
    return dict(
        id=identity,
        persona_id="cocoa",
        platform_id="qq1",
        bot_id="999",
        group_id=group,
        user_id=user,
        text=text,
        sent_at=ts(sent),
        purpose=purpose,
        scope_key=f"qq1:999:{group}",
        umo=f"qq1:GroupMessage:{group}",
    )


def proposal(identity="m1", content="在练幽玄", **kwargs):
    return dict(
        source_ids=[identity],
        kind="goal",
        key="练习目标",
        content=content,
        state="in_progress",
        **kwargs,
    )


def test_global_person_identity_ignores_model_user_and_crosses_groups(store):
    store.add_source(source())
    store.apply_memories([proposal(user_id="456", persona_id="evil")], store.epoch())
    store.add_source(
        source("m2", group="g2", text="我想改练万戈", sent="2026-10-29T10:00")
    )
    store.apply_memories([proposal("m2", "在练万戈")], store.epoch())
    assert store.profile("cocoa", "123")[0]["content"] == "在练万戈"
    assert store.profile("cocoa", "456") == []
    assert store.profile("evil", "123") == []
    assert store.relationship("cocoa", "123")["group_count"] == 2
    assert store.pending_sources()[0]["umo"] == "qq1:GroupMessage:g1"


def test_observation_and_mixed_authors_cannot_become_personal_memory(store):
    store.add_source(source(purpose="observation"))
    assert store.apply_memories([proposal()], store.epoch()) == []
    store.add_source(source("m2"))
    store.add_source(source("m3", user="456"))
    item = proposal("m2")
    item["source_ids"] = ["m2", "m3"]
    assert store.apply_memories([item], store.epoch()) == []


def test_late_history_never_replaces_newer_effective_state(store):
    store.add_source(source("new", sent="2026-10-28T12:00"))
    store.apply_memories([proposal("new", "在练万戈", time_text="今天")], store.epoch())
    store.add_source(source("old", sent="2026-10-20T12:00"))
    store.apply_memories([proposal("old", "在练幽玄", time_text="今天")], store.epoch())
    assert store.profile("cocoa", "123")[0]["content"] == "在练万戈"
    assert store.timeline("cocoa", "123")[0]["event_start"] == ts("2026-10-28")


def test_today_precision_does_not_outrank_later_same_day_assertion(store):
    store.add_source(source("early", sent="2026-10-29T08:00"))
    store.apply_memories([proposal("early", "旧目标")], store.epoch())
    store.add_source(source("late", sent="2026-10-29T10:00"))
    store.apply_memories([proposal("late", "新目标", time_text="今天")], store.epoch())
    assert store.profile("cocoa", "123")[0]["content"] == "新目标"


def test_future_plan_is_visible_as_plan_never_completed(store):
    store.add_source(source())
    item = proposal(time_text="下周")
    item["state"] = "planned"
    store.apply_memories([item], store.epoch())
    result = store.profile("cocoa", "123")[0]
    assert result["state"] == "planned"
    assert result["event_start"] == ts("2026-11-02")
    assert result["valid_from"] == ts("2026-10-28T10:00")


def test_unknown_event_time_remains_unknown(store):
    store.add_source(source())
    store.apply_memories([proposal(time_text="以前")], store.epoch())
    item = store.timeline("cocoa", "123")[0]
    assert item["event_start"] is None
    assert item["precision"] == "unknown"


def test_source_promotion_and_replay_are_idempotent(store):
    assert store.add_source(source(purpose="observation"))
    store.mark_sources(["m1"], store.epoch())
    assert store.add_source(source(purpose="interaction"))
    assert store.pending_sources("interaction")[0]["id"] == "m1"
    assert not store.add_source(source())
    assert len(store.apply_memories([proposal(), proposal()], store.epoch())) == 1


def test_forget_cancels_inflight_work_erases_evidence_and_blocks_replay(store):
    store.add_source(source())
    old_epoch = store.epoch()
    memory_id = store.apply_memories([proposal()], old_epoch)[0]
    assert store.forget("cocoa", "123", memory_id) == 1
    assert store.apply_memories([proposal()], old_epoch) == []
    assert store.apply_memories([proposal()], store.epoch()) == []
    assert not store.add_source(source())
    assert store.get_source("m1")["text"] == ""
    assert "umo" not in store.get_source("m1")
    assert store.get_memory(memory_id) is None
    assert store.relationship("cocoa", "123")["interaction_count"] == 0
    # Newly supplied facts remain possible after forgetting old evidence.
    assert store.add_source(source("new"))


def test_targeted_correction_preserves_history_and_rejects_other_person(store):
    store.add_source(source())
    original = store.apply_memories([proposal()], store.epoch())[0]
    store.add_source(source("other", user="456"))
    assert (
        store.apply_memories([proposal("other", corrects_id=original)], store.epoch())
        == []
    )
    corrected = store.correct(original, "改练万戈")
    assert store.profile("cocoa", "123")[0]["id"] == corrected
    assert store.get_memory(original)["status"] == "corrected"
    assert store.get_memory(corrected)["corrects_id"] == original


def test_evidence_quote_cannot_be_fabricated(store):
    store.add_source(source())
    item = proposal(evidence=[{"source_id": "m1", "quote": "我已十段"}])
    assert store.apply_memories([item], store.epoch()) == []


def test_styles_are_group_scoped_and_require_human_occurrence(store):
    store.add_source(source(purpose="observation"))
    item = dict(
        source_ids=["m1"],
        expression="牢谱",
        meaning="难打的谱",
        contexts="闲聊",
        avoid="求助时",
    )
    style_id = store.apply_styles([item], store.epoch())[0]
    store.set_style_status(style_id, "active")
    assert len(store.styles("qq1:999:g1")) == 1
    assert store.styles("qq1:999:g1")[0]["contexts"] == ["闲聊"]
    assert store.styles("qq1:999:g1")[0]["avoid"] == ["求助时"]
    assert store.styles("qq1:999:g2") == []
    item["expression"] = "凭空编造"
    assert store.apply_styles([item], store.epoch()) == []


def test_automatic_style_activation_requires_authors_days_and_evidence(store):
    for i in range(5):
        store.add_source(
            source(
                str(i),
                user=str(100 + i % 3),
                purpose="observation",
                sent=f"2026-10-{27 + i % 2}T10:00",
            )
        )
    item = dict(
        source_ids=[str(i) for i in range(4)], expression="牢谱", meaning="难打的谱"
    )
    style_id = store.apply_styles([item], store.epoch())[0]
    assert store.auto_activate_styles("qq1:999:g1") == []
    item["source_ids"] = ["4"]
    store.apply_styles([item], store.epoch())
    assert store.auto_activate_styles("qq1:999:g1") == [style_id]
    store.set_style_status(style_id, "disabled")
    assert store.auto_activate_styles("qq1:999:g1") == []


def test_style_rollback_does_not_resurrect_forgotten_content(store):
    store.add_source(source())
    first = store.apply_styles(
        [dict(source_ids=["m1"], expression="牢谱", meaning="难打的谱")], store.epoch()
    )[0]
    version = store.set_style_status(first, "active")
    store.forget("cocoa", "123")
    store.rollback_styles("qq1:999:g1", version)
    assert store.styles("qq1:999:g1") == []
    assert store.get_style(first) is None


def test_core_rollback_is_independent_of_memory_and_other_personas(store):
    version = store.set_core("cocoa", "稳定底稿")
    store.set_core("cocoa", "新底稿")
    store.add_source(source())
    store.apply_memories([proposal()], store.epoch())
    store.rollback_core("cocoa", version)
    assert store.get_core("cocoa") == "稳定底稿"
    assert len(store.profile("cocoa", "123")) == 1
    with pytest.raises(ValueError):
        store.rollback_core("other", version)


def test_budget_is_atomic_across_threads_and_survives_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(
            executor.map(
                lambda _: store.reserve_budget("2026-10-29", "scope", 7), range(40)
            )
        )
    assert sum(outcomes) == 7
    store.close()
    reopened = Store(path)
    assert reopened.budget_used("2026-10-29", "scope") == 7
    assert reopened.reserve_budget("2026-10-30", "scope", 7)
    reopened.close()


def test_retention_expires_raw_text_without_erasing_approved_memory(store):
    store.add_source(source(sent="2026-10-01T10:00"))
    store.apply_memories([proposal()], store.epoch())
    epoch = store.epoch()
    assert store.cleanup() == 1
    assert store.get_source("m1")["text"] == ""
    assert store.pending_sources() == []
    assert len(store.profile("cocoa", "123")) == 1
    assert store.apply_memories([proposal()], epoch) == []


def test_empty_group_and_invalid_source_time_are_rejected(store):
    item = source(group="")
    assert not store.add_source(item)
    item = source()
    item["sent_at"] = float("nan")
    assert not store.add_source(item)


def test_metadata_stats_and_scope_listing(store):
    store.set_meta("paused", True)
    assert store.get_meta("paused") is True
    assert store.get_meta("missing", False) is False
    store.add_source(source())
    assert store.scopes("cocoa")[0]["group_id"] == "g1"
    assert store.stats()["pending_sources"] == 1
    assert store.profile("cocoa", "123", limit=0) == []


def test_correction_can_change_state_without_inventing_a_new_source_date(store):
    store.add_source(source())
    item = proposal()
    item["state"] = "planned"
    original = store.apply_memories([item], store.epoch())[0]
    before = store.get_memory(original)
    corrected = store.correct(original, "已经完成", state="completed")
    after = store.get_memory(corrected)
    assert after["state"] == "completed"
    assert after["source_sent_at"] == before["source_sent_at"]
    assert after["valid_from"] == before["valid_from"]
    with pytest.raises(ValueError):
        store.correct(corrected, "错误状态", state="invented")


def test_correcting_historical_goal_does_not_replace_newer_goal(store):
    store.add_source(source("old", sent="2026-10-20T10:00"))
    old = store.apply_memories([proposal("old", "幽玄")], store.epoch())[0]
    store.add_source(source("new", sent="2026-10-28T10:00"))
    store.apply_memories([proposal("new", "万戈")], store.epoch())
    store.correct(old, "当时其实练的是夏祭")
    assert store.profile("cocoa", "123")[0]["content"] == "万戈"


def test_current_practice_and_future_plan_remain_separate(store):
    store.add_source(source("current", sent="2026-10-28T10:00"))
    store.apply_memories([proposal("current", "目前在练万戈")], store.epoch())
    store.add_source(source("plan", sent="2026-10-29T10:00"))
    planned = proposal("plan", "计划下周练幽玄", time_text="下周")
    planned["state"] = "planned"
    store.apply_memories([planned], store.epoch())
    assert {r["state"]: r["content"] for r in store.profile("cocoa", "123")} == {
        "in_progress": "目前在练万戈",
        "planned": "计划下周练幽玄",
    }
    store.add_source(source("cancel", sent="2026-10-29T11:00"))
    cancelled = proposal("cancel", "取消下周练幽玄的计划", time_text="下周")
    cancelled["state"] = "cancelled"
    store.apply_memories([cancelled], store.epoch())
    # Cancelled plans are known immediately, not hidden until their former date.
    rows = store.profile("cocoa", "123")
    assert {r["state"] for r in rows} == {"in_progress", "cancelled"}


def test_unknown_historical_claim_does_not_replace_current_profile(store):
    store.add_source(source("current", sent="2026-10-28T10:00"))
    store.apply_memories([proposal("current", "我现在在练万戈")], store.epoch())
    store.add_source(source("past", sent="2026-10-29T10:00"))
    store.apply_memories(
        [proposal("past", "我以前在练幽玄", time_text="以前")], store.epoch()
    )
    assert [r["content"] for r in store.profile("cocoa", "123")] == ["我现在在练万戈"]
    assert len(store.timeline("cocoa", "123")) == 2


@pytest.mark.parametrize("phrase", ["本周", "本月", "今年"])
def test_current_interval_uses_source_order_instead_of_interval_start(store, phrase):
    store.add_source(source("early", sent="2026-10-29T08:00"))
    store.apply_memories([proposal("early", "旧目标")], store.epoch())
    store.add_source(source("late", sent="2026-10-29T10:00"))
    store.apply_memories([proposal("late", "新目标", time_text=phrase)], store.epoch())
    assert store.profile("cocoa", "123")[0]["content"] == "新目标"


def test_correction_quarantines_original_source_against_future_relearning(store):
    store.add_source(source())
    original = store.apply_memories([proposal()], store.epoch())[0]
    store.correct(original, "实际上在练万戈")
    changed_key = proposal()
    changed_key["key"] = "模型下次换一个主题"
    assert store.apply_memories([changed_key], store.epoch()) == []
    assert store.pending_sources() == []
    assert not store.add_source(source())
    store.mark_sources(["m1"], store.epoch(), "pending")
    assert store.pending_sources() == []
    assert store.relationship("cocoa", "123")["interaction_count"] == 1


def test_unchanged_relative_time_in_correction_keeps_original_anchor(store):
    store.add_source(source(sent="2026-10-20T10:00"))
    original = store.apply_memories([proposal(time_text="昨天")], store.epoch())[0]
    old = store.get_memory(original)
    corrected = store.correct(original, "纠正描述", time_text="昨天")
    row = store.get_memory(corrected)
    assert row["event_start"] == old["event_start"] == ts("2026-10-19")
    assert row["time_text"] == "昨天"


def test_pending_cursor_preserves_same_timestamp_order(store):
    for identity in ("c", "a", "b"):
        store.add_source(source(identity))
    first = store.pending_sources(limit=2)
    following = store.pending_sources(after=(first[-1]["sent_at"], first[-1]["id"]))
    assert [r["id"] for r in first + following] == ["a", "b", "c"]


def test_budget_reservation_does_not_consume_other_counter_on_failure(store):
    assert store.reserve_call_budget("2026-10-29", "g1", 1, 4)
    assert not store.reserve_call_budget("2026-10-29", "g2", 1, 4)
    assert store.budget_used("2026-10-29", "g2") == 0
    assert not store.reserve_call_budget("2026-10-29", "g1", 10, 1)
    assert store.budget_used("2026-10-29", "*") == 1


def test_call_budget_is_atomic_across_separate_connections(tmp_path):
    stores = [Store(tmp_path / "budget.sqlite3") for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(
                executor.map(
                    lambda index: stores[index % 2].reserve_call_budget(
                        "2026-10-29", f"g{index % 2}", 7, 20
                    ),
                    range(40),
                )
            )
        assert sum(outcomes) == 7
        assert stores[0].budget_used("2026-10-29", "*") == 7
        assert sum(stores[0].budget_used("2026-10-29", f"g{i}") for i in range(2)) == 7
    finally:
        for db in stores:
            db.close()


def test_legacy_database_adds_time_text_without_losing_old_memory(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE memories (
            id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
            kind TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
            state TEXT NOT NULL, source_ids TEXT NOT NULL, source_group TEXT NOT NULL,
            source_sent_at REAL NOT NULL, learned_at REAL NOT NULL,
            event_start REAL, event_end REAL, precision TEXT NOT NULL,
            valid_from REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            corrects_id INTEGER, fingerprint TEXT UNIQUE NOT NULL)""")
        db.execute("""INSERT INTO memories VALUES
            (1,'cocoa','123','goal','current_practice','旧版记忆','in_progress','[]','g1',
            100,200,NULL,NULL,'unknown',100,'active',NULL,'legacy')""")
    migrated = Store(path)
    try:
        assert migrated.profile("cocoa", "123")[0]["content"] == "旧版记忆"
        assert migrated.get_memory(1)["time_text"] == ""
    finally:
        migrated.close()


@pytest.mark.parametrize("phrase", ["2026-02-30", "昨天以前", "今天或明天"])
def test_invalid_or_ambiguous_time_cannot_overwrite_current_profile(store, phrase):
    store.add_source(source("current", sent="2026-10-28T10:00"))
    store.apply_memories([proposal("current", "当前目标")], store.epoch())
    store.add_source(source("ambiguous", sent="2026-10-29T10:00"))
    store.apply_memories(
        [proposal("ambiguous", "时间不明的目标", time_text=phrase)], store.epoch()
    )
    assert [r["content"] for r in store.profile("cocoa", "123")] == ["当前目标"]
