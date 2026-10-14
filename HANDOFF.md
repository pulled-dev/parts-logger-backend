# Pricing engine: handoff

_Branch `pricing-engine`, 14 Oct 2026. Nothing has been merged or deployed. The
live yard tool is untouched until you merge._

## 1. What was built

When a logger types a part number, the Parts Logger now asks a new **pricing
engine** for a price. The engine works through these sources in order:

1. **Our own eBay sales** of that part number (last 12 months, recent sales
   count more). This is the strongest signal.
2. **Live competitor listings** on eBay UK for the same number: used, UK
   sellers only, Buy It Now, **price + postage**, with our own accounts
   (`pulled_apart_ltd`, `singlesorsets`) excluded. Results are cached for 7
   days so we stay well inside eBay's limits.
3. **Fallbacks** when neither exists: our sales of the same kind of part on the
   same car (e.g. "Golf Mk7 · window motor"), then the old BreakerPro group
   average, then a default.

Each price comes with a **High / Medium / Low** badge and a one-line reason,
e.g. *"13 of our sales (median £17.99, last 11 months)"*. Low shows a red
**"Check price"** marker.

On the logger screen:

- The price fills itself in a moment after the part is added. Logging is never
  held up. If the engine is slow or down, the old price stays and you can carry
  on.
- If the logger has already typed a price, a late suggestion **never
  overwrites it**.
- The **vehicle ref still stays filled in** between parts, as before.
- The **CSV export is unchanged**: same 5 columns, same order, byte-for-byte.
  An automated test checks this.
- When you export, the final price on each card is sent back quietly. That
  lets the system measure which suggestions loggers changed.

Behind the scenes:

- All 3,705 of your eBay sales (Jan 2025 – Oct 2026) can be loaded into the
  live database. **No buyer names, emails, addresses or phone numbers ever
  leave your PC.** The import keeps only order/item numbers, title, prices and
  dates.
- A report page shows how accurate the suggestions turn out once items sell:
  `/price/report`, plus a spreadsheet version at `/price/report?format=csv`.
- Every number you might want to change (rounding, minimum price, how much
  weight the market gets, and so on) is in **one file: `pricing_config.py`**.

## 2. Headline backtest result: honest version

I replayed the last 12 months of sales. For each sale I asked what the engine
*would* have suggested using only sales before that day, and compared it with
what the old method would have pre-filled.

| | Typical miss (median error) | Priced within ±15% |
|---|---|---|
| Old method | 20.0% | 40.5% |
| New engine | 21.9% | 35.6% |

(2,165 sales where both methods could give a price.)

**The new engine is not clearly better on past data. On these two numbers it is
slightly worse.** Why, briefly (full detail in `docs/pricing-backtest.md`):

- **Where we've sold the part before, the engine is level with or better than
  the old method.** Door locks, bonnet release handles/cables and seat-belt
  buckles come out clearly better.
- **Where we haven't, it's worse.** Its fallbacks (averages across models)
  are weak for parts like headlights, airbag modules and ECUs, where the exact
  variant drives the price.
- **The old method's score is flattered.** Its prices came from a March 2026
  BreakerPro stock export. For older sales, that file often holds *the very
  item's own price*, so it "knew the answer".
- **The engine's competitor-price leg couldn't be tested,** because eBay has
  no record of past asking prices. That's the leg meant for parts we haven't
  sold before, which is exactly where it lost.
- **The old method under-prices by about 12% on a typical sale.** The engine
  doesn't lean either way.

**My recommendation:** go live with the eBay keys set, so the competitor leg
works. Then check `/price/report` after 4–6 weeks: that's the first real test.
Treat Low-confidence prices as "check price". There's also an optional switch
(`USE_BREAKERPRO_EXACT` in `pricing_config.py`, **off** by default) that does
better in the backtest. I explain in the report why that figure can't be fully
trusted.

## 3. Blockers and things I couldn't check

