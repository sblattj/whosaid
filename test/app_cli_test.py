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
check(info.get("CFBundleIconFile") == "AppIcon" and (app_cli.APP_SRC / "Icon" / "AppIcon.icns").is_file(),
      "the bundle names AppIcon and the icns is checked in")
check("NSMicrophoneUsageDescription" not in info and "NSScreenCaptureUsageDescription" not in info,
      "the app asks for no capture permission (the recorder app keeps the grant)")
argv = app_cli.test_argv()
check(argv[:2] == ["swift", "test"] and "--package-path" in argv, f"swift test argv: {argv}")
if app_cli.CLT_DEV.joinpath("Frameworks").is_dir():
    check("-rpath" in argv, "Command Line Tools: swift-testing rpath flags added")
if app_cli.CLT_TESTING_PLUGINS.is_dir():
    i = argv.index("-plugin-path")
    check(argv[i - 1] == "-Xswiftc" and argv[i + 2] == str(app_cli.CLT_TESTING_PLUGINS),
          f"Command Line Tools: the swift-testing macro plugin folder is passed: {argv}")

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

# Signing: the local certificate when the keychain has it, else ad hoc; the env wins.
real_run = app_cli.subprocess.run
def fake_security(out):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
os.environ.pop("WHOSAID_SIGN_IDENTITY", None)
app_cli.subprocess.run = fake_security('  1) ABC "Apple Development: someone"\n     1 identities found\n')
check(app_cli.sign_identity() == "-", "no local certificate: sign ad hoc")
app_cli.subprocess.run = fake_security('  1) 64712D "whosaid local signing" (CSSMERR_TP_NOT_TRUSTED)\n')
check(app_cli.sign_identity() == app_cli.SIGN_NAME, "local certificate present (untrusted is fine): sign with it")
os.environ["WHOSAID_SIGN_IDENTITY"] = "Developer ID Application: X"
check(app_cli.sign_identity() == "Developer ID Application: X", "WHOSAID_SIGN_IDENTITY overrides")
del os.environ["WHOSAID_SIGN_IDENTITY"]
check(app_cli.cmd_sign_setup(argparse.Namespace()) == 0, "sign-setup with the certificate present does nothing")
app_cli.subprocess.run = real_run
check(app_cli.parse_args(["sign-setup"]).cmd == "sign-setup", "`whosaid app sign-setup` parses")
check("extendedKeyUsage = critical, codeSigning" in app_cli.CERT_CONF, "the certificate is for code signing")

# SDK fallback (#245): the 27.2 CLT SDK has no SwiftUI macro plugin, so build and test
# try the default SDK first, then MacOSX26.5.sdk, and log which one they used.
# A fake SDK folder and a fake `swift`; nothing is built and app/.build is never touched.
import contextlib
import io

def run_fallback(sdk_dir, fails_on, sdkroot=None, argv=("swift", "build", "-c", "release")):
    """Run run_with_sdk_fallback with a fake swift that fails on SDKs whose path ends in fails_on."""
    calls, err = [], io.StringIO()
    def fake_run(a, **kw):
        if a[:2] == ["xcrun", "--sdk"]:  # the default SDK: the folder's MacOSX.sdk symlink
            return subprocess.CompletedProcess(a, 0, stdout=f"{sdk_dir / 'MacOSX.sdk'}\n", stderr="")
        calls.append((list(a), kw["env"].get("SDKROOT")))
        root = kw["env"]["SDKROOT"]
        return subprocess.CompletedProcess(a, 1 if any(os.path.realpath(root).endswith(f) for f in fails_on) else 0)
    os.environ.pop("SDKROOT", None)
    if sdkroot:
        os.environ["SDKROOT"] = sdkroot
    app_cli.subprocess.run = fake_run
    try:
        with contextlib.redirect_stderr(err):
            rc, env = app_cli.run_with_sdk_fallback("app build", list(argv), sdk_dir=sdk_dir, env={"PATH": "/usr/bin"})
    finally:
        app_cli.subprocess.run = real_run
        os.environ.pop("SDKROOT", None)
    return rc, env, calls, err.getvalue()

