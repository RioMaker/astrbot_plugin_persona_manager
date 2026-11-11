"""Seven memory lifecycles, administrator classifiers and scoped public recall."""

import hashlib
import json
from datetime import datetime

from .memory_types import CATEGORIES, PROVENANCE, classifier, number, tags, text
from .temporal import BEIJING


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def upgrade_classified(store):
    with store.transaction() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS memory_classes (
            persona_id TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,category TEXT NOT NULL,
            description TEXT NOT NULL,keywords TEXT NOT NULL,ttl_seconds INTEGER NOT NULL,
            status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(persona_id,id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS memory_details (
            memory_id INTEGER PRIMARY KEY REFERENCES memories(id),class_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,provenance TEXT NOT NULL,accuracy TEXT NOT NULL DEFAULT 'unconfirmed',
            mood TEXT NOT NULL DEFAULT '',salience REAL NOT NULL DEFAULT 0,tags TEXT NOT NULL DEFAULT '[]',
            details TEXT NOT NULL DEFAULT '{}',ttl_seconds INTEGER NOT NULL DEFAULT 1800,
            hold_reason TEXT NOT NULL DEFAULT '',admin_forced INTEGER NOT NULL DEFAULT 0,
            claim_key TEXT NOT NULL DEFAULT '',subject_user_id TEXT NOT NULL DEFAULT '')""")
        if "subject_user_id" not in {
            r[1] for r in db.execute("PRAGMA table_info(memory_details)")
        }:
            db.execute(
                "ALTER TABLE memory_details ADD COLUMN subject_user_id TEXT NOT NULL DEFAULT ''"
            )
        db.execute("""CREATE TABLE IF NOT EXISTS memory_confirmations (
            claim_key TEXT NOT NULL,witness TEXT NOT NULL,source_id TEXT NOT NULL,
            PRIMARY KEY(claim_key,witness))""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS memory_details_scope ON memory_details(scope_key,class_id)"
        )
        db.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES ('classified_memory_version',1)"
        )


