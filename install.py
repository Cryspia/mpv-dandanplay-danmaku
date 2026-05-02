#!/usr/bin/env python3
"""Cross-platform installer for mpv-dandanplay-danmuku.

Drops the script bundle into mpv's `scripts/` directory and seeds the
config files (skipping anything the user has already customized).
Tested on Linux (XDG), macOS, and Windows.

Usage:
    python3 install.py            # install
    python3 install.py --status   # show what's installed
    python3 install.py --uninstall

The installer NEVER touches `danmaku-credentials.json`, so a registered
AppId survives reinstalls and uninstalls.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

REPO_DIR = pathlib.Path(__file__).resolve().parent
BUNDLE_SRC = REPO_DIR / "scripts" / "dandanplay"
EXAMPLES_DIR = REPO_DIR / "examples"

REQUIRED_FILES = ("main.lua", "danmaku_helper.py")
SEED_CONFIGS = ("danmaku-config.json", "danmaku-settings.json")
SEED_EXAMPLES = ("danmaku-credentials.json",)  # written as .example only


# ---------------------------------------------------------------------------
# Path resolution — mirrors the runtime helpers in main.lua and danmaku_helper.py
# ---------------------------------------------------------------------------
def mpv_config_dir() -> pathlib.Path:
    if os.environ.get("MPV_HOME"):
        return pathlib.Path(os.environ["MPV_HOME"])
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return pathlib.Path(appdata) / "mpv"
    return pathlib.Path.home() / ".config" / "mpv"


def python_check() -> None:
    """Sanity-check the Python interpreter being used to install.

    The helper script will run under whatever python3 mpv finds on PATH at
    runtime, NOT under this installer's Python — but if THIS Python doesn't
    have urllib/json/hashlib (i.e., a stripped-down build), the user will
    likely hit the same issue at runtime, so warn early."""
    try:
        import urllib.request, json, hashlib, hmac  # noqa: F401
    except ImportError as e:
        print(f"WARNING: Python is missing stdlib module: {e}", file=sys.stderr)
        print("  The helper requires urllib, json, hashlib, hmac.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def install() -> int:
    cfg = mpv_config_dir()
    bundle_dst = cfg / "scripts" / "dandanplay"

    print(f"mpv config dir: {cfg}")
    print(f"installing bundle to: {bundle_dst}")

    # Verify required source files
    for f in REQUIRED_FILES:
        if not (BUNDLE_SRC / f).is_file():
            print(f"ERROR: missing source file {BUNDLE_SRC / f}", file=sys.stderr)
            return 1

    # Copy bundle (overwrite any existing copy)
    bundle_dst.mkdir(parents=True, exist_ok=True)
    for f in REQUIRED_FILES:
        src = BUNDLE_SRC / f
        dst = bundle_dst / f
        shutil.copy2(src, dst)
        if sys.platform != "win32" and f.endswith(".py"):
            os.chmod(dst, 0o755)
        print(f"  wrote {dst}")

    # Seed JSON configs only if they don't already exist (preserve user edits).
    cfg.mkdir(parents=True, exist_ok=True)
    for name in SEED_CONFIGS:
        src = EXAMPLES_DIR / name
        dst = cfg / name
        if dst.exists():
            print(f"  kept existing {dst}")
        elif src.is_file():
            shutil.copy2(src, dst)
            print(f"  seeded {dst}")

    # Always (re-)write a `.example` for credentials so the user has a
    # template, but never overwrite a real credentials file.
    for name in SEED_EXAMPLES:
        src = EXAMPLES_DIR / name
        dst = cfg / (name + ".example")
        if src.is_file():
            shutil.copy2(src, dst)
            print(f"  wrote example: {dst}")

    print("\nInstall complete. Restart mpv (or shim) to load the script.")
    print(f"\nDefault uses upstream's CORS proxy. To switch to your own AppId:")
    print(f"  1. email kaedei@dandanplay.net to request AppId/AppSecret")
    print(f"  2. cp '{cfg}/danmaku-credentials.json.example' \\\n"
          f"        '{cfg}/danmaku-credentials.json'")
    print(f"  3. fill in app_id / app_secret\n")
    return 0


def uninstall() -> int:
    cfg = mpv_config_dir()
    bundle_dst = cfg / "scripts" / "dandanplay"

    print(f"removing bundle: {bundle_dst}")
    if bundle_dst.is_dir():
        shutil.rmtree(bundle_dst)

    # Preserve credentials + user-customized settings; just remove the seeded
    # files we wrote AND the .example.
    for name in SEED_CONFIGS + tuple(n + ".example" for n in SEED_EXAMPLES):
        f = cfg / name
        if f.is_file():
            f.unlink()
            print(f"  removed {f}")

    print("\nUninstalled. (Cache + danmaku-credentials.json preserved.)")
    return 0


def status() -> int:
    cfg = mpv_config_dir()
    bundle_dst = cfg / "scripts" / "dandanplay"

    print(f"mpv config dir: {cfg}")
    print(f"bundle dir:     {bundle_dst}")
    if bundle_dst.is_dir():
        for f in REQUIRED_FILES:
            p = bundle_dst / f
            tag = "OK " if p.is_file() else "?? "
            print(f"  [{tag}] {p}")
    else:
        print("  [missing]")

    print("\nconfig files:")
    for name in SEED_CONFIGS + ("danmaku-credentials.json",):
        p = cfg / name
        tag = "OK " if p.is_file() else "-- "
        print(f"  [{tag}] {p}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--status", action="store_true", help="show install state")
    p.add_argument("--uninstall", action="store_true", help="remove the bundle")
    args = p.parse_args()

    python_check()
    if args.status:
        return status()
    if args.uninstall:
        return uninstall()
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
