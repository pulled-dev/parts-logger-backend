# Pricing Engine — Phase 0 Recon

_Branch `pricing-engine`, 14 Oct 2026. Read-only investigation; no code changed in this phase._

## 1. How the price suggestion works today

There are **two layers**, and the one the logger actually sees is mostly *not* eBay.

### 1a. Frontend pre-fill (`index.html`, `addPart()`)

When a part number is added, the frontend calls `POST /lookup` and pre-fills
"Your price" with the first non-null of:

1. `breakerpro_price`
2. `suggested_price` (eBay live listings)
3. `avg_price` (eBay live listings)

So **`breakerpro_price` wins whenever it exists**, and eBay only matters when it
doesn't.

### 1b. `breakerpro_price` (`vag_lookup.lookup_part`, `vag_parts_db.json`)

`vag_parts_db.json` is a static file built in March 2026 from BreakerPro stock
exports (2,927 exact part numbers, 724 "middle groups").

- **Exact match:** the price that one BreakerPro listing of that part number
  happened to carry. It's a single asking price (not a sale price), frozen in
  March 2026, and often from a different car/model.
- **Middle-group match:** the 6-digit VAG group (e.g. `880201` = driver airbag)
  maps to `avg_price`, the **mean of every BreakerPro listing in that group across
  every model**. A Polo airbag (£29.99) and a Passat airbag (£99.99) get averaged
  into the same number. This is the main reason prices look "semi-random": the
  number depends on which other cars happened to share the 6 digits.
- **Learned / Claude match:** no price, so it falls through to eBay.
- `/db-correct` writes whatever price is on the card back as the exact
  `breakerpro_price`, so one-off manual prices get baked in permanently.

### 1c. eBay layer (`main.py`: `search_ebay`, `calculate_pricing`, `_ebay_pricing`)

- Browse API `item_summary/search`, `EBAY_GB`, app token (client credentials).
- `q` = the **unspaced** part number (`6R2880201B`). Most UK breaker titles use
  the spaced format (`6R2 880 201 B`), so many real comps are missed.
- `sort=price`, `limit=20`: it only ever sees the **20 cheapest** listings.
- Filter is `conditionIds:{3000}` (used) only. There's **no item-location filter**
  (overseas sellers included), **no seller exclusion** (our own
  `pulled_apart_ltd` listings and `singlesorsets` are included, so we price
  against ourselves), and **no category filter**.
- **Shipping is ignored.** Only `price.value` is used, so free-P&P sellers and
  £9.99-P&P sellers look the same.
- If the part-number search finds nothing, it **re-searches by description +
  "VAG"** (e.g. "Window Motor VAG"). That returns parts for any VAG car, not this
  part number.
- Then it trims the top/bottom 20% and suggests **mean × 0.875** (12.5% below
  the average of the cheapest 20). Confidence comes from listing count only.

**Net effect:** the pre-filled price is either a stale single BreakerPro asking
price, a cross-model group average, or 87.5% of the cheapest-20 asking prices
(item-only, including our own listings and keyword-fallback junk). None of these
uses what our parts actually sell for.

## 2. Inventory

### DB tables (SQLite, `DB_PATH` env var, Railway volume)
- `vehicles` (ref PK, make, model, year_range, paint_code, paint_name,
  engine_code, transmission, vin, notes, is_active, created_at, updated_at).
- That's the only table. Part lookups live in the JSON file `vag_parts_db.json`,
  not SQLite.

### Env vars in use
| Var | Used by | Locally set? |
|---|---|---|
| `EBAY_APP_ID`, `EBAY_CERT_ID` | Browse API app token. Mock mode if missing | No |
| `ANTHROPIC_API_KEY` | Claude part identification | No |
| `DB_PATH` | SQLite location | No (defaults to `./vehicles.db`) |

