"""Pydantic models for the v2.0 vehicle endpoints."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class VehicleIn(BaseModel):
    """Input shape for POST /vehicles. Required: ref, make, model."""

    model_config = ConfigDict(extra="ignore")

    ref: str
    make: str
    model: str
    year_range: Optional[str] = None
    paint_code: Optional[str] = None
    paint_name: Optional[str] = None
    engine_code: Optional[str] = None
    transmission: Optional[str] = None
    vin: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = True


class VehicleUpdate(BaseModel):
    """Input shape for PATCH /vehicles/{ref}. All fields optional. Any 'ref'
    field in the body is ignored — the URL path ref is authoritative
    (Phase 3b hard rule #9: ref is immutable after creation)."""

    model_config = ConfigDict(extra="ignore")

    make: Optional[str] = None
    model: Optional[str] = None
    year_range: Optional[str] = None
    paint_code: Optional[str] = None
    paint_name: Optional[str] = None
    engine_code: Optional[str] = None
    transmission: Optional[str] = None
    vin: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class VehicleOut(VehicleIn):
    """Response shape — adds timestamps."""

    created_at: str
    updated_at: str
