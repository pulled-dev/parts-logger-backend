# Parts Logger v2.0 — Phase 3b Spec

**Repo:** pulled-dev/parts-logger-backend
**Backend:** https://web-production-7a1f0.up.railway.app
**Frontend:** https://pulled-dev.github.io/parts-logger-backend/
**Local working dir:** `C:\Users\ACER\Desktop\Pulled Apart Ltd\parts-logger-v2\parts-logger-backend\`
**Baseline:** Phase 3a complete (commits ffd56a0, c883ad1, f78a553, bfd4c8f). Assumes clean 3a verified on S25 Ultra.

---

## Hard rules — read before touching code

1. **v1 IDs preserved.** Do not rename or remove: `s-demo`, `s-url`, `pa_cfg`, `pulledapart_last_ref`, `lookup()` function. v1 features must remain functional: mock mode, config panel, edit description, price override, status badge.
2. **Single-file frontend.** All frontend changes go in `index.html` at repo root. No build step, no bundler, no new files.
3. **Do not touch `lead-logger/` subdirectory.** Separate deployed PWA.
4. **Mobile-first.** S25 Ultra (412px viewport) is the test target. Every UI change verified on phone before commit.
5. **Atomic commits per task.** One task = one commit. Commit message format: `Phase 3b Task N: <summary>`.
6. **Build → test → fix → retest** at the end of every task. If a task fails 3 build/test cycles, stop and write `BLOCKED.md` (template at end of spec).
7. **Backend changes deploy to Railway automatically on push to main.** Wait 60s after push before frontend testing.
8. **Paint codes: backend is source of truth.** Frontend hardcoded list is removed in Task 2. Do not re-introduce.
9. **Vehicle ref is immutable after creation.** Edit form locks the ref field. This is non-negotiable.
10. **Soft delete only.** No DELETE endpoints that remove rows. Use `is_active` flag.

---

## Pre-phase check

Before starting Task 0, run these and confirm output:

```bash
cd "C:\Users\ACER\Desktop\Pulled Apart Ltd\parts-logger-v2\parts-logger-backend"
git status                          # must be: clean, on main, up to date with origin
git log --oneline -5                # must show: bfd4c8f, f78a553, c883ad1, ffd56a0 in recent history
gh auth status                      # must show: pulled-dev, HTTPS
curl https://web-production-7a1f0.up.railway.app/health
                                    # must return 200
curl https://web-production-7a1f0.up.railway.app/vehicles | head -c 200
                                    # must return JSON array, ~124 vehicles
```

If any check fails, stop and report. Do not proceed.

---

## Task 0 — Backend: vehicle CRUD endpoints + soft delete

**Goal:** Add POST/PATCH endpoints and `is_active` flag to vehicles table. List endpoint filters out inactive by default.

```xml
<task id="3b-0" type="backend">
  <files>
    backend/main.py
    backend/models.py (or wherever SQLAlchemy Vehicle model lives)
    backend/schemas.py (or wherever Pydantic schemas live)
  </files>

  <changes>
    1. Add column to Vehicle model:
       is_active: bool = Column(Boolean, default=True, nullable=False)

    2. Migration: on app startup, run ALTER TABLE if column missing.
       Use this pattern (SQLite-safe):
         try:
             conn.execute("ALTER TABLE vehicles ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL")
         except OperationalError:
             pass  # column already exists

    3. Add Pydantic schemas:
       - VehicleCreate: vrm, make, model, year, ref, paint_code, colour_name, notes (all optional except vrm and ref)
       - VehicleUpdate: same as Create but ref is excluded (immutable)

    4. Endpoints:
       - POST /vehicles
         body: VehicleCreate
         returns: Vehicle (201)
         validates: ref must be unique, vrm must be unique among active vehicles
         on conflict: 409 with {"error": "ref already exists"} or vrm equivalent

       - PATCH /vehicles/{ref}
         body: VehicleUpdate
         returns: Vehicle (200)
         404 if ref not found
         ref field in body is ignored if present

       - DELETE /vehicles/{ref}
         soft delete: sets is_active = False
         returns: 204
         404 if ref not found

       - GET /vehicles
         add query param: include_inactive: bool = False
         default behaviour: only returns is_active = True

    5. Update CSV import to set is_active=True on insert.
  </changes>

  <test>
    curl -X POST https://web-production-7a1f0.up.railway.app/vehicles \
      -H "Content-Type: application/json" \
      -d '{"vrm":"TEST123","ref":"VEH-TEST-0001","make":"Audi","model":"A3","year":2018,"paint_code":"LY7W","colour_name":"Florett Silver"}'
    # expect: 201 with vehicle object

    curl -X POST https://web-production-7a1f0.up.railway.app/vehicles \
      -H "Content-Type: application/json" \
      -d '{"vrm":"TEST123","ref":"VEH-TEST-0001"}'
    # expect: 409

    curl -X PATCH https://web-production-7a1f0.up.railway.app/vehicles/VEH-TEST-0001 \
      -H "Content-Type: application/json" \
      -d '{"notes":"updated"}'
    # expect: 200, notes field updated

    curl -X DELETE https://web-production-7a1f0.up.railway.app/vehicles/VEH-TEST-0001
    # expect: 204

    curl https://web-production-7a1f0.up.railway.app/vehicles | grep VEH-TEST-0001
    # expect: empty (soft deleted, hidden by default)

    curl "https://web-production-7a1f0.up.railway.app/vehicles?include_inactive=true" | grep VEH-TEST-0001
    # expect: present, is_active: false
  </test>

  <commit>Phase 3b Task 0: vehicle CRUD endpoints + soft delete</commit>

  <on_failure>
    If migration fails: check Railway logs, confirm column doesn't already exist with different type.
    If 409 not returned: check unique constraint on ref column.
    3 cycles max, then BLOCKED.md.
  </on_failure>
