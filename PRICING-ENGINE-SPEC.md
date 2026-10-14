# PRICING-ENGINE-SPEC.md — Parts Logger Pricing Engine

**Repo:** `pulled-dev/parts-logger-backend` (FastAPI on Railway; frontend `index.html` on GitHub Pages)
**Owner:** Naveed Mohammed, Pulled Apart Ltd
**Status:** Ready to build (rev 2, 14 Oct 2026: eBay-only, sales data in place)
**Source of truth:** This file. Commit after each phase. Railway auto-deploys from `origin/main`; GitHub Pages serves `index.html` from main.

> **Scope rule:** this engine prices **eBay listings only**. On-site, phone and trade sales are deliberately excluded because they carry trade discounts that would drag prices down. Do not import BreakerPro sales data.

---

## 1. The problem

When a logger enters a part number, the Parts Logger suggests a price that is often wrong ("semi-random"). Naveed then corrects prices by eye, which is slow and inconsistent. We list around 250 items a week, so bad prices cost money both ways: under-priced parts sell too cheap, and over-priced parts sit unsold.

## 2. The goal

Replace the current price suggestion with a **pricing engine** that blends:

1. **Our own sold prices.** This is the strongest signal: real money for our parts, at our seller rating.
2. **Live competitor prices** on eBay UK for the same part number, used condition, including postage.
3. **Fallbacks** for when there's no part-number match (part type + model + engine code group, then the category price schedule).

Every suggestion comes back with a **confidence level** and a **one-line reason**, so the logger and Naveed can see *why* that price was given.

Success = the backtest in Phase 7 shows the new engine's suggestions are measurably closer to our actual achieved prices than the current method.

## 3. Non-negotiable constraints

- **Do not break the logging flow.** The vehicle ref still persists across entries (batch working style), and tile behaviour stays the same.
- **Do not change the CSV export layout.** It stays at 5 columns: `Part Number,Paint Code,Part Description,Price,Ref`. The data-input team depends on it.
- **No eBay scraping.** Use the official eBay APIs only (Browse API, Fulfillment API), plus CSV reports Naveed downloads himself. The eBay account is the main revenue channel, so don't put it at risk.
- **Keep the existing database.** Use the SQLite DB that is already persisted on Railway, and add new tables to it. Do **not** introduce Supabase for this build.
- **Secrets** go in Railway env vars, using the single-variable fields. Existing: `EBAY_APP_ID`, `EBAY_CERT_ID`, `ANTHROPIC_API_KEY`. Never hard-code keys or commit them.
- Use async clients inside async FastAPI functions (the sync clients have caused silent failures before).
- Scripts that POST to the live Railway API must run from Naveed's machine (the cloud sandbox is blocked from POSTing to Railway).

## 4. Data sources

