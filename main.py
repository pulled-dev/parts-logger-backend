"""
Pulled Apart — Parts Logger Backend
FastAPI server that handles eBay Browse API lookups and Claude AI identification.
Keeps API keys secure on the server side.
"""

import os
import re
import asyncio
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import anthropic

from vag_lookup import lookup_part as db_lookup, save_learned, reload_db, get_db, save_db
from claude_prompt import build_identification_prompt

# ── CONFIG ───────────────────────────────────────────────────────
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Toggle mock mode when no keys are set
USE_MOCK = not (EBAY_APP_ID and EBAY_CERT_ID)

app = FastAPI(title="Pulled Apart Parts Logger API")

# Allow frontend to call this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MODELS ───────────────────────────────────────────────────────

class LookupRequest(BaseModel):
    part_number: str
    vehicle_ref: str = ""

class LookupResponse(BaseModel):
    part_number: str
    description: str
    avg_price: float | None = None
    median_price: float | None = None
    low_price: float | None = None
    high_price: float | None = None
    suggested_price: float | None = None
    total_listings: int = 0
    confidence: str = "none"  # high/medium/low/none
    source: str = "none"
    breakerpro_price: float | None = None  # historical price from BreakerPro database

class CorrectionRequest(BaseModel):
    part_number: str
    corrected_description: str
    price: Optional[float] = None

class HealthResponse(BaseModel):
    status: str
    mode: str
    ebay_configured: bool
    claude_configured: bool

# ── MOCK DATA ────────────────────────────────────────────────────

MOCK_DATA = {
    "5G0927225D": {"description": "Electric Handbrake Switch", "avg": 24.99, "low": 14.99, "high": 34.99, "count": 18},
    "1T0857756C": {"description": "Seatbelt Buckle OSF", "avg": 29.99, "low": 19.99, "high": 39.99, "count": 12},
    "1K0315065AR": {"description": "Webasto Auxiliary Heater", "avg": 69.99, "low": 45.00, "high": 89.99, "count": 6},
    "8P0920982H": {"description": "Speedo Cluster", "avg": 24.99, "low": 15.00, "high": 39.99, "count": 22},
    "8P0953549K": {"description": "Steering Angle Sensor", "avg": 24.99, "low": 14.99, "high": 34.99, "count": 15},
    "1K0959653D": {"description": "Slip Ring", "avg": 19.99, "low": 12.99, "high": 29.99, "count": 20},
    "8P0953519F": {"description": "Wiper Stalk", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 25},
    "5G0907426M": {"description": "Heater Control Panel", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 14},
    "5Q0959655M": {"description": "SRS Airbag Module", "avg": 59.99, "low": 39.99, "high": 79.99, "count": 8},
}


def mock_lookup(part_number: str) -> LookupResponse:
    clean = part_number.strip().upper().replace(" ", "")
    if clean in MOCK_DATA:
        d = MOCK_DATA[clean]
        avg_price = d["avg"]
        suggested = round(avg_price * 0.875, 2)  # 12.5% below average
        return LookupResponse(
            part_number=clean, description=d["description"],
            avg_price=avg_price, median_price=avg_price,
            low_price=d["low"], high_price=d["high"],
            suggested_price=suggested,
            total_listings=d["count"], confidence="high", source="mock",
        )
    if clean == "N/A" or clean == "":
        return LookupResponse(part_number=clean, description="—", source="mock")
    hash_val = sum(ord(c) for c in clean)
    descs = ["Control Module", "Relay Unit", "Sensor Assembly", "Switch Unit",
             "Bracket Mount", "Cover Panel", "Trim Piece", "Wiring Loom"]
    base = 15 + (hash_val % 60)
    suggested = round(base * 0.875, 2)
    return LookupResponse(
        part_number=clean, description=descs[hash_val % len(descs)],
        avg_price=float(base), median_price=float(base),
        low_price=round(base * 0.6, 2),
        high_price=round(base * 1.4, 2),
        suggested_price=suggested,
        total_listings=3 + (hash_val % 20), confidence="medium", source="mock",
    )

# ── EBAY API ─────────────────────────────────────────────────────

_ebay_token_cache = {"token": None, "expires": 0}


async def get_ebay_token(client: httpx.AsyncClient) -> str:
    import time, base64
    if _ebay_token_cache["token"] and time.time() < _ebay_token_cache["expires"]:
        return _ebay_token_cache["token"]
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    resp = await client.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"eBay auth failed: {resp.status_code}")
    data = resp.json()
    _ebay_token_cache["token"] = data["access_token"]
    _ebay_token_cache["expires"] = time.time() + data.get("expires_in", 7200) - 60
    return data["access_token"]


