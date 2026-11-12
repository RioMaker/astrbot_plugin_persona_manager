"""Durable learning queues and sent conversation evidence, without reply ownership."""

import hashlib
import json
import re

from .retrieval import normalized, overlap


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def feedback_signal(text):
    # Laughter, silence, agreement with facts and mere continuation are unknown.
    if re.search(r"他说|她说|转述|引用|不是不|并非不|如果|假如", text):
        return ""
    if re.search(
        r"(?:这个语气|这样说|这个说法|这句话).{0,5}(?:不喜欢|不(?:太|够)?自然|不舒服|不好听)|(?:不喜欢|讨厌).{0,4}你这样说",
        text,
    ):
        return "negative"
    if re.search(
        r"别这么说|别这样说|别学|正常说话|好尬|太尬|别装|不要这个语气|别再用|别老说",
        text,
    ):
        return "negative"
    if re.search(
        r"(?:这样说|这个语气|这个说法|这句话).{0,8}(?:自然|喜欢|舒服|好听)|喜欢你这样说",
        text,
    ):
        return "positive"
    return ""


class DialogueStore:
    def add_source(self, source):
        with self.transaction():
            added = super().add_source(source)
            if added:
                self.db.execute(
                    """INSERT INTO learning_queue(source_id,lane,status) VALUES (?,?,'pending')
                    ON CONFLICT(source_id,lane) DO UPDATE SET status='pending'""",
                    (source["id"], source["purpose"]),
                )
            return added

    def queue_expression(self, source_id):
        with self.transaction():
            source = self.get_source(source_id)
            if source and source["status"] not in ("forgotten", "expired", "reviewed"):
                self.db.execute(
                    "INSERT OR IGNORE INTO learning_queue(source_id,lane) VALUES (?,'observation')",
                    (source_id,),
                )
                self.db.execute(
                    "UPDATE sources SET status='pending' WHERE id=? AND EXISTS "
                    "(SELECT 1 FROM learning_queue WHERE source_id=? AND status='pending')",
                    (source_id, source_id),
                )

    def pending_learning(
        self, *, persona_id, limit=500, after=None, scope_key=None, lane=None
    ):
        sql = """SELECT s.*,q.lane AS learning_lane FROM learning_queue q
            JOIN sources s ON s.id=q.source_id WHERE q.status='pending' AND s.text<>''
            AND s.status NOT IN ('forgotten','expired','reviewed') AND s.persona_id=?"""
        args = [persona_id]
        if scope_key is not None:
            sql += " AND s.scope_key=?"
            args.append(scope_key)
        if lane is not None:
            if lane not in {"interaction", "observation"}:
                raise ValueError("学习队列类型无效")
            sql += " AND q.lane=?"
            args.append(lane)
        if after is not None:
            sql += " AND (s.sent_at>? OR (s.sent_at=? AND s.id>?))"
            args.extend((float(after[0]), float(after[0]), str(after[1])))
        sql += " ORDER BY s.sent_at,s.id,q.lane LIMIT ?"
        args.append(min(1000, max(1, int(limit))))
        with self._lock:
            rows = [self._source_dict(row) for row in self.db.execute(sql, args)]
            return [
                {
                    **row,
                    "original_purpose": row["purpose"],
                    "purpose": row["learning_lane"],
                }
                for row in rows
            ]

    def mark_learning(self, ids, epoch, lane, status="done"):
        if lane not in ("observation", "interaction") or status not in (
            "done",
            "failed",
            "pending",
        ):
            raise ValueError("Invalid learning lane/status")
        with self.transaction():
            if epoch != self.epoch():
                return False
            for source_id in ids:
                self.db.execute(
                    "UPDATE learning_queue SET status=? WHERE source_id=? AND lane=?",
                    (status, source_id, lane),
                )
                pending = self.db.execute(
                    "SELECT 1 FROM learning_queue WHERE source_id=? AND status='pending'",
                    (source_id,),
                ).fetchone()
                self.db.execute(
                    "UPDATE sources SET status=? WHERE id=? AND status NOT IN ('forgotten','expired','reviewed')",
                    ("pending" if pending else status, source_id),
                )
            return True

    def mark_sources(self, ids, epoch, status="done"):
        ids = list(ids)
        with self.transaction():
            if not super().mark_sources(ids, epoch, status):
                return False
            self.db.executemany(
                "UPDATE learning_queue SET status=? WHERE source_id=?",
                ((status, item) for item in ids),
            )
            return True

    def conversation_material(self, sources, *, max_chars=6000):
        """Bounded adjacent episodes. Replies are context, never human evidence."""
        from .learning import _unsafe

        if not sources:
            return []
        scope = sources[0]["scope_key"]
        start, end = (
            min(x["sent_at"] for x in sources),
            max(x["sent_at"] for x in sources),
        )
        with self._lock:
            sql = """SELECT s.*,d.reply_text FROM sources s LEFT JOIN dialogue_turns d ON d.turn_id=s.id
                WHERE s.scope_key=? AND s.persona_id=? AND s.sent_at BETWEEN ? AND ? AND s.text<>''
                AND s.status NOT IN ('forgotten','expired','reviewed')"""
            args = [scope, sources[0]["persona_id"], start - 300, end + 300]
            if sources[0]["purpose"] == "interaction":
                sql += " AND s.purpose='interaction'"
            rows = self.db.execute(
                sql + " ORDER BY s.sent_at,s.id LIMIT 80", args
            ).fetchall()
            selected = {row["id"] for row in sources}
            episodes, block, consumed, last = [], [], 0, None
            for raw in rows:
                row = self._source_dict(raw)
                if _unsafe(row["text"]):
                    continue
                linked = last and row.get("reply_to") == last.get("message_id")
                boundary = last and (
                    row["sent_at"] - last["sent_at"] > 180
                    or (
                        len(block) >= 3
                        and not linked
                        and overlap(row["text"], last["text"]) == 0
                    )
                )
                if block and (boundary or len(block) >= 8):
                    if any(x["eligible_evidence"] for x in block):
                        episodes.append(block)
                    block = []
                reply = row.get("reply_text") or ""
                item = {
                    "id": row["id"],
                    "speaker": hashlib.sha256(row["user_id"].encode()).hexdigest()[:10],
                    "time": row["sent_at"],
                    "text": row["text"][:600],
                    "reply_to": row.get("reply_to", ""),
                    "eligible_evidence": row["id"] in selected,
                    "bot_sent_reply": "" if _unsafe(reply) else reply[:800],
                }
                size = len(dump(item))
                if consumed + size > max_chars:
                    break
                block.append(item)
                consumed += size
                last = row
            if block and any(x["eligible_evidence"] for x in block):
                episodes.append(block)
            return episodes

    def record_delivery(
        self, source, reply, style_ids, *, keep_dialogue=True, epoch=None
    ):
        """Count once after the host send hook. A send return is not client delivery."""
        from .learning import _unsafe

        with self.transaction():
            if epoch is not None and epoch != self.epoch():
                style_ids = []
            rows = [self.get_style(x) for x in dict.fromkeys(style_ids)]
            rows = [x for x in rows if x and x["status"] == "active"]
            used = [
                x["id"]
                for x in rows
                if x["form_type"] == "phrase"
                and normalized(x["expression"]) in normalized(reply)
            ]
            at = self.clock()
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO delivery_receipts VALUES (?,?,?,?,?)",
                (
                    source["id"],
                    source["scope_key"],
                    at,
                    dump([x["id"] for x in rows]),
                    dump(used),
                ),
            )
            if not cursor.rowcount:
                return False
            for row in rows:
                self.db.execute(
                    """INSERT INTO style_usage(style_id,scope_key,injections,uses,last_injected,last_used)
                    VALUES (?,?,1,?,?,?) ON CONFLICT(style_id,scope_key) DO UPDATE SET
                    injections=injections+1,uses=uses+excluded.uses,last_injected=excluded.last_injected,
                    last_used=CASE WHEN excluded.uses>0 THEN excluded.last_used ELSE last_used END""",
                    (
                        row["id"],
                        source["scope_key"],
                        int(row["id"] in used),
                        at,
                        at if row["id"] in used else None,
                    ),
                )
            original = self.get_source(source["id"])
            if (
                keep_dialogue
                and original
                and original["text"]
                and original["status"] not in ("forgotten", "expired", "reviewed")
            ):
                self.db.execute(
                    "INSERT OR IGNORE INTO dialogue_turns(turn_id,persona_id,scope_key,user_id,reply_text,sent_at) VALUES (?,?,?,?,?,?)",
                    (
                        source["id"],
                        source["persona_id"],
                        source["scope_key"],
                        source["user_id"],
                        "" if _unsafe(reply) else reply[:2400],
                        at,
                    ),
                )
            return True

    def consume_feedback(self, source):
        signal = feedback_signal(source["text"])
        if not signal:
            return {}
        with self.transaction():
            if self.db.execute(
                "SELECT 1 FROM source_tombstones WHERE id=?", (source["id"],)
            ).fetchone():
                return {}
            row = self.db.execute(
                """SELECT d.*,r.used_ids FROM dialogue_turns d
                JOIN delivery_receipts r ON r.turn_id=d.turn_id WHERE d.persona_id=? AND d.scope_key=?
                AND d.user_id=? AND d.sent_at<? AND d.sent_at>=? AND d.feedback=''
                ORDER BY d.sent_at DESC LIMIT 1""",
                (
                    source["persona_id"],
                    source["scope_key"],
                    source["user_id"],
                    source["sent_at"],
                    source["sent_at"] - 600,
                ),
            ).fetchone()
            # We cannot authenticate a quote aimed at a different message without
            # the host's outgoing platform ID. Ambiguous quoted feedback stays unknown.
            if not row or source.get("reply_to"):
                return {}
            used = [self.get_style(x) for x in json.loads(row["used_ids"])]
            used = [x for x in used if x and x["status"] == "active"]
            explicit = [
                x
                for x in used
                if normalized(x["expression"]) in normalized(source["text"])
            ]
            targets = explicit or (used if len(used) == 1 else [])
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO feedback_events VALUES (?,?,?,?,?)",
                (
                    source["id"],
                    row["turn_id"],
                    signal,
                    dump([x["id"] for x in targets]),
                    source["sent_at"],
                ),
            )
            if not cursor.rowcount:
                return {}
            self.db.execute(
                "UPDATE dialogue_turns SET feedback=? WHERE turn_id=?",
                (signal if targets else signal + "_unattributed", row["turn_id"]),
            )
            for item in targets:
                column = "negative" if signal == "negative" else "positive"
                self.db.execute(
                    f"UPDATE style_usage SET {column}={column}+1 WHERE style_id=? AND scope_key=?",
                    (item["id"], source["scope_key"]),
                )
                totals = self.db.execute(
                    "SELECT sum(negative),sum(positive) FROM style_usage WHERE style_id=?",
                    (item["id"],),
                ).fetchone()
                if signal == "negative" and totals[0] >= 2 and totals[0] > totals[1]:
                    self.set_style_status(item["id"], "candidate")
                    self.db.execute(
                        "UPDATE style_traits SET review_reason='两次明确负面反馈，等待人工复审' WHERE style_id=?",
                        (item["id"],),
                    )
            return {"polarity": signal, "style_ids": [x["id"] for x in targets]}

    def dialogue_history(self, persona_id, scope_key="", user_id="", limit=20):
        sql = """SELECT d.*,s.text AS user_text,s.sent_at AS received_at FROM dialogue_turns d
            JOIN sources s ON s.id=d.turn_id WHERE d.persona_id=? AND s.text<>''
            AND s.status NOT IN ('forgotten','expired')"""
        args = [persona_id]
        for key, value in (("scope_key", scope_key), ("user_id", user_id)):
            if value:
                sql += f" AND d.{key}=?"
                args.append(value)
        with self._lock:
            return [
                dict(row)
                for row in self.db.execute(
                    sql + " ORDER BY d.sent_at DESC LIMIT ?", (*args, min(100, limit))
                )
            ]

    def prune_sources(self, before, purpose=None):
        with self.transaction():
            count = super().prune_sources(before, purpose)
            self.db.execute(
                "DELETE FROM dialogue_turns WHERE turn_id IN (SELECT id FROM sources WHERE status IN ('expired','forgotten'))"
            )
            self.db.execute(
                "DELETE FROM feedback_events WHERE turn_id NOT IN (SELECT turn_id FROM dialogue_turns)"
            )
            self.db.execute(
                "DELETE FROM delivery_receipts WHERE sent_at<?",
                (self.clock() - 30 * 86400,),
            )
            return count

    def stats(self, persona_id=None):
        with self._lock:
            where = " AND s.persona_id=?" if persona_id is not None else ""
            args = (persona_id,) if persona_id is not None else ()
            return {
                **super().stats(persona_id),
                "schema_version": 1,
                "dialogue_turns": self.db.execute(
                    "SELECT count(*) FROM dialogue_turns"
                ).fetchone()[0],
                "learning_jobs": self.db.execute(
                    "SELECT count(*) FROM learning_queue q JOIN sources s ON s.id=q.source_id WHERE q.status='pending' AND s.text<>'' AND s.status NOT IN ('expired','forgotten','reviewed')"
                    + where,
                    args,
                ).fetchone()[0],
                "pending_interactions": self.db.execute(
                    "SELECT count(*) FROM learning_queue q JOIN sources s ON s.id=q.source_id "
                    "WHERE q.lane='interaction' AND q.status='pending' AND s.text<>'' "
                    "AND s.status NOT IN ('forgotten','expired','reviewed')" + where,
                    args,
                ).fetchone()[0],
                "feedback_events": self.db.execute(
                    "SELECT count(*) FROM feedback_events"
                ).fetchone()[0],
            }