</task>
```

---

## Task 1 — Backend: body panel categories endpoint

**Goal:** Expose body panel categories as a dedicated endpoint, separate from existing engine/gearbox category dictionary.

```xml
<task id="3b-1" type="backend">
  <files>
    backend/main.py
    backend/categories.py (or wherever category dict lives)
  </files>

  <changes>
    1. Add body panel category list as Python constant:
       BODY_PANEL_CATEGORIES = [
           {"id": "door_front_left", "label": "Door — Front Left"},
           {"id": "door_front_right", "label": "Door — Front Right"},
           {"id": "door_rear_left", "label": "Door — Rear Left"},
           {"id": "door_rear_right", "label": "Door — Rear Right"},
           {"id": "wing_front_left", "label": "Wing — Front Left"},
           {"id": "wing_front_right", "label": "Wing — Front Right"},
           {"id": "bonnet", "label": "Bonnet"},
           {"id": "bumper_front", "label": "Bumper — Front"},
           {"id": "bumper_rear", "label": "Bumper — Rear"},
           {"id": "tailgate", "label": "Tailgate / Boot Lid"},
           {"id": "roof", "label": "Roof"},
           {"id": "quarter_panel_left", "label": "Quarter Panel — Left"},
           {"id": "quarter_panel_right", "label": "Quarter Panel — Right"},
           {"id": "sill_left", "label": "Sill — Left"},
           {"id": "sill_right", "label": "Sill — Right"},
           {"id": "mirror_left", "label": "Mirror — Left"},
           {"id": "mirror_right", "label": "Mirror — Right"},
       ]

    2. Endpoint:
       GET /body-panel-categories
       returns: BODY_PANEL_CATEGORIES list

    3. Do not modify existing /lookup-panel or category dictionary endpoints.
  </changes>

  <test>
    curl https://web-production-7a1f0.up.railway.app/body-panel-categories | python -m json.tool
    # expect: JSON array, 17 entries, each with id and label
  </test>

  <commit>Phase 3b Task 1: body panel categories endpoint</commit>

  <on_failure>
    Standard 3-cycle limit then BLOCKED.md.
  </on_failure>
