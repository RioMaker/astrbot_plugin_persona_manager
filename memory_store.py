"""Evidence-backed memories with independent pinned and topical retrieval."""

import json
import math

from .retrieval import overlap, recall_requested, terms
from .temporal import is_past_reference


class MemoryStore:
    def index_memory(self, memory_id):
        self.db.execute("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
        row = self.db.execute(
            "SELECT key,content,status FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if row and row["status"] == "active":
            self.db.executemany(
                "INSERT OR IGNORE INTO memory_terms VALUES (?,?)",
                (
                    (memory_id, term)
                    for term in sorted(terms(row["key"] + " " + row["content"]))[:600]
                ),
            )

    def _with_traits(self, row):
        if row is None:
            return None
        traits = self.db.execute(
            "SELECT * FROM memory_traits WHERE memory_id=?", (row["id"],)
        ).fetchone()
        result = dict(row)
        result.update(
            dict(traits)
            if traits
            else {
                "pinned": 0,
                "qualifiers": "[]",
                "retention": "durable",
                "expires_at": None,
                "confidence": 1,
                "evidence": "[]",
                "revision": 1,
            }
        )
        for name in ("qualifiers", "evidence"):
            result[name] = json.loads(result[name])
        result["pinned"] = bool(result["pinned"])
        return result

    def get_memory(self, memory_id):
        with self._lock:
            return self._with_traits(super().get_memory(memory_id))

    def timeline(self, persona_id, user_id, limit=30):
        with self._lock:
            return [
                self._with_traits(row)
                for row in super().timeline(persona_id, user_id, limit)
            ]

    def profile(self, persona_id, user_id, limit=12):
        if int(limit) <= 0:
            return []
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status='active'
                AND kind<>'event' AND valid_from<=? ORDER BY valid_from DESC,source_sent_at DESC,id DESC""",
                (persona_id, user_id, self.clock()),
            )
            output, seen = [], set()
            for raw in rows:
                row = self._with_traits(self._memory_dict(raw))
                if row["expires_at"] is not None and row["expires_at"] <= self.clock():
                    continue
                if row["precision"] == "unknown":
                    if row["time_text"] not in (
                        "",
                        "最近",
                        "目前",
                        "现在",
                        "以后",
                    ) or is_past_reference(row["time_text"] or row["content"]):
                        continue
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
                attribute = (
                    row["kind"],
                    row["key"],
                    planning,
                    tuple(row["qualifiers"]),
                )
                if attribute not in seen:
                    output.append(row)
                    seen.add(attribute)
            # Resolve the newest fact per attribute before prioritizing pins, so
            # an old pinned fact cannot hide a newer correction of that fact.
            output.sort(key=lambda row: row["pinned"], reverse=True)
            return output[:limit]

    def apply_memories(self, proposals, epoch):
        with self.transaction():
            if epoch != self.epoch():
                return []
            result = []
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                qualifiers = proposal.get("qualifiers", [])
                confidence = proposal.get("confidence", 1.0)
                retention = proposal.get("retention", "durable")
                if (
                    not isinstance(qualifiers, list)
                    or len(qualifiers) > 4
                    or any(
                        not isinstance(x, str) or not x or len(x) > 100
                        for x in qualifiers
                    )
                    or type(confidence) not in (int, float)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                    or retention not in ("short", "durable")
                ):
                    continue
                old = (
                    self.get_memory(proposal.get("corrects_id"))
                    if proposal.get("corrects_id")
                    else None
                )
                if (
                    old
                    and proposal.get("expected_revision", old["revision"])
                    != old["revision"]
                ):
                    continue
                ids = super().apply_memories([proposal], epoch)
                for memory_id in ids:
                    row = super().get_memory(memory_id)
                    pinned = (
                        bool(old["pinned"])
                        if old
                        else row["key"] in ("preferred_name", "response_length")
                    )
                    expires = (
                        row["source_sent_at"] + 7 * 86400
                        if retention == "short"
                        else None
                    )
                    self.db.execute(
                        """INSERT INTO memory_traits(memory_id,pinned,qualifiers,retention,expires_at,confidence,evidence,revision)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            memory_id,
                            int(pinned),
                            json.dumps(qualifiers, ensure_ascii=False),
                            retention,
                            expires,
                            confidence,
                            json.dumps(
                                proposal.get("evidence", []), ensure_ascii=False
                            ),
                            old["revision"] + 1 if old else 1,
                        ),
                    )
                    if confidence < 0.8:
                        self.db.execute(
                            "UPDATE memories SET status='pending' WHERE id=?",
                            (memory_id,),
                        )
                        # An unconfirmed correction must not retire an approved fact.
                        if old:
                            self.db.execute(
                                "UPDATE memories SET status='active' WHERE id=?",
                                (old["id"],),
                            )
                    self.index_memory(memory_id)
                    if old:
                        self.index_memory(old["id"])
                result.extend(ids)
            return result

    def correct(self, memory_id, content, **kwargs):
        with self.transaction():
            previous = self.get_memory(memory_id)
            result = super().correct(memory_id, content, **kwargs)
            self.db.execute(
                """INSERT INTO memory_traits(memory_id,pinned,qualifiers,retention,expires_at,confidence,evidence,revision)
                SELECT ?,pinned,qualifiers,retention,expires_at,1,evidence,revision+1 FROM memory_traits WHERE memory_id=?""",
                (result, memory_id),
            )
            self.index_memory(memory_id)
            self.index_memory(result)
            if previous is None:
                raise ValueError("Memory not found")
            return result

    def manage_memory(self, memory_id, *, action, revision):
        with self.transaction():
            row = self.get_memory(memory_id)
            if row is None or type(revision) is not int or row["revision"] != revision:
                raise ValueError("记忆已更新，请刷新后重试")
            if action in ("pin", "unpin") and row["status"] == "active":
                self.db.execute(
                    "UPDATE memory_traits SET pinned=?,revision=revision+1 WHERE memory_id=?",
                    (int(action == "pin"), memory_id),
                )
            elif action in ("approve", "reject") and row["status"] == "pending":
                if action == "approve" and row["corrects_id"]:
                    old = self.get_memory(row["corrects_id"])
                    if (
                        old is None
                        or old["status"] != "active"
                        or old["revision"] + 1 != row["revision"]
                    ):
                        raise ValueError("被纠正的记忆已改变，不能批准旧提案")
                    self.db.execute(
                        "UPDATE memories SET status='corrected' WHERE id=?",
                        (old["id"],),
                    )
                    self.index_memory(old["id"])
                self.db.execute(
                    "UPDATE memories SET status=? WHERE id=?",
                    ("active" if action == "approve" else "rejected", memory_id),
                )
                self.db.execute(
                    "UPDATE memory_traits SET revision=revision+1 WHERE memory_id=?",
                    (memory_id,),
                )
                self.index_memory(memory_id)
            else:
                raise ValueError("该记忆当前不能执行此操作")
            self._bump_epoch()

    def recall(self, persona_id, user_id, query, *, limit=4):
        needles = sorted(terms(query))[:64]
        with self._lock:
            ids = []
            if needles:
                slots = ",".join("?" for _ in needles)
                ids = [
                    x[0]
                    for x in self.db.execute(
                        f"""SELECT t.memory_id FROM memory_terms t
                    JOIN memories m ON m.id=t.memory_id WHERE t.term IN ({slots}) AND m.persona_id=?
                    AND m.user_id=? AND m.status='active' GROUP BY t.memory_id
                    ORDER BY count(*) DESC,m.source_sent_at DESC LIMIT 200""",
                        (*needles, persona_id, user_id),
                    )
                ]
            if recall_requested(query):
                ids.extend(
                    x["id"]
                    for x in super().timeline(persona_id, user_id, limit=100)
                    if x["status"] == "active"
                )
            ranked = []
            for memory_id in dict.fromkeys(ids):
                row = self.get_memory(memory_id)
                if (
                    not row
                    or row["status"] != "active"
                    or (
                        row["expires_at"] is not None
                        and row["expires_at"] <= self.clock()
                    )
                ):
                    continue
                if row["qualifiers"] and not any(
                    overlap(query, condition) > 0 for condition in row["qualifiers"]
                ):
                    continue
                score = overlap(query, row["key"] + " " + row["content"])
                if score <= 0 and not recall_requested(query):
                    continue
                ranked.append((score, row["source_sent_at"], row))
            ranked.sort(key=lambda x: (x[0], x[1], x[2]["id"]), reverse=True)
            return [row for _, _, row in ranked[: max(0, limit)]]

    def memory_references(self, persona_id, user_id, query):
        rows = self.profile(persona_id, user_id, limit=12) + self.recall(
            persona_id, user_id, query, limit=8
        )
        unique = {row["id"]: row for row in rows}
        return [
            {
                "handle": f"m{row['id']}:r{row['revision']}",
                "kind": row["kind"],
                "key": row["key"],
                "content": row["content"],
                "qualifiers": row["qualifiers"],
                "state": row["state"],
            }
            for row in list(unique.values())[:20]
        ]

    def forget(self, persona_id, user_id, memory_id=None):
        with self.transaction():
            count = super().forget(persona_id, user_id, memory_id)
            self.db.execute(
                "DELETE FROM memory_terms WHERE memory_id IN (SELECT id FROM memories WHERE status='forgotten')"
            )
            self.db.execute(
                "UPDATE memory_traits SET qualifiers='[]',evidence='[]',pinned=0,revision=revision+1 WHERE memory_id IN (SELECT id FROM memories WHERE status='forgotten')"
            )
            self.db.execute(
                "UPDATE style_traits SET situation='',keywords='[]',evidence='[]',signature='',review_reason='' WHERE style_id IN (SELECT id FROM styles WHERE status='forgotten')"
            )
            self.db.execute(
                "DELETE FROM delivery_receipts WHERE turn_id IN (SELECT id FROM sources WHERE status='forgotten')"
            )
            self.db.execute(
                "DELETE FROM feedback_events WHERE source_id IN (SELECT id FROM sources WHERE status='forgotten') OR turn_id IN (SELECT id FROM sources WHERE status='forgotten')"
            )
            self.db.execute(
                "DELETE FROM dialogue_turns WHERE turn_id IN (SELECT id FROM sources WHERE status='forgotten')"
            )
            forgotten = {
                str(x[0])
                for x in self.db.execute(
                    "SELECT id FROM styles WHERE status='forgotten'"
                )
            }
            for row in self.db.execute(
                "SELECT id,snapshot_json FROM style_versions WHERE snapshot_json<>'{}'"
            ).fetchall():
                snapshot = json.loads(row["snapshot_json"])
                cleaned = {
                    key: value
                    for key, value in snapshot.items()
                    if key not in forgotten
                }
                self.db.execute(
                    "UPDATE style_versions SET snapshot_json=? WHERE id=?",
                    (json.dumps(cleaned, ensure_ascii=False), row["id"]),
                )
            return count
