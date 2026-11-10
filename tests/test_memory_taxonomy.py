"""End-to-end memory lifecycles, evidence, isolation and accounting regressions."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.configuration import validate_changes
from astrbot_plugin_persona_manager.memory_types import parse_memory_response
from astrbot_plugin_persona_manager.runtime import Runtime
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.temporal import BEIJING
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import route
from astrbot_plugin_persona_manager.tests.test_runtime import FakeContext, FakeLogger


@pytest.fixture
def env(tmp_path):
    clock = SimpleNamespace(now=datetime(2026, 11, 10, 12, tzinfo=BEIJING).timestamp())
    store = Store(tmp_path / "mem.sqlite3", clock=lambda: clock.now)
    yield store, clock
    store.close()


def source(
    store, clock, sid="one", user="123456", scope="group-a", content="我今天很开心"
):
    row = dict(
        id=sid,
        persona_id="cocoa",
        platform_id="qq",
        bot_id="999999",
        group_id=scope,
        user_id=user,
        text=content,
        sent_at=clock.now,
        purpose="interaction",
        scope_key=scope,
        umo="qq:GroupMessage:" + scope,
    )
    store.add_source(row)
    return row


def propose(store, row, category="daily", **overrides):
    value = dict(
        class_id=category,
        key="日常",
        content=row["text"],
        subject_source_id=row["id"],
        evidence=[dict(source_id=row["id"], quote=row["text"], role="user")],
        provenance="self_report",
        confidence=0.9,
        mood="开心",
        salience=0.8,
        tags=["日常"],
        details={},
        ttl_seconds=300,
    )
    value.update(overrides)
    return value


def apply(store, rows, items, **kwargs):
    proposals = parse_memory_response(
        rows,
        json.dumps({"proposals": items}, ensure_ascii=False),
        store.classifiers("cocoa"),
        store.conversation_material(rows),
    )
    return store.apply_classified(proposals, store.epoch(), **kwargs)


def test_default_gate_groups_and_public_opt_in():
    cfg = Settings()
    assert cfg.remembers("123456", "qq")
    assert not cfg.uses_public_memory("123456", "qq")
    assert not replace(cfg, memory_disabled_groups=frozenset({"123456"})).remembers(
        "123456", "qq"
    )
    assert not replace(cfg, memory_default_enabled=False).remembers("123456", "qq")
    assert Settings.read({"public_memory_groups": ["qq:123456"]}).uses_public_memory(
        "123456", "qq"
    )


def test_timed_lifetime_uses_source_time_and_restart(env):
    store, clock = env
    row = source(store, clock)
    mid = apply(store, [row], [propose(store, row, "timed")])[0]
    assert store.get_memory(mid)["expires_at"] == clock.now + 300
    assert store.recall("cocoa", "123456", "开心")
    clock.now += 300
    assert store.recall("cocoa", "123456", "开心") == []
    store.maintain_memories()
    assert store.get_memory(mid)["status"] == "expired"


def test_dream_only_cleans_daily_and_cache_discards_oldest(env):
    store, clock = env
    ids = []
    for index, category in enumerate(
        ["daily", "timed", "relationship", "cache", "cache", "cache"]
    ):
        row = source(store, clock, str(index))
        ids.extend(
            apply(store, [row], [propose(store, row, category)], cache_capacity=2)
        )
        clock.now += 1
    assert store.get_memory(ids[3])["status"] == "evicted"
    assert store.get_memory(ids[3])["content"] == ""
    assert store.dream("cocoa") == 1
    assert store.get_memory(ids[0])["status"] == "dreamed"
    assert store.get_memory(ids[0])["evidence"] == []
    assert all(
        store.get_memory(mid)["status"] == "active"
        for mid in (ids[1], ids[2], ids[4], ids[5])
    )


@pytest.mark.parametrize(
    "category,status", [("daily", "auto"), ("relationship", "pending")]
)
def test_unknown_type_approval_and_migration(env, category, status):
    store, clock = env
    row = source(store, clock)
    definition = dict(
        id="new_kind",
        name="新类型",
        category=category,
        description="归类新认识",
        keywords=["新认识"],
        ttl_seconds=600,
    )
    mid = apply(store, [row], [propose(store, row, "new_kind", new_class=definition)])[
        0
    ]
    cls = store._class("cocoa", "new_kind")
    assert cls["status"] == status
    assert store.get_memory(mid)["status"] == (
        "pending" if status == "pending" else "active"
    )
    if status == "pending":
        assert store.profile("cocoa", "123456") == []
        with pytest.raises(ValueError):
            store.manage_memory(mid, action="approve", revision=1)
    definition["category"] = "timed"
    assert (
        store.save_classifier(
            "cocoa", definition, revision=cls["revision"], confirm=True
        )
        == 1
    )
    memory = store.get_memory(mid)
    assert memory["category"] == "timed"
    assert memory["status"] == "active"
    assert memory["expires_at"] == clock.now + 600
    with pytest.raises(ValueError):
        store.save_classifier(
            "cocoa", definition, revision=cls["revision"], confirm=True
        )


def test_pending_long_can_be_confirmed_and_moved_to_self(env):
    store, clock = env
    row = source(store, clock)
    definition = dict(
        id="temper",
        name="特性",
        category="relationship",
        description="特性",
        keywords=[],
        ttl_seconds=600,
    )
    mid = apply(store, [row], [propose(store, row, "temper", new_class=definition)])[0]
    definition["category"] = "self"
    store.save_classifier("cocoa", definition, revision=1, confirm=True)
    assert store.get_memory(mid)["user_id"] == "__self__"
    assert store.get_memory(mid)["status"] == "active"


def test_knowledge_needs_independent_evidence_and_replays_do_not_count(env):
    store, clock = env
    for i in range(3):
        row = source(
            store,
            clock,
            str(i),
            user=str(123456 + i),
            content="标准大气压下水的沸点是摄氏一百度",
        )
        mid = apply(store, [row], [propose(store, row, "knowledge", key="水的沸点")])[0]
        assert store.get_memory(mid)["confirmations"] == i + 1
        assert store.get_memory(mid)["status"] == ("active" if i == 2 else "pending")
        assert (
            apply(store, [row], [propose(store, row, "knowledge", key="水的沸点")])
            == []
        )
        assert store.get_memory(mid)["confirmations"] == i + 1
    assert store.profile("cocoa", "__knowledge__")


def test_same_speaker_same_day_does_not_certify_knowledge(env):
    store, clock = env
    for i in range(4):
        row = source(store, clock, str(i))
        mid = apply(store, [row], [propose(store, row, "knowledge")])[0]
    assert store.get_memory(mid)["confirmations"] == 1
    assert store.get_memory(mid)["status"] == "pending"


def test_hearsay_keeps_attribution_and_cannot_confirm_knowledge(env):
    store, clock = env
    row = source(store, clock, content="听说他今天很开心")
    mid = apply(store, [row], [propose(store, row, "relationship")])[0]
    memory = store.get_memory(mid)
    assert memory["provenance"] == "hearsay"
    assert memory["confidence"] <= 0.65
    assert memory["accuracy"] == "unconfirmed"
    assert memory["evidence"][0]["quote"] == row["text"]


def test_self_and_emotional_events_need_actual_reply_then_refresh_traits(env):
    store, clock = env
    row = source(store, clock)
    with pytest.raises(ValueError):
        apply(store, [row], [propose(store, row, "self")])
    with pytest.raises(ValueError):
        apply(store, [row], [propose(store, row, "event")])
    store.record_delivery(row, "我很开心，谢谢你记得", [])
    evidence = [
        dict(source_id=row["id"], quote="我很开心，谢谢你记得", role="assistant")
    ]
    mid = apply(
        store, [row], [propose(store, row, "self", key="在意心意", evidence=evidence)]
    )[0]
    clock.now += 10
    row2 = source(store, clock, "two")
    store.record_delivery(row2, "谢谢你一直记得", [])
    evidence2 = [dict(source_id=row2["id"], quote="谢谢你一直记得", role="assistant")]
    newer = apply(
        store,
        [row2],
        [
            propose(
                store,
                row2,
                "self",
                key="在意心意",
                evidence=evidence2,
                content="我重视持续的关心",
            )
        ],
    )[0]
    assert store.get_memory(mid)["status"] == "superseded"
    assert store.get_memory(newer)["status"] == "active"
    event = apply(store, [row2], [propose(store, row2, "event", evidence=evidence2)])[0]
    assert store.get_memory(event)["mood"] == "开心"


@pytest.mark.parametrize(
    "body", ["not-json", '{"proposals":[', "[]", '{"proposals":[],"proposals":[]}']
)
def test_bad_json_is_retryable(env, body):
    store, clock = env
    row = source(store, clock)
    with pytest.raises(ValueError):
        parse_memory_response([row], body, store.classifiers("cocoa"), [])


def test_model_cannot_force_knowledge_or_invent_quotes(env):
    store, clock = env
    row = source(store, clock)
    for bad in [
        dict(provenance="admin"),
        dict(evidence=[dict(source_id="one", quote="编造", role="user")]),
        dict(subject_source_id="not-in-batch"),
    ]:
        with pytest.raises(ValueError):
            apply(store, [row], [propose(store, row, "knowledge", **bad)])


def test_scoped_recall_filters_before_profile_dedup(env):
    store, clock = env
    first = source(store, clock, scope="a")
    one = apply(store, [first], [propose(store, first, "relationship", key="偏好")])[0]
    clock.now += 1
    second = source(store, clock, "two", scope="b", content="我今天有点累")
    two = apply(store, [second], [propose(store, second, "relationship", key="偏好")])[
        0
    ]
    assert store.profile("cocoa", "123456", scope_key="a", public=False)[0]["id"] == one
    assert store.profile("cocoa", "123456", scope_key="a", public=True)[0]["id"] == two
    assert not store.recall("cocoa", "123456", "有点累", scope_key="a", public=False)


def test_admin_knowledge_and_reclassification_cannot_bypass_class_approval(env):
    store, clock = env
    with pytest.raises(ValueError):
        store.force_knowledge("cocoa", "内容", "主题", actor="")
    mid = store.force_knowledge("cocoa", "水有三态", "水", actor="administrator")
    assert store.get_memory(mid)["admin_forced"] == 1
    assert store.get_memory(mid)["status"] == "active"
    definition = dict(
        id="unknown",
        name="待确认",
        category="relationship",
        description="测试",
        keywords=[],
        ttl_seconds=300,
    )
    row = source(store, clock)
    apply(store, [row], [propose(store, row, "unknown", new_class=definition)])
    store.edit_memory_details(
        "cocoa", mid, {"class_id": "unknown", "mood": "平静"}, revision=1
    )
    assert store.get_memory(mid)["status"] == "pending"
    assert store.get_memory(mid)["hold_reason"] == "classification"


def reserve(store, day="2026-11-10", **extra):
    args = dict(
        persona_id="cocoa",
        provider_id="fake",
        purpose="interaction",
        estimated_input=60,
        output_limit=40,
        global_calls=100,
        group_calls=100,
        global_tokens=10000,
        group_tokens=10000,
        memory_daily_tokens=1000,
        memory_monthly_tokens=1000,
    )
    args.update(extra)
    return store.reserve_learning(day, "group", **args)


def test_daily_monthly_budget_atomic_reservation_failure_restart_and_rollover(env):
    store, clock = env
    first = reserve(store, memory_daily_tokens=100, memory_monthly_tokens=150)
    assert first
    assert reserve(store, memory_daily_tokens=100, memory_monthly_tokens=150) is None
    store.begin_learning(first)
    store.settle_learning(first, "error")
    assert reserve(store, "2026-11-11", memory_monthly_tokens=150) is None
    assert reserve(store, "2026-12-01", memory_monthly_tokens=150)
    assert store.memory_usage("2026-11-11")["monthly"]["mtokens"] == 0.0001
    assert store.memory_usage("2026-11-11")["daily"]["mtokens"] == 0
    assert reserve(store, "2026-11-12", memory_daily_tokens=0) is None


def test_unused_reservation_releases_monthly_and_observation_is_independent(env):
    store, clock = env
    rid = reserve(store, memory_monthly_tokens=100)
    store.release_learning(rid, not_sent=True)
    assert reserve(store, memory_monthly_tokens=100)
    assert reserve(store, purpose="observation", memory_monthly_tokens=0)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_mtokens_are_rejected(value):
    with pytest.raises(ValueError):
        validate_changes({}, {"memory_daily_mtokens": value})


def test_runtime_threshold_thinking_json_retry_and_shared_group_batch(env, monkeypatch):
    store, clock = env
    import astrbot_plugin_persona_manager.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    config = Settings(
        persona_id="cocoa",
        memory_batch_threshold=2,
        daily_group_token_limit=100000,
        thinking_notice=True,
    )
    context = FakeContext()
    seen = []

    async def notice(row):
        assert runtime.thinking_scope == row["scope_key"]
        seen.append(row)

    runtime = Runtime(
        store, context, lambda: config, FakeLogger(), thinking_callback=notice
    )
    one = source(store, clock, content="我喜欢喝温水")
    asyncio.run(runtime.run_once())
    assert not context.calls
    two = source(store, clock, "two", user="654321", content="我喜欢喝温水")
    asyncio.run(runtime.run_once())
    assert len(context.calls) == len(seen) == 1
    assert runtime.thinking_scope is None
    assert store.profile("cocoa", one["user_id"])
    assert store.profile("cocoa", two["user_id"])
    clock.now += 2000
    source(store, clock, "bad1")
    source(store, clock, "bad2")

    async def invalid(**kwargs):
        return SimpleNamespace(completion_text='{"proposals": [', usage=None)

    context.llm_generate = invalid
    asyncio.run(runtime.run_once())
    assert runtime.scheduler_status["state"] == "error"
    assert len(store.pending_learning(persona_id="cocoa")) == 2
    assert runtime.thinking_scope is None


def test_management_actions_require_dashboard_login(entry):
    async def run():
        async with entry.running() as env:
            env.request.username = None
            for action in [
                "classifier_save",
                "memory_detail_save",
                "memory_dream",
                "knowledge_force",
            ]:
                env.request.payload = {"action": action}
                response = await route(env.context, "action")()
                assert response.status_code == 403

    asyncio.run(run())


def test_large_public_history_does_not_hide_local_recall(env):
    store, clock = env
    local = source(store, clock, "local", scope="local")
    mid = apply(store, [local], [propose(store, local, "relationship", key="开心")])[0]
    for i in range(205):
        clock.now += 1
        remote = source(store, clock, str(i), scope="remote")
        apply(store, [remote], [propose(store, remote, "relationship", key="开心")])
    assert (
        store.recall("cocoa", "123456", "开心", scope_key="local", public=False)[0][
            "id"
        ]
        == mid
    )


def test_knowledge_threshold_changes_and_forgetting_remove_confirmation(env):
    store, clock = env
    for index in range(2):
        row = source(store, clock, str(index), user=str(123456 + index))
        mid = apply(store, [row], [propose(store, row, "knowledge")])[0]
    assert store.get_memory(mid)["status"] == "pending"
    store.reconcile_knowledge(2)
    assert store.get_memory(mid)["status"] == "active"
    store.reconcile_knowledge(3)
    assert store.get_memory(mid)["status"] == "pending"
    store.forget("cocoa", "123456")
    assert store.get_memory(mid)["confirmations"] == 1


def test_v1_backup_and_classifier_persistence(tmp_path):
    import sqlite3

    path = tmp_path / "upgrade.sqlite3"
    store = Store(path)
    store.db.executescript(
        "DROP TABLE memory_classes; DROP TABLE memory_details; DROP TABLE memory_confirmations; DELETE FROM meta WHERE key='classified_memory_version';"
    )
    store.close()
    upgraded = Store(path)
    backup = path.with_suffix(".before-v2.sqlite3")
    assert backup.exists()
    with sqlite3.connect(backup) as check:
        assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    upgraded.force_knowledge("cocoa", "水有三态", "水", actor="admin")
    cls = upgraded._class("cocoa", "knowledge")
    upgraded.save_classifier(
        "cocoa", {**cls, "name": "已核实知识"}, revision=1, confirm=True
    )
    upgraded.close()
    before = backup.read_bytes()
    reopened = Store(path)
    assert reopened._class("cocoa", "knowledge")["name"] == "已核实知识"
    assert reopened.profile("cocoa", "__knowledge__")[0]["content"] == "水有三态"
    assert backup.read_bytes() == before
    reopened.close()


def test_new_json_runtime_and_scheduled_dream(env, monkeypatch):
    store, clock = env
    import astrbot_plugin_persona_manager.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    row = source(store, clock)
    settings = Settings(
        persona_id="cocoa", memory_batch_threshold=1, thinking_notice=False
    )
    context = FakeContext()

    async def generate(**kwargs):
        return SimpleNamespace(
            completion_text=json.dumps({"proposals": [propose(store, row)]}),
            usage={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
        )

    context.llm_generate = generate
    runtime = Runtime(store, context, lambda: settings, FakeLogger())
    asyncio.run(runtime.run_once())
    assert runtime.scheduler_status["state"] == "completed"
    assert store.memory_catalog("cocoa")["items"][0]["category"] == "daily"
    assert store.memory_usage("2026-11-10")["daily"]["mtokens"] == 0.0002
    clock.now += 86400
    asyncio.run(runtime.run_once())
    assert store.memory_catalog("cocoa")["total"] == 0


def test_day_switch_during_thinking_releases_request(env, monkeypatch):
    store, clock = env
    import astrbot_plugin_persona_manager.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    source(store, clock, content="我喜欢喝温水")
    config = Settings(persona_id="cocoa", memory_batch_threshold=1)
    context = FakeContext()

    async def notice(row):
        clock.now += 86400

    runtime = Runtime(
        store, context, lambda: config, FakeLogger(), thinking_callback=notice
    )
    asyncio.run(runtime.run_once())
    assert context.calls == []
    assert runtime.scheduler_status["state"] == "deferred"
    assert runtime.thinking_scope is None
    assert store.memory_usage("2026-11-10")["monthly"]["mtokens"] == 0


def test_logged_in_admin_can_confirm_class_and_force_knowledge(entry):
    async def run():
        async with entry.running() as env:
            env.request.payload = {
                "action": "knowledge_force",
                "key": "水",
                "content": "水有三态",
            }
            result = await route(env.context, "action")()
            assert result["saved"] is True
            response = await route(env.context, "state")()
            memory = response["memory_catalog"]["items"][0]
            assert memory["admin_forced"] == 1
            assert memory["provenance"] == "admin"
            cls = next(c for c in response["classifiers"] if c["id"] == "knowledge")
            env.request.payload = {
                "action": "classifier_save",
                "definition": {**cls, "name": "核实知识"},
                "revision": cls["revision"],
                "confirm": True,
            }
            result = await route(env.context, "action")()
            assert result["saved"] is True
            result = await route(env.context, "action")()
            assert result.status_code == 400

    asyncio.run(run())


def test_cache_capacity_is_shared_per_user_but_keeps_group_visibility(env):
    store, clock = env
    local = source(store, clock, "local", scope="a")
    first = apply(store, [local], [propose(store, local, "cache")], cache_capacity=1)[0]
    clock.now += 1
    remote = source(store, clock, "remote", scope="b")
    apply(store, [remote], [propose(store, remote, "cache")], cache_capacity=1)
    assert store.get_memory(first)["status"] == "evicted"
    assert store.profile("cocoa", "123456", scope_key="a", public=False) == []
    assert store.profile("cocoa", "123456", scope_key="a", public=True)


def test_ready_backlog_can_be_processed_in_character_bounded_chunks(env, monkeypatch):
    store, clock = env
    import astrbot_plugin_persona_manager.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock.now))
    for i in range(2):
        source(store, clock, str(i), content="我喜欢喝温水。" * 90)
    config = Settings(
        persona_id="cocoa",
        memory_batch_threshold=2,
        max_batch_chars=1000,
        thinking_notice=False,
    )
    context = FakeContext()

    async def generated(**kwargs):
        context.calls.append(kwargs)
        return SimpleNamespace(completion_text='{"proposals":[]}', usage=None)

    context.llm_generate = generated
    runtime = Runtime(store, context, lambda: config, FakeLogger())
    asyncio.run(runtime.run_once())
    assert len(context.calls) == 1
    assert runtime.scheduler_status["state"] == "completed"
    assert len(store.pending_learning(persona_id="cocoa")) == 1
