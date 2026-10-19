"""Demo identity selection only; this is not login authentication."""
import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from . import store

router = APIRouter(prefix="/business", tags=["demo business"])
logger = logging.getLogger(__name__)
SOURCE = "demo_business_db"


def user_identity(user_id: Annotated[str, Query(min_length=1)]) -> str:
    value = user_id.strip()
    if not value:
        raise HTTPException(422, "invalid_user_id")
    return value


User = Annotated[str, Depends(user_identity)]
Limit = Annotated[int, Query(ge=1, le=20)]


def query(operation, *args):
    try:
        return operation(store.database_path(), *args)
    except (sqlite3.Error, OSError) as error:
        logger.warning("Business query unavailable: %s", error)
        raise HTTPException(503, "business_unavailable") from error


def listing(items: list[dict]) -> dict:
    return {"items": items, "count": len(items), "source": SOURCE}


def detail(item: dict | None) -> dict:
    if item is None:
        raise HTTPException(404, "not_found")
    return {"item": item, "source": SOURCE}


@router.get("/orders")
def orders(user_id: User, limit: Limit = 10):
    return listing(query(store.list_orders, user_id, limit))


@router.get("/orders/{order_id}")
def order(order_id: str, user_id: User):
    return detail(query(store.get_order, user_id, order_id))


@router.get("/bills")
def bills(user_id: User, order_id: str | None = None, limit: Limit = 10):
    return listing(query(store.list_bills, user_id, order_id, limit))


@router.get("/bills/{bill_id}")
def bill(bill_id: str, user_id: User):
    return detail(query(store.get_bill, user_id, bill_id))