async def search_ebay(client: httpx.AsyncClient, token: str, query: str) -> list[dict]:
    """Search eBay by part number (exact match preferred) then by description."""
    resp = await client.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DGB",
        },
        params={"q": query, "limit": "20", "filter": "conditionIds:{3000}", "sort": "price"},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("itemSummaries", [])


def calculate_pricing(listings: list[dict]) -> dict:
    """
    Extract pricing from eBay listings. 
    - Remove top/bottom 20% as outliers (if 5+ listings)
    - Calculate avg, median, low, high
    - Add confidence level based on listing count
    - Calculate suggested price (10-15% below average for competitive pricing)
    """
    prices = []
    for item in listings:
        try:
            price_info = item.get("price", {})
            price = float(price_info.get("value", 0))
            currency = price_info.get("currency", "GBP")
            if currency == "GBP" and 0.99 < price < 5000:
                prices.append(price)
        except (ValueError, TypeError):
            continue
    
    if not prices:
        return {
            "avg_price": None, "median_price": None, "low_price": None, 
            "high_price": None, "suggested_price": None, "confidence": "none", 
            "total_listings": 0
        }
    
    prices.sort()
    total = len(prices)
    
    # Confidence level based on number of listings
    if total >= 10:
        confidence = "high"
    elif total >= 5:
        confidence = "medium"
    elif total >= 1:
        confidence = "low"
    else:
        confidence = "none"
    
    # Remove outliers: top/bottom 20% if 5+ items, otherwise use all
    if total >= 5:
        trim_start = max(1, int(total * 0.2))
        trim_end = max(trim_start + 1, int(total * 0.8))
        trimmed = prices[trim_start:trim_end]
    else:
        trimmed = prices
    
    # Calculate statistics
    low_price = trimmed[0]
    high_price = trimmed[-1]
    avg_price = sum(trimmed) / len(trimmed)
    
    # Median from trimmed prices
    sorted_trimmed = sorted(trimmed)
    mid = len(sorted_trimmed) // 2
    median_price = (
        sorted_trimmed[mid] if len(sorted_trimmed) % 2 == 1
        else (sorted_trimmed[mid - 1] + sorted_trimmed[mid]) / 2
    )
    
    # Suggested price: 10-15% below average for competitive turnover
    # Using 12.5% as middle ground
    suggested_price = round(avg_price * 0.875, 2)
    
    return {
        "avg_price": round(avg_price, 2),
        "median_price": round(median_price, 2),
        "low_price": round(low_price, 2),
        "high_price": round(high_price, 2),
        "suggested_price": suggested_price,
        "confidence": confidence,
        "total_listings": total
    }

# ── CLAUDE API ───────────────────────────────────────────────────

async def identify_with_claude(part_number: str) -> str | None:
    """Identify a VAG part using Claude AI with comprehensive VAG knowledge."""
    if not ANTHROPIC_API_KEY:
        print("Claude API: ANTHROPIC_API_KEY not set")
        return None
    try:
        sdk_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        message = await sdk_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=80,
            messages=[
                {
                    "role": "user",
                    "content": build_identification_prompt(part_number),
                }
            ],
        )
        text = message.content[0].text.strip()
        # Safe string cleaning: remove leading/trailing punctuation/whitespace
        text = re.sub(r'^[\s.\'"]+|[\s.\'"]+$', '', text)
        # Limit to 8 words max (allow slightly longer for side designation)
        words = text.split()
        if len(words) > 8:
            text = " ".join(words[:8])
        return text if text else None
    except Exception as e:
        print(f"Claude API error: {type(e).__name__}: {e}")
        return None

# ── HELPERS ──────────────────────────────────────────────────────

def is_paint_code(code: str) -> bool:
    """Check if a code looks like a VAG paint code (e.g., LC9X, LB9A)."""
    if re.match(r'^L[A-Z][0-9][A-Z0-9]$', code):
        return True
    if re.match(r'^[A-Z][0-9][A-Z0-9]{2}$', code):
        return True
    return False

# ── ENDPOINTS ────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        mode="mock" if USE_MOCK else "live",
        ebay_configured=bool(EBAY_APP_ID and EBAY_CERT_ID),
        claude_configured=bool(ANTHROPIC_API_KEY),
    )


_NO_PRICING = {
    "avg_price": None, "median_price": None, "low_price": None,
    "high_price": None, "suggested_price": None, "confidence": "none",
    "total_listings": 0,
}


async def _ebay_pricing(client: httpx.AsyncClient, part_number: str, description: str) -> tuple[dict, str]:
    """Fetch eBay pricing for a part. Returns (pricing_dict, ebay_source_suffix)."""
    try:
        token = await get_ebay_token(client)
        listings = await search_ebay(client, token, part_number)
        if not listings and description:
            listings = await search_ebay(client, token, f"{description} VAG")
        if listings:
            return calculate_pricing(listings), "+ebay"
    except Exception as e:
        print(f"eBay lookup failed for {part_number}: {e}")
    return _NO_PRICING.copy(), ""


