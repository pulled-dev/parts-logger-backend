#!/usr/bin/env python3
"""
Pulled Apart — Google Business Profile hero-photo step.

Posts the MAIN 1 photo of every new vehicle to the Pulled Apart Google
Business Profile. Called from Update Vehicles.bat after the website step.

Usage:
    python update_gbp.py             # normal run (asks before posting)
    python update_gbp.py --dry-run   # list candidates, touch nothing
    python update_gbp.py --authorize # one-time: sign the API into Google (API route)
    python update_gbp.py --api-check # prove API sign-in + location, upload nothing
    python update_gbp.py --login     # one-time: sign the browser profile into Google (browser route)
    python update_gbp.py --probe     # open the upload dialog, find the file input, upload nothing
    python update_gbp.py --pending   # open the media tool, print photo + pending counts, upload nothing
    python update_gbp.py --clear-unconfirmed VEH176   # release an unconfirmed car for retry

State: gbp-posted.txt — first line BASELINE=VEHn (everything <= n is done),
then one ref per line, appended only after Google confirms the upload.
"UNCONFIRMED VEHn" = an upload was attempted but not confirmed. Never retried
automatically (a blind retry could duplicate the photo on the public profile);
check the profile, then --clear-unconfirmed VEHn to retry it.
"""

import datetime
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_website as uw  # noqa: E402  (reuse photo + live-site helpers)

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

UPLOADER = "api"  # Business Profile API (approved 13 Nov 2026). "playwright" = old browser route

GBP_ACCOUNT = "18336683313410948860"
GBP_FID = "1457236246160685913"
ADD_PHOTOS_URL = f"https://business.google.com/n/{GBP_ACCOUNT}/photos/add?fid={GBP_FID}"
MEDIATOOL_URL = f"https://business.google.com/n/{GBP_ACCOUNT}/photos?fid={GBP_FID}"  # redirects into the /photos/mediatool iframe
GBP_FRAME_PREFIX = "https://www.google.com/local/business/"

STATE_FILE = os.path.join(uw.IMPORTS_DIR, "gbp-posted.txt")
DEFAULT_BASELINE = "VEH175"
MAX_PER_RUN = 20
VERIFY_TIMEOUT = 60  # seconds: wait for Google to show the new photos
LOAD_TIMEOUT = 45    # seconds: wait for the media tool to load

PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PulledApart", "gbp-browser-profile",
)

# Ordinary Chrome, started by us with a debugging port and attached over CDP.
# (Chrome launched *by* Playwright gets "This browser or app may not be secure"
# from Google sign-in.)
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_CANDIDATES = [
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]

FAIL_MSG = "** Google photo step failed - will retry next run"
UNCONFIRMED_MSG = "** Google upload not confirmed - check the profile manually"

# ═══════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════

_log_lines = []
_stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def log(msg=""):
    print(msg, flush=True)
    _log_lines.append(msg)


