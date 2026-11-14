"""SQLite snapshot/receipt extension. One transaction publishes state and receipt together."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path


class PersistenceExtension:
    id, version, status = "persistence", "1", "available"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock_file = path.with_suffix(path.suffix + ".lock").open("a+")
        os.chmod(self._lock_file.name, 0o600)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise RuntimeError("This local database is already open by another service.") from None
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError("Unsupported local database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (
                epoch TEXT NOT NULL, route TEXT NOT NULL, request_id TEXT NOT NULL,
                digest TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(epoch,route,request_id));
            PRAGMA user_version=1;
        """)

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation == "load":
            row = self.db.execute("SELECT body FROM snapshot WHERE id=1").fetchone()
            return json.loads(row[0]) if row else {}
        if operation == "receipt":
            row = self.db.execute("SELECT digest,response FROM receipts WHERE epoch=? AND route=? AND request_id=?",
                                  (payload["epoch"], payload["route"], payload["request_id"])).fetchone()
            return {"digest": row[0], "response": json.loads(row[1]) if row[1] else None} if row else {}
        if operation == "commit":
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if payload.get("erase_receipts"):
                    self.db.execute("DELETE FROM receipts")
                if payload.get("receipt_cutoff"):
                    self.db.execute("DELETE FROM receipts WHERE created_at < ?", (payload["receipt_cutoff"],))
                self.db.execute("INSERT OR REPLACE INTO snapshot VALUES(1,?)",
                                (json.dumps(payload["document"], separators=(",", ":")),))
                r = payload.get("receipt")
                if r:
                    self.db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,?)",
                                    (r["epoch"], r["route"], r["request_id"], r["digest"],
                                     json.dumps(r["response"]) if r["response"] is not None else None, r["created_at"]))
                if payload.get("recover_pending"):
                    self.db.execute("UPDATE receipts SET response=? WHERE response IS NULL",
                                    (json.dumps(payload["document"]["state"]),))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            if payload.get("erase_receipts"):
                self.db.execute("VACUUM")
            return {"committed": True}
        if operation == "close":
            self.db.close()
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            return {"closed": True}
        raise ValueError("Unsupported persistence operation")
