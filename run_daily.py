#!/usr/bin/env python3
"""
GRV Item Availability -- Daily Automation Wrapper
=================================================
Replaces the manual "run CMD, then upload files via the GitHub web portal"
workflow. It runs the existing refresh, validates the output, mirrors it into
the local docs\\ folder, and publishes to GitHub Pages via the Contents API.

WHAT IT DOES (in order):
  1. Runs refresh_availability.py -- the single existing entry point, which
     also runs the compute engine. This writes availability.json + metadata.json
     into this same folder (C:\\GRV-Availability).
  2. SANITY GATE: availability.json must parse and be non-empty, AND metadata.json
     must report EXACTLY the expected finished-good count -- len(config["fg_list"]) --
     for BOTH locations. A shortfall means a query returned partial data, so we
     ABORT and publish nothing (the last good dashboard stays live).
  3. Patches metadata.json: refreshed_by -> "scheduled". The compute engine is
     NOT modified; this is a post-run edit of its output only.
  4. Mirrors availability.json / metadata.json / config.json into local docs\\.
  5. Publishes to GitHub docs/ via the Contents API. Only files whose content
     actually changed are pushed (config.json is therefore pushed only when it
     differs from the copy already on GitHub).
  6. Writes STATUS.txt -- "OK" + timestamp on success, or the failure reason.
     On failure NOTHING is published, so the live dashboard's metadata timestamp
     stays stale. That staleness is your signal to open STATUS.txt.

This wrapper NEVER modifies refresh_availability.py or compute_availability.py,
and never deletes anything on GitHub.

DEPENDENCIES: requests (already required by refresh_availability.py). Everything
else is Python standard library. No new installs needed.

USAGE:
  python run_daily.py                 full run: refresh -> gate -> publish
  python run_daily.py --skip-refresh  reuse existing JSON (test publish only)
  python run_daily.py --no-push       run + gate + local mirror, skip GitHub push

Combine flags for a no-network dry run of just the gate:
  python run_daily.py --skip-refresh --no-push
"""

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Paths (everything is resolved relative to THIS file's folder)
# ---------------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REFRESH     = os.path.join(SCRIPT_DIR, "refresh_availability.py")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
AVAIL_FILE  = os.path.join(SCRIPT_DIR, "availability.json")
META_FILE   = os.path.join(SCRIPT_DIR, "metadata.json")
DOCS_DIR    = os.path.join(SCRIPT_DIR, "docs")
GITHUB_CFG  = os.path.join(SCRIPT_DIR, "github.json")
STATUS_FILE = os.path.join(SCRIPT_DIR, "STATUS.txt")
LOG_FILE    = os.path.join(SCRIPT_DIR, "run_daily.log")

# Files published to GitHub docs/. Unchanged files are skipped automatically,
# so config.json only generates a commit when the analysis domain changes.
PUBLISH_FILES = ["availability.json", "metadata.json", "config.json"]

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Logging (rotating local log + console)
# ---------------------------------------------------------------------------
def setup_logging():
    logger = logging.getLogger("grv_daily")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# STATUS file + controlled failure
# ---------------------------------------------------------------------------
def write_status(ok, message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = "OK" if ok else "FAILED"
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(f"{state}\n")
            f.write(f"timestamp: {stamp}\n")
            f.write(f"detail: {message}\n")
    except Exception as e:
        log.error("Could not write STATUS.txt: %s", e)
    log.info("STATUS -> %s (%s)", state, message)


def fail(message):
    """Log the reason, record STATUS=FAILED, and stop. Nothing is published."""
    log.error(message)
    write_status(False, message)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Standard git blob hash -- lets us compare a local file against the copy on
# GitHub WITHOUT downloading remote content (works for files of any size,
# which matters because availability.json is several MB).
# ---------------------------------------------------------------------------
def git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


# ---------------------------------------------------------------------------
# Step 1 -- run the existing refresh (single entry point)
# ---------------------------------------------------------------------------
def run_refresh():
    log.info("Running refresh_availability.py ...")
    if not os.path.exists(REFRESH):
        fail(f"refresh_availability.py not found at {REFRESH}")

    # Force UTF-8 in the child so its arrow/box-drawing output does not crash
    # print() when stdout is a pipe (as it is under Task Scheduler).
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.run(
            [sys.executable, REFRESH],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
    except Exception as e:
        fail(f"Could not launch refresh_availability.py: {e}")

    for line in (proc.stdout or "").splitlines():
        log.info("  [refresh] %s", line)

    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines():
            log.error("  [refresh] %s", line)
        fail(f"refresh_availability.py exited with code {proc.returncode}")

    log.info("Refresh completed.")


# ---------------------------------------------------------------------------
# Step 2 -- sanity gate (exact FG-count match in both locations)
# ---------------------------------------------------------------------------
def sanity_gate():
    log.info("Running sanity gate ...")

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        fail(f"Could not read config.json: {e}")

    expected = len(config.get("fg_list", []))
    if expected == 0:
        fail("config.json fg_list is empty -- refusing to publish.")

    try:
        with open(AVAIL_FILE, encoding="utf-8") as f:
            avail = json.load(f)
    except Exception as e:
        fail(f"availability.json missing or unparseable: {e}")
    if not avail:
        fail("availability.json is empty -- refusing to publish.")

    try:
        with open(META_FILE, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        fail(f"metadata.json missing or unparseable: {e}")

    v = meta.get("sku_count_verrayes")
    u = meta.get("sku_count_usa3pl")
    log.info("  expected FG count: %s", expected)
    log.info("  verrayes: %s | usa3pl: %s", v, u)

    if v != expected or u != expected:
        fail(
            f"SKU-count mismatch -- expected {expected} in both locations, "
            f"got verrayes={v}, usa3pl={u}. Likely a partial query result. "
            f"Not publishing; last good dashboard stays live."
        )

    log.info("Sanity gate passed.")


# ---------------------------------------------------------------------------
# Step 3 -- patch refreshed_by (engine output only; engine itself untouched)
# ---------------------------------------------------------------------------
def patch_refreshed_by():
    try:
        with open(META_FILE, encoding="utf-8") as f:
            meta = json.load(f)
        meta["refreshed_by"] = "scheduled"
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        log.info('Patched metadata.json: refreshed_by = "scheduled"')
    except Exception as e:
        fail(f"Could not patch metadata.json: {e}")


# ---------------------------------------------------------------------------
# Step 4 -- mirror into local docs\
# ---------------------------------------------------------------------------
def mirror_to_docs():
    os.makedirs(DOCS_DIR, exist_ok=True)
    for name in PUBLISH_FILES:
        src = os.path.join(SCRIPT_DIR, name)
        dst = os.path.join(DOCS_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            log.info("Mirrored %s -> docs\\", name)
        else:
            log.warning("Cannot mirror %s (not found)", name)


# ---------------------------------------------------------------------------
# Step 5 -- publish to GitHub via the Contents API
# ---------------------------------------------------------------------------
def load_github_cfg():
    if not os.path.exists(GITHUB_CFG):
        fail(f"{GITHUB_CFG} not found. See AUTOMATION_SETUP.md.")
    try:
        with open(GITHUB_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        fail(f"Could not read github.json: {e}")
    for k in ("token", "owner", "repo", "branch", "docs_path"):
        if not cfg.get(k):
            fail(f"'{k}' missing from github.json")
    if cfg["token"].startswith("REPLACE_"):
        fail("github.json still contains the placeholder token. Paste your PAT.")
    return cfg


def gh_headers(token):
    # Header set per GitHub REST API version 2022-11-28. If GitHub changes the
    # contract, verify against the current docs at docs.github.com.
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def remote_shas(requests_mod, cfg):
    """One directory listing of docs/ -> {filename: blob sha}. Listing returns
    the sha for every file without fetching content, so it is not affected by
    the API's per-file content size limit."""
    url = f"{GITHUB_API}/repos/{cfg['owner']}/{cfg['repo']}/contents/{cfg['docs_path']}"
    r = requests_mod.get(url, headers=gh_headers(cfg["token"]),
                         params={"ref": cfg["branch"]}, timeout=60)
    if r.status_code == 404:
        return {}
    if r.status_code != 200:
        fail(f"GitHub listing failed: {r.status_code} {r.text[:300]}")
    return {it["name"]: it["sha"] for it in r.json() if it.get("type") == "file"}


def publish(requests_mod, cfg):
    shas = remote_shas(requests_mod, cfg)
    pushed, skipped = [], []
    for name in PUBLISH_FILES:
        local = os.path.join(SCRIPT_DIR, name)
        if not os.path.exists(local):
            log.warning("Skipping %s (not found locally)", name)
            continue
        with open(local, "rb") as f:
            data = f.read()

        if shas.get(name) == git_blob_sha(data):
            skipped.append(name)
            log.info("Unchanged, not pushing: %s", name)
            continue

        body = {
            "message": f"Automated refresh {datetime.now():%Y-%m-%d %H:%M}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": cfg["branch"],
        }
        if name in shas:                 # update needs the current sha
            body["sha"] = shas[name]
        url = (f"{GITHUB_API}/repos/{cfg['owner']}/{cfg['repo']}"
               f"/contents/{cfg['docs_path']}/{name}")
        r = requests_mod.put(url, headers=gh_headers(cfg["token"]),
                             json=body, timeout=120)
        if r.status_code not in (200, 201):
            fail(f"GitHub push failed for {name}: {r.status_code} {r.text[:300]}")
        pushed.append(name)
        log.info("Pushed: %s", name)
    return pushed, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    skip_refresh = "--skip-refresh" in args
    no_push      = "--no-push" in args

    log.info("=" * 60)
    log.info("GRV daily automation start")
    log.info("=" * 60)

    try:
        import requests
    except ImportError:
        fail("The 'requests' package is not installed (pip install requests).")

    if skip_refresh:
        log.info("--skip-refresh: reusing existing availability.json / metadata.json")
    else:
        run_refresh()

    sanity_gate()
    patch_refreshed_by()
    mirror_to_docs()

    if no_push:
        write_status(True, "Local run OK; GitHub push skipped (--no-push).")
        log.info("Done (no push).")
        return

    cfg = load_github_cfg()
    pushed, skipped = publish(requests, cfg)
    summary = (f"Published: {', '.join(pushed) or 'none'}; "
               f"unchanged: {', '.join(skipped) or 'none'}.")
    write_status(True, summary)
    log.info("Done. %s", summary)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                            # fail() already recorded STATUS
    except Exception as e:
        log.error("Unhandled error:\n%s", traceback.format_exc())
        write_status(False, f"Unhandled error: {e}")
        sys.exit(1)