### eBay calls
- `POST https://api.ebay.com/identity/v1/oauth2/token` (client_credentials,
  scope `https://api.ebay.com/oauth/api_scope`).
- `GET https://api.ebay.com/buy/browse/v1/item_summary/search`.
- No Fulfillment / Sell API calls exist. `/lookup-panel` only builds eBay URLs
  and makes no API call.

### Message bot user token
`pulled-apart-message-bot` uses a single static `EBAY_AUTH_TOKEN`, sent as an
`X-EBAY-API-IAF-TOKEN` header to the **Trading API** (XML). There is **no
refresh-token flow** in that repo, and nothing in its code requests the
`sell.fulfillment.readonly` scope. (I only read code and docs, not `.env`
values.) **Conclusion: no reusable user OAuth token with Fulfillment scope is
available.** Phase 2 builds the Fulfillment sync against recorded fixtures and
documents how to mint a refresh token.

### Fulfillment API lookback (checked against eBay docs)
`getOrders` with no filter returns the **last 90 days**. With a `creationdate`
filter it can return orders **up to 2 years old**. The CSV backfill covers
anything older.

### Browse API filter syntax (checked against eBay's Buy API filter reference)
`excludeSellers:{a|b}`, `conditionIds:{3000}`, `itemLocationCountry:GB`,
`buyingOptions:{FIXED_PRICE|BEST_OFFER}`. The default Browse quota is 5,000
calls/day per app. **I couldn't confirm the account's actual quota** (no keys
locally). Naveed can check it in the eBay developer portal under "Application
Growth Check / API call limits". The 7-day cache keeps us far below it (~250
new part numbers a week).

## 3. Sales data profile (`data/ebay-orders/ebay_sales_clean.csv`)
- 3,705 rows, 12 columns as the spec describes, all quantity 1, no duplicate
  (order, item, record) keys, `Custom label` empty on every row.
- `Postage and packaging`: £6.99 ×1,897, £0.00 ×1,521, £64.99 ×199 (courier
  panels), blank ×43, plus a few others.
- **`Total price` is unreliable on 469 rows:** 384 show £0.00 despite a
  non-zero `Sold for`, and 85 don't equal `Sold for + P&P` (e.g. £30.00 + £6.99
  shown as £6.99). These look like multi-item order lines, where Seller Hub
  puts the order total on a different row.

## 4. Contradictions with the spec, and the resolutions chosen

The run brief says to record contradictions and continue with the safest option
rather than stop. Each one below is resolved and the build carries on.

1. **"Store `Total price` as the price."** `Total price` is £0 or wrong on 469
   rows. **Resolution:** store `price = Sold for + Postage and packaging` (blank
   P&P = £0). This is what `Total price` means (item + P&P) and equals it on
   every consistent row. The reported `Total price` is not used.
2. **"Middle groups keyed by part type + model/generation + engine code."** The
   existing ~700 middle groups are keyed only by the **6-digit VAG group number**
   and averaged across all models. **Resolution:** the
   part type + model/generation (+ engine-code first 3 letters) grouping is built
   new from our own sold titles (§4c "own sales by part type"). The BreakerPro
   6-digit group average is kept as a lower rung, flagged Low.
3. **"Category default: the existing price schedule."** No category price
   schedule exists in the code. The body-panel "pricing schedule" screen is
   manual entry. **Resolution:** category default = median of our own sales of
   that part type across all models, then a configurable global default in
   `pricing_config.py` (`DEFAULT_PRICE`, £24.99, near the £25 overall median).
   Both are flagged Low. `CATEGORY_DEFAULTS` in config lets Naveed pin specific
   part types.
4. **"Reuse the message bot's refresh-token pattern."** There isn't one (see
   §2). **Resolution:** Fulfillment sync is built and tested against fixtures and
   reads `EBAY_USER_REFRESH_TOKEN`. When it's missing, the sync endpoint returns a
   clear "not configured" response. The CSV importer is the working path today.