| Item | What it means | What to do |
|---|---|---|
| **No eBay "user token" for automatic order sync** | Sales can't flow in automatically yet. The message bot's token is a different kind (Trading API) and can't be reused. | Use the CSV import weekly (step 6), or set up the token (section 5). The sync code is built and tested, just switched off. |
| **No eBay keys on this PC** | I tested the competitor search against recorded sample responses, never against live eBay. | It'll run live once Railway has `EBAY_APP_ID` / `EBAY_CERT_ID` (already set for the current tool). Check step 5 below. |
| **eBay daily call limit not confirmed** | Default is 5,000 searches/day; we'd use roughly 250 a week. | Optional: check under "API call limits" in the eBay developer portal. |
| **`.gitignore` was never committed** | It's what keeps `data/` (your sales file) out of GitHub. It works on this PC, but it isn't saved in the repo. | Commit it yourself when convenient: `git add .gitignore` then `git commit -m "Add gitignore"`. I wasn't allowed to touch your uncommitted files. |
| **Open questions in the spec** (rounding, minimum prices, pricing stance, postage) | Defaults are used: £x.99 under £50, £5 − 1p above; £9.99 minimum; match the market; prices include postage. | Change them in `pricing_config.py` whenever you like. |

## 4. Going live: step by step

### Step 1. Make an admin password for the import

Make up a long random password (30+ characters, letters and numbers). You'll
use it in two places. It only protects the "load sales data" door; the logger
doesn't need it.

### Step 2. Add the env vars in Railway

Railway → the Parts Logger backend service → **Variables**. Use the
single-variable "New Variable" field, one at a time:

| Variable | Value | Needed? |
|---|---|---|
| `PRICING_ADMIN_TOKEN` | the password from Step 1 | **Yes (new)** |
| `EBAY_APP_ID` | already there | Check it's set |
| `EBAY_CERT_ID` | already there | Check it's set |
| `DB_PATH` | already there (the volume path, e.g. `/data/vehicles.db`) | Check it's set. The new tables go in the same file. |
| `EBAY_USER_REFRESH_TOKEN` | see section 5 | Optional, later |
| `PRICING_DAILY_SYNC` | `1` | Optional, only with the token above |

### Step 3. Merge the branch

1. Open https://github.com/pulled-dev/parts-logger-backend/pull/new/pricing-engine
2. Click **Create pull request**, then **Merge pull request**.
3. Railway redeploys the backend automatically (about 2 minutes), and GitHub
   Pages updates the logger page. Hard-refresh the logger on the phones
   (or close and reopen it) to pick up the new page.

### Step 4. Check it's up

Open these in a browser:

- https://web-production-7a1f0.up.railway.app/health should say `"status":"ok"`.
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

Then add a part you sell often in the logger. You should see a price, a
**PRICE · HIGH** badge and a reason line.

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

- https://web-production-7a1f0.up.railway.app/price/report shows accuracy by
  confidence level and which part types loggers override most.
- Add `?format=csv` to the end of that address for a spreadsheet, one row per
  suggestion.

### If something goes wrong

On GitHub, open the merged pull request and click **Revert**, then merge the
revert. Railway and the logger go back to how they were. The new database
tables are left in place, but nothing old reads them, so they're harmless.

## 5. Optional: automatic daily order sync

This needs a one-off eBay "user token" with permission to **read orders**:

1. Log in at https://developer.ebay.com → **Application Keys** → under your
   Production keyset click **User Tokens**.
2. Choose **"Get a Token from eBay via Your Application"** (OAuth). If asked to
   add a redirect (RuName), accept the defaults.
3. Tick the scope `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly`,
   sign in as **pulled_apart_ltd** and agree.
4. Copy the **Refresh Token** (lasts about 18 months; set a reminder to redo
   this). It contains `#` characters, so paste it into Railway's
   **single-variable** field as `EBAY_USER_REFRESH_TOKEN`, and add
   `PRICING_DAILY_SYNC` = `1`.
5. Test from PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri https://web-production-7a1f0.up.railway.app/price/sync-orders -Headers @{ "X-Admin-Token" = $env:PRICING_ADMIN_TOKEN }
```

It reports how many new sales it found. After that it runs once a day by
itself. eBay only lets it look back 90 days on the first run (up to 2 years if
asked); the CSV import already covers the older history.

## 6. Where things are (for a developer)

- `PRICING-ENGINE-SPEC.md`: the brief. `docs/pricing-recon.md`: how the old
  price worked, plus every place the build differs from the spec and why.
  `docs/pricing-backtest.md`: full backtest.
- `pricing/`: engine, market search, importer, order sync, feedback, backtest.
  `pricing_config.py`: all tunables.
- Endpoints: `GET /price/suggest`, `GET /price/report`, `GET /price/part-types`,
  `POST /price/suggestions/final`, and (admin) `POST /price/sold-history/import`,
  `POST /price/sync-orders`.
- Tests: `python -m pytest` (152 tests; needs Node.js for the CSV check). They
  never call eBay.
