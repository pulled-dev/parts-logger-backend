# vehicle-imports

`Update Vehicles.bat` runs, in order: Parts Logger import -> `update_website.py`
-> `update_gbp.py` -> `update_stock_sheets.py`. A failure in the website or
Google step only prints a warning; the later steps still run.

## Google Business Profile hero photos (`update_gbp.py`)

Since 13 Nov 2026 it uses the **Google Business Profile API** (`UPLOADER = "api"`).
Access approved by Google on 13 Nov 2026 (case 8-2980000041381) for Cloud project
**Pulled Apart GBP** (`pulled-apart-gbp`, number 630683302520, owner nmabmm11@gmail.com).
No browser is involved when a photo is posted.

### First time only (or if it says "sign-in missing or expired")

Double-click **`Google Sign-in.bat`** in this folder. A browser opens: sign in as
**nmabmm11@gmail.com** and click **Allow** (if Google says the app is unverified,
click Advanced -> Go to Pulled Apart GBP Photos). It then finds the Pulled Apart
location and runs a read-only check.

Secrets live outside the repo in `%LOCALAPPDATA%\PulledApart`:
`gbp-oauth-client.json` (OAuth desktop client), `gbp-token.json` (sign-in),
`gbp-api.json` (account + location IDs). Never commit these.

### How it works

- After the website step, it looks for cars in `Parts Photos` numbered above the
  baseline in `gbp-posted.txt` that are not listed there yet, have a `Main 1`
  photo, and are published on pulledapart.co.uk (if the live check fails it
  uses the photo folders only and says so). Cars without a Main 1 are picked up
  on a later run.
- It lists them and asks `Post N hero photo(s) to Google Business Profile? (Y/N)`.
  Anything but Y skips; they are offered again next run.
- Each Main 1 (max 20 a run) is uploaded on its own: start upload -> send the
  photo -> create the media item (category "Additional"). A car is added to
  `gbp-posted.txt` only when Google returns the new photo's ID.
- If a car fails cleanly (Google said no, or it failed before the photo was
  created) nothing is recorded and it is offered again next run:
  `** Not posted, will retry next run: VEH177`.
- Only if the final "create" call gets no clear answer (connection dropped,
  Google 5xx) is the car marked `UNCONFIRMED` - see below.
- Summary: `Google: added VEH176 (pending Google review)`. Log: `logs/<date>_gbp.log`.

### `gbp-posted.txt`

First line `BASELINE=VEH175` = every car up to VEH175 is done (as of 12 Nov 2026).
Each line after is a car posted by the script. To stop a car ever being posted,
add its ref on its own line. To re-post one, remove its line.

`UNCONFIRMED VEH176` = the photo was sent to Google but the script couldn't
confirm it landed. It is **never retried automatically** (a blind retry could put
a duplicate photo on the public profile), and every run lists it under
"Needs a manual check on the Google profile". Look at the profile, then:

- photo missing -> `py -3 update_gbp.py --clear-unconfirmed VEH176` (offered again next run)
- photo there -> change the line to just `VEH176`

### Other commands

    py -3 update_gbp.py --dry-run     # show which cars would be posted, change nothing
    py -3 update_gbp.py --api-check   # prove sign-in + location, upload nothing
    py -3 update_gbp.py --authorize   # same as Google Sign-in.bat (first half)
    py -3 update_gbp.py --clear-unconfirmed VEH176   # release an unconfirmed car for retry

### Fallback: browser route

The old Playwright/Chrome uploader is still in the file. To use it, set
`UPLOADER = "playwright"` in `update_gbp.py`, then `py -3 update_gbp.py --login`
once. `--probe` and `--pending` only apply to that route. It breaks whenever
Google changes the page, so only use it if the API is down.
