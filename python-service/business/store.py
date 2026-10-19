"""Fixed, user-scoped queries; one connection per operation."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def database_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    path = Path(os.getenv("BUSINESS_DB_PATH", "data/business/business.sqlite3")).expanduser()
    return path if path.is_absolute() else root / path


@contextmanager
def connect(path: Path):
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path: Path, seed_demo: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                total_amount_cents INTEGER NOT NULL CHECK(total_amount_cents >= 0),
                currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency = 'CNY'),
                status TEXT NOT NULL CHECK(status IN
                    ('pending_payment', 'processing', 'shipped', 'completed', 'cancelled')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bills (
                bill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
                currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency = 'CNY'),
                status TEXT NOT NULL CHECK(status IN ('unpaid', 'paid')),
                payment_method TEXT,
                paid_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS orders_user_id ON orders(user_id);
            CREATE INDEX IF NOT EXISTS bills_order_id ON bills(order_id);
        """)
        if seed_demo:
            from .seed import seed
            seed(db)


def list_orders(path: Path, user_id: str, limit: int = 10) -> list[dict]:
    with connect(path) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC, order_id DESC LIMIT ?",
            (user_id, limit),
        )]


def get_order(path: Path, user_id: str, order_id: str) -> dict | None:
    with connect(path) as db:
        row = db.execute("SELECT * FROM orders WHERE user_id = ? AND order_id = ?", (user_id, order_id)).fetchone()
        return dict(row) if row is not None else None


def list_bills(path: Path, user_id: str, order_id: str | None = None, limit: int = 10) -> list[dict]:
    with connect(path) as db:
        return [dict(row) for row in db.execute(
            """SELECT b.* FROM bills b JOIN orders o ON b.order_id = o.order_id
               WHERE o.user_id = ? AND (? IS NULL OR b.order_id = ?)
               ORDER BY b.created_at DESC, b.bill_id DESC LIMIT ?""",
            (user_id, order_id, order_id, limit),
        )]


def get_bill(path: Path, user_id: str, bill_id: str) -> dict | None:
    with connect(path) as db:
        row = db.execute(
            """SELECT b.* FROM bills b JOIN orders o ON b.order_id = o.order_id
               WHERE o.user_id = ? AND b.bill_id = ?""", (user_id, bill_id),
        ).fetchone()
        return dict(row) if row is not None else None
