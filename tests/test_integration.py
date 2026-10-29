"""Boundary tests with real storage/runtime and fixture-scoped AstrBot doubles."""

import asyncio
import importlib.util
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.ingress import own_text, sample
from astrbot_plugin_persona_manager.runtime import CONTEXT_MARKER, Runtime
from astrbot_plugin_persona_manager.settings import Settings
from astrbot_plugin_persona_manager.storage import Store
from astrbot_plugin_persona_manager.tests.astrbot_stub import (
    At,
    MockContext,
    MockEvent,
    Plain,
    Reply,
    TextPart,
    install,
)


@pytest.fixture
def entry(monkeypatch, tmp_path):
    """Load entrypoint without leaving fake AstrBot modules in other tests."""
    stub = install(monkeypatch, tmp_path)
    name = "astrbot_plugin_persona_manager._integration_entry"
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    @asynccontextmanager
    async def running(config=None, context=None):
        context = context or stub.context()
        plugin = module.PersonaManagerPlugin(context, config or {})
        await plugin.initialize()
        try:
            yield SimpleNamespace(
                plugin=plugin, context=context, request=stub.request, module=module
            )
        finally:
            await plugin.terminate()

    return SimpleNamespace(running=running, module=module, stub=stub)


def request(persona_id="可可子"):
    return SimpleNamespace(
        system_prompt="原核心与专业工具规则",
        prompt="用户真实问题",
        contexts=[{"role": "user", "content": "原有上下文"}],
        func_tool=SimpleNamespace(tools=[SimpleNamespace(name="query_rtlink")]),
        extra_user_content_parts=[TextPart("其他插件添加的上下文")],
        conversation=SimpleNamespace(persona_id=persona_id),
        image_urls=["https://example.test/existing.png"],
    )


def route(context, name):
    matches = [
        handler for path, handler in context.routes.items() if path.endswith("/" + name)
    ]
    assert len(matches) == 1
    return matches[0]


def snapshot(req):
    return (
        req.system_prompt,
        req.prompt,
        json.dumps(req.contexts),
        req.func_tool,
        list(req.func_tool.tools),
        list(req.image_urls),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"private": True},
        {"stopped": True},
        {"user_id": "345678"},
        {"platform_name": "webchat"},
        {"group_id": ""},
        {"timestamp": float("nan")},
        {"timestamp": time.time() + 3600},
        {"text": "/六爻 测试"},
        {"text": "api_key=not-a-real-credential"},
        {"extras": {"handlers_parsed_params": {"command": "数字卦"}}},
    ],
)
def test_ingress_rejects_non_sources_without_changing_event(overrides):
    event = MockEvent(**overrides)
    before = list(event.message_obj.message)
    assert sample(event, Settings(), "interaction") is None
    assert event.message_obj.message == before
    assert event.stop_calls == 0


def test_ingress_keeps_only_sender_plain_parts_not_quote_or_at():
    event = MockEvent(
        chain=[Reply(text="我已通过十段"), At("345678"), Plain("以后回答简短")]
    )
    assert own_text(event) == "以后回答简短"
    source = sample(event, Settings(), "interaction")
    assert source["text"] == "以后回答简短"
    assert source["user_id"] == "123456"
    assert source["sent_at"] == event.message_obj.timestamp


