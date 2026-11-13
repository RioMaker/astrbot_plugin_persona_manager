"""Evidence-bound chat operations over the existing memory store.

Tool arguments never supply an actor, persona, group or authorization decision.
AI may remember the sender's own current text without a storage request, unless
the sender declines. Other mutations still need explicit intent. Database checks
and mutations share one transaction; no files or extra model calls are created.
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime

from .ingress import own_text, sample
from .learning import _unsafe
from .memory_types import parse_memory_response, text
from .retrieval import overlap
from .temporal import BEIJING

_INTENTS = {
    "correct": r"纠正|更正|改成|改为|记错|修改.{0,12}记忆|correct|update.{0,15}memor",
    "forget": r"忘记|忘掉|遗忘|删除|别记|不要记|forget|delete.{0,15}memor",
    "knowledge_confirm": r"灌输|(?:确认|核实).{0,12}(?:知识|事实)|(?:知识|事实).{0,12}(?:确认|核实)",
    "dream": r"入梦|清理.{0,8}日常记忆",
    "consolidate": r"整理.{0,15}记忆|总结.{0,15}记忆|记忆.{0,15}整理",
}


def _declines_memory(body):
    # Quoted instructions do not express the current sender's preference.
    plain = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", body)
    return bool(
        re.search(
            r"(?:不要|别|不许|不必|无需|不用|不需要|禁止|不想|不能|不准|勿).{0,12}"
            r"(?:记住|记下|记着|记牢|记录|记在|记入|记进|记到|保存|存储|储存|留存|写入.{0,4}记忆)"
            r"|(?:不要|别|不用|不必|无需)记(?:了|[，。！？!?\s]|$)"
            r"|\b(?:don['’]t|do\s+not|never)\b.{0,20}\b(?:remember|memorize|memorise|save|store|record)\b",
            plain,
            re.I,
        )
    )


def _intent(body, action):
    # A quoted/forwarded instruction is not the sender's own instruction.
    plain = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", body)
    if re.search(
        r"\b(?:don't|do not|never)\b.{0,20}\b(?:forget|delete|remember|memorize|correct|update)\b",
        plain,
        re.I,
    ):
        return False
    if re.search(
        r"(?:如何|怎么|为什么|什么是|会不会).{0,12}(?:记住|入梦|遗忘|删除|纠正|整理)",
        plain,
    ):
        return False
    if re.search(r"(?:记住|保存|删除|入梦)(?:了没|了吗|过吗|没有[？?]?$)", plain):
        return False
    if action == "forget" and re.search(
        r"(?:不要|别|不许|不用|无需|禁止|不想).{0,4}(?:删除|忘记|忘掉|遗忘)|我(?:已经|已|又|还)?忘(?:记|掉|了)",
        plain,
    ):
        return False
    if action != "forget" and re.search(
        r"(?:不要|别|不许|无需|不用|禁止|不想).{0,6}(?:记住|记录|保存|入梦|整理|纠正|更正|修改|灌输|确认)",
        plain,
    ):
        return False
    if re.search(r"(?:他说|她说|转述|引用).{0,12}(?:记住|入梦|删除|忘掉)", plain):
        return False
    if action == "dream":
        return bool(
            re.search(
                r"(?:^|[，,。!！；;：:\s])(?:请|现在|立即|马上|开始|帮我|给我|可可子|可可|执行|手动|确认|同意|允许|去|先|你)*[，,\s]*(?:入梦|清理日常记忆)(?:一下|吧|[。！!，,:：\s]|$)",
                plain,
            )
        )
    return bool(re.search(_INTENTS[action], plain, re.I))


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}须为正整数；先查询实际记录编号和版本。")
    return value


def _view(row):
    keys = (
        "id",
        "revision",
        "key",
        "content",
        "category",
        "class_id",
        "class_name",
        "status",
        "hold_reason",
        "provenance",
        "accuracy",
        "expires_at",
        "confirmations",
        "learned_at",
    )
    return {key: row.get(key) for key in keys}


class MemoryTools:
    def __init__(self, plugin):
        self.plugin = plugin
        self.runtime = plugin.runtime
        self.store = plugin.store

    async def _authorize(self, event):
        settings = self.plugin.settings()
        source = sample(event, settings, "interaction")
        if (
            source is None
            or not self.plugin._memory_request_authorized(event, source)
            or not settings.remembers(source["group_id"], source["platform_id"])
            or not await self.plugin._gate_allowed(event)
            or not await self.plugin._selected(event)
            or not await self.plugin._source_allowed(source)
            or self.runtime.closed
            or settings != self.plugin.settings()
        ):
            raise ValueError("当前会话未获记忆权限；仅门禁放行的目标人格群聊可调用。")
        return source, settings

    def _admin(self, event):
        check = getattr(event, "is_admin", None)
        if callable(check) and check() is True:
            return True
        config = self.plugin.context.get_config(umo=event.unified_msg_origin)
        return str(event.get_sender_id()) in {
            str(value) for value in config.get("admins_id", [])
        }

    async def execute(self, event, action, **arguments):
        """Return a bounded result; errors are never represented as successful writes."""
        if action not in {*_INTENTS, "remember", "search", "status"}:
            return {"ok": False, "status": "denied", "error": "未知记忆操作。"}
        try:
            async with self.runtime.lock:
                source, settings = await self._authorize(event)
                if action == "remember" and _declines_memory(own_text(event)):
                    raise ValueError("当前发送者明确表示不要记忆，未保存。")
                if action in _INTENTS and not _intent(own_text(event), action):
                    raise ValueError("当前发送者没有明确要求此操作，请勿自行修改记忆。")
                if action in {"dream", "knowledge_confirm"}:
                    if not self._admin(event):
                        raise ValueError("此操作仅 AstrBot 管理员可执行。")
                    if arguments.get("confirm") is not True:
                        raise ValueError(
                            "请先说明作用范围并取得用户确认，再传 confirm=true。"
                        )
                if action == "forget" and arguments.get("confirm") is not True:
                    raise ValueError(
                        "请先展示将遗忘的记录并取得确认，再传 confirm=true。"
                    )
                if action in {"remember", "correct", "consolidate"} and (
                    not settings.learning_enabled or await self.runtime.paused()
                ):
                    raise ValueError(
                        "学习已暂停或关闭，未写入记忆；请由管理员恢复学习。"
                    )
                epoch = await self.runtime.db("epoch")
                if action != "consolidate":
                    return await asyncio.to_thread(
                        self._perform, source, settings, epoch, action, arguments
                    )
            # Never retain runtime.lock while the scheduler acquires it itself.
            await self.runtime.run_once(scope_key=source["scope_key"])
            async with self.runtime.lock:
                source, settings = await self._authorize(event)
                result = await asyncio.to_thread(
                    self._perform,
                    source,
                    settings,
                    await self.runtime.db("epoch"),
                    "status",
                    {},
                )
            result["status"] = "checked"
            result["message"] = (
                "已检查本群整理条件；是否执行以调度状态为准，未绕过数量、间隔、时段或预算。"
            )
            return result
        except (ValueError, TypeError, KeyError) as error:
            return {"ok": False, "status": "denied", "error": str(error)}

    def _perform(self, source, settings, epoch, action, args):
        with self.store.transaction():
            if self.store.epoch() != epoch or self.plugin.settings() != settings:
                raise ValueError("权限或记忆状态已更新，请重新查询后重试。")
            if action == "search":
                return self._search(source, settings, args)
            if action == "status":
                return self._status(source, settings)
            if action == "remember":
                return self._remember(source, settings, epoch, args)
            if action == "correct":
                return self._correct(source, settings, args)
            if action == "forget":
                row = self._owned(source, args)
                count = self.store.forget(
                    source["persona_id"], source["user_id"], row["id"]
                )
                return {
                    "ok": True,
                    "status": "forgotten",
                    "memory_id": row["id"],
                    "count": count,
                }
            if action == "knowledge_confirm":
                return self._knowledge(source, settings, args)
            if action == "dream":
                receipt = "memory_tool:dream:" + source["id"]
                old = self.store.get_meta(receipt, None)
                if old is not None:
                    return {
                        "ok": True,
                        "status": "dreamed",
                        "count": old,
                        "replayed": True,
                    }
                count = self.store.dream(source["persona_id"])
                self.store.set_meta(receipt, count)
                return {
                    "ok": True,
                    "status": "dreamed",
                    "count": count,
                    "last_dream": self.store.get_meta(
                        "last_dream:" + source["persona_id"], None
                    ),
                }
            raise ValueError("未知记忆操作。")

    def _quoted(self, source, args):
        content = text(args.get("content"))
        evidence = text(args.get("evidence") or args.get("quote") or content)
        if content not in source["text"] or evidence not in source["text"]:
            raise ValueError(
                "内容与证据必须逐字来自发送者本条消息；不可保存推测、引用或文件内容。"
            )
        if content not in evidence:
            raise ValueError("证据必须包含需要保存或纠正的完整内容。")
        if _unsafe(content) or _unsafe(evidence):
            raise ValueError("内容包含敏感字段或控制指令，未保存。")
        return content, evidence

    def _visible(self, row, source, settings):
        if not row or row["persona_id"] != source["persona_id"]:
            return False
        if row["status"] not in {"active", "pending"}:
            return False
        if (
            row.get("expires_at") is not None
            and row["expires_at"] <= self.store.clock()
        ):
            return False
        if row["user_id"] not in {source["user_id"], "__knowledge__", "__self__"}:
            return False
        if row["status"] == "pending" and (
            (row.get("subject_user_id") or row["user_id"]) != source["user_id"]
            or not self.store._visible_memory(row, source["scope_key"], False)
        ):
            return False
        return self.store._visible_memory(
            row,
            source["scope_key"],
            settings.uses_public_memory(source["group_id"], source["platform_id"]),
        )

    def _search(self, source, settings, args):
        limit = args.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit须为1～10。")
        memory_id = args.get("memory_id", 0)
        if memory_id:
            row = self.store.get_memory(_positive(memory_id, "记录编号"))
            if not self._visible(row, source, settings):
                raise ValueError("记录不存在、已失效或当前会话无权读取。")
            return {"ok": True, "status": "found", "memories": [_view(row)]}
        query = text(args.get("query", ""), 200, False)
        rows = []
        if query:
            for owner in (source["user_id"], "__knowledge__", "__self__"):
                rows.extend(
                    self.store.recall(
                        source["persona_id"],
                        owner,
                        query,
                        limit=10,
                        scope_key=source["scope_key"],
                        public=settings.uses_public_memory(
                            source["group_id"], source["platform_id"]
                        ),
                    )
                )
        # Filter by scope and owner before the SQL limit, including own pending knowledge.
        sql = """SELECT m.id FROM memories m JOIN memory_traits t ON t.memory_id=m.id
            LEFT JOIN memory_details d ON d.memory_id=m.id
            WHERE m.persona_id=? AND m.status IN ('active','pending')
            AND (t.expires_at IS NULL OR t.expires_at>?)
            AND (m.user_id=? OR m.user_id IN ('__knowledge__','__self__'))
            AND (m.status='active' OR m.user_id=? OR d.subject_user_id=?)"""
        parameters = [
            source["persona_id"],
            self.store.clock(),
            source["user_id"],
            source["user_id"],
            source["user_id"],
        ]
        public = settings.uses_public_memory(source["group_id"], source["platform_id"])
        if not public:
            sql += """ AND (d.scope_key=? OR (COALESCE(d.scope_key,'')='' AND EXISTS
                (SELECT 1 FROM json_each(m.source_ids) j JOIN sources s ON s.id=j.value WHERE s.scope_key=?)))"""
            parameters.extend([source["scope_key"], source["scope_key"]])
        sql += " ORDER BY m.learned_at DESC,m.id DESC LIMIT 100"
        for entry in self.store.db.execute(sql, parameters).fetchall():
            row = self.store.get_memory(entry[0])
            if self._visible(row, source, settings) and (
                not query or overlap(query, row["key"] + " " + row["content"]) > 0
            ):
                rows.append(row)
        unique = {
            row["id"]: row for row in rows if self._visible(row, source, settings)
        }
        rows = sorted(
            unique.values(),
            key=lambda row: (
                overlap(query, row["key"] + " " + row["content"]),
                row["learned_at"],
                row["id"],
            ),
            reverse=True,
        )
        return {
            "ok": True,
            "status": "found" if rows else "empty",
            "memories": [_view(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
        }

    def _owned(self, source, args):
        row = self.store.get_memory(_positive(args.get("memory_id"), "记录编号"))
        revision = _positive(args.get("revision"), "版本")
        if (
            row is None
            or row["persona_id"] != source["persona_id"]
            or row["user_id"] != source["user_id"]
            or row.get("category") in {"knowledge", "self"}
            or row["status"] not in {"active", "pending"}
            or not self.store._visible_memory(row, source["scope_key"], False)
            or (
                row.get("expires_at") is not None
                and row["expires_at"] <= self.store.clock()
            )
        ):
            raise ValueError(
                "只能修改本人在当前群的有效记忆；知识库与自我认知请由管理员处理。"
            )
        if row["revision"] != revision:
            raise ValueError("记忆版本已变化，请重新查询后重试。")
        return row

    def _remember(self, source, settings, epoch, args):
        content, evidence = self._quoted(source, args)
        key = text(args.get("key"), 100)
        if _unsafe(key):
            raise ValueError("记忆主题包含控制指令或敏感字段。")
        category = args.get("category", "cache")
        if category not in {"timed", "daily", "cache", "relationship", "knowledge"}:
            raise ValueError(
                "请选择timed/daily/cache/relationship/knowledge；情绪和自我认知由后台依据实际回复整理。"
            )
        ttl = args.get("ttl_seconds", 1800)
        if type(ttl) is not int or not 1 <= ttl <= 2592000:
            raise ValueError("有效期须为1～2592000秒。")
        classes = self.store.classifiers(source["persona_id"])
        proposal = dict(
            class_id=category,
            key=key,
            content=content,
            subject_source_id=source["id"],
            evidence=[dict(source_id=source["id"], quote=evidence, role="user")],
            provenance="self_report",
            confidence=0.9,
            mood="",
            salience=0,
            details={},
            tags=[],
            ttl_seconds=ttl,
        )
        parsed = parse_memory_response(
            [source],
            json.dumps({"proposals": [proposal]}, ensure_ascii=False),
            classes,
            [],
        )
        self.store.add_source(source)
        saved_source = self.store.get_source(source["id"])
        if not saved_source or saved_source["status"] in {
            "forgotten",
            "expired",
            "reviewed",
        }:
            raise ValueError("本条来源已被清理或撤销，未重新记忆。")
        ids = self.store.apply_classified(
            parsed,
            epoch,
            cache_capacity=settings.short_cache_capacity,
            knowledge_confirmations=settings.knowledge_confirmations,
        )
        replayed = not ids
        if not ids:
            ids = [
                row[0]
                for row in self.store.db.execute(
                    """SELECT m.id FROM memories m JOIN memory_details d ON d.memory_id=m.id
                WHERE m.persona_id=? AND m.key=? AND m.content=? AND d.class_id=?
                AND EXISTS (SELECT 1 FROM json_each(m.source_ids) WHERE value=?)
                AND m.status IN ('active','pending') ORDER BY m.id DESC LIMIT 1""",
                    (source["persona_id"], key, content, category, source["id"]),
                )
            ]
        if not ids:
            raise ValueError("记录未被采纳，可能已到期、被清理或分类尚不可用。")
        row = self.store.get_memory(ids[0])
        if row["status"] not in {"active", "pending"}:
            raise ValueError("记忆已到期或被清理，未形成有效记录。")
        return self._saved(row, replayed=replayed)

    def _correct(self, source, settings, args):
        row = self._owned(source, args)
        if row["status"] != "active":
            raise ValueError("待确认记忆请在控制台调整；本工具只纠正生效记录。")
        content, evidence = self._quoted(source, args)
        self.store.add_source(source)
        saved_source = self.store.get_source(source["id"])
        if not saved_source or saved_source["status"] in {
            "forgotten",
            "expired",
            "reviewed",
        }:
            raise ValueError("纠错来源已失效。")
        mid = self.store.correct(
            row["id"],
            content,
            actor="qq:" + source["platform_id"] + ":" + source["user_id"],
        )
        self.store.db.execute(
            "UPDATE memories SET source_ids=? WHERE id=?",
            (json.dumps([source["id"]]), mid),
        )
        self.store.db.execute(
            "UPDATE memory_traits SET evidence=? WHERE memory_id=?",
            (
                json.dumps(
                    [dict(source_id=source["id"], quote=evidence, role="user")],
                    ensure_ascii=False,
                ),
                mid,
            ),
        )
        self.store.db.execute(
            "UPDATE memory_details SET provenance='self_report',accuracy='supported',details='{}',tags='[]' WHERE memory_id=?",
            (mid,),
        )
        return self._saved(self.store.get_memory(mid), replaced_memory_id=row["id"])

    def _knowledge(self, source, settings, args):
        content, _ = self._quoted(source, args)
        key = text(args.get("key"), 100)
        if _unsafe(key):
            raise ValueError("知识主题包含控制指令或敏感字段。")
        receipt = (
            "memory_tool:knowledge:"
            + hashlib.sha256(
                json.dumps([source["id"], key, content], ensure_ascii=False).encode()
            ).hexdigest()
        )
        previous = self.store.get_meta(receipt, None)
        if previous is not None:
            row = self.store.get_memory(previous)
            if not row or row["status"] != "active":
                raise ValueError("该次灌输已处理，记录后来已被修改或清理。")
            return self._saved(row, replayed=True)
        mid = self.store.force_knowledge(
            source["persona_id"],
            content,
            key,
            actor="qq:" + source["platform_id"] + ":" + source["user_id"],
        )
        self.store.set_meta(receipt, mid)
        return self._saved(
            self.store.get_memory(mid),
            public_memory_enabled=settings.uses_public_memory(
                source["group_id"], source["platform_id"]
            ),
        )

    @staticmethod
    def _saved(row, **extra):
        return {
            "ok": True,
            "status": row["status"],
            "memory_id": row["id"],
            "revision": row["revision"],
            "memory": _view(row),
            **extra,
        }

    def _status(self, source, settings):
        count = self.store.db.execute(
            """SELECT count(*) FROM learning_queue q JOIN sources s ON s.id=q.source_id
            WHERE q.lane='interaction' AND q.status='pending' AND s.status NOT IN ('forgotten','expired','reviewed')
            AND s.persona_id=? AND s.scope_key=? AND s.text<>''""",
            (source["persona_id"], source["scope_key"]),
        ).fetchone()[0]
        day = datetime.fromtimestamp(self.store.clock(), BEIJING).date().isoformat()
        groups = self.runtime.scheduler_status.get("groups", [])
        schedule = next(
            (
                row
                for row in reversed(groups)
                if row.get("scope_key") == source["scope_key"]
                and row.get("purpose") == "interaction"
            ),
            {},
        )
        return {
            "ok": True,
            "status": "available",
            "pending_messages": count,
            "threshold": settings.memory_batch_threshold,
            "paused": bool(self.store.get_meta("paused", False)),
            "learning_enabled": settings.learning_enabled,
            "public_memory_enabled": settings.uses_public_memory(
                source["group_id"], source["platform_id"]
            ),
            "last_dream": self.store.get_meta(
                "last_dream:" + source["persona_id"], None
            ),
            "usage": self.store.memory_usage(day),
            "limits": {
                "daily_mtokens": settings.memory_daily_mtokens,
                "monthly_mtokens": settings.memory_monthly_mtokens,
            },
            "schedule": {
                key: schedule[key]
                for key in ("state", "reason", "checked_at", "sample_count", "accepted")
                if key in schedule
            },
        }
