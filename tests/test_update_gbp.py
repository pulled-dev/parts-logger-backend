"""Google Business Profile hero-photo step (vehicle-imports/update_gbp.py).
Everything runs on a temp photo folder + temp state file; no browser, no network."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "vehicle-imports")]
import update_gbp as gbp  # noqa: E402
import update_website as uw  # noqa: E402


def make_car(photos, ref, main1="Main 1.jpg", extra=("Main 2.jpg",)):
    d = photos / ref
    d.mkdir()
    for name in ([main1] if main1 else []) + list(extra):
        (d / name).write_bytes(b"\xff\xd8jpg")
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    photos = tmp_path / "Parts Photos"
    photos.mkdir()
    logs = tmp_path / "logs"
    state = tmp_path / "gbp-posted.txt"
    monkeypatch.setattr(uw, "PHOTOS_DIR", str(photos))
    monkeypatch.setattr(uw, "LOGS_DIR", str(logs))
    monkeypatch.setattr(gbp, "STATE_FILE", str(state))
    monkeypatch.setattr(gbp, "_log_lines", [])
    monkeypatch.setattr(gbp, "get_live_refs", lambda: None)
    return photos, state, logs


# ── candidate selection ─────────────────────────────────────────────

def test_baseline_respected(env):
    photos, _, _ = env
    for r in ("VEH174", "VEH175", "VEH176"):
        make_car(photos, r)
    refs = [r for r, _ in gbp.find_candidates(175, set())]
    assert refs == ["VEH176"]


def test_posted_list_respected(env):
    photos, _, _ = env
    for r in ("VEH176", "VEH177"):
        make_car(photos, r)
    refs = [r for r, _ in gbp.find_candidates(175, {"VEH176"})]
    assert refs == ["VEH177"]


def test_no_main1_skipped(env):
    photos, _, _ = env
    make_car(photos, "VEH176", main1=None, extra=("Main 2.jpg", "Main 3.jpg"))
    make_car(photos, "VEH177")
    assert [r for r, _ in gbp.find_candidates(175, set())] == ["VEH177"]


@pytest.mark.parametrize("name", ["Main 1.jpg", "MAIN 1.jpg", "Main 1.jpeg", "main1.JPG", "MAIN  1.JPEG"])
def test_main1_case_and_space_variants(env, name):
    photos, _, _ = env
    make_car(photos, "VEH176", main1=name, extra=())
    cands = gbp.find_candidates(175, set())
    assert [r for r, _ in cands] == ["VEH176"]
    assert os.path.basename(cands[0][1]) == name


def test_lowercase_folder_and_numeric_order(env):
    photos, _, _ = env
    make_car(photos, "veh180")
    make_car(photos, "VEH1000")
    make_car(photos, "VEH200")
    (photos / "_Branded Covers").mkdir()
    assert [r for r, _ in gbp.find_candidates(175, set())] == ["VEH180", "VEH200", "VEH1000"]


def test_live_refs_filter_published_only(env):
    photos, _, _ = env
    for r in ("VEH176", "VEH177", "VEH178"):
        make_car(photos, r)
    live = {"VEH176": "publish", "VEH177": "draft"}
    assert [r for r, _ in gbp.find_candidates(175, set(), live)] == ["VEH176"]


def test_main2_not_used_as_hero(env):
    photos, _, _ = env
    make_car(photos, "VEH176", main1="Main 1.jpg", extra=("Main 2.jpg", "Main 3.jpg"))
    (_, path), = gbp.find_candidates(175, set())
    assert os.path.basename(path) == "Main 1.jpg"
    assert gbp.ref_for_path(path) == "VEH176"


# ── state file ──────────────────────────────────────────────────────

def test_state_created_and_seeded_if_missing(env):
    _, state, _ = env
    assert not state.exists()
    gbp.ensure_state()
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175"]
    assert gbp.load_state() == (175, set())


def test_load_state_missing_file_uses_default(env):
    assert gbp.load_state() == (175, set())


def test_append_posted_creates_and_seeds(env):
    _, state, _ = env
    gbp.append_posted(["VEH176"])
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH176"]
    assert gbp.load_state() == (175, {"VEH176"})


class FakeUploader:
    def __init__(self, confirm):
        self.confirm = confirm
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def upload(self, paths):
        self.sent = list(paths)
        return [gbp.ref_for_path(p) for p in paths if gbp.ref_for_path(p) in self.confirm]


def test_partial_confirmation_marks_nothing(env, monkeypatch, capsys):
    photos, state, _ = env
    for r in ("VEH176", "VEH177"):
        make_car(photos, r)
    up = FakeUploader(confirm={"VEH176"})
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "y")
    rc = gbp.main([])
    assert rc == 1
    assert len(up.sent) == 2
    assert gbp.UNCONFIRMED_MSG in capsys.readouterr().out
    # Nothing marked as posted; the whole batch is parked as UNCONFIRMED.
    assert state.read_text(encoding="utf-8").splitlines() == [
        "BASELINE=VEH175", "UNCONFIRMED VEH176", "UNCONFIRMED VEH177"]
    assert gbp.load_state() == (175, set())


def test_all_confirmed_summary(env, monkeypatch, capsys):
    photos, state, _ = env
    for r in ("VEH176", "VEH177"):
        make_car(photos, r)
    monkeypatch.setattr(gbp, "make_uploader", lambda: FakeUploader({"VEH176", "VEH177"}))
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 0
    assert "Google: added VEH176, VEH177 (pending Google review)" in capsys.readouterr().out
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH176", "VEH177"]


def test_answer_not_y_skips(env, monkeypatch):
    photos, state, _ = env
    make_car(photos, "VEH176")
    monkeypatch.setattr(gbp, "make_uploader", lambda: pytest.fail("must not upload"))
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert gbp.main([]) == 0
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175"]


def test_max_per_run(env, monkeypatch):
    photos, _, _ = env
    for n in range(176, 176 + 25):
        make_car(photos, f"VEH{n}")
    up = FakeUploader(confirm=set())
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    gbp.main([])
    assert len(up.sent) == gbp.MAX_PER_RUN


def test_exception_prints_fail_and_exits_1(env, monkeypatch, capsys):
    photos, _, _ = env
    make_car(photos, "VEH176")

    def boom():
        raise RuntimeError("browser crashed")
    monkeypatch.setattr(gbp, "make_uploader", boom)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    assert gbp.FAIL_MSG in capsys.readouterr().out


# ── API uploader (fake HTTP, no network) ─────────────────────────────

class FakeApi:
    """Stands in for http_json. fail = {(ref, step): ApiError} where step is
    start / bytes / create. Records every call."""
    def __init__(self, fail=None):
        self.fail = fail or {}
        self.calls = []
        self.current = None
        self.n = 0

    def __call__(self, url, method="GET", body=None, token=None, data=None, **kw):
        if url.endswith(":startUpload"):
            step = "start"
        elif "/upload/v1/media/" in url:
            step = "bytes"
        elif method == "POST" and url.endswith("/media"):
            step = "create"
        else:
            step = "other"
        self.calls.append((self.current, step))
        err = self.fail.get((self.current, step))
        if err:
            raise err
        if step == "start":
            self.n += 1
            return {"resourceName": f"res{self.n}"}
        if step == "create":
            assert body["mediaFormat"] == "PHOTO"
            assert body["locationAssociation"] == {"category": "ADDITIONAL"}
            return {"name": f"accounts/1/locations/2/media/m{self.n}"}
        return {}


def api_uploader(fake):
    up = gbp.ApiUploader(call=fake, sleep=lambda s: None)
    up.account, up.location, up.token = "accounts/1", "locations/2", "tok"
    orig = up.upload_one

    def tracked(path):
        fake.current = gbp.ref_for_path(path)
        return orig(path)
    up.upload_one = tracked
    return up


def big_car(photos, ref, size=20 * 1024):
    d = photos / ref
    d.mkdir()
    (d / "Main 1.jpg").write_bytes(b"\xff\xd8" + b"0" * size)
    return str(d / "Main 1.jpg")


def test_api_posts_each_photo_in_three_steps(tmp_path):
    a, b = big_car(tmp_path, "VEH176"), big_car(tmp_path, "VEH177")
    fake = FakeApi()
    up = api_uploader(fake)
    assert up.upload([a, b]) == ["VEH176", "VEH177"]
    assert [s for _, s in fake.calls] == ["start", "bytes", "create"] * 2
    assert not up.ambiguous and not up.not_uploaded


def test_api_clean_failure_is_retried_not_unconfirmed(tmp_path):
    a, b = big_car(tmp_path, "VEH176"), big_car(tmp_path, "VEH177")
    fake = FakeApi({("VEH176", "create"): gbp.ApiError("400 bad photo", status=400)})
    up = api_uploader(fake)
    assert up.upload([a, b]) == ["VEH177"]
    assert up.not_uploaded == {"VEH176"} and not up.ambiguous


def test_api_create_timeout_is_ambiguous(tmp_path):
    a = big_car(tmp_path, "VEH176")
    fake = FakeApi({("VEH176", "create"): gbp.ApiError("timeout", ambiguous=True)})
    up = api_uploader(fake)
    assert up.upload([a]) == []
    assert up.ambiguous == {"VEH176"}


def test_api_start_failure_retries_then_gives_up_cleanly(tmp_path):
    a = big_car(tmp_path, "VEH176")
    fake = FakeApi({("VEH176", "start"): gbp.ApiError("503", status=503, ambiguous=True)})
    up = api_uploader(fake)
    assert up.upload([a]) == []
    assert [s for _, s in fake.calls] == ["start"] * 3  # retried, never created
    # Before the create call nothing can exist on the profile, but ambiguous
    # flag from a 5xx start is still not "create ambiguous" - see below.


def test_api_tiny_photo_rejected_without_calling_google(tmp_path):
    d = tmp_path / "VEH176"
    d.mkdir()
    (d / "Main 1.jpg").write_bytes(b"\xff\xd8jpg")
    fake = FakeApi()
    up = api_uploader(fake)
    assert up.upload([str(d / "Main 1.jpg")]) == []
    assert fake.calls == [] and up.not_uploaded == {"VEH176"}


class FakeApiUploader:
    per_item = True

    def __init__(self, confirmed=(), ambiguous=(), not_uploaded=(), raise_exc=None):
        self.confirmed = list(confirmed)
        self.ambiguous = set(ambiguous)
        self.not_uploaded = set(not_uploaded)
        self.raise_exc = raise_exc
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def upload(self, paths):
        self.sent = list(paths)
        if self.raise_exc:
            raise self.raise_exc
        return self.confirmed


def test_run_per_item_records_each_car(env, monkeypatch, capsys):
    photos, state, _ = env
    for r in ("VEH176", "VEH177", "VEH178"):
        make_car(photos, r)
    up = FakeApiUploader(confirmed=["VEH176"], ambiguous=["VEH177"], not_uploaded=["VEH178"])
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    out = capsys.readouterr().out
    assert "Google: added VEH176" in out
    assert "will retry next run: VEH178" in out
    assert state.read_text(encoding="utf-8").splitlines() == [
        "BASELINE=VEH175", "VEH176", "UNCONFIRMED VEH177"]


def test_run_per_item_all_posted(env, monkeypatch, capsys):
    photos, state, _ = env
    for r in ("VEH176", "VEH177"):
        make_car(photos, r)
    monkeypatch.setattr(gbp, "make_uploader",
                        lambda: FakeApiUploader(confirmed=["VEH176", "VEH177"]))
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 0
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH176", "VEH177"]


def test_run_per_item_auth_error_keeps_earlier_posts(env, monkeypatch, capsys):
    photos, state, _ = env
    for r in ("VEH176", "VEH177"):
        make_car(photos, r)
    up = FakeApiUploader(confirmed=["VEH176"], raise_exc=gbp.ApiAuthError("invalid_grant"))
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    out = capsys.readouterr().out
    assert gbp.REAUTH_MSG in out
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH176"]


def test_run_per_item_auth_error_on_enter(env, monkeypatch, capsys):
    photos, state, _ = env
    make_car(photos, "VEH176")
    up = gbp.ApiUploader()
    monkeypatch.setattr(gbp, "load_api_config", lambda: ("accounts/1", "locations/2"))
    def no_token():
        raise gbp.ApiAuthError("No saved Google API sign-in.")
    monkeypatch.setattr(gbp, "refresh_access_token", no_token)
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    assert gbp.REAUTH_MSG in capsys.readouterr().out
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175"]


def test_refresh_token_invalid_grant_is_auth_error(tmp_path):
    client = tmp_path / "c.json"
    client.write_text('{"installed": {"client_id": "id", "client_secret": "s"}}')
    token = tmp_path / "t.json"
    token.write_text('{"refresh_token": "r"}')
    def post(*a, **k):
        raise gbp.ApiError("invalid_grant", status=400)
    with pytest.raises(gbp.ApiAuthError):
        gbp.refresh_access_token(str(client), str(token), post=post)


def test_discover_location_picks_pulled_apart():
    def call(url, token=None, **k):
        if url == gbp.ACCOUNTS_URL:
            return {"accounts": [{"name": "accounts/1"}]}
        return {"locations": [{"name": "locations/9", "title": "Pulled Apart - VAG Breakers"}]}
    assert gbp.discover_location("t", call=call) == ("accounts/1", "locations/9", "Pulled Apart - VAG Breakers")


def test_default_uploader_is_api():
    assert gbp.UPLOADER == "api"
    assert isinstance(gbp.make_uploader(), gbp.ApiUploader)


# ── frame selection ─────────────────────────────────────────────────

class FakeFrame:
    def __init__(self, url):
        self.url = url


def test_picks_gbp_frame_not_main_search_page():
    main = FakeFrame("https://www.google.com/search?q=pulled+apart&kgmid=/g/x"
                     "#mpd=~123/customers/gbp/photos/add?fid=1457236246160685913")
    other = FakeFrame("https://www.google.com/recaptcha/api2/anchor")
    gbp_fr = FakeFrame("https://www.google.com/local/business/1457236246160685913"
                       "/customers/photos/add?hl=en")
    assert gbp.pick_gbp_frame([main, other, gbp_fr]) is gbp_fr
    assert gbp.pick_gbp_frame([main, other]) is None


def test_picks_mediatool_frame():
    main = FakeFrame("https://www.google.com/search#mpd=/photos/mediatool")
    mt = FakeFrame("https://www.google.com/local/business/1/customers/photos/mediatool?x")
    assert gbp.pick_mediatool_frame([main, mt]) is mt


# ── dry run ─────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(env, monkeypatch):
    photos, state, logs = env
    make_car(photos, "VEH176")
    monkeypatch.setattr(gbp, "make_uploader", lambda: pytest.fail("must not upload"))
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("must not prompt"))
    assert gbp.main(["--dry-run"]) == 0
    assert not state.exists()
    assert not logs.exists()


# ── PENDING verification (real upload() flow, scripted media-tool counts) ──

class ScriptedUploader(gbp.PlaywrightUploader):
    """upload() as written, with the browser replaced by a list of media-tool
    PENDING reads (before, then after) and a recorder for sent files."""

    def __init__(self, counts):
        super().__init__(profile_dir="unused")
        self.counts = list(counts)
        self.sent = None

        class _Page:
            def wait_for_timeout(self, ms):
                pass
        self.page = _Page()

    def mediatool_count(self):
        c = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        if isinstance(c, Exception):
            raise c
        return c

    def send_files(self, paths):
        self.sent = list(paths)


PATHS = [os.path.join("x", "VEH176", "Main 1.jpg"), os.path.join("x", "VEH177", "Main 1.jpg")]


@pytest.fixture
def fast_verify(monkeypatch):
    monkeypatch.setattr(gbp, "VERIFY_TIMEOUT", 0)


def test_exact_rise_confirms(fast_verify):
    assert ScriptedUploader([75, 77]).upload(PATHS) == ["VEH176", "VEH177"]


def test_old_pending_photos_failed_upload_does_not_pass(fast_verify):
    # 75 photos incl. 15 from 12 Nov still pending; the upload silently failed.
    # The old ">= before+N" check with before read off the add-photos page (0)
    # would have passed this. Reading before in the media tool fails it.
    up = ScriptedUploader([75, 75])
    assert up.upload(PATHS) == []
    assert up.sent == PATHS


def test_wrong_before_count_cannot_pass():
    # Even if 'before' were misread as 0 with 15 old photos pending, only an
    # exact rise counts, so 15 or 16 or 17 after 0 is never a confirmation of 2.
    for after in (15, 16, 17):
        assert not gbp.upload_confirmed(0, after, 2)
    assert gbp.upload_confirmed(15, 17, 2)


def test_overshoot_or_partial_rise_not_confirmed(fast_verify):
    assert ScriptedUploader([75, 78]).upload(PATHS) == []   # extra photo appeared
    assert ScriptedUploader([75, 76]).upload(PATHS) == []   # only one landed
    assert ScriptedUploader([75, 74]).upload(PATHS) == []   # Google removed one meanwhile


def test_slow_upload_waits_for_full_rise(monkeypatch):
    # Google shows one photo, then the second: keep reloading until both land.
    monkeypatch.setattr(gbp, "VERIFY_TIMEOUT", 60)
    assert ScriptedUploader([75, 76, 77]).upload(PATHS) == ["VEH176", "VEH177"]


def test_unreadable_before_count_uploads_nothing():
    up = ScriptedUploader([RuntimeError("media tool never loaded")])
    with pytest.raises(RuntimeError):
        up.upload(PATHS)
    assert up.sent is None


def test_unknown_counts_not_confirmed():
    assert not gbp.upload_confirmed(None, 17, 2)
    assert not gbp.upload_confirmed(15, None, 2)


# ── plain Chrome + CDP launch ───────────────────────────────────────

def test_chrome_args_plain_chrome_with_cdp():
    args = gbp.chrome_args(r"C:\chrome.exe", r"C:\prof", "https://accounts.google.com/")
    assert args[0] == r"C:\chrome.exe"
    assert "--remote-debugging-port=9222" in args
    assert r"--user-data-dir=C:\prof" in args
    assert args[-1] == "https://accounts.google.com/"
    assert not any("enable-automation" in a for a in args)


def test_start_chrome_reuses_open_debug_chrome(monkeypatch):
    monkeypatch.setattr(gbp, "cdp_ready", lambda timeout=1.0: True)
    monkeypatch.setattr(gbp.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    assert gbp.start_chrome("unused") is False


def test_start_chrome_launches_and_waits(monkeypatch, tmp_path):
    ready = iter([False, False, True])
    launched = []
    monkeypatch.setattr(gbp, "cdp_ready", lambda timeout=1.0: next(ready))
    monkeypatch.setattr(gbp, "find_chrome", lambda: "chrome.exe")
    monkeypatch.setattr(gbp.subprocess, "Popen", lambda args, **k: launched.append(args))
    monkeypatch.setattr(gbp.time, "sleep", lambda s: None)
    assert gbp.start_chrome(str(tmp_path / "prof")) is True
    assert launched and "--remote-debugging-port=9222" in launched[0]


def test_uploader_exposes_every_mode_main_calls():
    for name in ("open", "close", "login", "probe", "mediatool_tiles", "mediatool_count", "upload"):
        assert callable(getattr(gbp.PlaywrightUploader, name, None)), name


def test_newest_without_views_counts_leading_run_only():
    # 15 new photos first, then reviewed ones; older no-view photos further
    # down (low traffic) are not counted as pending.
    tiles = [False] * 15 + [True, False, True, True, False]
    assert gbp.newest_without_views(tiles) == 15
    assert gbp.newest_without_views([True, False]) == 0
    assert gbp.newest_without_views([]) == 0


# ── UNCONFIRMED: attempted uploads are never retried blind ─────────

def test_unconfirmed_upload_parked_and_reported(env, monkeypatch, capsys):
    photos, state, _ = env
    make_car(photos, "VEH176")
    monkeypatch.setattr(gbp, "make_uploader", lambda: FakeUploader(confirm=set()))
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    out = capsys.readouterr().out
    assert "Needs a manual check on the Google profile: VEH176" in out
    assert "--clear-unconfirmed VEH176" in out
    assert gbp.load_unconfirmed() == {"VEH176"}


def test_unconfirmed_skipped_on_future_runs(env, monkeypatch, capsys):
    photos, state, _ = env
    make_car(photos, "VEH176")
    make_car(photos, "VEH177")
    state.write_text("BASELINE=VEH175\nUNCONFIRMED VEH176\n", encoding="utf-8")
    up = FakeUploader(confirm={"VEH177"})
    monkeypatch.setattr(gbp, "make_uploader", lambda: up)
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 0
    assert [gbp.ref_for_path(p) for p in up.sent] == ["VEH177"]
    out = capsys.readouterr().out
    assert "Needs a manual check on the Google profile: VEH176" in out
    assert state.read_text(encoding="utf-8").splitlines() == [
        "BASELINE=VEH175", "UNCONFIRMED VEH176", "VEH177"]


def test_unconfirmed_only_gives_no_new_cars_and_no_upload(env, monkeypatch, capsys):
    photos, state, _ = env
    make_car(photos, "VEH176")
    state.write_text("BASELINE=VEH175\nUNCONFIRMED VEH176\n", encoding="utf-8")
    monkeypatch.setattr(gbp, "make_uploader", lambda: pytest.fail("must not upload"))
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("must not prompt"))
    assert gbp.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "no new cars" in out and "VEH176" in out


class CrashAfterSendUploader(FakeUploader):
    """Files reached Google, then the browser died before the count check."""
    attempted = False

    def upload(self, paths):
        self.sent = list(paths)
        self.attempted = True
        raise RuntimeError("Chrome crashed while verifying")


def test_crash_after_send_parks_unconfirmed(env, monkeypatch, capsys):
    photos, state, _ = env
    make_car(photos, "VEH176")
    monkeypatch.setattr(gbp, "make_uploader", lambda: CrashAfterSendUploader(set()))
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    out = capsys.readouterr().out
    assert gbp.FAIL_MSG in out and "Needs a manual check" in out
    assert gbp.load_unconfirmed() == {"VEH176"}


class CrashBeforeSendUploader(FakeUploader):
    """Couldn't even read the 'before' count: nothing reached Google."""
    attempted = False

    def upload(self, paths):
        raise RuntimeError("media tool never loaded")


