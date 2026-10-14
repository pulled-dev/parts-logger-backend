# Claude Code Task — Parts Logger v2: Body Panel multi-select + pricing schedule + CSV codes

Repo: `pulled-dev/parts-logger-backend` (monorepo). Frontend is the single-file `index.html`
(served via GitHub Pages). Backend is FastAPI. Body-panel categories are defined in
`categories.py` as `BODY_PANEL_CATEGORIES` and served by the `/body-panel-categories`
endpoint (in `lookup_router.py`). The frontend body-panel flow lives in the `initBodyPanel()`
IIFE inside `index.html`.

## Goal

Rework "Body Panel" mode from one-panel-at-a-time into: pick vehicle → multi-select any number
of panels → a pricing screen with one row per selected item → fill prices (and flip toggles) →
log the batch → export CSV. Update the category list. Change CSV output so the trade code goes
in the Description column and the Part Number column is left blank.

Do NOT touch "Part Number" mode, the eBay message bot, or anything under BreakerPro. Keep
`index.html` a single file. Mobile-first — this is used on a Samsung S25 Ultra in the yard.

---

## Task 1 — Replace the category list (`categories.py`)

Replace `BODY_PANEL_CATEGORIES` entirely with the list below. Note the new fields: `csv_code`
(what goes in the CSV Description column), `paint` (bool — append the vehicle paint code when
true), and optional `toggles` (rendered as chip toggles on the pricing row; first option is the
default).

```python
BODY_PANEL_CATEGORIES: list[dict] = [
    # ── Body panels — painted (paint code appended in CSV when available) ──
    {"id": "door_front_left",  "label": "Door — Front Left",   "csv_code": "NSF Door",     "paint": True},
    {"id": "door_front_right", "label": "Door — Front Right",  "csv_code": "OSF Door",     "paint": True},
    {"id": "door_rear_left",   "label": "Door — Rear Left",    "csv_code": "NSR Door",     "paint": True},
    {"id": "door_rear_right",  "label": "Door — Rear Right",   "csv_code": "OSR Door",     "paint": True},
    {"id": "wing_front_left",  "label": "Wing — Front Left",   "csv_code": "NSF Wing",     "paint": True},
    {"id": "wing_front_right", "label": "Wing — Front Right",  "csv_code": "OSF Wing",     "paint": True},
    {"id": "bonnet",           "label": "Bonnet",              "csv_code": "Bonnet",       "paint": True},
    {"id": "bumper_front",     "label": "Bumper — Front",      "csv_code": "Front Bumper", "paint": True},
    {"id": "bumper_rear",      "label": "Bumper — Rear",       "csv_code": "Rear Bumper",  "paint": True},
    {"id": "tailgate",         "label": "Tailgate / Boot Lid", "csv_code": "Tailgate",     "paint": True},
    {"id": "fuel_flap",        "label": "Fuel Flap",           "csv_code": "Fuel Flap",    "paint": True},

    # ── Wing mirrors — painted, with actuation + fold toggles ──
    {"id": "mirror_left",  "label": "Mirror — Left",  "csv_code": "NS Mirror", "paint": True,
     "toggles": [{"key": "act",  "options": ["Electric", "Manual"]},
                 {"key": "fold", "options": ["Folding", "Non-Folding"]}]},
    {"id": "mirror_right", "label": "Mirror — Right", "csv_code": "OS Mirror", "paint": True,
     "toggles": [{"key": "act",  "options": ["Electric", "Manual"]},
                 {"key": "fold", "options": ["Folding", "Non-Folding"]}]},

    # ── Lights — no paint ──
    {"id": "headlight_driver",           "label": "Headlight — Driver",           "csv_code": "OSF Headlight",   "paint": False},
    {"id": "headlight_passenger",        "label": "Headlight — Passenger",        "csv_code": "NSF Headlight",   "paint": False},
    {"id": "rear_light_driver_outer",    "label": "Rear Light — Driver Outer",    "csv_code": "OSR Outer Light", "paint": False},
    {"id": "rear_light_driver_inner",    "label": "Rear Light — Driver Inner",    "csv_code": "OSR Inner Light", "paint": False},
    {"id": "rear_light_passenger_outer", "label": "Rear Light — Passenger Outer", "csv_code": "NSR Outer Light", "paint": False},
    {"id": "rear_light_passenger_inner", "label": "Rear Light — Passenger Inner", "csv_code": "NSR Inner Light", "paint": False},

    # ── Drivetrain — no paint, with toggle ──
    {"id": "engine",  "label": "Engine",  "csv_code": "Engine",  "paint": False,
     "toggles": [{"key": "fuel",  "options": ["Diesel", "Petrol"]}]},
    {"id": "gearbox", "label": "Gearbox", "csv_code": "Gearbox", "paint": False,
     "toggles": [{"key": "trans", "options": ["Manual", "Auto"]}]},
]
```

