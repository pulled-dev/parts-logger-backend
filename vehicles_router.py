"""Vehicle CRUD endpoints for Parts Logger v2.0.

Routes are registered via FastAPI APIRouter so main.py can mount them with a
single line, leaving the existing /lookup and /health logic untouched.

All endpoints return JSON. Errors return {"error": "message"}.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

import db
from models import VehicleIn, VehicleOut


router = APIRouter()


def _err(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


@router.post("/vehicles", response_model=VehicleOut, status_code=201)
def create_vehicle(payload: VehicleIn):
    data = payload.model_dump()
    data["ref"] = db.normalise_ref(data.get("ref"))
    if not data["ref"]:
        return _err(400, "ref is required")
    try:
        saved = db.create_vehicle(data)
    except ValueError as e:
        return _err(400, str(e))
    return JSONResponse(status_code=201, content=saved)


@router.get("/vehicles")
def list_vehicles():
    return db.list_vehicles()


@router.get("/vehicles/{ref}", response_model=VehicleOut)
def get_vehicle(ref: str):
    record = db.get_vehicle(ref)
    if record is None:
        return _err(404, "vehicle not found")
    return record


@router.put("/vehicles/{ref}", response_model=VehicleOut)
def update_vehicle(ref: str, payload: VehicleIn):
    data = payload.model_dump()
    norm = db.normalise_ref(ref)
    if not norm:
        return _err(400, "ref is required")
    updated = db.update_vehicle(norm, data)
    if updated is None:
        return _err(404, "vehicle not found")
    return updated


@router.delete("/vehicles/{ref}")
def delete_vehicle(ref: str):
    norm = db.normalise_ref(ref)
    if not norm:
        return _err(400, "ref is required")
    deleted = db.delete_vehicle(norm)
    if not deleted:
        return _err(404, "vehicle not found")
    return Response(status_code=204)
