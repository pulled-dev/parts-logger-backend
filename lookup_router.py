"""Smart lookup endpoint for body panels, engines & gearboxes.

GET /lookup-panel?ref=X&category=Y

Constructs an eBay search query from the vehicle context + category,
returns the query and pre-built eBay URLs (sold + live). No eBay API
calls — the frontend opens eBay in a new tab.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from db import normalise_ref, get_vehicle
from categories import (
    CATEGORIES,
    VALID_CATEGORY_KEYS,
    get_category,
    list_category_keys,
    BODY_PANEL_CATEGORIES,
)
from paint_codes import PAINT_CODES, lookup_paint_name


router = APIRouter()


@router.get("/body-panel-categories")
def body_panel_categories():
    """Return body panel category list for Body Panel mode tap-grid."""
    return BODY_PANEL_CATEGORIES


@router.get("/paint-codes")
def paint_codes():
    """Return VAG paint code dictionary as a list of {code, name} objects.

    Frontend caches this in window.PA_PAINT_CODES on init and uses it for
    paint-code dropdowns / labels (Phase 3b Task 2)."""
    return [{"code": code, "name": name} for code, name in PAINT_CODES.items()]


def _err(status_code: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


@router.get("/lookup-panel")
def lookup_panel(
    ref: str = Query("", description="Vehicle ref (e.g. VEH47 or 47)"),
    category: str = Query("", description="Category key (e.g. front_bumper)"),
):
    # ── Validate category ──────────────────────────────────────────
    if category not in VALID_CATEGORY_KEYS:
        return _err(400, {
            "error": "invalid category",
            "valid_categories": list_category_keys(),
        })

    # ── Validate & normalise ref ───────────────────────────────────
    try:
        norm_ref = normalise_ref(ref)
    except ValueError:
        return _err(400, {"error": "invalid ref"})

    # ── Look up vehicle ────────────────────────────────────────────
    vehicle = get_vehicle(norm_ref)
    if vehicle is None:
        return _err(404, {"error": "vehicle not found", "ref": norm_ref})

    cat = get_category(category)

    # ── Build search query ─────────────────────────────────────────
    parts = [vehicle["make"], vehicle["model"]]

    year_range = vehicle.get("year_range")
    if year_range:
        parts.append(year_range)

    parts.append(cat["search_keywords"])

    if cat["use_paint_code"] and vehicle.get("paint_code"):
        parts.append(vehicle["paint_code"])

    search_query = " ".join(parts)

    # ── Build eBay URLs ────────────────────────────────────────────
    encoded = quote_plus(search_query)
    ebay_sold_url = f"https://www.ebay.co.uk/sch/i.html?_nkw={encoded}&LH_Sold=1&LH_Complete=1"
    ebay_live_url = f"https://www.ebay.co.uk/sch/i.html?_nkw={encoded}"

    # ── Paint name resolution ──────────────────────────────────────
    paint_name = vehicle.get("paint_name")
    if not paint_name and vehicle.get("paint_code"):
        paint_name = lookup_paint_name(vehicle["paint_code"])

    return {
        "ref": norm_ref,
        "category": category,
        "category_label": cat["label"],
        "vehicle": {
            "make": vehicle["make"],
            "model": vehicle["model"],
            "year_range": vehicle.get("year_range"),
            "paint_code": vehicle.get("paint_code"),
            "paint_name": paint_name,
            "engine_code": vehicle.get("engine_code"),
        },
        "search_query": search_query,
        "ebay_sold_url": ebay_sold_url,
        "ebay_live_url": ebay_live_url,
    }
