# Phase 3b Result Report

**Date:** 2026-05-09
**Status:** Phase 3b COMPLETE. All 5 tasks delivered, pushed to `main` (live), and verified end-to-end on S25 Ultra (412px viewport).
**Repo:** `pulled-dev/parts-logger-backend` (monorepo — `index.html` at root served by GitHub Pages, FastAPI backend deployed to Railway).
**Live frontend:** https://pulled-dev.github.io/parts-logger-backend/
**Live backend:** https://web-production-7a1f0.up.railway.app

---

## Commit ledger

| Task | Commit | Title |
| --- | --- | --- |
| Pre-3b infra | (Railway dashboard) | Volume `web-volume` mounted at `/data`, env `DB_PATH=/data/vehicles.db`, 124 vehicles re-imported, restart-persistence verified |
| Task 0 | `1f0814e` | Phase 3b Task 0: vehicle CRUD endpoints + soft delete |
| Task 1 | `beee7e4` | Phase 3b Task 1: body panel categories endpoint |
| Task 2 | `95741cb` | Phase 3b Task 2: paint codes exposed via `/paint-codes`, cached on frontend init |
| Task 3 | `1ce3cdf` | Phase 3b Task 3: vehicle CRUD overlay |
| Task 4 | `2e9991d` | Phase 3b Task 4: Body Panel mode flow |
| Task 5 | (this commit) | Phase 3b Task 5: result doc |

All commits sit on `main`, no uncommitted changes (working tree clean apart from `__pycache__/`).
`git log origin/main..HEAD` is empty — everything is already on the remote.

---

## Automated pre-smoke checks (2026-05-09)

| Check | Endpoint / source | Result |
| --- | --- | --- |
| Backend health | `GET /health` | 200 — `{status:"ok", mode:"live", ebay_configured:true, claude_configured:true}` |
| Vehicle count (active) | `GET /vehicles` | **124** entries; sample row `VEH126` Volkswagen Polo 2G 2018-2024 LA7N "Limestone Grey", `is_active:true` |
| Paint codes | `GET /paint-codes` | **32** entries, `{code,name}` shape |
| Body panel categories | `GET /body-panel-categories` | **17** entries, `{id,label}` shape (Door×4, Wing×2, Bonnet, Bumper×2, Tailgate, Roof, Quarter×2, Sill×2, Mirror×2) |
| Live frontend HTML | `GET https://pulled-dev.github.io/parts-logger-backend/` | 200, 2724 lines |
| Frontend markers present | grep on live HTML | All present: `vehicles-btn`, `vehicles-overlay`, `veh-ref-locked`, `PA_VEHICLES`, `PA_PAINT_CODES`, `PA_BODY_PANEL_CATS`, `bp-step1/2/3`, `mode-body-panel`, `mode-part-number`, `s-demo`, `pa_cfg`, `pulledapart_last_ref`, `lookup`, `loadVehicles`, `loadPaintCodes` |

---

## 11-step smoke test on S25 Ultra (manual)

Run by Naveed against live URL on S25 Ultra (412px viewport). All 11 steps **PASS**.

