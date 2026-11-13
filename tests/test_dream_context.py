"""Dream boundaries reset native histories without losing durable memories."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.dream_context import GROUP_HISTORY_PREFIX
from astrbot_plugin_persona_manager.tests.astrbot_stub import TextPart
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import (
    request,
    route,
    snapshot,
)
from astrbot_plugin_persona_manager.tests.test_memory_tools import approved, remember


def chat_request(cid="conversation-1", persona_id="可可子"):
    req = request(persona_id)
    req.conversation.cid = cid
    req.conversation.history = json.dumps(req.contexts, ensure_ascii=False)
    return req


@pytest.mark.parametrize("trigger", ["page", "tool", "scheduled"])
def test_all_dream_entries_reset_history_and_keep_saved_memories(
    entry, monkeypatch, trigger
):
    async def run():
        async with entry.running() as env:
            _, daily = await remember(env, "今天打了太鼓", category="daily")
            _, relationship = await remember(
                env, "喜欢简短回答", category="relationship", message_id="long-term"
            )
            if trigger == "page":
                env.request.payload = {"action": "memory_dream"}
                assert (await route(env.context, "action")())["saved"]
            elif trigger == "tool":
                event = approved("现在入梦", message_id="dream")
                env.context.cfg["admins_id"] = [event.get_sender_id()]
                result = await env.plugin.persona_memory_admin(
                    event, "dream", confirm=True
                )
                assert json.loads(result)["context_reset"] == "next_chat"
            else:
                import astrbot_plugin_persona_manager.runtime as runtime_module

                now = time.time() + 86400
                monkeypatch.setattr(
                    runtime_module, "time", SimpleNamespace(time=lambda: now)
                )
                env.plugin.config["learning_enabled"] = False
                env.plugin.store.set_meta("last_dream:可可子", now - 86400)
                await env.plugin.runtime.run_once()
            assert env.plugin.store.get_meta("dream_context:可可子") == 1
            req = chat_request()
            before = snapshot(req)
            original_extra = req.extra_user_content_parts[0]
            req.extra_user_content_parts.append(
                TextPart(GROUP_HISTORY_PREFIX + "旧群聊天")
            )
            event = approved("继续聊天", message_id="after-dream")
            await env.plugin.on_request(event, req)
            assert req.contexts == []
            assert req.conversation.history == "[]"
            after = snapshot(req)
            assert after[:2] == before[:2]
            assert after[3:] == before[3:]
            assert req.extra_user_content_parts[0] is original_extra
            assert all(
                not part.text.startswith(GROUP_HISTORY_PREFIX)
                for part in req.extra_user_content_parts
            )
            env.context.conversation_manager.update_conversation.assert_awaited_once_with(
                event.unified_msg_origin, "conversation-1", history=[]
            )
            assert env.plugin.store.get_memory(daily["id"])["status"] == "dreamed"
            assert env.plugin.store.get_memory(relationship["id"])["status"] == "active"
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_reset_is_once_per_conversation_and_survives_restart(entry):
    async def run():
        async with entry.running() as env:
            env.plugin.store.dream("可可子")
            event = approved("第一轮")
            req = chat_request()
            await env.plugin.on_request(event, req)
            req.contexts.append({"role": "user", "content": "入梦后新对话"})
            await env.plugin.on_request(event, req)
            assert req.contexts[0]["content"] == "入梦后新对话"
            env.context.conversation_manager.update_conversation.assert_awaited_once()
        async with entry.running() as env:
            req = chat_request()
            await env.plugin.on_request(approved("重启后继续"), req)
            assert req.contexts
            env.context.conversation_manager.update_conversation.assert_not_awaited()
            for cid, group in [
                ("another-conversation", "234567"),
                ("group-b", "456789"),
            ]:
                req = chat_request(cid)
                await env.plugin.on_request(approved("另一段对话", group_id=group), req)
                assert req.contexts == []
            # Two dreams may have the same clock timestamp; each is a new boundary.
            env.plugin.store.dream("可可子")
            req = chat_request()
            await env.plugin.on_request(approved("再次入梦后"), req)
            assert req.contexts == []
            assert env.context.conversation_manager.update_conversation.await_count == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "boundary",
    ["private", "other_persona", "auxiliary", "command", "gate_missing", "wrong_group"],
)
def test_dream_reset_keeps_chat_authorization_boundaries(entry, boundary):
    async def run():
        async with entry.running() as env:
            env.plugin.store.dream("可可子")
            req = chat_request(
                persona_id="其他人格" if boundary == "other_persona" else "可可子"
            )
            event = approved(
                "/六爻 测试" if boundary == "command" else "继续聊天",
                private=boundary == "private",
                group_id="777777" if boundary == "wrong_group" else "234567",
            )
            if boundary == "auxiliary":
                event.set_extra("provider_request", chat_request())
            elif boundary == "gate_missing":
                env.context.stars.clear()
            before = snapshot(req)
            await env.plugin.on_request(event, req)
            assert snapshot(req) == before
            env.context.conversation_manager.update_conversation.assert_not_awaited()

    asyncio.run(run())


def test_image_only_main_chat_also_starts_with_empty_history(entry):
    async def run():
        async with entry.running() as env:
            env.plugin.store.dream("可可子")
            event, req = approved("", chain=[]), chat_request()
            await env.plugin.on_request(event, req)
            assert req.contexts == []
            env.context.conversation_manager.update_conversation.assert_awaited_once()
            assert env.plugin.store.pending_sources() == []

    asyncio.run(run())


def test_reset_keeps_persona_examples_and_current_file_extract(entry):
    async def run():
        async with entry.running() as env:
            env.plugin.store.dream("可可子")
            req = chat_request()
            example = {"role": "assistant", "content": "人格开场范例"}
            current_file = {"role": "system", "content": "本轮附件解析结果"}
            req.contexts.insert(0, example)
            req.contexts.append(current_file)
            await env.plugin.on_request(approved("分析这个附件"), req)
            assert req.contexts == [example, current_file]
            assert req.conversation.history == "[]"

    asyncio.run(run())


def test_dream_inside_current_tool_loop_cannot_save_old_buffer(entry):
    async def run():
        async with entry.running() as env:
            event, req = approved("现在入梦"), chat_request()
            env.context.cfg["admins_id"] = [event.get_sender_id()]
            await env.plugin.on_request(event, req)
            result = json.loads(
                await env.plugin.persona_memory_admin(event, "dream", confirm=True)
            )
            assert result["ok"]
            assert req.contexts == []
            assert req.conversation is None
            env.context.conversation_manager.update_conversation.assert_not_awaited()
            replay = json.loads(
                await env.plugin.persona_memory_admin(event, "dream", confirm=True)
            )
            assert replay["replayed"]
            assert env.plugin.store.get_meta("dream_context:可可子") == 1
            next_req = chat_request()
            await env.plugin.on_request(
                approved("醒来继续聊天", message_id="next"), next_req
            )
            assert next_req.contexts == []
            env.context.conversation_manager.update_conversation.assert_awaited_once_with(
                event.unified_msg_origin, "conversation-1", history=[]
            )

    asyncio.run(run())


@pytest.mark.parametrize("hook", ["response", "tool_only"])
def test_old_reply_does_not_erase_new_dialogue_after_reset(entry, hook):
    async def run():
        async with entry.running() as env:
            old_event, old_req = approved("入梦前的请求"), chat_request()
            await env.plugin.on_request(old_event, old_req)
            env.plugin.store.dream("可可子")
            new_req = chat_request()
            await env.plugin.on_request(approved("新一轮", message_id="new"), new_req)
            new_req.contexts.append({"role": "assistant", "content": "新对话已保存"})
            if hook == "response":
                await env.plugin.on_response(
                    old_event, SimpleNamespace(completion_text="旧回复")
                )
            else:
                await env.plugin.on_tool_delivery(
                    old_event, SimpleNamespace(name="some_tool"), {}, []
                )
            assert old_req.conversation is None
            assert new_req.contexts[0]["content"] == "新对话已保存"
            env.context.conversation_manager.update_conversation.assert_awaited_once()

    asyncio.run(run())


def test_failed_storage_reset_uses_empty_request_and_retries(entry):
    async def run():
        async with entry.running() as env:
            env.plugin.store.dream("可可子")
            manager = env.context.conversation_manager
            manager.update_conversation.side_effect = RuntimeError(
                "storage unavailable"
            )
            req = chat_request()
            await env.plugin.on_request(approved("第一轮"), req)
            assert req.contexts == []
            assert req.conversation is None
            manager.update_conversation.side_effect = None
            req = chat_request()
            await env.plugin.on_request(approved("重试", message_id="retry"), req)
            assert req.contexts == []
            assert req.conversation is not None
            assert manager.update_conversation.await_count == 2

    asyncio.run(run())


def test_initial_schedule_timestamp_and_other_persona_dream_do_not_reset(entry):
    async def run():
        async with entry.running() as env:
            env.plugin.store.set_meta("last_dream:可可子", time.time())
            env.plugin.store.dream("其他人格")
            req = chat_request()
            await env.plugin.on_request(approved("照常聊天"), req)
            assert req.contexts
            env.context.conversation_manager.update_conversation.assert_not_awaited()

    asyncio.run(run())
