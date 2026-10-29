"""Small, fixture-installed AstrBot doubles; never installed at import time."""

from __future__ import annotations

import enum
import logging
import sys
import time
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock


class ResultContentType(enum.Enum):
    GENERAL_RESULT = enum.auto()
    LLM_RESULT = enum.auto()
    AGENT_RUNNER_ERROR = enum.auto()
    STREAMING_RESULT = enum.auto()
    STREAMING_FINISH = enum.auto()


@dataclass
class Plain:
    text: str


@dataclass
class At:
    qq: str


@dataclass
class Reply:
    id: str = "quoted-message"
    chain: list | None = None
    text: str = "引用内容不属于发送者原文"


class TextPart:
    def __init__(self, text="", **kwargs):
        self.text = text
        self.temporary = False
        self.is_temp = False
        self.__dict__.update(kwargs)

    def mark_as_temp(self):
        self.temporary = True
        self.is_temp = True
        return self


class MockEvent:
    def __init__(
        self,
        text="以后回答简短",
        *,
        user_id="123456",
        group_id="234567",
        bot_id="345678",
        platform_id="qq-main",
        platform_name="aiocqhttp",
        message_id="message-1",
        timestamp=None,
        private=False,
        stopped=False,
        persona_id="可可子",
        chain=None,
        extras=None,
        wake=True,
    ):
        self.private = private
        self.stopped = stopped
        self.stop_calls = 0
        self.platform_id = platform_id
        self.platform_name = platform_name
        self.persona_id = persona_id
        self.extras = dict(extras or {})
        self.is_at_or_wake_command = wake
        self.is_wake = wake
        self.call_llm = False
        # Most entry tests call the post-send hook directly after an LLM result.
        self._has_send_oper = True
        self.sent = []
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message=chain if chain is not None else [Plain(text)],
            message_str=text,
            message_id=message_id,
            timestamp=time.time() if timestamp is None else timestamp,
            group_id=group_id,
            self_id=bot_id,
            sender=SimpleNamespace(user_id=user_id, nickname="测试用户", role="member"),
            type="FriendMessage" if private else "GroupMessage",
            raw_message={},
        )
        self.unified_msg_origin = (
            f"{platform_id}:{'FriendMessage' if private else 'GroupMessage'}:"
            f"{user_id if private else group_id}"
        )
        self.platform_meta = SimpleNamespace(
            id=platform_id, name=platform_name, description="Test platform"
        )
        self._result = SimpleNamespace(
            chain=[Plain("收到")], result_content_type=ResultContentType.LLM_RESULT
        )

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stop_calls += 1
        self.stopped = True

    def is_private_chat(self):
        return self.private

    def is_admin(self):
        return False

    def should_call_llm(self, value):
        self.call_llm = value

    def plain_result(self, text):
        return SimpleNamespace(
            chain=[Plain(text)], result_content_type=ResultContentType.GENERAL_RESULT
        )

    def get_group_id(self):
        return self.message_obj.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_platform_id(self):
        return self.platform_id

    def get_platform_name(self):
        return self.platform_name

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key=None, default=None):
        return self.extras if key is None else self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self._result

    async def send(self, result):
        self.sent.append(result)
        self._has_send_oper = True


class Query(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class WebRequest:
    def __init__(self):
        self.username = "dashboard-admin"
        self.query = Query()
        self.payload = {}
        self.method = "GET"

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


class WebResponse(dict):
    def __init__(self, data, status_code=200):
        super().__init__(data)
        self.status_code = status_code

    async def get_json(self):
        return dict(self)


class PersonaManager:
    def __init__(self):
        self.personas = {
            "可可子": SimpleNamespace(
                persona_id="可可子",
                system_prompt="原生核心不得自动覆盖",
                begin_dialogs=["用户开场", "角色开场"],
                tools=["query_rtlink", "cast_liuyao"],
            ),
            "其他人格": SimpleNamespace(
                persona_id="其他人格", system_prompt="其他核心", tools=[]
            ),
        }
        self.updates = []

    async def get_persona(self, persona_id):
        return self.personas.get(persona_id)

    async def get_all_personas(self):
        return list(self.personas.values())

    async def update_persona(self, persona_id, **kwargs):
        self.updates.append((persona_id, kwargs))
        persona = self.personas[persona_id]
        for name, value in kwargs.items():
            setattr(persona, name, value)
        return persona

    async def resolve_persona(self, *args, **kwargs):
        event = kwargs.get("event")
        if event is None:
            event = next((arg for arg in args if isinstance(arg, MockEvent)), None)
        persona_id = getattr(event, "persona_id", "可可子")
        return self.personas.get(persona_id)

    async def resolve_selected_persona(
        self, *, umo, conversation_persona_id, platform_name
    ):
        persona_id = conversation_persona_id or "可可子"
        return persona_id, self.personas.get(persona_id), None, None


class MockContext:
    def __init__(self, *, access_gate=True):
        self.persona_manager = PersonaManager()
        self.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation-1"),
            get_conversation=AsyncMock(
                return_value=SimpleNamespace(persona_id="可可子", history=[])
            ),
        )
        self.routes = {}
        self.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text='{"proposals":[]}')
        )
        self.get_current_chat_provider_id = AsyncMock(return_value="existing-model")
        self.stars = (
            [
                SimpleNamespace(
                    name="astrbot_plugin_access_gate",
                    activated=True,
                    enabled=True,
                    module_path="astrbot_plugin_access_gate",
                    star_cls=SimpleNamespace(
                        _bool=lambda key, default=True: default,
                        _entries=lambda key: (
                            {"234567", "456789"} if key == "group_whitelist" else set()
                        ),
                    ),
                )
            ]
            if access_gate
            else []
        )
        self.provider_cfg = {"persona_id": "可可子"}
        self.cfg = {"provider_settings": self.provider_cfg}
        self.provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text='{"should_reply":true}')
            )
        )

    def get_all_stars(self):
        return self.stars

    def get_config(self, *args, **kwargs):
        return self.cfg

    def get_using_provider(self, *args, **kwargs):
        return self.provider

    def register_web_api(self, route, handler, methods, description=""):
        self.routes[route] = handler


