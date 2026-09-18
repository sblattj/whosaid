#!/usr/bin/env python3
"""
diarize_sherpa.py: fully local speaker diarization + speaker-labeled transcripts.

Runs sherpa-onnx offline speaker diarization (pyannote segmentation-3.0 ONNX +
a configurable speaker-embedding model — NeMo TitaNet-small by default, see the
DIARIZE_EMB_NAME env var; ungated GitHub-release models, CPU) over an audio
file, optionally names the anonymous clusters by matching them against reference
voice clips (enrollment), and merges the result with an MLX-Whisper .json transcript
into a speaker-labeled transcript (<base>.speakers.txt) plus an RTTM file.

Everything stays LOCAL: no audio, text, or embeddings leave the machine.

Invoked by the `whosaid` CLI via:
  uv run --with sherpa-onnx --with numpy python diarize_sherpa.py <audio> \
      [--whisper-json X.json] [--outdir DIR] [--name BASE] [--num-speakers N] \
      [--ref Name=clip.m4a ...] [--match-threshold 0.50]

Or, to pre-download/verify the models without any audio (used by `whosaid setup`):
  uv run --quiet --with sherpa-onnx --with numpy python diarize_sherpa.py --ensure-models-only

Models are cached under ~/.cache/sherpa-diarization/ on first use (~30 MB).
"""

import argparse
import concurrent.futures
import json
import math
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import numpy as np

CACHE = Path(os.environ.get("SHERPA_DIARIZE_CACHE", Path.home() / ".cache" / "sherpa-diarization"))
SEG_TAR_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEG_MODEL = CACHE / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
# Embedding model for speaker clustering. Default is NeMo TitaNet-small (English-native): in
# sherpa's own benchmark it runs ~2.5x faster than the 3D-Speaker ERes2Net models (RTF 0.11 vs
# 0.30) and separates English voices at least as cleanly. Alternatives from the same release:
#   3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx      (English ERes2Net)
#   3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx  (Mandarin — set for zh audio)
# Voiceprints in the registry are keyed by this model name, so switching models re-enrolls speakers.
EMB_NAME = os.environ.get(
    "DIARIZE_EMB_NAME", "nemo_en_titanet_small.onnx"
)
# The release tag really is misspelled upstream; try both spellings.
EMB_URLS = [
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{EMB_NAME}",
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongnition-models/{EMB_NAME}",
]
EMB_MODEL = CACHE / EMB_NAME

SAMPLE_RATE = 16000

# ---- local speaker registry -------------------------------------------------
# Persisted voiceprints (name -> embedding) so a person you identify ONCE is
# auto-named in every future transcript. This is private, derived data: it lives
# OUTSIDE the repo by default (never pushed to public sblattj/whosaid). Override
# with WHOSAID_SPEAKER_DB. The model name is stored alongside each embedding
# because cosine similarity is only meaningful within the same embedding model.
SPEAKER_DB = Path(
    os.environ.get("WHOSAID_SPEAKER_DB", Path.home() / ".config" / "whosaid" / "speakers.json")
)


def log(msg: str) -> None:
    print(f"diarize: {msg}", file=sys.stderr)


def emb_friendly(name: str) -> str:
    """Human-readable label for the active speaker-embedding model (transcript headers)."""
    known = {
        "nemo_en_titanet_small.onnx": "NeMo TitaNet-small",
        "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx": "3D-Speaker ERes2Net (en)",
        "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx": "3D-Speaker ERes2Net (zh-cn)",
    }
    return known.get(name, name)


# Conventional per-speaker role tags for downstream consumers (e.g. ranking
# action items: boss > self > others). Free-form lowercase tags are allowed —
# this tuple documents the conventional set, it is not a whitelist.
VALID_ROLES = ("self", "boss", "peer", "report", "external")


def normalize_role(role: str) -> str:
    """Normalize a role tag for storage: lowercase + strip; "" means no role."""
    return role.strip().lower()


def _registry_entry(name: str, model: str, embedding: list, added: str,
                    old: dict | None = None, role: str | None = None) -> dict:
    """Build a registry speaker entry, preserving unknown keys (e.g. "role")
    from `old` when overwriting the same (name, model) speaker, then applying
    `role` if given (an empty string removes the "role" key; None leaves it)."""
    entry = {"name": name, "model": model, "embedding": embedding, "added": added}
    if old:
        entry.update({k: v for k, v in old.items() if k not in entry})
    if role is not None:
        if role:
            entry["role"] = role
        else:
            entry.pop("role", None)
    return entry


def load_registry() -> dict:
    """Return {"speakers": [{"name","model","embedding":[...],"added",
    optional "role": str}...]}. The schema is tolerant: unknown keys are kept,
    and a speaker with no "role" simply has none (no migration needed)."""
    try:
        data = json.loads(SPEAKER_DB.read_text())
        if isinstance(data, dict) and isinstance(data.get("speakers"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"WARN speaker registry unreadable ({e}); starting empty")
    return {"speakers": []}


def save_registry(reg: dict) -> None:
    SPEAKER_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = SPEAKER_DB.with_suffix(".json.part")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.rename(SPEAKER_DB)


def registry_entries_for_model(reg: dict) -> list:
    """Only embeddings computed with the CURRENT embedding model are comparable."""
    return [s for s in reg.get("speakers", []) if s.get("model") == EMB_NAME and s.get("embedding")]


def fetch(url: str, dest: Path) -> bool:
    try:
        log(f"downloading {url.rsplit('/', 1)[-1]} ...")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"WARN download failed ({e})")
        return False


def ensure_models() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    if not SEG_MODEL.exists():
        tar_path = CACHE / "seg.tar.bz2"
        if not fetch(SEG_TAR_URL, tar_path):
            sys.exit("diarize: FATAL could not download the segmentation model")
        with tarfile.open(tar_path, "r:bz2") as tf:
            tf.extractall(CACHE)
        tar_path.unlink()
    else:
        log(f"segmentation model already cached at {SEG_MODEL}")
    if not EMB_MODEL.exists():
        if not any(fetch(u, EMB_MODEL) for u in EMB_URLS):
            sys.exit("diarize: FATAL could not download the embedding model")
    else:
        log(f"embedding model already cached at {EMB_MODEL}")


def load_audio(path: str, start: float | None = None, dur: float | None = None) -> np.ndarray:
    """Decode any ffmpeg-readable audio to mono float32 @16k. No soundfile/librosa needed."""
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", str(dur)]
    cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def probe_duration(path: str) -> float:
    """Audio duration in seconds via ffprobe (cheap; avoids decoding the whole file)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return 0.0


def probe_creation_time(path: str) -> str | None:
    """Container creation_time tag via ffprobe, as an ISO-8601 string (None if absent).

    Same ffprobe invocation as lib/workspace.py's probe_creation_time(), which owns
    the parsing/normalisation used for folder naming; here the raw tag is enough.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def source_metadata(path: str) -> dict:
    """Recording provenance for the sidecar: absolute path, duration, creation time.

    Saves consumers an out-of-band ffprobe call to derive per-meeting timestamps.
    """
    return {
        "path": str(Path(path).resolve()),
        "duration_seconds": round(probe_duration(path), 3) or None,
        "creation_time": probe_creation_time(path),
    }


def make_diar_config(num_speakers: int):
    """Build the sherpa diarization config. Imported lazily so worker processes can call it."""
    import sherpa_onnx
    return sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )


def make_embed(ex):
    """Return an embed(wave)->unit-vector closure over a sherpa embedding extractor."""
    def embed(wave: np.ndarray) -> np.ndarray:
        st = ex.create_stream()
        st.accept_waveform(SAMPLE_RATE, wave)
        st.input_finished()
        v = np.array(ex.compute(st), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)
    return embed


def cluster_embeddings(samples: np.ndarray, segs: list, embed, key: str = "speaker") -> dict:
    """One embedding per cluster, from up to ~40s of that cluster's longest turns.
    `segs` times must be relative to `samples` (window-local for a chunk)."""
    out = {}
    for cid in sorted({s[key] for s in segs}):
        turns = sorted((s for s in segs if s[key] == cid),
                       key=lambda s: s["end"] - s["start"], reverse=True)
        chunks, total = [], 0.0
        for t in turns:
            d = min(t["end"] - t["start"], 40.0 - total)
            if d <= 0.5:
                continue
            chunks.append(samples[int(t["start"] * SAMPLE_RATE):int((t["start"] + d) * SAMPLE_RATE)])
            total += d
            if total >= 40.0:
                break
        if chunks:
            out[cid] = embed(np.concatenate(chunks))
    return out


