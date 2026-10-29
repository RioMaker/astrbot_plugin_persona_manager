"""Group discovery stores metadata only, independently of chat/learning routing."""

import asyncio
import re
import time
from collections import OrderedDict


def clean_name(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:200]


def group_metadata(event):
    # Do not inspect message text, sender identity, quoted content or credentials.
    if event.is_private_chat() or event.get_platform_name() != "aiocqhttp":
        return None
    group_id, bot_id = str(event.get_group_id() or ""), str(event.get_self_id() or "")
    platform_id = str(event.get_platform_id() or "")
    if not all(re.fullmatch(r"[1-9]\d{3,19}", item) for item in (group_id, bot_id)):
        return None
    if not re.fullmatch(r"[^\s:]{1,100}", platform_id):
        return None
    obj = event.message_obj
    group = getattr(obj, "group", None)
    name = ""
    if str(getattr(group, "group_id", "")) == group_id:
        name = clean_name(getattr(group, "group_name", ""))
    raw = getattr(obj, "raw_message", None)
    if not name and isinstance(raw, dict) and str(raw.get("group_id", "")) == group_id:
        name = clean_name(raw.get("group_name"))
    return {
        "platform_id": platform_id,
        "bot_id": bot_id,
        "group_id": group_id,
        "group_name": name,
    }


class GroupDiscovery:
    def __init__(self, runtime, *, clock=time.time):
        self.runtime = runtime
        self.clock = clock
        self.closed = False
        self.seen = OrderedDict()
        self.tasks = {}
        self.lock = asyncio.Lock()

    async def observe(self, event):
        if self.closed or not self.runtime.settings().enabled:
            return
        info = group_metadata(event)
        if info is None:
            return
        key = (info["platform_id"], info["group_id"])
        now = self.clock()
        async with self.lock:
            if self.closed:
                return
            previous = self.seen.get(key)
            fingerprint = (info["bot_id"], info["group_name"])
            if previous and previous[1] == fingerprint and now - previous[0] < 60:
                return
            row = await self.runtime.db("record_group", info, now=now)
            self.seen[key] = (now, fingerprint)
            self.seen.move_to_end(key)
            if len(self.seen) > 2000:
                self.seen.popitem(last=False)
            client = getattr(event, "bot", None)
            call = getattr(client, "call_action", None)
            if (
                callable(call)
                and not self.closed
                and key not in self.tasks
                and len(self.tasks) < 8
                and row["name_retry_at"] <= now
            ):
                # Fetch metadata in the background; never delay the gate on a QQ API.
                self.tasks[key] = asyncio.create_task(self._refresh(key, info, call))
                self.tasks[key].add_done_callback(self._lookup_done)

    def _lookup_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.runtime.logger.warning(
                "[可可子人格] 群信息缓存更新失败，后续消息将重试。"
            )

    async def _refresh(self, key, info, call):
        try:
            result = await asyncio.wait_for(
                call(
                    "get_group_info",
                    group_id=int(info["group_id"]),
                    self_id=info["bot_id"],
                ),
                timeout=5,
            )
            if (
                not isinstance(result, dict)
                or str(result.get("group_id", "")) != info["group_id"]
            ):
                raise ValueError("Mismatched group metadata")
            name = clean_name(result.get("group_name"))
            if not name:
                raise ValueError("Missing group name")
            count = result.get("member_count")
            if type(count) is not int or not 0 <= count <= 1000000:
                count = None
            await self.runtime.db(
                "update_group_info",
                info,
                name=name,
                member_count=count,
                now=self.clock(),
                retry_seconds=86400,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # No raw API error logging: adapter messages can contain private data.
            await self.runtime.db(
                "update_group_info", info, now=self.clock(), retry_seconds=3600
            )
        finally:
            self.tasks.pop(key, None)

    async def close(self):
        self.closed = True
        # A listener already holding the lock must finish before the DB is closed.
        async with self.lock:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.tasks.clear()
            self.seen.clear()
