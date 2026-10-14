# Pricing engine backtest (Phase 7, re-run 14 Oct 2026 with the tightened fallback)

## Verdict, in plain English

**The engine is still not better than the current method on our past sales,
and tightening the fallback made its numbers worse, not better.** On the
1,975 sales from the last 12 months where both gave a price, the current
method's typical miss was **20.0%**, against the engine's **24.2%**. The
current method also got more sales within ±15%: 39.8% vs 32.9%.

### What the tightening changed (same 1,975 sales, before vs after)

| | Typical miss (MdAPE) | Within ±15% |
|---|---|---|
| Current method (old pre-fill) | 20.0% | 39.8% |
| Engine before tightening (commit `a8022d2`) | 21.4% | 36.5% |
| **Engine after tightening (this version)** | **24.2%** | **32.9%** |

The rules changed in this version:

1. **Part-type groups must match model AND generation.** The old "same model,
   any generation" level and the cross-model BreakerPro 6-digit group average
   are gone.
2. **Variant-sensitive parts get no price when there's no exact match.**
   These are headlights, tail lights, airbag modules, ECUs/control modules,
   clusters, steering wheels and infotainment. The engine returns Low /
   "Check price" plus the same-car range for reference.

Effect on the backtest:

- **808 sales lost their old cross-model fallback.** 701 had been priced by
  the BreakerPro all-models group average and 95 by a model-only part-type
  group. With no same model + generation group to fall back on (only 33 sales
  had one with 2+ earlier sales), they now get the flat £24.99 general default.
  On those 808 sales the engine's typical miss went from 23.6% to **28.6%**.
  The current method scored 19.9% on the same sales. **This is the main reason
  the headline got worse.** Borrowing across models was a better guess than a
  flat default, even though it's the wrong thing to rely on.
- **200 sales now get no price at all** (variant-sensitive, no exact match):
  35 headlights, 55 rear/tail lights, 21 speedo clusters, 17 central control
  units, 12 engine ECUs, 11 airbag modules, 6 steering wheels and others. On
  these, the previous engine's group fallback missed by a median **30.4%**
  (25.5% within ±15%), so withholding a price removed genuinely bad guesses.
  The current method's DB price scored 16.7% on them, but that figure carries
  the leakage described below. These sales are reported separately and
  excluded from the head-to-head, so they count as neither a win nor a loss.

### What hasn't changed

- **With our own sales of the part number, the engine still matches the
  current method** (20.0% both; 40.0% vs 39.4% within ±15%), with no leakage
  involved. High-confidence suggestions miss by a median 17.6%. On parts we
  sell often it's clearly better: door locks 20.0% vs 29.7%, bonnet release
  handles 12.4% vs 18.2%, window regulators 18.9% vs 22.5%, bonnet release
  cables 16.7% vs 23.9%.
- **The current method's score is flattered.** Its prices come from
  `vag_parts_db.json`, built on 7 March 2026 from BreakerPro stock exports.
  For sales before then, its "price" is identical to that very item's own
  sold price about 70% of the time. That's the item's own listing, not a
  prediction. The overlap drops to about 25% by Q3 2026, and the gap narrows
  with it.
- **The engine ran without its market leg.** There's no historical record of
  competitor asking prices, so every no-history sale went straight to the
  fallback. Live, those parts would get eBay competitor prices first. The
  backtest can't measure that leg.
- **The current method under-prices** (median bias −12.5%; the engine's is
  0.0%).
- **Prices are noisy.** Even a plain median of 3+ earlier sales of the same
  number misses by a median 17.5%.

### What this means in practice

- **Shadow mode is now the default,** so none of this reaches the loggers
  until you switch `PRICING_MODE` to `live`. The engine runs silently and
  `/price/report` will compare current method vs engine vs final listed price
  on real new listings, then on real sales with days to sell. That's the fair
  test the backtest can't give.
- In live mode, every Low suggestion shows "Check price". After this change
  most Low suggestions are the flat £24.99 default, a placeholder rather than
  an estimate (typical miss 32%).

### Options for you (not done; your call)

1. **Leave it in shadow for 4–6 weeks with eBay keys set,** then read
   `/price/report`. This is my recommendation. It's the only way to see the
   market leg working.
2. **Treat the general default the same way as variant-sensitive parts:** give
   no price, keep the current price and say "Check price". The backtest
   suggests a flat £24.99 is worse than the current pre-fill for these parts.
   I didn't make this change because you didn't ask for it.
3. `USE_BREAKERPRO_EXACT` stays **off**, as instructed. Its diagnostic row
   below is for information only.

### Method

- **Window:** every sale from 15 Oct 2025 to 14 Oct 2026 (2,727 sales). The 430
  without a part number in the title are out of scope for both methods.
- **No peeking:** each suggestion uses only sales strictly before the sale's
  date, including for working out the part type. Unit-tested in
  `tests/test_backtest.py`.
- **Inputs:** the car (model / generation / engine code) comes from the sale's
  own title, standing in for the vehicle ref. 26% of titles carry no
  generation, so under the new rule those sales can never use a part-type
  group. No market data.
- **Target:** the total the buyer paid (item + P&P).
- **Current method:** the logger's old pre-fill (`breakerpro_price` from
  `vag_parts_db.json`: exact entry, else 6-digit group average). Its live-eBay
  leg can't be replayed.
- **No settings were tuned to this data.** Everything in `pricing_config.py`
  is a spec default, apart from the variant-sensitive category list, which
  follows your brief. The before/after table above was produced by running
  commit `a8022d2` and this version on the same database and comparing
  sale by sale.
