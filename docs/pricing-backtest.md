# Pricing engine backtest (Phase 7; re-run 14 Oct 2026 with "no evidence = no price" and the Hybrid line)

## Verdict, in plain English

The engine now only gives a price when it has real evidence. Otherwise it
returns no price, and the logger keeps the current-method price. The
**Hybrid** line measures exactly that: the engine's price where it gives one,
the current method's price where it doesn't. That's what loggers would see in
live mode.

On the 2,165 sales from the last 12 months that the current method could
price (same sales for every line):

| | Sales priced | Typical miss (MdAPE) | Within ±15% | Median bias |
|---|---|---|---|---|
| Current method | 2,165 | 20.0% | 40.5% | −12.3% |
| Engine (only where it gives a price) | 1,160 | 20.0% | 39.6% | 0.0% |
| **Hybrid** | 2,165 | **18.9%** | **40.9%** | **0.0%** |

**Hybrid is slightly better than the current method overall, but the gain is
small and not consistent quarter by quarter.** Not "clearly better". In
detail:

- **Overall:** Hybrid's typical miss is 1.1 points lower and it gets 0.4
  points more sales within ±15%. That's a small margin.
- **The clearest gain is bias.** The current method typically prices 12% below
  what the part actually sold for. Hybrid doesn't lean either way. On the
  sales where Hybrid uses the engine, that's money that would otherwise be
  left on the table.
- **By confidence:**
  - *High* (198 sales): Hybrid misses slightly less (17.3% vs 17.6%) but gets
    slightly fewer within ±15% (43.9% vs 46.0%). A wash.
  - *Medium* (935): Hybrid is better on both (20.0% vs 21.9%; 39.0% vs 37.8%).
  - *Low* (1,032): the engine priced only 27 of these. The rest fall back to
    the current method, so Hybrid is identical to it here (18.9%; 42.0%).
- **By quarter:**
  - Hybrid is better in 2025 Q4 (20.6% vs 23.3%) and 2026 Q2 (18.7% vs 19.9%;
    43.5% vs 39.9%).
  - It's about even in 2026 Q3 (21.3% vs 21.4% MdAPE, but 35.7% vs 37.8%
    within ±15%).
  - It's worse in 2026 Q1 (16.7% vs 15.1%) and 2026 Q4 (14.6% vs 12.4%, only
    74 sales).
  - Q1 2026 is the quarter where the current method most often "knew the
    answer" (see the leakage note below), but the pattern is still mixed
    rather than a clean win.
- **Where the engine does give a price** (1,160 sales), it now edges the
  current method head-to-head: 20.0% vs 20.8% MdAPE, 39.6% vs 38.9% within
  ±15%, and it was closer on 532 sales against 509 (119 ties). Almost all of
  these are parts we've sold before.
- **The engine stays silent a lot.** It gave no price on 1,114 of the 2,297
  sales with a part number: 200 variant-sensitive parts, the rest with no own
  sales, no same model + generation group, and (in this backtest) no market
  data. It priced 23 of the 132 sales the current method couldn't.

### Caveats that still apply

- **Hybrid inherits the current method's flattered score.** On the 1,005 sales
  where it falls back, Hybrid *is* the current method. That method's prices
  come from `vag_parts_db.json`, built on 7 March 2026 from BreakerPro stock
  exports, and for sales before then the price is often identical to that
  very item's own sold price (57–58% of the head-to-head sales in 2025 Q4 /
  2026 Q1; see "Over time" below). Both the current-method line and part of
  the Hybrid line benefit from that.
- **No market leg in the backtest.** There's no historical record of
  competitor asking prices. Live, the engine would also price parts from
  competitor listings, so it would stay silent less often. The backtest can't
  measure that.
- **Prices are noisy.** Even a plain median of 3+ earlier sales of the same
  number misses by a median 17.5%. Margins of 1–2 points are within what
  different months produce on their own.

### How we got here (same day)

| Version | What changed | Result |
|---|---|---|
| First build | Spec ladder incl. cross-model fallbacks | Engine 21.9% vs current 20.0% (2,165 sales) |
| Tightened | Same model + generation only; variant-sensitive parts get no price | Engine 24.2% vs 20.0% (1,975 sales): the flat £24.99 default was doing most of the fallback work |
| **This version** | Flat default removed: no evidence = no price; Hybrid measured | **Hybrid 18.9% vs current 20.0%** (2,165 sales) |

### What this means in practice

- **Shadow mode is still the default.** `/price/report` will measure the real
  thing on new listings and sales: current method vs engine vs final listed
  price, with days to sell. That includes the market leg once eBay keys are
  set, which this backtest can't test.
- **Live mode now behaves like Hybrid.** The engine replaces the pre-filled
  price only when it has evidence (own sales of the number, live competitors,
  or a same-car group). Otherwise the current price stays and the card says
  "Check price".
