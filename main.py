"""
Pulled Apart — Parts Logger Backend
FastAPI server that handles eBay Browse API lookups and Claude AI fallback.
Keeps API keys secure on the server side.
"""

import os
import re
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import Counter

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
    allow_origins=["*"],  # We'll lock this down to the Vercel domain later
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
    low_price: float | None = None
    high_price: float | None = None
    total_listings: int = 0
    source: str = "none"  # "ebay", "claude", "mock", "none"

class HealthResponse(BaseModel):
    status: str
    mode: str
    ebay_configured: bool
    claude_configured: bool

# ── MOCK DATA ────────────────────────────────────────────────────

MOCK_DATA = {
    "5G0927225D": {"description": "Electric Handbrake Switch", "avg": 24.99, "low": 14.99, "high": 34.99, "count": 18},
    "1T0857756C": {"description": "Seatbelt Buckle OSF", "avg": 29.99, "low": 19.99, "high": 39.99, "count": 12},
    "1T1857755E": {"description": "Seatbelt Buckle NSF", "avg": 29.99, "low": 19.99, "high": 39.99, "count": 10},
    "1K0315065AR": {"description": "Webasto Auxiliary Heater", "avg": 69.99, "low": 45.00, "high": 89.99, "count": 6},
    "8P0920982H": {"description": "Speedo Cluster", "avg": 24.99, "low": 15.00, "high": 39.99, "count": 22},
    "8P0953549K": {"description": "Steering Angle Sensor", "avg": 24.99, "low": 14.99, "high": 34.99, "count": 15},
    "1K0959653D": {"description": "Slip Ring", "avg": 19.99, "low": 12.99, "high": 29.99, "count": 20},
    "8P0953519F": {"description": "Wiper Stalk", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 25},
    "8P0953513F": {"description": "Indicator Stalk", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 20},
    "5G0907426M": {"description": "Heater Control Panel", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 14},
    "5Q0953549A": {"description": "Slip Ring", "avg": 19.99, "low": 12.99, "high": 29.99, "count": 18},
    "5Q0959655M": {"description": "SRS Airbag Module", "avg": 59.99, "low": 39.99, "high": 79.99, "count": 8},
    "5G0927225D": {"description": "Electric Handbrake Switch", "avg": 24.99, "low": 14.99, "high": 34.99, "count": 18},
    "5G2819703P": {"description": "Dash Air Vent N/S", "avg": 14.99, "low": 9.99, "high": 19.99, "count": 16},
    "5Q0937084S": {"description": "BCM Module", "avg": 34.99, "low": 24.99, "high": 49.99, "count": 10},
    "5Q2723503D": {"description": "Accelerator Pedal", "avg": 14.99, "low": 9.99, "high": 24.99, "count": 22},
    "LC9X": {"description": "Deep Black Pearl Paint Code", "avg": None, "low": None, "high": None, "count": 0},
    "LB9A": {"description": "Pure White Paint Code", "avg": None, "low": None, "high": None, "count": 0},
    "LC9A": {"description": "Deep Black Solid Paint Code", "avg": None, "low": None, "high": None, "count": 0},
    "LD7R": {"description": "Lapiz Blue Paint Code", "avg": None, "low": None, "high": None, "count": 0},
    "LY9T": {"description": "Indium Grey Paint Code", "avg": None, "low": None, "high": None, "count": 0},
    "LH1X": {"description": "Flash Red Paint Code", "avg": None, "low": None, "high": None, "count": 0},
}


