#!/usr/bin/env python3
"""
Offline test for the realistic-roster options of test/diarize_eval/render.py
(GitHub issue #80): --voices-file / --lines-file / --max-chars / --out / --dry-run,
the per-voice `instructions` override and the `roster` passthrough into pool.json,
plus lines_realistic.json / voices_realistic.json themselves.

No network: urllib is monkeypatched to raise, and http_post is replaced by a recorder.

Run:
    uv run --with numpy --with mcp python test/render_options_test.py
"""

import array
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

EVAL = Path(__file__).resolve().parent / "diarize_eval"
sys.path.insert(0, str(EVAL))
import render  # noqa: E402

LINES_FILE = EVAL / "lines_realistic.json"
VOICES_FILE = EVAL / "voices_realistic.json"
lines_doc = json.loads(LINES_FILE.read_text())
voices_doc = json.loads(VOICES_FILE.read_text())
LINES, ENROLL = lines_doc["lines"], lines_doc["enroll"]
VOICES = voices_doc["voices"]


def wc(s):
    return len(s.split())


def mix(ls):
    out = {}
    for l in ls:
        out[l["kind"]] = out.get(l["kind"], 0) + 1
    return {k: v / float(len(ls)) for k, v in out.items()}


# ---- lines_realistic.json -------------------------------------------------
assert lines_doc["schema"] == 1
assert 290 <= len(LINES) <= 310, len(LINES)
ids = [l["id"] for l in LINES]
assert len(ids) == len(set(ids)), "duplicate line ids"
assert len({l["text"] for l in LINES}) == len(LINES), "duplicate texts"
for l in LINES:
    assert set(l) == {"id", "kind", "sub", "text"}, l
    assert l["kind"] in ("short", "medium", "long"), l  # kinds build.py/render.py know
    assert l["sub"] in ("backchannel", "phrase", "medium", "long"), l
    assert l["text"].strip() == l["text"] and l["text"], l
subs = {}
for l in LINES:
    subs[l["sub"]] = subs.get(l["sub"], 0) + 1
assert subs["backchannel"] == 80 and subs["phrase"] >= 75 and subs["medium"] >= 85 and subs["long"] >= 45, subs
assert all(l["kind"] == "short" for l in LINES if l["sub"] in ("backchannel", "phrase"))
assert len(ENROLL) == 4
for e in ENROLL:
    assert 140 <= wc(e["text"]) <= 180, (e["id"], wc(e["text"]))
assert not ({e["id"] for e in ENROLL} & set(ids))

# ---- voices_realistic.json ------------------------------------------------
assert voices_doc["schema"] == 1 and len(VOICES) == 12
el = [v for v in VOICES if v["vendor"] == "elevenlabs"]
oa = [v for v in VOICES if v["vendor"] == "openai"]
assert len(el) == 6 and len(oa) == 6
assert {v["voice_id"] for v in el} == {
    "KaZlt7yMxQwped3MhCKb", "4JIEG1ARQAwbiVJGDrKb", "C3Bpz4XTJrSR4PjT2l2I",
    "kXRfttyQVklKiD5nHT95", "w2VczpnpJO48HDyBg1xV", "wdJgf7VyQ9YzWyl0KXyk"}
assert [v["id"] for v in oa] == ["oa-in-" + n for n in ("ash", "ballad", "echo", "onyx", "verse", "cedar")]
for v in VOICES:
    assert v["roster"] == "in-m" and v["gender"] == "male" and v["accent"] == "indian", v
for v in oa:
    assert "Indian" in v["instructions"], v
assert len({v["instructions"] for v in oa}) == 6, "instructions must differ per voice"
assert len({v["id"] for v in VOICES}) == 12

# ---- subset selector ------------------------------------------------------
MAX = 9000
for v in VOICES:
    e = render.pick_enroll(v, ENROLL)
    a = render.pick_lines_budget(v, LINES, MAX, len(e["text"]))
    b = render.pick_lines_budget(v, LINES, MAX, len(e["text"]))
    assert [l["id"] for l in a] == [l["id"] for l in b], "not deterministic"
    chars = sum(len(l["text"]) for l in a) + len(e["text"])
    assert chars <= MAX, (v["id"], chars)
    assert chars >= MAX * 0.85, (v["id"], chars)  # budget actually used
    full, sub = mix(LINES), mix(a)
    for k in full:
        assert abs(full[k] - sub.get(k, 0)) <= 0.10, (v["id"], k, full[k], sub.get(k))
    sfull = {}
    for l in LINES:
        sfull[l["sub"]] = sfull.get(l["sub"], 0) + 1
    ssub = {}
    for l in a:
        ssub[l["sub"]] = ssub.get(l["sub"], 0) + 1
    for k in sfull:
        assert abs(sfull[k] / len(LINES) - ssub.get(k, 0) / len(a)) <= 0.10, (v["id"], k)
    assert len({l["id"] for l in a}) == len(a)
