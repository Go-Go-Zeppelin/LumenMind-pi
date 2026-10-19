"""Fixed demo records; never overwrite existing rows."""
import sqlite3

ORDERS = [
    ("ORD-1001-001", "u1001", "无线键盘", 1, 12900, "CNY", "shipped", "2026-10-01T10:00:00+08:00"),
    ("ORD-1001-002", "u1001", "鼠标", 1, 5900, "CNY", "pending_payment", "2026-10-02T10:00:00+08:00"),
    ("ORD-1001-003", "u1001", "USB-C 扩展坞", 1, 9900, "CNY", "completed", "2026-10-03T10:00:00+08:00"),
    ("ORD-1002-001", "u1002", "机械键盘", 1, 19900, "CNY", "processing", "2026-10-03T11:00:00+08:00"),
]
BILLS = [
    ("BILL-1001-001", "ORD-1001-001", 12900, "CNY", "paid", "alipay", "2026-10-01T10:05:00+08:00", "2026-10-01T10:00:00+08:00"),
    ("BILL-1001-002", "ORD-1001-002", 5900, "CNY", "unpaid", None, None, "2026-10-02T10:00:00+08:00"),
    ("BILL-1001-003", "ORD-1001-003", 9900, "CNY", "paid", "wechat", "2026-10-03T10:05:00+08:00", "2026-10-03T10:00:00+08:00"),
    ("BILL-1002-001", "ORD-1002-001", 19900, "CNY", "paid", "alipay", "2026-10-03T11:05:00+08:00", "2026-10-03T11:00:00+08:00"),
]


def seed(db: sqlite3.Connection) -> None:
    # The caller's connection context commits both tables together.
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(order_id) DO NOTHING", ORDERS)
    db.executemany("INSERT INTO bills VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(bill_id) DO NOTHING", BILLS)
