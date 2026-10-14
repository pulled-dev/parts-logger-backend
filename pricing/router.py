"""Pricing endpoints.

Write endpoints (importing sales, syncing orders) need the `X-Admin-Token`
header to match the PRICING_ADMIN_TOKEN env var, and are disabled entirely when
that var isn't set.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pricing import ebay_orders, history, store
from pricing.sales_import import import_rows, sanitise_row

router = APIRouter()

MAX_IMPORT_ROWS = 2000


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _check_admin(token: str | None) -> JSONResponse | None:
    expected = os.environ.get("PRICING_ADMIN_TOKEN", "")
    if not expected:
        return _err(503, "admin endpoints disabled: PRICING_ADMIN_TOKEN not set")
    if not token or not hmac.compare_digest(token, expected):
        return _err(401, "bad or missing X-Admin-Token")
    return None


class ImportBody(BaseModel):
    rows: list[dict]
    source: str = "csv_clean"
    dry_run: bool = False


@router.post("/price/sold-history/import")
def import_sold_history(body: ImportBody, x_admin_token: str | None = Header(default=None)):
    """Rows come from scripts/import_ebay_sales.py --api, already PII-stripped on
    Naveed's PC. They're re-filtered through the allowlist here anyway."""
    denied = _check_admin(x_admin_token)
    if denied:
        return denied
    if body.source not in ("csv_clean", "csv_raw"):
        return _err(400, "source must be csv_clean or csv_raw")
    if len(body.rows) > MAX_IMPORT_ROWS:
        return _err(400, f"max {MAX_IMPORT_ROWS} rows per request")
    rows = [sanitise_row(r) for r in body.rows]
    with store.session() as conn:
        return import_rows(conn, rows, body.source, dry_run=body.dry_run)


@router.post("/price/sync-orders")
async def sync_orders(dry_run: bool = False, x_admin_token: str | None = Header(default=None)):
    denied = _check_admin(x_admin_token)
    if denied:
        return denied
    if not ebay_orders.is_configured():
        return _err(503, "Fulfillment sync not configured: set EBAY_USER_REFRESH_TOKEN (see HANDOFF.md)")
    with store.session() as conn:
        try:
            return await ebay_orders.sync_orders(conn, dry_run=dry_run)
        except Exception as e:  # surface eBay failures as 502, never a silent 500
            return _err(502, f"order sync failed: {e}")


@router.get("/price/part-types")
def part_types(min_sales: int = 3, limit: int = 200):
    """The own-sales part-type index (part type x model x generation [x engine])."""
    with store.session() as conn:
        return history.part_type_index(conn, min_sales=min_sales)[:limit]
