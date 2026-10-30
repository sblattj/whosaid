#!/usr/bin/env python3
"""
app_cli.py: build and install the native macOS app (`whosaid app`).

  build      `swift build -c release` in app/ with the Command Line Tools only,
             then assemble app/.build/whosaid.app (Info.plist, resources) and
             sign it ad hoc. --debug builds the debug configuration.
  install    build, then copy whosaid.app into ~/Applications (or
             $WHOSAID_APP_DIR / --dest). The app finds this checkout's
             `whosaid` (WHOSAID_BIN baked into its defaults at install).
  open       open the installed app (or the built one with --built).
  uninstall  quit it and remove the installed copy.
  test       `swift test` with the flags swift-testing needs under the CLT.

The app never records and never holds the Screen & System Audio Recording
grant: `Whosaid Call Recorder.app` keeps that. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_SRC = REPO / "app"
BUNDLE_ID = "com.whosaid.app"
APP_NAME = "whosaid.app"
EXECUTABLE = "WhosaidApp"
CLT_DEV = Path("/Library/Developer/CommandLineTools/Library/Developer")
BUILD_TIMEOUT = 900


def log(msg: str) -> None:
    print(f"whosaid: {msg}", file=sys.stderr, flush=True)


def version() -> str:
    for line in (REPO / "whosaid").read_text().splitlines():
        if line.startswith("WHOSAID_VERSION="):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def info_plist(ver: str) -> dict:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "whosaid",
        "CFBundleDisplayName": "whosaid",
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": ver,
        "CFBundleVersion": ver,
        "CFBundleIconFile": "AppIcon",  # app/Icon/AppIcon.icns, copied into Contents/Resources
        "LSMinimumSystemVersion": "15.0",
        "LSUIElement": True,  # a menu bar app; the main window opens at launch and from the menu
        "NSHumanReadableCopyright": "MIT",
    }


def swift_env() -> dict:
    env = dict(os.environ)
    env.setdefault("WHOSAID_BIN", str(REPO / "whosaid"))
    return env


def built_app(config: str) -> Path:
    return APP_SRC / ".build" / f"{APP_NAME}" if config == "release" else APP_SRC / ".build" / f"debug-{APP_NAME}"


def cmd_build(a: argparse.Namespace) -> int:
    if shutil.which("swift") is None:
        log("app build: `swift` not found. Install the Command Line Tools: xcode-select --install")
        return 1
    config = "debug" if a.debug else "release"
    r = subprocess.run(["swift", "build", "-c", config, "--package-path", str(APP_SRC)],
                       timeout=BUILD_TIMEOUT, env=swift_env())
    if r.returncode:
        log(f"app build: swift build failed (exit {r.returncode})")
        return r.returncode
    show = subprocess.run(["swift", "build", "-c", config, "--package-path", str(APP_SRC), "--show-bin-path"],
                          capture_output=True, text=True, timeout=120, env=swift_env())
    binpath = show.stdout.strip()
    if show.returncode or not (Path(binpath) / EXECUTABLE).is_file():
        log(f"app build: no built binary at {binpath or '(swift build --show-bin-path failed)'}")
        return show.returncode or 1
    out = built_app(config)
    if out.exists():
        shutil.rmtree(out)
    (out / "Contents" / "MacOS").mkdir(parents=True)
    (out / "Contents" / "Resources").mkdir()
    shutil.copy2(Path(binpath) / EXECUTABLE, out / "Contents" / "MacOS" / EXECUTABLE)
    icon = APP_SRC / "Icon" / "AppIcon.icns"
    if icon.is_file():
        shutil.copy2(icon, out / "Contents" / "Resources" / icon.name)
    for res in Path(binpath).glob("*.bundle"):
        shutil.copytree(res, out / "Contents" / "Resources" / res.name)
    with open(out / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info_plist(version()), fh)
    sign = subprocess.run(["codesign", "--force", "--sign", "-", str(out)], capture_output=True, text=True)
    if sign.returncode:
        log(f"app build: codesign failed: {sign.stderr.strip()}")
        return 1
    print(out)
    return 0


def dest_dir(a: argparse.Namespace) -> Path:
    return Path(a.dest or os.environ.get("WHOSAID_APP_DIR") or Path.home() / "Applications").expanduser()


def cmd_install(a: argparse.Namespace) -> int:
    if not a.no_build:
        rc = cmd_build(a)
        if rc:
            return rc
    src = built_app("debug" if a.debug else "release")
    if not src.exists():
        log(f"app install: {src} missing; run `whosaid app build`")
        return 1
    dest = dest_dir(a) / APP_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    quit_app()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True)
    log(f"installed {dest}")
    if not a.no_defaults:
        subprocess.run(["defaults", "write", BUNDLE_ID, "whosaidPath", str(REPO / "whosaid")], capture_output=True)
    print(dest)
    return 0


def quit_app() -> None:
    subprocess.run(["pkill", "-TERM", "-f", f"{APP_NAME}/Contents/MacOS/{EXECUTABLE}"], capture_output=True)


def cmd_open(a: argparse.Namespace) -> int:
    target = built_app("release") if a.built else dest_dir(a) / APP_NAME
    if not target.exists():
        log(f"app open: {target} missing; run `whosaid app install` (or `build` and --built)")
        return 1
    return subprocess.run(["open", str(target)]).returncode


def cmd_uninstall(a: argparse.Namespace) -> int:
    dest = dest_dir(a) / APP_NAME
    quit_app()
    if dest.exists():
        shutil.rmtree(dest)
        log(f"removed {dest}")
    else:
        log(f"not installed ({dest} missing)")
    return 0


def test_argv() -> list[str]:
    fw, lib = CLT_DEV / "Frameworks", CLT_DEV / "usr" / "lib"
    argv = ["swift", "test", "--package-path", str(APP_SRC)]
    if fw.is_dir():  # Command Line Tools only: point SwiftPM at its swift-testing copy
        argv += ["-Xswiftc", "-F", "-Xswiftc", str(fw), "-Xlinker", "-F", "-Xlinker", str(fw),
                 "-Xlinker", "-rpath", "-Xlinker", str(fw), "-Xlinker", "-rpath", "-Xlinker", str(lib)]
    return argv


def cmd_test(a: argparse.Namespace) -> int:
    env = swift_env()
    env["WHOSAID_TEST_BIN"] = str(REPO / "whosaid")
    return subprocess.run(test_argv(), timeout=BUILD_TIMEOUT, env=env).returncode


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="whosaid app")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    i = sub.add_parser("install")
    for q in (b, i):
        q.add_argument("--debug", action="store_true")
    i.add_argument("--dest", help="folder to install into (default ~/Applications or $WHOSAID_APP_DIR)")
    i.add_argument("--no-build", action="store_true", help="install the last build")
    i.add_argument("--no-defaults", action="store_true", help="do not save this checkout's whosaid path")
    o = sub.add_parser("open")
    o.add_argument("--built", action="store_true", help="open app/.build/whosaid.app instead")
    o.add_argument("--dest")
    u = sub.add_parser("uninstall")
    u.add_argument("--dest")
    sub.add_parser("test")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    return {"build": cmd_build, "install": cmd_install, "open": cmd_open,
            "uninstall": cmd_uninstall, "test": cmd_test}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
