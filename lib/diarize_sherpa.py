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
      [--ref Name=clip.m4a ...] [--ref-threshold 0.40]

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


def load_registry() -> dict:
    """Return {"speakers": [{"name","model","embedding":[...],"added"}...]}."""
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


# When auto-detect saturates at this many clusters we stop trusting the count:
# on a long, noisy recording every window looks a little different and the
# farthest-first pass pins at the cap, which then feeds k-means a k of 20 and
# shatters real voices into phantom speakers.
SPEAKER_CAP = 20


def _farthest_first_k(X: np.ndarray, thresh: float, cap: int) -> int:
    centers = [X[0]]
    for x in X[1:]:
        if max(float(x @ c) for c in centers) < thresh:
            centers.append(x)
            if len(centers) >= cap:
                break
    return len(centers)


def estimate_k(X: np.ndarray, thresh: float = 0.5, cap: int = SPEAKER_CAP) -> int:
    """Coarse farthest-first estimate of the speaker count when none is given.

    A single greedy pass opens a new center whenever a turn's best similarity to
    the existing centers falls below `thresh`, so `thresh` is really a merge
    radius: a LOWER threshold merges more aggressively and yields FEWER clusters,
    a higher one splits more and yields more. (NB the merge direction is the
    opposite of what a "raise the threshold to collapse clusters" intuition
    suggests — verified empirically against TitaNet-small embeddings.)

    On long recordings the pass saturates at `cap`; handing k-means a k we know
    is wrong is what splits real voices, so when we hit the cap we retry with
    progressively LOWER thresholds until the estimate drops below it.
    """
    k = _farthest_first_k(X, thresh, cap)
    if k < cap:
        return k
    for t in (0.45, 0.40, 0.35, 0.30, 0.25):
        if t >= thresh:
            continue
        k2 = _farthest_first_k(X, t, cap)
        if k2 < cap:
            log(f"auto-detect saturated at cap {cap} at thresh {thresh:.2f}; "
                f"re-estimated k={k2} at thresh {t:.2f}")
            return k2
    log(f"auto-detect saturated at cap {cap} at thresh {thresh:.2f}; still k={k} "
        f"after re-estimating down to thresh 0.25")
    return k


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


def cluster_segments(all_segments: list, num_speakers: int) -> tuple:
    """Assign every segment a global speaker by clustering ALL per-turn voiceprints at
    once — a global view that matches whole-file quality even though the segmentation
    ran chunk-by-chunk. Returns (segments with SPEAKER_NN, {SPEAKER_NN: centroid}, speakers)."""
    embedded = [s for s in all_segments if s.get("emb")]
    if not embedded:
        return [], {}, []
    X = np.array([s["emb"] for s in embedded], dtype=np.float32)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    k = num_speakers if num_speakers and num_speakers > 0 else estimate_k(X)
    k = max(1, min(k, len(embedded)))
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
    return segs, cluster_emb, sorted(cluster_emb)


def diarize_parallel(audio: str, total_dur: float, num_speakers: int, jobs: int,
                     chunk_seconds: float) -> tuple:
    """Split the audio into windows, segment+embed them concurrently, then cluster
    globally. Returns (segments, {SPEAKER_NN: embedding}, speakers)."""
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
    return cluster_segments(all_segments, num_speakers)


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
                   emb_name: str = EMB_NAME) -> None:
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
            f.write(f"# Speakers ({len(speakers)}): {', '.join(sorted(set(names.values())))}\n\n")
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
             f"Read the snippets, then persist names with:",
             f"#   whosaid relabel {base} SPEAKER_XX=Name [SPEAKER_YY=Name ...]", ""]
    for fn in groups:
        clusters = members[fn]
        g_turns = sum(nturns[sp] for sp in clusters)
        g_talk = sum(talk[sp] for sp in clusters)
        if fn in clusters:            # unnamed: final name IS the cluster id
            label = f"{fn}  (UNIDENTIFIED)"
        elif len(clusters) > 1:
            label = f"{fn}  ({', '.join(clusters)})"
        else:
            label = fn
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


