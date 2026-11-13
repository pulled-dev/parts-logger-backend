# vehicle-imports

`Update Vehicles.bat` runs, in order: Parts Logger import -> `update_website.py`
-> `update_gbp.py` -> `update_stock_sheets.py`. A failure in the website or
Google step only prints a warning; the later steps still run.

## Google Business Profile hero photos (`update_gbp.py`)

### First time only: sign in to Google

Open a Command Prompt in this folder and run:

    py -3 update_gbp.py --login

An ordinary Chrome window opens (nothing is attached to it, so Google allows the
sign-in). Sign in as **nmabmm11@gmail.com**, then close that Chrome window. The sign-in is kept in its own browser profile at
`%LOCALAPPDATA%\PulledApart\gbp-browser-profile` (not your normal Chrome), so
you only do this again if Google signs you out — the step will tell you.

### How it works

- After the website step, it looks for cars in `Parts Photos` numbered above the
  baseline in `gbp-posted.txt` that are not listed there yet, have a `Main 1`
  photo, and are published on pulledapart.co.uk (if the live check fails it
  uses the photo folders only and says so). Cars without a Main 1 are picked up
  on a later run.
- It lists them and asks `Post N hero photo(s) to Google Business Profile? (Y/N)`.
  Anything but Y skips; they are offered again next run.
- It opens Chrome, counts the photos in the Google media tool, uploads every
  Main 1 in one go (max 20 a run), then reloads the media tool, scrolls it until no more photos load, and counts again.
  (Google shows no "PENDING" label there - new photos just sit first in the grid
  with no view count until reviewed - so it counts photos.) Only if the count
  rose by exactly the number uploaded are the cars added to `gbp-posted.txt`;
  anything else marks nothing and prints
  `** Google upload not confirmed - check the profile manually`. Summary: `Google: added VEH176 (pending Google review)`.
- Log: `logs/<date>_gbp.log`.

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

    py -3 update_gbp.py --dry-run   # show which cars would be posted, change nothing
    py -3 update_gbp.py --probe     # open the upload dialog, check it can find it, upload nothing
    py -3 update_gbp.py --pending   # print photo count + newest awaiting review, upload nothing
    py -3 update_gbp.py --clear-unconfirmed VEH176   # release an unconfirmed car for retry

`UPLOADER = "api"` in `update_gbp.py` is reserved for the Business Profile API
once Google approves access; it is not built yet.
