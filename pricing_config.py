"""Pricing engine tunables, all in one place (PRICING-ENGINE-SPEC.md §5).

Change a number here and redeploy; no other code changes needed. All prices are
TOTAL prices in GBP (item + postage), because P&P is now built into our prices.
"""

# ── Own sold history ────────────────────────────────────────────────────
OWN_LOOKBACK_DAYS = 365          # only our sales from the last N days count
OWN_RECENCY_HALF_LIFE_DAYS = 120 # a sale this old counts half as much as one today
MIN_OWN_SALES = 3                # >= this many own sales -> own median is the base

# ── Market (eBay Browse API) ────────────────────────────────────────────
MIN_MARKET_COMPS = 3             # need this many competitor listings to use the market
MARKET_DIVERGENCE_PCT = 25       # own vs market gap that triggers a nudge
MARKET_NUDGE = 0.30              # how far to move toward the market when it diverges
MARKET_CACHE_DAYS = 7            # reuse a part number's market snapshot for N days
MARKET_HIGH_CONF_COMPS = 5       # >= this many comps with a tight spread -> High
MARKET_TIGHT_SPREAD = 0.35       # (median - lower quartile) / median at or under this = tight
MARKET_REQUIRE_PN_IN_TITLE = True  # drop listings whose title doesn't carry the part number
EXCLUDED_SELLERS = ("pulled_apart_ltd", "singlesorsets")  # never price against ourselves
EBAY_MARKETPLACE = "EBAY_GB"
EBAY_CATEGORY_ID = "131090"      # eBay UK: Vehicle Parts & Accessories
EBAY_USED_CONDITION_IDS = ("3000",)  # Used
EBAY_SEARCH_LIMIT = 100

# ── Fallbacks ───────────────────────────────────────────────────────────
# Opt-in, NOT in the spec: when there are no own sales and no usable market,
# try our last BreakerPro asking price for this exact number (vag_parts_db.json)
# before the part-type rung. See docs/pricing-backtest.md for the evidence and
# its caveat before turning this on.
USE_BREAKERPRO_EXACT = False
PART_TYPE_MIN_SALES = 2          # own sales of the same part type + car needed to use that rung
PART_TYPE_MEDIUM_SALES = 5       # >= this many -> Medium confidence, else Low
DEFAULT_PRICE = 24.99            # last resort (overall median sale is ~£25)
# Pin a default for specific part types, e.g. {"ENGINE": 349.99, "GEARBOX": 199.99}.
# Keys are canonical part types as shown by GET /price/part-types.
CATEGORY_DEFAULTS: dict[str, float] = {}

# ── Rounding and floor ──────────────────────────────────────────────────
# "x99_then_5": nearest £x.99 under £50, nearest £5 minus 1p at £50+.
# "none": round to the penny only.
ROUNDING_RULE = "x99_then_5"
ROUNDING_THRESHOLD = 50.0
# Floor so nothing is suggested below what's worth photographing and listing.
MIN_LIST_PRICE = 9.99
MIN_LIST_PRICE_BY_TYPE: dict[str, float] = {}   # e.g. {"DOOR BARE": 79.99}
