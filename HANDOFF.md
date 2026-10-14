# Pricing engine: handoff

_Branch `pricing-engine`, 14 Oct 2026 (updated since: tightened fallback,
shadow mode, Hybrid backtest, order sync with the message bot's eBay token,
3-second speed limit, "not worth listing" warning). Pull request:
https://github.com/pulled-dev/parts-logger-backend/pull/1, **merged and
deployed 14 Oct 2026**: all 3,705 past sales are loaded and the order sync
works with the message bot's token. **The logger screen hasn't changed: the
engine runs in shadow mode** (see below).
Added since (branch `listing-snapshot`): a daily, read-only snapshot of our
live eBay listings, so the report judges the price that was REALLY on eBay
(see "Real eBay listings" below)._

## 1. What was built

### Shadow mode (the default) and live mode

There's a new Railway setting, **`PRICING_MODE`**:

- **`shadow` (default, also used if the setting is missing or misspelt):** the
  logger screen looks and behaves **exactly as it does today**: same price,
  no badge, no new text. I checked this pixel by pixel against the current
  page at phone width. In the background, the engine still works out its
  price for every part. On export, the system records three prices for each
  card: the **current-method price**, the **engine's price**, and the **final
  price the logger actually used**.
- **`live`:** the logger shows the engine's price, a High / Medium / Low badge
  and a one-line reason (the behaviour described below).

`/price/report` compares the three prices side by side. Once items sell, it
shows which of the three was closest to the sold price, and the **days to
sell** for each item. That gives you real evidence before you ever switch to
`live`.

### The engine

When a logger types a part number, the Parts Logger asks the new **pricing
engine** for a price. It works through these sources in order:

1. **Our own eBay sales** of that part number (last 12 months, recent sales
   count more). This is the strongest signal.
2. **Live competitor listings** on eBay UK for the same number: used, UK
   sellers only, Buy It Now, **price + postage**, with our own accounts
   (`pulled_apart_ltd`, `singlesorsets`) excluded. Results are cached for 7
   days so we stay well inside eBay's limits.
3. **Fallbacks** when neither exists: our sales of the same kind of part on the
   **same model AND generation** (e.g. "Golf Mk7 · window motor", never just
   "Golf"). Nothing is borrowed from other models or generations. The old
   all-models BreakerPro group average is no longer used.