def test_real_runtime_retrieves_same_qq_across_groups_but_not_other_users(tmp_path):
    async def run():
        store = Store(tmp_path / "memory.sqlite3")
        context = MockContext()
        runtime = Runtime(
            store, context, lambda: Settings(), logging.getLogger(__name__)
        )
        try:
            original = sample(MockEvent(group_id="234567"), Settings(), "interaction")
            await runtime.capture(original)
            other_group = sample(
                MockEvent(
                    "你记得我的偏好吗", group_id="456789", message_id="other-group"
                ),
                Settings(),
                "interaction",
            )
            prompt, trace = await runtime.chat_context(other_group)
            assert "以后回答简短" in prompt
            assert CONTEXT_MARKER in prompt
            assert "234567" not in prompt  # No source group exposed to the chat model.
            assert trace["memory_ids"]
            assert (
                store.timeline("可可子", "123456")[0]["source_sent_at"]
                == original["sent_at"]
            )
            stranger = dict(other_group, user_id="987654")
            other_prompt, _ = await runtime.chat_context(stranger)
            assert "以后回答简短" not in other_prompt
            context.llm_generate.assert_not_called()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_private_chat_all_hooks_leave_routing_request_and_database_untouched(entry):
    async def run():
        async with entry.running({"observation_groups": ["234567"]}) as env:
            event = MockEvent(
                private=True, extras={"access_gate_original_text": "以后回答简短"}
            )
            req = request()
            original = snapshot(req)
            parts = list(req.extra_user_content_parts)
            await env.plugin.observe(event)
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert snapshot(req) == original
            assert req.extra_user_content_parts == parts
            assert env.plugin.store.pending_sources() == []
            assert env.plugin.store.timeline("可可子", "123456") == []
            assert event.stop_calls == 0
            assert event.sent == []
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_request_without_access_gate_marker_does_not_record_interaction(entry):
    async def run():
        async with entry.running() as env:
            event = MockEvent()
            req = request()
            before = snapshot(req)
            parts = list(req.extra_user_content_parts)
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert env.plugin.store.pending_sources() == []
            assert env.plugin.store.timeline("可可子", "123456") == []
            assert snapshot(req) == before
            assert req.extra_user_content_parts == parts
            assert event.stop_calls == 0

    asyncio.run(run())


def test_observation_is_whitelisted_and_cannot_create_personal_memory(entry):
    async def run():
        async with entry.running({"observation_groups": ["234567"]}) as env:
            allowed = MockEvent("这局稳如老狗", wake=False)
            blocked = MockEvent(
                "这局稳如老狗", group_id="456789", message_id="blocked", wake=False
            )
            await env.plugin.observe(allowed)
            await env.plugin.observe(blocked)
            rows = env.plugin.store.pending_sources()
            assert len(rows) == 1
            assert rows[0]["group_id"] == "234567"
            assert rows[0]["purpose"] == "observation"
            assert env.plugin.store.timeline("可可子", "123456") == []
            assert allowed.stop_calls == blocked.stop_calls == 0

    asyncio.run(run())