def diarize_window(payload: tuple) -> list:
    """Worker (own process): segment one [start, start+dur) window and embed each turn.

    Returns global-time segments, each with a per-turn voiceprint (`emb`) when the turn
    is long enough to embed. We deliberately do NOT trust the per-chunk cluster ids —
    identities are recovered globally in cluster_segments() so speakers stay consistent
    across chunk boundaries."""
    audio_path, start, dur = payload
    import sherpa_onnx  # re-imported per process (spawn)
    samples = load_audio(audio_path, start=start, dur=dur)
    if samples.size == 0:
        return []
    result = sherpa_onnx.OfflineSpeakerDiarization(make_diar_config(-1)).process(samples).sort_by_start_time()
    if len(result) == 0:
        return []
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)))
    embed = make_embed(ex)
    out = []
    for s in result:
        seg = {"start": float(s.start) + start, "end": float(s.end) + start}
        if s.end - s.start >= 0.5:
            wave = samples[int(s.start * SAMPLE_RATE):int(s.end * SAMPLE_RATE)]
            if wave.size > 0:
                seg["emb"] = embed(wave).tolist()
        out.append(seg)
    return out


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) + 1e-9) * (np.linalg.norm(b) + 1e-9)))


# Upper bound on the auto-detected speaker count. It is a BOUND, not a target:
# an estimate that lands on the cap is treated as a FAILED estimate (the run
# still proceeds, but `saturated` is set, the WARN fires and the count warning
# is written into the speaker cards) rather than as a result. See GitHub #5.
SPEAKER_CAP = 20

# Same-speaker cosine cut for the agglomerative count estimate, calibrated
# against REAL TitaNet-small per-turn embeddings.
#
# With AVERAGE linkage the distance between two groups converges to the MEAN
# pairwise similarity between them, so the usable cut is between mean-intra and
# mean-inter, and the robust choice is the midpoint of the band over which the
# true count holds.
#
# Measured (not assumed) on two 17.8-minute 6-voice fixtures put through this
# exact pipeline — 65 and 72 embedded turns, 192-dim embeddings:
#   intra-speaker cosine  mean 0.902 (p10 0.815) / mean 0.891 (p10 0.721)
#   inter-speaker cosine  mean 0.252 (p90 0.452) / mean 0.256 (p90 0.454)
# k == 6 (the true count) holds for a cut in [0.55, 0.61] on the harder
# channel-varied fixture and [0.44, 0.61] on the clean one; 0.58 is the
# midpoint of the intersection.
#
# NB issue #5 states same-speaker turns sit at 0.6-0.8. That is NOT what this
# model produces on this path: per-turn intra-speaker similarity measures ~0.90,
# with the 0.6-0.8 range being roughly the p10 tail. A cut calibrated to the
# quoted 0.6-0.8 lands near 0.45 and OVER-merges — it returned 5 speakers for 6
# on the channel-varied fixture, which is how this number was corrected.
AGGLOM_THRESHOLD = float(os.environ.get("WHOSAID_COUNT_THRESHOLD", "0.58"))


def _average_linkage_merges(D: np.ndarray) -> list:
    """Full average-linkage (UPGMA) dendrogram over a square distance matrix.

    Uses the nearest-neighbour-chain algorithm, which is exact for average
    linkage (a reducible Lance-Williams method) and runs in O(n^2) time with
    O(n^2) memory — the naive "rescan the whole matrix per merge" loop is
    O(n^3) and would not survive the ~700 turns of a 52-minute meeting.

    Returns [(height, a, b), ...] where `a` and `b` are the row indices of the
    two groups merged at cosine distance `height`. The surviving row is `a`.
    Because average linkage is monotone the list can be cut by height in any
    order, so `_cut_merges` just unions every merge below the cut.
    """
    n = D.shape[0]
    D = np.array(D, dtype=np.float64, copy=True)
    np.fill_diagonal(D, np.inf)
    size = np.ones(n)
    dead = np.zeros(n, dtype=bool)
    merges: list = []
    chain: list = []
    for _ in range(n - 1):
        if not chain:
            chain = [int(np.flatnonzero(~dead)[0])]
        while True:
            a = chain[-1]
            row = D[a]
            b = int(np.argmin(row))
            # Prefer the previous chain link on ties, or an exact-tie pair can
            # chase each other forever without ever forming a reciprocal pair.
            if len(chain) >= 2 and row[chain[-2]] <= row[b]:
                b = chain[-2]
            if len(chain) >= 2 and b == chain[-2]:
                break
            chain.append(b)
        height = float(D[chain[-1], chain[-2]])
        b = chain.pop()
        a = chain.pop()
        merges.append((height, a, b))
        na, nb = size[a], size[b]
        newrow = (na * D[a] + nb * D[b]) / (na + nb)   # Lance-Williams, average linkage
        newrow[a] = np.inf
        newrow[b] = np.inf
        D[a] = newrow
        D[:, a] = newrow
        D[b, :] = np.inf
        D[:, b] = np.inf
        size[a] = na + nb
        dead[b] = True
    return merges


