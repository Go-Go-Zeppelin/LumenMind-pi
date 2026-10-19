import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from business import store
from business.api import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "business.sqlite3"
    monkeypatch.setenv("BUSINESS_DB_PATH", str(path))
    store.initialize(path, seed_demo=True)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_initialization_persists_and_never_overwrites(tmp_path):
    path = tmp_path / "nested" / "business.sqlite3"
    store.initialize(path)
    assert store.list_orders(path, "u1001") == []
    store.initialize(path, True)
    with store.connect(path) as db:
        db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = 'ORD-1001-001'")
        db.execute("UPDATE bills SET payment_method = 'test' WHERE bill_id = 'BILL-1001-001'")
    store.initialize(path, True)
    store.initialize(path, True)
    with store.connect(path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM bills").fetchone()[0] == 4
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.get_order(path, "u1001", "ORD-1001-001")["status"] == "cancelled"
    assert store.get_bill(path, "u1001", "BILL-1001-001")["payment_method"] == "test"


def test_four_queries_and_amounts(client):
    orders = client.get("/business/orders", params={"user_id": " u1001 "}).json()
    assert orders["source"] == "demo_business_db"
    assert orders["count"] == 3
    assert [o["order_id"] for o in orders["items"]] == ["ORD-1001-003", "ORD-1001-002", "ORD-1001-001"]
    order = client.get("/business/orders/ORD-1001-001?user_id=u1001").json()["item"]
    assert (order["product_name"], order["total_amount_cents"], order["status"]) == ("无线键盘", 12900, "shipped")
    bills = client.get("/business/bills?user_id=u1001").json()
    assert bills["count"] == 3
    assert [b["bill_id"] for b in bills["items"]] == ["BILL-1001-003", "BILL-1001-002", "BILL-1001-001"]
    filtered = client.get("/business/bills?user_id=u1001&order_id=ORD-1001-001").json()
    assert filtered["count"] == 1
    assert (filtered["items"][0]["amount_cents"], filtered["items"][0]["status"]) == (12900, "paid")
    bill = client.get("/business/bills/BILL-1001-002?user_id=u1001").json()["item"]
    assert (bill["amount_cents"], bill["status"], bill["payment_method"], bill["paid_at"]) == (5900, "unpaid", None, None)
    assert client.get("/business/orders?user_id=u1001&limit=1").json()["count"] == 1
    assert client.get("/business/bills?user_id=u1001&limit=2").json()["count"] == 2


@pytest.mark.parametrize("resource,prefix", [("orders", "ORD"), ("bills", "BILL")])
def test_user_scoping_and_indistinguishable_not_found(client, resource, prefix):
    own = client.get(f"/business/{resource}?user_id=u1002").json()
    assert own["count"] == 1
    assert own["items"][0][f"{'order' if resource == 'orders' else 'bill'}_id"] == f"{prefix}-1002-001"
    assert client.get(f"/business/{resource}/{prefix}-1002-001?user_id=u1002").status_code == 200
    foreign = client.get(f"/business/{resource}/{prefix}-1002-001?user_id=u1001")
    missing = client.get(f"/business/{resource}/{prefix}-9999-999?user_id=u1001")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "not_found"}
    for user in ("anonymous", "demo", "unknown", "u1001' OR 1=1 --"):
        assert client.get(f"/business/{resource}", params={"user_id": user}).json()["items"] == []
    assert client.get("/business/bills?user_id=u1001&order_id=ORD-1002-001").json()["items"] == []


@pytest.mark.parametrize("resource", ["orders", "bills"])
@pytest.mark.parametrize("params", [{}, {"user_id": ""}, {"user_id": "  "}, {"user_id": "u1001", "limit": 0}, {"user_id": "u1001", "limit": 21}, {"user_id": "u1001", "limit": "bad"}])
def test_bad_parameters(client, resource, params):
    assert client.get(f"/business/{resource}", params=params).status_code == 422


@pytest.mark.parametrize("path", ["orders", "orders/ORD-1001-001", "bills", "bills/BILL-1001-001"])
def test_unavailable_is_not_empty(client, monkeypatch, path):
    def fail(_path):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "connect", fail)
    response = client.get(f"/business/{path}?user_id=u1001")
    assert response.status_code == 503
    assert response.json() == {"detail": "business_unavailable"}
