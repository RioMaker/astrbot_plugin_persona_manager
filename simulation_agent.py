"""Run a dashboard turn through AstrBot's group-chat agent in a virtual session."""

import asyncio
import json
import time
from contextlib import ExitStack, asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

import httpx
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.db.po import Conversation
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.platform import astr_message_event
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.star.star_handler import EventType

from . import simulation_bailian
from .simulation import CONFIGURED, SEED_SOURCE, SimulationToolError


class SimulatedGroupEvent(AstrMessageEvent):
    """Group metadata for request hooks and tools, with delivery to the page."""

    def __init__(self, job, provider_id):
        scope = json.dumps(
            [job.owner, job.persona, job.model, provider_id, job.session_id]
        )
        self.conversation_id = str(uuid5(NAMESPACE_URL, scope))
        group_id = f"sim-{self.conversation_id}"
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "sim-bot"
        msg.session_id = group_id
        msg.message_id = job.id
        msg.group = Group(group_id=group_id, group_name="模拟群聊")
        msg.sender = MessageMember(
            f"sim-user-{uuid5(NAMESPACE_URL, job.owner)}", "试聊成员"
        )
        msg.message = [Plain(job.question)]
        msg.message_str = job.question
        msg.raw_message = {}
        meta = PlatformMetadata(
            name="aiocqhttp",
            id="persona-simulator",
            description="人格管理模拟群聊",
            support_proactive_message=False,
            support_streaming_message=False,
        )
        super().__init__(job.question, msg, meta, group_id)
        self.is_at_or_wake_command = self.is_wake = True
        self.replies: list[str] = []

    async def send(self, message):
        if text := message.get_plain_text(with_other_comps_mark=True).strip():
            self.replies.append(text)
        self._has_send_oper = True

    async def send_streaming(self, generator, use_fallback=False):
        parts = [
            chain.get_plain_text()
            async for chain in generator
            if chain and chain.type != "reasoning"
        ]
        if text := "".join(parts).strip():
            self.replies.append(text)
        self._has_send_oper = True


class _LocalProvider(ProviderOpenAIOfficial):
    def _create_http_client(self, provider_config):
        transport = httpx.AsyncHTTPTransport(uds=provider_config["socket"])
        return httpx.AsyncClient(transport=transport, trust_env=False)


@asynccontextmanager
async def _provider(context, plugin_config, job, provider_id, engine):
    if job.model == CONFIGURED:
        provider = context.get_provider_by_id(provider_id)
        if not provider or not callable(getattr(provider, "text_chat", None)):
            raise SimulationToolError("请为模拟对话选择有效的聊天提供商")
        modalities = provider.provider_config.get("modalities")
        if modalities and "tool_use" not in modalities:
            raise SimulationToolError("此提供商未启用工具调用，请检查提供商的模型能力")
        yield provider
        return

    # Use the framework's tool-call parser and transport contract without
    # registering a provider globally or changing another chat's selected model.
    if engine is not None:
        provider = _LocalProvider(
            {
                "type": "openai_chat_completion",
                "id": job.model,
                "model": job.model,
                "modalities": ["text", "tool_use"],
                "timeout": 360,
                "custom_extra_body": {"max_tokens": 512},
                "api_base": "http://localhost/v1",
                "key": ["local"],
                "socket": engine.client.connector.path,
                "max_context_tokens": engine.context_size,
            },
            {},
        )
    else:
        _, workspace, model = job.model.split(":", 2)
        try:
            provider = simulation_bailian.create_agent_provider(
                plugin_config, workspace, model, job.model
            )
        except ValueError as exc:
            raise SimulationToolError(str(exc)) from None
    try:
        yield provider
    finally:
        await provider.terminate()


