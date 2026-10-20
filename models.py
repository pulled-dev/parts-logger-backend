"""Pydantic models for the v2.0 vehicle endpoints."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

VehicleStatus = Literal["on_yard", "stripped", "crushed", "unknown"]


def _iso_date(value: Optional[str]) -> Optional[str]:
    """arrived_at is a plain YYYY-MM-DD date; blank means not known."""
    if value is None or str(value).strip() == "":
        return None
    return date.fromisoformat(str(value).strip()).isoformat()


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
    damage_notes: Optional[str] = None
    arrived_at: Optional[str] = None

    _check_arrived = field_validator("arrived_at")(_iso_date)


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
    damage_notes: Optional[str] = None
    arrived_at: Optional[str] = None

    _check_arrived = field_validator("arrived_at")(_iso_date)


class StatusUpdate(BaseModel):
    """Body for PATCH /vehicles/{ref}/status. damage_notes is only changed
    when sent (null or "" clears it)."""

    model_config = ConfigDict(extra="ignore")

    status: VehicleStatus
    damage_notes: Optional[str] = None
    changed_by: Optional[str] = None


class VehicleOut(VehicleIn):
    """Response shape — adds timestamps."""

    created_at: str
    updated_at: str
    status: str = "unknown"
    status_changed_at: Optional[str] = None
    status_changed_by: Optional[str] = None
    # Set when the paint name's colour contradicts the paint code (e.g. LC9X
    # with "Reflex Silver"); computed on read, never stored.
    paint_warning: Optional[str] = None


class Availability(BaseModel):
    """GET /vehicles/{ref}/availability: what the eBay message bot needs to
    say whether a donor car is still on the yard."""

    ref: str
    make: str
    model: str
    year: Optional[str] = None
    engine_code: Optional[str] = None
    paint_code: Optional[str] = None
    status: str
    status_changed_at: Optional[str] = None
    damage_notes: Optional[str] = None
