"""
Audio channel model for the realistic synthetic diarization eval (issue #80).

Real setup being imitated: a single-channel AAC voice memo recorded on a laptop
during video calls. ONE participant ("local") sits near the laptop mic; everyone
else ("remote") arrives through the call's audio path (conferencing codec, then
laptop-speaker playback re-captured by the same mic in the room). Local and
remote voices therefore have very different channel character, and a remote
voice drifts over a long meeting (bitrate, level, EQ, room).

Public API (16 kHz mono int16 everywhere; numpy arrays or array('h') accepted):

    remote_profile(rng) -> dict      sample a conferencing path (JSON-safe)
    local_profile(rng)  -> dict      near-mic path (JSON-safe)
    drift(profile, rng, strength=1.0) -> dict   perturbed copy, same codec family
    apply(samples, profile) -> int16 ndarray    same length, time-aligned (<10 ms)
    tempo(samples, factor) -> int16 ndarray     pitch-preserving, len ~= len/factor
    aac_roundtrip(samples, bitrate_k=64) -> int16 ndarray   same length, aligned

Remote chain (every random choice comes from the rng and is stored in the profile):
    codec family (weights): opus 0.6, telephone 0.25, g722 0.15
      opus      libopus, -application voip, bitrate 12..32 kbps
      telephone resample to 8 kHz + pcm_mulaw, back to 16 kHz (G.711 mu-law)
      g722      g722 wideband (64 kbps fixed)
    -> AGC-like acompressor: ratio 2..4, threshold -30..-18 dB, attack 5..20 ms,
       release 100..400 ms
    -> laptop-speaker playback EQ: highpass 150..300 Hz, lowpass 5000..7500 Hz
       (two cascaded 2-pole sections), one resonant peak 400..1500 Hz,
       +3..+8 dB, Q 1..3
    -> room pickup: synthetic RIR of exponentially decaying noise, RT60 0.2..0.5 s,
       wet share 0.15..0.45 (direct path kept at index 0 so timing is preserved),
       seed drawn from the rng and stored in the profile
    -> level offset -12..-3 dB relative to local
Local chain: highpass ~80 Hz (70..95), small-room RIR (RT60 0.08..0.2 s, wet
share 0.03..0.10), level 0 dB +/- 2.

Codec delay is compensated by measuring each stage's lag once on a fixed noise
probe (cached) and shifting the output; the result is padded/trimmed to the
input length. Heavy work runs through ffmpeg over raw s16le pipes plus a
block FFT convolution in numpy; there are no per-sample Python loops.
"""

import copy
import os
import random
import subprocess

import numpy as np

SR = 16000
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

CODECS = ("opus", "telephone", "g722")
CODEC_WEIGHTS = (0.6, 0.25, 0.15)
_TAIL_PAD = 8192  # zeros appended before encoding so the encoder flush is not lost


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _to_np(samples) -> np.ndarray:
    if isinstance(samples, np.ndarray):
        return samples.astype(np.int16, copy=False).reshape(-1)
    return np.frombuffer(samples, dtype=np.int16) if hasattr(samples, "tobytes") \
        and getattr(samples, "typecode", "h") == "h" else np.asarray(samples, dtype=np.int16)


def _ff(args, data: bytes) -> bytes:
    p = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"] + args,
                       input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({p.returncode}): {p.stderr.decode(errors='replace')[:500]}")
    return p.stdout


_RAW_IN = ["-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0"]
_RAW_OUT = ["-f", "s16le", "-ar", str(SR), "-ac", "1", "pipe:1"]


def _fit(x: np.ndarray, n: int, lag: int = 0) -> np.ndarray:
    """Drop `lag` leading samples, then zero-pad / trim to exactly n."""
    x = x[lag:] if lag > 0 else x
    if lag < 0:
        x = np.concatenate([np.zeros(-lag, dtype=x.dtype), x])
    if len(x) >= n:
        return x[:n]
    return np.concatenate([x, np.zeros(n - len(x), dtype=x.dtype)])


def _probe() -> np.ndarray:
    r = np.random.RandomState(1234)
    return (r.randn(SR * 2) * 3000).clip(-32000, 32000).astype(np.int16)


def _measure_lag(fn) -> int:
    """Lag (samples) fn adds to a noise probe, found by FFT cross-correlation."""
    x = _probe()
    y = fn(x).astype(np.float64)[: len(x) + 4000]
    xf = x.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(y) + len(xf))))
    c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(xf, n)), n)
    lags = np.concatenate([c[:3000], c[-3000:]])
    i = int(np.argmax(lags))
    return i if i < 3000 else i - 6000


_LAG_CACHE: dict = {}


def _lag(key, fn) -> int:
    if key not in _LAG_CACHE:
        _LAG_CACHE[key] = _measure_lag(fn)
    return _LAG_CACHE[key]


