from contextlib import closing
from pathlib import Path
import re
import sqlite3

from factory.evidence import Conflict, digest, encode


def receipt_for(operation_id: str, request_digest: str) -> dict:
    request = {"operation_id": operation_id, "request_digest": request_digest}
    return {"schema_version": 1, **request, "receipt_id": digest(encode(request))}


def validate_receipt(receipt: dict, operation_id: str, request_digest: str):
    if receipt != receipt_for(operation_id, request_digest):
        raise Conflict(f"conflicting fixture receipt: {operation_id}")


class Ledger:
    def __init__(self, database: Path):
        self.database = database
        with closing(self.connect()) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS effects (
                    operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
                    apply_requests INTEGER NOT NULL DEFAULT 1
                )
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.database)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def lookup(self, operation_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT request_digest FROM effects WHERE operation_id = ?", (operation_id,),
            ).fetchone()
        return receipt_for(operation_id, row[0]) if row else None

    def apply(self, operation_id: str, request_digest: str) -> dict:
        if not re.fullmatch(r"records-delivery:[a-z0-9][a-z0-9-]{0,62}", operation_id):
            raise ValueError("invalid operation ID")
        if not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise ValueError("invalid request digest")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_digest FROM effects WHERE operation_id = ?", (operation_id,),
            ).fetchone()
            if row is None:
                db.execute("INSERT INTO effects VALUES (?, ?, 1)", (operation_id, request_digest))
            elif row[0] == request_digest:
                db.execute(
                    "UPDATE effects SET apply_requests = apply_requests + 1 WHERE operation_id = ?",
                    (operation_id,),
                )
            else:
                raise Conflict(f"conflicting fixture request: {operation_id}")
        return receipt_for(operation_id, request_digest)

    def inspect(self) -> dict:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM effects ORDER BY operation_id").fetchall()
        return {
            "effect_count": len(rows),
            "effects": [
                {**receipt_for(operation_id, request_digest), "apply_requests": apply_requests}
                for operation_id, request_digest, apply_requests in rows
            ],
        }
