# Spec: Google Business Profile hero-photo step for Update Vehicles.bat

## Goal
When Naveed runs `vehicle-imports/Update Vehicles.bat`, after the website step, post the
**MAIN 1** image of every new vehicle to the Pulled Apart Google Business Profile (GBP).
One hero photo per car. No duplicates. Never breaks the Parts Logger or website steps.

## Context (read before coding)
- `vehicle-imports/Update Vehicles.bat` runs: Parts Logger import -> `update_website.py` -> `update_stock_sheets.py`.
- `update_website.py` already has `has_main1()`, `find_photo_folder()`, `get_main_files()` and the
  photo root `C:\Users\ACER\Desktop\Pulled Apart Ltd\Parts Photos` (one folder per car, `VEHn`,
  hero image named `Main 1.jpg` / `MAIN 1.jpg` / `Main 1.jpeg` — match case-insensitively, ignore spaces).
  Reuse these helpers (import them or move them to a small shared module) — do not copy-paste.
- GBP IDs: account/location path `18336683313410948860`, listing CID/fid `1457236246160685913`.
- As of 12 Nov 2026, VEH1–VEH175 are already handled on GBP (VEH161–175 uploaded manually that day).

## Deliverables
1. `vehicle-imports/update_gbp.py`
2. `vehicle-imports/gbp-posted.txt` — state file, one VEH ref per line. Seed it with `BASELINE=VEH175`
   on the first line (meaning: everything numbered <= 175 counts as done).
3. A new step in `Update Vehicles.bat` after the website step and before the stock-sheet step,
   in the same style as the website step (`if exist`, `errorlevel` only prints a warning).
4. Tests in `tests/test_update_gbp.py` (pytest).
5. A short section added to `vehicle-imports/README` or the SOP notes: first-time login + how it works.

## Behaviour of update_gbp.py
- **Candidates** = VEH folders with a number > baseline, not listed in gbp-posted.txt, that have a MAIN 1.
  Folders with no MAIN 1 are skipped silently and picked up on a later run.
  Prefer only cars that are **published on the live website** (reuse the server-ref check in
  update_website.py, `get_server_refs(LIVE_PATH)`); if the live check fails, fall back to the folder rule and say so.
- Print the candidate list, then ask once: `Post N hero photo(s) to Google Business Profile? (Y/N)`.
  Anything but Y = skip, exit 0.
- **Uploader is pluggable**: `UPLOADER = "playwright"` now; `"api"` later (see below). Same interface:
  `upload(paths: list[str]) -> list[str]` returning the refs confirmed uploaded.
- Only append a ref to gbp-posted.txt **after** the upload is confirmed (see verification).
- Output a one-line summary, e.g. `Google: added VEH176, VEH177 (pending Google review)`.
- Log to `vehicle-imports/logs/<stamp>_gbp.log`.
- Flags: `--dry-run` (list candidates, touch nothing), `--login` (open the browser profile so Naveed
  can sign into Google once), `--probe` (open the upload dialog, find the file input, close WITHOUT uploading).
- Any exception: log it, print `** Google photo step failed - will retry next run`, exit 1. Never raise
  into the .bat in a way that skips the stock-sheet step.

## Playwright uploader (build now)
- Python `playwright`, **headed**, persistent context with its own profile dir:
  `%LOCALAPPDATA%\PulledApart\gbp-browser-profile`, `channel="chrome"`,
  `ignore_default_args=["--enable-automation"]`. First run needs `--login` (Naveed signs in as nmabmm11@gmail.com).
  If not signed in during a normal run: print a clear message telling him to run `update_gbp.py --login`, exit 1.
- Open `https://business.google.com/n/18336683313410948860/photos/add?fid=1457236246160685913`.
  It redirects to a Google Search page with an "Add photos and videos" dialog inside an **iframe**.
- **Gotcha found on 12 Nov:** the top-level search page URL ALSO contains `/photos/add` (in the `#mpd=` fragment)
  and has its own hidden `input[type=file]` (Google Lens). Setting files there puts them in the search bar, not GBP.
  Select the frame whose URL **starts with** `https://www.google.com/local/business/` and contains `/photos/add`,
  then `set_input_files` on that frame's `input[type=file]`. Never use the main frame's input.
- **Verification:** after upload the dialog closes into the media tool (frame URL contains `/photos/mediatool`).
  Count occurrences of `PENDING` in that frame's text before/after (wait up to ~60s, polling). Confirm the count
  rose by N. If it can't be confirmed, do NOT mark as posted; report it.
- Upload in one batch (all candidate files in a single `set_input_files`), max 20 per run; if more, do the rest next run.

## API uploader (stub now, implement after Google approves API access)
- Interface only + clear `NotImplementedError` message for now.
- Later: OAuth2 (scope `https://www.googleapis.com/auth/business.manage`), token stored outside the repo,
  `POST https://mybusiness.googleapis.com/v4/accounts/{account}/locations/{location}/media` with
  `mediaFormat: PHOTO`, `locationAssociation.category: ADDITIONAL`, `sourceUrl` = the car's MAIN 1 URL on
  pulledapart.co.uk (already public after the website step).

## Tests (must pass)
- Candidate selection: baseline respected, posted list respected, no-MAIN-1 skipped, case/space variants of "Main 1".
- State file: only confirmed refs appended; file created/seeded if missing.
- Frame selection: given fake frames (main frame URL with `#mpd=.../photos/add` + real GBP frame), picks the GBP frame.
- `--dry-run` writes nothing.

## Acceptance
- `pytest tests/test_update_gbp.py` green.
- `python vehicle-imports/update_gbp.py --dry-run` prints "no new cars" (everything <= VEH175 is done).
- `python vehicle-imports/update_gbp.py --probe` finds the correct iframe and file input, uploads nothing.
- The .bat runs end to end with an empty inbox without errors.
- Do NOT upload any real photo during testing — the first real upload happens on the next real new car.
