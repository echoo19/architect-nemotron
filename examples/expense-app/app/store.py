"""Receipts kept in SQLite, and the totals per day."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from typing import Any

from pydantic import BaseModel

from app.extract import Receipt

SCHEMA = """CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    total INTEGER,
    subtotal INTEGER,
    tax INTEGER,
    item_count INTEGER NOT NULL,
    text TEXT NOT NULL
)"""

DAILY = """SELECT date, COUNT(*) AS receipts, COALESCE(SUM(total), 0) AS total,
    COUNT(*) - COUNT(total) AS missing_total
FROM receipts GROUP BY date ORDER BY date"""


class StoredReceipt(Receipt):
    id: int
    date: dt.date
    text: str


class DayTotal(BaseModel):
    date: dt.date
    receipts: int
    total: int  # the sum of the totals the receipts print
    missing_total: int  # receipts that print no total


class Store:
    """One SQLite database (":memory:" keeps it in memory), shared between threads."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(SCHEMA)

    def add(self, date: dt.date, text: str, receipt: Receipt) -> StoredReceipt:
        row = {"date": date.isoformat(), "text": text, **receipt.model_dump()}
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO receipts (date, total, subtotal, tax, item_count, text) VALUES"
                " (:date, :total, :subtotal, :tax, :item_count, :text)",
                row,
            )
        return StoredReceipt.model_validate({**row, "id": cursor.lastrowid})

    def get(self, receipt_id: int) -> StoredReceipt | None:
        rows = self._rows("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
        return StoredReceipt.model_validate(rows[0]) if rows else None

    def receipts(self) -> list[StoredReceipt]:
        rows = self._rows("SELECT * FROM receipts ORDER BY id")
        return [StoredReceipt.model_validate(row) for row in rows]

    def daily_totals(self) -> list[DayTotal]:
        return [DayTotal.model_validate(row) for row in self._rows(DAILY)]

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args)]