def test_crash_before_send_leaves_car_retryable(env, monkeypatch):
    photos, state, _ = env
    make_car(photos, "VEH176")
    monkeypatch.setattr(gbp, "make_uploader", lambda: CrashBeforeSendUploader(set()))
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert gbp.main([]) == 1
    assert gbp.load_unconfirmed() == set()
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175"]


def test_send_files_flags_attempt_before_handing_over():
    up = gbp.PlaywrightUploader(profile_dir="unused")
    assert up.attempted is False

    class _Input:
        def set_input_files(self, paths):
            assert up.attempted is True  # flagged before Google sees the files
            raise RuntimeError("dialog vanished")

    class _Loc:
        first = _Input()

    class _Frame:
        def locator(self, sel):
            return _Loc()
    up.open_add_dialog = lambda: _Frame()
    with pytest.raises(RuntimeError):
        up.send_files(["x/VEH176/Main 1.jpg"])
    assert up.attempted is True


def test_clear_unconfirmed_releases_for_retry(env, monkeypatch):
    photos, state, _ = env
    make_car(photos, "VEH176")
    state.write_text("BASELINE=VEH175\nUNCONFIRMED VEH176\nVEH177\n", encoding="utf-8")
    assert gbp.main(["--clear-unconfirmed", "veh176"]) == 0
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH177"]
    assert [r for r, _ in gbp.find_candidates(175, gbp.load_state()[1] | gbp.load_unconfirmed())] == ["VEH176"]


def test_clear_unconfirmed_unknown_or_missing_ref(env):
    _, state, _ = env
    state.write_text("BASELINE=VEH175\nVEH177\n", encoding="utf-8")
    assert gbp.main(["--clear-unconfirmed", "VEH177"]) == 1  # posted, not unconfirmed
    assert gbp.main(["--clear-unconfirmed"]) == 1
    assert state.read_text(encoding="utf-8").splitlines() == ["BASELINE=VEH175", "VEH177"]


# ── media tool lazy loading ────────────────────────────────────────

class LazyFrame:
    """A grid that loads 30 more photos each time it is scrolled, up to total."""

    def __init__(self, total, page=30):
        self.total, self.page, self.loaded = total, page, page

    def evaluate(self, js):
        if js == gbp._SCROLL_JS:
            self.loaded = min(self.total, self.loaded + self.page)
            return None
        return [True] * self.loaded


def test_mediatool_scrolls_until_no_new_photos(monkeypatch):
    up = gbp.PlaywrightUploader(profile_dir="unused")
    fr = LazyFrame(total=75)
    up.open_mediatool = lambda: fr

    class _Page:
        def wait_for_timeout(self, ms):
            pass
    up.page = _Page()
    assert up.mediatool_count() == 75