def name_clusters(cluster_emb: dict, ref_threshold: float, absorb_threshold: float,
                  registry_entries: list, ref_voices: list | None = None,
                  names: dict | None = None) -> dict:
    """Assign real names to anonymous clusters in three passes and return the
    {SPEAKER_NN: name-or-self} map. Shared by the transcribe path and
    `relabel --auto` so both name clusters identically.

    Passes (each only touches STILL-UNNAMED clusters, so earlier/explicit names win):
      1. registry one-best — each known voiceprint claims its single best cluster
         when cosine >= ref_threshold (default 0.40).
      2. --ref clips — each reference voice claims its best cluster (>= ref_threshold),
         but a ref whose name the registry already assigned is skipped, so one
         person never lands on two cards.
      3. absorb — every cluster still unnamed whose centroid cosine to ANY known
         voice (registry entries AND --ref voices) is >= absorb_threshold takes
         that name. Multiple clusters may share a name; the cards merge them.

    Absorb-threshold rationale (0.85 default): same-speaker TitaNet-small centroids
    measured 0.90-0.95 across window splits, while distinct speakers stayed <= 0.73,
    so 0.85 folds phantom splits back together without swallowing real strangers.

    `registry_entries` : list of {"name","embedding"} for the active model.
    `ref_voices`       : list of (name, embedding) already-embedded --ref clips.
    """
    ref_voices = ref_voices or []
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
    # absorb_threshold, so the loose 0.40 gate can't annex a second, low-confidence
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
        if sims[best] >= ref_threshold:
            names[best] = entry_name
            assigned.add(entry_name)
            registry_named.add(entry_name)
            log(f"  registry: {best} -> {entry_name} (sim {sims[best]:.3f})")

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
        if cands[bn] >= absorb_threshold:
            names[sp] = bn
            log(f"  absorb: {sp} -> {bn} (sim {cands[bn]:.3f})")
    return names