4. **No evidence, no price.** If none of the above has anything, the engine
   gives **no price** (there's no flat default any more): just Low and
   "Check price". In live mode the logger then **keeps the current-method
   price** for that part. The engine only replaces a price when it has real
   evidence.
5. **Variant-sensitive parts never get a fallback price.** These are
   headlights, tail lights, airbag modules, ECUs/control modules, clusters,
   steering wheels and infotainment/sat-nav. Their price depends on the exact
   variant, so with no sales of that exact number and no live competitors,
   the engine gives **no price**: just Low, "Check price", and the same-car
   price range for reference. You can edit the list in `pricing_config.py`
   (`VARIANT_SENSITIVE_PART_TYPES`, with exceptions like "switch" or "trim" in
   `VARIANT_SENSITIVE_EXCLUDE`).

Each price comes with a **High / Medium / Low** badge and a one-line reason,
e.g. *"13 of our sales (median £17.99, last 11 months)"*. Low shows a red
**"Check price"** marker. (Live mode only; in shadow mode none of this is
shown.)

On the logger screen in **live** mode:

- The price fills itself in a moment after the part is added. Logging is never
  held up. If the engine is slow or down, the old price stays and you can carry
  on.
- If the logger has already typed a price, a late suggestion **never
  overwrites it**.
- The **vehicle ref still stays filled in** between parts, as before.
- The **CSV export is unchanged**: same 5 columns, same order, byte-for-byte.
  An automated test checks this.
- When you export, the final price and the current-method price on each card
  are sent back quietly, in both modes. That lets the system measure which
  suggestions loggers changed.

### "Not worth listing" warning (shadow AND live)

If the price a logger is shown is **under £8** (total, including postage),
that row gets an amber warning: **"Under £8 — not worth listing, consider
scrap"**, plus what the price is based on, e.g. *"Based on: our last 3 sales
£5–£7"* or *"Based on: BreakerPro price on file"*.

- It's judged on the price the logger actually sees: today's pre-filled price
  in shadow mode, the engine's price in live mode.
- It's **only a warning.** It never blocks logging, never changes the price,
  and the logger can still list the part.
- The old hidden "minimum price" (which quietly raised low prices to £9.99) is
  **gone**. A low price is shown as it is, with the warning.
- Each warning is recorded. `/price/report` shows how many were warned, how
  many were listed anyway, and how those sold.

**To change the £8:** open `pricing_config.py`, change
`NOT_WORTH_LISTING_BELOW = 8.00` to the new amount (e.g. `10.00`), commit and
merge. Railway redeploys, and the logger picks the new number up
automatically. No other changes are needed.

### Fast enough for the yard

- The engine waits **at most 3 seconds** for live eBay competitor prices. If
  eBay is slow, the price comes back without them (the reason says "live
  prices still loading"), the lookup finishes in the background, and the next
  time that part is added it's instant from the cache.
- **Every night at about 2am** it refreshes the saved competitor prices for
  the parts we sell most (up to 300 a night), so most lookups in the day come
  straight from the cache.
- `/price/report` shows typical (median) and slowest-5% response times.

### Automatic order top-up

With `PRICING_DAILY_SYNC=1`, the backend fetches new eBay orders once a day,
using **the same eBay token as the message bot** (`EBAY_AUTH_TOKEN`). eBay
only allows the last 90 days this way, which is fine: the full history comes
from the one-off CSV import. Only order/item numbers, title, prices and dates
are kept. Buyer names, usernames, emails, addresses and phone numbers are
thrown away as the data is read.

### Real eBay listings (daily snapshot, read-only)

**The problem it fixes:** the report used to treat the price the logger
exported as "the listed price". But prices often get changed in BreakerPro
after logging, and those changes go to eBay. Also, the report only saw items
that **sold**, so over-priced items sitting unsold were invisible.

**What it does now:** once a day, straight after the order top-up, the backend
reads **our own active eBay listings** (the same list you see under "Active" in
Seller Hub). For each listing it notes:

- the price **including postage**;
- when it went live;
- whether Best Offer is on;
- the part number from the title.

It **only reads**. It never creates, changes or ends a listing.

From that it works out, for every listing:

- the **first price** it went on eBay at and its **current price**;
- **every price change**, with the date;
- **how many days** it's been listed;
- whether it **sold** or **ended without an eBay sale**. When a listing disappears from the
  active list, it's checked against our sales by eBay item number. No matching
  sale means it "ended without an eBay sale". Note: parts sold over the
  counter or to trade and then ended on eBay are counted here too.

Each engine suggestion is matched to the listing it most likely became: the
same part number, listed on or after the day of the suggestion (within 60
days), the nearest one first. Each listing is matched to only one suggestion.

**What changes in `/price/report`:** wherever a suggestion is matched to a
listing, the report uses **the real first eBay price** instead of the logger's
exported price, and **that listing's own sale**. There's also a new
**`ebay_listings`** section (see Step 7).

To save space, a listing only gets a new row when something changes (price,
postage, title or Best Offer). Otherwise only its "last seen" date moves on.
If a run fails half-way (e.g. eBay times out), nothing from that run is
saved, so listings never wrongly look "ended".

It uses one eBay call per 200 listings, plus any automatic retries. With
3,000 listings that's about 15 calls a day. eBay's default allowance is 5,000
calls a day, so this is well within it. Each run logs how many calls it made.

Behind the scenes:

- All 3,705 of your eBay sales (Jan 2025 – Oct 2026) can be loaded into the
  live database. **No buyer names, emails, addresses or phone numbers ever
  leave your PC.** The import keeps only order/item numbers, title, prices and
  dates.
- A report page shows how accurate the suggestions turn out once items sell:
  `/price/report`, plus a spreadsheet version at `/price/report?format=csv`.
- Every number you might want to change (rounding, the £8 "not worth
  listing" line, the 3-second limit, how much weight the market gets, and so
  on) is in **one file: `pricing_config.py`**.

## 2. Headline backtest result: honest version

I replayed the last 12 months of sales. For each sale I asked what the engine
*would* have suggested using only sales before that day. I compared three
things on the same 2,165 sales that the current method could price:

| | Typical miss (median error) | Priced within ±15% | Leans |
|---|---|---|---|
| Current method (today's pre-fill) | 20.0% | 40.5% | 12% too low |
| Engine alone (gives a price on 1,160 of them) | 20.0% | 39.6% | neither way |
| **Hybrid**: engine where it has evidence, otherwise today's price (= what live mode does) | **18.9%** | **40.9%** | neither way |

**Hybrid comes out slightly ahead of the current method, but it's a small
margin and not consistent: it's better in some quarters and worse in others.**
I haven't changed any settings to get these numbers. Briefly (full detail in
`docs/pricing-backtest.md`):

- **The clearest benefit is that it stops under-pricing.** The current method
  typically prices 12% below what the part actually sold for; Hybrid doesn't.
- **Where the engine has evidence (mostly parts we've sold before), it's a
  little better than today's price:** 20.0% vs 20.8% typical miss, closer on
  532 sales vs 509. Medium-confidence parts gain the most.
- **Where it has no evidence (about half of parts), it now says nothing,** so
  Hybrid is just today's price there. That's by design.
- **Quarter by quarter it's mixed:**
  - Better in late 2025 and spring 2026.
  - About even in summer 2026.
  - Worse in early 2026 and the first two weeks of October.
- **Today's price score is flattered.** It comes from a March 2026 BreakerPro
  stock export that often holds *the very item's own price*, so it "knew the
  answer" on older sales. Hybrid inherits that on the sales where it falls
  back to today's price.
- **The competitor-price leg couldn't be tested,** because eBay has no record
  of past asking prices. Live, it would let the engine price more parts.

How we got here today:

| Version | Engine vs current |
|---|---|
| First build | 21.9% vs 20.0% |
| Tightened fallback | 24.2% vs 20.0%. The flat £24.99 default was doing most of the work. |
| Now (no flat default, measured as Hybrid) | **18.9% vs 20.0%** |

**My recommendation:** merge and run in **shadow** mode with the eBay keys set.
After 4–6 weeks, check `/price/report` (current method vs engine vs what
loggers actually listed at, then vs what sold and how fast). Switching to
`live` is now low-risk, because live only changes a price when the engine has
evidence. But the backtest gain is too small to call it proven: let the report
decide. `USE_BREAKERPRO_EXACT` stays **off**.

## 3. Blockers and things I couldn't check

| Item | What it means | What to do |
|---|---|---|
| **No eBay keys on this PC** | The order sync and competitor search were tested only against made-up sample responses. The optional live check (one read-only call) was skipped because `EBAY_AUTH_TOKEN` isn't set on this PC. | After merging, run the sync test in section 5. It'll tell you straight away if a variable is wrong. |
| **Listing snapshot not yet tried against the real eBay** | It was built and tested only against made-up sample responses. `EBAY_AUTH_TOKEN` isn't on this PC, so the optional one-call live check was skipped. | After merging, run the listing test in section 5 once. |
| **"Best Offer on" may read as off** | eBay doesn't always say in this list whether Best Offer is switched on. The count of Best-Offer listings could come out too low. | Nothing to do. The Best Offer figures in the report don't depend on it: they compare the sold price with the price that was on eBay that day. |
| **eBay daily call limit not confirmed** | Default is 5,000 searches/day; we'd use roughly 250 a week. | Optional: check under "API call limits" in the eBay developer portal. |
| **`.gitignore`** | Now committed on this branch (it keeps `data/` and your sales file out of GitHub). | Nothing: it goes live with the merge. |
| **Open questions in the spec** (rounding, pricing stance, postage) | Defaults are used: £x.99 under £50, £5 − 1p above; match the market; prices include postage. There's no minimum price any more, just the £8 "not worth listing" warning. | Change them in `pricing_config.py` whenever you like. |

## 4. Going live: step by step

### Step 1. Make an admin password for the import

Make up a long random password (30+ characters, letters and numbers). You'll
use it in two places. It only protects the "load sales data" door; the logger
doesn't need it.

### Step 2. Add the env vars in Railway

Railway → the **Parts Logger backend** service → **Variables**. Use the
**single-variable "New Variable" field**, one at a time:

| Variable | Value | Needed? |
|---|---|---|
| `EBAY_APP_ID` | copy from the **message bot** service | **Yes** (probably already there) |
| `EBAY_CERT_ID` | copy from the message bot service | **Yes** (probably already there) |
| `EBAY_DEV_ID` | a Railway **reference** to the message bot service's variable (`${{pulled-apart-message-bot.EBAY_DEV_ID}}`) | **Yes (new here)**. This is how it's set up now. |
| `EBAY_AUTH_TOKEN` | a Railway **reference** to the message bot service's variable (`${{pulled-apart-message-bot.EBAY_AUTH_TOKEN}}`) | **Yes (new here)**. This is how it's set up now. See the note below. |
| `PRICING_ADMIN_TOKEN` | the password from Step 1 | **Yes (new)** |
| `PRICING_MODE` | `shadow` | **Yes**. Shadow is the default anyway; setting it makes the choice visible. Change to `live` only at Step 8. |
| `PRICING_DAILY_SYNC` | `1` | Optional, but **needed for the report's real eBay prices**: turns on the daily order top-up and the daily listing snapshot. |
| `DB_PATH` | already there (the volume path, e.g. `/data/vehicles.db`) | Check it's set. The new tables go in the same file. |

**Important: paste `EBAY_AUTH_TOKEN` into the single-variable field, not the
Raw Editor.** The token contains `#` characters, and the Raw Editor cuts it
off at the first `#`. The order sync would then fail with "eBay rejected the
token". (`EBAY_SITE_ID` isn't needed: it defaults to 3, the UK.)

**On the parts logger's web service, `EBAY_DEV_ID` and `EBAY_AUTH_TOKEN` are
references to the `pulled-apart-message-bot` service's variables, not
copies.** So **if the eBay token is ever renewed, update it only in the
message bot service.** The parts logger picks up the new value automatically
(Railway redeploys it). Don't paste a separate copy into the parts logger: that
would break the link, and the two would drift apart again. If the order sync
ever reports "eBay rejected the token (check EBAY_AUTH_TOKEN)", the token in
the message bot itself has expired or been cut off. Logging and pricing keep
working regardless.

### Step 3. Merge the branch

1. Open the pull request: https://github.com/pulled-dev/parts-logger-backend/pull/1
2. Read the "one extra commit" note in it (your `update_website.py` work rides
   along), then click **Merge pull request**.
3. Railway redeploys the backend automatically (about 2 minutes), and GitHub
   Pages updates the logger page. Hard-refresh the logger on the phones
   (or close and reopen it) to pick up the new page.

### Step 4. Check it's up

Open these in a browser:

- https://web-production-7a1f0.up.railway.app/health should say `"status":"ok"`,
  `"pricing_mode":"shadow"` and `"not_worth_listing_below":8.0`.
- https://web-production-7a1f0.up.railway.app/price/part-types should show `[]`
  (empty until Step 5).

### Step 5. Load your sales history into the live database (from your PC)

Open **PowerShell** and paste these lines, one at a time:

```powershell
cd "C:\Users\ACER\Desktop\Pulled Apart Ltd\parts-logger-backend"
git checkout main
git pull --ff-only origin main
$env:PRICING_ADMIN_TOKEN = "PASTE-THE-PASSWORD-FROM-STEP-1"

# Practice run, writes nothing. Should say "Would insert: 3705":
python scripts\import_ebay_sales.py --api https://web-production-7a1f0.up.railway.app --dry-run

# The real thing. Should say "Inserted: 3705":
python scripts\import_ebay_sales.py --api https://web-production-7a1f0.up.railway.app
```

Your uncommitted changes (`import_breakerpro.py` and the `.bat` file) come
along when you switch to `main`; nothing is lost. If Python complains about a
missing module, run `pip install -r requirements.txt` once and try again.
Running the import twice is harmless: duplicates are skipped.

Then add a part you sell often in the logger. In shadow mode the screen should
look **exactly as it always has**. Open
https://web-production-7a1f0.up.railway.app/price/report?format=csv and you'll
see a row for that part with the engine's price.

### Step 6. Keeping it up to date (weekly, about 2 minutes)

1. Seller Hub → Orders → **Download report** → "All orders" (CSV) → save to
   **Downloads**.
2. In PowerShell (same folder as above):

```powershell
$env:PRICING_ADMIN_TOKEN = "PASTE-THE-PASSWORD"
python scripts\import_ebay_sales.py --from-downloads --api https://web-production-7a1f0.up.railway.app
```

It picks the newest orders report in Downloads, strips all buyer details on
your PC and uploads only the clean rows. **Delete the raw report from Downloads
afterwards**, as it contains buyer details. Don't put sales files in
`C:\AutoManagement\`; the script refuses to touch that folder anyway.

### Step 7. Watching accuracy

- https://web-production-7a1f0.up.railway.app/price/report shows:
  - **listing:** how often the logger's final price matched the current method
    and how often it matched the engine, plus how far each method was from the
    final price;
  - **after_sale:** once items sell, how close the **current method**, the
    **engine** and the **final listed price** each were to the sold price
    (typical miss, % within ±15%, and whether each runs high or low);
  - **days_to_sell:** overall, and split by whether the item was listed
    below, at or above the engine's price;
  - **sold_items:** each sold item with all three prices, the sold price and
    its days to sell;
  - **not_worth_listing:** how many "under £8" warnings were shown, how many
    of those parts were listed anyway, and how they sold;
  - **response_time:** typical and slowest-5% times for a price, and how often
    the competitor prices came from the cache vs live vs timed out;
  - accuracy by confidence level, and which part types loggers change most;
  - **ebay_listings** (from the daily listing snapshot; empty until it has
    run):
    - **vs_sold_price:** how close the engine, the current method and **the
      real first eBay price** each were to what it sold for;
    - **repriced_after_logging:** how many prices were changed after logging.
      There are two parts: from the logger's price to the first eBay price,
      and while the item was listed. Each shows whether prices went up or
      down, by how much (typical change), and whether they moved **toward or
      away from the engine's price**;
    - **sell_through:** % of listings sold within 14, 30 and 60 days. It's
      split by whether the first eBay price was above, at (within 5%) or
      below the engine's price. A listing only counts for "30 days" once it's
      been up 30 days, so new listings don't drag the numbers down;
    - **unsold_over_60_days:** listings still unsold after 60 days, and how
      their price compares with the engine's;
    - **tracked_listings → ended_without_ebay_sale:** listings that left eBay
      with no eBay sale. Parts sold over the counter or to trade and then
      ended on eBay land here too (the report repeats this in its `note`);
    - **best_offer:** how many items sold for less than the price on eBay
      that day, and the typical discount.
- Add `?format=csv` to the end of that address for a spreadsheet, one row per
  suggestion, with all of the above per item. The spreadsheet has the same
  columns as before. New columns at the end show the real eBay price, the
  listing's item number, its price changes, whether it sold, and days listed.
  `final_price` is still the logger's exported price; `listed_price` is the
  real eBay one when there's a match.

### Step 8. Switching to live (only when the report says so)

Railway → Variables → set `PRICING_MODE` to `live` → Railway redeploys. The
logger then shows the engine price, badge and reason (hard-refresh the phones).
To go back, set it to `shadow` again. Nothing else changes, and the CSV export
is identical in both modes.

### If something goes wrong

On GitHub, open the merged pull request and click **Revert**, then merge the
revert. Railway and the logger go back to how they were. The new database
tables are left in place, but nothing old reads them, so they're harmless.

## 5. Automatic daily order top-up and listing snapshot

This uses the message bot's eBay token, so there's nothing new to set up at
eBay. You just need the four eBay variables from Step 2 plus
`PRICING_DAILY_SYNC=1`.

Test it once from PowerShell after merging:

```powershell
$env:PRICING_ADMIN_TOKEN = "PASTE-THE-PASSWORD"
Invoke-RestMethod -Method Post -Uri https://web-production-7a1f0.up.railway.app/price/sync-orders -Headers @{ "X-Admin-Token" = $env:PRICING_ADMIN_TOKEN }
```

It reports how many new sales it added. Running it again adds nothing, because
duplicates are skipped. After that it runs once a day by itself.

- **If it says a variable is missing:** add that variable in Railway (Step 2).
- **If it says the token was rejected:** the token in the **message bot**
  service is cut off or out of date. Renew/re-paste it there (single-variable
  field). The parts logger references it, so nothing needs changing on the
  parts logger itself.

The same daily run then takes the **listing snapshot**. To try it once by
hand (it only reads from eBay):

```powershell
Invoke-RestMethod -Method Post -Uri https://web-production-7a1f0.up.railway.app/price/sync-listings -Headers @{ "X-Admin-Token" = $env:PRICING_ADMIN_TOKEN }
```

It reports how many active listings it found (`items`), how many eBay calls it
made (`api_calls`), and how many listings were new, changed or gone since the
last run. Add `?dry_run=true` to the address to see the numbers without saving
anything. A missing variable or a rejected token is reported the same way as
for the order sync above, and the fix is the same.

You can also refresh the competitor-price cache on demand (it normally runs
nightly):

```powershell
Invoke-RestMethod -Method Post -Uri https://web-production-7a1f0.up.railway.app/price/prewarm -Headers @{ "X-Admin-Token" = $env:PRICING_ADMIN_TOKEN }
```

## 6. Where things are (for a developer)

- `PRICING-ENGINE-SPEC.md`: the brief. `docs/pricing-recon.md`: how the old
  price worked, plus every place the build differs from the spec and why.
  `docs/pricing-backtest.md`: full backtest.
- `pricing/`: engine, market search, importer, order sync, feedback, backtest.
  `pricing_config.py`: all tunables.
- Endpoints: `GET /price/suggest`, `GET /price/report`, `GET /price/part-types`,
  `POST /price/suggestions/final`, `POST /price/suggestions/{id}/warning`, and
  (admin) `POST /price/sold-history/import`, `POST /price/sync-orders`,
  `POST /price/sync-listings`, `POST /price/prewarm`.
- Listing snapshot: `pricing/listings.py`. Trading API `GetMyeBaySelling`
  (ActiveList only, 200 a page) with the same token and headers as the order
  sync. Tables `listing_snapshots` (one row per listing per state) and
  `listing_snapshot_runs`. `price_suggestions.listing_item_id` holds the
  match. Settings in `pricing_config.py`: `LISTING_LINK_WINDOW_DAYS`,
  `SELL_THROUGH_DAYS`, `UNSOLD_LONG_DAYS`.
- Order sync: Trading API `GetOrders` with `EBAY_AUTH_TOKEN` (IAF header),
  last 90 days only. Nightly pre-warm: `PRICING_PREWARM=0` turns it off.
- Tests: `python -m pytest` (needs Node.js for the CSV and shadow/live
  checks). They never call eBay.
