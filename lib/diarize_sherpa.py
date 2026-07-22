#!/usr/bin/env python3
"""
diarize_sherpa.py: fully local speaker diarization + speaker-labeled transcripts.

Runs sherpa-onnx offline speaker diarization (pyannote segmentation-3.0 ONNX +
3D-Speaker ERes2Net embeddings, ungated GitHub-release models, CPU) over an audio
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
import json
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
# The release tag really is misspelled upstream; try both spellings.
EMB_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongnition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
]
EMB_MODEL = CACHE / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"

SAMPLE_RATE = 16000


def log(msg: str) -> None:
    print(f"diarize: {msg}", file=sys.stderr)


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
    ap.add_argument("--ensure-models-only", action="store_true",
                    help="download/verify the sherpa models then exit; no audio needed")
    args = ap.parse_args()

    if args.ensure_models_only:
        ensure_models()
        log("models ready")
        return

    if not args.audio:
        ap.error("the following arguments are required: audio (unless --ensure-models-only is given)")

    import sherpa_onnx  # deferred: uv provides it

    ensure_models()

    outdir = Path(args.outdir or Path(args.audio).parent)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.name or Path(args.audio).stem

    samples = load_audio(args.audio)
    log(f"audio loaded: {len(samples) / SAMPLE_RATE:.0f}s")

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=args.num_speakers, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        sys.exit("diarize: FATAL invalid config (model files missing?)")

    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    result = sd.process(samples).sort_by_start_time()
    segs = [{"start": s.start, "end": s.end, "speaker": f"SPEAKER_{s.speaker:02d}"} for s in result]
    if not segs:
        sys.exit("diarize: FATAL diarization produced zero segments")
    speakers = sorted({s["speaker"] for s in segs})
    log(f"{len(segs)} turns across {len(speakers)} speakers")

    # ---- enrollment: name clusters by cosine similarity to reference clips ----
    names = {sp: sp for sp in speakers}
    if args.ref:
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)))

        def embed(wave: np.ndarray) -> np.ndarray:
            st = ex.create_stream()
            st.accept_waveform(SAMPLE_RATE, wave)
            st.input_finished()
            v = np.array(ex.compute(st), dtype=np.float32)
            return v / (np.linalg.norm(v) + 1e-9)

        # per-cluster embedding from up to ~40s of its longest turns
        cluster_emb = {}
        for sp in speakers:
            turns = sorted((s for s in segs if s["speaker"] == sp),
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
                cluster_emb[sp] = embed(np.concatenate(chunks))

        for spec in args.ref:
            if "=" not in spec:
                sys.exit(f"diarize: FATAL bad --ref (want NAME=CLIP): {spec}")
            ref_name, ref_path = spec.split("=", 1)
            ref_emb = embed(load_audio(ref_path))
            sims = {sp: float(np.dot(ref_emb, e)) for sp, e in cluster_emb.items()}
            best = max(sims, key=sims.get)
            log(f"ref {ref_name}: " + ", ".join(f"{sp}={v:.3f}" for sp, v in sorted(sims.items())))
            if sims[best] >= args.ref_threshold:
                names[best] = ref_name
            else:
                log(f"WARN ref {ref_name}: best similarity {sims[best]:.3f} < {args.ref_threshold}, cluster left unnamed")

    # ---- RTTM ----
    rttm_path = outdir / f"{base}.rttm"
    with open(rttm_path, "w") as f:
        for s in segs:
            f.write(f"SPEAKER {base} 1 {s['start']:.3f} {s['end'] - s['start']:.3f} "
                    f"<NA> <NA> {names[s['speaker']]} <NA> <NA>\n")
    log(f"wrote {rttm_path}")

    # ---- merge with whisper segments into a speaker-labeled transcript ----
    if args.whisper_json:
        wj = json.loads(Path(args.whisper_json).read_text())
        wsegs = wj.get("segments", [])

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

        turns = []  # (speaker, start, [texts])
        for w in wsegs:
            txt = (w.get("text") or "").strip()
            if not txt:
                continue
            sp = speaker_for(w["start"], w["end"])
            if turns and turns[-1][0] == sp:
                turns[-1][2].append(txt)
            else:
                turns.append((sp, w["start"], [txt]))

        out_path = outdir / f"{base}.speakers.txt"
        with open(out_path, "w") as f:
            f.write(f"# Speaker-labeled transcript: {base}\n")
            f.write(f"# Diarization: sherpa-onnx (pyannote segmentation-3.0 + ERes2Net), local.\n")
            f.write(f"# Speakers: {', '.join(sorted(set(names.values())))}\n\n")
            for sp, start, texts in turns:
                f.write(f"[{hms(start)}] {names[sp]}: {' '.join(texts)}\n\n")
        log(f"wrote {out_path} ({len(turns)} speaker turns)")

    print(json.dumps({"speakers": [names[sp] for sp in speakers], "turns": len(segs)}))


if __name__ == "__main__":
    main()
