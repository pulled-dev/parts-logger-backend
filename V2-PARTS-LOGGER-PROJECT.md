# Parts Logger v2.0 — Backend Setup

This document captures setup steps for the v2.0 vehicle database that live
*outside* the codebase (Railway dashboard config). The code itself is auto-
deployed from `main` and needs no manual intervention to ship.

## Setup Steps for v2.0 Vehicle Database

The vehicle database is a SQLite file at the path given by the `DB_PATH` env
var. Without a mounted volume, the file lives in the container's ephemeral
filesystem and is wiped on every deploy. Endpoints still work, just no
persistence yet. To make data persistent across deploys:

1. Open Railway → your service → **Volumes** tab → **+ Add Volume**.
   - Mount path: `/data`
   - Size: 1 GB (more than enough for vehicles at 2–3/week for years)
2. Go to the **Variables** tab and add:
   - `DB_PATH=/data/vehicles.db`
3. Trigger a manual redeploy from the Railway UI so the new volume is mounted
   and the env var is picked up.

After step 3, any vehicles you log will survive future deploys.

## Endpoints

All endpoints are on the same base URL as the existing `/lookup`:
`https://web-production-7a1f0.up.railway.app`

| Method | Path             | Purpose                                  |
|--------|------------------|------------------------------------------|
| POST   | `/vehicles`      | Create or upsert a vehicle (201)         |
| GET    | `/vehicles`      | List all vehicles, newest first          |
| GET    | `/vehicles/{ref}`| Fetch one vehicle (404 if missing)       |
| PUT    | `/vehicles/{ref}`| Update a vehicle (404 if missing)        |
| DELETE | `/vehicles/{ref}`| Delete a vehicle (204, 404 if missing)   |

Refs are normalised to uppercase + `VEH` prefix (so `47`, `veh47`, and `VEH47`
all resolve to the same record `VEH47`).

Paint codes are auto-resolved: send `paint_code: "LC9X"` and the response
will include `paint_name: "Reflex Silver"` (unless you supplied your own
`paint_name`, which is preserved verbatim).
