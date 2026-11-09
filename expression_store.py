"""Expression candidates, explicit review, versioning and delivery-based selection."""

import json
from datetime import datetime

from .expression import eligibility, signature, validate_form
from .retrieval import normalized
from .style_matching import fits_group
from .temporal import BEIJING


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ExpressionStore:
    def _style_traits(self, row):
        if row is None:
            return None
        result = dict(row)
        traits = self.db.execute(
            "SELECT * FROM style_traits WHERE style_id=?", (row["id"],)
        ).fetchone()
        result.update(
            dict(traits)
            if traits
            else {
                "form_type": "phrase",
                "situation": "；".join(row["contexts"]),
                "intent": "casual",
                "keywords": "[]",
                "evidence": "[]",
                "signature": "",
                "revision": 1,
                "review_reason": "",
            }
        )
        for key in ("keywords", "evidence"):
            result[key] = json.loads(result[key])
        owners = {
            source["persona_id"]
            for source_id in row["source_ids"]
            if (source := self.get_source(source_id))
        }
        result["persona_id"] = next(iter(owners)) if len(owners) == 1 else ""
        usage = self.db.execute(
            """SELECT coalesce(sum(injections),0) AS injections,coalesce(sum(uses),0) AS uses,
            coalesce(sum(positive),0) AS positive,coalesce(sum(negative),0) AS negative
            FROM style_usage WHERE style_id=?""",
            (row["id"],),
        ).fetchone()
        result.update(dict(usage))
        return result

    def get_style(self, style_id):
        with self._lock:
            return self._style_traits(super().get_style(style_id))

    def styles(self, scope_key, status="active", limit=10):
        with self._lock:
            return [
                self._style_traits(row)
                for row in super().styles(scope_key, status, limit)
            ]

    def apply_styles(self, proposals, epoch):
        with self.transaction():
            if epoch != self.epoch():
                return []
            result = []
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                if "form" not in proposal:
                    ids = super().apply_styles([proposal], epoch)
                    for style_id in ids:
                        self.db.execute(
                            "INSERT OR IGNORE INTO style_traits(style_id,situation,evidence) VALUES (?,?,?)",
                            (
                                style_id,
                                str(proposal.get("usage", "")),
                                dump(proposal.get("evidence", [])),
                            ),
                        )
                    result.extend(ids)
                    continue
                try:
                    form = validate_form(proposal["form"])
                except (ValueError, TypeError):
                    continue
                sources = self._proposal_sources(proposal, interaction=False)
                if not sources or not proposal.get("evidence"):
                    continue
                if form["kind"] == "phrase" and not all(
                    form["pattern"] in item["quote"] for item in proposal["evidence"]
                ):
                    continue
                scope, identity = sources[0]["scope_key"], signature(form)
                matches = self.db.execute(
                    """SELECT s.* FROM styles s JOIN style_traits t ON t.style_id=s.id
                    WHERE s.scope_key=? AND t.signature=?""",
                    (scope, identity),
                ).fetchall()
                old = next(
                    (
                        row
                        for row in matches
                        if self.get_style(row["id"])["persona_id"]
                        == sources[0]["persona_id"]
                    ),
                    None,
                )
                evidence = list(proposal["evidence"])
                if old:
                    if old["status"] in ("forgotten", "disabled"):
                        continue
                    original = self.get_style(old["id"])
                    ids = sorted(
                        set(original["source_ids"]) | {x["id"] for x in sources}
                    )
                    evidence = list(
                        {
                            (x["source_id"], x["quote"]): x
                            for x in original["evidence"] + evidence
                        }.values()
                    )[-40:]
                    self.db.execute(
                        "UPDATE styles SET source_ids=?,updated_at=? WHERE id=?",
                        (dump(ids), self.clock(), old["id"]),
                    )
                    self.db.execute(
                        "UPDATE style_traits SET evidence=? WHERE style_id=?",
                        (dump(evidence), old["id"]),
                    )
                    result.append(old["id"])
                    continue
                meaning = str(proposal.get("meaning") or form["situation"])[:200]
                cursor = self.db.execute(
                    """INSERT INTO styles(scope_key,expression,meaning,contexts,avoid,source_ids,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        scope,
                        form["pattern"],
                        meaning,
                        dump([form["situation"]]),
                        dump([form["avoid"]] if form["avoid"] else []),
                        dump(sorted({x["id"] for x in sources})),
                        self.clock(),
                        self.clock(),
                    ),
                )
                self.db.execute(
                    """INSERT INTO style_traits(style_id,form_type,situation,intent,keywords,evidence,signature)
                    VALUES (?,?,?,?,?,?,?)""",
                    (
                        cursor.lastrowid,
                        form["kind"],
                        form["situation"],
                        form["intent"],
                        dump(form["keywords"]),
                        dump(evidence),
                        identity,
                    ),
                )
                result.append(cursor.lastrowid)
            return result

    def auto_activate_styles(
        self,
        scope_key,
        *,
        min_authors=3,
        min_days=2,
        min_evidence=5,
        daily_limit=2,
        active_limit=20,
        persona_id=None,
    ):
        with self.transaction():
            day = datetime.fromtimestamp(self.clock(), BEIJING).date().isoformat()
            used = self.db.execute(
                "SELECT count(*) FROM style_activations WHERE scope_key=? AND day=?",
                (scope_key, day),
            ).fetchone()[0]
            active = self.db.execute(
                "SELECT count(*) FROM styles WHERE scope_key=? AND status='active'",
                (scope_key,),
            ).fetchone()[0]
            remaining = min(max(0, daily_limit - used), max(0, active_limit - active))
            result = []
            for rule in self.styles(scope_key, "candidate", 1000):
                if persona_id is not None and rule["persona_id"] != persona_id:
                    continue
                if len(result) >= remaining:
                    break
                if rule["review_reason"]:
                    continue  # Feedback demotion cannot be undone by old frequency.
                evidence = [self.get_source(x) for x in rule["source_ids"]]
                independent = {
                    (x["user_id"], int(x["sent_at"] // 3600)): x
                    for x in evidence
                    if x and x["status"] != "forgotten"
                }
                if (
                    len(independent) < min_evidence
                    or len({x[0] for x in independent}) < min_authors
                    or len(
                        {
                            datetime.fromtimestamp(x["sent_at"], BEIJING).date()
                            for x in independent.values()
                        }
                    )
                    < min_days
                ):
                    continue
                if not result:
                    self._style_snapshot(scope_key)
                self.db.execute(
                    "UPDATE styles SET status='active',updated_at=? WHERE id=?",
                    (self.clock(), rule["id"]),
                )
                self.db.execute(
                    "INSERT INTO style_activations(scope_key,style_id,day) VALUES (?,?,?)",
                    (scope_key, rule["id"], day),
                )
                result.append(rule["id"])
            if result:
                self._style_snapshot(scope_key)
            return result

    def style_references(self, scope_key, *, persona_id=None):
        with self._lock:
            return [
                {
                    "handle": f"s{row['id']}:r{row['revision']}",
                    "status": row["status"],
                    "pattern": row["expression"],
                    "situation": row["situation"],
                    "intent": row["intent"],
                    "avoid": row["avoid"],
                    "review_reason": row["review_reason"],
                }
                for row in self.styles(scope_key, None, 30)
                if persona_id is None or row["persona_id"] == persona_id
            ]

    def select_styles(self, source, settings, *, now):
        with self._lock:
            local = (
                self.styles(source["scope_key"], limit=100)
                if settings.observes(source["group_id"], source["platform_id"])
                else []
            )
            global_rows = []
            tags = settings.tags_for(source["group_id"], source["platform_id"])
            if (
                settings.uses_global_styles(source["group_id"], source["platform_id"])
                and tags
            ):
                rows = self.db.execute(
                    """SELECT * FROM styles WHERE status='active' AND scope_key<>?
                    AND scope_key IN (SELECT DISTINCT scope_key FROM sources WHERE persona_id=?)
                    ORDER BY updated_at DESC LIMIT 1000""",
                    (source["scope_key"], source["persona_id"]),
                ).fetchall()
                for raw in rows:
                    row = dict(raw)
                    for key in ("contexts", "avoid", "source_ids"):
                        row[key] = json.loads(row[key])
                    row = self._style_traits(row)
                    if fits_group(row, tags, source["text"]):
                        global_rows.append(row)
            ranked = []
            cooling_patterns = (
                {
                    normalized(row["expression"])
                    for row in self.db.execute(
                        """SELECT s.expression FROM style_usage u JOIN styles s ON s.id=u.style_id
                    WHERE u.scope_key=? AND u.last_injected>?""",
                        (
                            source["scope_key"],
                            now - settings.expression_cooldown_seconds,
                        ),
                    )
                }
                if settings.expression_cooldown_seconds
                else set()
            )
            for origin, rows in enumerate((local, global_rows)):
                for row in rows:
                    if row["persona_id"] != source["persona_id"]:
                        continue
                    if normalized(row["expression"]) in cooling_patterns:
                        continue
                    score = eligibility(row, source["text"])
                    if score <= 0:
                        continue
                    usage = self.db.execute(
                        "SELECT * FROM style_usage WHERE style_id=? AND scope_key=?",
                        (row["id"], source["scope_key"]),
                    ).fetchone()
                    if (
                        usage
                        and usage["last_injected"] is not None
                        and now - usage["last_injected"]
                        < settings.expression_cooldown_seconds
                    ):
                        continue
                    frequency = usage["injections"] if usage else 0
                    ranked.append(
                        (origin, -score, frequency, -row["updated_at"], row["id"], row)
                    )
            ranked.sort(key=lambda x: x[:-1])
            result, seen = [], set()
            for *_, row in ranked:
                key = normalized(row["expression"])
                if key not in seen:
                    result.append(row)
                    seen.add(key)
                if len(result) >= settings.expression_per_turn:
                    break
            return result if settings.expression_per_turn else []

    def _style_snapshot(self, scope):
        version = super()._style_snapshot(scope)
        snapshot = {
            str(row["id"]): {
                key: row[key]
                for key in (
                    "expression",
                    "meaning",
                    "contexts",
                    "avoid",
                    "status",
                    "form_type",
                    "situation",
                    "intent",
                    "keywords",
                    "signature",
                    "review_reason",
                )
            }
            for row in self.styles(scope, None, 1000)
        }
        self.db.execute(
            "UPDATE style_versions SET snapshot_json=? WHERE id=?",
            (dump(snapshot), version),
        )
        return version

    def set_style_status(self, style_id, status):
        with self.transaction():
            row = self.get_style(style_id)
            if row is None:
                raise ValueError("表达不存在")
            self._style_snapshot(row["scope_key"])
            self.db.execute(
                "UPDATE style_traits SET revision=revision+1,review_reason=? WHERE style_id=?",
                ("" if status == "active" else row["review_reason"], style_id),
            )
            return super().set_style_status(style_id, status)

    def edit_style(self, style_id, form, *, revision):
        form = validate_form(form)
        with self.transaction():
            row = self.get_style(style_id)
            if (
                row is None
                or row["status"] == "forgotten"
                or type(revision) is not int
                or row["revision"] != revision
            ):
                raise ValueError("表达已改变，请刷新后重试")
            identity = signature(form)
            duplicate = self.db.execute(
                "SELECT s.id FROM styles s JOIN style_traits t ON t.style_id=s.id WHERE s.scope_key=? AND t.signature=? AND s.id<>? AND s.status<>'forgotten'",
                (row["scope_key"], identity, style_id),
            ).fetchone()
            if duplicate:
                raise ValueError("相同情境与模板已有规则，请编辑原规则")
            self._style_snapshot(row["scope_key"])
            self.db.execute(
                "UPDATE styles SET expression=?,meaning=?,contexts=?,avoid=?,status='candidate',updated_at=? WHERE id=?",
                (
                    form["pattern"],
                    form["situation"],
                    dump([form["situation"]]),
                    dump([form["avoid"]] if form["avoid"] else []),
                    self.clock(),
                    style_id,
                ),
            )
            self.db.execute(
                "UPDATE style_traits SET form_type=?,situation=?,intent=?,keywords=?,signature=?,revision=revision+1,review_reason='编辑后待审核' WHERE style_id=?",
                (
                    form["kind"],
                    form["situation"],
                    form["intent"],
                    dump(form["keywords"]),
                    identity,
                    style_id,
                ),
            )
            self._bump_epoch()
            return self._style_snapshot(row["scope_key"])

    def rollback_styles(self, scope_key, version):
        with self.transaction():
            row = self.db.execute(
                "SELECT snapshot_json FROM style_versions WHERE id=? AND scope_key=?",
                (version, scope_key),
            ).fetchone()
            if not row:
                raise ValueError("群表达版本不存在")
            snapshot = json.loads(row["snapshot_json"])
            if not snapshot:
                return super().rollback_styles(scope_key, version)
            self._style_snapshot(scope_key)
            self.db.execute(
                "UPDATE styles SET status='disabled' WHERE scope_key=? AND status<>'forgotten'",
                (scope_key,),
            )
            for key, value in snapshot.items():
                current = self.get_style(int(key))
                if (
                    current is None
                    or current["status"] == "forgotten"
                    or current["scope_key"] != scope_key
                ):
                    continue
                self.db.execute(
                    "UPDATE styles SET expression=?,meaning=?,contexts=?,avoid=?,status=?,updated_at=? WHERE id=?",
                    (
                        value["expression"],
                        value["meaning"],
                        dump(value["contexts"]),
                        dump(value["avoid"]),
                        value["status"],
                        self.clock(),
                        int(key),
                    ),
                )
                self.db.execute(
                    "UPDATE style_traits SET form_type=?,situation=?,intent=?,keywords=?,signature=?,review_reason=?,revision=revision+1 WHERE style_id=?",
                    (
                        value["form_type"],
                        value["situation"],
                        value["intent"],
                        dump(value["keywords"]),
                        value["signature"],
                        value["review_reason"],
                        int(key),
                    ),
                )
            self._bump_epoch()
            return self._style_snapshot(scope_key)