| # | Step | Result |
| --- | --- | --- |
| 1 | App loads, no console errors, branding intact | PASS |
| 2 | Mode toggle cycles through all available modes (Part Number ↔ Body Panel) | PASS |
| 3 | Part Number mode (v1 lookup): part lookup resolves market price, log appended, badge green | PASS |
| 4 | Part Number mode: second lookup variant works (stand-in for spec's Gearbox step — same `lookup()` path) | PASS |
| 5 | Vehicles overlay: opens, lists 124 vehicles, search works | PASS |
| 6 | Vehicles overlay: add new vehicle → toast, list refreshes, new vehicle visible | PASS |
| 7 | Vehicles overlay: edit existing vehicle, ref is read-only (no input element in DOM in edit mode), notes save → toast | PASS |
| 8 | Vehicles overlay: soft-delete via confirm dialog → vehicle gone from list, stays gone after overlay close+reopen | PASS |
| 9 | Body Panel mode: select vehicle → category → Step 3 confirm → Log Part → success badge + history entry, return to Step 2 with same vehicle | PASS |
| 10 | Mock/demo mode toggle in v1 settings panel: still works, no regressions to Part Number lookup | PASS |
| 11 | Page reload: history persists (`pulledapart_logged_parts`), config persists (`pa_cfg`), last ref persists (`pulledapart_last_ref`) | PASS |

---

## Definition of Done — checklist

- [x] Backend: POST/PATCH/DELETE `/vehicles` endpoints live on Railway (Task 0)
- [x] Backend: `is_active` column added, soft delete working, `GET /vehicles` defaults to active-only (Task 0)
- [x] Backend: `GET /body-panel-categories` returns 17-item list (Task 1)
- [x] Backend: `GET /paint-codes` returns 32-item list (Task 2 — added because `/lookup-panel` shape didn't match; documented in Task 2 commit)
- [x] Frontend: `window.PA_PAINT_CODES` populated from backend at init (Task 2)
- [x] Frontend: Vehicles overlay (full-screen) — list, search, add, edit, soft-delete all work (Task 3)
- [x] Frontend: Ref field is read-only on edit form (dynamic render — no `<input>` in DOM in edit mode) (Task 3)
- [x] Frontend: Body Panel mode replaces placeholder, three-step flow works end-to-end (Task 4)
- [x] Frontend: v1 features intact — mock/demo toggle, config panel, edit description, price override, status badge, ERROR badge, `lookup()` function, history persistence
- [x] All commits pushed, atomic, prefixed `Phase 3b Task N:`
- [x] PHASE-3B-RESULT.md committed (this file)
- [x] All 11 smoke test steps PASS on S25 Ultra
- [x] No regressions in `lead-logger/` (untouched throughout phase)

---

## Spec deviations

1. **Spec field schema vs actual model (Task 0).** Spec proposed `vrm`, `year`, `colour_name` on `VehicleCreate/Update`. The actual DB/Pydantic model (carried from earlier phases) uses `vin`, `year_range`, `paint_name`. Task 0 implemented endpoints against the **real model**, not the spec's hypothetical fields. Spec was written from memory and contradicted the production schema — see MEMORY hard rule on spec discipline. Non-blocking; CSV import + frontend overlay both work against the real schema.

2. **Paint codes endpoint (Task 2).** Spec's primary plan was to fetch from existing `/lookup-panel`. Investigation showed that endpoint returns a different shape (per-vehicle lookup, not the dictionary). Spec's fallback path was followed: a new `GET /paint-codes` endpoint was added on the backend, returning the 32-entry `{code,name}` list from `paint_codes.py:PAINT_CODES`. Frontend caches in `window.PA_PAINT_CODES` at init.

3. **No hardcoded paint code list ever existed in the deployed frontend.** Task 2 was reframed from "delete frontend list + replace with fetch" to "expose backend dict + prime cache on init". Verified by grep of v1 source 2026-05-09. Documented in MEMORY.

4. **Body Panel logging does NOT call `/lookup` (Task 4).** Backend's `clean = part_number.upper().replace(" ", "")` (`main.py:325`) collapses any descriptive query into one no-space token, which eBay returns 0 results for (verified via curl 2026-05-09). Body Panel logs therefore use a `/health` ping (~430ms typical, 6s timeout) for online detection — fails offline so the ERROR badge still triggers correctly. Body-panel records persist with `source:'body_panel'` and all market fields null; `total_listings:0` means they render via the existing `isPaint` no-pricing branch in `render()`. User's price override is the only price for body-panel rows.

5. **Mode set in current build is Part Number + Body Panel** (not Engine + Gearbox + Body Panel as the spec smoke test assumed). Phase 3a Task 2 introduced a 2-mode toggle scaffold; engine/gearbox split is a future-phase concern. Smoke step 4 was treated as a second Part Number lookup (same `lookup()` path the spec was checking) per Naveed's approval mid-test.

6. **No localStorage/hash/query persistence on mode toggle.** Defaults to Part Number on every load. Documented in MEMORY as a deliberate choice — revisit when Body Panel persistence becomes useful.

---

## Deferred items / Phase 4 candidates

- **Backend `is_descriptive_query` flag on `/lookup`.** Skip the `upper()`+strip-spaces normalisation when set, so Body Panel descriptive queries can hit eBay for real used-panel pricing (used wings/doors/bumpers).
- **VAG parts JSON learned-entry persistence.** `vag_parts_db.json` runtime writes via `vag_lookup.py` go to ephemeral `/app` filesystem and get wiped each redeploy. Baseline 528 KB committed to repo survives, but Claude-learned entries don't. Move learned entries to SQLite or write to `/data/`.
- **OCR for vehicle ref recognition** (out-of-scope for 3b; flagged in spec).
- **BreakerPro public API** (no public API confirmed at time of phase).
- **Bulk edit/delete vehicles, hard-delete-restore UI, vehicle merge/dedupe** (deliberately out-of-scope for 3b).
- **Body panel sub-categories** (door cards vs door shells, etc.) and **photo attachments** on body-panel logs.
- **Pagination on vehicle list** — current 124-row implementation is in-memory + client-side filter; revisit at 1,000+ vehicles.
- **Mode persistence** revisit once Body Panel is doing more than logging.

---

## Lessons captured to MEMORY

- v1 `style.display='none'` on a parent does NOT reliably disable inputs on Android Chrome. If a field must be uneditable, do not put an `<input>` in the DOM at all. (Hit during Task 3; fixed via dynamic render of the ref cell.)
- Any flex-column container with `max-height` must set `flex-shrink:0` on children whose content exceeds their min-height. (Hit during Task 4 S25 Ultra test — `.bp-veh-row` content was bleeding into the next row's gap.)
- Spec discipline (hard rule): any spec asserting "preserve v1 behaviour" must cite file path + line range. Specs written from memory between sessions hallucinate features. Re-read source before asserting "preserve X". (Re-confirmed during Task 0 schema deviation and Task 2 hardcoded-list non-existence.)

---

## Sign-off

Phase 3b is complete. Production frontend + backend are aligned, all 11 smoke steps PASS on S25 Ultra, no v1 regressions, no `lead-logger/` impact. Ready for Phase 4 planning.
