"""Vehicle CRUD endpoints for Parts Logger v2.0.

Routes are registered via FastAPI APIRouter so main.py can mount them with a
single line, leaving the existing /lookup and /health logic untouched.

All endpoints return JSON. Errors return {"error": "message"}.

Phase 3b Task 0:
- POST /vehicles returns 409 on duplicate ref (was: silent overwrite).
- PATCH replaces PUT (semantics identical: ref is locked from URL path).
- DELETE is now a soft delete (sets is_active = 0) — no rows ever removed.
- GET /vehicles filters out is_active = 0 by default; pass include_inactive=true
  to include soft-deleted rows.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse

import db
from models import Availability, StatusUpdate, VehicleIn, VehicleUpdate, VehicleOut
from paint_codes import paint_warning


router = APIRouter()


def _err(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


# Every vehicle write needs the staff key (STAFF_API_KEY) in this header; the
# availability endpoint for the eBay message bot needs BOT_API_KEY in its own.
# Reads stay open.
STAFF_KEY_HEADER = "X-Staff-Key"
BOT_KEY_HEADER = "X-API-Key"


def _key_error(env_var: str, header: str, sent: str | None) -> JSONResponse | None:
    """None if `sent` matches the key in env_var, otherwise the error response.
    Fails closed: with no key configured nobody gets in."""
    expected = os.environ.get(env_var, "")
    if not expected:
        return _err(503, f"not configured on the server ({env_var} unset)")
    if not sent:
        return _err(401, f"missing API key: send it in the {header} header")
    if not hmac.compare_digest(sent.encode(), expected.encode()):
        return _err(401, "invalid API key")
    return None


def _staff_key(x_staff_key: str | None = Header(default=None, alias=STAFF_KEY_HEADER)):
    return x_staff_key


def _with_paint_check(record: dict) -> dict:
    """Attach paint_warning so the logger can flag a code/name mismatch or
    an unverified code with no name."""
    record["paint_warning"] = paint_warning(
        record.get("paint_code"), record.get("paint_name"))
    return record


@router.post("/vehicles", response_model=VehicleOut, status_code=201)
def create_vehicle(payload: VehicleIn, staff_key: str | None = Depends(_staff_key)):
    denied = _key_error("STAFF_API_KEY", STAFF_KEY_HEADER, staff_key)
    if denied:
        return denied
    data = payload.model_dump()
    try:
        data["ref"] = db.normalise_ref(data.get("ref"))
        saved = db.create_vehicle(data)
    except db.RefAlreadyExists:
        return _err(409, "ref already exists")
    except ValueError as e:
        return _err(400, str(e))
    return JSONResponse(status_code=201, content=_with_paint_check(saved))


@router.get("/vehicles")
def list_vehicles(include_inactive: bool = False):
    return [_with_paint_check(v)
            for v in db.list_vehicles(include_inactive=include_inactive)]


@router.get("/vehicles/{ref}", response_model=VehicleOut)
def get_vehicle(ref: str):
    record = db.get_vehicle(ref)
    if record is None:
        return _err(404, "vehicle not found")
    return _with_paint_check(record)


@router.patch("/vehicles/{ref}", response_model=VehicleOut)
def update_vehicle(ref: str, payload: VehicleUpdate, staff_key: str | None = Depends(_staff_key)):
    denied = _key_error("STAFF_API_KEY", STAFF_KEY_HEADER, staff_key)
    if denied:
        return denied
    # exclude_unset=True means only fields the client actually sent are merged.
    # Per spec: any 'ref' in body is ignored; URL path ref wins.
    data = payload.model_dump(exclude_unset=True)
    data.pop("ref", None)
    try:
        norm = db.normalise_ref(ref)
    except ValueError as e:
        return _err(400, str(e))
    updated = db.update_vehicle(norm, data)
    if updated is None:
        return _err(404, "vehicle not found")
    return _with_paint_check(updated)


@router.delete("/vehicles/{ref}")
def delete_vehicle(ref: str, staff_key: str | None = Depends(_staff_key)):
    """Soft delete: marks vehicle is_active = 0 (Phase 3b hard rule #10).
    The row is never removed from the database. Returns 204 on success,
    404 if the ref does not exist."""
    denied = _key_error("STAFF_API_KEY", STAFF_KEY_HEADER, staff_key)
    if denied:
        return denied
    try:
        norm = db.normalise_ref(ref)
    except ValueError as e:
        return _err(400, str(e))
    deleted = db.delete_vehicle(norm)
    if not deleted:
        return _err(404, "vehicle not found")
    return Response(status_code=204)


@router.patch("/vehicles/{ref}/status", response_model=VehicleOut)
def set_vehicle_status(ref: str, payload: StatusUpdate, staff_key: str | None = Depends(_staff_key)):
    """Set on_yard / stripped / crushed / unknown, plus damage_notes if sent.
    status_changed_at is stamped whenever the status actually changes."""
    denied = _key_error("STAFF_API_KEY", STAFF_KEY_HEADER, staff_key)
    if denied:
        return denied
    sent = payload.model_dump(exclude_unset=True)
    extra = {"damage_notes": sent["damage_notes"]} if "damage_notes" in sent else {}
    try:
        updated = db.set_vehicle_status(ref, payload.status,
                                        changed_by=payload.changed_by, **extra)
    except ValueError as e:
        return _err(400, str(e))
    if updated is None:
        return _err(404, "vehicle not found")
    return _with_paint_check(updated)


@router.get("/vehicles/{ref}/availability", response_model=Availability)
def vehicle_availability(ref: str,
                         x_api_key: str | None = Header(default=None, alias=BOT_KEY_HEADER)):
    """Read-only, for the eBay message bot: is this donor car still on the
    yard? Needs the X-API-Key header. Soft-deleted vehicles are still
    answered (they may well have been crushed)."""
    denied = _key_error("BOT_API_KEY", BOT_KEY_HEADER, x_api_key)
    if denied:
        return denied
    try:
        record = db.get_vehicle(ref)
    except ValueError as e:
        return _err(400, str(e))
    if record is None:
        return _err(404, f"no vehicle with ref {ref!r}")
    return {
        "ref": record["ref"],
        "make": record["make"],
        "model": record["model"],
        "year": record.get("year_range"),
        "engine_code": record.get("engine_code"),
        "paint_code": record.get("paint_code"),
        "status": record.get("status") or "unknown",
        "status_changed_at": record.get("status_changed_at"),
        "damage_notes": record.get("damage_notes"),
    }
