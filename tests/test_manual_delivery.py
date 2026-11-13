"""AstrBot v4.29.4 direct-tool sends can skip both ordinary delivery hooks.

Verified against the v4.29.4 tag: SendMessageToUserTool.call records current-session
receipts only after a successful send; MainAgentHooks.on_tool_end clears _result
before notifying plugins; RespondStage suppresses an identical final plain reply.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_persona_manager.delivery import (
    SENT_TEXTS_KEY,
    confirmed_result_delivery,
    confirmed_tool_delivery,
)
from astrbot_plugin_persona_manager.tests.astrbot_stub import (
    At,
    MockEvent,
    Plain,
    Reply,
    ResultContentType,
)
from astrbot_plugin_persona_manager.tests.test_integration import (
    actual_gate as actual_gate,
)
from astrbot_plugin_persona_manager.tests.test_integration import entry as entry
from astrbot_plugin_persona_manager.tests.test_integration import request

SENDER = SimpleNamespace(name="send_message_to_user")


def tool_result(event, *, target=None, error=False, text=None):
    return SimpleNamespace(
        isError=error,
        content=[
            SimpleNamespace(
                type="text",
                text=text
                if text is not None
                else f"Message sent to session {target or event.unified_msg_origin}",
            )
        ],
    )


def sent_receipt(event, text):
    event._has_send_oper = True
    event.set_extra(SENT_TEXTS_KEY, [text])


def arguments(*parts, session=None):
    result = {"messages": [{"type": "plain", "text": value} for value in parts]}
    if session is not None:
        result["session"] = session
    return result


async def prepare_custom_wake(
    env,
    actual_gate,
    *,
    message_id="message-1",
    text="可可子，今天练习左右手交替更稳定",
):
    gate = actual_gate(env.context, {"group_whitelist": ["234567"]})
    env.context.stars[0].star_cls = gate
    event = MockEvent(text, message_id=message_id)
    event._has_send_oper = False
    await gate.enforce_access(event)
    await gate.route_wake(event)
    assert gate._bool("wake_enabled", True)
    assert event.get_extra("access_gate_original_text") is not None
    req = request()
    event.set_extra("provider_request", req)
    await env.plugin.on_request(event, req)
    assert event.get_extra(env.module.PENDING_KEY) is not None
    return event


def test_core_receipt_normalizes_plain_parts_with_spaces():
    event = MockEvent()
    sent_receipt(event, "第一段 第二段")
    result = SimpleNamespace(
        chain=[Reply(), Plain("第一段"), At("345678"), Plain("第二段")]
    )
    assert confirmed_result_delivery(event, result) == "第一段 第二段"
    assert (
        confirmed_tool_delivery(
            event, SENDER, arguments(" 第一段 ", " 第二段 "), tool_result(event)
        )
        == "第一段 第二段"
    )
    # Concatenating without Core's separator must not claim a matching receipt.
    event.set_extra(SENT_TEXTS_KEY, ["第一段第二段"])
    assert confirmed_result_delivery(event, result) is None


@pytest.mark.parametrize("target", [None, "qq-main:GroupMessage:234567", "234567"])
def test_successful_current_session_tool_delivery(target):
    event = MockEvent()
    sent_receipt(event, "已发送的回复")
    event._result = None  # The real MainAgentHooks clears this before callbacks.
    assert (
        confirmed_tool_delivery(
            event, SENDER, arguments("已发送的回复", session=target), tool_result(event)
        )
        == "已发送的回复"
    )
    assert event.get_result() is None


@pytest.mark.parametrize(
    "boundary",
    [
        "send_exception",
        "missing_platform",
        "no_receipt",
        "not_sent",
        "other_session",
        "other_tool",
        "tool_error",
        "success_text_without_receipt",
        "old_receipt_different_body",
        "nontext",
        "mixed_media",
        "empty_body",
        "missing_result",
        "malformed_result",
    ],
)
def test_tool_arguments_and_errors_never_prove_delivery(boundary):
    event = MockEvent()
    sent_receipt(event, "已发送的回复")
    args = arguments("已发送的回复")
    tool = SENDER
    result = tool_result(event)
    if boundary == "send_exception":
        event.extras.clear()
        event._has_send_oper = False
        result = tool_result(
            event, text="error: failed to send message: connection lost"
        )
    elif boundary == "missing_platform":
        event.extras.clear()
        event._has_send_oper = False
        result = tool_result(event, text="error: failed to find platform")
    elif boundary in {"no_receipt", "success_text_without_receipt"}:
        event.extras.clear()
    elif boundary == "not_sent":
        event._has_send_oper = False
    elif boundary == "other_session":
        args["session"] = "qq-main:GroupMessage:456789"
        result = tool_result(event, target=args["session"])
    elif boundary == "other_tool":
        tool = SimpleNamespace(name="execute_shell")
    elif boundary == "tool_error":
        result = tool_result(event, error=True)
    elif boundary == "old_receipt_different_body":
        args = arguments("本次没有发送的内容")
    elif boundary == "nontext":
        args["messages"] = [{"type": "image", "url": "https://example.test/image"}]
    elif boundary == "mixed_media":
        args["messages"].append({"type": "image", "url": "https://example.test/image"})
    elif boundary == "empty_body":
        args = arguments(" ")
    elif boundary == "missing_result":
        result = None
    else:
        result = SimpleNamespace(content=[SimpleNamespace(type="image", text="")])
    assert confirmed_tool_delivery(event, tool, args, result) is None


@pytest.mark.parametrize(
    "boundary", ["no_receipt", "not_sent", "different", "media", "empty"]
)
def test_decorating_hook_requires_exact_already_sent_plain_result(boundary):
    event = MockEvent()
    sent_receipt(event, "已发送的回复")
    result = SimpleNamespace(chain=[Plain("已发送的回复")])
    if boundary == "no_receipt":
        event.extras.clear()
    elif boundary == "not_sent":
        event._has_send_oper = False
    elif boundary == "different":
        result.chain = [Plain("尚未发送的最终回复")]
    elif boundary == "media":
        result.chain.append(SimpleNamespace(type="image"))
    else:
        result.chain = []
    assert confirmed_result_delivery(event, result) is None


def test_tool_callback_records_custom_wake_chat_when_final_reply_empty(
    entry, actual_gate
):
    async def run():
        async with entry.running() as env:
            event = await prepare_custom_wake(env, actual_gate)
            sent_receipt(event, "先保持节奏 再提高速度")
            event._result = None
            await env.plugin.on_tool_delivery(
                event,
                SENDER,
                arguments(" 先保持节奏 ", " 再提高速度 "),
                tool_result(event),
            )
            assert event.get_result() is None
            rows = env.plugin.store.pending_sources()
            assert len(rows) == 1 and rows[0]["purpose"] == "interaction"
            assert (
                len(
                    env.plugin.store.pending_learning(
                        persona_id="可可子", lane="interaction"
                    )
                )
                == 1
            )
            history = env.plugin.store.dialogue_history("可可子")
            assert len(history) == 1
            assert history[0]["reply_text"] == "先保持节奏 再提高速度"
            env.context.llm_generate.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("first", ["tool", "decorate"])
def test_duplicate_final_reply_and_repeated_hooks_record_only_once(
    entry, actual_gate, first
):
    async def run():
        async with entry.running() as env:
            event = await prepare_custom_wake(env, actual_gate)
            sent_receipt(event, "已发送的回复")
            args = arguments("已发送的回复")
            result = tool_result(event)
            event._result = SimpleNamespace(
                chain=[Plain("已发送的回复")],
                result_content_type=ResultContentType.LLM_RESULT,
            )
            if first == "tool":
                await env.plugin.on_tool_delivery(event, SENDER, args, result)
            # v4.29.4 RespondStage returns before after_message_sent for this.
            await env.plugin.after_stream(event)
            await env.plugin.on_tool_delivery(event, SENDER, args, result)
            await env.plugin.after_sent(event)
            await env.plugin.after_stream(event)
            assert len(env.plugin.store.pending_sources()) == 1
            assert len(env.plugin.store.dialogue_history("可可子")) == 1
            assert (
                env.plugin.store.db.execute(
                    "SELECT count(*) FROM delivery_receipts"
                ).fetchone()[0]
                == 1
            )
            assert (
                env.plugin.store.db.execute(
                    "SELECT count(*) FROM learning_queue WHERE lane='interaction'"
                ).fetchone()[0]
                == 1
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "boundary", ["error", "other_session", "not_sent", "no_pending"]
)
def test_failed_or_unrelated_tool_send_never_queues_chat(entry, actual_gate, boundary):
    async def run():
        async with entry.running() as env:
            event = await prepare_custom_wake(env, actual_gate)
            args = arguments("已发送的回复")
            result = tool_result(event)
            sent_receipt(event, "已发送的回复")
            if boundary == "error":
                result = tool_result(event, text="error: network failure")
                event.extras.pop(SENT_TEXTS_KEY)
            elif boundary == "other_session":
                args["session"] = "qq-main:GroupMessage:456789"
                result = tool_result(event, target=args["session"])
                event.extras.pop(SENT_TEXTS_KEY)
            elif boundary == "not_sent":
                event._has_send_oper = False
            else:
                event.extras.pop(env.module.PENDING_KEY)
            event._result = None
            await env.plugin.on_tool_delivery(event, SENDER, args, result)
            assert env.plugin.store.pending_sources() == []
            assert env.plugin.store.dialogue_history("可可子") == []

    asyncio.run(run())


def test_general_result_without_framework_receipt_is_still_rejected(entry, actual_gate):
    async def run():
        async with entry.running() as env:
            event = await prepare_custom_wake(env, actual_gate)
            event._has_send_oper = True
            event._result = SimpleNamespace(
                chain=[Plain("其他插件的普通结果")],
                result_content_type=ResultContentType.GENERAL_RESULT,
            )
            await env.plugin.after_sent(event)
            await env.plugin.after_stream(event)
            assert env.plugin.store.pending_sources() == []
            assert env.plugin.store.dialogue_history("可可子") == []

    asyncio.run(run())


def test_twenty_custom_wake_tool_replies_consolidate_to_active_memory(
    entry, actual_gate
):
    async def run():
        # Keep the product's default threshold, ordinary text and real queue path.
        config = {
            "memory_default_enabled": True,
            "memory_batch_threshold": 20,
            "thinking_notice": False,
        }
        async with entry.running(config) as env:
            assert env.plugin.settings().memory_batch_threshold == 20

            async def model_response(**kwargs):
                records = json.loads(kwargs["prompt"].split("原始消息 JSON：\n")[1])
                assert len(records) == 20
                assert len({row["id"] for row in records}) == 20
                row = records[0]
                return SimpleNamespace(
                    completion_text=json.dumps(
                        {
                            "proposals": [
                                {
                                    "class_id": "cache",
                                    "key": "太鼓练习",
                                    "content": "正在练习左右手交替并保持节奏",
                                    "subject_source_id": row["id"],
                                    "evidence": [
                                        {
                                            "source_id": row["id"],
                                            "quote": row["text"],
                                            "role": "user",
                                        }
                                    ],
                                    "provenance": "self_report",
                                    "confidence": 0.95,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                )

            env.context.llm_generate.side_effect = model_response
            for index in range(20):
                event = await prepare_custom_wake(
                    env,
                    actual_gate,
                    message_id=f"practice-{index}",
                    text=f"可可子，我今天练习第{index + 1}组左右手交替，尝试慢速保持节奏",
                )
                sent_receipt(event, "保持这个节奏继续练习")
                event._result = None
                await env.plugin.on_tool_delivery(
                    event,
                    SENDER,
                    arguments("保持这个节奏继续练习"),
                    tool_result(event),
                )
                scope = event.get_extra(env.module.PENDING_KEY)["scope_key"]
                if index == 18:
                    await env.plugin.runtime.run_once(scope_key=scope)
                    env.context.llm_generate.assert_not_called()
                    assert env.plugin.store.memory_catalog("可可子")["total"] == 0
            assert (
                len(
                    env.plugin.store.pending_learning(
                        persona_id="可可子", scope_key=scope, lane="interaction"
                    )
                )
                == 20
            )
            await env.plugin.runtime.run_once(scope_key=scope)
            env.context.llm_generate.assert_awaited_once()
            catalog = env.plugin.store.memory_catalog("可可子", status="active")
            assert catalog["total"] == 1
            assert catalog["items"][0]["category"] == "cache"
            assert catalog["items"][0]["user_id"] == "123456"
            assert catalog["items"][0]["content"] == "正在练习左右手交替并保持节奏"
            assert (
                env.plugin.store.pending_learning(
                    persona_id="可可子", scope_key=scope, lane="interaction"
                )
                == []
            )

    asyncio.run(run())