### 4a. Our own eBay sold history (highest weight)
- **Historic backfill, already in place:** `data/ebay-orders/ebay_sales_clean.csv` inside this repo. The `data/` folder is **git-ignored and must stay that way**. Never commit sales data.
  - Built from two Seller Hub "All orders" reports covering **1 Jan 2025 – 14 Oct 2026**: **3,705 line items**, about £204k of sales, median £25.
  - Buyer names, emails, addresses and phone numbers have **already been stripped**. Columns kept: `Order number, Sales record number, Item number, Item title, Custom label, Sold via Promoted listings, Quantity, Sold for, Postage and packaging, Total price, Sale date, Dispatched on date`.
  - Dates are formatted `14-Oct-26`, money is formatted `£14.99`, UTF-8. Every quantity is 1.
  - **82% of titles contain a VAG part number** (e.g. `... WINDOW MOTOR (PASSENGER FRONT) 6RU 959 802`). Extract it with the Phase 1 normaliser. Titles also carry model, generation, year range, engine code and part type, which are used for the part-type fallback in §4c.
  - **Postage changed over the period.** 2025 sales mostly show £6.99 P&P on top; 2026 sales mostly show £0.00, i.e. free postage built into the price. **Always compare on `Total price` (item + P&P)**, both for our own history and for competitors. Otherwise the 2025 and 2026 numbers aren't like-for-like.
  - Future top-ups: Naveed downloads a new "All orders" report into `Downloads`. The importer strips the PII columns itself and must **never** write buyer data into the DB or the repo.
  - **Never read from or write to `C:\AutoManagement\`.** That's BreakerPro's folder.
- **Ongoing sync (API):** eBay Fulfillment API `getOrders`, which needs a **user OAuth token** (the app-only token used for Browse won't work). Reuse the refresh-token pattern from the message bot repo (`pulled-dev/pulled-apart-message-bot`) if it already holds a user token with the `sell.fulfillment.readonly` scope. **Verify the Fulfillment API's actual lookback limit in eBay's docs** rather than assuming one; the CSV backfill covers anything older.

### 4b. Competitor market prices
- **eBay Browse API** `item_summary/search`, using the app token (`EBAY_APP_ID`/`EBAY_CERT_ID`, client-credentials grant).
- Marketplace: `EBAY_GB`. Filters: used condition, item location GB, Vehicle Parts & Accessories category tree.
- **Exclude our own listings** by filtering out seller `pulled_apart_ltd`. This is critical: otherwise we'd be pricing against ourselves. (Naveed's personal account `singlesorsets` should also be excluded.)
- Use **total price = item price + cheapest UK shipping**.
- Respect the rate limits. Check the account's actual daily Browse quota and cache results (see Phase 3).

### 4c. Fallbacks (lowest weight)
- **Why these matter:** across the 3,705 sales there are about 1,614 distinct part numbers, but only **~277 have 3+ of our own sales** and ~546 have 2+. Exact-number own history will be thin for most parts, so the competitor data and these fallbacks will do most of the work.
- **Own sales by part type (new):** parse our own sold titles into part type + model/generation (+ engine code where relevant), e.g. "Golf Mk7 · WINDOW MOTOR". Use the median of that group when there's no exact-number match. Confidence: Medium if 5+ sales, Low otherwise.
- **Part-type group:** the existing "middle groups" (~700 from BreakerPro) keyed by part type + model/generation + engine code (first 3 letters only; the 4th letter doesn't matter. `DSGB` is a real engine code, not the DSG gearbox).
- **Category default:** the existing price schedule, flagged Low confidence.

## 5. Pricing logic (v1, configurable)

All thresholds and weights live in one config file (`pricing_config.py`) so Naveed can tune them without code changes.

1. **Normalise the part number:** uppercase, strip spaces, dots and dashes (`6R2 880 201 B` → `6R2880201B`). Also derive a **base number** with the trailing index letter(s) removed, for a looser match. VAG index letters are often interchangeable, but not always, so base-number matches get lower confidence than exact ones.
2. **Own sold comps:** exact-number sales in the last `OWN_LOOKBACK_DAYS` (default 365), with recent sales weighted more heavily.
3. **Market comps:** fetch competitor listings, drop outliers (IQR method), and compute the **lower quartile, median and count**. Asking prices run higher than what actually sells, so anchor to the lower half, not the mean.
4. **Blend:**
   - If own sales ≥ `MIN_OWN_SALES` (default 3), the base is our own weighted median. If the market median is more than `MARKET_DIVERGENCE_PCT` (default 25%) away from it, nudge 30% toward the market and flag it in the reason.
   - If there are 1–2 own sales, take a 50/50 blend of our own median and the market midpoint (the average of the lower quartile and the median).
   - If there are no own sales but ≥ `MIN_MARKET_COMPS` competitor listings (default 3), use the market midpoint.
   - Otherwise fall back to the part-type group, then the category default.
5. **Rounding:** apply `ROUNDING_RULE` (default: nearest £x.99 under £50, nearest £5 minus 1p at £50+). **Naveed to confirm.**
6. **Floor:** a per-category `MIN_LIST_PRICE`, so nothing is suggested below the point where it's worth listing.
7. **Confidence:**
   - **High:** ≥3 own sales, or ≥5 market comps with a tight spread.
   - **Medium:** some data, or a base-number-only match.
   - **Low:** fallback only. The logger sees a "check price" marker.

### Response shape: `GET /price/suggest`
Params: `part_number` (required), `vehicle_ref`, `part_type` (optional).
```json
{
  "suggested_price": 84.99,
  "confidence": "high",
  "reason": "4 of our sales (median £85, last 6 months) · 11 live competitors (£79–£110)",
  "own": {"count": 4, "median": 85.0, "last_sold": "2026-09-21"},
  "market": {"count": 11, "lower_quartile": 79.0, "median": 92.5},
  "match": "exact",
  "source": "own_sold"
}
```

## 6. Build phases

> **Every phase ends with the self-correction loop:** build → run tests → if anything fails, diagnose the root cause and fix it → re-run the tests → repeat until all pass. Only then commit and move to the next phase. Never mark a phase done with failing tests or skipped checks.

### Phase 0: Recon (no code changes)
- Read `main.py` and find exactly how the current price suggestion works today. Explain **why** it gives semi-random prices (e.g. keyword search instead of part number, first result taken, new/aftermarket listings included, own listings included, no shipping).
- List the DB tables, env vars and eBay calls currently in use. Check whether the message bot repo holds a user OAuth token with Fulfillment scope.
- Write the findings to `docs/pricing-recon.md`. **Stop and show Naveed the findings before Phase 1** if anything in them contradicts this spec.

### Phase 1: Part number normaliser
- `pricing/normalise.py`: `normalise(pn)` and `base_number(pn)`.
- Unit tests: spaced, dotted, lowercase, embedded in free text ("Part Number is 6R2 880 201 B"), index-letter variants, junk input.

### Phase 2: Own sold history
- New table `sold_history` (part_number_norm, base_number, price, shipping, sold_date, channel, source, vehicle_ref nullable, raw_title).
- `scripts/import_ebay_sales.py` loads `data/ebay-orders/ebay_sales_clean.csv`, and also accepts a raw Seller Hub "All orders" CSV. Raw reports have a junk first row, the header on row 2 and 81 columns; drop every buyer/post-to column on read. Dedupe on `Order number` + `Item number` + `Sales record number`. Store `Total price` as the price. Support `--dry-run`, make it idempotent (no duplicates on re-run), and print a summary.
- There is **no BreakerPro sales importer**. eBay only (see the scope rule at the top).
- Also build the part-type index from titles (§4c) in this phase.
- If a user token is available: `pricing/ebay_orders.py` syncs new orders via the Fulfillment API, plus an endpoint or scheduled job to run it daily. If no token is available, document how to get one and continue with CSV only.
- Tests: small **synthetic** fixture CSVs in both formats (raw report and clean). Never copy real sales rows into `tests/`. Cover the dedupe check, PII-column stripping (assert that no buyer column reaches the DB), mixed £0 / £6.99 postage, and the dry-run output.

### Phase 3: Market comps (Browse API)
- `pricing/market.py`: search by normalised part number, falling back to the spaced format if there are zero hits. Apply the §4b filters, exclude our seller, and compute total price.
- New table `market_snapshots` (part_number_norm, fetched_at, count, lq, median, min, max, raw_json). Cache for `MARKET_CACHE_DAYS` (default 7) to stay well inside API limits.
- Tests use recorded API fixtures and must not hit the live API. Include cases for outlier removal, own-seller exclusion, new-condition exclusion and zero results.

### Phase 4: Pricing engine + endpoint
- `pricing/engine.py` implements §5. `pricing_config.py` holds all the tunables.
- `GET /price/suggest` returns the §5 response shape.
- Tests cover every blend branch, rounding, the floor and the confidence levels.

### Phase 5: Parts Logger integration
- When a part number is entered, call `/price/suggest` and fill the price field.
- Show a **confidence badge** (High / Medium / Low) and the **reason line** under the price. Low confidence shows a "check price" marker.
- The logger can still overwrite the price, and the override is recorded (Phase 6).
- Don't slow the logger down: show the price asynchronously with a short loading state, and never block logging if the pricing call fails (fall back to the current behaviour).
- Verify on a phone-width screen. Confirm the CSV export is unchanged (5 columns, same order).

### Phase 6: Suggestion log + feedback
- New table `price_suggestions` (timestamp, part_number_norm, vehicle_ref, suggested, confidence, source, final_price, overridden bool).
- When an item later appears in `sold_history`, link it back so we know the suggested price, the listed price, the sold price, and the days to sell.
- `GET /price/report` returns accuracy by confidence level and the most-overridden part types, with a CSV download option.

### Phase 7: Backtest (proof it works)
- Over the last 6–12 months of `sold_history`, for each sale compute what the engine **would have suggested** using only data available before that sale date (no peeking at the sale itself).
- Compare against the old method from Phase 0. Report the **median absolute % error** and **% of items within ±15%**, broken down by confidence level and part type.
- Write the results to `docs/pricing-backtest.md`. If the new engine isn't clearly better, report that honestly along with the likely causes. Don't tune the numbers just to make the report look good.

## 7. Decisions and open questions
**Decided (14 Oct 2026):**
- eBay sales only; no BreakerPro or trade sales.
- Seller to exclude: `pulled_apart_ltd` (plus `singlesorsets`).
- Sales data lives in `data/ebay-orders/` (git-ignored), never in `C:\AutoManagement\`.

**Still open (defaults are used until Naveed answers):**
1. Pricing stance: match the market, or price slightly above it on the strength of the Top-Rated seller status? (Default: the §5 logic, no premium.)
2. Rounding rule: confirm or change the §5 default.
3. Minimum listing price per category: what is it worth photographing and listing for?
4. Free postage: is free P&P now standard on everything? If so, the suggested price **is** the total price. (Default: yes, suggest the total including postage.)

## 8. Out of scope (later)
Copart buy screener (it will reuse this engine), eBay listing module / BreakerPro replacement, website phase two, Terapeak automation, and price-drop suggestions for stale unsold listings.