- **No settings were tuned to these results.** All values in
  `pricing_config.py` are spec defaults or follow your instructions (the
  variant-sensitive list, no flat default). `USE_BREAKERPRO_EXACT` stays off.

### Method

- **Window:** every sale from 15 Oct 2025 to 14 Oct 2026 (2,727 sales). The 430
  without a part number in the title are out of scope.
- **No peeking:** each suggestion uses only sales strictly before the sale's
  date, including for working out the part type. Unit-tested in
  `tests/test_backtest.py`.
- **Inputs:** the car (model / generation / engine code) comes from the sale's
  own title. No market data.
- **Target:** the total the buyer paid (item + P&P).
- **Current method:** the logger's old pre-fill (`breakerpro_price` from
  `vag_parts_db.json`: exact entry, else 6-digit group average). Its live-eBay
  leg can't be replayed.
- **Hybrid:** the engine's suggested price if it gave one, else the current
  method's. Compared on every sale the current method could price.
- **Re-run:** `python scripts/backtest_pricing.py` regenerates every table
  below. The "how we got here" figures come from the earlier versions on this
  branch (commits `a8022d2` and `c93092c`).

## Results

<!-- AUTO:START -->
_Generated by `scripts/backtest_pricing.py` for sales from 2025-10-15 to 2026-10-14 (last 365 days). Error = |suggested − actual total price| / actual. MdAPE = median absolute % error (lower is better). ±15% = share of sales priced within 15% (higher is better)._

### Coverage

| | Sales |
|---|---|
| Sales in window | 2727 |
| With a part number in the title (engine can be asked) | 2297 |
| No part number (body panels etc.; neither method applies) | 430 |
| Old method had a replayable DB price | 2165 |
| Engine deliberately gave **no price** (variant-sensitive or no evidence; "Check price") | 1114 |
| Both methods gave a price (head-to-head set) | 1160 |

On the 1114 no-price sales the old method's DB price had MdAPE 18.9% and 42.4% within ±15% (n=1005). Those sales are excluded from the head-to-head, not counted as wins or losses for either side.

### Headline: head-to-head on the same sales (both gave a price)

| Method | Sales | MdAPE | Within ±15% | Median bias |
|---|---|---|---|---|
| Old method (BreakerPro DB price) | 1160 | 20.8% | 38.9% | -12.1% |
| New engine (no market data) | 1160 | 20.0% | 39.6% | +0.0% |

Sale by sale, the new engine was closer on **532** sales, the old method on **509**, and they tied on 119.

New engine on every sale with a part number where it gave a price (1183): MdAPE 20.0%, within ±15% 39.4%.

### Current method vs engine vs Hybrid

Hybrid = engine price where the engine gives one, current-method price where it doesn't (what the logger shows in live mode). Judged on every sale the current method could price, so all three lines cover the same sales; the engine line only counts the sales it priced.

| Method | Sales priced | MdAPE | Within ±15% | Median bias |
|---|---|---|---|---|
| Current method (BreakerPro DB price) | 2165 | 20.0% | 40.5% | -12.3% |
| Engine (where it gives a price) | 1160 | 20.0% | 39.6% | +0.0% |
| **Hybrid** | 2165 | 18.9% | 40.9% | +0.0% |

Hybrid took the engine's price on 1160 of these sales and the current method's on 1005. On the 132 sales where the current method had no price, Hybrid (= engine) priced 23 (22.2% MdAPE, 30.4% within ±15%).

By engine confidence (same sales for all three lines within each level):

| Confidence | Sales | Current MdAPE | Current ±15% | Engine priced | Engine MdAPE | Engine ±15% | Hybrid MdAPE | Hybrid ±15% |
|---|---|---|---|---|---|---|---|---|
| High | 198 | 17.6% | 46.0% | 198 | 17.3% | 43.9% | 17.3% | 43.9% |
| Medium | 935 | 21.9% | 37.8% | 935 | 20.0% | 39.0% | 20.0% | 39.0% |
| Low | 1032 | 18.9% | 42.0% | 27 | 29.5% | 25.9% | 18.9% | 42.0% |

By quarter (leakage check: the current method's DB is most often the sold item's own price in 2025 Q4 / 2026 Q1, see 'Over time' below):

| Quarter | Sales | Current MdAPE | Current ±15% | Engine priced | Engine MdAPE | Engine ±15% | Hybrid MdAPE | Hybrid ±15% |
|---|---|---|---|---|---|---|---|---|
| 2025 Q4 | 340 | 23.3% | 28.2% | 150 | 18.6% | 38.0% | 20.6% | 33.8% |
| 2026 Q1 | 540 | 15.1% | 50.0% | 263 | 21.3% | 36.5% | 16.7% | 47.2% |
| 2026 Q2 | 584 | 19.9% | 39.9% | 344 | 17.7% | 44.5% | 18.7% | 43.5% |
| 2026 Q3 | 627 | 21.4% | 37.8% | 364 | 20.0% | 37.4% | 21.3% | 35.7% |
| 2026 Q4 | 74 | 12.4% | 55.4% | 39 | 16.7% | 43.6% | 14.6% | 50.0% |