- **Re-run:** `python scripts/backtest_pricing.py` regenerates the tables below.

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
| Engine deliberately gave **no price** (variant-sensitive, "Check price") | 200 |
| Both methods gave a price (head-to-head set) | 1975 |

On the 200 no-price sales the old method's DB price had MdAPE 16.7% and 47.4% within ±15% (n=190). Those sales are excluded from the head-to-head, not counted as wins or losses for either side.

### Headline: head-to-head on the same sales (both gave a price)

| Method | Sales | MdAPE | Within ±15% | Median bias |
|---|---|---|---|---|
| Old method (BreakerPro DB price) | 1975 | 20.0% | 39.8% | -12.5% |
| New engine (tightened fallback, no market data) | 1975 | 24.2% | 32.9% | +0.0% |

Sale by sale, the new engine was closer on **792** sales, the old method on **1007**, and they tied on 176.

New engine on every sale with a part number (2097): MdAPE 25.0%, within ±15% 32.1%.

### Where the engine wins and loses

| Subset (same sales for both) | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| Engine priced from our own sales of this number | 1128 | 20.0% | 39.4% | 20.0% | 40.0% |
| Engine had to use a fallback (no own sales) | 847 | 20.0% | 40.5% | 28.6% | 23.5% |
| Free-postage sales only | 1178 | 15.2% | 49.9% | 25.0% | 31.6% |

### By engine confidence

| Confidence | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| High | 201 | 17.6% | 46.0% | 17.6% | 43.3% |
| Medium | 954 | 21.9% | 37.8% | 20.0% | 39.0% |
| Low | 942 | 18.9% | 42.0% | 31.7% | 22.7% |

### By engine source (which rung priced it)

| Source | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| category_default | 914 | 19.6% | 41.2% | 32.0% | 22.6% |
| own_sold | 1150 | 20.0% | 39.4% | 20.0% | 39.8% |
| part_type | 33 | 31.8% | 21.9% | 26.7% | 24.2% |
| variant_check | 0 | 16.7% | 47.4% | – | – |

### Old method by match type

| Old match | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| exact | 1533 | 18.9% | 42.5% | 22.7% | 34.4% |
| group | 442 | 25.1% | 30.8% | 25.0% | 27.6% |

### Over time (leakage check)

`vag_parts_db.json` was built on 2026-03-07 from BreakerPro stock exports. The last column is the share of sales where the old method's exact price is *identical* to that item's own sold price.

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% | Old price = item's own price |
|---|---|---|---|---|---|---|
| 2025 Q4 | 291 | 25.9% | 25.8% | 22.8% | 32.6% | 70% |
| 2026 Q1 | 490 | 16.7% | 48.8% | 25.0% | 29.4% | 69% |
| 2026 Q2 | 544 | 20.0% | 39.3% | 22.2% | 37.1% | 33% |
| 2026 Q3 | 583 | 21.4% | 38.3% | 25.0% | 31.9% | 25% |
| 2026 Q4 | 67 | 13.6% | 53.7% | 26.2% | 34.3% | 24% |

### By part type (15 most common)

| Part type | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| door lock | 103 | 29.7% | 32.4% | 20.0% | 38.8% |
| window motor front | 58 | 24.0% | 34.5% | 21.4% | 34.5% |
| seat belt front | 50 | 15.8% | 50.0% | 25.0% | 20.0% |
| window regulator | 44 | 22.5% | 29.5% | 18.9% | 40.9% |
| airbag squib/slip ring | 41 | 14.9% | 51.2% | 25.0% | 36.6% |
| bonnet release handle | 41 | 18.2% | 46.3% | 12.4% | 56.1% |
| seat belt buckle front | 39 | 18.8% | 38.9% | 16.7% | 48.7% |
| fuel pump | 38 | 20.9% | 41.7% | 19.3% | 39.5% |
| bonnet release cable | 37 | 23.9% | 27.0% | 16.7% | 35.1% |
| heater flap actuator | 35 | 25.0% | 31.4% | 31.6% | 25.7% |
| heater blower motor | 31 | 25.0% | 35.5% | 22.8% | 35.5% |
| wiper motor | 29 | 21.9% | 34.5% | 21.9% | 41.4% |
| wiper & indicator stalk | 28 | 18.9% | 37.0% | 27.2% | 21.4% |
| abs pump/modulator/control unit | 27 | 28.0% | 35.5% | 28.6% | 18.5% |
| headlight switch | 27 | 28.6% | 37.0% | 21.4% | 29.6% |

### Diagnostic: opt-in `USE_BREAKERPRO_EXACT` rung (off by default)

| Method | Sales | MdAPE | Within ±15% |
|---|---|---|---|
| Old method | 1975 | 20.0% | 39.8% |
| Engine + BreakerPro exact rung | 1975 | 20.0% | 39.5% |

Quarter by quarter with the rung on:

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| 2025 Q4 | 291 | 25.9% | 25.8% | 21.4% | 33.0% |
| 2026 Q1 | 490 | 16.7% | 48.8% | 17.6% | 45.5% |
| 2026 Q2 | 544 | 20.0% | 39.3% | 19.0% | 42.3% |
| 2026 Q3 | 583 | 21.4% | 38.3% | 23.5% | 34.8% |
| 2026 Q4 | 67 | 13.6% | 53.7% | 20.0% | 43.3% |

<!-- AUTO:END -->