</task>
```

---

## Task 2 — Frontend: kill hardcoded paint code list, fetch from backend

**Goal:** Remove the embedded paint code array in `index.html`. Fetch from `/lookup-panel` (or wherever the 32-code dictionary is exposed) on page load. Cache in `window.PA_PAINT_CODES`.

```xml
<task id="3b-2" type="frontend">
  <files>
    index.html
  </files>

  <changes>
    1. Locate the hardcoded paint code array in index.html. Delete it.

    2. Add fetch on page load (inside existing init flow, after config panel setup):
       async function loadPaintCodes() {
           try {
               const r = await fetch(`${API_BASE}/lookup-panel`);
               if (!r.ok) throw new Error(`HTTP ${r.status}`);
               window.PA_PAINT_CODES = await r.json();
           } catch (e) {
               console.error('Paint code fetch failed:', e);
               window.PA_PAINT_CODES = [];
               // show non-blocking error toast: "Paint codes unavailable — check connection"
           }
       }
       // call inside existing init function

    3. Update any code that reads paint codes to use window.PA_PAINT_CODES instead of the deleted array.

    4. If /lookup-panel doesn't return the 32-code dictionary in the format the frontend expects, add a new endpoint /paint-codes that does, and use that. Decide based on actual /lookup-panel response shape — check it first.

    5. Verify v1 lookup() function still works. Mock mode must still resolve paint codes (use cached window.PA_PAINT_CODES, fall back to empty array).
  </changes>

  <test>
    Build: open index.html locally (or push and load GitHub Pages URL after 60s).
    Test on S25 Ultra:
      a. Page loads without console errors.
      b. window.PA_PAINT_CODES is populated (check via remote console or add temp debug log).
      c. v1 engine mode: lookup still works.
      d. v1 gearbox mode: lookup still works.
      e. Disconnect wifi, reload — error toast shows, app doesn't crash.
  </test>

  <commit>Phase 3b Task 2: paint codes fetched from backend, hardcoded list removed</commit>

  <on_failure>
    If /lookup-panel response shape doesn't match: add /paint-codes endpoint in backend (small task, append to Task 1 commit if not yet pushed, otherwise new commit).
    3 cycles max, then BLOCKED.md.
  </on_failure>
</task>
```

---

## Task 3 — Frontend: vehicle CRUD overlay

**Goal:** Full-screen overlay accessed from a "Vehicles" button. Lists active vehicles, search by VRM/ref/make, add/edit/soft-delete.

```xml
<task id="3b-3" type="frontend">
  <files>
    index.html
  </files>

  <changes>
    1. Add "Vehicles" button to main UI (near mode toggle). Tapping opens full-screen overlay.

    2. Overlay structure:
       - Header: "Vehicles" title, close button (X), "+ Add" button
       - Search input: filters by VRM, ref, or make (client-side filter on loaded list)
       - Scrollable list: each row shows VRM (bold), make/model/year, ref (small/grey)
       - Tap row: opens edit form (same overlay, swap content)
       - Long-press or swipe: not in scope, use explicit edit/delete buttons in edit view

    3. Add form fields:
       - VRM (required, uppercased on blur)
       - Ref (required, format hint: VEH-YYYY-NNNN)
       - Make (text)
       - Model (text)
       - Year (number, 1990–current year)
       - Paint code (dropdown from window.PA_PAINT_CODES, or free text fallback)
       - Colour name (text)
       - Notes (textarea)
       - Save button → POST /vehicles
       - Cancel button → back to list

    4. Edit form: same fields except Ref is shown as read-only text (not an input). Save → PATCH /vehicles/{ref}. Includes "Delete" button at bottom (red), confirms with native confirm() dialog, then DELETE /vehicles/{ref}.

    5. List loads from GET /vehicles on overlay open. Show spinner while loading. Empty state: "No vehicles yet — tap + Add".

    6. After successful add/edit/delete: refresh list, show toast ("Vehicle added", "Vehicle updated", "Vehicle deleted").

    7. Vehicle dropdown elsewhere in app (Body Panel mode, future engine/gearbox modes) reads from same cached list. Refresh cache after CRUD ops.

    8. Mobile-first: full-screen overlay on phone, all tap targets ≥44px, form inputs use appropriate keyboards (inputmode="numeric" for year, autocapitalize for VRM).
  </changes>

  <test>
    On S25 Ultra:
      a. Tap Vehicles → overlay opens, list of ~124 vehicles loads.
      b. Search "VW" → list filters.
      c. Tap + Add → form opens, all fields visible without scroll-jank.
      d. Submit valid vehicle → toast, list refreshes, new vehicle visible.
      e. Submit duplicate ref → 409 handled, error message shown inline.
      f. Tap existing vehicle → edit form, ref is read-only.
      g. Edit notes, save → toast, list refreshes.
      h. Tap delete on edit form → confirm dialog → soft delete → toast, vehicle gone from list.
      i. Re-open overlay → deleted vehicle not present.
      j. Close overlay, return to main UI → mode toggle still works, v1 features intact.
  </test>

  <commit>Phase 3b Task 3: vehicle CRUD overlay</commit>

  <on_failure>
    Standard 3-cycle limit then BLOCKED.md.
  </on_failure>
