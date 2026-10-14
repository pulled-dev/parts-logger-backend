"""Pricing engine tunables, all in one place (PRICING-ENGINE-SPEC.md §5).

Change a number here and redeploy; no other code changes needed. All prices are
TOTAL prices in GBP (item + postage), because P&P is now built into our prices.
"""

# ── Mode ────────────────────────────────────────────────────────────────
# Set with the PRICING_MODE env var in Railway (not here):
#   shadow (default) - the logger looks and behaves exactly as before; the
#                      engine runs silently and every suggestion is logged
#                      next to the current-method price and the final price.
#   live             - the logger shows the engine price, badge and reason.
PRICING_MODES = ("shadow", "live")
DEFAULT_PRICING_MODE = "shadow"

# ── Own sold history ────────────────────────────────────────────────────
OWN_LOOKBACK_DAYS = 365          # only our sales from the last N days count
OWN_RECENCY_HALF_LIFE_DAYS = 120 # a sale this old counts half as much as one today
MIN_OWN_SALES = 3                # >= this many own sales -> own median is the base

# ── Market (eBay Browse API) ────────────────────────────────────────────
MIN_MARKET_COMPS = 3             # need this many competitor listings to use the market
MARKET_DIVERGENCE_PCT = 25       # own vs market gap that triggers a nudge
MARKET_NUDGE = 0.30              # how far to move toward the market when it diverges
MARKET_CACHE_DAYS = 7            # reuse a part number's market snapshot for N days
MARKET_TIME_BUDGET_SECONDS = 3   # /price/suggest waits at most this long for live eBay prices;
                                 # after that it answers without them ("live prices still
                                 # loading") and the lookup finishes in the background
MARKET_PREWARM_MAX_PER_NIGHT = 300  # nightly cache refresh: at most this many part numbers
MARKET_PREWARM_HOUR_UTC = 2      # when the nightly refresh runs (02:00 UTC)
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
PART_TYPE_MIN_SALES = 2          # own sales of the same part type + model + generation needed to use that rung
# Part-type groups always match model AND generation (e.g. Golf Mk7, never just
# Golf). No borrowing from other models or generations.

# Variant-sensitive categories: price depends on the exact variant (xenon vs
# halogen, coding, trim...), so with no exact part-number match the engine gives
# NO price. It returns Low / "Check price" and the same-car group's price range
# for reference only. A part type counts as variant-sensitive if it contains any
# phrase below as whole words, unless it also contains an EXCLUDE word
# (so "HEADLIGHT SWITCH" or "STEERING WHEEL TRIM" are not caught).
VARIANT_SENSITIVE_PART_TYPES = (
    "HEADLIGHT", "HEADLAMP",
    "REAR LIGHT", "TAIL LIGHT", "TAILLIGHT",
    "AIRBAG MODULE", "AIRBAG CONTROL MODULE", "SRS MODULE",
    "ECU", "CONTROL MODULE", "CONTROL UNIT",
    "SPEEDO CLUSTER", "INSTRUMENT CLUSTER", "SPEEDO", "CLOCKS",
    "STEERING WHEEL",
    "INFOTAINMENT", "SAT NAV", "SATNAV", "NAVIGATION", "HEAD UNIT", "RADIO",
)
VARIANT_SENSITIVE_EXCLUDE = (
    "SWITCH", "BULB", "HOLDER", "BRACKET", "COVER", "TRIM", "CAP", "WIRING", "LOOM",
    "CABLE", "BUTTON", "SURROUND", "WASHER",
)
PART_TYPE_MEDIUM_SALES = 5       # >= this many -> Medium confidence, else Low
# There are NO default or pinned prices of any kind: with no real evidence (no
# own sales of the number, no usable market, no same model + generation group)
# the engine gives no price, Low / "Check price", and the logger keeps the
# current-method price.

# ── Rounding ────────────────────────────────────────────────────────────
# "x99_then_5": nearest £x.99 under £50, nearest £5 minus 1p at £50+.
# "none": round to the penny only.
ROUNDING_RULE = "x99_then_5"
ROUNDING_THRESHOLD = 50.0

# ── Not worth listing ───────────────────────────────────────────────────
# Below this TOTAL price (GBP, including postage) eBay fees + VAT + free postage
# + labour make the part a net loss: better to scrap it. A price under this is
# never raised or changed. The logger just shows an amber "not worth listing,
# consider scrap" warning, and can still list it. Change the number here and
# redeploy; no other code changes are needed.
NOT_WORTH_LISTING_BELOW = 8.00
