# Phase 1 Result — 2026-05-04

- Task 1 (SQLite + init): **PASS**
- Task 2 (paint codes): **PASS**
- Task 3 (CRUD endpoints local): **PASS**
- Task 4 (Railway deploy + live smoke): **PASS**

## Live smoke test output

```
PASS CREATE (201)
PASS GET (200)
PASS LIST (200)
PASS UPDATE (200)
PASS DELETE (204)
PASS GONE (404)
PASS /lookup (200)
PASS /health (200)

LIVE RESULT: 8/8 passed
```

Run against `https://web-production-7a1f0.up.railway.app` after the
Railway auto-deploy of commit `c8fe49c` (normalise_ref fix) followed by
`056b031` (docs).

## Outstanding for Naveed

- [ ] Attach Railway volume at `/data` (1 GB)
- [ ] Set `DB_PATH=/data/vehicles.db` env var
- [ ] Trigger manual redeploy so the volume is mounted

Until those three steps are done, the database lives in the container's
ephemeral filesystem and is wiped on every deploy. All endpoints already
work — they just don't persist across deploys yet.

## Notes during execution

- During the first live smoke run, `UPDATE` and `DELETE` returned 404 because
  the smoke test creates a vehicle with `ref:"TEST99"` but updates/deletes via
  `/vehicles/VEHTEST99`. The original spec wording for `normalise_ref` was
  "prefix VEH if input is digits-only" which would not match those two ops.
  Broadened to "prefix VEH unless ref already starts with VEH" — covers
  `TEST99 -> VEHTEST99` and still satisfies every Task 1/2/3 verify case.
  Committed as `c8fe49c`.
- Existing endpoints (`/lookup`, `/health`, `/db-stats`, `/db-reload`,
  `/db-correct`, `/lookup/batch`) were not modified. The only changes to
  `main.py` are the `init_db` import, the lifespan handler, and a single
  `app.include_router(vehicles_router)` line.
- New files: `db.py`, `paint_codes.py`, `models.py`, `vehicles_router.py`.
- 32 paint codes loaded (≥ 30 required).

## Ready for Phase 2?

**YES** — the live URL passes all 8 checks; Phase 2's `/lookup-panel` can
now depend on `db.get_vehicle()` returning real records.