@app.post("/lookup", response_model=LookupResponse)
async def lookup_part(req: LookupRequest):
    part_number = req.part_number.strip()
    if not part_number:
        raise HTTPException(status_code=400, detail="Part number is required")

    if USE_MOCK:
        return mock_lookup(part_number)

    clean = part_number.upper().replace(" ", "")

    if clean in ("N/A", ""):
        return LookupResponse(part_number=clean, description="—", source="none")

    async with httpx.AsyncClient() as client:

        # Paint codes -> Claude only (no eBay market for paint codes)
        if is_paint_code(clean):
            claude_desc = await identify_with_claude(clean)
            return LookupResponse(
                part_number=clean,
                description=claude_desc or "Paint Code",
                source="claude",
            )

        # Engine/gearbox codes (only letters, no digits) -> Claude only
        if re.match(r'^[A-Za-z]{2,5}$', clean):
            claude_desc = await identify_with_claude(clean)
            return LookupResponse(
                part_number=clean,
                description=claude_desc or "Engine/Gearbox Code",
                source="claude",
            )

        # ── Database lookup (primary) ──────────────────────────────────────
        db_result = db_lookup(clean)

        if db_result:
            description     = db_result["description"]
            breakerpro_price = db_result.get("breakerpro_price")
            db_source       = db_result["source"]  # "database" or "learned"

            # Still fetch eBay pricing so user sees current market value
            pricing, ebay_suffix = await _ebay_pricing(client, clean, description)
            source = db_source + ebay_suffix  # e.g. "database+ebay" or "database"

            return LookupResponse(
                part_number=clean,
                description=description,
                source=source,
                breakerpro_price=breakerpro_price,
                **pricing,
            )

        # ── Claude fallback ────────────────────────────────────────────────
        # Run Claude + eBay in parallel for minimum latency
        claude_task = identify_with_claude(clean)
        ebay_task   = _ebay_pricing(client, clean, "")

        claude_desc, (pricing, ebay_suffix) = await asyncio.gather(claude_task, ebay_task)

        # If eBay by part number found nothing but Claude succeeded, try description search
        if not pricing["avg_price"] and claude_desc:
            pricing, ebay_suffix = await _ebay_pricing(client, clean, claude_desc)

        # Auto-save to learned section for future lookups
        if claude_desc and claude_desc.lower() != "unknown part":
            try:
                save_learned(clean, claude_desc)
                print(f"Learned: {clean} -> {claude_desc}")
            except Exception as e:
                print(f"Failed to save learned entry for {clean}: {e}")

        description = claude_desc or "Unknown Part"
        source      = ("claude" + ebay_suffix) if claude_desc else "none"

        return LookupResponse(
            part_number=clean,
            description=description,
            source=source,
            **pricing,
        )


@app.post("/lookup/batch", response_model=list[LookupResponse])
async def lookup_batch(parts: list[LookupRequest]):
    if len(parts) > 10:
        raise HTTPException(status_code=400, detail="Max 10 parts per batch")
    results = []
    for part in parts:
        try:
            result = await lookup_part(part)
            results.append(result)
        except Exception:
            results.append(LookupResponse(
                part_number=part.part_number, description="Lookup Failed", source="none"
            ))
    return results


@app.get("/db-stats")
async def db_stats():
    """Return current database entry counts."""
    db = get_db()
    return {
        "exact_entries":   len(db.get("exact", {})),
        "group_entries":   len(db.get("groups", {})),
        "learned_entries": len(db.get("learned", {})),
        "last_updated":    db.get("_meta", {}).get("last_updated", "unknown"),
    }


@app.post("/db-reload")
async def db_reload():
    """Reload the database from disk (useful after manual edits to vag_parts_db.json)."""
    reload_db()
    return {"status": "reloaded"}


@app.post("/db-correct")
async def db_correct(req: CorrectionRequest):
    """
    User corrected a description in the frontend.
    Saves as an exact match — user corrections are the highest authority.
    Promotes the entry out of 'learned' if it was there.
    """
    clean = req.part_number.strip().upper().replace(" ", "")
    if not clean:
        raise HTTPException(status_code=400, detail="part_number is required")

    db = get_db()

    db.setdefault("exact", {})[clean] = {
        "description":     req.corrected_description,
        "breakerpro_price": req.price,
        "vehicle":         None,
        "corrected":       True,
        "corrected_at":    datetime.now(timezone.utc).isoformat(),
    }

    # Remove from learned if present — it's now verified
    if clean in db.get("learned", {}):
        del db["learned"][clean]

    db.setdefault("_meta", {})["total_exact_entries"] = len(db["exact"])
    save_db(db)
    reload_db()

    return {
        "status":      "saved",
        "part_number": clean,
        "description": req.corrected_description,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