def test_passive_listener_preserves_access_gate_routing_bits(entry):
    async def run():
        async with entry.running({"observation_groups": ["234567"]}) as env:
            # ALL listeners may mark is_wake without granting a default LLM run.
            # ProcessStage uses is_at_or_wake_command and not call_llm.
            event = MockEvent("这局稳如老狗", wake=False)
            event.call_llm = True
            event.is_wake = True
            event.is_at_or_wake_command = False
            before = (event.call_llm, event.is_wake, event.is_at_or_wake_command)
            await env.plugin.observe(event)
            assert (
                event.call_llm,
                event.is_wake,
                event.is_at_or_wake_command,
            ) == before
            assert not (event.is_at_or_wake_command and not event.call_llm)
            assert event.stop_calls == 0
            assert event.sent == []
            assert env.plugin.store.pending_sources()[0]["purpose"] == "observation"
            req = request()
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert len(req.extra_user_content_parts) == 1
            assert env.plugin.store.timeline("可可子", "123456") == []
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_other_persona_request_receives_no_personal_context(entry):
    async def run():
        async with entry.running() as env:
            env.context.conversation_manager.get_conversation.return_value.persona_id = "其他人格"
            env.context.provider_cfg["persona_id"] = "其他人格"
            event = MockEvent(
                persona_id="其他人格",
                extras={"access_gate_original_text": "以后回答简短"},
            )
            req = request("其他人格")
            parts = list(req.extra_user_content_parts)
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert req.extra_user_content_parts == parts
            assert env.plugin.store.timeline("可可子", "123456") == []
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_accepted_request_preserves_other_plugins_and_marks_context_temporary(entry):
    async def run():
        async with entry.running() as env:
            event = MockEvent(extras={"access_gate_original_text": "以后回答简短"})
            req = request()
            before = snapshot(req)
            first_part = req.extra_user_content_parts[0]
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert snapshot(req) == before
            assert req.extra_user_content_parts[0] is first_part
            added = [
                part
                for part in req.extra_user_content_parts
                if CONTEXT_MARKER in part.text
            ]
            assert len(added) == 1
            assert added[0].temporary
            assert (
                env.plugin.store.profile("可可子", "123456")[0]["content"]
                == "以后回答简短"
            )
            assert event.stop_calls == 0
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_repeated_request_and_send_hooks_do_not_duplicate_context_or_memory(entry):
    async def run():
        async with entry.running() as env:
            event = MockEvent(extras={"access_gate_original_text": "以后回答简短"})
            req = request()
            await env.plugin.on_request(event, req)
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            await env.plugin.after_sent(event)
            assert (
                sum(
                    CONTEXT_MARKER in part.text for part in req.extra_user_content_parts
                )
                == 1
            )
            assert len(env.plugin.store.timeline("可可子", "123456")) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "condition", ["missing", "inactive", "scope_disabled", "ordinary_llm"]
)
def test_gate_must_be_active_and_request_must_be_approved(entry, condition):
    async def run():
        context = MockContext(access_gate=condition != "missing")
        if condition == "inactive":
            context.stars[0].activated = False
        async with entry.running({"observation_groups": ["234567"]}, context) as env:
            event = MockEvent(extras={"access_gate_original_text": "以后回答简短"})
            if condition == "scope_disabled":
                event.plugins_name = ["astrbot_plugin_persona_manager"]
            if condition == "ordinary_llm":
                event.call_llm = True
            req = request()
            await env.plugin.on_request(event, req)
            await env.plugin.after_sent(event)
            assert len(req.extra_user_content_parts) == 1
            assert env.plugin.store.timeline("可可子", "123456") == []
            assert event.stop_calls == 0
            context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_main_hooks_retrieve_same_qq_across_groups_and_page_timeline(entry):
    async def run():
        async with entry.running() as env:
            first_event = MockEvent(
                extras={"access_gate_original_text": "以后回答简短"}
            )
            await env.plugin.on_request(first_event, request())
            await env.plugin.after_sent(first_event)
            second_event = MockEvent(
                "你还记得我吗",
                group_id="456789",
                message_id="second-group",
                extras={"access_gate_original_text": "你还记得我吗"},
            )
            second_request = request()
            await env.plugin.on_request(second_event, second_request)
            assert "以后回答简短" in second_request.extra_user_content_parts[-1].text
            env.request.query.update(user_id="123456")
            response = await route(env.context, "state")()
            assert response.status_code == 200
            assert (
                response["memories"][0]["source_sent_at"]
                == first_event.message_obj.timestamp
            )
            assert response["memories"][0]["user_id"] == "123456"
            env.request.query["user_id"] = "987654"
            stranger = await route(env.context, "state")()
            assert stranger["memories"] == []

    asyncio.run(run())


def test_admin_core_save_changes_only_prompt_and_rejects_other_persona_version(entry):
    async def run():
        async with entry.running() as env:
            manager = env.context.persona_manager
            persona = manager.personas["可可子"]
            tools_before, dialogs_before = (
                list(persona.tools),
                list(persona.begin_dialogs),
            )
            env.request.payload = {
                "action": "core_save",
                "content": "管理员明确设置的新核心",
            }
            response = await route(env.context, "action")()
            assert response.status_code == 200
            assert manager.updates == [
                ("可可子", {"system_prompt": "管理员明确设置的新核心"})
            ]
            assert persona.tools == tools_before
            assert persona.begin_dialogs == dialogs_before
            assert manager.personas["其他人格"].system_prompt == "其他核心"
            foreign_version = env.plugin.store.set_core("其他人格", "其他备份")
            env.request.payload = {
                "action": "core_rollback",
                "version_id": str(foreign_version),
            }
            rejected = await route(env.context, "action")()
            assert rejected.status_code == 400
            assert persona.system_prompt == "管理员明确设置的新核心"

    asyncio.run(run())