va, vb = VOICES[0], VOICES[1]
assert ([l["id"] for l in render.pick_lines_budget(va, LINES, MAX, 800)]
        != [l["id"] for l in render.pick_lines_budget(vb, LINES, MAX, 800)]), "voices must differ"
# a bigger cap than the whole file takes everything; a tiny cap takes (almost) nothing
assert len(render.pick_lines_budget(va, LINES, 10 ** 9, 800)) == len(LINES)
assert sum(len(l["text"]) for l in render.pick_lines_budget(va, LINES, 2000, 800)) <= 1200

# enrollment is always among the jobs, and jobs obey max_chars per voice
jobs = render.build_jobs(VOICES, LINES, ENROLL, lambda v: {}, max_chars=MAX)
for v in VOICES:
    vj = [j for j in jobs if j["voice"] is v]
    assert sum(1 for j in vj if j["kind"] == "enroll") == 1
    assert sum(len(j["text"]) for j in vj) <= MAX

# ---- dry-run: no network, expected voices printed -------------------------
def boom(*a, **k):
    raise AssertionError("network call attempted")


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = boom
real_http_post = render.http_post
render.http_post = boom
try:
    tmp = tempfile.mkdtemp(prefix="render-options-")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = render.main(["--voices-file", str(VOICES_FILE), "--lines-file", str(LINES_FILE),
                          "--max-chars", str(MAX), "--out", tmp, "--dry-run"])
    out = buf.getvalue()
    assert rc == 0, rc
    for v in VOICES:
        assert "%s (%s)" % (v["id"], v["vendor"]) in out, v["id"]
    assert "cache: " + tmp in out
    assert not os.listdir(tmp), "dry-run wrote into the cache dir"
    # per-voice totals printed in the dry-run agree with the selector
    for v in VOICES:
        vj = [j for j in jobs if j["voice"]["id"] == v["id"]]
        want = "chars=%d " % sum(len(j["text"]) for j in vj)
        line = [ln for ln in out.splitlines() if ln.startswith(v["id"] + " (")][0]
        assert want in line, (want, line)
    # an unknown voice is still rejected
    with contextlib.redirect_stderr(io.StringIO()):
        assert render.main(["--voices-file", str(VOICES_FILE), "--lines-file", str(LINES_FILE),
                            "--voices", "nope", "--dry-run"]) == 2
    # a custom voices file may not target the default pool (non-dry-run guard)
    saved = os.environ.pop("WHOSAID_DIARIZE_EVAL_CACHE", None)
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = render.main(["--voices-file", str(VOICES_FILE), "--lines-file", str(LINES_FILE)])
        assert rc == 2 and "--out" in err.getvalue(), (rc, err.getvalue())
    finally:
        if saved is not None:
            os.environ["WHOSAID_DIARIZE_EVAL_CACHE"] = saved
    # ...nor the pool $WHOSAID_DIARIZE_EVAL_CACHE points at, passed explicitly
    envpool = tempfile.mkdtemp(prefix="render-envpool-")
    saved = os.environ.get("WHOSAID_DIARIZE_EVAL_CACHE")
    os.environ["WHOSAID_DIARIZE_EVAL_CACHE"] = envpool
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = render.main(["--voices-file", str(VOICES_FILE), "--lines-file", str(LINES_FILE),
                              "--out", envpool])
        assert rc == 2 and "--out" in err.getvalue(), (rc, err.getvalue())
        assert not os.listdir(envpool), "guard let a custom render into the env pool"
    finally:
        if saved is None:
            os.environ.pop("WHOSAID_DIARIZE_EVAL_CACHE", None)
        else:
            os.environ["WHOSAID_DIARIZE_EVAL_CACHE"] = saved
finally:
    urllib.request.urlopen = real_urlopen
    render.http_post = real_http_post