def _agent_config(config, job, streaming):
    settings = config.get("provider_settings", {})
    # AstrBot 4.29 moved agent options from provider_settings to agent_runner.
    runner = config.get("agent_runner", {}).get("config", {})
    misc = runner.get("misc", settings)
    persona = runner.get("persona", {})
    plugins = config.get("plugin_set", ["*"])
    build = MainAgentBuildConfig(
        tool_call_timeout=misc.get("tool_call_timeout", 60),
        tool_schema_mode=misc.get("tool_schema_mode", "full"),
        streaming_response=streaming,
        kb_agentic_mode=config.get("kb_agentic_mode", False),
        computer_use_runtime=settings.get("computer_use_runtime", "none"),
        sandbox_cfg=settings.get("sandbox", {}),
        llm_safety_mode=persona.get(
            "safety_mode", settings.get("llm_safety_mode", True)
        ),
        safety_mode_strategy=persona.get(
            "safety_mode_strategy",
            settings.get("safety_mode_strategy", "system_prompt"),
        ),
        add_cron_tools=settings.get("proactive_capability", {}).get(
            "add_cron_tools", True
        ),
        provider_settings={
            **settings,
            "default_personality": job.persona,
            "plugin_set": plugins,
            "fallback_chat_models": [],
        },
        timezone=config.get("timezone"),
    )
    steps = misc.get("max_steps", settings.get("max_agent_step", 30))
    return build, steps if type(steps) is int and steps > 0 else 30


async def _hook(event, kind, *args):
    stopped = await call_event_hook(event, kind, *args)
    # Some framework versions catch cancellation inside third-party hooks.
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError
    if stopped:
        if result := event.get_result():
            await event.send(result)
        if not event.replies:
            raise SimulationToolError("插件在请求模型前结束了本轮群聊")
    return stopped


async def _run_group_agent(context, event, job, messages, system, provider, streaming):
    config = context.get_config(umo=event.unified_msg_origin)
    plugins = config.get("plugin_set", ["*"])
    event.plugins_name = None if plugins == ["*"] else list(plugins)
    if await _hook(event, EventType.OnWaitingLLMRequestEvent):
        return
    build_config, max_steps = _agent_config(config, job, streaming)
    conversation = Conversation(
        platform_id=event.get_platform_id(),
        user_id=event.unified_msg_origin,
        cid=event.conversation_id,
        persona_id=job.persona,
    )
    req = ProviderRequest(
        conversation=conversation,
        contexts=messages[:-1],
        prompt=job.question,
        system_prompt=(system + "\n" if job.prompt_source == SEED_SOURCE else "")
        + "当前在模拟群聊中回复一位普通群成员。",
    )
    event.set_extra("provider_request", req)
    result = await build_main_agent(
        event=event,
        plugin_context=context,
        config=build_config,
        provider=provider,
        req=req,
        apply_reset=False,
    )
    if result is None:
        raise SimulationToolError("AstrBot 未能构建本轮群聊请求")
    # Exactly as in the native pipeline, let plugins filter tools and change
    # prompts before the runner takes its snapshot of the request.
    reset = result.reset_coro
    try:
        if await _hook(event, EventType.OnLLMRequestEvent, result.provider_request):
            return
        await reset
    finally:
        reset.close()

    runner = result.agent_runner
    job.progress("模型正在回复")
    async for response in runner.step_until_done(max_steps):
        chain = response.data["chain"]
        if response.type == "tool_call":
            data = getattr(chain.chain[0], "data", {}) if chain.chain else {}
            if isinstance(data, dict) and (name := data.get("name")):
                job.tool_calls.append(name)
                job.progress(f"正在调用 {name}")
        elif response.type == "tool_call_result":
            if chain.type == "tool_direct_result":
                await event.send(chain)
            job.progress("工具已返回，模型正在继续回复")
        elif response.type == "streaming_delta" and chain.type != "reasoning":
            chunk = chain.get_plain_text()
            job.answer += chunk
            if chunk.strip() and job.first_text_at is None:
                job.first_text_at = time.monotonic()
    final = runner.get_final_llm_resp()
    if final is None and event.replies:
        return
    if final is None or final.role == "err":
        raise SimulationToolError(
            "群聊模型调用失败，请检查模型的工具调用支持与上下文长度"
        )
    answer = (final.completion_text or "").strip()
    if answer and (not event.replies or event.replies[-1] != answer):
        event.replies.append(answer)


async def group_reply(
    context, plugin_config, job, messages, system, provider_id, engine
):
    event = SimulatedGroupEvent(job, provider_id if job.model == CONFIGURED else "")
    with ExitStack() as cleanup:
        # Newer hosts bind native message tools to the current event.
        if bind := getattr(astr_message_event, "event_context", None):
            cleanup.enter_context(bind(event))
        else:
            cleanup.callback(event.cleanup_temporary_local_files)
        async with _provider(
            context, plugin_config, job, provider_id, engine
        ) as provider:
            await _run_group_agent(
                context,
                event,
                job,
                messages,
                system,
                provider,
                streaming=engine is not None,
            )
    return "\n\n".join(event.replies)
