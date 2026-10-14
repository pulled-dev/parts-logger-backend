"""Pricing endpoints.

Write endpoints (importing sales, syncing orders) need the `X-Admin-Token`
header to match the PRICING_ADMIN_TOKEN env var, and are disabled entirely when
that var isn't set.
"""

from __future__ import annotations

import hmac
import os

import pricing_config as cfg

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import db
from pricing import ebay_orders, engine, feedback, history, market, store
from pricing.normalise import normalise
from pricing.sales_import import import_rows, sanitise_row
from pricing.titles import engine_group, parse_model

router = APIRouter()

MAX_IMPORT_ROWS = 2000


def pricing_mode() -> str:
    """PRICING_MODE env var: 'live' or 'shadow'. Anything else -> shadow (the
    safe choice: the logger behaves exactly as it did before the engine)."""
    mode = os.environ.get("PRICING_MODE", cfg.DEFAULT_PRICING_MODE).strip().lower()
    return mode if mode in cfg.PRICING_MODES else cfg.DEFAULT_PRICING_MODE


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
        summary = import_rows(conn, rows, body.source, dry_run=body.dry_run)
        if not body.dry_run:
            summary["suggestions_linked"] = feedback.link_sales(conn)
        return summary


@router.post("/price/sync-orders")
async def sync_orders(dry_run: bool = False, x_admin_token: str | None = Header(default=None)):
    denied = _check_admin(x_admin_token)
    if denied:
        return denied
    if not ebay_orders.is_configured():
        return _err(503, "Fulfillment sync not configured: set EBAY_USER_REFRESH_TOKEN (see HANDOFF.md)")
    with store.session() as conn:
        try:
            summary = await ebay_orders.sync_orders(conn, dry_run=dry_run)
        except Exception as e:  # surface eBay failures as 502, never a silent 500
            return _err(502, f"order sync failed: {e}")
        if not dry_run:
            summary["suggestions_linked"] = feedback.link_sales(conn)
        return summary


@router.get("/price/part-types")
def part_types(min_sales: int = 3, limit: int = 200):
    """The own-sales part-type index (part type x model x generation [x engine])."""
    with store.session() as conn:
        return history.part_type_index(conn, min_sales=min_sales)[:limit]


def car_from_vehicle_ref(vehicle_ref: str | None) -> dict:
    """{model, generation, engine_group} from the vehicles table ({} if unknown)."""
    if not vehicle_ref:
        return {}
    try:
        v = db.get_vehicle(vehicle_ref)
    except ValueError:
        return {}
    if not v:
        return {}
    _, model, gen = parse_model(f"{v.get('make') or ''} {v.get('model') or ''}")
    return {"model": model, "generation": gen, "engine_group": engine_group(v.get("engine_code"))}


@router.get("/price/suggest")
async def price_suggest(
    part_number: str = Query(..., description="Part number, any format"),
    vehicle_ref: str | None = Query(None),
    part_type: str | None = Query(None, description="e.g. the description from /lookup"),
    log: bool = Query(True, description="record this suggestion in price_suggestions"),
):
    pn = normalise(part_number)
    if not pn:
        return _err(400, "part_number is not a recognisable part number")
    car = car_from_vehicle_ref(vehicle_ref)
    with store.session() as conn:
        mkt = await market.get_market(conn, pn)
        result = engine.suggest(conn, pn, part_type=part_type, car=car, market=mkt)
        mode = pricing_mode()
        result["mode"] = mode
        result["suggestion_id"] = (
            feedback.log_suggestion(conn, result, _clean_ref(vehicle_ref), mode=mode) if log else None
        )
        return result


def _clean_ref(vehicle_ref: str | None) -> str | None:
    if not vehicle_ref:
        return None
    try:
        return db.normalise_ref(vehicle_ref)
    except ValueError:
        return vehicle_ref.strip()[:40] or None


class FinalPrice(BaseModel):
    suggestion_id: int
    final_price: float
    current_price: float | None = None  # what the current method pre-filled


class FinalsBody(BaseModel):
    items: list[FinalPrice]


@router.post("/price/suggestions/final")
def suggestions_final(body: FinalsBody):
    """Called by the logger on CSV export with each card's final price and
    the current-method price it started from."""
    if len(body.items) > 1000:
        return _err(400, "max 1000 items")
    with store.session() as conn:
        return feedback.record_finals(conn, [i.model_dump() for i in body.items])


@router.get("/price/report")
def price_report(format: str = Query("json", pattern="^(json|csv)$")):
    """Current method vs engine vs final listed price (and vs the sold price
    once items sell, with days to sell), accuracy by confidence, most-overridden
    part types. format=csv downloads one row per suggestion."""
    with store.session() as conn:
        if format == "csv":
            return Response(
                content=feedback.report_csv(conn), media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=price-suggestions.csv"},
            )
        return feedback.report(conn)