# ---- instructions override ------------------------------------------------
sent = []
render.http_post = lambda url, headers, payload: sent.append((url, payload)) or b""
os.environ["OPENAI_API_KEY"] = "test-not-a-real-key"
try:
    ov = dict(oa[0])
    render.synth_openai(ov, "Right.", {})
    assert sent[-1][1]["instructions"] == ov["instructions"], sent[-1]
    plain = {"id": "oa-x", "voice_id": "ash"}
    render.synth_openai(plain, "Right.", {})
    assert sent[-1][1]["instructions"] in render.OA_INSTRUCTIONS
    # ElevenLabs shared-library voice: synthesized by voice_id, no account lookup
    os.environ["ELEVENLABS_API_KEY"] = "test-not-a-real-key"
    render.synth_elevenlabs(el[0], "Right.", {})
    assert sent[-1][0].startswith("https://api.elevenlabs.io/v1/text-to-speech/" + el[0]["voice_id"]), sent[-1][0]
    assert len(sent) == 3
finally:
    render.http_post = real_http_post
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("ELEVENLABS_API_KEY", None)

# ---- roster passthrough into pool.json ------------------------------------
cache = tempfile.mkdtemp(prefix="render-pool-")
silence = array.array("h", [1000] * 1600)
sel = [VOICES[0], VOICES[6]]
for v in sel:
    j = [j for j in render.build_jobs([v], LINES, ENROLL, lambda v: {}, max_chars=3000)]
    clip = [x for x in j if x["kind"] != "enroll"][0]
    render.write_wav_atomic(os.path.join(cache, clip["rel"]), silence)
    en = [x for x in j if x["kind"] == "enroll"][0]
    render.write_wav_atomic(os.path.join(cache, en["rel"]), silence)
pool = render.write_pool(cache, VOICES, LINES, ENROLL)
assert set(pool["voices"]) == {v["id"] for v in sel}
for v in sel:
    ent = pool["voices"][v["id"]]
    assert ent["roster"] == "in-m" and ent["accent"] == "indian", ent
    assert ent["clips"][0]["sub"] in ("backchannel", "phrase", "medium", "long")
assert json.load(open(os.path.join(cache, "pool.json")))["voices"][sel[0]["id"]]["roster"] == "in-m"
# a voice without `roster` (and a line without `sub`) leaves the pool entry as before
legacy_v = {"id": "x-legacy", "name": "L", "vendor": "openai", "voice_id": "ash",
            "gender": "male", "accent": "american"}
legacy_l = [{"id": "L001", "kind": "short", "text": "Yeah."}]
c2 = tempfile.mkdtemp(prefix="render-pool-")
render.write_wav_atomic(os.path.join(c2, "clips/x-legacy/L001.wav"), silence)
ent = render.write_pool(c2, [legacy_v], legacy_l, [{"id": "E1", "text": "t"}])["voices"]["x-legacy"]
assert "roster" not in ent and "sub" not in ent["clips"][0], ent

# ---- default invocation unchanged -----------------------------------------
# Digest of the default selection (voices.json x lines.json, short=3,medium=6,long=2),
# recorded from the pre-change render.py (git show 3e75d7a:test/diarize_eval/render.py).
OLD_DIGEST = "2d76b49ca14cfede1d8125ac0f31baa44add8138a6221cba3ed0bdcb29cf69c5"
dv = render.load_json("voices.json")["voices"]
dl = render.load_json("lines.json")
djobs = render.build_jobs(dv, dl["lines"], dl["enroll"],
                          lambda v: render.parse_counts("short=3,medium=6,long=2"))
got = hashlib.sha256(json.dumps([(j["voice"]["id"], j["rel"], j["line_id"], j["kind"])
                                 for j in djobs]).encode()).hexdigest()
assert got == OLD_DIGEST, got
assert len(djobs) == 408
env = os.environ.pop("WHOSAID_DIARIZE_EVAL_CACHE", None)
try:
    assert render.default_cache() == os.path.expanduser("~/.cache/whosaid/diarize-eval")
finally:
    if env is not None:
        os.environ["WHOSAID_DIARIZE_EVAL_CACHE"] = env
# default dry-run prints no per-voice plan (unchanged output shape)
urllib.request.urlopen = boom
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert render.main(["--dry-run", "--cache", tempfile.mkdtemp(prefix="render-def-")]) == 0
finally:
    urllib.request.urlopen = real_urlopen
assert "cache: " not in buf.getvalue() and "dry-run: budget=45000" in buf.getvalue()

print("render_options_test: all checks passed")