def save_log():
    os.makedirs(uw.LOGS_DIR, exist_ok=True)
    path = os.path.join(uw.LOGS_DIR, f"{_stamp}_gbp.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(_log_lines))
    return path


# ═══════════════════════════════════════════════════════════════════
# State file
# ═══════════════════════════════════════════════════════════════════

def ref_number(ref):
    """'VEH176' -> 176; anything else -> None."""
    m = re.fullmatch(r"VEH(\d+)", (ref or "").strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


def ensure_state(path=None):
    """Create the state file seeded with the baseline if it is missing."""
    path = path or STATE_FILE
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"BASELINE={DEFAULT_BASELINE}\n")


UNCONFIRMED = "UNCONFIRMED "


def _parse_state(path):
    baseline = ref_number(DEFAULT_BASELINE)
    posted, unconfirmed = set(), set()
    if not os.path.exists(path):
        return baseline, posted, unconfirmed
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().upper()
            if not line or line.startswith("#"):
                continue
            if line.startswith("BASELINE="):
                n = ref_number(line.split("=", 1)[1])
                if n is not None:
                    baseline = n
            elif line.startswith(UNCONFIRMED):
                unconfirmed.add(line[len(UNCONFIRMED):].strip())
            else:
                posted.add(line)
    return baseline, posted, unconfirmed


def load_state(path=None):
    """Return (baseline_number, posted_refs). Missing file -> default baseline."""
    baseline, posted, _ = _parse_state(path or STATE_FILE)
    return baseline, posted


def load_unconfirmed(path=None):
    """Refs whose upload was attempted but never confirmed."""
    return _parse_state(path or STATE_FILE)[2]


def append_posted(refs, path=None):
    """Record confirmed refs. Creates/seeds the file first if needed."""
    path = path or STATE_FILE
    ensure_state(path)
    if not refs:
        return
    with open(path, "a", encoding="utf-8") as f:
        for ref in refs:
            f.write(ref.upper() + "\n")


def append_unconfirmed(refs, path=None):
    """Record refs whose upload was attempted but not confirmed."""
    path = path or STATE_FILE
    ensure_state(path)
    with open(path, "a", encoding="utf-8") as f:
        for ref in refs:
            f.write(UNCONFIRMED + ref.upper() + "\n")


def clear_unconfirmed(ref, path=None):
    """Remove the UNCONFIRMED line for ref. True if one was removed."""
    path = path or STATE_FILE
    target = UNCONFIRMED + ref.strip().upper()
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    kept = [ln for ln in lines if ln.strip().upper() != target]
    if len(kept) == len(lines):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    return True


# ═══════════════════════════════════════════════════════════════════
# Candidates
# ═══════════════════════════════════════════════════════════════════

def photo_refs():
    """VEH refs that have a folder in Parts Photos, upper-cased."""
    if not os.path.isdir(uw.PHOTOS_DIR):
        return []
    refs = set()
    for d in os.listdir(uw.PHOTOS_DIR):
        if ref_number(d) is not None and os.path.isdir(os.path.join(uw.PHOTOS_DIR, d)):
            refs.add(d.upper())
    return sorted(refs, key=ref_number)


def main1_path(ref):
    """Path to the car's MAIN 1 image, or None."""
    ok, folder = uw.has_main1(ref)
    if not ok:
        return None
    return uw.get_main_files(folder).get(1)


def find_candidates(baseline, posted, live_refs=None):
    """[(ref, main1_path)] for cars > baseline, not posted (or unconfirmed —
    pass those in `posted` too), with a MAIN 1.

    live_refs: {REF: post_status} from the live site; when given, only
    published cars count. None = live check unavailable, use folders only.
    """
    out = []
    for ref in photo_refs():
        if ref_number(ref) <= baseline or ref in posted:
            continue
        if live_refs is not None and live_refs.get(ref) != "publish":
            continue
        path = main1_path(ref)
        if path:
            out.append((ref, path))
    return out


def ref_for_path(path):
    """The VEH ref a MAIN 1 path belongs to (its folder name)."""
    return os.path.basename(os.path.dirname(path)).upper()


# ═══════════════════════════════════════════════════════════════════
# Uploaders — same interface: upload(paths) -> refs confirmed uploaded
# ═══════════════════════════════════════════════════════════════════

class NotSignedIn(Exception):
    pass


def pick_gbp_frame(frames):
    """The GBP 'Add photos' iframe. Never the top-level search page, whose
    URL also contains /photos/add (in #mpd=) and has a Google Lens file input."""
    for fr in frames:
        url = fr.url or ""
        if url.startswith(GBP_FRAME_PREFIX) and "/photos/add" in url:
            return fr
    return None


def pick_mediatool_frame(frames):
    for fr in frames:
        url = fr.url or ""
        if url.startswith(GBP_FRAME_PREFIX) and "/photos/mediatool" in url:
            return fr
    return None


# Google shows no "PENDING" label in the media tool (checked 13 Nov 2026):
# new photos just sit first in the grid with no view count until reviewed.
# So verification counts photos: one "Delete photo N" button per photo
# (cover and logo are "Delete cover photo" / "Delete logo photo", not counted).
# Scroll every scrollable box in the media tool to the bottom, so a lazily
# loaded grid fetches its next page of photos.
_SCROLL_JS = """() => {
    for (const el of document.querySelectorAll('*')) {
        if (el.scrollHeight > el.clientHeight + 20) el.scrollTop = el.scrollHeight;
    }
    window.scrollTo(0, document.body.scrollHeight);
}"""

_TILES_JS = """() => [...document.querySelectorAll('[aria-label^="Delete photo "]')].map(b => {
    let t = b;
    for (let i = 0; i < 6 && t.parentElement; i++) { t = t.parentElement; if (t.querySelector('img')) break; }
    return t.innerText.indexOf('views') !== -1;
})"""


def read_tiles(frame):
    """[has_view_count, ...] per photo in grid order (newest first), or None."""
    try:
        return frame.evaluate(_TILES_JS)
    except Exception:
        return None


def newest_without_views(tiles):
    """Photos at the top of the grid with no view count yet = awaiting review."""
    n = 0
    for has_views in tiles:
        if has_views:
            break
        n += 1
    return n


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError("Google Chrome not found. Looked in: " + "; ".join(CHROME_CANDIDATES))


def chrome_args(chrome, profile_dir, url=None):
    """Command line for an ordinary Chrome on the GBP profile with CDP enabled."""
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if url:
        args.append(url)
    return args


def cdp_ready(timeout=1.0):
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_chrome(profile_dir, url=None, wait=20):
    """Start Chrome unless one is already listening on the CDP port.
    Returns True if we started it (so we should close it afterwards)."""
    if cdp_ready():
        return False
    os.makedirs(profile_dir, exist_ok=True)
    subprocess.Popen(chrome_args(find_chrome(), profile_dir, url),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_ready():
            return True
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome did not open its debugging port {CDP_PORT}. If a Chrome window "
        "from a previous Google step is still open, close it and run again.")


class PlaywrightUploader:
    def __init__(self, profile_dir=PROFILE_DIR):
        self.profile_dir = profile_dir
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self._started = False
        self.attempted = False  # True once files have been handed to Google

    # ── browser lifecycle ──
    def open(self):
        from playwright.sync_api import sync_playwright
        self._started = start_chrome(self.profile_dir)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(CDP_URL)
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self.ctx.new_page()
        return self

    def close(self):
        try:
            if self.page is not None and not self._started:
                self.page.close()  # Chrome was already open: leave it, just tidy our tab
            if self.browser is not None and self._started:
                # Graceful quit so the sign-in cookies are flushed to the profile.
                self.browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ── steps ──
    @classmethod
    def login(cls, profile_dir=PROFILE_DIR):
        """Open plain Chrome on the profile for a manual Google sign-in.
        Nothing attaches to it while Naveed signs in."""
        if cdp_ready():
            raise RuntimeError("A Google-step Chrome is already open - close it and run --login again.")
        os.makedirs(profile_dir, exist_ok=True)
        proc = subprocess.Popen(chrome_args(find_chrome(), profile_dir, "https://accounts.google.com/"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("  A Chrome window is open. Sign in as nmabmm11@gmail.com,")
        log("  then CLOSE that Chrome window and come back here.")
        proc.wait()  # Chrome exits when the window is closed (cookies saved)

    def _signed_out(self):
        return "accounts.google.com" in (self.page.url or "")

    def open_add_dialog(self, timeout=45):
        """Navigate to the add-photos URL and return the GBP iframe."""
        self.page.goto(ADD_PHOTOS_URL, wait_until="domcontentloaded")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._signed_out():
                raise NotSignedIn()
            fr = pick_gbp_frame(self.page.frames)
            if fr is not None:
                try:
                    fr.wait_for_selector("input[type=file]", state="attached", timeout=5000)
                    return fr
                except Exception:
                    pass
            self.page.wait_for_timeout(1000)
        if self._signed_out():
            raise NotSignedIn()
        urls = [f.url for f in self.page.frames]
        raise RuntimeError(f"GBP 'Add photos' iframe not found. Frames: {urls}")

    def probe(self):
        """Open the add-photos dialog and find its file input. Uploads nothing."""
        fr = self.open_add_dialog()
        inputs = fr.locator("input[type=file]")
        n = inputs.count()
        log(f"  GBP frame: {fr.url[:120]}")
        log(f"  File inputs in GBP frame: {n}")
        if n:
            log(f"    accept={inputs.first.get_attribute('accept')!r} "
                f"multiple={inputs.first.get_attribute('multiple') is not None}")
        main_inputs = self.page.main_frame.locator("input[type=file]").count()
        log(f"  (main-frame file inputs, deliberately NOT used: {main_inputs})")
        return n > 0

    def open_mediatool(self, timeout=LOAD_TIMEOUT):
        """Load the media tool fresh and return its iframe."""
        self.page.goto(MEDIATOOL_URL, wait_until="domcontentloaded")
        fr = self._wait_mediatool_frame(timeout)
        if fr is None:
            if self._signed_out():
                raise NotSignedIn()
            urls = [f.url for f in self.page.frames]
            raise RuntimeError(f"GBP media tool iframe not found. Frames: {urls}")
        return fr

    def _wait_mediatool_frame(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._signed_out():
                raise NotSignedIn()
            fr = pick_mediatool_frame(self.page.frames)
            if fr is not None:
                return fr
            self.page.wait_for_timeout(1000)
        return None

    def mediatool_tiles(self):
        """Photo tiles from a freshly loaded media tool. Scrolls the grid to the
        bottom before every read (lazy loading) and returns once the photo count
        has held steady (and non-zero) for 3 scroll+read rounds. Raises if not."""
        fr = self.open_mediatool()
        deadline = time.time() + LOAD_TIMEOUT
        reads = []
        while time.time() < deadline:
            try:
                fr.evaluate(_SCROLL_JS)
            except Exception:
                pass
            self.page.wait_for_timeout(2000)
            tiles = read_tiles(fr)
            reads.append(len(tiles) if tiles is not None else None)
            if len(reads) >= 3 and reads[-1] and reads[-3:] == [reads[-1]] * 3:
                return tiles
        raise RuntimeError(f"Media tool photo count never settled: {reads}")

    def mediatool_count(self):
        return len(self.mediatool_tiles())

    def send_files(self, paths):
        """Put the files into the GBP dialog's input, then wait for the dialog
        to close into the media tool (= Google has taken the upload)."""
        fr = self.open_add_dialog()
        self.attempted = True  # from here on, a photo may reach the profile
        fr.locator("input[type=file]").first.set_input_files(list(paths))
        log(f"  Sent {len(paths)} file(s) to Google, waiting for confirmation...")
        if self._wait_mediatool_frame(VERIFY_TIMEOUT) is None:
            log("  ** Upload dialog did not close into the media tool.")

    def upload(self, paths):
        if not paths:
            return []
        n = len(paths)
        before = self.mediatool_count()  # raises -> nothing uploaded
        log(f"  Media tool photos before upload: {before}")
        self.send_files(paths)

        deadline = time.time() + VERIFY_TIMEOUT
        after = None
        while True:
            after = self.mediatool_count()
            if after >= before + n or time.time() >= deadline:
                break
            self.page.wait_for_timeout(5000)
        log(f"  Media tool photos after upload: {after} (expected exactly {before + n})")
        if upload_confirmed(before, after, n):
            return [ref_for_path(p) for p in paths]
        return []


def upload_confirmed(before, after, n):
    """Only an exact rise of n counts. Unknown counts, no change, or any other
    rise (old photos appearing, partial upload) is not a confirmation."""
    if before is None or after is None:
        return False
    return after - before == n


# ── Business Profile API uploader ─────────────────────────────────────
# Approved 13 Nov 2026 (case 8-2980000041381, Cloud project pulled-apart-gbp /
# 630683302520, 300 QPM). Stdlib only: nothing extra to install on the PC.
# Secrets live outside the repo in %LOCALAPPDATA%\PulledApart:
#   gbp-oauth-client.json  OAuth "Desktop app" client downloaded from Cloud Console
#   gbp-token.json         refresh token, written by --authorize
#   gbp-api.json           {"account": "accounts/..", "location": "locations/.."}

import http.server
import json
import secrets
import urllib.error
import urllib.parse
import webbrowser

API_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PulledApart")
CLIENT_FILE = os.path.join(API_DIR, "gbp-oauth-client.json")
TOKEN_FILE = os.path.join(API_DIR, "gbp-token.json")
API_CONFIG_FILE = os.path.join(API_DIR, "gbp-api.json")

SCOPE = "https://www.googleapis.com/auth/business.manage"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
MEDIA_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/media"
BYTES_URL = "https://mybusiness.googleapis.com/upload/v1/media/{resource}?upload_type=media"

MAX_PHOTO_BYTES = 5 * 1024 * 1024   # Google's limit per photo
MIN_PHOTO_BYTES = 10 * 1024
HTTP_TIMEOUT = 60
REAUTH_MSG = "** Google API sign-in missing or expired. Run once:  py -3 update_gbp.py --authorize"


class ApiAuthError(Exception):
    """Sign-in missing/revoked - needs --authorize."""


class ApiError(Exception):
    def __init__(self, msg, status=None, ambiguous=False):
        super().__init__(msg)
        self.status = status
        self.ambiguous = ambiguous  # True = the request may have taken effect


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_client(path=None):
    path = path or CLIENT_FILE
    if not os.path.exists(path):
        raise ApiAuthError(f"OAuth client file not found: {path}")
    data = _read_json(path)
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id") or not c.get("client_secret"):
        raise ApiAuthError(f"OAuth client file has no client_id/client_secret: {path}")
    return c["client_id"], c["client_secret"]


def http_json(url, method="GET", body=None, token=None, data=None,
              content_type="application/json", timeout=HTTP_TIMEOUT, opener=None):
    """One HTTP call -> parsed JSON (or {}). Raises ApiError.
    ambiguous=True only when no HTTP status came back (timeout / dropped
    connection) or the server errored (5xx): the request may have worked."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        if e.code == 401:
            raise ApiAuthError(f"401 from {url}: {detail}")
        raise ApiError(f"HTTP {e.code} from {url}: {detail}", status=e.code,
                       ambiguous=e.code >= 500)
    except Exception as e:  # URLError, timeout, reset
        raise ApiError(f"No response from {url}: {e}", ambiguous=True)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}


def refresh_access_token(client_file=None, token_file=None, post=None):
    client_id, client_secret = load_client(client_file)
    token_file = token_file or TOKEN_FILE
    if not os.path.exists(token_file):
        raise ApiAuthError("No saved Google API sign-in.")
    refresh = _read_json(token_file).get("refresh_token")
    if not refresh:
        raise ApiAuthError("Saved Google API sign-in has no refresh token.")
    form = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    try:
        res = (post or http_json)(TOKEN_URL, method="POST", data=form,
                                  content_type="application/x-www-form-urlencoded")
    except ApiError as e:
        if e.status in (400, 401):  # invalid_grant = revoked/expired
            raise ApiAuthError(str(e))
        raise
    tok = res.get("access_token")
    if not tok:
        raise ApiAuthError(f"Token refresh returned no access token: {res}")
    return tok


def authorize(client_file=None, token_file=None, timeout=300):
    """One-time browser sign-in (loopback flow). Saves the refresh token."""
    client_id, client_secret = load_client(client_file)
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and ("code" in q or "error" in q):
                got.update({k: v[0] for k, v in q.items()})
                msg = "Google sign-in complete - you can close this tab." if "code" in q \
                    else "Google sign-in failed: " + q["error"][0]
            else:
                msg = "Waiting for Google sign-in..."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    srv.timeout = 2
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "login_hint": "nmabmm11@gmail.com",
    })
    log("  Opening Google sign-in in your browser. Sign in as nmabmm11@gmail.com")
    log("  and allow access. If no browser opens, paste this link into Chrome:")
    log(f"  {url}")
    webbrowser.open(url)
    deadline = time.time() + timeout
    while not got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise ApiAuthError(f"Google sign-in not completed ({got.get('error', 'timed out')}).")
    form = urllib.parse.urlencode({
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }).encode()
    res = http_json(TOKEN_URL, method="POST", data=form,
                    content_type="application/x-www-form-urlencoded")
    if not res.get("refresh_token"):
        raise ApiAuthError(f"Google returned no refresh token: {res}")
    _write_json(token_file or TOKEN_FILE,
                {"refresh_token": res["refresh_token"], "scope": res.get("scope"),
                 "saved": datetime.datetime.now().isoformat(timespec="seconds")})
    return res["access_token"]


def discover_location(token, call=None):
    """Find the single Pulled Apart location. Returns (account, location, title)."""
    call = call or http_json
    accounts = call(ACCOUNTS_URL, token=token).get("accounts", [])
    found = []
    for acc in accounts:
        url = LOCATIONS_URL.format(account=acc["name"]) + "?readMask=name,title&pageSize=100"
        for loc in call(url, token=token).get("locations", []):
            found.append((acc["name"], loc["name"], loc.get("title", "")))
    pa = [f for f in found if "pulled apart" in f[2].lower()]
    pick = pa if pa else found
    if len(pick) != 1:
        raise ApiError(f"Expected one Pulled Apart location, found: {found}")
    return pick[0]


def load_api_config(path=None):
    path = path or API_CONFIG_FILE
    if not os.path.exists(path):
        raise ApiAuthError(f"API location not set up ({path} missing).")
    cfg = _read_json(path)
    if not cfg.get("account") or not cfg.get("location"):
        raise ApiAuthError(f"{path} must contain account and location.")
    return cfg["account"], cfg["location"]


def photo_problem(path):
    """Why Google would reject this file, or None."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return f"cannot read file ({e})"
    if size > MAX_PHOTO_BYTES:
        return f"{size / 1048576:.1f} MB - Google's limit is 5 MB"
    if size < MIN_PHOTO_BYTES:
        return f"{size} bytes - Google's minimum is 10 KB"
    if not path.lower().endswith((".jpg", ".jpeg", ".png")):
        return "not a JPG or PNG"
    return None


def shrink_photo(path):
    """Bytes of the photo, re-saved as JPEG under 5 MB if it is too big.
    Uses Pillow when installed; returns None if it can't get under the limit."""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) <= MAX_PHOTO_BYTES:
        return raw
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return None
    img = Image.open(BytesIO(raw)).convert("RGB")
    for edge, q in ((4000, 90), (3000, 88), (2400, 85), (2000, 82)):
        im = img.copy()
        im.thumbnail((edge, edge))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=q)
        if buf.tell() <= MAX_PHOTO_BYTES:
            return buf.getvalue()
    return None


class ApiUploader:
    """Posts each photo with the Business Profile API: startUpload -> send
    bytes -> create media (category ADDITIONAL). A car counts as posted only
    when Google returns the new media item. Failures before the create call
    can't have created anything, so those cars are simply retried next run;
    only a create call with no clear answer is 'ambiguous' (needs a check)."""

    per_item = True

    def __init__(self, call=None, sleep=time.sleep):
        self.call = call or http_json
        self.sleep = sleep
        self.token = None
        self.account = self.location = None
        self.attempted = False
        self.not_uploaded = set()   # refs definitely not on the profile
        self.ambiguous = set()      # refs that may or may not be on the profile
        self.results = {}           # ref -> media name / error text
        self.confirmed = []         # refs Google returned a media item for

    def __enter__(self):
        self.account, self.location = load_api_config()
        self.token = refresh_access_token()
        return self

    def __exit__(self, *exc):
        pass

    def _media_url(self):
        return MEDIA_URL.format(account=self.account, location=self.location)

    def _safe(self, fn, tries=3):
        """Retry steps that can't create a photo (start upload, send bytes)."""
        for i in range(tries):
            try:
                return fn()
            except ApiError as e:
                if i == tries - 1 or (e.status and e.status < 500 and e.status != 429):
                    raise
                self.sleep(5 * (i + 1))

    def upload_one(self, path):
        ref = ref_for_path(path)
        problem = photo_problem(path)
        data = None
        if problem and "5 MB" in problem:
            data = shrink_photo(path)
            if data is None:
                raise ApiError(f"{ref}: {problem} and could not be shrunk", status=0)
        elif problem:
            raise ApiError(f"{ref}: {problem}", status=0)
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        start = self._safe(lambda: self.call(self._media_url() + ":startUpload",
                                             method="POST", body={}, token=self.token))
        resource = start.get("resourceName")
        if not resource:
            raise ApiError(f"{ref}: startUpload gave no resourceName: {start}", status=0)
        self._safe(lambda: self.call(BYTES_URL.format(resource=urllib.parse.quote(resource, safe="")),
                                     method="POST", data=data, token=self.token,
                                     content_type="application/octet-stream"))
        self.attempted = True
        media = self.call(self._media_url(), method="POST", token=self.token, body={
            "mediaFormat": "PHOTO",
            "locationAssociation": {"category": "ADDITIONAL"},
            "dataRef": {"resourceName": resource},
        })
        if not media.get("name"):
            raise ApiError(f"{ref}: create returned no media name: {media}", ambiguous=True)
        return media["name"]

    def upload(self, paths):
        confirmed = []
        for p in paths:
            ref = ref_for_path(p)
            try:
                self.results[ref] = self.upload_one(p)
                confirmed.append(ref)
                self.confirmed.append(ref)
                log(f"    {ref}: posted ({self.results[ref].rsplit('/', 1)[-1]})")
            except ApiAuthError:
                for q in paths[paths.index(p):]:
                    self.not_uploaded.add(ref_for_path(q))
                raise
            except ApiError as e:
                self.results[ref] = str(e)
                (self.ambiguous if e.ambiguous else self.not_uploaded).add(ref)
                log(f"    {ref}: {'NOT CONFIRMED' if e.ambiguous else 'failed'} - {e}")
            self.sleep(1)
        return confirmed

    def media_summary(self):
        res = self.call(self._media_url() + "?pageSize=1", token=self.token)
        return res.get("totalMediaItemCount", 0)


def api_setup(argv):
    """--authorize: browser sign-in, then find and save the location."""
    authorize()
    token = refresh_access_token()
    account, location, title = discover_location(token)
    _write_json(API_CONFIG_FILE, {"account": account, "location": location, "title": title})
    log(f"  Signed in. Location: {title} ({account}/{location})")
    log(f"  Saved to {API_DIR}")
    return 0


def api_check():
    """--api-check: prove sign-in + location + read access. Uploads nothing."""
    up = ApiUploader()
    with up:
        n = up.media_summary()
    log(f"  API OK - {up.account}/{up.location}, {n} photos/videos on the profile.")
    return 0


def make_uploader(kind=None):
    kind = kind or UPLOADER
    if kind == "playwright":
        return PlaywrightUploader()
    if kind == "api":
        return ApiUploader()
    raise ValueError(f"Unknown UPLOADER: {kind!r}")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def get_live_refs():
    """Live-site refs via update_website, or None if the check fails."""
    try:
        return uw.get_server_refs(uw.LIVE_PATH)
    except Exception:
        return None


def run(argv):
    dry_run = "--dry-run" in argv

    log("")
    log("================================================")
    log("  Pulled Apart - Google Business Profile photos")
    log("================================================")
    log("")

    if "--authorize" in argv:
        return api_setup(argv)

    if "--api-check" in argv:
        try:
            return api_check()
        except ApiAuthError as e:
            log(f"  {REAUTH_MSG}")
            log(f"  ({e})")
            return 1

    if "--login" in argv:
        PlaywrightUploader.login()
        log("  Browser profile saved. Run Update Vehicles.bat as normal from now on.")
        return 0

    if "--probe" in argv:
        log("  Probe: opening the upload dialog (nothing will be uploaded)...")
        try:
            with PlaywrightUploader() as up:
                ok = up.probe()
        except NotSignedIn:
            log("  ** Not signed in to Google. Run once:  python update_gbp.py --login")
            return 1
        log("  Probe OK - correct iframe and file input found." if ok
            else "  ** Probe: GBP frame found but no file input.")
        return 0 if ok else 1

    if "--clear-unconfirmed" in argv:
        i = argv.index("--clear-unconfirmed")
        ref = argv[i + 1].upper() if i + 1 < len(argv) else ""
        if ref_number(ref) is None:
            log("  Usage: update_gbp.py --clear-unconfirmed VEH176")
            return 1
        if clear_unconfirmed(ref):
            log(f"  {ref} released - it will be offered again on the next run.")
            return 0
        log(f"  {ref} is not marked UNCONFIRMED in gbp-posted.txt - nothing changed.")
        return 1

    if "--pending" in argv:
        log("  Reading the media tool (nothing will be uploaded)...")
        try:
            with PlaywrightUploader() as up:
                tiles = up.mediatool_tiles()
        except NotSignedIn:
            log("  ** Not signed in to Google. Run once:  python update_gbp.py --login")
            return 1
        log(f"  Photos on the profile: {len(tiles)}")
        log(f"  Newest photos with no views yet (pending Google review): {newest_without_views(tiles)}")
        return 0

    if not dry_run:
        ensure_state()
    baseline, posted = load_state()
    unconfirmed = load_unconfirmed()
    report_unconfirmed(unconfirmed)

    live_refs = get_live_refs()
    if live_refs is None:
        log("  (Live website check failed - using photo folders only.)")
    candidates = find_candidates(baseline, posted | unconfirmed, live_refs)

    if not candidates:
        log(f"  Google: no new cars (all up to VEH{baseline} plus "
            f"{len(posted)} logged are done).")
        return 0

    batch = candidates[:MAX_PER_RUN]
    log(f"  New hero photos ({len(candidates)}):")
    for ref, path in candidates:
        log(f"    {ref}: {path}")
    if len(candidates) > len(batch):
        log(f"  (Max {MAX_PER_RUN} per run - the other {len(candidates) - len(batch)} go next run.)")
    log("")

    if dry_run:
        log("  --dry-run: nothing posted, nothing written.")
        return 0

    print(f"  Post {len(batch)} hero photo(s) to Google Business Profile? (Y/N) ", end="", flush=True)
    try:
        answer = input().strip().upper()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    _log_lines.append(f"  Post {len(batch)} hero photo(s)? -> {answer!r}")
    if answer != "Y":
        log("  Skipped - Google not updated. They'll be offered again next run.")
        return 0

    batch_refs = [r for r, _ in batch]
    up = make_uploader()
    if getattr(up, "per_item", False):
        return run_per_item(up, batch, batch_refs)
    try:
        with up:
            confirmed = up.upload([p for _, p in batch])
    except Exception as e:
        if getattr(up, "attempted", False):
            # Files were handed to Google: they may be on the profile.
            log(f"  {UNCONFIRMED_MSG}")
            append_unconfirmed(batch_refs)
            report_unconfirmed(batch_refs)
        if isinstance(e, NotSignedIn):
            log("  ** Not signed in to Google. Run once:  python update_gbp.py --login")
            log("  ** (from the vehicle-imports folder), then run Update Vehicles.bat again.")
            return 1
        raise
    if sorted(confirmed) != sorted(batch_refs):
        # All or nothing, and never retried blind: a retry could duplicate photos.
        log(f"  {UNCONFIRMED_MSG}")
        append_unconfirmed(batch_refs)
        report_unconfirmed(batch_refs)
        return 1
    append_posted(confirmed)
    log(f"  Google: added {', '.join(confirmed)} (pending Google review)")
    return 0


def run_per_item(up, batch, batch_refs):
    """API route: Google answers per photo, so each car is recorded on its own.
    Posted -> logged; clean failure -> retried next run; no clear answer on the
    create call -> UNCONFIRMED (never retried blind, could duplicate)."""
    error = None
    try:
        with up:
            up.upload([p for _, p in batch])
    except ApiAuthError as e:
        error = e
    except Exception as e:  # recorded below, then re-raised
        error = e
    confirmed = [r for r in batch_refs if r in getattr(up, "confirmed", [])]
    append_posted(confirmed)
    ambiguous = [r for r in batch_refs if r in up.ambiguous]
    if ambiguous:
        log(f"  {UNCONFIRMED_MSG}")
        append_unconfirmed(ambiguous)
        report_unconfirmed(ambiguous)
    retry = [r for r in batch_refs if r not in confirmed and r not in ambiguous]
    if confirmed:
        log(f"  Google: added {', '.join(confirmed)} (pending Google review)")
    if retry:
        log(f"  ** Not posted, will retry next run: {', '.join(retry)}")
    if isinstance(error, ApiAuthError):
        log(f"  {REAUTH_MSG}")
        log(f"  ({error})")
        return 1
    if error is not None:
        raise error
    return 0 if len(confirmed) == len(batch_refs) else 1


def report_unconfirmed(refs):
    if not refs:
        return
    refs = sorted(refs, key=lambda r: ref_number(r) or 0)
    log(f"  ** Needs a manual check on the Google profile: {', '.join(refs)}")
    log("  ** These won't be retried. If the photo is missing, release it with:")
    for r in refs:
        log(f"  **   py -3 update_gbp.py --clear-unconfirmed {r}")
    log("  ** If it IS on the profile, change its line in gbp-posted.txt to just the ref.")
    log("")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        rc = run(argv)
    except Exception as e:  # never let the .bat see a traceback-only failure
        import traceback
        _log_lines.append(traceback.format_exc())
        log(f"  Error: {e}")
        log(f"  {FAIL_MSG}")
        rc = 1
    if "--dry-run" not in argv:
        try:
            log(f"  Log saved: {save_log()}")
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
