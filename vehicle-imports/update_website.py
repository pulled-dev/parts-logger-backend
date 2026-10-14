#!/usr/bin/env python3
"""
Pulled Apart — Update Website orchestrator.

Creates new vehicles on staging2 + live from BreakerPro CSV and Parts Photos.
Designed to be called from Update Vehicles.bat after the Parts Logger import.

Usage:
    python update_website.py                # normal run
    python update_website.py --dry-run      # show candidates, touch nothing
    python update_website.py --staging-only  # create drafts on staging2, stop

Requirements: Python 3.8+, stdlib only. Uses Windows OpenSSH for server access.
"""

import csv
import datetime
import glob
import os
import re
import subprocess
import sys
import textwrap

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

SSH_EXE = r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP_EXE = r"C:\Windows\System32\OpenSSH\scp.exe"
SSH_HOST = "pa-sg"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

STAGING_PATH = "/home/customer/www/staging2.pulledapart.co.uk/public_html"
LIVE_PATH = "/home/customer/www/pulledapart.co.uk/public_html"
STAGING_URL = "https://staging2.pulledapart.co.uk"
LIVE_URL = "https://pulledapart.co.uk"

PHOTOS_DIR = r"C:\Users\ACER\Desktop\Pulled Apart Ltd\Parts Photos"
IMPORTS_DIR = os.path.dirname(os.path.abspath(__file__))
DONE_DIR = os.path.join(IMPORTS_DIR, "done")
INBOX_DIR = os.path.join(IMPORTS_DIR, "inbox")
LOGS_DIR = os.path.join(IMPORTS_DIR, "logs")
IGNORE_FILE = os.path.join(IMPORTS_DIR, "website-ignore.txt")

IMPORTER_PHP = "wp-content/plugins/pulled-apart-core/import-vehicles.php"
PLUGIN_DIR_SERVER = "wp-content/plugins/pulled-apart-core"

TEMP_DIR_BASE = "/home/customer/tmp-vehicle-import"

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

_log_lines = []
_stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def log(msg="", end="\n"):
    print(msg, end=end, flush=True)
    _log_lines.append(msg)


def log_error(msg):
    print(f"  ** ERROR: {msg}")
    _log_lines.append(f"ERROR: {msg}")


def save_log():
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"{_stamp}_website.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(_log_lines))
    return path


def ssh_run(cmd, path=None, timeout=120):
    """Run a command on the server via SSH. Returns (returncode, stdout, stderr)."""
    if path:
        cmd = f"wp {cmd} --path={path}"
    full = [SSH_EXE] + SSH_OPTS + [SSH_HOST, cmd]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "SSH command timed out"
    except FileNotFoundError:
        return 1, "", f"SSH executable not found: {SSH_EXE}"


def scp_upload(local_path, remote_path):
    """Upload a file via SCP. Returns (success, error_msg)."""
    full = [SCP_EXE] + SSH_OPTS + [local_path, f"{SSH_HOST}:{remote_path}"]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "SCP timed out"
    except FileNotFoundError:
        return False, f"SCP executable not found: {SCP_EXE}"


def wp_cli(cmd, path):
    """Run a WP-CLI command on the server. Returns (returncode, stdout, stderr)."""
    return ssh_run(cmd, path=path)


def get_server_refs(path):
    """Get all _pac_stock_ref values from a WordPress install."""
    rc, out, err = wp_cli(
        "eval \"\\$q = get_posts(['post_type'=>'vehicle','post_status'=>'any',"
        "'posts_per_page'=>-1,'fields'=>'ids']); "
        "foreach(\\$q as \\$id){echo strtoupper(get_post_meta(\\$id,'_pac_stock_ref',true)).'|'.get_post_status(\\$id).\\\"\\\\n\\\";}\"",
        path,
    )
    if rc != 0:
        return None
    refs = {}
    for line in out.splitlines():
        line = line.strip()
        if "|" in line:
            ref, status = line.split("|", 1)
            if ref:
                refs[ref.upper()] = status
    return refs


# ═══════════════════════════════════════════════════════════════════
# CSV parsing
# ═══════════════════════════════════════════════════════════════════