@pytest.mark.parametrize("action", ["memory_forget", "memory_correct"])
def test_admin_memory_actions_reject_wrong_qq_and_persona(entry, action):
    async def run():
        async with entry.running() as env:
            source = sample(MockEvent(), Settings(), "interaction")
            await env.plugin.runtime.capture(source)
            memory = env.plugin.store.profile("可可子", "123456")[0]
            env.request.payload = {
                "action": action,
                "memory_id": str(memory["id"]),
                "user_id": "987654",
                "content": "未经验证的替换内容",
            }
            response = await route(env.context, "action")()
            assert response.status_code == 400
            assert (
                env.plugin.store.get_memory(memory["id"])["content"] == "以后回答简短"
            )
            env.request.payload.update(persona_id="其他人格", user_id="123456")
            response = await route(env.context, "action")()
            assert response.status_code == 400
            assert env.plugin.store.get_memory(memory["id"]) is not None

    asyncio.run(run())


def test_admin_style_mutation_rejects_wrong_group_and_foreign_version(entry):
    async def run():
        async with entry.running() as env:
            store = env.plugin.store
            sources = [
                sample(
                    MockEvent("这局稳如老狗", group_id=group, message_id=group),
                    Settings(),
                    "observation",
                )
                for group in ("234567", "456789")
            ]
            ids, versions = [], []
            for source in sources:
                store.add_source(source)
                style_id = store.apply_styles(
                    [
                        {
                            "source_ids": [source["id"]],
                            "expression": "稳如老狗",
                            "meaning": "表现稳定",
                            "contexts": "游戏闲聊",
                        }
                    ],
                    store.epoch(),
                )[0]
                ids.append(style_id)
                versions.append(store.set_style_status(style_id, "active"))
            env.request.payload = {
                "action": "style_disable",
                "style_id": str(ids[0]),
                "scope_key": sources[1]["scope_key"],
            }
            rejected = await route(env.context, "action")()
            assert rejected.status_code == 400
            assert store.get_style(ids[0])["status"] == "active"
            env.request.payload = {
                "action": "style_rollback",
                "version_id": str(versions[1]),
                "scope_key": sources[0]["scope_key"],
            }
            rejected = await route(env.context, "action")()
            assert rejected.status_code == 400
            assert store.get_style(ids[0])["status"] == "active"

    asyncio.run(run())


def test_dashboard_rejects_unauthenticated_state_and_action(entry):
    async def run():
        async with entry.running() as env:
            env.request.username = None
            env.request.payload = {"action": "learning_pause", "paused": True}
            state_response = await route(env.context, "state")()
            action_response = await route(env.context, "action")()
            assert state_response.status_code in {401, 403}
            assert action_response.status_code in {401, 403}
            assert not env.plugin.store.get_meta("paused", False)
            assert env.context.persona_manager.updates == []

    asyncio.run(run())


def test_initialize_never_overwrites_native_persona_and_observer_priority(entry):
    async def run():
        async with entry.running() as env:
            assert env.context.persona_manager.updates == []
            assert (
                env.context.persona_manager.personas["可可子"].system_prompt
                == "原生核心不得自动覆盖"
            )
            filters = env.plugin.observe._astrbot_filters
            assert any(
                kind == "event_message_type" and values.get("priority") == 9998
                for kind, _args, values in filters
            )

    asyncio.run(run())
