"""Observe after Access Gate; enrich only its actual group chat requests."""

import asyncio
import copy
import inspect
import json
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .configuration import page_config, revision, usage_for_page, validate_changes
from .groups import GroupDiscovery
from .ingress import group_identity, sample
from .runtime import CONTEXT_MARKER, Runtime
from .settings import Settings
from .storage import Store
from .temporal import BEIJING

PLUGIN_NAME = "astrbot_plugin_persona_manager"
GATE_NAME = "astrbot_plugin_access_gate"
PENDING_KEY = PLUGIN_NAME + ":interaction"
SENT_KEY = PLUGIN_NAME + ":sent"


class PersonaManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / PLUGIN_NAME
            / "persona.sqlite3"
        )
        self.store = Store(path)
        self.runtime = Runtime(
            self.store,
            context,
            self.settings,
            logger,
            source_guard=self._source_allowed,
        )
        self._debug = {}
        self.discovery = GroupDiscovery(self.runtime)
        for endpoint, method, handler in (
            ("state", "GET", self.page_state),
            ("seed", "GET", self.page_seed),
            ("action", "POST", self.page_action),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", handler, [method], "可可子人格管理"
            )

    def settings(self):
        return Settings.read(self.config)

    async def initialize(self):
        self.runtime.start()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10002)
    async def discover_groups(self, event: AstrMessageEvent):
        """Before Access Gate: metadata only, never read chat bodies or wake the bot."""
        try:
            await self.discovery.observe(event)
        except Exception:
            logger.warning("[可可子人格] 群目录更新失败，继续原消息流程。")

    def _gate_loaded(self, event=None) -> bool:
        plugins = getattr(event, "plugins_name", None)
        if plugins is not None and plugins != ["*"] and GATE_NAME not in plugins:
            return False
        return any(
            getattr(star, "name", None) == GATE_NAME
            and getattr(star, "activated", False)
            and getattr(star, "star_cls", None) is not None
            for star in self.context.get_all_stars()
        )

    async def _gate_allowed(self, event) -> bool:
        if not self._gate_loaded(event):
            return False
        return await SessionPluginManager.is_plugin_enabled_for_session(
            event.unified_msg_origin, GATE_NAME
        )

    async def _source_allowed(self, source) -> bool:
        """Recheck queued sources without invoking or mutating the gate handlers."""
        umo = str(source.get("umo", ""))
        parts = umo.split(":", 2)
        if (
            len(parts) != 3
            or parts[0] != source.get("platform_id")
            or parts[1] != "GroupMessage"
            or not parts[2]
            or not self._gate_loaded()
        ):
            return False
        cfg = self.context.get_config(umo=umo)
        plugins = cfg.get("plugin_set", ["*"])
        if plugins != ["*"] and (
            GATE_NAME not in plugins or PLUGIN_NAME not in plugins
        ):
            return False
        for name in (GATE_NAME, PLUGIN_NAME):
            if not await SessionPluginManager.is_plugin_enabled_for_session(umo, name):
                return False
        if not cfg.get("provider_settings", {}).get("enable", True):
            return False
        if not await SessionServiceManager.is_session_enabled(umo):
            return False
        if not await SessionServiceManager.is_llm_enabled_for_session(umo):
            return False
        gate = next(
            star.star_cls
            for star in self.context.get_all_stars()
            if getattr(star, "name", None) == GATE_NAME
            and getattr(star, "activated", False)
        )
        # These are read-only helpers on the supported Access Gate. If its
        # contract changes, pause extraction rather than guessing permissions.
        if not callable(getattr(gate, "_bool", None)) or not callable(
            getattr(gate, "_entries", None)
        ):
            return False
        admin = str(source.get("user_id")) in {
            str(value) for value in cfg.get("admins_id", [])
        }
        if not (admin and gate._bool("group_admin_bypass", True)):
            if gate._bool("group_whitelist_enabled", True) and not (
                {str(source.get("group_id")), umo} & gate._entries("group_whitelist")
            ):
                return False
        return await self._selected_origin(umo, "aiocqhttp")

    async def _selected(self, event, req=None) -> bool:
        """Resolve real persona identity; never guess from system prompt text."""
        return await self._selected_origin(
            event.unified_msg_origin, event.get_platform_name(), req
        )

    async def _selected_origin(self, umo, platform_name, req=None) -> bool:
        conversation = getattr(req, "conversation", None)
        if conversation is None:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if cid:
                conversation = await manager.get_conversation(umo, cid)
        persona_id = getattr(conversation, "persona_id", None)
        manager = self.context.persona_manager
        resolver = getattr(manager, "resolve_selected_persona", None)
        if callable(resolver):
            persona_id, _, _, _ = await resolver(
                umo=umo,
                conversation_persona_id=persona_id,
                platform_name=platform_name,
            )
        elif persona_id is None:
            persona = manager.get_default_persona_v3(umo=umo)
            if inspect.isawaitable(persona):
                persona = await persona
            persona_id = persona.get("name") if isinstance(persona, dict) else None
        return persona_id == self.settings().persona_id

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9998)
    async def observe(self, event: AstrMessageEvent):
        """A passive listener, strictly after Access Gate's 10000/9999 handlers."""
        settings = self.settings()
        identity = group_identity(event, settings)
        if identity is None:
            return
        if not settings.observes(identity["group_id"], identity["platform_id"]):
            return
        try:
            if not await self._gate_allowed(event) or not await self._selected(event):
                return
            # An actual chat request will be recorded on its successful send hook.
            if event.get_extra("access_gate_original_text", None) is not None:
                return
            source = sample(event, settings, "observation")
            if source is not None:
                await self.runtime.capture(source)
        except Exception:
            logger.warning("[可可子人格] 旁听跳过；未修改事件或其他插件状态。")

    @filter.on_llm_request(priority=-100)
    async def on_request(self, event: AstrMessageEvent, req: ProviderRequest):
        settings = self.settings()
        if group_identity(event, settings) is None:
            return
        # Gate's trusted event extra is set only for approved main-chat requests.
        if event.get_extra("access_gate_original_text", None) is None:
            return
        if getattr(event, "call_llm", False):
            return
        # collect_initial_request caches the default chat request itself, while
        # explicit plugin requests are shallow-copied before invoking this hook.
        # Presence alone cannot distinguish them: normal chat also has this key.
        cached_request = event.get_extra("provider_request", None)
        if cached_request is not None and cached_request is not req:
            return
        try:
            if not await self._gate_allowed(event) or not await self._selected(
                event, req
            ):
                return
            source = sample(event, settings, "interaction")
            if source is None:
                return
            if settings.remembers(source["group_id"], source["platform_id"]):
                event.set_extra(PENDING_KEY, source)
            # Serialize with Dashboard mutations through the actual injection.
            async with self.runtime.lock:
                context, detail = await self.runtime.chat_context(source)
                # Replace only this plugin's own marked block on repeated hooks.
                parts = req.extra_user_content_parts
                parts[:] = [
                    part
                    for part in parts
                    if not (
                        isinstance(part, TextPart)
                        and getattr(part, "text", "").startswith(CONTEXT_MARKER)
                    )
                ]
                if context:
                    parts.append(TextPart(text=context).mark_as_temp())
                if len(self._debug) >= 100:
                    self._debug.pop(next(iter(self._debug)))
                self._debug[source["scope_key"]] = detail
        except Exception:
            logger.warning("[可可子人格] 上下文补充失败，继续原对话。")

    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        result = event.get_result()
        if (
            not result
            or getattr(result, "result_content_type", None)
            != ResultContentType.LLM_RESULT
            or not getattr(event, "_has_send_oper", False)
        ):
            return
        await self._record_interaction(event)

    @filter.on_decorating_result(priority=-100)
    async def after_stream(self, event: AstrMessageEvent):
        # RespondStage skips after_message_sent for streaming. The framework
        # creates STREAMING_FINISH only after the stream has been consumed.
        result = event.get_result()
        if (
            not result
            or getattr(result, "result_content_type", None)
            != ResultContentType.STREAMING_FINISH
            or not getattr(result, "chain", None)
            or not getattr(event, "_has_send_oper", False)
        ):
            return
        await self._record_interaction(event)

    async def _record_interaction(self, event):
        settings = self.settings()
        if group_identity(event, settings) is None or event.get_extra(SENT_KEY, False):
            return
        source = event.get_extra(PENDING_KEY, None)
        if not isinstance(source, dict):
            return
        if not settings.remembers(source["group_id"], source["platform_id"]):
            return
        try:
            if not await self._gate_allowed(event) or not await self._selected(event):
                return
            event.set_extra(SENT_KEY, True)
            await self.runtime.capture(source)
        except Exception:
            logger.warning("[可可子人格] 相处记忆记录跳过，消息已按原流程发送。")

    def _page_persona(self, value=None) -> str:
        persona_id = self.settings().persona_id
        if not persona_id or (value and str(value) != persona_id):
            raise ValueError("请选择插件配置中的目标人格")
        return persona_id

    @staticmethod
    def _id(value) -> int:
        if not re.fullmatch(r"[1-9]\d{0,17}", str(value)):
            raise ValueError("记录编号无效")
        return int(value)

    @staticmethod
    def _qq(value) -> str:
        value = str(value or "")
        if not re.fullmatch(r"[1-9]\d{3,19}", value):
            raise ValueError("请输入真实QQ号")
        return value

    async def _native_core(self, persona_id):
        persona = await self.context.persona_manager.get_persona(persona_id)
        content = persona.system_prompt
        if await self.runtime.db("get_core", persona_id) != content:
            await self.runtime.db("set_core", persona_id, content)
        return content

    async def _page_choices(self):
        choices = {"personas": [], "providers": []}
        try:
            personas = await self.context.persona_manager.get_all_personas()
            for persona in personas:
                identity = getattr(persona, "persona_id", None)
                if identity:
                    choices["personas"].append(
                        {"id": str(identity), "label": str(identity)}
                    )
        except Exception:
            pass  # Typed IDs remain usable if a selector API is unavailable.
        try:
            for provider in self.context.get_all_providers():
                config = provider.provider_config
                identity = config.get("id")
                if identity:
                    choices["providers"].append(
                        {"id": str(identity), "label": str(identity)}
                    )
        except Exception:
            pass
        return choices

    async def _save_page_config(self, payload):
        if payload.get("revision") != revision(self.config):
            return error_response(
                "配置已被其他页面修改，请重新载入并核对草稿", status_code=409
            )
        updated = validate_changes(self.config, payload.get("values"))
        changed = payload["values"]
        if "persona_id" in changed:
            persona = await self.context.persona_manager.get_persona(
                updated["persona_id"]
            )
            if persona is None:
                raise ValueError("目标原生人格不存在，请先在 AstrBot 创建")
        if "learning_provider_id" in changed and updated["learning_provider_id"]:
            providers = (await self._page_choices())["providers"]
            if updated["learning_provider_id"] not in {row["id"] for row in providers}:
                raise ValueError("学习模型不存在或未启用，请选择已启用的对话模型")
        # Persona/provider lookups may yield to a second Dashboard editor.
        if payload.get("revision") != revision(self.config):
            return error_response("配置已变化，请重新载入并核对草稿", status_code=409)
        old = copy.deepcopy(dict(self.config))
        async_save = getattr(self.config, "save_config_async", None)
        sync_save = getattr(self.config, "save_config", None)
        if not callable(async_save) and not callable(sync_save):
            raise ValueError("当前 AstrBot 配置保存接口不可用")
        # Invalidate in-flight extraction even if the direction later changes back.
        await self.runtime.db("invalidate")
        if payload.get("revision") != revision(self.config):
            return error_response("配置已变化，请重新载入并核对草稿", status_code=409)
        try:
            if callable(async_save):
                saved = await async_save(updated)
                if saved is False:
                    return error_response(
                        "保存被较新的配置覆盖，请刷新核对", status_code=409
                    )
            else:
                await asyncio.to_thread(sync_save, updated)
        except Exception:
            # AstrBot merges the mapping before disk IO. Restore failed writes in memory.
            for key, value in updated.items():
                if self.config.get(key) == value:
                    if key in old:
                        self.config[key] = old[key]
                    else:
                        self.config.pop(key, None)
            raise
        self._debug.clear()
        return json_response({"saved": True, "revision": revision(self.config)})

    async def page_state(self):
        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=403)
        try:
            persona_id = self._page_persona(request.query.get("persona_id", ""))
            user = request.query.get("user_id", "")
            user = self._qq(user) if user else ""
            scopes = await self.runtime.db("scopes", persona_id)
            scope = request.query.get("scope_key", "")
            if scope and scope not in {x["scope_key"] for x in scopes}:
                raise ValueError("群作用域不存在")
            core_error = ""
            try:
                core = await self._native_core(persona_id)
            except (ValueError, AttributeError):
                core = ""
                core_error = (
                    "目标原生人格不存在或接口不可用，请先在AstrBot中创建并选择人格。"
                )
            versions = await self.runtime.db("style_versions", scope) if scope else []
            for version in versions:
                active_ids = json.loads(version["active_ids"])
                labels = []
                for style_id in active_ids:
                    row = await self.runtime.db("get_style", style_id)
                    labels.append(row["expression"] if row else "已遗忘的表达")
                version["content"] = "、".join(labels) or "未启用学习表达"
            day = datetime.now(BEIJING).date().isoformat()
            usage = await self.runtime.db("usage_report", day, days=7)
            return json_response(
                {
                    "config": page_config(self.config),
                    "groups": await self.runtime.db("known_groups"),
                    "choices": await self._page_choices(),
                    "usage": usage_for_page(usage, self.settings()),
                    "schedule": self.runtime.scheduler_status,
                    "status": {
                        **await self.runtime.db("stats"),
                        "paused": await self.runtime.paused(),
                        "enabled": self.settings().enabled,
                        "learning_enabled": self.settings().learning_enabled,
                        "gate_loaded": self._gate_loaded(),
                        "private_chat": "disabled",
                        "last_error": self.runtime.last_error,
                        "last_learning_at": self.runtime.last_learning_at,
                        "core_error": core_error,
                        "debug": self._debug.get(scope, {}),
                    },
                    "persona": {
                        "id": persona_id,
                        "core": core,
                        "versions": await self.runtime.db("core_versions", persona_id),
                    },
                    "scopes": scopes,
                    "profile": {
                        "items": await self.runtime.db("profile", persona_id, user)
                        if user
                        else [],
                        "relationship": await self.runtime.db(
                            "relationship", persona_id, user
                        )
                        if user
                        else {},
                    },
                    "memories": await self.runtime.db(
                        "timeline", persona_id, user, limit=100
                    )
                    if user
                    else [],
                    "styles": await self.runtime.db(
                        "styles", scope, status=None, limit=100
                    )
                    if scope
                    else [],
                    "style_versions": versions,
                }
            )
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.warning("[可可子人格] 管理视图读取失败。")
            return error_response("读取失败，请检查插件日志", status_code=500)

    async def page_seed(self):
        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=403)
        content = (Path(__file__).parent / "persona_seed.txt").read_text(
            encoding="utf-8"
        )
        return json_response({"content": content})

    async def page_action(self):
        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=403)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求必须是对象", status_code=400)
        try:
            if payload.get("action") == "config_save":
                async with self.runtime.lock:
                    return await self._save_page_config(payload)
            persona_id = self._page_persona(payload.get("persona_id"))
            action = payload.get("action")
            async with self.runtime.lock:
                if action == "learning_pause":
                    if not isinstance(payload.get("paused"), bool):
                        raise ValueError("paused必须是布尔值")
                    await self.runtime.db("set_meta", "paused", payload["paused"])
                elif action in {"core_save", "core_rollback"}:
                    if action == "core_save":
                        content = payload.get("content")
                    else:
                        version_id = self._id(payload.get("version_id"))
                        versions = await self.runtime.db("core_versions", persona_id)
                        version = next(
                            (v for v in versions if v["id"] == version_id), None
                        )
                        if version is None:
                            raise ValueError("人格版本不存在")
                        content = version["content"]
                    if (
                        not isinstance(content, str)
                        or not content.strip()
                        or len(content) > 16000
                    ):
                        raise ValueError("人格正文需为1～16000字")
                    await self._native_core(persona_id)  # Back up the actual old core.
                    await self.context.persona_manager.update_persona(
                        persona_id, system_prompt=content
                    )  # Do not touch tools, skills, dialogs or other personas.
                    await self.runtime.db("set_core", persona_id, content)
                elif action in {"memory_forget", "memory_correct"}:
                    record_id = self._id(payload.get("memory_id"))
                    memory = await self.runtime.db("get_memory", record_id)
                    if memory is None or memory["persona_id"] != persona_id:
                        raise ValueError("记忆不存在")
                    if (
                        payload.get("user_id")
                        and str(payload["user_id"]) != memory["user_id"]
                    ):
                        raise ValueError("QQ与记忆不匹配")
                    if action == "memory_forget":
                        await self.runtime.db(
                            "forget", persona_id, memory["user_id"], record_id
                        )
                    else:
                        content = payload.get("content")
                        if (
                            not isinstance(content, str)
                            or not content.strip()
                            or len(content) > 600
                        ):
                            raise ValueError("纠正内容需为1～600字")
                        time_text = payload.get("time_expression")
                        if time_text is not None and (
                            not isinstance(time_text, str) or len(time_text) > 100
                        ):
                            raise ValueError("时间表达无效")
                        await self.runtime.db(
                            "correct",
                            record_id,
                            content,
                            actor=request.username,
                            time_text=time_text,
                            state=payload.get("state"),
                        )
                    self._debug.clear()
                elif action == "user_forget":
                    await self.runtime.db(
                        "forget", persona_id, self._qq(payload.get("user_id"))
                    )
                    self._debug.clear()
                elif action in {"style_disable", "style_rollback"}:
                    scopes = {
                        x["scope_key"]
                        for x in await self.runtime.db("scopes", persona_id)
                    }
                    if action == "style_disable":
                        style = await self.runtime.db(
                            "get_style", self._id(payload.get("style_id"))
                        )
                        if style is None or style["scope_key"] not in scopes:
                            raise ValueError("表达不存在")
                        if (
                            payload.get("scope_key")
                            and payload["scope_key"] != style["scope_key"]
                        ):
                            raise ValueError("群与表达不匹配")
                        await self.runtime.db(
                            "set_style_status", style["id"], "disabled"
                        )
                    else:
                        scope = payload.get("scope_key")
                        if scope not in scopes:
                            raise ValueError("群作用域不存在")
                        await self.runtime.db(
                            "rollback_styles",
                            scope,
                            self._id(payload.get("version_id")),
                        )
                    self._debug.clear()
                else:
                    raise ValueError("不支持的操作")
            return json_response({"saved": True})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.warning("[可可子人格] 管理操作失败。")
            return error_response("操作失败，请检查插件日志", status_code=500)

    async def terminate(self):
        self._debug.clear()
        await self.discovery.close()
        await self.runtime.close()