def install(monkeypatch, data_path):
    """Install doubles only for this monkeypatch fixture's lifetime."""
    modules = {}

    def module(name):
        if name not in modules:
            item = ModuleType(name)
            item.__path__ = []
            modules[name] = item
            monkeypatch.setitem(sys.modules, name, item)
            if "." in name:
                parent, child = name.rsplit(".", 1)
                setattr(module(parent), child, item)
        return modules[name]

    def decorator(kind):
        def factory(*args, **kwargs):
            def wrap(function):
                records = list(getattr(function, "_astrbot_filters", []))
                records.append((kind, args, kwargs))
                function._astrbot_filters = records
                return function

            return wrap

        return factory

    filters = SimpleNamespace(
        EventMessageType=SimpleNamespace(ALL="all", GROUP_MESSAGE="group"),
        PermissionType=SimpleNamespace(ADMIN="admin"),
    )
    for name in (
        "event_message_type",
        "on_llm_request",
        "after_message_sent",
        "command",
        "permission_type",
        "on_decorating_result",
        "on_llm_response",
    ):
        setattr(filters, name, decorator(name))

    class Star:
        def __init__(self, context, *args, **kwargs):
            self.context = context

    api = module("astrbot.api")
    api.logger = logging.getLogger("persona-manager-test")
    api.AstrBotConfig = dict
    event = module("astrbot.api.event")
    event.filter = filters
    event.AstrMessageEvent = MockEvent
    event.MessageEventResult = SimpleNamespace
    star = module("astrbot.api.star")
    star.Context = MockContext
    star.Star = Star
    star.register = decorator("register")
    command = module("astrbot.core.star.filter.command")
    command.GreedyStr = str
    session = module("astrbot.core.star.session_plugin_manager")
    session.SessionPluginManager = SimpleNamespace(
        is_plugin_enabled_for_session=AsyncMock(return_value=True)
    )
    services = module("astrbot.core.star.session_llm_manager")
    services.SessionServiceManager = SimpleNamespace(
        is_llm_enabled_for_session=AsyncMock(return_value=True),
        is_session_enabled=AsyncMock(return_value=True),
    )
    result = module("astrbot.core.message.message_event_result")
    result.ResultContentType = ResultContentType
    components = module("astrbot.api.message_components")
    components.Plain = Plain
    components.At = At
    components.Reply = Reply
    content = module("astrbot.core.message.message")
    content.TextPart = TextPart
    content.Message = SimpleNamespace
    agent_message = module("astrbot.core.agent.message")
    agent_message.TextPart = TextPart
    provider = module("astrbot.api.provider")
    provider.ProviderRequest = SimpleNamespace
    provider.LLMResponse = SimpleNamespace
    entity = module("astrbot.core.provider.entities")
    entity.ProviderRequest = SimpleNamespace
    entity.LLMResponse = SimpleNamespace
    entity.TextPart = TextPart
    paths = module("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_data_path = lambda: str(data_path)
    paths.get_astrbot_plugin_data_path = lambda: str(data_path / "plugin_data")
    request = WebRequest()
    web = module("astrbot.api.web")
    web.request = request
    web.json_response = lambda data, status_code=200: WebResponse(data, status_code)
    web.error_response = lambda message, status_code=400: WebResponse(
        {"status": "error", "message": message}, status_code
    )
    return SimpleNamespace(
        context=MockContext, event=MockEvent, request=request, modules=modules
    )