**Verify:** run the backend (`uvicorn main:app --reload`) and
`curl -s localhost:8000/body-panel-categories | python -m json.tool` — confirm 21 items, the 5
removals gone (roof, sill_left, sill_right, quarter_panel_left, quarter_panel_right), and the
new fields present.

---

## Task 2 — Multi-select grid (Step 2, `index.html` → `initBodyPanel`)

- Change the single `selectedCategory` model to a `selectedCategories` **array** (preserve tap
  order — that's the order rows appear on the pricing screen).
- In `renderCategories()`, tapping a `.bp-cat-btn` now **toggles** a `selected` class and
  adds/removes the item from `selectedCategories` instead of jumping straight to Step 3.
  Give selected tiles a clear highlight (use the existing red accent `#E31E24`).
- Add a primary action button under the grid: **"Price N item(s)"** — disabled when nothing is
  selected, live-updating the count. Tapping it calls `enterPricingSchedule()` (was `enterStep3`).
- Keep the Step-2 "Change Vehicle" back button behaviour.

## Task 3 — Pricing schedule (Step 3 rebuild)

Replace the single price/description form with a scrollable list — one row per item in
`selectedCategories`, in tap order. Each row shows:

- The item's `csv_code` as the row title (e.g. "OSF Door").
- For items with `toggles`: a chip toggle per toggle group, defaulting to the first option.
  Selected chip highlighted; tap to switch.
- For `paint: true` items: show the vehicle's paint code as a small muted hint if on file (or
  "no paint code" warning, reusing the existing `bp-warn` style).
- A price input (numeric, same sanitising as the current `logPart`).

Footer: a **"Log all"** button that writes every row into `parts[]` at once (batch), then returns
to Step 2 with the grid cleared for the next car (same pattern as the current post-log reset).
Rows with a blank price still log (price blank in CSV) — do not block on empty prices.

**Description string builder** for each logged row:
```
base = item.csv_code
if item.toggles: base += " — " + [selected option of each toggle, space-joined]
if item.paint and vehicle.paint_code: base += " — " + vehicle.paint_code
```
Examples: `OSF Door — LC9X` · `Engine — Diesel` · `Gearbox — Manual` ·
`NS Mirror — Electric Folding — LC9X` (mirror with paint on file).

When pushing to `parts[]`, set:
- `part_number: ""`  (blank — Naveed fills real part numbers manually later)
- `description: <the built string above>`
- `your_price: <parsed price or null>`
- `vehicle_ref: v.ref`, `source: "body_panel"`, other price fields null as now.

## Task 4 — On-screen card + CSV

- The parts-list card currently shows `p.part_number` as the `.card-pn` headline. For
  `source === "body_panel"` items `part_number` is now blank, so fall back to showing
  `p.description` as the headline so the item is still identifiable on screen.
- CSV export (`exportBtn`): confirm body-panel rows now emit a **blank Part Number** column
  (the existing `/^[A-Z0-9]+$/` guard already yields blank for an empty string) and the code in
  the **Description** column. No change should be needed to the export function itself — verify,
  don't assume.

---

## Feedback loop — build → test → fix → retest until green

1. Make the Task 1 change; verify the endpoint (curl above).
2. Make the Task 2–4 changes.
3. Serve `index.html` against the running backend and run this smoke test end to end:
   - Body Panel mode shows 21 tiles; the 5 removed panels are absent.
   - Select 3 items including **Engine** and **Mirror — Left**; the "Price 3 items" button enables.
   - On the pricing screen: Engine shows a Diesel｜Petrol toggle, Mirror shows two toggles, all
     three show price inputs; the paint hint appears on the mirror if the test vehicle has a code.
   - Set toggles (leave one item's price blank on purpose), tap "Log all".
   - All 3 land in the parts list with readable headlines; the blank-price one shows no price.
   - Export CSV, open it, and assert: Part Number column blank for all 3; Description reads
     `Engine — Diesel`, `NS Mirror — Electric Folding[ — <paint>]`, and the third item's code;
     Price populated where entered; Ref populated.
4. If any step fails, diagnose the specific cause, fix, and rerun the **whole** smoke test.
   Do not stop until every assertion passes.
5. Commit atomically: (a) categories.py list change, (b) frontend multi-select + schedule,
   (c) CSV/card tweaks — with clear messages.

## Constraints
- `index.html` stays a single file; no new build step or dependencies.
- Don't regress "Part Number" mode or the settings/paint-code caching already in place.
- Mobile-first: tiles and chips must be tap-friendly at S25 Ultra width.