# --------------------------------------------------------------------------
# codec stage
# --------------------------------------------------------------------------

def _codec_raw(x: np.ndarray, codec: str, bitrate_k: int, post_af: str = "") -> np.ndarray:
    """Encode + decode through the given codec; output NOT lag-compensated."""
    data = np.concatenate([x, np.zeros(_TAIL_PAD, dtype=np.int16)]).tobytes()
    af = ["-af", post_af] if post_af else []
    if codec == "opus":
        enc = _ff(_RAW_IN + ["-c:a", "libopus", "-application", "voip",
                             "-b:a", f"{bitrate_k}k", "-f", "ogg", "pipe:1"], data)
        dec = _ff(["-i", "pipe:0"] + af + _RAW_OUT, enc)
    elif codec == "telephone":
        enc = _ff(_RAW_IN + ["-ar", "8000", "-c:a", "pcm_mulaw", "-f", "mulaw", "pipe:1"], data)
        dec = _ff(["-f", "mulaw", "-ar", "8000", "-ac", "1", "-i", "pipe:0"] + af + _RAW_OUT, enc)
    elif codec == "g722":
        enc = _ff(_RAW_IN + ["-c:a", "g722", "-f", "g722", "pipe:1"], data)
        dec = _ff(["-f", "g722", "-i", "pipe:0"] + af + _RAW_OUT, enc)
    elif codec == "aac":
        enc = _ff(_RAW_IN + ["-c:a", "aac", "-b:a", f"{bitrate_k}k", "-f", "adts", "pipe:1"], data)
        dec = _ff(["-f", "aac", "-i", "pipe:0"] + af + _RAW_OUT, enc)
    else:
        raise ValueError(f"unknown codec {codec!r}")
    return np.frombuffer(dec, dtype=np.int16)


def _codec(x: np.ndarray, codec: str, bitrate_k: int = 0, post_af: str = "") -> np.ndarray:
    n = len(x)
    if n == 0:
        return x.copy()
    lag = _lag((codec, bitrate_k),
               lambda p: _codec_raw(p, codec, bitrate_k))
    return _fit(_codec_raw(x, codec, bitrate_k, post_af), n, lag)


# --------------------------------------------------------------------------
# room / convolution
# --------------------------------------------------------------------------

def _make_ir(seed: int, rt60: float, wet: float) -> np.ndarray:
    n = max(int(rt60 * SR), 16)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    tail = rng.standard_normal(n) * 10.0 ** (-3.0 * t / rt60)  # -60 dB at rt60
    tail[: int(0.003 * SR)] = 0.0  # keep the direct path clean
    tail /= np.sqrt(np.sum(tail ** 2)) + 1e-12
    ir = wet * tail
    ir[0] += 1.0 - wet
    return ir


def _convolve(x: np.ndarray, ir: np.ndarray, block: int = 1 << 16) -> np.ndarray:
    """Overlap-add FFT convolution, truncated to len(x) (float32 out)."""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(block + len(ir) - 1)))
    h = np.fft.rfft(ir, nfft)
    out = np.zeros(n + len(ir), dtype=np.float32)
    for s in range(0, n, block):
        seg = x[s:s + block].astype(np.float64)
        y = np.fft.irfft(np.fft.rfft(seg, nfft) * h, nfft)[: len(seg) + len(ir) - 1]
        out[s:s + len(y)] += y.astype(np.float32)
    return out[:n]


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------

def remote_profile(rng: random.Random) -> dict:
    codec = rng.choices(CODECS, weights=CODEC_WEIGHTS)[0]
    return {
        "kind": "remote",
        "codec": codec,
        "bitrate_k": rng.randint(12, 32) if codec == "opus" else 0,
        "comp_ratio": round(rng.uniform(2.0, 4.0), 3),
        "comp_threshold_db": round(rng.uniform(-30.0, -18.0), 2),
        "comp_attack_ms": round(rng.uniform(5.0, 20.0), 2),
        "comp_release_ms": round(rng.uniform(100.0, 400.0), 1),
        "hp_hz": round(rng.uniform(150.0, 300.0), 1),
        "lp_hz": round(rng.uniform(5000.0, 7500.0), 1),
        "peak_hz": round(rng.uniform(400.0, 1500.0), 1),
        "peak_db": round(rng.uniform(3.0, 8.0), 2),
        "peak_q": round(rng.uniform(1.0, 3.0), 2),
        "room_seed": rng.randrange(1 << 31),
        "room_rt60": round(rng.uniform(0.2, 0.5), 3),
        "room_wet": round(rng.uniform(0.15, 0.45), 3),
        "level_db": round(rng.uniform(-12.0, -3.0), 2),
    }