def find_csv():
    """Find the CSV to use: newest in done/ (the .bat already moved it there)."""
    # Check if there's a CSV that was just imported (newest in done/)
    csvs = sorted(glob.glob(os.path.join(DONE_DIR, "*.csv")), key=os.path.getmtime)
    if csvs:
        chosen = csvs[-1]  # newest
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(chosen))
        return chosen, mtime
    return None, None


def parse_csv(path):
    """Parse BreakerPro CSV (3 junk header lines, then real headers)."""
    vehicles = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        # Skip 3 junk lines
        for _ in range(3):
            f.readline()
        reader = csv.DictReader(f)
        for row in reader:
            ref = (row.get("Stock Reference") or "").strip().upper()
            if ref:
                vehicles[ref] = row
    return vehicles


# ═══════════════════════════════════════════════════════════════════
# Ignore file
# ═══════════════════════════════════════════════════════════════════

def load_ignore():
    """Load refs from website-ignore.txt."""
    ignored = set()
    if not os.path.exists(IGNORE_FILE):
        return ignored
    with open(IGNORE_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                ignored.add(line.upper())
    return ignored


# ═══════════════════════════════════════════════════════════════════
# Candidate detection
# ═══════════════════════════════════════════════════════════════════

# Skip list — same as import-vehicles.php
SKIP_LIST = {"VEH67", "VEH120"}


def has_main1(ref):
    """Check if the vehicle's photo folder has a Main 1 image."""
    folder = os.path.join(PHOTOS_DIR, ref)
    if not os.path.isdir(folder):
        # Try case-insensitive
        for d in os.listdir(PHOTOS_DIR):
            if d.upper() == ref.upper() and os.path.isdir(os.path.join(PHOTOS_DIR, d)):
                folder = os.path.join(PHOTOS_DIR, d)
                break
        else:
            return False, None
    for f in os.listdir(folder):
        if re.match(r"^main\s*1\.(jpe?g)$", f, re.IGNORECASE):
            return True, folder
    return False, folder


def find_photo_folder(ref):
    """Find the actual photo folder path for a ref (case-insensitive)."""
    folder = os.path.join(PHOTOS_DIR, ref)
    if os.path.isdir(folder):
        return folder
    for d in os.listdir(PHOTOS_DIR):
        if d.upper() == ref.upper() and os.path.isdir(os.path.join(PHOTOS_DIR, d)):
            return os.path.join(PHOTOS_DIR, d)
    return None


def get_main_files(folder):
    """Get list of Main 1-4 image paths from a folder."""
    mains = {}
    for f in os.listdir(folder):
        m = re.match(r"^main\s*(\d)\.(jpe?g)$", f, re.IGNORECASE)
        if m and 1 <= int(m.group(1)) <= 4:
            mains[int(m.group(1))] = os.path.join(folder, f)
    return mains


# ═══════════════════════════════════════════════════════════════════
# RESULT line parser
# ═══════════════════════════════════════════════════════════════════

def parse_result_lines(output):
    """Parse RESULT| lines from importer output. Returns list of dicts."""
    results = []
    fields = [
        "ref", "action", "post_id", "post_status", "title", "year",
        "engine_code", "colour_code", "colour", "mileage", "images",
        "permalink", "warnings",
    ]
    for line in output.splitlines():
        if line.startswith("RESULT|"):
            parts = line.split("|")[1:]  # skip "RESULT"
            row = {}
            for i, name in enumerate(fields):
                row[name] = parts[i] if i < len(parts) else ""
            results.append(row)
    return results


# ═══════════════════════════════════════════════════════════════════
# Server operations
# ═══════════════════════════════════════════════════════════════════

def create_temp_dir():
    """Create a temporary directory on the server for uploads."""
    temp_dir = f"{TEMP_DIR_BASE}-{_stamp}"
    rc, _, err = ssh_run(f"mkdir -p {temp_dir}/photos {temp_dir}/csv")
    if rc != 0:
        return None, f"Failed to create temp dir: {err}"
    return temp_dir, ""


def remove_temp_dir(temp_dir):
    """Remove the temporary directory from the server."""
    if temp_dir:
        ssh_run(f"rm -rf {temp_dir}")


def upload_photos_and_csv(candidates, csv_path, temp_dir):
    """Upload Main 1-4 photos and CSV to the server temp dir."""
    # Upload CSV
    ok, err = scp_upload(csv_path, f"{temp_dir}/csv/stock.csv")
    if not ok:
        return False, f"CSV upload failed: {err}"

    # Upload photos per vehicle
    for ref in candidates:
        folder = find_photo_folder(ref)
        if not folder:
            continue
        mains = get_main_files(folder)
        if not mains:
            continue

        # Create ref folder on server
        ssh_run(f"mkdir -p {temp_dir}/photos/{ref}")

        for num, local_path in sorted(mains.items()):
            fname = f"Main {num}.jpg"
            ok, err = scp_upload(local_path, f"{temp_dir}/photos/{ref}/{fname}")
            if not ok:
                return False, f"Photo upload failed ({ref} Main {num}): {err}"

    return True, ""


def run_importer(path, refs, temp_dir, extra_args=""):
    """Run the importer on a server WordPress install."""
    refs_str = ",".join(refs)
    cmd = (
        f"cd {path} && wp eval-file {IMPORTER_PHP} "
        f"only:{refs_str} "
        f"csv:{temp_dir}/csv/stock.csv "
        f"photos:{temp_dir}/photos "
        f'vstatus:"Breaking Now" '
        f"{extra_args}"
    )
    rc, out, err = ssh_run(cmd, timeout=300)
    return rc, out, err


def publish_drafts(path, refs):
    """Publish existing draft vehicles by ref."""
    refs_str = ",".join(refs)
    cmd = f"cd {path} && wp eval-file {IMPORTER_PHP} publish-existing:{refs_str}"
    rc, out, err = ssh_run(cmd, timeout=60)
    return rc, out, err


def delete_drafts(path, refs):
    """Delete draft vehicles by ref."""
    refs_str = ",".join(refs)
    cmd = f"cd {path} && wp eval-file {IMPORTER_PHP} delete-drafts:{refs_str}"
    rc, out, err = ssh_run(cmd, timeout=60)
    return rc, out, err


def purge_cache(path):
    """Purge SiteGround cache."""
    rc, out, err = wp_cli("sg purge", path)
    return rc == 0


def verify_live(results):
    """Verify each vehicle is accessible on live. Returns list of (ref, url, ok)."""
    checks = []
    for r in results:
        permalink = r.get("permalink", "")
        if not permalink:
            checks.append((r["ref"], "", False))
            continue
        # Replace staging URL with live URL if needed
        url = permalink.replace(STAGING_URL, LIVE_URL)
        # Check via curl on server (avoids staging basic auth issues)
        rc, out, _ = ssh_run(
            f'curl -s -o /dev/null -w "%{{http_code}}" {url}',
            timeout=15,
        )
        ok = out.strip() == "200"
        checks.append((r["ref"], url, ok))
    return checks


# ═══════════════════════════════════════════════════════════════════
# Display
# ═══════════════════════════════════════════════════════════════════

def print_table(results):
    """Print a summary table of RESULT lines."""
    if not results:
        log("  (no vehicles processed)")
        return
    log("")
    header = f"  {'Ref':<8} {'Title':<35} {'Year':<5} {'Engine':<12} {'Paint':<6} {'Colour':<18} {'Miles':<8} {'Imgs':<5} {'Status':<10} {'Warnings'}"
    log(header)
    log("  " + "-" * (len(header) - 2))
    for r in results:
        warns = r.get("warnings", "").replace(";", ", ")
        log(
            f"  {r['ref']:<8} {r.get('title','')[:35]:<35} {r.get('year',''):<5} "
            f"{r.get('engine_code',''):<12} {r.get('colour_code',''):<6} "
            f"{r.get('colour',''):<18} {r.get('mileage',''):<8} "
            f"{r.get('images',''):<5} {r.get('post_status',''):<10} {warns}"
        )
    log("")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    dry_run = "--dry-run" in sys.argv
    staging_only = "--staging-only" in sys.argv

    log("")
    log("================================================")
    log("  Pulled Apart — Update Website")
    log("================================================")
    log("")

    # ── Step 1: Find CSV ──────────────────────────────────────────
    csv_path, csv_date = find_csv()
    if not csv_path:
        log_error("No CSV found in done/ folder.")
        log("  Export from BreakerPro and run Update Vehicles.bat first.")
        save_log()
        return 1

    log(f"  CSV: {os.path.basename(csv_path)}")
    log(f"  Date: {csv_date.strftime('%Y-%m-%d %H:%M')}")

    csv_vehicles = parse_csv(csv_path)
    log(f"  Vehicles in CSV: {len(csv_vehicles)}")
    log("")

    # ── Step 2: Preflight — SSH + server refs ─────────────────────
    log("  Checking server connection...")
    rc, out, err = ssh_run("echo OK")
    if rc != 0 or out.strip() != "OK":
        log_error(f"Cannot connect to server ({SSH_HOST}).")
        log("  Check your internet connection and try again.")
        log(f"  Detail: {err}")
        save_log()
        return 1

    log("  Reading existing vehicles from live...")
    live_refs = get_server_refs(LIVE_PATH)
    if live_refs is None:
        log_error("Could not read vehicle list from live site.")
        log("  The server may be down — just run it again later.")
        save_log()
        return 1
    log(f"  Live vehicles: {len(live_refs)}")

    log("  Reading existing vehicles from staging2...")
    staging_refs = get_server_refs(STAGING_PATH)
    if staging_refs is None:
        log_error("Could not read vehicle list from staging2.")
        save_log()
        return 1
    log(f"  Staging2 vehicles: {len(staging_refs)}")
    log("")

    # ── Step 3: Build candidate + waiting lists ───────────────────
    ignored = load_ignore()
    candidates = []
    waiting = []

    for ref in sorted(csv_vehicles.keys()):
        # Already on live?
        if ref in live_refs:
            continue
        # In skip list?
        if ref in SKIP_LIST:
            continue
        # In ignore file?
        if ref in ignored:
            continue
        # Has Main 1?
        has_m1, folder = has_main1(ref)
        if has_m1:
            candidates.append(ref)
        else:
            waiting.append(ref)

    # Also check for stale staging2 drafts from a previous failed run
    stale_staging = []
    for ref in candidates:
        if ref in staging_refs and staging_refs[ref] == "draft":
            stale_staging.append(ref)

    if not candidates and not waiting:
        log("  Nothing to do — all CSV vehicles are already on the live site.")
        log("")
        save_log()
        return 0

    if candidates:
        log(f"  Candidates ({len(candidates)} vehicles ready to add):")
        for ref in candidates:
            row = csv_vehicles[ref]
            make = row.get("Make", "")
            model = row.get("Model", "")
            year = row.get("Year Manufacture", "")
            stale = " (stale draft on staging2 — will reuse)" if ref in stale_staging else ""
            log(f"    {ref}: {year} {make} {model}{stale}")
        log("")

    if waiting:
        log(f"  Waiting for photos ({len(waiting)} vehicles):")
        for ref in waiting:
            row = csv_vehicles[ref]
            make = row.get("Make", "")
            model = row.get("Model", "")
            log(f"    {ref}: {make} {model} — needs Main 1-4 images in Parts Photos")
        log("")

    if not candidates:
        log("  No candidates ready — waiting for photos on the vehicles listed above.")
        log("")
        save_log()
        return 0

    if dry_run:
        log("  --dry-run: stopping here. Nothing was changed on the server.")
        log("")
        save_log()
        return 0

    # ── Step 4: Clean up stale staging drafts ─────────────────────
    if stale_staging:
        log(f"  Cleaning up {len(stale_staging)} stale staging2 draft(s)...")
        rc, out, err = delete_drafts(STAGING_PATH, stale_staging)
        if rc != 0:
            log_error(f"Failed to clean stale drafts: {err}")
            # Non-fatal — the importer is idempotent
        else:
            for line in out.splitlines():
                if "DELETED" in line or "no draft" in line:
                    log(f"    {line}")
        log("")

    # ── Step 5: Upload photos + CSV ───────────────────────────────
    log("  Creating temp directory on server...")
    temp_dir, err = create_temp_dir()
    if not temp_dir:
        log_error(err)
        save_log()
        return 1

    try:
        log(f"  Uploading photos and CSV to {temp_dir}...")
        ok, err = upload_photos_and_csv(candidates, csv_path, temp_dir)
        if not ok:
            log_error(err)
            log("  Just run it again — uploads resume safely.")
            return 1

        log(f"  Uploaded {len(candidates)} vehicle(s) + CSV.")
        log("")

        # ── Step 6: Create on staging2 as draft ───────────────────
        log("  Creating vehicles on staging2 (draft)...")
        rc, out, err = run_importer(STAGING_PATH, candidates, temp_dir)
        _log_lines.append(f"--- staging2 importer output ---\n{out}\n{err}\n---")

        if rc != 0:
            log_error(f"Staging2 importer failed (exit {rc}).")
            if out:
                for line in out.splitlines()[-10:]:
                    log(f"  stdout: {line}")
            if err:
                for line in err.splitlines()[-5:]:
                    log(f"  stderr: {line}")
            log("  Tell Claude -- include the log file.")
            return 1

        staging_results = parse_result_lines(out)
        if not staging_results:
            log_error("Staging2 importer ran but produced no RESULT lines.")
            log("  Importer output:")
            for line in out.splitlines():
                log(f"    {line}")
            log("  Tell Claude — include the log file.")
            return 1

        # Show the summary table
        log("")
        log("  -- Staging2 Results --")
        print_table(staging_results)

        # Check for blocked vehicles
        publishable = [r for r in staging_results if r["action"] == "CREATED" and r["post_status"] == "draft"]
        blocked = [r for r in staging_results if r["action"] == "BLOCKED"]
        if blocked:
            log(f"  {len(blocked)} vehicle(s) BLOCKED — will not be published:")
            for r in blocked:
                log(f"    {r['ref']}: {r.get('warnings', '')}")
            log("")

        if not publishable:
            log("  No vehicles to publish.")
            return 0

        if staging_only:
            log("  --staging-only: stopping here. Drafts created on staging2.")
            log("  To clean up: run with --dry-run or manually delete drafts.")
            log("")
            # Clean up staging drafts
            staging_refs_to_clean = [r["ref"] for r in publishable]
            log(f"  Cleaning up {len(staging_refs_to_clean)} staging2 draft(s)...")
            delete_drafts(STAGING_PATH, staging_refs_to_clean)
            return 0

        # ── Step 7: Prompt ────────────────────────────────────────
        log(f"  Publish these {len(publishable)} vehicles to the live website? [y/N] ", end="")
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer != "y":
            log("")
            log("  Cancelled. Cleaning up staging2 drafts...")
            staging_refs_to_clean = [r["ref"] for r in publishable]
            delete_drafts(STAGING_PATH, staging_refs_to_clean)
            log("  Done — nothing was changed on the live site.")
            return 0

        log("")

        # ── Step 8: Create + publish on live ──────────────────────
        log("  Creating and publishing vehicles on live...")
        live_refs_to_create = [r["ref"] for r in publishable]
        rc, out, err = run_importer(
            LIVE_PATH, live_refs_to_create, temp_dir,
            extra_args="status:publish "
        )
        _log_lines.append(f"--- live importer output ---\n{out}\n{err}\n---")

        if rc != 0:
            log_error(f"Live importer failed (exit {rc}).")
            if err:
                log(f"  {err}")
            log("  Tell Claude — the staging2 drafts are still there.")
            return 1

        live_results = parse_result_lines(out)

        log("")
        log("  -- Live Results --")
        print_table(live_results)

        # Publish staging2 drafts
        log("  Publishing staging2 drafts...")
        publish_drafts(STAGING_PATH, live_refs_to_create)

        # Purge live cache
        log("  Purging live site cache...")
        purge_cache(LIVE_PATH)

        # ── Step 9: Verify ────────────────────────────────────────
        log("")
        log("  -- Verification --")
        checks = verify_live(live_results)
        all_ok = True
        for ref, url, ok in checks:
            status = "PASS" if ok else "FAIL"
            log(f"  [{status}] {ref}: {url}")
            if not ok:
                all_ok = False

        log("")
        if all_ok:
            log("  All checks PASSED.")
        else:
            log("  Some checks FAILED — check the URLs manually.")

        log("")
        log("  Live URLs:")
        for ref, url, ok in checks:
            if url:
                log(f"    {url}")

        return 0 if all_ok else 1

    finally:
        # Always clean up temp dir
        log("")
        log("  Removing temp directory from server...")
        remove_temp_dir(temp_dir)
        log_path = save_log()
        log(f"  Log saved: {log_path}")


if __name__ == "__main__":
    sys.exit(main())
