from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from execution.safety_gate import SafetyDecision, SafetyRejection, SafetyRejectionCode


class SQLiteSafetyDecisionStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS safety_gate_decisions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "execution_request_id TEXT NOT NULL, "
                "allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)), "
                "checked_at_utc TEXT NOT NULL, "
                "rejections_json TEXT NOT NULL, "
                "account_id TEXT, position_count INTEGER)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_safety_gate_decisions_request "
                "ON safety_gate_decisions(execution_request_id, checked_at_utc DESC)"
            )

    def record(self, decision: SafetyDecision) -> None:
        payload = json.dumps(
            [{"code": item.code.value, "message": item.message} for item in decision.rejections],
            separators=(",", ":"),
        )
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO safety_gate_decisions "
                "(execution_request_id, allowed, checked_at_utc, rejections_json, account_id, position_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision.execution_request_id,
                    int(decision.allowed),
                    decision.checked_at_utc.isoformat(),
                    payload,
                    decision.account_id,
                    decision.position_count,
                ),
            )

    def list_for_request(self, execution_request_id: str) -> tuple[SafetyDecision, ...]:
        with sqlite3.connect(self._path) as connection:
            rows = connection.execute(
                "SELECT execution_request_id, allowed, checked_at_utc, rejections_json, "
                "account_id, position_count FROM safety_gate_decisions "
                "WHERE execution_request_id = ? ORDER BY checked_at_utc ASC, id ASC",
                (execution_request_id,),
            ).fetchall()
        return tuple(
            SafetyDecision(
                execution_request_id=row[0],
                allowed=bool(row[1]),
                checked_at_utc=datetime.fromisoformat(row[2]),
                rejections=tuple(
                    SafetyRejection(SafetyRejectionCode(item["code"]), item["message"])
                    for item in json.loads(row[3])
                ),
                account_id=row[4],
                position_count=row[5],
            )
            for row in rows
        )