def _cut_merges(n: int, merges: list, cutoff: float) -> np.ndarray:
    """Flat labels from a dendrogram, keeping every merge below `cutoff`."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for height, a, b in merges:
        if height < cutoff:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    order: dict = {}
    labels = np.empty(n, dtype=int)
    for i in range(n):
        r = find(i)
        if r not in order:
            order[r] = len(order)
        labels[i] = order[r]
    return labels


def agglomerative_labels(X: np.ndarray, thresh: float = AGGLOM_THRESHOLD) -> np.ndarray:
    """Average-linkage clustering of unit-norm embeddings, cut at cosine `thresh`."""
    n = len(X)
    if n <= 1:
        return np.zeros(n, dtype=int)
    D = 1.0 - (np.asarray(X, dtype=np.float32) @ np.asarray(X, dtype=np.float32).T)
    return _cut_merges(n, _average_linkage_merges(D), 1.0 - thresh)


def estimate_speakers(X: np.ndarray, thresh: float = AGGLOM_THRESHOLD,
                      cap: int = SPEAKER_CAP, min_speakers: int = 0,
                      max_speakers: int = 0) -> dict:
    """Estimate the speaker count by agglomerative clustering of per-turn voiceprints.

    The old farthest-first pass opened a new center whenever a turn's similarity
    to every existing center fell below a threshold. That is order-dependent and
    compares each turn to a SINGLE turn, so on real meeting audio — where
    same-speaker turns sit at 0.6-0.8 depending on channel and prosody — it kept
    minting centers until it pinned at the cap on every long recording (#5).
    Average linkage instead compares GROUP means, which is exactly the quantity
    that separates cleanly, and needs no cap to terminate.

    `cap` (and `max_speakers`, which lowers it) is a bound only. If the raw
    estimate reaches it we do not trust the count: `saturated` is True and the
    caller warns instead of silently shipping an inflated speaker list.

    Returns {"method", "threshold", "k", "raw_k", "cap", "min", "max",
             "saturated", "labels"}.
    """
    labels = agglomerative_labels(X, thresh)
    raw_k = int(labels.max()) + 1 if len(labels) else 0
    eff_cap = min(cap, max_speakers) if max_speakers and max_speakers > 0 else cap
    k = raw_k
    saturated = raw_k >= eff_cap
    if saturated:
        k = eff_cap
    if min_speakers and min_speakers > 0:
        k = max(k, min_speakers)
    k = max(1, min(k, len(X)))
    if saturated:
        log(f"WARN speaker-count estimate hit the bound of {eff_cap} "
            f"(agglomerative at cosine {thresh:.2f} found {raw_k}); the count is NOT trustworthy")
    return {
        "method": "agglomerative",
        "threshold": round(float(thresh), 4),
        "k": int(k),
        "raw_k": int(raw_k),
        "cap": int(cap),
        "min": int(min_speakers) if min_speakers and min_speakers > 0 else None,
        "max": int(max_speakers) if max_speakers and max_speakers > 0 else None,
        "saturated": bool(saturated),
        "labels": labels,
    }


def estimate_k(X: np.ndarray, thresh: float = AGGLOM_THRESHOLD,
               cap: int = SPEAKER_CAP) -> int:
    """Speaker count only; see estimate_speakers for the full estimate record."""
    return estimate_speakers(X, thresh=thresh, cap=cap)["k"]


def spherical_kmeans(X: np.ndarray, k: int, iters: int = 100, restarts: int = 8) -> np.ndarray:
    """Cluster unit-norm embeddings on cosine similarity (k-means++ init, best of `restarts`)."""
    n = len(X)
    if k >= n:
        return np.arange(n)
    rng = np.random.default_rng(0)
    best_labels, best_score = None, -1e18
    for _ in range(restarts):
        centers = [X[rng.integers(n)]]
        for _ in range(1, k):
            sims = np.max(X @ np.array(centers).T, axis=1)
            d2 = np.clip(1.0 - sims, 0, None) ** 2
            total = d2.sum()
            centers.append(X[rng.integers(n) if total < 1e-12 else rng.choice(n, p=d2 / total)])
        C = np.array(centers, dtype=np.float32)
        labels = np.zeros(n, dtype=int)
        for _ in range(iters):
            new_labels = np.argmax(X @ C.T, axis=1)
            if np.array_equal(new_labels, labels) and _ > 0:
                break
            labels = new_labels
            for j in range(k):
                m = X[labels == j]
                if len(m):
                    v = m.sum(axis=0)
                    C[j] = v / (np.linalg.norm(v) + 1e-9)
                else:
                    C[j] = X[rng.integers(n)]
        score = float(np.sum(np.max(X @ C.T, axis=1)))
        if score > best_score:
            best_score, best_labels = score, labels
    return best_labels


# Cosine a single TURN must reach against an ENROLLED voiceprint before that turn
# is pinned to that person's cluster (`--expected-speakers`, GitHub issue #1 part 4).
#
# This gate sits on the SAME per-turn quantity the count estimator was calibrated
# against, measured on real TitaNet-small embeddings through this exact pipeline
# (two 17.8-minute 6-voice fixtures, 65 and 72 embedded turns, 192-dim):
#   intra-speaker cosine  mean 0.902 (p10 0.815) / mean 0.891 (p10 0.721)
#   inter-speaker cosine  mean 0.252 (p90 0.452) / mean 0.256 (p90 0.454)
# so the empty band between a stranger's p90 (~0.45) and the same person's p10
# (~0.72) is roughly [0.46, 0.72]. 0.70 sits at the TOP of that band, just under
# the intra-speaker p10, and that asymmetry is deliberate: this is not the count
# cut (AGGLOM_THRESHOLD 0.58, which compares GROUP means, where the midpoint is
# right) but a per-turn IDENTITY assertion, where the two errors are not
# symmetric. A false anchor writes a real person's name onto someone else's
# words and is invisible in the output; a missed anchor merely drops the turn
# into the residual pool, where the ordinary estimator + the absorb pass still
# have a chance to recover it. So we buy precision with recall: at 0.70 roughly
# the bottom decile of genuine same-speaker turns falls through to the residual
# clustering, while a stranger would have to score ~5 sigma above their mean to
# be pinned. Raise it if you see a name on the wrong turns; lower it (towards
# ~0.55) on channel-varied audio where one person's turns score low.
DEFAULT_ANCHOR_THRESHOLD = 0.70


def default_anchor_threshold() -> float:
    """argparse default for --anchor-threshold: env WHOSAID_ANCHOR_THRESHOLD wins.

    Read through a function (not at import time) so tests can flip the env var
    and observe the new default without reloading the module.
    """
    raw = os.environ.get("WHOSAID_ANCHOR_THRESHOLD", "")
    try:
        return float(raw) if raw.strip() else DEFAULT_ANCHOR_THRESHOLD
    except ValueError:
        return DEFAULT_ANCHOR_THRESHOLD


def cluster_segments(all_segments: list, num_speakers: int, min_speakers: int = 0,
                     max_speakers: int = 0, anchors: list | None = None,
                     anchor_threshold: float | None = None) -> tuple:
    """Assign every segment a global speaker by clustering ALL per-turn voiceprints at
    once — a global view that matches whole-file quality even though the segmentation
    ran chunk-by-chunk.

    Returns (segments with SPEAKER_NN, {SPEAKER_NN: centroid}, speakers, estimate,
    anchor_info), where `estimate` is the estimate_speakers record (minus its label
    array) or None when the count came from an explicit --num-speakers.

    `anchors` (GitHub issue #1 part 4) is a list of (name, embedding) for the people
    named by --expected-speakers. When given, clustering is ANCHORED: every embedded
    turn is compared to each anchor voiceprint, a turn whose best anchor cosine is
    >= `anchor_threshold` is pinned to that anchor's cluster, and only the RESIDUAL
    turns go through the ordinary estimator + k-means (so min/max/cap bound the
    number of UNKNOWN speakers, not the total). An anchor that received zero turns
    is dropped — that person did not attend — which is exactly the failure mode an
    exact --num-speakers has on a varying-attendance roster.

    `anchor_info` is None on the unanchored path; otherwise
    {"threshold", "anchored": {SPEAKER_NN: name}, "names": {name: {turns, mean_cosine}}}."""
    embedded = [s for s in all_segments if s.get("emb")]
    if not embedded:
        return [], {}, [], None, None
    X = np.array([s["emb"] for s in embedded], dtype=np.float32)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    estimate = None
    anchor_info = None

    if anchors:
        if anchor_threshold is None:
            anchor_threshold = default_anchor_threshold()
        # Clamp into the only range where a cosine gate means anything. A value
        # outside [-1, 1] would either pin every turn or none, silently.
        anchor_threshold = float(min(1.0, max(-1.0, float(anchor_threshold))))
        A = np.array([a[1] for a in anchors], dtype=np.float32)
        A = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
        sims = X @ A.T                       # (turns, anchors)
        best_a = np.argmax(sims, axis=1)
        best_s = sims[np.arange(len(X)), best_a]
        pinned = best_s >= anchor_threshold

        n_anchor = len(anchors)
        cid = np.full(len(X), -1, dtype=int)
        cid[pinned] = best_a[pinned]         # anchor clusters occupy ids 0..n_anchor-1
        residual = np.flatnonzero(~pinned)
        log(f"anchored clustering: {len(embedded)} embedded turns, "
            f"{int(pinned.sum())} pinned to {len(set(best_a[pinned].tolist()))} of "
            f"{n_anchor} expected speaker(s) at cosine >= {anchor_threshold:.2f}, "
            f"{len(residual)} residual")

        if len(residual):
            Xr = X[residual]
            active = len(set(best_a[pinned].tolist()))
            if num_speakers and num_speakers > 0:
                # An exact total still means the total. Whatever the anchors did
                # not claim is the residual budget (at least one cluster, since
                # there are residual turns to put somewhere).
                k_res = max(1, num_speakers - active)
            else:
                est = estimate_speakers(Xr, min_speakers=min_speakers,
                                        max_speakers=max_speakers)
                k_res = est["k"]
                estimate = {p: est[p] for p in est if p != "labels"}
                estimate["anchored"] = True
                estimate["anchors_active"] = int(active)
                estimate["residual_turns"] = int(len(residual))
            k_res = max(1, min(k_res, len(residual)))
            log(f"  residual clustering: {len(residual)} turn(s) -> {k_res} new speaker(s)")
            rlabels = spherical_kmeans(Xr, k_res)
            for i, lab in zip(residual, rlabels):
                cid[i] = n_anchor + int(lab)

        for s, c in zip(embedded, cid):
            s["_c"] = int(c)
        # Anchor clusters that took zero turns simply never appear in `cid`, so
        # they drop out of the talk-time ordering below with no extra work.
        anchor_stats = {}
        for j, (name, _e) in enumerate(anchors):
            rows = np.flatnonzero(cid == j)
            if not len(rows):
                log(f"  expected speaker '{name}' claimed no turns — dropped (did not attend)")
                continue
            anchor_stats[name] = {"turns": int(len(rows)),
                                  "mean_cosine": round(float(best_s[rows].mean()), 6)}
        anchor_info = {"threshold": anchor_threshold, "names": anchor_stats,
                       "cluster_of": {j: anchors[j][0] for j in range(n_anchor)
                                      if anchors[j][0] in anchor_stats}}
    else:
        if num_speakers and num_speakers > 0:
            k = num_speakers
        else:
            est = estimate_speakers(X, min_speakers=min_speakers, max_speakers=max_speakers)
            k = est["k"]
            estimate = {p: est[p] for p in est if p != "labels"}
        k = max(1, min(k, len(embedded)))
        # The agglomerative pass gives the COUNT; spherical k-means still does the
        # ASSIGNMENT. On the synthetic fixtures both label every turn correctly
        # (purity 1.000 each, test/estimate_k_test.py), so k-means is kept: it is
        # the path --num-speakers already takes, so auto and hinted runs stay
        # identical given the same k, and its reassignment step recovers turns that
        # average linkage chained into a neighbour.
        log(f"global clustering: {len(embedded)} embedded turns -> {k} speaker(s)")
        labels = spherical_kmeans(X, k)
        for s, lab in zip(embedded, labels):
            s["_c"] = int(lab)

    # Segments too short to embed inherit the label of the nearest embedded turn in time.
    mids = np.array([0.5 * (s["start"] + s["end"]) for s in embedded])
    emb_labels = np.array([s["_c"] for s in embedded])
    for s in all_segments:
        if "_c" not in s:
            s["_c"] = int(emb_labels[int(np.argmin(np.abs(mids - 0.5 * (s["start"] + s["end"]))))])

    talk = {}
    for s in all_segments:
        talk[s["_c"]] = talk.get(s["_c"], 0.0) + (s["end"] - s["start"])
    order = sorted(talk, key=lambda c: talk[c], reverse=True)  # biggest talker -> SPEAKER_00
    relabel = {c: f"SPEAKER_{i:02d}" for i, c in enumerate(order)}

    cluster_emb = {}
    for c in order:
        rows = X[[i for i, s in enumerate(embedded) if s["_c"] == c]]
        v = rows.sum(axis=0)
        cluster_emb[relabel[c]] = v / (np.linalg.norm(v) + 1e-9)

    segs = [{"start": s["start"], "end": s["end"], "speaker": relabel[s["_c"]]} for s in all_segments]
    segs.sort(key=lambda s: s["start"])

    if anchor_info is not None:
        # Map the internal anchor cluster ids onto the SPEAKER_NN labels the rest
        # of the pipeline speaks, so main() can pre-seed `names` before naming runs.
        anchor_info["anchored"] = {relabel[c]: name
                                   for c, name in anchor_info.pop("cluster_of").items()
                                   if c in relabel}
        for sp, name in anchor_info["anchored"].items():
            log(f"  anchor: {sp} -> {name} "
                f"({anchor_info['names'][name]['turns']} turns, "
                f"mean cosine {anchor_info['names'][name]['mean_cosine']:.3f})")

    return segs, cluster_emb, sorted(cluster_emb), estimate, anchor_info


def diarize_parallel(audio: str, total_dur: float, num_speakers: int, jobs: int,
                     chunk_seconds: float, min_speakers: int = 0,
                     max_speakers: int = 0, anchors: list | None = None,
                     anchor_threshold: float | None = None) -> tuple:
    """Split the audio into windows, segment+embed them concurrently, then cluster
    globally. Returns (segments, {SPEAKER_NN: embedding}, speakers, estimate, anchor_info).

    `n_chunks == 1` is a supported degenerate case: a single window covering the
    whole file. That is how --expected-speakers reaches short recordings, whose
    default path (sherpa FastClustering) has no place to apply an anchor."""
    n_chunks = max(1, math.ceil(total_dur / chunk_seconds))
    bounds = [(i * chunk_seconds, min(chunk_seconds, total_dur - i * chunk_seconds))
              for i in range(n_chunks)]
    log(f"parallel diarization: {n_chunks} chunk(s) of ~{chunk_seconds:.0f}s across {jobs} job(s)")
    payloads = [(audio, start, dur) for start, dur in bounds if dur > 0.5]
    per_chunk = [None] * len(payloads)
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(diarize_window, p): idx for idx, p in enumerate(payloads)}
        for fut in concurrent.futures.as_completed(futs):
            idx = futs[fut]
            segs = fut.result()
            per_chunk[idx] = segs
            log(f"  chunk {idx + 1}/{len(payloads)} done: {len(segs)} turns "
                f"({sum(1 for s in segs if s.get('emb'))} embedded)")
    all_segments = [s for chunk in per_chunk if chunk for s in chunk]
    return cluster_segments(all_segments, num_speakers, min_speakers, max_speakers,
                            anchors=anchors, anchor_threshold=anchor_threshold)


def build_turns(segs: list, whisper_json: str | None) -> list:
    """Merge diarization segments with a whisper .json into (speaker, start, [texts]) turns."""
    turns = []
    if not whisper_json:
        return turns
    wj = json.loads(Path(whisper_json).read_text())

    def speaker_for(a: float, b: float) -> str:
        overlaps = {}
        for s in segs:
            ov = min(b, s["end"]) - max(a, s["start"])
            if ov > 0:
                overlaps[s["speaker"]] = overlaps.get(s["speaker"], 0.0) + ov
        if overlaps:
            return max(overlaps, key=overlaps.get)
        mid = (a + b) / 2  # no overlap (silence-gap segment): nearest turn wins
        nearest = min(segs, key=lambda s: min(abs(mid - s["start"]), abs(mid - s["end"])))
        return nearest["speaker"]

    for w in wj.get("segments", []):
        txt = (w.get("text") or "").strip()
        if not txt:
            continue
        sp = speaker_for(w["start"], w["end"])
        if turns and turns[-1][0] == sp:
            turns[-1][2].append(txt)
        else:
            turns.append((sp, w["start"], [txt]))
    return turns


def render_outputs(outdir: Path, base: str, segs: list, speakers: list, names: dict,
                   turns: list, snippets_n: int, detect_mode: str,
                   emb_name: str = EMB_NAME, count_warning: str | None = None,
                   roles: dict | None = None, local_names: set | None = None) -> None:
    """Write RTTM, the speaker-labeled transcript, and the human-facing speaker cards."""
    talk = {sp: 0.0 for sp in speakers}
    nturns = {sp: 0 for sp in speakers}
    for s in segs:
        talk[s["speaker"]] += s["end"] - s["start"]
        nturns[s["speaker"]] += 1

    rttm_path = outdir / f"{base}.rttm"
    with open(rttm_path, "w") as f:
        for s in segs:
            f.write(f"SPEAKER {base} 1 {s['start']:.3f} {s['end'] - s['start']:.3f} "
                    f"<NA> <NA> {names[s['speaker']]} <NA> <NA>\n")
    log(f"wrote {rttm_path}")

    if turns:
        out_path = outdir / f"{base}.speakers.txt"
        with open(out_path, "w") as f:
            f.write(f"# Speaker-labeled transcript: {base}\n")
            f.write(f"# Diarization: sherpa-onnx (pyannote segmentation-3.0 + "
                    f"{emb_friendly(emb_name)}), local.\n")
            f.write(f"# Speakers ({len(speakers)}): {', '.join(sorted(set(names.values())))}\n")
            if roles:  # per-speaker role tags (registry "role"); one line each
                for nm in sorted(roles):
                    f.write(f"# Role: {nm} = {roles[nm]}\n")
            f.write("\n")
            for sp, start, texts in turns:
                f.write(f"[{hms(start)}] {names[sp]}: {' '.join(texts)}\n\n")
        log(f"wrote {out_path} ({len(turns)} speaker turns)")

    if snippets_n <= 0:
        return
    # Group cards by FINAL speaker name so a person split across several clusters
    # (e.g. absorbed SPEAKER_04 + SPEAKER_07 -> Matt) shows as ONE card with the
    # combined turns/talk time. Unidentified clusters stay one card each.
    def final_name(sp: str) -> str:
        return names[sp] if names.get(sp, sp) != sp else sp

    groups = []        # final names in talk-time order (speakers is biggest-talker first)
    members = {}       # final name -> [cluster ids]
    for sp in speakers:
        fn = final_name(sp)
        if fn not in members:
            members[fn] = []
            groups.append(fn)
        members[fn].append(sp)
    snippets = {fn: [] for fn in groups}
    for sp, start, texts in turns:
        joined = " ".join(texts).strip()
        if len(joined.split()) >= 4:  # skip "yeah", "mm-hm" backchannel
            snippets[final_name(sp)].append((start, joined))
    cards_path = outdir / f"{base}.speaker-cards.txt"
    lines = [f"# Speaker cards: {base}",
             f"# {len(groups)} speaker(s) ({detect_mode}). "
             f"Read the snippets, then persist names with:"]
    # #6: an unreliable count must be visible to whoever reads THIS file, not
    # only to whoever was watching the log when the run happened.
    if count_warning:
        lines.append(f"# WARNING: {count_warning}")
    lines += [f"#   whosaid relabel {base} SPEAKER_XX=Name [SPEAKER_YY=Name ...]", ""]
    for fn in groups:
        clusters = members[fn]
        g_turns = sum(nturns[sp] for sp in clusters)
        g_talk = sum(talk[sp] for sp in clusters)
        role_tag = f"  [{roles[fn]}]" if roles and fn in roles else ""
        if fn in clusters:            # unnamed: final name IS the cluster id
            label = f"{fn}  (UNIDENTIFIED)"
        elif fn in (local_names or set()):
            label = f"{fn}{role_tag}  (transcript-only label; registry untouched)"
        elif len(clusters) > 1:
            label = f"{fn}{role_tag}  ({', '.join(clusters)})"
        else:
            label = f"{fn}{role_tag}"
        lines.append("=" * 60)
        lines.append(f"{label}   —   {g_turns} turns, {hms(g_talk)} talk time")
        lines.append("=" * 60)
        picks = sorted(snippets[fn], key=lambda x: len(x[1]), reverse=True)[:snippets_n]
        picks.sort(key=lambda x: x[0])
        if not picks:
            lines.append("  (no substantive snippets — mostly short backchannel)")
        for start, quote in picks:
            q = quote if len(quote) <= 280 else quote[:277] + "..."
            lines.append(f"  [{hms(start)}] \"{q}\"")
        lines.append("")
    cards_path.write_text("\n".join(lines) + "\n")
    log(f"wrote {cards_path}")
    for ln in lines:  # echo to stderr so the human sees it right after the run
        print(ln, file=sys.stderr)


DEFAULT_REF_THRESHOLD = 0.50


def default_ref_threshold() -> float:
    """argparse default for --ref-threshold: env WHOSAID_MATCH_THRESHOLD wins.

    Read through a function (not at import time) so tests can flip the env var
    and observe the new default without reloading the module.
    """
    raw = os.environ.get("WHOSAID_MATCH_THRESHOLD", "")
    try:
        return float(raw) if raw.strip() else DEFAULT_REF_THRESHOLD
    except ValueError:
        return DEFAULT_REF_THRESHOLD


def name_clusters(cluster_emb: dict, ref_threshold: float, absorb_threshold: float,
                  registry_entries: list, ref_voices: list | None = None,
                  names: dict | None = None, report: list | None = None) -> dict:
    """Assign real names to anonymous clusters in three passes and return the
    {SPEAKER_NN: name-or-self} map. Shared by the transcribe path and
    `relabel --auto` so both name clusters identically.

    Passes (each only touches STILL-UNNAMED clusters, so earlier/explicit names win):
      1. registry one-best — each known voiceprint claims its single best cluster
         when cosine >= ref_threshold (default 0.50).
      2. --ref clips — each reference voice claims its best cluster (>= ref_threshold),
         but a ref whose name the registry already assigned is skipped, so one
         person never lands on two cards.
      3. absorb — every cluster still unnamed whose centroid cosine to ANY known
         voice (registry entries AND --ref voices) is >= absorb_threshold takes
         that name. Multiple clusters may share a name; the cards merge them.

    Ref-threshold rationale (0.50 default, raised from 0.40 for GitHub issue #1):
    on real meeting audio TitaNet-small asserted wrong names in the 0.40-0.53 band,
    while a genuine same-speaker match scores far higher -- the end-to-end test's
    enrolled reference matches its cluster at 0.986, against 0.194 for the nearest
    stranger, so 0.50 sits in a wide empty gap rather than near a real match.

    Absorb-threshold rationale (0.85 default): same-speaker TitaNet-small centroids
    measured 0.90-0.95 across window splits, while distinct speakers stayed <= 0.73,
    so 0.85 folds phantom splits back together without swallowing real strangers.

    Clusters whose best candidate stays BELOW the gate keep their anonymous
    SPEAKER_NN label (see the `>= ref_threshold` guards below) — a low-confidence
    voiceprint never takes a real name.

    `registry_entries` : list of {"name","embedding"} for the active model.
    `ref_voices`       : list of (name, embedding) already-embedded --ref clips.
    `report`           : optional list; every candidate decision (matched AND
                         near-miss) is appended as a dict with keys
                         cluster/name/similarity/threshold/matched/pass, so the
                         caller can persist machine-readable match confidence.
                         Passing it is optional, keeping positional callers working.
    """
    ref_voices = ref_voices or []
    if report is None:
        report = []

    def record(cluster, name, similarity, threshold, matched, which):
        report.append({"cluster": cluster, "name": name,
                       "similarity": round(float(similarity), 6),
                       "threshold": float(threshold),
                       "matched": bool(matched), "pass": which})
    if names is None:
        names = {sp: sp for sp in cluster_emb}

    def unit(v):
        v = np.asarray(v, dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    known = [(e["name"], unit(e["embedding"])) for e in registry_entries]

    # Pass 1: registry one-best (each voiceprint -> its single closest free cluster).
    # A voice that ALREADY owns a cluster (an explicit relabel spec, or a name kept
    # from a prior run in relabel --auto) is skipped here: extending one person onto
    # extra clusters is the absorb pass's job, gated at the far stricter
    # absorb_threshold, so the looser ref gate can't annex a second, low-confidence
    # cluster to someone who is already placed.
    assigned = {names[sp] for sp in names if names.get(sp, sp) != sp}
    registry_named = set()
    for entry_name, kemb in known:
        if entry_name in assigned:
            continue
        sims = {sp: float(np.dot(kemb, unit(e))) for sp, e in cluster_emb.items()
                if names.get(sp, sp) == sp}
        if not sims:
            continue
        best = max(sims, key=sims.get)
        matched = sims[best] >= ref_threshold
        record(best, entry_name, sims[best], ref_threshold, matched, "registry")
        if matched:
            names[best] = entry_name
            assigned.add(entry_name)
            registry_named.add(entry_name)
            log(f"  registry: {best} -> {entry_name} (sim {sims[best]:.3f})")
        else:
            log(f"  registry: {entry_name} best {best} sim {sims[best]:.3f} "
                f"< {ref_threshold}, left unnamed")

    # Pass 2: --ref clips (still-unnamed only; skip a name the registry already used).
    for ref_name, remb in ref_voices:
        remb = unit(remb)
        allsims = {sp: float(np.dot(remb, unit(e))) for sp, e in cluster_emb.items()}
        log(f"ref {ref_name}: " + ", ".join(f"{sp}={v:.3f}" for sp, v in sorted(allsims.items())))
        if ref_name in registry_named:
            log(f"ref {ref_name}: already named by registry, skipping")
            continue
        sims = {sp: v for sp, v in allsims.items() if names.get(sp, sp) == sp}
        if not sims:
            continue
        best = max(sims, key=sims.get)
        record(best, ref_name, sims[best], ref_threshold,
               sims[best] >= ref_threshold, "ref")
        if sims[best] >= ref_threshold:
            names[best] = ref_name
            log(f"  ref: {best} -> {ref_name} (sim {sims[best]:.3f})")
        else:
            log(f"WARN ref {ref_name}: best similarity {sims[best]:.3f} < {ref_threshold}, cluster left unnamed")

    # Pass 3: absorb phantom splits into the nearest known voice.
    for sp, e in cluster_emb.items():
        if names.get(sp, sp) != sp:
            continue
        ce = unit(e)
        cands = {n: float(np.dot(ce, k)) for n, k in known}
        for n, k in ref_voices:
            cands[n] = max(cands.get(n, -1.0), float(np.dot(ce, unit(k))))
        if not cands:
            continue
        bn = max(cands, key=cands.get)
        matched = cands[bn] >= absorb_threshold
        record(sp, bn, cands[bn], absorb_threshold, matched, "absorb")
        if matched:
            names[sp] = bn
            log(f"  absorb: {sp} -> {bn} (sim {cands[bn]:.3f})")
    return names


def save_print_guarded(reg: dict, cluster: str, person: str, emb_vec, emb_model: str,
                       base: str, ref_threshold: float, force: bool) -> None:
    """Replace person's registry print with this cluster's embedding (#19).

    Refuses the replacement when the cluster barely matches the print it is
    about to overwrite (cosine < ref_threshold) — that is how a clean print
    gets swapped for a mixed cluster. --force overrides; --no-save avoids
    this path entirely.
    """
    cur = next((s for s in reg.get("speakers", [])
                if s.get("name") == person and s.get("model") == emb_model
                and s.get("embedding")), None)
    if not force and cur is not None:
        def unit(v):
            v = np.asarray(v, dtype=np.float32)
            return v / (np.linalg.norm(v) + 1e-9)
        sim = float(np.dot(unit(emb_vec), unit(cur["embedding"])))
        if sim < ref_threshold:
            sys.exit(f"diarize: FATAL registry: {cluster} matches '{person}' current print "
                     f"at {sim:.2f}; replacing it. Pass --force or use --no-save.")
    reg["speakers"] = [s for s in reg.get("speakers", [])
                       if not (s.get("name") == person and s.get("model") == emb_model)]
    # _registry_entry preserves unknown keys (e.g. "role") from the entry the
    # guard inspected, so a guarded re-save never drops a role tag.
    reg["speakers"].append(_registry_entry(person, emb_model, emb_vec.tolist(), base, old=cur))
    log(f"registry: saved {cluster} as '{person}' -> {SPEAKER_DB}")


def do_relabel(args) -> None:
    """Apply new cluster->name assignments from a cached sidecar, persist voiceprints
    to the registry, and re-render the transcript + cards. No re-diarization."""
    sidecar = Path(args.relabel)
    data = json.loads(sidecar.read_text())
    base = data["base"]
    outdir = Path(args.outdir) if args.outdir else sidecar.parent
    outdir.mkdir(parents=True, exist_ok=True)
    segs = data["segments"]
    speakers = sorted({s["speaker"] for s in segs})
    names = dict(data.get("names", {sp: sp for sp in speakers}))
    cluster_emb = {sp: np.array(v, dtype=np.float32) for sp, v in data.get("cluster_emb", {}).items()}
    emb_model = data.get("emb_model", EMB_NAME)

    reg = load_registry()
    explicit_roles: dict[str, str] = {}  # --save-role targets (sidecar even w/o entry)
    local_labels = dict(data.get("local_labels", {}))
    wrote_registry = False
    for spec in args.save_speaker:
        if "=" not in spec:
            sys.exit(f"diarize: FATAL bad relabel spec (want CLUSTER=NAME): {spec}")
        cluster, person = (x.strip() for x in spec.split("=", 1))
        if cluster not in speakers:
            sys.exit(f"diarize: FATAL relabel: unknown cluster '{cluster}' (have: {', '.join(speakers)})")
        names[cluster] = person
        if getattr(args, "no_save", False):
            local_labels[cluster] = {"name": person, "note": getattr(args, "note", None)}
            log(f"relabel: labeled {cluster} as '{person}' (transcript-only; registry untouched)")
        elif cluster in cluster_emb:
            save_print_guarded(reg, cluster, person, cluster_emb[cluster], emb_model, base,
                               args.ref_threshold, getattr(args, "force", False))
            local_labels.pop(cluster, None)
            wrote_registry = True
        else:
            log(f"WARN relabel: no voiceprint cached for {cluster}; renamed in transcript but not persisted")
    if wrote_registry:
        save_registry(reg)

    # ---- --save-role NAME=ROLE: tag speakers in the registry (and the sidecar
    # below) so downstream agents can rank action items (boss > self > others).
    role_touched = False
    for spec in args.save_role:
        name, _, role = spec.partition("=")  # format validated in main()
        name, role = name.strip(), normalize_role(role)
        entry = next((s for s in reg.get("speakers", [])
                      if s.get("name") == name and s.get("model") == emb_model), None)
        if entry is None:
            if name not in names.values():
                print(json.dumps({
                    "relabeled": True,
                    "error": f"--save-role: no saved speaker '{name}' for model "
                             f"{emb_model} (not in the registry and not assigned in "
                             f"this run). Save the voiceprint first, e.g. "
                             f"--save-speaker CLUSTER={name}",
                }))
                return
        else:
            if role:
                entry["role"] = role
            else:
                entry.pop("role", None)
            role_touched = True
        if role:
            explicit_roles[name] = role
    if role_touched:
        save_registry(reg)

    # --auto: re-run registry matching + absorb over the cached voiceprints, so a
    # sidecar produced before names were enrolled (or before the absorb pass
    # existed) picks them up with no re-diarization. Explicit CLUSTER=NAME specs
    # above already won (name_clusters only fills STILL-UNNAMED clusters).
    detect_mode = f"{len(speakers)} speakers (relabel)"
    if getattr(args, "auto", False):
        detect_mode = f"{len(speakers)} speakers (relabel --auto)"
        reg = load_registry()
        registry_entries = [] if args.no_registry else [
            s for s in reg.get("speakers", [])
            if s.get("model") == emb_model and s.get("embedding")]
        if registry_entries:
            log(f"registry: matching against {len(registry_entries)} known voice(s) [{emb_model}]")
        registry_matches: list = []
        name_clusters(cluster_emb, args.ref_threshold, args.absorb_threshold,
                      registry_entries, [], names, report=registry_matches)
        data["registry_matches"] = registry_matches

    # Final roles for the sidecar + rendering: explicit --save-role tags plus the
    # registry role of every name this run ended up using (covers --auto naming).
    roles: dict[str, str] = dict(explicit_roles)
    for nm in set(names.values()) | set(explicit_roles):
        if nm in roles:
            continue
        entry = next((s for s in reg.get("speakers", [])
                      if s.get("name") == nm and s.get("model") == emb_model and s.get("role")),
                     None)
        if entry:
            roles[nm] = entry["role"]

    data["names"] = names
    if roles:
        data["roles"] = roles
    else:
        data.pop("roles", None)  # never leave a stale roles map behind
    data["local_labels"] = local_labels
    sidecar.write_text(json.dumps(data, indent=2))
    turns = build_turns(segs, data.get("whisper_json"))
    render_outputs(outdir, base, segs, speakers, names, turns, args.snippets,
                   detect_mode, emb_name=emb_model,
                   count_warning=data.get("count_warning"), roles=roles or None,
                   local_names={v["name"] for v in local_labels.values()})
    print(json.dumps({"num_speakers": len(speakers), "clusters": names, "relabeled": True}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?", help="audio file to diarize (omit with --ensure-models-only)")
    ap.add_argument("--whisper-json", help="MLX-Whisper .json output to merge with")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--num-speakers", type=int, default=-1, help="-1 = auto-detect")
    ap.add_argument("--min-speakers", type=int, default=0, metavar="N",
                    help="lower bound for the auto-detected count (0 = no bound). "
                         "Ignored when --num-speakers gives an exact count.")
    ap.add_argument("--max-speakers", type=int, default=0, metavar="N",
                    help=f"upper bound for the auto-detected count (0 = no bound); also "
                         f"lowers the hard cap of {SPEAKER_CAP}. Ignored when "
                         f"--num-speakers gives an exact count.")
    ap.add_argument("--expected-speakers", action="append", default=[], metavar="NAME[,NAME...]",
                    help="names of people expected in this recording (comma-separated and/or "
                         "repeatable). Each must already be a known voice — a registry entry "
                         "for the active embedding model, or a --ref NAME. Clustering is then "
                         "ANCHORED: every turn within --anchor-threshold of one of these "
                         "voiceprints is pinned to that person, and only the remaining turns "
                         "are clustered into new speakers. A listed person who never speaks "
                         "is dropped, so a varying-attendance roster needs no exact count.")
    ap.add_argument("--anchor-threshold", type=float, default=default_anchor_threshold(),
                    help=f"min per-turn cosine for --expected-speakers to pin a turn to a known "
                         f"voice. Default {DEFAULT_ANCHOR_THRESHOLD:.2f}; env "
                         f"WHOSAID_ANCHOR_THRESHOLD. Turns below it fall through to ordinary "
                         f"clustering rather than taking a low-confidence name.")
    ap.add_argument("--ref", action="append", default=[], metavar="NAME=CLIP",
                    help="reference voice clip for naming a cluster (repeatable)")
    ap.add_argument("--ref-threshold", "--match-threshold", dest="ref_threshold",
                    type=float, default=default_ref_threshold(),
                    help="min cosine similarity to accept a registry/--ref match. "
                         "A cluster whose best candidate scores below it keeps its "
                         "anonymous SPEAKER_NN label. Default 0.50; "
                         "env WHOSAID_MATCH_THRESHOLD.")
    ap.add_argument("--absorb-threshold", type=float,
                    default=float(os.environ.get("WHOSAID_ABSORB_THRESHOLD", "0.85")),
                    help="min cosine similarity for a still-unnamed cluster to be absorbed "
                         "into a known voice (registry or --ref), so phantom splits of one "
                         "person merge into that person. Default 0.85; env WHOSAID_ABSORB_THRESHOLD.")
    ap.add_argument("--auto", action="store_true",
                    help="with --relabel: re-run registry matching + the absorb pass over the "
                         "sidecar's cached voiceprints (no CLUSTER=NAME needed, no re-diarization)")
    ap.add_argument("--save-speaker", action="append", default=[], metavar="CLUSTER=NAME",
                    help="persist a cluster's voiceprint under NAME in the local registry "
                         "(e.g. SPEAKER_02=Jane). Repeatable. Names it here and in future runs.")
    ap.add_argument("--save-role", action="append", default=[], metavar="NAME=ROLE",
                    help=f"tag a saved speaker with a role (e.g. --save-role Karen=boss; "
                         f"repeatable; with --relabel). Conventional roles: "
                         f"{', '.join(VALID_ROLES)} — free-form lowercase tags allowed; "
                         f"an empty ROLE removes the tag. The speaker must already be "
                         f"in the registry (or be saved via --save-speaker in the same run).")
    ap.add_argument("--no-save", action="store_true",
                    help="with --save-speaker/--relabel: label the cluster in the sidecar and "
                         "outputs only; never write the speaker registry")
    ap.add_argument("--force", action="store_true",
                    help="with --save-speaker/--relabel: replace a registry print even when "
                         "the new cluster's similarity to it is below the match threshold")
    ap.add_argument("--note", default=None, metavar="TEXT",
                    help="with --no-save: short provenance note stored with each local label")
    ap.add_argument("--no-registry", action="store_true",
                    help="do not auto-name clusters from the local speaker registry")
    ap.add_argument("--snippets", type=int, default=3,
                    help="representative snippets to show per speaker in the cards file (0=off)")
    ap.add_argument("--jobs", type=int, default=0,
                    help="parallel diarization workers for long audio (0=auto)")
    ap.add_argument("--chunk-seconds", type=float, default=0.0,
                    help="window length for parallel diarization (0=auto by --jobs; only long audio)")
    ap.add_argument("--no-chunk", action="store_true",
                    help="force single-process, whole-file diarization (disable chunking)")
    ap.add_argument("--relabel", metavar="SIDECAR.diarization.json",
                    help="apply CLUSTER=NAME assignments (via --save-speaker) to a cached "
                         "diarization sidecar and re-render outputs; no re-diarization")
    ap.add_argument("--ensure-models-only", action="store_true",
                    help="download/verify the sherpa models then exit; no audio needed")
    args = ap.parse_args()

    for spec in args.save_role:
        if "=" not in spec:
            ap.error(f"--save-role wants NAME=ROLE (got {spec!r})")
    if args.save_role and not args.relabel:
        ap.error("--save-role works with --relabel (tag speakers when re-rendering "
                 "a cached sidecar)")

    if args.ensure_models_only:
        ensure_models()
        log("models ready")
        return

    if args.relabel:
        do_relabel(args)
        return

    if not args.audio:
        ap.error("the following arguments are required: audio (unless --ensure-models-only is given)")

    import sherpa_onnx  # deferred: uv provides it

    ensure_models()

    outdir = Path(args.outdir or Path(args.audio).parent)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.name or Path(args.audio).stem

    if args.min_speakers < 0 or args.max_speakers < 0:
        sys.exit("diarize: FATAL --min-speakers/--max-speakers must be >= 0 (0 = no bound)")
    if args.min_speakers and args.max_speakers and args.min_speakers > args.max_speakers:
        sys.exit(f"diarize: FATAL --min-speakers {args.min_speakers} is greater than "
                 f"--max-speakers {args.max_speakers}; the range is empty")

    bound_note = ""
    if args.num_speakers < 0 and (args.min_speakers or args.max_speakers):
        lo = args.min_speakers or 1
        hi = args.max_speakers or SPEAKER_CAP
        bound_note = f", bounded {lo}-{hi}"
    detect_mode = (f"auto-detected{bound_note}" if args.num_speakers < 0
                   else f"as hinted (--num-speakers {args.num_speakers})")

    # Decide whether to diarize the whole file at once or split it into windows and
    # diarize them in parallel (much faster on long recordings — the feedback loop
    # goes from ~10 min to a couple of minutes).
    total_dur = probe_duration(args.audio)
    jobs = args.jobs if args.jobs and args.jobs > 0 else max(1, min((os.cpu_count() or 2) - 2, 8))
    if args.chunk_seconds and args.chunk_seconds > 0:
        chunk_seconds = args.chunk_seconds
    else:  # auto: ~`jobs` windows, but never shorter than 300s (keeps enough voice per chunk)
        chunk_seconds = max(300.0, float(math.ceil(total_dur / jobs))) if total_dur else 0.0
    # Auto-chunk only long audio (>15 min), but honor an EXPLICIT --chunk-seconds at any length.
    explicit_chunk = bool(args.chunk_seconds and args.chunk_seconds > 0)
    use_chunk = ((not args.no_chunk) and jobs > 1 and 0 < chunk_seconds < total_dur
                 and (total_dur > 900.0 or explicit_chunk))

    # Lazy embedder for --ref clip matching (the chunked path builds no in-main extractor).
    _ref_ex = {}

    def ref_embed(wave: np.ndarray) -> np.ndarray:
        if "fn" not in _ref_ex:
            ex = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)))
            _ref_ex["fn"] = make_embed(ex)
        return _ref_ex["fn"](wave)

    # ---- known voices, loaded BEFORE clustering so --expected-speakers can anchor it.
    # (Naming used to embed --ref clips only afterwards; anchoring needs the
    # voiceprints in hand while the turns are still being assigned.)
    registry_entries = [] if args.no_registry else registry_entries_for_model(load_registry())
    if registry_entries:
        log(f"registry: matching against {len(registry_entries)} known voice(s) [{EMB_NAME}]")

    ref_voices = []
    for spec in args.ref:
        if "=" not in spec:
            sys.exit(f"diarize: FATAL bad --ref (want NAME=CLIP): {spec}")
        ref_name, ref_path = spec.split("=", 1)
        ref_voices.append((ref_name, ref_embed(load_audio(ref_path))))

    # ---- --expected-speakers: resolve each name to a known voiceprint (issue #1 part 4).
    expected = [n.strip() for spec in args.expected_speakers
                for n in spec.split(",") if n.strip()]
    anchors = []
    if expected:
        known = {}
        for e in registry_entries:
            known.setdefault(e["name"], np.asarray(e["embedding"], dtype=np.float32))
        for rname, remb in ref_voices:  # a --ref clip wins: it is this recording's own audio
            known[rname] = np.asarray(remb, dtype=np.float32)
        if not known:
            sys.exit("diarize: FATAL --expected-speakers needs known voices, but none are "
                     f"available (no --ref clips and no registry entries for {EMB_NAME}"
                     f"{'; --no-registry is set' if args.no_registry else ''}). "
                     "Enroll a voice first (whosaid enroll) or pass --ref NAME=CLIP.")
        seen, missing = set(), []
        for name in expected:
            if name in seen:
                continue
            seen.add(name)
            if name not in known:
                missing.append(name)
            else:
                anchors.append((name, known[name]))
        if missing:
            sys.exit(f"diarize: FATAL --expected-speakers: unknown voice(s) "
                     f"{', '.join(repr(m) for m in missing)}. Known names for {EMB_NAME}: "
                     f"{', '.join(sorted(known)) if known else '(none)'}")
        if args.anchor_threshold < -1.0 or args.anchor_threshold > 1.0:
            sys.exit(f"diarize: FATAL --anchor-threshold {args.anchor_threshold} is outside "
                     f"[-1, 1]; it is a cosine similarity")
        log(f"expected speakers: {', '.join(n for n, _ in anchors)} "
            f"(anchoring at cosine >= {args.anchor_threshold:.2f})")
        detect_mode += ", registry-anchored"
        if not use_chunk:
            # Anchoring works on PER-TURN voiceprints, which only the chunked path
            # produces; the whole-file path hands clustering to sherpa's own
            # FastClustering, which has no hook for a prior. One window over the
            # whole file gives identical segmentation with per-turn embeddings.
            use_chunk = True
            chunk_seconds = max(total_dur, 1.0)
            log("--expected-speakers: using the chunked diarization path (one window over the "
                "whole file) — anchoring needs per-turn voiceprints, which sherpa's "
                "whole-file FastClustering does not expose")

    count_estimate = None
    anchor_info = None
    if use_chunk:
        log(f"audio {total_dur:.0f}s -> parallel diarization")
        segs, cluster_emb, speakers, count_estimate, anchor_info = diarize_parallel(
            args.audio, total_dur, args.num_speakers, jobs, chunk_seconds,
            args.min_speakers, args.max_speakers,
            anchors=anchors or None, anchor_threshold=args.anchor_threshold)
    else:
        samples = load_audio(args.audio)
        log(f"audio loaded: {len(samples) / SAMPLE_RATE:.0f}s (whole-file diarization)")
        # The whole-file path hands the count to sherpa's own FastClustering,
        # which takes an EXACT num_clusters or nothing — it has no notion of a
        # range. A degenerate range (min == max) is therefore the only bound we
        # can honour here; anything wider is announced as unenforced rather than
        # silently ignored. The agglomerative estimator only runs on the chunked
        # (>15 min) path, which is where issue #5's saturation was measured.
        whole_file_k = args.num_speakers
        if whole_file_k < 0 and args.min_speakers and args.min_speakers == args.max_speakers:
            whole_file_k = args.min_speakers
            log(f"whole-file diarization: --min-speakers == --max-speakers, "
                f"using an exact count of {whole_file_k}")
        elif whole_file_k < 0 and (args.min_speakers or args.max_speakers):
            log("WARN --min-speakers/--max-speakers are not enforced on the whole-file "
                "path (sherpa FastClustering takes an exact count only); pass equal "
                "min/max for an exact count, or --chunk-seconds to force the chunked path.")
        config = make_diar_config(whole_file_k)
        if not config.validate():
            sys.exit("diarize: FATAL invalid config (model files missing?)")
        result = sherpa_onnx.OfflineSpeakerDiarization(config).process(samples).sort_by_start_time()
        segs = [{"start": s.start, "end": s.end, "speaker": f"SPEAKER_{s.speaker:02d}"} for s in result]
        speakers = sorted({s["speaker"] for s in segs})
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)))
        cluster_emb = cluster_embeddings(samples, segs, make_embed(ex), key="speaker")

    if not segs:
        sys.exit("diarize: FATAL diarization produced zero segments")

    log("=" * 56)
    log(f"SPEAKERS DETECTED: {len(speakers)} ({detect_mode})")
    log("=" * 56)

    # per-speaker talk time, for the headline and the snippet cards below
    talk = {sp: 0.0 for sp in speakers}
    for s in segs:
        talk[s["speaker"]] += s["end"] - s["start"]
    for sp in speakers:
        log(f"  {sp}: {len([s for s in segs if s['speaker'] == sp])} turns, {hms(talk[sp])} talk time")
    log(f"{len(segs)} turns total")

    # Over-segmentation guard: auto-detect (FastClustering) can shatter a long
    # recording into dozens of phantom clusters. If the count looks implausible,
    # say so loudly and tell the user the one-flag fix rather than silently
    # emitting a 100-speaker transcript.
    count_warning = None
    if args.num_speakers < 0:
        tiny = [sp for sp in speakers if talk[sp] < 5.0]
        saturated = bool(count_estimate and count_estimate.get("saturated"))
        if saturated:
            eff_cap = count_estimate["max"] or count_estimate["cap"]
            count_warning = (
                f"speaker count is UNRELIABLE — the auto estimate hit its bound of "
                f"{eff_cap} (agglomerative at cosine {count_estimate['threshold']:.2f} "
                f"found {count_estimate['raw_k']} clusters), so {len(speakers)} is a "
                f"bound, not a measurement. Re-run with a known count "
                f"(--speakers N) or a range (--min-speakers/--max-speakers).")
        elif (len(speakers) > 12
                or (len(speakers) >= 6 and len(tiny) >= len(speakers) / 2)):
            count_warning = (
                f"speaker count may be UNRELIABLE — auto-detect found {len(speakers)} "
                f"speakers, {len(tiny)} of them with under 5s of speech, which usually "
                f"means over-segmentation on long or mixed audio. Re-run with a known "
                f"count (--speakers N) or a range (--min-speakers/--max-speakers).")
        if count_warning:
            log("!" * 56)
            for part in count_warning.split(". "):
                log(f"WARN {part.strip().rstrip('.')}.")
            log("!" * 56)

    names = {sp: sp for sp in speakers}

    # ---- name clusters: anchors (pre-seeded) -> registry one-best -> --ref -> absorb.
    # The last three passes live in name_clusters() so `relabel --auto` names identically;
    # anchored clusters are seeded into `names` FIRST, and since every pass only touches
    # still-unnamed clusters the anchor wins, while the absorb pass can still fold a
    # residual phantom split of the same person into them.
    registry_matches: list = []
    if anchor_info:
        for sp, name in anchor_info["anchored"].items():
            if sp in names:
                names[sp] = name
                stat = anchor_info["names"][name]
                registry_matches.append({
                    "cluster": sp, "name": name,
                    "similarity": float(stat["mean_cosine"]),
                    "threshold": float(anchor_info["threshold"]),
                    "matched": True, "pass": "anchor", "turns": int(stat["turns"]),
                })

    name_clusters(cluster_emb, args.ref_threshold, args.absorb_threshold,
                  registry_entries, ref_voices, names, report=registry_matches)

    # ---- persist identified speakers to the local registry (--save-speaker) ----
    local_labels: dict = {}
    if args.save_speaker:
        reg = load_registry()
        wrote_registry = False
        for spec in args.save_speaker:
            if "=" not in spec:
                sys.exit(f"diarize: FATAL bad --save-speaker (want CLUSTER=NAME): {spec}")
            cluster, person = spec.split("=", 1)
            cluster, person = cluster.strip(), person.strip()
            if cluster not in cluster_emb:
                sys.exit(f"diarize: FATAL --save-speaker: no voiceprint for cluster '{cluster}' "
                         f"(have: {', '.join(sorted(cluster_emb))})")
            if getattr(args, "no_save", False):
                names[cluster] = person
                local_labels[cluster] = {"name": person, "note": getattr(args, "note", None)}
                log(f"relabel: labeled {cluster} as '{person}' (transcript-only; registry untouched)")
                continue
            save_print_guarded(reg, cluster, person, cluster_emb[cluster], EMB_NAME, base,
                               args.ref_threshold, getattr(args, "force", False))
            names[cluster] = person
            local_labels.pop(cluster, None)
            wrote_registry = True
        if wrote_registry:
            save_registry(reg)

    # ---- per-speaker roles (registry "role" tags) for rendering + the sidecar ----
    roles: dict[str, str] = {}
    if not args.no_registry:
        reg_now = load_registry()
        for nm in set(names.values()):
            entry = next((s for s in reg_now.get("speakers", [])
                          if s.get("name") == nm and s.get("model") == EMB_NAME and s.get("role")),
                         None)
            if entry:
                roles[nm] = entry["role"]

    # ---- render RTTM + speaker-labeled transcript + snippet cards ----
    turns = build_turns(segs, args.whisper_json)
    render_outputs(outdir, base, segs, speakers, names, turns, args.snippets, detect_mode,
                   count_warning=count_warning, roles=roles or None,
                   local_names={v["name"] for v in local_labels.values()})

    # ---- sidecar: segments + voiceprints so `whosaid relabel` is instant later ----
    sidecar = outdir / f"{base}.diarization.json"
    sidecar_data = {
        "base": base,
        "emb_model": EMB_NAME,
        "num_speakers": len(speakers),
        "names": names,
        "local_labels": local_labels,
        "registry_matches": registry_matches,
        "source": source_metadata(args.audio),
        "detect_mode": detect_mode,
        "count_warning": count_warning,
        "count_estimate": count_estimate,
        "anchors": (anchor_info["names"] if anchor_info else None),
        "segments": segs,
        "cluster_emb": {sp: cluster_emb[sp].tolist() for sp in cluster_emb},
        "whisper_json": str(Path(args.whisper_json).resolve()) if args.whisper_json else None,
    }
    if roles:  # omit the key entirely when no speaker carries a role
        sidecar_data["roles"] = roles
    sidecar.write_text(json.dumps(sidecar_data, indent=2))

    print(json.dumps({
        "num_speakers": len(speakers),
        "detect_mode": detect_mode,
        "count_warning": count_warning,
        "count_estimate": count_estimate,
        "anchors": (anchor_info["names"] if anchor_info else None),
        "speakers": [names[sp] for sp in speakers],
        "clusters": {sp: names[sp] for sp in speakers},
        "registry_matches": registry_matches,
        "source": source_metadata(args.audio),
        "turns": len(segs),
    }))


if __name__ == "__main__":
    main()