### Where the engine wins and loses

| Subset (same sales for both) | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| Engine priced from our own sales of this number | 1128 | 20.0% | 39.4% | 20.0% | 40.0% |
| Engine had to use a fallback (no own sales) | 32 | 31.8% | 21.9% | 24.7% | 25.0% |
| Free-postage sales only | 725 | 16.6% | 48.1% | 20.0% | 40.8% |

### By engine confidence

| Confidence | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| High | 201 | 17.6% | 46.0% | 17.6% | 43.3% |
| Medium | 954 | 21.9% | 37.8% | 20.0% | 39.0% |
| Low | 28 | 18.9% | 42.0% | 29.8% | 25.0% |

### By engine source (which rung priced it)

| Source | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| no_evidence | 0 | 19.6% | 41.2% | – | – |
| own_sold | 1150 | 20.0% | 39.4% | 20.0% | 39.8% |
| part_type | 33 | 31.8% | 21.9% | 26.7% | 24.2% |
| variant_check | 0 | 16.7% | 47.4% | – | – |

### Old method by match type

| Old match | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| exact | 953 | 20.0% | 39.5% | 18.9% | 40.5% |
| group | 207 | 22.2% | 36.2% | 20.0% | 35.3% |

### Over time (leakage check)

`vag_parts_db.json` was built on 2026-03-07 from BreakerPro stock exports. The last column is the share of sales where the old method's exact price is *identical* to that item's own sold price.

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% | Old price = item's own price |
|---|---|---|---|---|---|---|
| 2025 Q4 | 150 | 25.9% | 25.3% | 18.6% | 38.0% | 57% |
| 2026 Q1 | 263 | 19.9% | 42.2% | 21.3% | 36.5% | 58% |
| 2026 Q2 | 344 | 20.0% | 38.4% | 17.7% | 44.5% | 30% |
| 2026 Q3 | 364 | 20.0% | 40.9% | 20.0% | 37.4% | 26% |
| 2026 Q4 | 39 | 12.4% | 53.8% | 16.7% | 43.6% | 20% |

### By part type (15 most common)

| Part type | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| door lock | 77 | 29.7% | 32.4% | 20.0% | 36.4% |
| window motor front | 44 | 24.0% | 34.5% | 17.8% | 40.9% |
| bonnet release handle | 35 | 18.2% | 46.3% | 12.0% | 60.0% |
| airbag squib/slip ring | 33 | 14.9% | 51.2% | 25.0% | 36.4% |
| heater flap actuator | 29 | 25.0% | 31.4% | 26.2% | 31.0% |
| seat belt front | 29 | 15.8% | 50.0% | 18.0% | 31.0% |
| abs pump/modulator/control unit | 27 | 28.0% | 35.5% | 28.6% | 18.5% |
| fuel pump | 27 | 20.9% | 41.7% | 20.0% | 37.0% |
| airbag module | 26 | 21.9% | 37.8% | 28.3% | 30.8% |
| clutch pedal | 23 | 16.8% | 46.2% | 11.1% | 56.5% |
| brake servo | 21 | 15.8% | 39.1% | 15.8% | 47.6% |
| heater blower motor | 21 | 25.0% | 35.5% | 22.7% | 38.1% |
| window regulator | 21 | 22.5% | 29.5% | 18.5% | 42.9% |
| indicator stalk | 21 | 41.1% | 11.5% | 26.7% | 33.3% |
| wiper & indicator stalk | 20 | 18.9% | 37.0% | 28.6% | 30.0% |

### Diagnostic: opt-in `USE_BREAKERPRO_EXACT` rung (off by default)

| Method | Sales | MdAPE | Within ±15% |
|---|---|---|---|
| Old method | 1740 | 19.0% | 41.7% |
| Engine + BreakerPro exact rung | 1740 | 18.9% | 42.1% |

Quarter by quarter with the rung on:

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| 2025 Q4 | 287 | 25.9% | 26.1% | 20.8% | 33.1% |
| 2026 Q1 | 480 | 16.3% | 49.6% | 17.3% | 46.2% |
| 2026 Q2 | 466 | 18.9% | 41.8% | 16.7% | 46.1% |
| 2026 Q3 | 454 | 20.0% | 41.6% | 20.0% | 38.8% |
| 2026 Q4 | 53 | 12.3% | 54.7% | 16.7% | 45.3% |

<!-- AUTO:END -->