def do_relabel(args) -> None:
    """Apply new cluster->name assignments from a cached sidecar, persist voiceprints
    to the registry, and re-render the transcript + cards. No re-diarization."""
    sidecar = Path(args.relabel)
    data = json.loads(sidecar.read_text())
    base = data["base"]
    outdir = Path(args.outdir) if args.outdir else sidecar.parent
    segs = data["segments"]
    speakers = sorted({s["speaker"] for s in segs})
    names = dict(data.get("names", {sp: sp for sp in speakers}))
    cluster_emb = {sp: np.array(v, dtype=np.float32) for sp, v in data.get("cluster_emb", {}).items()}
    emb_model = data.get("emb_model", EMB_NAME)

    reg = load_registry()
    for spec in args.save_speaker:
        if "=" not in spec:
            sys.exit(f"diarize: FATAL bad relabel spec (want CLUSTER=NAME): {spec}")
        cluster, person = (x.strip() for x in spec.split("=", 1))
        if cluster not in speakers:
            sys.exit(f"diarize: FATAL relabel: unknown cluster '{cluster}' (have: {', '.join(speakers)})")
        names[cluster] = person
        if cluster in cluster_emb:
            reg["speakers"] = [s for s in reg.get("speakers", [])
                               if not (s.get("name") == person and s.get("model") == emb_model)]
            reg["speakers"].append({"name": person, "model": emb_model,
                                    "embedding": cluster_emb[cluster].tolist(), "added": base})
            log(f"registry: saved {cluster} as '{person}' -> {SPEAKER_DB}")
        else:
            log(f"WARN relabel: no voiceprint cached for {cluster}; renamed in transcript but not persisted")
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
        name_clusters(cluster_emb, args.ref_threshold, args.absorb_threshold,
                      registry_entries, [], names)

    data["names"] = names
    sidecar.write_text(json.dumps(data, indent=2))
    turns = build_turns(segs, data.get("whisper_json"))
    render_outputs(outdir, base, segs, speakers, names, turns, args.snippets,
                   detect_mode, emb_name=emb_model)
    print(json.dumps({"num_speakers": len(speakers), "clusters": names, "relabeled": True}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?", help="audio file to diarize (omit with --ensure-models-only)")
    ap.add_argument("--whisper-json", help="MLX-Whisper .json output to merge with")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--num-speakers", type=int, default=-1, help="-1 = auto-detect")
    ap.add_argument("--ref", action="append", default=[], metavar="NAME=CLIP",
                    help="reference voice clip for naming a cluster (repeatable)")
    ap.add_argument("--ref-threshold", type=float, default=0.40,
                    help="min cosine similarity to accept a reference match")
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

    detect_mode = "auto-detected" if args.num_speakers < 0 else f"as hinted (--num-speakers {args.num_speakers})"

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

    if use_chunk:
        log(f"audio {total_dur:.0f}s -> parallel diarization")
        segs, cluster_emb, speakers = diarize_parallel(
            args.audio, total_dur, args.num_speakers, jobs, chunk_seconds)
    else:
        samples = load_audio(args.audio)
        log(f"audio loaded: {len(samples) / SAMPLE_RATE:.0f}s (whole-file diarization)")
        config = make_diar_config(args.num_speakers)
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
    if args.num_speakers < 0:
        tiny = [sp for sp in speakers if talk[sp] < 5.0]
        if (len(speakers) > 12 or len(speakers) == SPEAKER_CAP
                or (len(speakers) >= 6 and len(tiny) >= len(speakers) / 2)):
            log("!" * 56)
            log(f"WARN auto-detect found {len(speakers)} speakers ({len(tiny)} with <5s of speech).")
            log("WARN this usually means over-segmentation on long/mixed audio.")
            log("WARN re-run with a known count, e.g.  --speakers 5  (whosaid: --speakers 5).")
            log("!" * 56)

    names = {sp: sp for sp in speakers}

    # ---- name clusters: registry one-best -> --ref clips -> absorb phantom splits.
    # All three passes live in name_clusters() so `relabel --auto` names identically.
    registry_entries = [] if args.no_registry else registry_entries_for_model(load_registry())
    if registry_entries:
        log(f"registry: matching against {len(registry_entries)} known voice(s) [{EMB_NAME}]")

    ref_voices = []
    for spec in args.ref:
        if "=" not in spec:
            sys.exit(f"diarize: FATAL bad --ref (want NAME=CLIP): {spec}")
        ref_name, ref_path = spec.split("=", 1)
        ref_voices.append((ref_name, ref_embed(load_audio(ref_path))))

    name_clusters(cluster_emb, args.ref_threshold, args.absorb_threshold,
                  registry_entries, ref_voices, names)

    # ---- persist identified speakers to the local registry (--save-speaker) ----
    if args.save_speaker:
        reg = load_registry()
        for spec in args.save_speaker:
            if "=" not in spec:
                sys.exit(f"diarize: FATAL bad --save-speaker (want CLUSTER=NAME): {spec}")
            cluster, person = spec.split("=", 1)
            cluster, person = cluster.strip(), person.strip()
            if cluster not in cluster_emb:
                sys.exit(f"diarize: FATAL --save-speaker: no voiceprint for cluster '{cluster}' "
                         f"(have: {', '.join(sorted(cluster_emb))})")
            emb = cluster_emb[cluster].tolist()
            reg["speakers"] = [s for s in reg.get("speakers", [])
                               if not (s.get("name") == person and s.get("model") == EMB_NAME)]
            reg["speakers"].append({"name": person, "model": EMB_NAME,
                                    "embedding": emb, "added": base})
            names[cluster] = person
            log(f"registry: saved {cluster} as '{person}' -> {SPEAKER_DB}")
        save_registry(reg)

    # ---- render RTTM + speaker-labeled transcript + snippet cards ----
    turns = build_turns(segs, args.whisper_json)
    render_outputs(outdir, base, segs, speakers, names, turns, args.snippets, detect_mode)

    # ---- sidecar: segments + voiceprints so `whosaid relabel` is instant later ----
    sidecar = outdir / f"{base}.diarization.json"
    sidecar.write_text(json.dumps({
        "base": base,
        "emb_model": EMB_NAME,
        "num_speakers": len(speakers),
        "names": names,
        "segments": segs,
        "cluster_emb": {sp: cluster_emb[sp].tolist() for sp in cluster_emb},
        "whisper_json": str(Path(args.whisper_json).resolve()) if args.whisper_json else None,
    }, indent=2))

    print(json.dumps({
        "num_speakers": len(speakers),
        "detect_mode": detect_mode,
        "speakers": [names[sp] for sp in speakers],
        "clusters": {sp: names[sp] for sp in speakers},
        "turns": len(segs),
    }))


if __name__ == "__main__":
    main()