def local_profile(rng: random.Random) -> dict:
    return {
        "kind": "local",
        "codec": "none",
        "hp_hz": round(rng.uniform(70.0, 95.0), 1),
        "room_seed": rng.randrange(1 << 31),
        "room_rt60": round(rng.uniform(0.08, 0.2), 3),
        "room_wet": round(rng.uniform(0.03, 0.10), 3),
        "level_db": round(rng.uniform(-2.0, 2.0), 2),
    }


def _nudge(rng, v, spread, lo, hi, strength):
    return min(hi, max(lo, v + rng.uniform(-1.0, 1.0) * spread * strength))


def drift(profile: dict, rng: random.Random, strength: float = 1.0) -> dict:
    """Perturbed copy for a later epoch of the same speaker. Same codec family."""
    p = copy.deepcopy(profile)
    s = float(strength)
    p["hp_hz"] = round(_nudge(rng, p["hp_hz"], 15.0 if p["kind"] == "remote" else 5.0,
                              150.0 if p["kind"] == "remote" else 60.0,
                              300.0 if p["kind"] == "remote" else 110.0, s), 1)
    p["room_seed"] = rng.randrange(1 << 31)  # someone moved: new reflections
    p["room_rt60"] = round(_nudge(rng, p["room_rt60"], 0.05, 0.05, 0.6, s), 3)
    p["room_wet"] = round(_nudge(rng, p["room_wet"], 0.05, 0.01, 0.6, s), 3)
    if p["kind"] == "remote":
        if p["codec"] == "opus":
            p["bitrate_k"] = int(round(_nudge(rng, p["bitrate_k"], 6.0, 12, 32, s)))
        p["comp_ratio"] = round(_nudge(rng, p["comp_ratio"], 0.5, 2.0, 4.0, s), 3)
        p["comp_threshold_db"] = round(_nudge(rng, p["comp_threshold_db"], 3.0, -30.0, -18.0, s), 2)
        p["lp_hz"] = round(_nudge(rng, p["lp_hz"], 400.0, 5000.0, 7500.0, s), 1)
        p["peak_hz"] = round(_nudge(rng, p["peak_hz"], 150.0, 400.0, 1500.0, s), 1)
        p["peak_db"] = round(_nudge(rng, p["peak_db"], 1.0, 3.0, 8.0, s), 2)
        p["level_db"] = round(_nudge(rng, p["level_db"], 2.5, -12.0, -3.0, s), 2)
    else:
        p["level_db"] = round(_nudge(rng, p["level_db"], 1.0, -2.0, 2.0, s), 2)
    return p


# --------------------------------------------------------------------------
# public transforms
# --------------------------------------------------------------------------

def _remote_af(p: dict) -> str:
    return ",".join([
        f"highpass=f={p['hp_hz']}",
        f"lowpass=f={p['lp_hz']}", f"lowpass=f={p['lp_hz']}",
        f"equalizer=f={p['peak_hz']}:width_type=q:w={p['peak_q']}:g={p['peak_db']}",
        f"acompressor=threshold={p['comp_threshold_db']}dB:ratio={p['comp_ratio']}"
        f":attack={p['comp_attack_ms']}:release={p['comp_release_ms']}:makeup=2",
    ])


def apply(samples, profile: dict) -> np.ndarray:
    x = _to_np(samples)
    n = len(x)
    if n == 0:
        return x.copy()
    if profile["kind"] == "remote":
        y = _codec(x, profile["codec"], profile.get("bitrate_k", 0), _remote_af(profile))
    else:
        y = _fit(np.frombuffer(
            _ff(_RAW_IN + ["-af", f"highpass=f={profile['hp_hz']}"] + _RAW_OUT, x.tobytes()),
            dtype=np.int16), n)
    ir = _make_ir(profile["room_seed"], profile["room_rt60"], profile["room_wet"])
    z = _convolve(y, ir) * np.float32(10.0 ** (profile["level_db"] / 20.0))
    return np.clip(np.rint(z), -32768, 32767).astype(np.int16)


def tempo(samples, factor: float) -> np.ndarray:
    x = _to_np(samples)
    if len(x) == 0:
        return x.copy()
    if factor <= 0:
        raise ValueError("factor must be > 0")
    chain, f = [], float(factor)
    while f > 2.0:
        chain.append(2.0)
        f /= 2.0
    while f < 0.5:
        chain.append(0.5)
        f /= 0.5
    chain.append(f)
    af = ",".join(f"atempo={c:.6f}" for c in chain)
    return np.frombuffer(_ff(_RAW_IN + ["-af", af] + _RAW_OUT, x.tobytes()), dtype=np.int16).copy()


def aac_roundtrip(samples, bitrate_k: int = 64) -> np.ndarray:
    x = _to_np(samples)
    return _codec(x, "aac", int(bitrate_k))