real_sdkroot = os.environ.pop("SDKROOT", None)
with tempfile.TemporaryDirectory() as d:
    sdks = Path(d)
    (sdks / "MacOSX27.2.sdk").mkdir()
    (sdks / "MacOSX26.5.sdk").mkdir()
    (sdks / "MacOSX.sdk").symlink_to("MacOSX27.2.sdk")
    new, old = str(sdks / "MacOSX.sdk"), str(sdks / "MacOSX26.5.sdk")

    # 1. 27.2 builds: one swift build, on 27.2, and the log says so.
    rc, env, calls, log_ = run_fallback(sdks, fails_on=())
    check(rc == 0 and [c[1] for c in calls] == [new], f"27.2 succeeds: one build on the default SDK: {calls}")
    check(env["SDKROOT"] == new and "succeeded with the macOS 27.2 SDK" in log_ and "26.5" not in log_,
          f"27.2 succeeds: logs the 27.2 SDK only: {log_}")

    # 2. 27.2 fails: a second build on 26.5, logged, and its env (for --show-bin-path) carries 26.5.
    rc, env, calls, log_ = run_fallback(sdks, fails_on=("MacOSX27.2.sdk",))
    check(rc == 0 and [c[1] for c in calls] == [new, old], f"27.2 fails: retried on 26.5: {calls}")
    check(calls[0][0] == calls[1][0], "the retry runs the same swift command")
    check(env["SDKROOT"] == old and env["PATH"] == "/usr/bin", f"the returned env keeps the base env and names 26.5: {env}")
    check("failed with the macOS 27.2 SDK" in log_ and "retrying with the macOS 26.5 SDK" in log_
          and "succeeded with the macOS 26.5 SDK" in log_, f"27.2 fails: the log names both SDKs: {log_}")

    # Both fail: the last exit code, and one line naming every SDK tried.
    rc, env, calls, log_ = run_fallback(sdks, fails_on=("MacOSX27.2.sdk", "MacOSX26.5.sdk"))
    check(rc == 1 and len(calls) == 2 and "with every SDK tried" in log_, f"both fail: rc 1 after 2 tries: {log_}")

    # $SDKROOT wins over xcrun as the first SDK.
    rc, env, calls, log_ = run_fallback(sdks, fails_on=(), sdkroot=old)
    check(rc == 0 and [c[1] for c in calls] == [old], f"$SDKROOT is the first SDK: {calls}")

    # The default already being 26.5 builds once, not twice.
    (sdks / "MacOSX.sdk").unlink()
    (sdks / "MacOSX.sdk").symlink_to("MacOSX26.5.sdk")
    rc, env, calls, log_ = run_fallback(sdks, fails_on=("MacOSX26.5.sdk",))
    check(rc == 1 and len(calls) == 1, f"default = 26.5: tried once (deduped by resolved path): {calls}")

    # 3. Neither SDK present: no swift call, a clear error naming what it looked for.
    for name in ("MacOSX.sdk",):
        (sdks / name).unlink()
    for name in ("MacOSX27.2.sdk", "MacOSX26.5.sdk"):
        (sdks / name).rmdir()
    rc, env, calls, log_ = run_fallback(sdks, fails_on=())
    check(rc == 1 and calls == [], f"neither SDK: no swift build runs: {calls}")
    check("no macOS SDK found" in log_ and str(sdks / "MacOSX26.5.sdk") in log_ and "xcode-select --install" in log_,
          f"neither SDK: the error names the SDKs it looked for: {log_}")

    # cmd_build end to end with a fake swift: the fallback SDK reaches --show-bin-path too,
    # and the bundle goes to a temp folder (never app/.build).
    (sdks / "MacOSX27.2.sdk").mkdir()
    (sdks / "MacOSX26.5.sdk").mkdir()
    (sdks / "MacOSX.sdk").symlink_to("MacOSX27.2.sdk")
    binp = sdks / "bin"
    binp.mkdir()
    (binp / app_cli.EXECUTABLE).write_text("#!/bin/sh\n")
    seen = []
    def fake_build_run(a, **kw):
        if a[:2] == ["xcrun", "--sdk"]:
            return subprocess.CompletedProcess(a, 0, stdout=f"{sdks / 'MacOSX.sdk'}\n", stderr="")
        if a[0] == "swift":
            root = kw["env"]["SDKROOT"]
            seen.append(("show" if "--show-bin-path" in a else "build", root))
            if "--show-bin-path" in a:
                return subprocess.CompletedProcess(a, 0, stdout=f"{binp}\n", stderr="")
            return subprocess.CompletedProcess(a, 1 if os.path.realpath(root).endswith("MacOSX27.2.sdk") else 0)
        return subprocess.CompletedProcess(a, 0, stdout="", stderr="")  # codesign, security
    real_built, real_sdks, real_which = app_cli.built_app, app_cli.CLT_SDKS, app_cli.shutil.which
    app_cli.built_app = lambda config: sdks / "out" / "whosaid.app"
    app_cli.CLT_SDKS = sdks
    app_cli.shutil.which = lambda name: "/usr/bin/" + name
    app_cli.subprocess.run = fake_build_run
    os.environ["WHOSAID_SIGN_IDENTITY"] = "-"
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = app_cli.cmd_build(argparse.Namespace(debug=False))
    finally:
        app_cli.subprocess.run, app_cli.built_app, app_cli.CLT_SDKS, app_cli.shutil.which = real_run, real_built, real_sdks, real_which
        del os.environ["WHOSAID_SIGN_IDENTITY"]
    check(rc == 0 and seen == [("build", str(sdks / "MacOSX.sdk")), ("build", str(sdks / "MacOSX26.5.sdk")),
                               ("show", str(sdks / "MacOSX26.5.sdk"))],
          f"cmd_build: 27.2 fails, 26.5 builds, --show-bin-path asks the 26.5 build: {seen}")
    check((sdks / "out" / "whosaid.app" / "Contents" / "MacOS" / app_cli.EXECUTABLE).is_file(),
          "cmd_build assembles the bundle from the 26.5 build")
if real_sdkroot is not None:
    os.environ["SDKROOT"] = real_sdkroot

r = subprocess.run([str(ROOT / "whosaid"), "app"], capture_output=True, text=True, timeout=60)
check(r.returncode == 0 and "whosaid app install" in r.stdout, f"bare `whosaid app` prints its help: {r.stderr}")
r = subprocess.run([str(ROOT / "whosaid"), "app", "nope"], capture_output=True, text=True, timeout=60)
check(r.returncode == 2, "an unknown subcommand is a usage error")

print(f"PASS: app_cli_test ({CHECKS} checks)")
