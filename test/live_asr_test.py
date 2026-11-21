#!/usr/bin/env python3
"""
Offline test for `whosaid live --asr whisper|parakeet` (issue #235): flag and env
parsing, which transcriber gets built with which model, the workspace vocabulary
feeding Parakeet's word boost, Parakeet tokens turned into Whisper-shaped segments
with word times, the missing-package error, and the launcher adding parakeet-mlx to
the uv env only when Parakeet is picked. parakeet_mlx and mlx are stand-ins; no
model, no audio playback.

Run: uv run --with numpy python test/live_asr_test.py
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)


ENV_KEYS = ("WHOSAID_LIVE_ASR", "WHOSAID_LIVE_MODEL", "WHOSAID_LIVE_PARAKEET_MODEL", "WHOSAID_LIVE_BOOST",
            "WHOSAID_LIVE_BOOST_WEIGHT", "WHOSAID_WORKSPACE")


def parsed(argv, **env):
    """The run namespace main() hands cmd_run, under exactly `env` for the live vars."""
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    os.environ.update(env)
    got = {}
    real = live.cmd_run
    live.cmd_run = lambda a: got.setdefault("a", a) and 0
    try:
        live.main(argv)
    finally:
        live.cmd_run = real
        for k in ENV_KEYS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]
    return got["a"]


def exits(argv, **env) -> bool:
    try:
        parsed(argv, **env)
    except SystemExit as e:
        return e.code not in (0, None)
    return False


# ---- flag and env parsing ----
a = parsed(["--once"])
check(a.asr == "whisper", "default backend is whisper")
check(live.asr_model(a) == live.DEFAULT_MODEL, "default whisper model is turbo")
a = parsed(["--once"], WHOSAID_LIVE_MODEL="org/whisper-x")
check(live.asr_model(a) == "org/whisper-x", "WHOSAID_LIVE_MODEL still picks the whisper model")
a = parsed(["--asr", "parakeet"], WHOSAID_LIVE_MODEL="org/whisper-x")
check(a.asr == "parakeet", "--asr parakeet")
check(live.asr_model(a) == live.DEFAULT_PARAKEET_MODEL,
      "a whisper WHOSAID_LIVE_MODEL never reaches parakeet")
check(live.DEFAULT_PARAKEET_MODEL == "mlx-community/parakeet-tdt-0.6b-v2", "parakeet default is tdt v2")
a = parsed(["--asr=parakeet"], WHOSAID_LIVE_PARAKEET_MODEL="org/parakeet-y")
check(live.asr_model(a) == "org/parakeet-y", "WHOSAID_LIVE_PARAKEET_MODEL picks the parakeet model")
a = parsed(["--asr", "parakeet", "--model", "org/parakeet-z"], WHOSAID_LIVE_PARAKEET_MODEL="org/parakeet-y")
check(live.asr_model(a) == "org/parakeet-z", "an explicit --model is the chosen backend's model")
a = parsed(["--model", "org/whisper-q"])
check(live.asr_model(a) == "org/whisper-q", "--model alone is still the whisper model")
a = parsed([], WHOSAID_LIVE_ASR="parakeet")
check(a.asr == "parakeet", "WHOSAID_LIVE_ASR=parakeet")
a = parsed(["--asr", "whisper"], WHOSAID_LIVE_ASR="parakeet")
check(a.asr == "whisper", "the flag beats the env")
check(exits([], WHOSAID_LIVE_ASR="parakeat"), "a typo in WHOSAID_LIVE_ASR is an error, not whisper")
check(exits(["--asr", "nemo"]), "an unknown --asr is an error")
check(exits(["--asr", "parakeet", "--boost-weight", "0"]), "--boost-weight must be > 0")
check(exits([], WHOSAID_LIVE_BOOST_WEIGHT="x"), "a bad WHOSAID_LIVE_BOOST_WEIGHT is an error")

a = parsed(["--asr", "parakeet"])
check(a.boost is True and a.boost_weight == 1.0, "boost on at weight 1.0 by default")
check(parsed(["--asr", "parakeet", "--no-boost"]).boost is False, "--no-boost")
check(parsed(["--asr", "parakeet"], WHOSAID_LIVE_BOOST="0").boost is False, "WHOSAID_LIVE_BOOST=0")
check(parsed(["--asr", "parakeet"], WHOSAID_LIVE_BOOST_WEIGHT="2").boost_weight == 2.0, "boost weight env")

# ---- vocabulary: workspace [transcribe] vocabulary + --vocabulary ----
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "whosaid.toml").write_text('[transcribe]\nvocabulary = ["Kubernetes", "blob store lib"]\n')
    a = parsed(["--asr", "parakeet", "--vocabulary", "Camunda", "--vocabulary", "Kubernetes"], WHOSAID_WORKSPACE=d)
    check(live.live_vocabulary(a) == ["Kubernetes", "blob store lib", "Camunda"],
          "workspace vocabulary then --vocabulary, deduped, from $WHOSAID_WORKSPACE")
    a = parsed(["--asr", "parakeet", "--ws", d])
    check(live.live_vocabulary(a) == ["Kubernetes", "blob store lib"], "--ws names the workspace")
    (Path(d) / "whosaid.toml").write_text('[transcribe]\nvocabulary = "oops"\n')
    a = parsed(["--asr", "parakeet", "--ws", d, "--vocabulary", "Camunda"])
    check(live.live_vocabulary(a) == ["Camunda"], "a broken [transcribe] is logged and skipped, flags still count")
check(live.live_vocabulary(parsed(["--asr", "parakeet"])) == [], "no workspace, no flags: no vocabulary")

# ---- backend selection: make_transcriber builds the right one ----
built = []
real_w, real_p = live.whisper_transcriber, live.parakeet_transcriber
live.whisper_transcriber = lambda model, words=False: built.append(("whisper", model, words)) or "W"
live.parakeet_transcriber = lambda model, terms=(), boost=True, weight=1.0: \
    built.append(("parakeet", model, list(terms), boost, weight)) or "P"
try:
    check(live.make_transcriber(parsed(["--split-words"])) == "W", "whisper by default")
    check(built[-1] == ("whisper", live.DEFAULT_MODEL, True), "whisper gets its model and --split-words")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "whosaid.toml").write_text('[transcribe]\nvocabulary = ["Kubernetes"]\n')
        check(live.make_transcriber(parsed(["--asr", "parakeet", "--ws", d])) == "P", "parakeet when asked")
        check(built[-1] == ("parakeet", live.DEFAULT_PARAKEET_MODEL, ["Kubernetes"], True, 1.0),
              "parakeet gets its model, the vocabulary and the boost settings")
        live.make_transcriber(parsed(["--asr", "parakeet", "--ws", d, "--no-boost", "--boost-weight", "1.5"]))
        check(built[-1][3:] == (False, 1.5), "--no-boost reaches the transcriber")
finally:
    live.whisper_transcriber, live.parakeet_transcriber = real_w, real_p

# ---- the parakeet transcriber against a stand-in parakeet_mlx ----
calls = {}


class Tok:
    def __init__(self, text, start, dur, conf=0.9):
        self.text, self.start, self.duration, self.confidence = text, start, dur, conf
        self.end = start + dur


class Result:
    def __init__(self, tokens):
        self.tokens = tokens
        self.text = "".join(t.text for t in tokens).strip()


class Greedy:
    pass


class Beam:
    def __init__(self, beam_size=5, booster=None, **kw):
        self.beam_size, self.booster = beam_size, booster


class DecodingConfig:
    def __init__(self, decoding=None, **kw):
        self.decoding = decoding


class Model:
    vocabulary = ["<vocab>"]
    preprocessor_config = "pre"

    def generate(self, mel, decoding_config=None):
        calls.setdefault("generate", []).append((mel, decoding_config))
        return [Result([Tok(" Hello", 0.10, 0.2), Tok(" there", 0.32, 0.2), Tok(".", 0.52, 0.04),
                        Tok(" Ku", 1.50, 0.1), Tok("ber", 1.60, 0.1), Tok("netes", 1.70, 0.2),
                        Tok(" rocks", 1.92, 0.3, 0.4)])]


def from_pretrained(path, **kw):
    calls["model"] = path
    return Model()


boost_mod = types.SimpleNamespace(
    words=lambda table: calls.__setitem__("words", dict(table)) or ("words", tuple(table)),
    lift=lambda m, vocab: calls.__setitem__("lift", (m, vocab)) or "BOOSTER")
pm = types.ModuleType("parakeet_mlx")
pm.from_pretrained, pm.DecodingConfig, pm.Beam, pm.Greedy, pm.boost = \
    from_pretrained, DecodingConfig, Beam, Greedy, boost_mod
audio = types.ModuleType("parakeet_mlx.audio")
audio.get_logmel = lambda x, cfg: ("mel", len(x), cfg)
mlx = types.ModuleType("mlx")
mxc = types.ModuleType("mlx.core")
mxc.array = lambda x: x
mlx.core = mxc
sys.modules.update({"parakeet_mlx": pm, "parakeet_mlx.audio": audio, "mlx": mlx, "mlx.core": mxc})

real_resolve = live.resolve_model
live.resolve_model = lambda m: f"/cache/{m}"
try:
    run = live.parakeet_transcriber("org/p", ["Kubernetes", "Camunda"], boost=True, weight=1.0)
    check(calls["model"] == "/cache/org/p", "the model comes through resolve_model (cache first)")
    check(calls["words"] == {"Kubernetes": 1.0, "Camunda": 1.0}, "every term boosted at the weight")
    check(calls["lift"][1] == ["<vocab>"], "boost lifted onto the model's vocabulary")
    segs = run(np.full(3 * live.RATE, 0.1, dtype=np.float32))
    dec = calls["generate"][-1][1].decoding
    check(isinstance(dec, Beam) and dec.beam_size == live.BOOST_BEAM and dec.booster == "BOOSTER",
          "boosting decodes with beam search and the booster")
    check(calls["generate"][-1][0] == ("mel", 3 * live.RATE, "pre"), "samples go in as a log-mel")
    check([s["text"] for s in segs] == ["Hello there.", "Kubernetes rocks"],
          "tokens merge into words, a 0.6 s pause starts a new segment")
    w = segs[1]["words"]
    check([x["word"] for x in w] == [" Kubernetes", " rocks"] and w[0]["start"] == 1.5 and w[0]["end"] == 1.9,
          "words carry Whisper's leading space and their token span")
    check(abs(w[1]["probability"] - 0.4) < 1e-9, "word probability is its lowest token confidence")
    check(segs[1]["start"] == 1.5 and segs[1]["end"] == 2.22, "a segment spans its words")
    check(all(live.keep_segment(s) for s in segs), "parakeet segments pass the live keep filter")
    parts = live.split_words(segs[0], 0.4)
    check(parts is not None and parts[0][2] == "Hello" and parts[1][2] == "there.",
          "parakeet word times let a speaker change split a segment")
    n = len(calls["generate"])
    check(run(np.zeros(0, dtype=np.float32)) == [] and len(calls["generate"]) == n, "an empty slice is not decoded")
    check(run(np.zeros(100, dtype=np.float32)) == [], "a slice too short for a frame returns no segments")

    live.parakeet_transcriber("org/p", ["Kubernetes"], boost=False)
    run2 = live.parakeet_transcriber("org/p", [])
    run2(np.full(live.RATE, 0.1, dtype=np.float32))
    check(isinstance(calls["generate"][-1][1].decoding, Greedy), "no terms: greedy decoding (the fast path)")
    calls.pop("words", None)
    run3 = live.parakeet_transcriber("org/p", ["Kubernetes"], boost=False)
    run3(np.full(live.RATE, 0.1, dtype=np.float32))
    check("words" not in calls and isinstance(calls["generate"][-1][1].decoding, Greedy),
          "--no-boost: terms are ignored, greedy decoding")
finally:
    live.resolve_model = real_resolve

# ---- parakeet-mlx missing: a clear error, not a traceback ----
for k in ("parakeet_mlx", "parakeet_mlx.audio"):
    sys.modules[k] = None  # import fails like a missing package
try:
    live.parakeet_transcriber("org/p", [])
    check(False, "missing parakeet-mlx should raise")
except live.AsrUnavailable as e:
    check("whosaid[parakeet]" in str(e) and "parakeet-mlx" in str(e), "the error names the extra to install")
real_mt = live.make_transcriber


def boom(a):
    raise live.AsrUnavailable("parakeet-mlx is not installed")


live.make_transcriber = boom
try:
    rc = live.cmd_run(parsed(["--asr", "parakeet", "--once"]))
    check(rc == 2, "cmd_run exits 2 when the backend can't load")
finally:
    live.make_transcriber = real_mt

# ---- the launcher puts parakeet-mlx in the uv env only for parakeet ----
with tempfile.TemporaryDirectory() as d:
    fake = Path(d) / "uv"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS and k != "WHOSAID_PARAKEET_SPEC"}
    env["PATH"] = f"{d}:{env['PATH']}"

    def launched(*args, **extra):
        r = subprocess.run([str(ROOT / "whosaid"), "live", *args], env={**env, **extra},
                           capture_output=True, text=True, timeout=60)
        return r.stdout.splitlines()

    out = launched("--once")
    check("mlx-whisper" in out and not any(x.startswith("parakeet-mlx") for x in out),
          "whisper run: no parakeet-mlx in the env")
    out = launched("run", "--asr", "parakeet")
    check("parakeet-mlx==0.7.0" in out and "mlx-whisper" in out and out[-3:] == ["run", "--asr", "parakeet"],
          "--asr parakeet adds parakeet-mlx and passes the args through")
    check("parakeet-mlx==0.7.0" in launched("--asr=parakeet"), "--asr=parakeet adds parakeet-mlx")
    check("parakeet-mlx==0.7.0" in launched(WHOSAID_LIVE_ASR="parakeet"), "WHOSAID_LIVE_ASR adds parakeet-mlx")
    check("parakeet-mlx==0.7.0" not in launched("--asr", "whisper", WHOSAID_LIVE_ASR="parakeet"),
          "the flag beats the env in the launcher too")
    check("parakeet-mlx==9" in launched("--asr", "parakeet", WHOSAID_PARAKEET_SPEC="parakeet-mlx==9"),
          "WHOSAID_PARAKEET_SPEC overrides the pin")

print(f"PASS: live_asr_test ({CHECKS} checks)")
