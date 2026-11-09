"""Versioned, transactional upgrades for persona learning data."""

import json

SCHEMA_VERSION = 1


def upgrade(store):
    with store.transaction() as db:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row and row[0] > SCHEMA_VERSION:
            raise RuntimeError("数据由更新版本创建，不能用旧版覆盖")
        if row and row[0] == SCHEMA_VERSION:
            return
        db.execute("ALTER TABLE styles RENAME TO styles_v0")
        db.execute("""CREATE TABLE styles (
            id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, expression TEXT NOT NULL,
            meaning TEXT NOT NULL, contexts TEXT NOT NULL, avoid TEXT NOT NULL,
            source_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("INSERT INTO styles SELECT * FROM styles_v0")
        db.execute("DROP TABLE styles_v0")
        statements = (
            "CREATE INDEX styles_scope ON styles(scope_key,status)",
            """CREATE TABLE learning_queue (
                source_id TEXT NOT NULL REFERENCES sources(id), lane TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(source_id,lane))""",
            "CREATE INDEX learning_queue_pending ON learning_queue(status,lane)",
            """CREATE TABLE memory_traits (
                memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
                pinned INTEGER NOT NULL DEFAULT 0, qualifiers TEXT NOT NULL DEFAULT '[]',
                retention TEXT NOT NULL DEFAULT 'durable', expires_at REAL,
                confidence REAL NOT NULL DEFAULT 1, evidence TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1)""",
            """CREATE TABLE memory_terms (
                memory_id INTEGER NOT NULL REFERENCES memories(id), term TEXT NOT NULL,
                PRIMARY KEY(memory_id,term))""",
            "CREATE INDEX memory_terms_lookup ON memory_terms(term,memory_id)",
            """CREATE TABLE style_traits (
                style_id INTEGER PRIMARY KEY REFERENCES styles(id),
                form_type TEXT NOT NULL DEFAULT 'phrase', situation TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL DEFAULT 'casual', keywords TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '[]', signature TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1, review_reason TEXT NOT NULL DEFAULT '')""",
            "CREATE INDEX style_signature ON style_traits(signature)",
            """CREATE TABLE style_usage (
                style_id INTEGER NOT NULL REFERENCES styles(id), scope_key TEXT NOT NULL,
                injections INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0,
                positive INTEGER NOT NULL DEFAULT 0, negative INTEGER NOT NULL DEFAULT 0,
                last_injected REAL, last_used REAL, PRIMARY KEY(style_id,scope_key))""",
            """CREATE TABLE delivery_receipts (
                turn_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, sent_at REAL NOT NULL,
                style_ids TEXT NOT NULL, used_ids TEXT NOT NULL)""",
            """CREATE TABLE dialogue_turns (
                turn_id TEXT PRIMARY KEY REFERENCES sources(id), persona_id TEXT NOT NULL,
                scope_key TEXT NOT NULL, user_id TEXT NOT NULL, reply_text TEXT NOT NULL,
                sent_at REAL NOT NULL, feedback TEXT NOT NULL DEFAULT '')""",
            "CREATE INDEX dialogue_person ON dialogue_turns(scope_key,user_id,sent_at)",
            """CREATE TABLE feedback_events (
                source_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, polarity TEXT NOT NULL,
                style_ids TEXT NOT NULL, created_at REAL NOT NULL)""",
            "ALTER TABLE style_versions ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'",
        )
        for sql in statements:
            db.execute(sql)
        db.execute("""INSERT INTO learning_queue(source_id,lane,status)
            SELECT id,purpose,status FROM sources WHERE status IN ('pending','done','failed')""")
        for item in db.execute(
            "SELECT * FROM memories WHERE status<>'forgotten'"
        ).fetchall():
            db.execute(
                "INSERT INTO memory_traits(memory_id,pinned) VALUES (?,?)",
                (item["id"], int(item["key"] in ("preferred_name", "response_length"))),
            )
            store.index_memory(item["id"])
        for item in db.execute(
            "SELECT * FROM styles WHERE status<>'forgotten'"
        ).fetchall():
            db.execute(
                "INSERT INTO style_traits(style_id,situation) VALUES (?,?)",
                (item["id"], "；".join(json.loads(item["contexts"]))),
            )
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version',?)",
            (SCHEMA_VERSION,),
        )
