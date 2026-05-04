"""Pydantic models for the v2.0 vehicle endpoints."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class VehicleIn(BaseModel):
    """Input shape for POST/PUT /vehicles. Required: ref, make, model."""

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


class VehicleOut(VehicleIn):
    """Response shape — adds timestamps."""

    created_at: str
    updated_at: str