5. **"Use the SQLite DB on Railway"** plus **"scripts that POST to Railway run
   from Naveed's machine".** The import script can't open the Railway volume
   directly. **Resolution:** `scripts/import_ebay_sales.py` parses and strips PII
   **locally**, then either writes to a local DB (`--db`) or POSTs clean rows to
   a new `POST /price/sold-history/import` endpoint (`--api`). That endpoint is
   guarded by a new `PRICING_ADMIN_TOKEN` env var and **disabled when the var is
   unset**. No buyer data ever leaves the PC.
6. **Phase 0 says "stop and show Naveed".** The run brief overrides this:
   continue autonomously. Findings are recorded here and in `HANDOFF.md`.
7. **`.gitignore` is untracked.** The `data/` ignore rule works locally but isn't
   committed. The brief forbids staging untracked files I didn't create, so I
   left it alone. **Naveed should commit `.gitignore`** (flagged in
   `HANDOFF.md`). The sales CSV is confirmed ignored locally
   (`git check-ignore` passes).
8. **Backtest has no historical competitor prices.** The Browse API only shows
   live listings. **Resolution:** the Phase 7 backtest measures the engine with
   own history + fallbacks only, and says so. The market leg is unit-tested but
   can't be backtested.
9. **Old method in the backtest.** The live-eBay part of the old method can't be
   replayed historically. The `breakerpro_price` part (which wins in the
   frontend whenever present) can. **Resolution:** the backtest replays
   `breakerpro_price` (exact, then 6-digit group) and reports coverage, so it's
   clear how many sales the old method could price at all.
10. **Rounding wording.** "Nearest £x.99" is taken literally: £24.40 → £23.99,
    £24.60 → £24.99. At £50+, nearest £5 minus 1p: £87 → £84.99, £88 → £89.99.

## 5. Addendum (14 Oct 2026, later the same day): decisions from Naveed

These supersede resolutions 2 and 3 above:

- **Part-type groups must match model AND generation.** The BreakerPro 6-digit
  group average (all models) and the "same part type, any model" median are
  **no longer price sources**. After the same model + generation group, only
  the configured category default remains (Low / check price).
- **Variant-sensitive categories** (`VARIANT_SENSITIVE_PART_TYPES` in
  `pricing_config.py`) never use a group fallback. With no exact-number sales
  and no usable market, the engine returns no price, confidence Low, a
  "check price" reason, and the same-car group range for reference only.
- **`PRICING_MODE`** = `shadow` (default) or `live`. Shadow keeps the logger
  screen exactly as before and only logs. `USE_BREAKERPRO_EXACT` stays off.
- **No flat default.** The general `DEFAULT_PRICE` (£24.99) is removed. With
  no own sales, no usable market and no same model + generation group, the
  engine gives no price (source `no_evidence`, Low / "check price"), and in
  live mode the logger keeps the current-method price. `CATEGORY_DEFAULTS`
  (per-part-type pinned prices) has been removed as well, so there are no
  default or pinned prices of any kind.

## 6. Addendum: order sync, speed, "not worth listing"

- **Order sync now uses the Trading API, not Fulfillment.** This supersedes §2
  "Message bot user token" and resolution 4. `GetOrders` with the message
  bot's long-lived `EBAY_AUTH_TOKEN` (IAF header, plus APP/DEV/CERT names,
  site 3, compatibility level 1199). It only reaches back 90 days, which is
  fine because history comes from the CSV import. No user refresh token is
  needed; `EBAY_USER_REFRESH_TOKEN` is no longer used.
- **Market lookups:** a 3-second time budget, with a background fill into the
  cache and a nightly pre-warm (300 a night).
- **The price floor (`MIN_LIST_PRICE`, §5.6 of the spec) is removed.** Low
  prices are shown as-is and flagged with an amber "not worth listing"
  warning under `NOT_WORTH_LISTING_BELOW` (£8).
