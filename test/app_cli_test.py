#!/usr/bin/env python3
"""
Offline test for lib/app_cli.py (`whosaid app`): the bundle layout and Info.plist,
install/uninstall into a temp folder (never ~/Applications, never `defaults`),
and the swift test flags. Builds nothing: a fake built app stands in.

Run: python3 test/app_cli_test.py
"""
import argparse
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import app_cli  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


ver = app_cli.version()
check(ver.count(".") == 2 and ver != "0.0.0", f"version read from WHOSAID_VERSION: {ver}")
info = app_cli.info_plist(ver)
check(info["CFBundleIdentifier"] == "com.whosaid.app" and info["LSUIElement"] is True
      and info["CFBundleExecutable"] == app_cli.EXECUTABLE and info["LSMinimumSystemVersion"] == "15.0",
      f"Info.plist: {info}")
check("NSMicrophoneUsageDescription" not in info and "NSScreenCaptureUsageDescription" not in info,
      "the app asks for no capture permission (the recorder app keeps the grant)")
argv = app_cli.test_argv()
check(argv[:2] == ["swift", "test"] and "--package-path" in argv, f"swift test argv: {argv}")
if app_cli.CLT_DEV.joinpath("Frameworks").is_dir():
    check("-rpath" in argv, "Command Line Tools: swift-testing rpath flags added")

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    # A fake build output where `whosaid app build` would put it.
    real_built = app_cli.built_app
    fake = d / "built" / "whosaid.app"
    (fake / "Contents" / "MacOS").mkdir(parents=True)
    (fake / "Contents" / "MacOS" / app_cli.EXECUTABLE).write_text("#!/bin/sh\n")
    with open(fake / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    app_cli.built_app = lambda config: fake
    app_cli.quit_app = lambda: None  # never touch a running copy from a test
    dest = d / "Applications"
    ns = argparse.Namespace(debug=False, no_build=True, dest=str(dest), no_defaults=True)
    check(app_cli.cmd_install(ns) == 0 and (dest / "whosaid.app" / "Contents" / "MacOS" / app_cli.EXECUTABLE).exists(),
          "install --no-build copies the bundle into --dest")
    (fake / "Contents" / "marker").write_text("v2")
    check(app_cli.cmd_install(ns) == 0 and (dest / "whosaid.app" / "Contents" / "marker").exists(),
          "a second install replaces the installed copy")
    os.environ["WHOSAID_APP_DIR"] = str(d / "EnvApps")
    check(app_cli.dest_dir(argparse.Namespace(dest=None)) == d / "EnvApps", "WHOSAID_APP_DIR sets the folder")
    del os.environ["WHOSAID_APP_DIR"]
    check(app_cli.cmd_uninstall(argparse.Namespace(dest=str(dest))) == 0 and not (dest / "whosaid.app").exists(),
          "uninstall removes it")
    check(app_cli.cmd_uninstall(argparse.Namespace(dest=str(dest))) == 0, "uninstall twice is fine")
    check(app_cli.cmd_open(argparse.Namespace(built=False, dest=str(dest))) == 1, "open with nothing installed fails")
    app_cli.built_app = real_built

r = subprocess.run([str(ROOT / "whosaid"), "app"], capture_output=True, text=True, timeout=60)
check(r.returncode == 0 and "whosaid app install" in r.stdout, f"bare `whosaid app` prints its help: {r.stderr}")
r = subprocess.run([str(ROOT / "whosaid"), "app", "nope"], capture_output=True, text=True, timeout=60)
check(r.returncode == 2, "an unknown subcommand is a usage error")

print(f"PASS: app_cli_test ({CHECKS} checks)")
