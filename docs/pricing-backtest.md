# Pricing engine backtest (Phase 7)

## Verdict, in plain English

**The new engine is not clearly better than the old method on our past sales.
On the headline measure it is slightly worse.** On 2,165 sales from the last 12
months where both methods could give a price, the old method's typical miss was
20.0% against the engine's 21.9%. The old method also got more sales within ±15%
(40.5% vs 35.6%).

That result needs four caveats. Together they mean the backtest **can't show
either way** that the engine is better. They don't mean the old method is good.

1. **The old method is partly reading the answer.** Its prices come from
   `vag_parts_db.json`, built on 7 March 2026 from BreakerPro *stock* exports.
   For sales before then, the "old price" is identical to that very item's own
   listing price 70–72% of the time (see "Over time" below). That's not a
   prediction; the item was in stock when the file was built. The overlap falls
   to about 25% for summer 2026 sales, and the old method's lead shrinks with it
   (Q1 2026: 15.1% vs 21.4%; Q3 2026: 21.4% vs 23.3%). Some of the remaining
   overlap is genuine consistency (the same part number gets the same price),
   so leakage can't be cleanly separated from skill.
2. **The engine ran without its market leg.** There's no record of what
   competitors were asking on past dates, so every sale with no own history
   fell straight to the weakest fallbacks. In live use, those parts get live
   eBay competitor prices first. That's the leg designed for exactly the 1,037
   fallback sales where the engine lost most (25.0% vs 18.9%). The backtest
   can't measure it.
3. **The old method under-prices.** Its median bias is **−12.3%** (the engine's
   is 0.0%). On sales with paid postage it is typically 25% below what the
   buyer actually paid. Part of that is fair: the old price was the item price, with
   P&P on top. Part of it is money left on the table. Median % error treats
   under- and over-pricing the same, so it doesn't show this.
4. **Prices are naturally noisy.** Even the plain median of 3+ earlier sales of
   the *same* part number misses by a median 17.5%. No method gets most sales
   within ±15% on this data; around half is the best seen in any subset.

### Where the engine does and doesn't work

- **With our own sales of the part number, it's level with or slightly ahead of
  the old method** (20.0% both; 40.0% vs 39.4% within ±15%), with no leakage.
  High-confidence suggestions miss by a median 17.6%, which is at the noise
  floor. On parts we sell often it clearly beats the old prices: door locks
  20.0% vs 29.7%, bonnet release handles 12.4% vs 18.2%, front seat-belt
  buckles 14.3% vs 18.8%, bonnet release cables 16.7% vs 23.9%.
- **Without own sales, the fallbacks are weak** (24–38% error). The 6-digit
  BreakerPro group average mixes every model, and the part-type medians mix
  very different variants. Headlights, airbag modules, ECUs and control units
  are the worst (30–37%), because price depends on the exact variant
  (xenon/halogen, software, model), not the part type.
- **A postage-regime effect.** 2025 totals include £6.99 P&P. In 2026, P&P was
  mostly built in, but item prices weren't raised by the full £6.99. On recent
  free-postage sales priced from our own history, the engine runs about **7%
  high**. A shorter lookback or a faster recency decay would probably help. I
  have **not** tuned this: it should be confirmed on new sales, not fitted to
  this report.

### What I'd do next (Naveed's call)

1. **Go live as the spec intends, with eBay keys set**, so the market leg
   works, and let `GET /price/report` measure real accuracy for 4–6 weeks.
   That's the first fair test of the full engine.
2. **Consider turning on `USE_BREAKERPRO_EXACT`** in `pricing_config.py`. When
   there are no own sales and no usable market, it uses our last BreakerPro
   price for that exact number before the cross-model fallbacks. In the
   backtest that takes the engine to **18.9%** vs the old method's 20.0% (table
   at the bottom). But that figure is flattered by the same leakage as the old
   method, so it's left **off** by default, as the spec doesn't include it.
3. **Treat Low confidence as "check price"**, as the logger now shows. Low
   suggestions miss by a median 25.5%.
4. Revisit `OWN_RECENCY_HALF_LIFE_DAYS` (120) and `OWN_LOOKBACK_DAYS` (365) once
   a few months of 2026-only sales exist.

### Method

- **Window:** every sale from 15 Oct 2025 to 14 Oct 2026 (2,727 sales). The 430
  without a part number in the title (mostly body panels priced from tiles) are
  out of scope for both methods.
- **No peeking:** each suggestion uses only sales strictly before the sale's
  date (including for working out the part type). The sale itself is never
  visible. Unit-tested in `tests/test_backtest.py`.
- **Inputs:** the car (model / generation / engine code) comes from the sale's
  own title, standing in for the vehicle ref. No market data.
- **Target:** the total the buyer paid (item + P&P), per the spec.
- **Old method:** the logger's old pre-fill, replayed exactly. This is
  `breakerpro_price` from `vag_parts_db.json` (exact entry, else the 6-digit
  group average). Its live-eBay leg can't be replayed. That only mattered on
  sales with no DB price (132 of 2,297), which are excluded from the
  head-to-head.