def mock_lookup(part_number: str) -> LookupResponse:
    clean = part_number.strip().upper().replace(" ", "")
    if clean in MOCK_DATA:
        d = MOCK_DATA[clean]
        return LookupResponse(
            part_number=clean,
            description=d["description"],
            avg_price=d["avg"],
            low_price=d["low"],
            high_price=d["high"],
            total_listings=d["count"],
            source="mock",
        )
    if clean == "N/A" or clean == "":
        return LookupResponse(part_number=clean, description="—", source="mock")
    # Generate a plausible mock for unknown parts
    hash_val = sum(ord(c) for c in clean)
    descs = [
        "Control Module", "Relay Unit", "Sensor Assembly", "Switch Unit",
        "Bracket Mount", "Cover Panel", "Trim Piece", "Wiring Loom",
        "Motor Assembly", "Valve Unit", "Pump Assembly", "ECU Module",
    ]
    base = 15 + (hash_val % 60)
    return LookupResponse(
        part_number=clean,
        description=descs[hash_val % len(descs)],
        avg_price=float(base),
        low_price=round(base * 0.6, 2),
        high_price=round(base * 1.4, 2),
        total_listings=3 + (hash_val % 20),
        source="mock",
    )


# ── EBAY API ─────────────────────────────────────────────────────

_ebay_token_cache = {"token": None, "expires": 0}


async def get_ebay_token(client: httpx.AsyncClient) -> str:
    """Get or refresh eBay OAuth token."""
    import time
    if _ebay_token_cache["token"] and time.time() < _ebay_token_cache["expires"]:
        return _ebay_token_cache["token"]

    import base64
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()

    resp = await client.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
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
    """Search eBay UK active listings."""
    resp = await client.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DGB",
        },
        params={
            "q": query,
            "limit": "20",
            "filter": "conditionIds:{3000}",
            "sort": "price",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("itemSummaries", [])


def extract_short_description(listings: list[dict], part_number: str) -> str | None:
    """
    Extract a short breaker-style description from eBay listing titles.
    e.g. "Electric Handbrake Switch" not "VW Golf Mk7 2017 Genuine OEM..."
    """
    if not listings:
        return None

    titles = [item.get("title", "") for item in listings[:10]]

    # Remove common noise words and part-specific identifiers
    noise = re.compile(
        r'\b(genuine|oem|used|tested|vw|volkswagen|audi|seat|skoda|'
        r'golf|polo|leon|octavia|ibiza|fabia|superb|tiguan|passat|touran|caddy|'
        r'a1|a2|a3|a4|a5|a6|a7|a8|q2|q3|q5|q7|tt|'
        r'8p|8v|8j|4f|4g|4b|5f|5e|1t|3c|7n|'
        r'mk[0-9]|mk\s*[0-9]|[0-9]{4}[-–][0-9]{4}|[0-9]{4}|'
        r'lhd|rhd|uk|left|right|front|rear|driver|passenger|'
        r'n/s/f|o/s/f|n/s/r|o/s/r|n/s|o/s|nsf|osf|nsr|osr|ns|os|'
        r'ref|p/n|part\s*no|number|no\.|free\s*p&p|free\s*postage|fast\s*dispatch|'
        r'warranty|guaranteed|breaking|breaker[s]?|spares|'
        r'control|unit|button)\b',
        re.IGNORECASE
    )

    # Also strip the part number itself from titles
    pn_pattern = re.compile(re.escape(part_number.replace(" ", "")), re.IGNORECASE)

    cleaned = []
    for title in titles:
        t = noise.sub("", title)
        t = pn_pattern.sub("", t)
        t = re.sub(r'[^a-zA-Z\s/]', '', t)
        t = re.sub(r'\s+', ' ', t).strip()
        if len(t) > 3:
            cleaned.append(t)

    if not cleaned:
        return None

    # Find the most common 2-3 word phrases across titles
    word_groups = []
    for t in cleaned:
        words = t.split()
        # Extract 2-word and 3-word phrases
        for i in range(len(words)):
            if i + 2 <= len(words):
                word_groups.append(" ".join(words[i:i+2]))
            if i + 3 <= len(words):
                word_groups.append(" ".join(words[i:i+3]))

    if word_groups:
        counter = Counter(word_groups)
        most_common = counter.most_common(1)
        if most_common and most_common[0][1] >= 2:
            return most_common[0][0].title()

    # Fallback: use the shortest cleaned title
    cleaned.sort(key=len)
    result = cleaned[0]
    # Truncate to max ~40 chars
    if len(result) > 40:
        result = result[:40].rsplit(" ", 1)[0]
    return result.title()


def calculate_pricing(listings: list[dict]) -> dict:
    """Calculate pricing from eBay listings with outlier removal."""
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
        return {"avg_price": None, "low_price": None, "high_price": None, "total_listings": 0}

    prices.sort()

    # Remove top and bottom 10% as outliers
    trim_start = max(1, int(len(prices) * 0.1))
    trim_end = max(trim_start + 1, int(len(prices) * 0.9))
    trimmed = prices[trim_start:trim_end] if len(prices) > 4 else prices

    avg = sum(trimmed) / len(trimmed)

    return {
        "avg_price": round(avg, 2),
        "low_price": trimmed[0],
        "high_price": trimmed[-1],
        "total_listings": len(prices),
    }


# ── CLAUDE API FALLBACK ──────────────────────────────────────────

async def identify_with_claude(client: httpx.AsyncClient, part_number: str) -> str | None:
    """Use Claude to identify a part from its number or paint code."""
    if not ANTHROPIC_API_KEY:
        return None

    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 60,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You are a VAG vehicle parts expert working in a UK breakers yard. "
                            "Given this part number or paint code, respond with ONLY the short "
                            "breaker-style description (2-5 words max). No explanation. No punctuation. "
                            "Examples: Electric Handbrake Switch, Window Motor NSF, Bonnet Cable, "
                            "Deep Black Pearl Paint, Steering Angle Sensor, Wiper Stalk, BCM Module, "
                            "Accelerator Pedal, Slip Ring, Speedo Cluster.\n\n"
                            f"Part number/code: {part_number}"
                        ),
                    }
                ],
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "").strip()
        # Clean up - remove any trailing punctuation, quotes, etc
        text = text.strip('."\'').strip()
        # Limit to ~5 words
        words = text.split()
        if len(words) > 6:
            text = " ".join(words[:5])
        return text if text else None
    except Exception:
        return None


