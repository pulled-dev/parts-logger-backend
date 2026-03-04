"""
Pulled Apart — Parts Logger Backend
FastAPI server that handles eBay Browse API lookups and Claude AI fallback.
Keeps API keys secure on the server side.
"""

import os
import re
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
    low_price: float | None = None
    high_price: float | None = None
    total_listings: int = 0
    source: str = "none"

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
        return LookupResponse(
            part_number=clean, description=d["description"],
            avg_price=d["avg"], low_price=d["low"], high_price=d["high"],
            total_listings=d["count"], source="mock",
        )
    if clean == "N/A" or clean == "":
        return LookupResponse(part_number=clean, description="—", source="mock")
    hash_val = sum(ord(c) for c in clean)
    descs = ["Control Module", "Relay Unit", "Sensor Assembly", "Switch Unit",
             "Bracket Mount", "Cover Panel", "Trim Piece", "Wiring Loom"]
    base = 15 + (hash_val % 60)
    return LookupResponse(
        part_number=clean, description=descs[hash_val % len(descs)],
        avg_price=float(base), low_price=round(base * 0.6, 2),
        high_price=round(base * 1.4, 2), total_listings=3 + (hash_val % 20), source="mock",
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
    trim_start = max(1, int(len(prices) * 0.1))
    trim_end = max(trim_start + 1, int(len(prices) * 0.9))
    trimmed = prices[trim_start:trim_end] if len(prices) > 4 else prices
    avg = sum(trimmed) / len(trimmed)
    return {"avg_price": round(avg, 2), "low_price": trimmed[0], "high_price": trimmed[-1], "total_listings": len(prices)}


# ── TITLE DESCRIPTION EXTRACTION ─────────────────────────────────

# Words stripped from eBay listing titles before phrase extraction
_TITLE_NOISE = frozenset({
    # Brands
    "vw", "volkswagen", "audi", "skoda", "seat", "cupra",
    # Models
    "golf", "polo", "passat", "tiguan", "touareg", "arteon", "phaeton",
    "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "q2", "q3", "q5", "q7", "q8",
    "tt", "tts", "ttrs", "r8", "rs3", "rs4", "rs5", "rs6", "rs7",
    "leon", "ibiza", "altea", "ateca", "formentor",
    "octavia", "fabia", "yeti", "superb", "kodiaq", "karoq", "kamiq",
    "caddy", "touran", "sharan", "t4", "t5", "t6", "transporter", "caravelle",
    "beetle", "scirocco", "eos", "jetta", "bora", "lupo",
    # Condition / quality
    "genuine", "oem", "used", "tested", "good", "excellent", "perfect",
    "clean", "original", "factory", "quality", "aftermarket", "pattern",
    "working", "functional", "removed", "dismantled", "takeout", "pull",
    # Market / listing boilerplate
    "breaking", "breakers", "breaker", "sale", "spares", "repairs",
    "free", "postage", "delivery", "shipping", "fits",
    # Filler words
    "the", "and", "or", "with", "to", "from", "of", "in", "on", "at",
    "for", "by", "as", "is", "it", "its", "this", "that", "an", "a",
    "no", "ref", "part", "number", "inc", "approx",
    # Generation markers
    "mk1", "mk2", "mk3", "mk4", "mk5", "mk6", "mk7", "mk8", "mk9",
    "b5", "b6", "b7", "b8", "b9",
    # Trim levels / spec lines
    "comfort", "sport", "sportline", "highline", "trendline", "comfortline",
    "elegance", "match", "life", "style", "edition", "plus", "basic",
    "executive", "luxury", "premium", "base", "advanced",
    # Performance / engine badges
    "gti", "gtd", "gte", "gli", "tsi", "tdi", "tfsi", "fsi",
    "r32", "vr6", "rs", "rline", "sline",
    # Transmission / fuel
    "dsg", "auto", "manual", "automatic", "petrol", "diesel", "hybrid", "electric",
    # Body types
    "estate", "saloon", "hatchback", "convertible", "coupe", "cabriolet", "sedan", "wagon", "van",
    # Model additions
    "cc", "up", "id3", "id4", "id5",
    # Misc listing noise
    "car", "vehicle", "brand", "lhd", "rhd", "uk", "euro", "type",
})


def _is_part_number_token(token: str) -> bool:
    """True for alphanumeric tokens >= 5 chars that mix letters and digits (look like OEM part numbers)."""
    return (
        len(token) >= 5
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )


def extract_description_from_titles(titles: list[str]) -> str | None:
    """Find the most common 2-3 word phrase across eBay listing titles after stripping noise."""
    from collections import Counter

    def clean(title: str) -> list[str]:
        s = title.lower()
        # Remove year ranges e.g. 2013-2020, 2015/20
        s = re.sub(r"\b(19|20)\d{2}[-/](?:\d{2}|\d{4})\b", "", s)
        # Remove standalone 4-digit years
        s = re.sub(r"\b(19|20)\d{2}\b", "", s)
        tokens = re.findall(r"\b[a-z0-9]+\b", s)
        return [
            t for t in tokens
            if t.isalpha()
            and len(t) >= 2
            and t not in _TITLE_NOISE
            and not _is_part_number_token(t)
        ]

    cleaned = [clean(t) for t in titles if t]
    cleaned = [t for t in cleaned if t]  # drop empty lists

    if not cleaned:
        return None

    ngram_counts: Counter = Counter()
    for tokens in cleaned:
        seen: set = set()
        for n in (3, 2, 1):
            for i in range(len(tokens) - n + 1):
                gram = tuple(tokens[i : i + n])
                if gram not in seen:
                    ngram_counts[gram] += 1
                    seen.add(gram)

    if not ngram_counts:
        return None

    min_support = max(1, len(cleaned) // 3)

    # First try: multi-word n-grams (2+) that meet support threshold
    multi_word = [(gram, cnt) for gram, cnt in ngram_counts.items()
                  if cnt >= min_support and len(gram) >= 2]
    if multi_word:
        multi_word.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return " ".join(w.title() for w in multi_word[0][0])

    # Second try: any n-gram meeting support (including 1-grams)
    any_supported = [(gram, cnt) for gram, cnt in ngram_counts.items()
                     if cnt >= min_support]
    if any_supported:
        any_supported.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return " ".join(w.title() for w in any_supported[0][0])

    # Last resort: best of whatever we have
    fallback = list(ngram_counts.items())
    fallback.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
    return " ".join(w.title() for w in fallback[0][0])


# ── CLAUDE API ───────────────────────────────────────────────────

async def identify_with_claude(client: httpx.AsyncClient, part_number: str) -> str | None:
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
                "messages": [{
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
                }],
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "").strip()
        text = re.sub(r"[.\"']", "", text).strip()
        words = text.split()
        if len(words) > 6:
            text = " ".join(words[:5])
        return text if text else None
    except Exception:
        return None


# ── HELPERS ──────────────────────────────────────────────────────

def is_paint_code(code: str) -> bool:
    if re.match(r'^L[A-Z][0-9][A-Z0-9]$', code):
        return True
    if re.match(r'^[A-Z][0-9][A-Z0-9]$', code):
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


@app.post("/lookup", response_model=LookupResponse)
async def lookup_part(req: LookupRequest):
    part_number = req.part_number.strip()
    if not part_number:
        raise HTTPException(status_code=400, detail="Part number is required")

    if USE_MOCK:
        return mock_lookup(part_number)

    clean = part_number.upper().replace(" ", "")

    if clean == "N/A":
        return LookupResponse(part_number=clean, description="—", source="none")

    async with httpx.AsyncClient() as client:
        pricing = {"avg_price": None, "low_price": None, "high_price": None, "total_listings": 0}

        # Paint codes -> Claude only, skip eBay
        if is_paint_code(clean):
            claude_desc = await identify_with_claude(client, clean)
            return LookupResponse(
                part_number=clean, description=claude_desc or "Paint Code",
                source="claude", **pricing,
            )

        # Step 1: search eBay
        listings = []
        try:
            token = await get_ebay_token(client)
            listings = await search_ebay(client, token, clean)
        except Exception as e:
            print(f"eBay lookup failed for {clean}: {e}")

        if listings:
            # Step 2: extract description from listing titles (primary source)
            titles = [item.get("title", "") for item in listings if item.get("title")]
            ebay_desc = extract_description_from_titles(titles)
            pricing = calculate_pricing(listings)

            if ebay_desc:
                # eBay titles gave us a clean description
                return LookupResponse(
                    part_number=clean, description=ebay_desc, source="ebay", **pricing,
                )
            else:
                # Titles too noisy — fall back to Claude for description only
                claude_desc = await identify_with_claude(client, clean)
                return LookupResponse(
                    part_number=clean,
                    description=claude_desc or "Unknown Part",
                    source="ebay+claude",
                    **pricing,
                )
        else:
            # Step 3: no eBay listings — Claude fallback for description, no pricing
            desc = await identify_with_claude(client, clean)
            return LookupResponse(
                part_number=clean, description=desc or "Unknown Part",
                source="claude", avg_price=None, low_price=None, high_price=None, total_listings=0,
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
            results.append(LookupResponse(part_number=part.part_number, description="Lookup Failed", source="none"))
    return results


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