class ClassifiedMemoryStore:
    def memory_usage(self, day):
        with self._lock:
            values = {}
            for key, period in (("daily", day), ("monthly", day[:7])):
                rows = self.db.execute(
                    "SELECT * FROM learning_usage WHERE purpose='interaction' AND day LIKE ?",
                    (period + "%",),
                ).fetchall()
                values[key] = self._usage_summary(rows)
                values[key]["mtokens"] = (
                    sum(r["charged_tokens"] for r in rows) / 1_000_000
                )
            return values

    def classifiers(self, persona_id):
        with self.transaction():
            for key, (name, _, description) in CATEGORIES.items():
                self.db.execute(
                    """INSERT OR IGNORE INTO memory_classes
                    (persona_id,id,name,category,description,keywords,ttl_seconds,status)
                    VALUES (?,?,?,?,?,'[]',1800,'confirmed')""",
                    (persona_id, key, name, key, description),
                )
            return [
                dict(row, keywords=json.loads(row["keywords"]))
                for row in self.db.execute(
                    "SELECT * FROM memory_classes WHERE persona_id=? ORDER BY status,id",
                    (persona_id,),
                )
            ]

    def _class(self, persona_id, class_id):
        return next(
            (c for c in self.classifiers(persona_id) if c["id"] == class_id), None
        )

    def _with_traits(self, row):
        result = super()._with_traits(row)
        if not result:
            return result
        detail = self.db.execute(
            "SELECT * FROM memory_details WHERE memory_id=?", (result["id"],)
        ).fetchone()
        if detail:
            result.update(dict(detail))
            for name in ("tags", "details"):
                result[name] = json.loads(result[name])
            cls = self.db.execute(
                "SELECT * FROM memory_classes WHERE persona_id=? AND id=?",
                (result["persona_id"], result["class_id"]),
            ).fetchone()
            result["category"] = cls["category"] if cls else result["class_id"]
            result["class_name"] = cls["name"] if cls else result["class_id"]
            result["confirmations"] = self.db.execute(
                "SELECT count(*) FROM memory_confirmations WHERE claim_key=?",
                (result["claim_key"],),
            ).fetchone()[0]
        else:
            result.update(
                class_id="timed" if result["retention"] == "short" else "relationship",
                category="timed" if result["retention"] == "short" else "relationship",
                provenance="self_report",
                accuracy="supported",
                tags=[],
                details={},
                mood="",
                salience=0,
                ttl_seconds=604800,
                hold_reason="",
                scope_key="",
                admin_forced=0,
                confirmations=0,
            )
        return result

    def _visible_memory(self, row, scope_key, public):
        if scope_key is None or public:
            return True
        if row.get("scope_key"):
            return row["scope_key"] == scope_key
        # Older records retain their original source identities; never infer a
        # platform from a bare group number if the evidence has been pruned.
        for sid in row.get("source_ids", []):
            source = self.get_source(sid)
            if source and source["scope_key"] == scope_key:
                return True
        return False

    def _model_correction_target(self, proposal, subject, owner):
        target = proposal.get("corrects_id")
        expected = proposal.get("expected_revision")
        if type(target) is not int or type(expected) is not int:
            return None
        old = self.get_memory(target)
        if (
            not old
            or old["status"] != "active"
            or old["revision"] != expected
            or old["persona_id"] != subject["persona_id"]
            or old["user_id"] != owner
            or old.get("admin_forced")
            or (old.get("expires_at") is not None and old["expires_at"] <= self.clock())
        ):
            return None
        scopes = (
            {old["scope_key"]}
            if old.get("scope_key")
            else {
                source["scope_key"]
                for sid in old["source_ids"]
                if (source := self.get_source(sid))
            }
        )
        if scopes != {subject["scope_key"]}:
            return None
        return old

    def _settle_classified_correction(self, mid):
        """Retire the old record only after a verified correction becomes active."""
        pending = self.get_meta(f"classified_correction:{mid}", None)
        row = self.get_memory(mid)
        if not pending or pending.get("applied"):
            return True
        if not row or row["status"] != "active":
            return False
        old = self._model_correction_target(
            {
                "corrects_id": row["corrects_id"],
                "expected_revision": pending["revision"],
            },
            row,
            row["user_id"],
        )
        if old is None or pending.get("needs_review"):
            self.db.execute("UPDATE memories SET status='pending' WHERE id=?", (mid,))
            self.db.execute(
                "UPDATE memory_details SET hold_reason='correction' WHERE memory_id=?",
                (mid,),
            )
            self.index_memory(mid)
            return False
        self.db.execute(
            "UPDATE memories SET status='corrected' WHERE id=?", (old["id"],)
        )
        # Keep evidence available to administrators, but never learn it again.
        retired_sources = set(old["source_ids"]) - set(row["source_ids"])
        self.db.executemany(
            "UPDATE sources SET status='reviewed' WHERE id=? AND status NOT IN ('forgotten','expired')",
            ((sid,) for sid in retired_sources),
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO source_tombstones VALUES (?)",
            ((sid,) for sid in retired_sources),
        )
        self.set_meta(f"classified_correction:{mid}", {**pending, "applied": True})
        self.index_memory(old["id"])
        return True

    def apply_memories(self, proposals, epoch):
        legacy = [p for p in proposals if isinstance(p, dict) and "class_id" not in p]
        classified = [p for p in proposals if isinstance(p, dict) and "class_id" in p]
        with self.transaction():
            legacy = [
                p
                for p in legacy
                if "corrects_id" not in p
                or (
                    p.get("source_ids")
                    and (subject := self.get_source(p["source_ids"][0]))
                    and self._model_correction_target(p, subject, subject["user_id"])
                )
            ]
            ids = super().apply_memories(legacy, epoch)
            for mid in ids:
                row = self.get_memory(mid)
                sources = [self.get_source(sid) for sid in row["source_ids"]]
                cls = "timed" if row["retention"] == "short" else "relationship"
                self.classifiers(row["persona_id"])
                self.db.execute(
                    """INSERT OR IGNORE INTO memory_details
                    (memory_id,class_id,scope_key,provenance,accuracy,ttl_seconds)
                    VALUES (?,?,?,'self_report','supported',?)""",
                    (
                        mid,
                        cls,
                        next((s["scope_key"] for s in sources if s), ""),
                        604800,
                    ),
                )
                if row.get("corrects_id"):
                    self.set_meta(
                        f"classified_correction:{mid}",
                        {
                            "revision": row["revision"] - 1,
                            "applied": row["status"] == "active",
                            "needs_review": row["status"] == "pending",
                        },
                    )
                    if row["status"] == "pending":
                        self.db.execute(
                            "UPDATE memory_details SET hold_reason='correction' WHERE memory_id=?",
                            (mid,),
                        )
            if classified:
                ids.extend(self.apply_classified(classified, epoch))
            return ids

    def apply_classified(
        self, proposals, epoch, *, cache_capacity=30, knowledge_confirmations=3
    ):
        with self.transaction():
            if epoch != self.epoch():
                return []
            accepted = []
            for proposal in proposals:
                sources = [self.get_source(sid) for sid in proposal["source_ids"]]
                subject = self.get_source(proposal["subject_source_id"])
                if (
                    not subject
                    or not sources
                    or any(
                        not s
                        or s["purpose"] != "interaction"
                        or s["status"] in {"forgotten", "expired", "reviewed"}
                        for s in sources
                    )
                ):
                    continue
                if any(
                    s["scope_key"] != subject["scope_key"]
                    or s["persona_id"] != subject["persona_id"]
                    for s in sources
                ):
                    continue
                persona = subject["persona_id"]
                cls = self._class(persona, proposal["class_id"])
                if cls is None:
                    definition = classifier(proposal["new_class"])
                    if definition["id"] != proposal["class_id"]:
                        continue
                    state = (
                        "auto"
                        if CATEGORIES[definition["category"]][1] == "short"
                        else "pending"
                    )
                    self.db.execute(
                        "INSERT INTO memory_classes VALUES (?,?,?,?,?,?,?,?,1)",
                        (
                            persona,
                            definition["id"],
                            definition["name"],
                            definition["category"],
                            definition["description"],
                            encode(definition["keywords"]),
                            definition["ttl_seconds"],
                            state,
                        ),
                    )
                    cls = self._class(persona, definition["id"])
                if cls["status"] == "rejected":
                    continue
                cat = cls["category"]
                owner = (
                    "__self__"
                    if cat == "self"
                    else "__knowledge__"
                    if cat == "knowledge"
                    else subject["user_id"]
                )
                old = None
                if "corrects_id" in proposal:
                    old = self._model_correction_target(proposal, subject, owner)
                    if old is None:
                        continue
                    proposal = {**proposal, "key": old["key"]}
                newest = max(sources, key=lambda s: s["sent_at"])
                identity = [
                    persona,
                    owner,
                    proposal["source_ids"],
                    proposal["class_id"],
                    proposal["key"],
                    proposal["content"],
                ]
                if old:
                    identity.append(old["id"])
                fingerprint = hashlib.sha256(encode(identity).encode()).hexdigest()
                claim = hashlib.sha256(
                    encode(
                        [
                            persona,
                            subject["scope_key"],
                            proposal["key"].strip().casefold(),
                            proposal["content"].strip().casefold(),
                        ]
                    ).encode()
                ).hexdigest()
                if cat == "knowledge":
                    for ev in proposal["evidence"]:
                        src = self.get_source(ev["source_id"])
                        if (
                            ev["role"] == "user"
                            and proposal["provenance"] == "self_report"
                        ):
                            # One person on one day is one confirmation, even if
                            # the same batch is replayed or split into many lines.
                            witness = (
                                src["user_id"]
                                + ":"
                                + datetime.fromtimestamp(src["sent_at"], BEIJING)
                                .date()
                                .isoformat()
                            )
                            self.db.execute(
                                "INSERT OR IGNORE INTO memory_confirmations VALUES (?,?,?)",
                                (claim, witness, src["id"]),
                            )
                count = self.db.execute(
                    "SELECT count(*) FROM memory_confirmations WHERE claim_key=?",
                    (claim,),
                ).fetchone()[0]
                hold = (
                    "classification"
                    if cls["status"] == "pending"
                    else "knowledge"
                    if cat == "knowledge" and count < knowledge_confirmations
                    else "correction"
                    if old and proposal["confidence"] < 0.8
                    else ""
                )
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO memories
                    (persona_id,user_id,kind,key,content,state,source_ids,source_group,source_sent_at,learned_at,
                     precision,valid_from,status,fingerprint) VALUES (?,?,?,?,?,'asserted',?,?,?,?,'unknown',?,?,?)""",
                    (
                        persona,
                        owner,
                        "event" if cat == "event" else "fact",
                        proposal["key"],
                        proposal["content"],
                        encode(proposal["source_ids"]),
                        subject["group_id"],
                        newest["sent_at"],
                        self.clock(),
                        newest["sent_at"],
                        "pending" if hold else "active",
                        fingerprint,
                    ),
                )
                if not cursor.rowcount:
                    continue
                mid = cursor.lastrowid
                if old:
                    self.db.execute(
                        "UPDATE memories SET corrects_id=? WHERE id=?", (old["id"], mid)
                    )
                    self.set_meta(
                        f"classified_correction:{mid}",
                        {
                            "revision": old["revision"],
                            "applied": False,
                            "needs_review": proposal["confidence"] < 0.8,
                        },
                    )
                expires = (
                    newest["sent_at"] + proposal["ttl_seconds"]
                    if cat == "timed"
                    else None
                )
                self.db.execute(
                    """INSERT INTO memory_traits(memory_id,retention,expires_at,confidence,evidence)
                    VALUES (?,?,?,?,?)""",
                    (
                        mid,
                        CATEGORIES[cat][1],
                        expires,
                        proposal["confidence"],
                        encode(proposal["evidence"]),
                    ),
                )
                if old:
                    self.db.execute(
                        "UPDATE memory_traits SET revision=? WHERE memory_id=?",
                        (old["revision"] + 1, mid),
                    )
                self.db.execute(
                    "INSERT INTO memory_details VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mid,
                        cls["id"],
                        subject["scope_key"],
                        proposal["provenance"],
                        "confirmed"
                        if cat == "knowledge" and count >= knowledge_confirmations
                        else "supported"
                        if proposal["provenance"] == "self_report"
                        else "unconfirmed",
                        proposal["mood"],
                        proposal["salience"],
                        encode(proposal["tags"]),
                        encode(proposal["details"]),
                        proposal["ttl_seconds"],
                        hold,
                        0,
                        claim,
                        subject["user_id"],
                    ),
                )
                if (
                    cat == "knowledge"
                    and count >= knowledge_confirmations
                    and cls["status"] != "pending"
                ):
                    pending = self.db.execute(
                        "SELECT memory_id FROM memory_details WHERE claim_key=? AND hold_reason='knowledge'",
                        (claim,),
                    ).fetchall()
                    for old in pending:
                        self.db.execute(
                            "UPDATE memories SET status='active' WHERE id=? AND status='pending'",
                            (old[0],),
                        )
                        self.db.execute(
                            "UPDATE memory_details SET accuracy='confirmed',hold_reason='' WHERE memory_id=?",
                            (old[0],),
                        )
                        self.index_memory(old[0])
                        self._settle_classified_correction(old[0])
                settled = self._settle_classified_correction(mid)
                if cat == "self" and not hold and settled:
                    self._refresh_self(mid)
                self.index_memory(mid)
                accepted.append(mid)
            self.maintain_memories(cache_capacity=cache_capacity)
            return accepted

    def _refresh_self(self, mid):
        row = self.get_memory(mid)
        older = self.db.execute(
            """SELECT m.id FROM memories m JOIN memory_details d ON d.memory_id=m.id
            WHERE m.persona_id=? AND m.user_id='__self__' AND m.key=? AND d.scope_key=?
            AND m.status='active' AND m.id<>? AND m.source_sent_at<=?""",
            (
                row["persona_id"],
                row["key"],
                row["scope_key"],
                mid,
                row["source_sent_at"],
            ),
        ).fetchall()
        for old in older:
            self.db.execute(
                "UPDATE memories SET status='superseded' WHERE id=?", (old[0],)
            )
            self.index_memory(old[0])

    def _owner_for_category(self, row, category):
        if category == "self":
            return "__self__"
        if category == "knowledge":
            return "__knowledge__"
        if row.get("subject_user_id"):
            return row["subject_user_id"]
        for sid in row["source_ids"]:
            source = self.get_source(sid)
            if source:
                return source["user_id"]
        return row["user_id"]

    def maintain_memories(self, *, cache_capacity=30):
        with self.transaction():
            rows = self.db.execute(
                "SELECT m.id FROM memories m JOIN memory_traits t ON t.memory_id=m.id WHERE m.status IN ('active','pending') AND t.expires_at<=?",
                (self.clock(),),
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE memories SET status='expired',content='',key='',time_text='' WHERE id=?",
                    (row[0],),
                )
                self.db.execute(
                    "UPDATE memory_traits SET evidence='[]',qualifiers='[]',pinned=0 WHERE memory_id=?",
                    (row[0],),
                )
                self.db.execute(
                    "UPDATE memory_details SET mood='',details='{}',tags='[]' WHERE memory_id=?",
                    (row[0],),
                )
                self.index_memory(row[0])
            stacks = {}
            for row in self.db.execute("""SELECT m.id,m.persona_id,m.user_id,d.scope_key FROM memories m
                JOIN memory_details d ON d.memory_id=m.id JOIN memory_classes c ON c.persona_id=m.persona_id AND c.id=d.class_id
                WHERE c.category='cache' AND m.status IN ('active','pending') ORDER BY m.source_sent_at DESC,m.id DESC"""):
                stack = (row["persona_id"], row["user_id"])
                stacks[stack] = stacks.get(stack, 0) + 1
                if stacks[stack] > cache_capacity:
                    self.db.execute(
                        "UPDATE memories SET status='evicted',content='',key='',time_text='' WHERE id=?",
                        (row["id"],),
                    )
                    self.db.execute(
                        "UPDATE memory_traits SET evidence='[]',qualifiers='[]',pinned=0 WHERE memory_id=?",
                        (row["id"],),
                    )
                    self.db.execute(
                        "UPDATE memory_details SET mood='',details='{}',tags='[]' WHERE memory_id=?",
                        (row["id"],),
                    )
                    self.index_memory(row["id"])

    def reconcile_knowledge(self, threshold):
        with self.transaction():
            self.set_meta("knowledge_threshold", threshold)
            changed = False
            rows = self.db.execute("""SELECT m.id,m.status,d.claim_key,d.hold_reason FROM memories m JOIN memory_details d ON d.memory_id=m.id
                JOIN memory_classes c ON c.persona_id=m.persona_id AND c.id=d.class_id
                WHERE c.category='knowledge' AND c.status='confirmed' AND d.admin_forced=0 AND m.status IN ('active','pending')""").fetchall()
            for row in rows:
                count = self.db.execute(
                    "SELECT count(*) FROM memory_confirmations WHERE claim_key=?",
                    (row["claim_key"],),
                ).fetchone()[0]
                status = "active" if count >= threshold else "pending"
                if status == "active" and row["hold_reason"] == "correction":
                    continue
                if status != row["status"]:
                    self.db.execute(
                        "UPDATE memories SET status=? WHERE id=?", (status, row["id"])
                    )
                    self.db.execute(
                        "UPDATE memory_details SET hold_reason=?,accuracy=? WHERE memory_id=?",
                        (
                            "" if status == "active" else "knowledge",
                            "confirmed" if status == "active" else "unconfirmed",
                            row["id"],
                        ),
                    )
                    self.db.execute(
                        "UPDATE memory_traits SET revision=revision+1 WHERE memory_id=?",
                        (row["id"],),
                    )
                    self.index_memory(row["id"])
                    if status == "active":
                        self._settle_classified_correction(row["id"])
                    changed = True
            if changed:
                self._bump_epoch()

    def dream(self, persona_id, *, cutoff=None):
        with self.transaction():
            cutoff = self.clock() if cutoff is None else cutoff
            ids = [
                r[0]
                for r in self.db.execute(
                    """SELECT m.id FROM memories m JOIN memory_details d ON d.memory_id=m.id
                JOIN memory_classes c ON c.persona_id=m.persona_id AND c.id=d.class_id WHERE m.persona_id=?
                AND c.category='daily' AND m.status IN ('active','pending') AND m.learned_at<=?""",
                    (persona_id, cutoff),
                )
            ]
            for mid in ids:
                self.db.execute(
                    "UPDATE memories SET status='dreamed',content='',key='',time_text='' WHERE id=?",
                    (mid,),
                )
                self.db.execute(
                    "UPDATE memory_traits SET evidence='[]',qualifiers='[]',pinned=0 WHERE memory_id=?",
                    (mid,),
                )
                self.db.execute(
                    "UPDATE memory_details SET mood='',details='{}',tags='[]' WHERE memory_id=?",
                    (mid,),
                )
                self.index_memory(mid)
            self.set_meta("last_dream:" + persona_id, self.clock())
            self._bump_epoch()
            return len(ids)

    def save_classifier(
        self,
        persona_id,
        definition,
        *,
        revision,
        confirm=False,
        cache_capacity=30,
        knowledge_confirmations=3,
    ):
        definition = classifier(definition)
        with self.transaction():
            old = self._class(persona_id, definition["id"])
            if old is None or type(revision) is not int or old["revision"] != revision:
                raise ValueError("分类已改变，请刷新后重试")
            status = (
                "confirmed"
                if confirm or old["status"] == "confirmed"
                else "auto"
                if CATEGORIES[definition["category"]][1] == "short"
                else "pending"
            )
            self.db.execute(
                """UPDATE memory_classes SET name=?,category=?,description=?,keywords=?,ttl_seconds=?,status=?,revision=revision+1
                WHERE persona_id=? AND id=?""",
                (
                    definition["name"],
                    definition["category"],
                    definition["description"],
                    encode(definition["keywords"]),
                    definition["ttl_seconds"],
                    status,
                    persona_id,
                    definition["id"],
                ),
            )
            rows = self.db.execute(
                """SELECT m.id FROM memories m JOIN memory_details d ON d.memory_id=m.id
                WHERE m.persona_id=? AND d.class_id=? AND m.status IN ('active','pending')""",
                (persona_id, definition["id"]),
            ).fetchall()
            for entry in rows:
                row = self.get_memory(entry[0])
                cat = definition["category"]
                hold = (
                    "classification"
                    if status == "pending"
                    else "knowledge"
                    if cat == "knowledge"
                    and not row["admin_forced"]
                    and row["confirmations"] < knowledge_confirmations
                    else ""
                )
                expires = (
                    row["source_sent_at"] + definition["ttl_seconds"]
                    if cat == "timed"
                    else None
                )
                self.db.execute(
                    "UPDATE memories SET status=?,user_id=?,kind=? WHERE id=?",
                    (
                        "pending" if hold else "active",
                        self._owner_for_category(row, cat),
                        "event" if cat == "event" else "fact",
                        row["id"],
                    ),
                )
                self.db.execute(
                    "UPDATE memory_traits SET retention=?,expires_at=?,revision=revision+1 WHERE memory_id=?",
                    (CATEGORIES[cat][1], expires, row["id"]),
                )
                self.db.execute(
                    "UPDATE memory_details SET ttl_seconds=?,hold_reason=? WHERE memory_id=?",
                    (definition["ttl_seconds"], hold, row["id"]),
                )
                self.index_memory(row["id"])
                settled = self._settle_classified_correction(row["id"])
                if cat == "self" and not hold and settled:
                    self._refresh_self(row["id"])
            self.maintain_memories(cache_capacity=cache_capacity)
            self._bump_epoch()
            return len(rows)

    def memory_catalog(
        self, persona_id, *, user_id="", class_id="", status="", limit=100, offset=0
    ):
        with self._lock:
            sql = "SELECT m.id FROM memories m LEFT JOIN memory_details d ON d.memory_id=m.id WHERE m.persona_id=? AND m.status NOT IN ('forgotten','evicted','dreamed')"
            args = [persona_id]
            for column, value in (
                ("m.user_id", user_id),
                ("d.class_id", class_id),
                ("m.status", status),
            ):
                if value:
                    sql += f" AND {column}=?"
                    args.append(value)
            total = self.db.execute(
                "SELECT count(*) FROM (" + sql + ")", args
            ).fetchone()[0]
            rows = self.db.execute(
                sql + " ORDER BY m.learned_at DESC,m.id DESC LIMIT ? OFFSET ?",
                (*args, min(200, max(1, limit)), max(0, offset)),
            ).fetchall()
            return {"total": total, "items": [self.get_memory(r[0]) for r in rows]}

    def edit_memory_details(
        self,
        persona_id,
        mid,
        changes,
        *,
        revision,
        cache_capacity=30,
        knowledge_confirmations=3,
    ):
        if not isinstance(changes, dict):
            raise ValueError("记忆修改须为对象")
        with self.transaction():
            row = self.get_memory(mid)
            if (
                not row
                or row["persona_id"] != persona_id
                or row["revision"] != revision
                or row["status"] not in {"active", "pending", "rejected"}
            ):
                raise ValueError("记忆已改变或不能编辑，请刷新")
            value = {**row, **changes}
            cls = self._class(persona_id, text(value.get("class_id"), 64))
            if not cls:
                raise ValueError("分类不存在")
            content, key = text(value["content"]), text(value["key"], 100)
            if value["provenance"] not in PROVENANCE or value["accuracy"] not in {
                "unconfirmed",
                "supported",
                "confirmed",
            }:
                raise ValueError("来源或准确性无效")
            confidence, salience = (
                number(value["confidence"]),
                number(value["salience"]),
            )
            ttl = value["ttl_seconds"]
            if type(ttl) is not int or not 1 <= ttl <= 2592000:
                raise ValueError("有效期无效")
            detail = value["details"]
            if not isinstance(detail, dict) or len(detail) > 12:
                raise ValueError("详情须为最多12项的文本对象")
            detail = {text(k, 40): text(v, 600, False) for k, v in detail.items()}
            changed_claim = content != row["content"] or key != row["key"]
            hold = (
                "classification"
                if cls["status"] == "pending"
                else "knowledge"
                if cls["category"] == "knowledge"
                and not row["admin_forced"]
                and (changed_claim or row["confirmations"] < knowledge_confirmations)
                else ""
            )
            expires = (
                row["source_sent_at"] + ttl if cls["category"] == "timed" else None
            )
            self.db.execute(
                "UPDATE memories SET content=?,key=?,status=?,user_id=?,kind=? WHERE id=?",
                (
                    content,
                    key,
                    "pending" if hold else "active",
                    self._owner_for_category(row, cls["category"]),
                    "event" if cls["category"] == "event" else "fact",
                    mid,
                ),
            )
            self.db.execute(
                "UPDATE memory_traits SET confidence=?,retention=?,expires_at=?,revision=revision+1 WHERE memory_id=?",
                (confidence, CATEGORIES[cls["category"]][1], expires, mid),
            )
            self.db.execute(
                """INSERT INTO memory_details(memory_id,class_id,scope_key,provenance) VALUES (?,?,?,?)
                ON CONFLICT(memory_id) DO NOTHING""",
                (mid, cls["id"], row["scope_key"], value["provenance"]),
            )
            self.db.execute(
                """UPDATE memory_details SET class_id=?,provenance=?,accuracy=?,mood=?,salience=?,tags=?,details=?,ttl_seconds=?,hold_reason=? WHERE memory_id=?""",
                (
                    cls["id"],
                    value["provenance"],
                    value["accuracy"],
                    text(value["mood"], 200, False),
                    salience,
                    encode(tags(value["tags"])),
                    encode(detail),
                    ttl,
                    hold,
                    mid,
                ),
            )
            if changed_claim:
                claim = hashlib.sha256(
                    encode(
                        [
                            persona_id,
                            row["scope_key"],
                            key.casefold(),
                            content.casefold(),
                        ]
                    ).encode()
                ).hexdigest()
                self.db.execute(
                    "UPDATE memory_details SET claim_key=? WHERE memory_id=?",
                    (claim, mid),
                )
            self.index_memory(mid)
            settled = self._settle_classified_correction(mid)
            if cls["category"] == "self" and not hold and settled:
                self._refresh_self(mid)
            self.maintain_memories(cache_capacity=cache_capacity)
            self._bump_epoch()

    def correct(self, memory_id, content, **kwargs):
        with self.transaction():
            mid = super().correct(memory_id, content, **kwargs)
            self.db.execute(
                """INSERT INTO memory_details SELECT ?,class_id,scope_key,provenance,accuracy,mood,salience,tags,details,ttl_seconds,hold_reason,admin_forced,claim_key,subject_user_id
                FROM memory_details WHERE memory_id=?""",
                (mid, memory_id),
            )
            row = self.get_memory(mid)
            if (
                row
                and row.get("category") == "knowledge"
                and not row.get("admin_forced")
            ):
                self.db.execute(
                    "UPDATE memories SET status='pending' WHERE id=?", (mid,)
                )
                self.db.execute(
                    "UPDATE memory_details SET hold_reason='knowledge',claim_key='' WHERE memory_id=?",
                    (mid,),
                )
                self.index_memory(mid)
            return mid

    def manage_memory(self, memory_id, *, action, revision):
        with self.transaction():
            row = self.get_memory(memory_id)
            if (
                row
                and action == "approve"
                and row.get("hold_reason") in {"classification", "knowledge"}
            ):
                raise ValueError("请先确认分类；知识需达到核实门槛或由管理员灌输")
            pending = self.get_meta(f"classified_correction:{memory_id}", None)
            if pending and not pending.get("applied") and action == "approve":
                if (
                    not row
                    or row["status"] != "pending"
                    or type(revision) is not int
                    or row["revision"] != revision
                ):
                    raise ValueError("记忆已更新，请刷新后重试")
                old = self._model_correction_target(
                    {
                        "corrects_id": row["corrects_id"],
                        "expected_revision": pending["revision"],
                    },
                    row,
                    row["user_id"],
                )
                if old is None:
                    raise ValueError("被纠正的记忆已改变，不能批准旧提案")
                self.set_meta(
                    f"classified_correction:{memory_id}",
                    {**pending, "needs_review": False},
                )
                self.db.execute(
                    "UPDATE memories SET status='active' WHERE id=?", (memory_id,)
                )
                self.db.execute(
                    "UPDATE memory_details SET hold_reason='' WHERE memory_id=?",
                    (memory_id,),
                )
                self.db.execute(
                    "UPDATE memory_traits SET revision=revision+1 WHERE memory_id=?",
                    (memory_id,),
                )
                self._settle_classified_correction(memory_id)
                self.index_memory(memory_id)
                self._bump_epoch()
                return
            return super().manage_memory(memory_id, action=action, revision=revision)

    def force_knowledge(self, persona_id, content, key, *, actor):
        if not actor:
            raise ValueError("仅登录管理员可灌输知识")
        content, key = text(content), text(key, 100)
        with self.transaction():
            self.classifiers(persona_id)
            now = self.clock()
            fingerprint = hashlib.sha256(
                encode([persona_id, "admin", key, content, now]).encode()
            ).hexdigest()
            mid = self.db.execute(
                """INSERT INTO memories(persona_id,user_id,kind,key,content,state,source_ids,source_group,source_sent_at,learned_at,precision,valid_from,fingerprint)
                VALUES (?,'__knowledge__','fact',?,?,'asserted','[]','',?,?,'unknown',?,?)""",
                (persona_id, key, content, now, now, now, fingerprint),
            ).lastrowid
            self.db.execute(
                "INSERT INTO memory_traits(memory_id,confidence) VALUES (?,1)", (mid,)
            )
            self.db.execute(
                """INSERT INTO memory_details(memory_id,class_id,scope_key,provenance,accuracy,admin_forced,details)
                VALUES (?,'knowledge','','admin','confirmed',1,?)""",
                (mid, encode({"administrator": actor})),
            )
            self.index_memory(mid)
            self._bump_epoch()
            return mid

    def forget(self, persona_id, user_id, memory_id=None):
        with self.transaction():
            row = self.get_memory(memory_id) if memory_id else None
            affected = set(
                row["source_ids"]
                if row
                else (
                    r[0]
                    for r in self.db.execute(
                        "SELECT id FROM sources WHERE persona_id=? AND user_id=?",
                        (persona_id, user_id),
                    )
                )
            )
            count = super().forget(persona_id, user_id, memory_id)
            # Self/knowledge summaries can refer to another person's evidence.
            for item in self.db.execute(
                "SELECT id,source_ids FROM memories WHERE persona_id=? AND status<>'forgotten'",
                (persona_id,),
            ).fetchall():
                if (row and item["id"] == memory_id) or affected.intersection(
                    json.loads(item["source_ids"])
                ):
                    self.db.execute(
                        "UPDATE memories SET status='forgotten',content='',key='',time_text='' WHERE id=?",
                        (item["id"],),
                    )
                    self.index_memory(item["id"])
            self.db.execute(
                "UPDATE memory_details SET mood='',tags='[]',details='{}',claim_key='' WHERE memory_id IN (SELECT id FROM memories WHERE status='forgotten')"
            )
            self.db.execute(
                "UPDATE memory_traits SET evidence='[]',qualifiers='[]',pinned=0 WHERE memory_id IN (SELECT id FROM memories WHERE status='forgotten')"
            )
            for sid in affected:
                self.db.execute(
                    "DELETE FROM memory_confirmations WHERE source_id=?", (sid,)
                )
            self.reconcile_knowledge(self.get_meta("knowledge_threshold", 3))
            return count