# ── ENDPOINTS ────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        mode="mock" if USE_MOCK else "live",
        ebay_configured=bool(EBAY_APP_ID and EBAY_CERT_ID),
        claude_configured=bool(ANTHROPIC_API_KEY),
    )


@app.post("/lookup", response_model=LookupResponse)
async def lookup_part(req: LookupRequest):
    part_number = req.part_number.strip()

    if not part_number:
        raise HTTPException(status_code=400, detail="Part number is required")

    # Mock mode
    if USE_MOCK:
        return mock_lookup(part_number)

    clean = part_number.upper().replace(" ", "")

    # Handle N/A - Claude only
    if clean == "N/A":
        return LookupResponse(part_number=clean, description="—", source="none")

    async with httpx.AsyncClient() as client:
        description = None
        pricing = {"avg_price": None, "low_price": None, "high_price": None, "total_listings": 0}
        source = "none"

        # Step 1: Try eBay
        try:
            token = await get_ebay_token(client)
            listings = await search_ebay(client, token, clean)

            if listings:
                description = extract_short_description(listings, clean)
                pricing = calculate_pricing(listings)
                source = "ebay"
        except Exception as e:
            print(f"eBay lookup failed for {clean}: {e}")

        # Step 2: Claude fallback for description if eBay didn't find one
        if not description:
            claude_desc = await identify_with_claude(client, part_number)
            if claude_desc:
                description = claude_desc
                source = "claude" if source == "none" else source

        # Step 3: Final fallback
        if not description:
            description = "Unknown Part"
            source = "none"

        return LookupResponse(
            part_number=clean,
            description=description,
            source=source,
            **pricing,
        )


@app.post("/lookup/batch", response_model=list[LookupResponse])
async def lookup_batch(parts: list[LookupRequest]):
    """Lookup multiple parts at once (max 10 per batch)."""
    if len(parts) > 10:
        raise HTTPException(status_code=400, detail="Max 10 parts per batch")

    results = []
    for part in parts:
        try:
            result = await lookup_part(part)
            results.append(result)
        except Exception:
            results.append(LookupResponse(
                part_number=part.part_number,
                description="Lookup Failed",
                source="none",
            ))
    return results


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
