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

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

import db
from models import VehicleIn, VehicleUpdate, VehicleOut


router = APIRouter()


def _err(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


@router.post("/vehicles", response_model=VehicleOut, status_code=201)
def create_vehicle(payload: VehicleIn):
    data = payload.model_dump()
    try:
        data["ref"] = db.normalise_ref(data.get("ref"))
        saved = db.create_vehicle(data)
    except db.RefAlreadyExists:
        return _err(409, "ref already exists")
    except ValueError as e:
        return _err(400, str(e))
    return JSONResponse(status_code=201, content=saved)


@router.get("/vehicles")
def list_vehicles(include_inactive: bool = False):
    return db.list_vehicles(include_inactive=include_inactive)


@router.get("/vehicles/{ref}", response_model=VehicleOut)
def get_vehicle(ref: str):
    record = db.get_vehicle(ref)
    if record is None:
        return _err(404, "vehicle not found")
    return record


@router.patch("/vehicles/{ref}", response_model=VehicleOut)
def update_vehicle(ref: str, payload: VehicleUpdate):
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
    return updated


@router.delete("/vehicles/{ref}")
def delete_vehicle(ref: str):
    """Soft delete: marks vehicle is_active = 0 (Phase 3b hard rule #10).
    The row is never removed from the database. Returns 204 on success,
    404 if the ref does not exist."""
    try:
        norm = db.normalise_ref(ref)
    except ValueError as e:
        return _err(400, str(e))
    deleted = db.delete_vehicle(norm)
    if not deleted:
        return _err(404, "vehicle not found")
    return Response(status_code=204)