</task>
```

---

## Task 4 — Frontend: Body Panel mode flow

**Goal:** Replace the Body Panel placeholder card from Phase 3a with the real flow: vehicle select → panel category → paint code (auto-resolved from vehicle) → log + eBay query.

```xml
<task id="3b-4" type="frontend">
  <files>
    index.html
  </files>

  <changes>
    1. Replace placeholder card content. Body Panel mode now shows three sequential steps:

       Step 1: Vehicle selector
         - Searchable dropdown / typeahead
         - Source: cached vehicle list (same as CRUD overlay)
         - Display: "VRM — Make Model (Year) — Ref"
         - Sorted by most recently added first
         - On select: store selected vehicle, advance to step 2, show vehicle summary card at top

       Step 2: Panel category selector
         - Source: GET /body-panel-categories (fetch on first Body Panel mode entry, cache in window.PA_BODY_PANEL_CATS)
         - Render as tap-grid of buttons (3 cols on mobile, ≥44px tap targets)
         - On tap: store selected category, advance to step 3

       Step 3: Confirm + log
         - Show: vehicle (VRM, ref), panel category label, paint code (auto-pulled from selected vehicle's paint_code field), colour name
         - If vehicle has no paint code: warn inline ("No paint code on file — edit vehicle to add"), allow proceed
         - Description field: pre-filled, editable (preserves v1 edit description feature). Default: "{Year} {Make} {Model} {Category Label} — Paint Code {paint_code} {colour_name}"
         - Price override field (preserves v1 price override feature)
         - "Log Part" button → POST to existing parts log endpoint (same one v1 engine/gearbox uses), payload includes vehicle ref, category, paint code, description, price
         - On success: append to history list (existing v1 pattern), generate eBay search query (existing v1 pattern), show status badge
         - "Reset" button → back to step 1

    2. Back button on each step returns to previous step without losing earlier selections.

    3. Mode toggle still works — switching away from Body Panel mid-flow discards in-progress selection (don't persist partial state, keep it simple).

    4. Status badge on success: same green badge as v1 engine/gearbox (preserves v1 status badge feature).

    5. ERROR badge on failure: same as v1 (preserves Phase 3a fix).
  </changes>

  <test>
    On S25 Ultra:
      a. Switch to Body Panel mode → Step 1 visible, vehicle dropdown populated.
      b. Type partial VRM → list filters → tap result → Step 2 appears, vehicle summary at top.
      c. Tap "Door — Front Left" → Step 3 appears, paint code auto-shown.
      d. Description pre-filled correctly with year/make/model/category/paint code.
      e. Edit description → change persists.
      f. Override price → change persists.
      g. Tap Log Part → success toast + status badge + history entry + eBay query generated.
      h. Tap Reset → back to Step 1.
      i. Select vehicle with no paint code → warning shown, proceed allowed.
      j. Switch to Engine mode mid-flow → v1 engine flow works, no leaked Body Panel state.
      k. Switch back to Body Panel → fresh Step 1.
      l. Offline test: disconnect wifi at Step 3, tap Log Part → ERROR badge shows.
  </test>

  <commit>Phase 3b Task 4: Body Panel mode flow</commit>

  <on_failure>
    If parts log endpoint doesn't accept body panel payload shape: add `part_type` field to existing endpoint (engine/gearbox/body_panel), deploy, retest.
    3 cycles max, then BLOCKED.md.
  </on_failure>
</task>
```

---

## Task 5 — Live deploy + 11-step smoke test

**Goal:** All commits pushed, GitHub Pages live, Railway live, full end-to-end verification on S25 Ultra.

```xml
<task id="3b-5" type="verification">
  <steps>
    1. git push origin main (all 5 task commits)
    2. Wait 60s for Railway deploy + GitHub Pages rebuild
    3. Open https://pulled-dev.github.io/parts-logger-backend/ on S25 Ultra
    4. Smoke test (11 steps):

       [1] App loads, no console errors, branding intact
       [2] Mode toggle: Engine → Gearbox → Body Panel, all sections visible
       [3] Engine mode (v1): lookup function works, paint code resolves, log works
       [4] Gearbox mode (v1): lookup works, log works
       [5] Vehicles overlay: opens, lists 124+ vehicles, search works
       [6] Vehicles overlay: add new vehicle, appears in list
       [7] Vehicles overlay: edit vehicle, ref is read-only, save works
       [8] Vehicles overlay: soft delete vehicle, disappears from list
       [9] Body Panel mode: select vehicle → category → log → eBay query generated
       [10] Mock mode toggle (v1 config panel): still works, no regressions
       [11] Reload page: history persists (localStorage intact), config persists

    5. Write PHASE-3B-RESULT.md documenting:
       - Commit hashes for each task
       - Smoke test results (PASS/FAIL per step)
       - Any deviations from spec
       - Any deferred items
    6. Commit PHASE-3B-RESULT.md to main.
  </steps>

  <commit>Phase 3b Task 5: result doc</commit>

  <on_failure>
    Any smoke test step fails → diagnose → fix in a follow-up commit referencing the failed step → retest full smoke. Do not declare 3b complete with failures.
  </on_failure>
</task>
```

---

## Definition of Done

- [ ] Backend: POST/PATCH/DELETE `/vehicles` endpoints live on Railway
- [ ] Backend: `is_active` column added, soft delete working, default GET filters inactive
- [ ] Backend: GET `/body-panel-categories` returns 17-item list
- [ ] Frontend: hardcoded paint code list deleted, `window.PA_PAINT_CODES` populated from backend
- [ ] Frontend: Vehicles overlay (full-screen) — list, search, add, edit, soft-delete all work
- [ ] Frontend: Ref field is read-only on edit form
- [ ] Frontend: Body Panel mode replaces placeholder, three-step flow works end-to-end
- [ ] Frontend: v1 features all intact — mock mode, config panel, edit description, price override, status badge, ERROR badge, lookup function, history persistence
- [ ] All commits pushed, atomic, prefixed `Phase 3b Task N:`
- [ ] PHASE-3B-RESULT.md committed
- [ ] All 11 smoke test steps PASS on S25 Ultra
- [ ] No regressions in `lead-logger/` (untouched)

---

## Out of scope for Phase 3b

- OCR for vehicle ref recognition (Phase 4 candidate)
- BreakerPro API integration (no public API confirmed)
- Bulk edit/delete vehicles
- Hard delete or restore-from-soft-delete UI
- Body panel sub-categories (e.g. door cards vs door shells)
- Photo attachment to body panel logs
- Vehicle merge/dedupe tooling
- Pagination on vehicle list (124 vehicles fits in memory fine; revisit at 1,000+)
- Editing CSV import schema
- Multi-vehicle batch logging

---

## BLOCKED.md template

If any task fails 3 build/test cycles, stop and create `BLOCKED.md` at repo root with:

```markdown
# Phase 3b — BLOCKED

**Task:** [Task N — title]
**Date:** [YYYY-MM-DD]
**Attempts:** 3

## What I tried
1. [Attempt 1: approach + result]
2. [Attempt 2: approach + result]
3. [Attempt 3: approach + result]

## Error / failure mode
[Exact error message, stack trace, or behaviour observed]

## Hypotheses
- [What might be wrong]
- [What might be wrong]

## What I need from Naveed
- [Decision needed / info needed / external dependency]

## Files touched (uncommitted)
[git diff --stat output]

## Suggested next step
[Single concrete action]
```

Commit `BLOCKED.md` to main, push, and stop. Do not proceed to next task.

---

## Final note for Claude Code

Run tasks in order: 0 → 1 → 2 → 3 → 4 → 5. Each task is one commit. Backend tasks (0, 1) deploy and verify before moving to frontend. Do not batch commits. Do not skip the smoke test. If you finish Task 4 and the smoke test reveals a v1 regression, that's a fail — fix it before declaring done.

End every task with: build → test on S25 Ultra → if errors, diagnose and fix → retest until working.
