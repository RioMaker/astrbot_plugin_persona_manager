"""Repository facade: conversation evidence, memory, expression, and accounting."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .classified_memory import ClassifiedMemoryStore, upgrade_classified
from .dialogue import DialogueStore
from .expression_store import ExpressionStore
from .memory_store import MemoryStore
from .migrations import upgrade
from .storage_base import StorageBase


class Store(
    ClassifiedMemoryStore, MemoryStore, ExpressionStore, DialogueStore, StorageBase
):
    def __init__(self, path, **kwargs):
        existing = str(path) != ":memory:" and Path(path).is_file()
        super().__init__(path, **kwargs)
        try:
            version = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if existing and not version:
                backup = Path(path).with_suffix(".before-v1.sqlite3")
                if not backup.exists():
                    with TemporaryDirectory(
                        prefix="persona-backup-", dir=backup.parent
                    ) as temporary:
                        pending = Path(temporary) / "backup.sqlite3"
                        with closing(sqlite3.connect(str(pending))) as destination:
                            self.db.backup(destination)
                        pending.replace(backup)
                with closing(
                    sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
                ) as check:
                    if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("迁移前备份校验失败，请检查备份文件")
                    check.execute("SELECT id FROM memories LIMIT 1")
                    check.execute("SELECT id FROM styles LIMIT 1")
            if (
                existing
                and version
                and not self.db.execute(
                    "SELECT 1 FROM meta WHERE key='classified_memory_version'"
                ).fetchone()
            ):
                backup = Path(path).with_suffix(".before-v2.sqlite3")
                if not backup.exists():
                    with TemporaryDirectory(
                        prefix="persona-backup-", dir=backup.parent
                    ) as temporary:
                        pending = Path(temporary) / "backup.sqlite3"
                        with closing(sqlite3.connect(str(pending))) as destination:
                            self.db.backup(destination)
                        pending.replace(backup)
                with closing(
                    sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
                ) as check:
                    if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("迁移前备份校验失败，请检查备份文件")
            upgrade(self)
            upgrade_classified(self)
        except BaseException:
            self.db.close()
            raise