- **No numbers were tuned to this data.** The engine ran with the spec
  defaults in `pricing_config.py`. The one engine change made during this phase
  (part-type lookup now also respects the "before" date) made the backtest
  stricter, not easier.
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

### Headline: head-to-head on the same sales

| Method | Sales | MdAPE | Within ±15% | Median bias |
|---|---|---|---|---|
| Old method (BreakerPro DB price) | 2165 | 20.0% | 40.5% | -12.3% |
| New engine (spec, no market data) | 2165 | 21.9% | 35.6% | +0.0% |

Sale by sale, the new engine was closer on **827** sales, the old method on **1020**, and they tied on 318.

New engine on every sale with a part number (2297): MdAPE 22.7%, within ±15% 34.7%.

### Where the engine wins and loses

| Subset (same sales for both) | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| Engine priced from our own sales of this number | 1128 | 20.0% | 39.4% | 20.0% | 40.0% |
| Engine had to use a fallback (no own sales) | 1037 | 18.9% | 41.8% | 25.0% | 30.8% |
| Free-postage sales only | 1270 | 14.3% | 50.5% | 20.0% | 38.0% |

### By engine confidence

| Confidence | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| High | 201 | 17.6% | 46.0% | 17.6% | 43.3% |
| Medium | 981 | 21.9% | 37.7% | 20.0% | 38.6% |
| Low | 1115 | 18.9% | 42.1% | 25.5% | 29.7% |

### By engine source (which rung priced it)

| Source | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| breakerpro_group | 848 | 18.9% | 42.2% | 24.2% | 32.1% |
| category_default | 119 | 4.7% | 72.0% | 37.5% | 19.3% |
| own_sold | 1150 | 20.0% | 39.4% | 20.0% | 39.8% |
| part_type | 180 | 22.3% | 35.4% | 27.6% | 24.4% |

### Old method by match type

| Old match | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| exact | 1667 | 18.9% | 43.6% | 21.4% | 37.3% |
| group | 498 | 25.1% | 30.3% | 25.0% | 29.7% |

### Over time (leakage check)

`vag_parts_db.json` was built on 2026-03-07 from BreakerPro stock exports. The last column is the share of sales where the old method's exact price is *identical* to that item's own sold price.

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% | Old price = item's own price |
|---|---|---|---|---|---|---|
| 2025 Q4 | 340 | 23.3% | 28.2% | 22.2% | 31.8% | 72% |
| 2026 Q1 | 540 | 15.1% | 50.0% | 21.4% | 37.0% | 70% |
| 2026 Q2 | 584 | 19.9% | 39.9% | 21.3% | 38.5% | 34% |
| 2026 Q3 | 627 | 21.4% | 37.8% | 23.3% | 32.9% | 25% |
| 2026 Q4 | 74 | 12.4% | 55.4% | 19.0% | 41.9% | 24% |

### By part type (15 most common)

| Part type | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| door lock | 103 | 29.7% | 32.4% | 20.0% | 35.9% |
| window motor front | 58 | 24.0% | 34.5% | 21.4% | 34.5% |
| headlight | 51 | 14.0% | 55.8% | 33.3% | 17.6% |
| seat belt front | 50 | 15.8% | 50.0% | 25.1% | 24.0% |
| window regulator | 44 | 22.5% | 29.5% | 20.5% | 36.4% |
| airbag squib/slip ring | 41 | 14.9% | 51.2% | 30.0% | 31.7% |
| bonnet release handle | 41 | 18.2% | 46.3% | 12.4% | 56.1% |
| rear light | 39 | 18.9% | 33.3% | 29.8% | 20.5% |
| seat belt buckle front | 39 | 18.8% | 38.9% | 14.3% | 51.3% |
| fuel pump | 38 | 20.9% | 41.7% | 19.3% | 39.5% |
| airbag module | 37 | 21.9% | 37.8% | 33.4% | 24.3% |
| bonnet release cable | 37 | 23.9% | 27.0% | 16.7% | 32.4% |
| engine ecu | 36 | 25.9% | 42.9% | 34.8% | 16.7% |
| central control unit | 36 | 20.0% | 41.7% | 37.1% | 16.7% |
| heater flap actuator | 35 | 25.0% | 31.4% | 26.2% | 31.4% |

### Diagnostic: opt-in `USE_BREAKERPRO_EXACT` rung (off by default)

| Method | Sales | MdAPE | Within ±15% |
|---|---|---|---|
| Old method | 2165 | 20.0% | 40.5% |
| Engine + BreakerPro exact rung | 2165 | 18.9% | 40.7% |

Quarter by quarter with the rung on:

| Quarter | Sales | Old MdAPE | Old ±15% | New MdAPE | New ±15% |
|---|---|---|---|---|---|
| 2025 Q4 | 340 | 23.3% | 28.2% | 19.5% | 34.1% |
| 2026 Q1 | 540 | 15.1% | 50.0% | 16.7% | 47.0% |
| 2026 Q2 | 584 | 19.9% | 39.9% | 18.9% | 43.3% |
| 2026 Q3 | 627 | 21.4% | 37.8% | 20.0% | 35.7% |
| 2026 Q4 | 74 | 12.4% | 55.4% | 15.9% | 47.3% |

<!-- AUTO:END -->
