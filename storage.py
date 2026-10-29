"""Thread-safe SQLite storage. All model proposals are untrusted input."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .temporal import BEIJING, is_past_reference, resolve_time


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """Synchronous store; call from asyncio.to_thread in AstrBot handlers.

    A process-wide epoch invalidates pending extraction after corrections,
    deletion or retention cleanup. Source tombstones prevent replay learning.
    Every transaction and query is serialized on one connection.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('epoch',0);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, platform_id TEXT NOT NULL,
                bot_id TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                text TEXT NOT NULL, sent_at REAL NOT NULL, purpose TEXT NOT NULL,
                scope_key TEXT NOT NULL, stored_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                extra TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS sources_pending ON sources(status,scope_key,purpose,sent_at);
            CREATE TABLE IF NOT EXISTS source_tombstones (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL, user_id TEXT NOT NULL,
                kind TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
                state TEXT NOT NULL, source_ids TEXT NOT NULL, source_group TEXT NOT NULL,
                source_sent_at REAL NOT NULL, learned_at REAL NOT NULL,
                event_start REAL, event_end REAL, precision TEXT NOT NULL,
                valid_from REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                corrects_id INTEGER, fingerprint TEXT UNIQUE NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_person ON memories(persona_id,user_id,status,valid_from);
            CREATE TABLE IF NOT EXISTS styles (
                id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, expression TEXT NOT NULL,
                meaning TEXT NOT NULL, contexts TEXT NOT NULL, avoid TEXT NOT NULL,
                source_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(scope_key,expression));
            CREATE TABLE IF NOT EXISTS style_versions (
                id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, active_ids TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS core_versions (
                id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL, content TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cores (persona_id TEXT PRIMARY KEY, version_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (
                day TEXT NOT NULL, scope_key TEXT NOT NULL, used INTEGER NOT NULL,
                PRIMARY KEY(day,scope_key));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS style_activations (
                id INTEGER PRIMARY KEY,scope_key TEXT NOT NULL,style_id INTEGER NOT NULL,day TEXT NOT NULL);
        """)
        if "extra" not in {x[1] for x in self.db.execute("PRAGMA table_info(sources)")}:
            self.db.execute(
                "ALTER TABLE sources ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'"
            )
        if "time_text" not in {
            x[1] for x in self.db.execute("PRAGMA table_info(memories)")
        }:
            self.db.execute(
                "ALTER TABLE memories ADD COLUMN time_text TEXT NOT NULL DEFAULT ''"
            )
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def epoch(self) -> int:
        with self._lock:
            return int(
                self.db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[
                    0
                ]
            )

    get_epoch = epoch

    def _bump_epoch(self) -> None:
        self.db.execute("UPDATE meta SET value=value+1 WHERE key='epoch'")

    def add_source(self, source: dict) -> bool:
        fields = (
            "id",
            "persona_id",
            "platform_id",
            "bot_id",
            "group_id",
            "user_id",
            "text",
            "scope_key",
        )
        if any(
            source.get(x) is None or not str(source.get(x, "")).strip() for x in fields
        ):
            return False
        if source.get("purpose") not in {"observation", "interaction"}:
            return False
        try:
            sent_at = float(source["sent_at"])
            if not math.isfinite(sent_at):
                return False
        except (KeyError, ValueError, TypeError):
            return False
        try:
            extra = _json(
                {
                    key: value
                    for key, value in source.items()
                    if key not in {*fields, "sent_at", "purpose"}
                }
            )
        except (TypeError, ValueError):
            return False
        with self._lock, self.db:
            source_id = str(source["id"])
            if self.db.execute(
                "SELECT 1 FROM source_tombstones WHERE id=?", (source_id,)
            ).fetchone():
                return False
            values = [str(source[x]) for x in fields[:7]]
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO sources "
                "(id,persona_id,platform_id,bot_id,group_id,user_id,text,sent_at,purpose,scope_key,stored_at,extra) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *values,
                    sent_at,
                    source["purpose"],
                    str(source["scope_key"]),
                    self.clock(),
                    extra,
                ),
            )
            if cursor.rowcount:
                return True
            if source["purpose"] == "interaction":
                promoted = self.db.execute(
                    "UPDATE sources SET purpose='interaction',status='pending' WHERE id=? AND purpose='observation' "
                    "AND status NOT IN ('forgotten','expired','reviewed') AND persona_id=? AND user_id=? AND scope_key=?",
                    (
                        source_id,
                        source["persona_id"],
                        source["user_id"],
                        source["scope_key"],
                    ),
                )
                return promoted.rowcount == 1
            return False

    @staticmethod
    def _source_dict(row) -> dict:
        data = dict(row)
        extra = json.loads(data.pop("extra", "{}"))
        return {**extra, **data}

    def get_source(self, source_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM sources WHERE id=?", (str(source_id),)
            ).fetchone()
            return self._source_dict(row) if row else None

    def pending_sources(
        self, purpose=None, *, scope_key=None, persona_id=None, limit=50, after=None
    ) -> list[dict]:
        sql, args = "SELECT * FROM sources WHERE status='pending' AND text<>''", []
        for key, value in (
            ("purpose", purpose),
            ("scope_key", scope_key),
            ("persona_id", persona_id),
        ):
            if value is not None:
                sql += f" AND {key}=?"
                args.append(value)
        if after is not None:
            sql += " AND (sent_at>? OR (sent_at=? AND id>?))"
            args.extend((float(after[0]), float(after[0]), str(after[1])))
        sql += " ORDER BY sent_at,id LIMIT ?"
        args.append(max(0, min(int(limit), 1000)))
        with self._lock:
            return [self._source_dict(x) for x in self.db.execute(sql, args)]

    def mark_sources(self, ids, epoch: int, status="done") -> bool:
        if status not in {"done", "pending", "failed"}:
            raise ValueError("Unsupported source status")
        with self._lock, self.db:
            if epoch != self.epoch():
                return False
            self.db.executemany(
                "UPDATE sources SET status=? WHERE id=? AND status NOT IN ('forgotten','expired','reviewed')",
                ((status, str(x)) for x in ids),
            )
            return True

    def _proposal_sources(self, proposal: dict, *, interaction: bool) -> list[dict]:
        ids = proposal.get("source_ids", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            return []
        ids = list(dict.fromkeys(str(x) for x in ids))
        rows = [self.get_source(x) for x in ids]
        if any(
            not x
            or not x["text"]
            or x["status"] in {"forgotten", "expired", "reviewed"}
            for x in rows
        ):
            return []
        if interaction and any(x["purpose"] != "interaction" for x in rows):
            return []
        if len({x["persona_id"] for x in rows}) != 1:
            return []
        if interaction and len({x["user_id"] for x in rows}) != 1:
            return []
        if not interaction and len({x["scope_key"] for x in rows}) != 1:
            return []
        evidence = proposal.get("evidence", [])
        if evidence:
            if not isinstance(evidence, list):
                return []
            lookup = {x["id"]: x for x in rows}
            for item in evidence:
                if not isinstance(item, dict):
                    return []
                source = lookup.get(str(item.get("source_id", "")))
                quote = item.get("quote", "")
                if (
                    not source
                    or not isinstance(quote, str)
                    or not quote
                    or quote not in source["text"]
                ):
                    return []
        return rows

    def apply_memories(self, proposals: list[dict], epoch: int) -> list[int]:
        inserted = []
        with self._lock, self.db:
            if epoch != self.epoch():
                return []
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                sources = self._proposal_sources(proposal, interaction=True)
                kind, content = proposal.get("kind"), proposal.get("content")
                key = proposal.get("key")
                state = proposal.get("state", "asserted")
                if (
                    not sources
                    or kind not in {"preference", "fact", "goal", "event"}
                    or not isinstance(content, str)
                    or not 1 <= len(content.strip()) <= 1200
                    or not isinstance(key, str)
                    or not 1 <= len(key.strip()) <= 100
                    or state
                    not in {
                        "active",
                        "asserted",
                        "planned",
                        "in_progress",
                        "completed",
                        "cancelled",
                    }
                ):
                    continue
                source = max(sources, key=lambda x: (x["sent_at"], x["id"]))
                time_text = proposal.get("time_text") or ""
                if not isinstance(time_text, str) or len(time_text) > 100:
                    continue
                resolved = resolve_time(time_text, source["sent_at"])
                # A plan is known now; its scheduled date is not the date its status becomes current.
                effective = (
                    resolved["start"]
                    if resolved["start"] is not None
                    else source["sent_at"]
                )
                if (
                    state == "planned"
                    or (
                        state == "cancelled"
                        and (
                            "计划" in content
                            or (
                                resolved["start"] is not None
                                and resolved["start"] > source["sent_at"]
                            )
                        )
                    )
                    or (
                        resolved["start"] is not None
                        and resolved["start"] <= source["sent_at"] < resolved["end"]
                    )
                ):
                    effective = source["sent_at"]
                source_ids = sorted(x["id"] for x in sources)
                corrects = proposal.get("corrects_id")
                if corrects is not None:
                    if isinstance(corrects, bool) or not isinstance(corrects, int):
                        continue
                    old = self.db.execute(
                        "SELECT * FROM memories WHERE id=?", (corrects,)
                    ).fetchone()
                    if (
                        not old
                        or old["status"] != "active"
                        or (old["persona_id"], old["user_id"])
                        != (source["persona_id"], source["user_id"])
                    ):
                        continue
                    # Corrections must retain the target attribute's key.
                    key, kind = old["key"], old["kind"]
                fingerprint = hashlib.sha256(
                    _json(
                        [source_ids, kind, key, content.strip(), state, corrects]
                    ).encode()
                ).hexdigest()
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(persona_id,user_id,kind,key,content,state,source_ids,source_group,source_sent_at,learned_at,"
                    "event_start,event_end,precision,valid_from,corrects_id,fingerprint,time_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source["persona_id"],
                        source["user_id"],
                        kind,
                        key.strip(),
                        content.strip(),
                        state,
                        _json(source_ids),
                        source["group_id"],
                        source["sent_at"],
                        self.clock(),
                        resolved["start"],
                        resolved["end"],
                        resolved["precision"],
                        effective,
                        corrects,
                        fingerprint,
                        time_text,
                    ),
                )
                if cursor.rowcount:
                    inserted.append(cursor.lastrowid)
                    if corrects is not None:
                        self.db.execute(
                            "UPDATE memories SET status='corrected' WHERE id=?",
                            (corrects,),
                        )
            return inserted

    @staticmethod
    def _memory_dict(row) -> dict:
        result = dict(row)
        result["source_ids"] = json.loads(result["source_ids"])
        return result

    def profile(self, persona_id: str, user_id: str, limit=12) -> list[dict]:
        """Current attributes by effective time, not ingestion sequence."""
        if int(limit) <= 0:
            return []
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status='active' "
                "AND kind<>'event' AND valid_from<=? ORDER BY valid_from DESC,source_sent_at DESC,id DESC",
                (persona_id, user_id, self.clock()),
            )
            seen, output = set(), []
            for row in rows:
                # An undated historical statement is not a new current attribute.
                # Checking content also protects pre-migration records without time_text.
                if row["precision"] == "unknown":
                    if row["time_text"] not in {"", "最近", "目前", "现在", "以后"}:
                        continue
                    if is_past_reference(row["time_text"] or row["content"]):
                        continue
                # Knowing a future plan must not erase what the person is doing now.
                planning = row["state"] == "planned" or (
                    row["state"] == "cancelled"
                    and (
                        "计划" in row["content"]
                        or (
                            row["event_start"] is not None
                            and row["event_start"] > row["source_sent_at"]
                        )
                    )
                )
                attribute = (row["kind"], row["key"], planning)
                if attribute not in seen:
                    output.append(self._memory_dict(row))
                    seen.add(attribute)
                if len(output) >= max(0, int(limit)):
                    break
            return output

    def timeline(self, persona_id: str, user_id: str, limit=30) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status<>'forgotten' "
                "ORDER BY COALESCE(event_start,source_sent_at) DESC,source_sent_at DESC,id DESC LIMIT ?",
                (persona_id, user_id, max(0, int(limit))),
            )
            return [self._memory_dict(x) for x in rows]

    def forget(
        self, persona_id: str, user_id: str, memory_id: int | None = None
    ) -> int:
        """Remove evidence and every derived item; retain only non-content replay tombstones."""
        with self._lock, self.db:
            self._bump_epoch()
            if memory_id is None:
                source_ids = {
                    x[0]
                    for x in self.db.execute(
                        "SELECT id FROM sources WHERE persona_id=? AND user_id=?",
                        (persona_id, user_id),
                    )
                }
            else:
                row = self.db.execute(
                    "SELECT source_ids FROM memories WHERE id=? AND persona_id=? AND user_id=?",
                    (memory_id, persona_id, user_id),
                ).fetchone()
                if not row:
                    return 0
                source_ids = set(json.loads(row[0]))
            self.db.executemany(
                "INSERT OR IGNORE INTO source_tombstones VALUES (?)",
                ((x,) for x in source_ids),
            )
            self.db.executemany(
                "UPDATE sources SET text='',extra='{}',status='forgotten' WHERE id=?",
                ((x,) for x in source_ids),
            )
            count = 0
            for row in self.db.execute(
                "SELECT id,source_ids FROM memories WHERE persona_id=? AND user_id=? AND status<>'forgotten'",
                (persona_id, user_id),
            ).fetchall():
                if memory_id is None or source_ids.intersection(
                    json.loads(row["source_ids"])
                ):
                    self.db.execute(
                        "UPDATE memories SET content='',key='',time_text='',status='forgotten' WHERE id=?",
                        (row["id"],),
                    )
                    count += 1
            for row in self.db.execute(
                "SELECT id,source_ids FROM styles WHERE status<>'forgotten'"
            ).fetchall():
                if source_ids.intersection(json.loads(row["source_ids"])):
                    self.db.execute(
                        "UPDATE styles SET expression=?,meaning='',contexts='[]',avoid='[]',status='forgotten' WHERE id=?",
                        (f"__forgotten__:{row['id']}", row["id"]),
                    )
            return count

    def get_memory(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM memories WHERE id=? AND status<>'forgotten'",
                (memory_id,),
            ).fetchone()
            return self._memory_dict(row) if row else None

    def correct(
        self, memory_id: int, content: str, *, actor="admin", time_text=None, state=None
    ) -> int:
        """Explicit authorized correction. Actor is audit context, not an authorization check."""
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 1200:
            raise ValueError("Correction content is empty or too long")
        if state is not None and state not in {
            "active",
            "asserted",
            "planned",
            "in_progress",
            "completed",
            "cancelled",
        }:
            raise ValueError("Invalid memory state")
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM memories WHERE id=? AND status='active'", (memory_id,)
            ).fetchone()
            if not row:
                raise ValueError("Memory does not exist or is not active")
            self._bump_epoch()
            now = self.clock()
            # Editing an old description does not make that old state current again.
            if time_text and time_text != row["time_text"]:
                resolved = resolve_time(time_text, now)
                effective = (
                    resolved["start"]
                    if resolved["start"] is not None
                    else row["valid_from"]
                )
            else:
                resolved = {
                    "start": row["event_start"],
                    "end": row["event_end"],
                    "precision": row["precision"],
                }
                effective = row["valid_from"]
            fingerprint = hashlib.sha256(
                _json([memory_id, content, now, actor, state]).encode()
            ).hexdigest()
            cursor = self.db.execute(
                "INSERT INTO memories (persona_id,user_id,kind,key,content,state,source_ids,source_group,"
                "source_sent_at,learned_at,event_start,event_end,precision,valid_from,corrects_id,fingerprint,time_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["persona_id"],
                    row["user_id"],
                    row["kind"],
                    row["key"],
                    content.strip(),
                    state or row["state"],
                    row["source_ids"],
                    row["source_group"],
                    row["source_sent_at"],
                    now,
                    resolved["start"],
                    resolved["end"],
                    resolved["precision"],
                    effective,
                    memory_id,
                    fingerprint,
                    time_text or row["time_text"],
                ),
            )
            self.db.execute(
                "UPDATE memories SET status='corrected' WHERE id=?", (memory_id,)
            )
            # A later batch must not recreate the rejected interpretation under
            # another model-generated topic key. Keep evidence for audit only.
            source_ids = json.loads(row["source_ids"])
            self.db.executemany(
                "UPDATE sources SET status='reviewed' WHERE id=? AND status NOT IN ('forgotten','expired')",
                ((value,) for value in source_ids),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO source_tombstones VALUES (?)",
                ((value,) for value in source_ids),
            )
            return cursor.lastrowid

    def apply_styles(self, proposals: list[dict], epoch: int) -> list[int]:
        output = []
        with self._lock, self.db:
            if epoch != self.epoch():
                return []
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                sources = self._proposal_sources(proposal, interaction=False)
                expression, meaning = (
                    proposal.get("expression"),
                    proposal.get("meaning"),
                )
                if (
                    not sources
                    or not isinstance(expression, str)
                    or not 1 <= len(expression.strip()) <= 100
                    or not isinstance(meaning, str)
                    or not 1 <= len(meaning.strip()) <= 500
                ):
                    continue
                # Expressions must be seen in a human source, not invented by a model.
                if not any(expression.strip() in x["text"] for x in sources):
                    continue
                scope = sources[0]["scope_key"]
                old = self.db.execute(
                    "SELECT * FROM styles WHERE scope_key=? AND expression=?",
                    (scope, expression.strip()),
                ).fetchone()
                ids = {x["id"] for x in sources}
                if old:
                    if old["status"] in {"forgotten", "disabled"}:
                        continue
                    ids.update(json.loads(old["source_ids"]))
                    self.db.execute(
                        "UPDATE styles SET source_ids=?,updated_at=? WHERE id=?",
                        (_json(sorted(ids)), self.clock(), old["id"]),
                    )
                    output.append(old["id"])
                    continue
                contexts, avoid = (
                    proposal.get("contexts", []),
                    proposal.get("avoid", []),
                )
                if isinstance(contexts, str):
                    contexts = [contexts] if contexts else []
                if isinstance(avoid, str):
                    avoid = [avoid] if avoid else []
                if not isinstance(contexts, list) or not isinstance(avoid, list):
                    continue
                cursor = self.db.execute(
                    "INSERT INTO styles (scope_key,expression,meaning,contexts,avoid,source_ids,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        scope,
                        expression.strip(),
                        meaning.strip(),
                        _json([str(x)[:200] for x in contexts[:8]]),
                        _json([str(x)[:200] for x in avoid[:8]]),
                        _json(sorted(ids)),
                        self.clock(),
                        self.clock(),
                    ),
                )
                output.append(cursor.lastrowid)
            return output

    def styles(self, scope_key: str, status="active", limit=10) -> list[dict]:
        with self._lock:
            sql, args = "SELECT * FROM styles WHERE scope_key=?", [scope_key]
            if status is not None:
                sql += " AND status=?"
                args.append(status)
            else:
                sql += " AND status<>'forgotten'"
            sql += " ORDER BY updated_at DESC,id DESC LIMIT ?"
            args.append(max(0, int(limit)))
            output = []
            for row in self.db.execute(sql, args):
                item = dict(row)
                for key in ("contexts", "avoid", "source_ids"):
                    item[key] = json.loads(item[key])
                output.append(item)
            return output

    def get_style(self, style_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM styles WHERE id=? AND status<>'forgotten'", (style_id,)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            for key in ("contexts", "avoid", "source_ids"):
                item[key] = json.loads(item[key])
            return item

    def auto_activate_styles(
        self,
        scope_key: str,
        *,
        min_authors=3,
        min_days=2,
        min_evidence=5,
        daily_limit=2,
        active_limit=20,
    ) -> list[int]:
        """Promote only candidates with independent human evidence; disabled stays disabled."""
        with self._lock, self.db:
            day = datetime.fromtimestamp(self.clock(), BEIJING).date().isoformat()
            used = self.db.execute(
                "SELECT count(*) FROM style_activations WHERE scope_key=? AND day=?",
                (scope_key, day),
            ).fetchone()[0]
            active = self.db.execute(
                "SELECT count(*) FROM styles WHERE scope_key=? AND status='active'",
                (scope_key,),
            ).fetchone()[0]
            available = min(max(0, daily_limit - used), max(0, active_limit - active))
            activated = []
            for candidate in self.styles(scope_key, "candidate", limit=1000):
                if len(activated) >= available:
                    break
                evidence = [self.get_source(x) for x in candidate["source_ids"]]
                evidence = [x for x in evidence if x and x["status"] != "forgotten"]
                # Repetition by one account in one hour is only one observation.
                unique = {
                    (x["user_id"], int(x["sent_at"] // 3600)): x for x in evidence
                }
                evidence = list(unique.values())
                authors = {x["user_id"] for x in evidence}
                days = {
                    datetime.fromtimestamp(x["sent_at"], BEIJING).date()
                    for x in evidence
                }
                if (
                    len(evidence) < max(1, min_evidence)
                    or len(authors) < max(1, min_authors)
                    or len(days) < max(1, min_days)
                ):
                    continue
                if not activated:
                    self._style_snapshot(scope_key)
                self.db.execute(
                    "UPDATE styles SET status='active',updated_at=? WHERE id=?",
                    (self.clock(), candidate["id"]),
                )
                self.db.execute(
                    "INSERT INTO style_activations (scope_key,style_id,day) VALUES (?,?,?)",
                    (scope_key, candidate["id"], day),
                )
                activated.append(candidate["id"])
            if activated:
                self._style_snapshot(scope_key)
            return activated

    def _style_snapshot(self, scope: str) -> int:
        ids = [
            x[0]
            for x in self.db.execute(
                "SELECT id FROM styles WHERE scope_key=? AND status='active' ORDER BY id",
                (scope,),
            )
        ]
        return self.db.execute(
            "INSERT INTO style_versions (scope_key,active_ids,created_at) VALUES (?,?,?)",
            (scope, _json(ids), self.clock()),
        ).lastrowid

    def set_style_status(self, style_id: int, status: str) -> int:
        if status not in {"active", "disabled", "candidate"}:
            raise ValueError("Unsupported style status")
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT scope_key,status FROM styles WHERE id=?", (style_id,)
            ).fetchone()
            if not row or row["status"] == "forgotten":
                raise ValueError("Style does not exist")
            self.db.execute(
                "UPDATE styles SET status=?,updated_at=? WHERE id=?",
                (status, self.clock(), style_id),
            )
            self._bump_epoch()
            return self._style_snapshot(row["scope_key"])

    def style_versions(self, scope_key: str) -> list[dict]:
        with self._lock:
            return [
                dict(x)
                for x in self.db.execute(
                    "SELECT * FROM style_versions WHERE scope_key=? ORDER BY id DESC",
                    (scope_key,),
                )
            ]

    def rollback_styles(self, scope_key: str, version: int) -> int:
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT active_ids FROM style_versions WHERE id=? AND scope_key=?",
                (version, scope_key),
            ).fetchone()
            if not row:
                raise ValueError("Style version does not exist in this group")
            self.db.execute(
                "UPDATE styles SET status='disabled' WHERE scope_key=? AND status='active'",
                (scope_key,),
            )
            self.db.executemany(
                "UPDATE styles SET status='active' WHERE id=? AND scope_key=? AND status<>'forgotten'",
                ((x, scope_key) for x in json.loads(row[0])),
            )
            self._bump_epoch()
            return self._style_snapshot(scope_key)

    def set_core(self, persona_id: str, content: str) -> int:
        if not isinstance(content, str) or len(content) > 30000:
            raise ValueError("Invalid core persona")
        with self._lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO core_versions (persona_id,content,created_at) VALUES (?,?,?)",
                (persona_id, content, self.clock()),
            )
            self.db.execute(
                "INSERT INTO cores VALUES (?,?) ON CONFLICT(persona_id) DO UPDATE SET version_id=excluded.version_id",
                (persona_id, cursor.lastrowid),
            )
            return cursor.lastrowid

    def get_core(self, persona_id: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT v.content FROM core_versions v JOIN cores c ON v.id=c.version_id WHERE c.persona_id=?",
                (persona_id,),
            ).fetchone()
            return row[0] if row else None

    def core_versions(self, persona_id: str) -> list[dict]:
        with self._lock:
            return [
                dict(x)
                for x in self.db.execute(
                    "SELECT * FROM core_versions WHERE persona_id=? ORDER BY id DESC",
                    (persona_id,),
                )
            ]

    def rollback_core(self, persona_id: str, version: int) -> int:
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT content FROM core_versions WHERE id=? AND persona_id=?",
                (version, persona_id),
            ).fetchone()
            if not row:
                raise ValueError("Core version does not exist for this persona")
            return self.set_core(persona_id, row[0])

    def reserve_budget(self, day: str, scope_key: str, limit: int) -> bool:
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO budgets VALUES (?,?,0)", (day, scope_key)
            )
            cursor = self.db.execute(
                "UPDATE budgets SET used=used+1 WHERE day=? AND scope_key=? AND used<?",
                (day, scope_key, max(0, int(limit))),
            )
            return cursor.rowcount == 1

    def reserve_call_budget(
        self, day: str, scope_key: str, global_limit: int, group_limit: int
    ) -> bool:
        """Reserve both counters together; exhausting one never consumes the other."""
        if scope_key == "*":
            return False
        with self._lock, self.db:
            # BEGIN IMMEDIATE also serializes separate Store connections.
            self.db.execute("BEGIN IMMEDIATE")
            for key, limit in (("*", global_limit), (scope_key, group_limit)):
                if self.budget_used(day, key) >= max(0, int(limit)):
                    return False
            self.db.executemany(
                "INSERT INTO budgets(day,scope_key,used) VALUES (?,?,1) "
                "ON CONFLICT(day,scope_key) DO UPDATE SET used=used+1",
                ((day, key) for key in ("*", scope_key)),
            )
            return True

    def budget_used(self, day: str, scope_key: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT used FROM budgets WHERE day=? AND scope_key=?", (day, scope_key)
            ).fetchone()
            return row[0] if row else 0

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.db:
            if key == "paused" and self.get_meta(key, False) != value:
                self._bump_epoch()
            self.db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _json(value)),
            )

    def scopes(self, persona_id: str) -> list[dict]:
        with self._lock:
            return [
                dict(x)
                for x in self.db.execute(
                    "SELECT DISTINCT scope_key,group_id,platform_id,bot_id FROM sources WHERE persona_id=? AND status<>'forgotten'",
                    (persona_id,),
                )
            ]

    def stats(self) -> dict:
        with self._lock:
            return {
                "sources": self.db.execute(
                    "SELECT count(*) FROM sources WHERE text<>''"
                ).fetchone()[0],
                "pending_sources": self.db.execute(
                    "SELECT count(*) FROM sources WHERE status='pending'"
                ).fetchone()[0],
                "memories": self.db.execute(
                    "SELECT count(*) FROM memories WHERE status='active'"
                ).fetchone()[0],
                "active_styles": self.db.execute(
                    "SELECT count(*) FROM styles WHERE status='active'"
                ).fetchone()[0],
                "candidate_styles": self.db.execute(
                    "SELECT count(*) FROM styles WHERE status='candidate'"
                ).fetchone()[0],
                "epoch": self.epoch(),
            }

    def relationship(self, persona_id: str, user_id: str) -> dict:
        with self._lock:
            rows = self.db.execute(
                "SELECT sent_at,scope_key FROM sources WHERE persona_id=? AND user_id=? "
                "AND purpose='interaction' AND status<>'forgotten'",
                (persona_id, user_id),
            ).fetchall()
            days = {datetime.fromtimestamp(x["sent_at"], BEIJING).date() for x in rows}
            return {
                "interaction_count": len(rows),
                "interaction_days": len(days),
                "group_count": len({x["scope_key"] for x in rows}),
                "first_interaction_at": min((x["sent_at"] for x in rows), default=None),
                "last_interaction_at": max((x["sent_at"] for x in rows), default=None),
            }

    def cleanup(self, *, observation_hours=48, interaction_days=7) -> int:
        with self._lock:
            now = self.clock()
            return self.prune_sources(
                now - max(0, observation_hours) * 3600, "observation"
            ) + self.prune_sources(
                now - max(0, interaction_days) * 86400, "interaction"
            )

    def prune_sources(self, before: float, purpose: str | None = None) -> int:
        """Expire raw evidence without deleting approved derived memories."""
        with self._lock, self.db:
            sql, args = (
                "UPDATE sources SET text='',status='expired' WHERE sent_at<? AND text<>''",
                [before],
            )
            if purpose is not None:
                sql += " AND purpose=?"
                args.append(purpose)
            cursor = self.db.execute(sql, args)
            if cursor.rowcount:
                self._bump_epoch()
            return cursor.rowcount
