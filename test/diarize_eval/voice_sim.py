#!/usr/bin/env python3
"""Measure how separable the pool's voices are to whosaid's own speaker embedder.

TTS voices are not guaranteed to be different people as far as a voiceprint is
concerned: OpenAI's built-in voices score 0.55 cosine to each other on average
(max 0.85), above the ~0.25 between real people and, for the closest pairs,
above the 0.72 a real person's own turns reach at p10. A meeting with such a
pair is a test of the embedder, not of clustering, so run.py reports it apart.

Embeds every voice's enrollment clip with the same TitaNet-small extractor the
diarizer uses and writes <pool dir>/voice_sim.json: {"embedder", "ids", "sim"}.
Local only; nothing leaves the machine.

  uv run --with numpy --with sherpa-onnx python test/diarize_eval/voice_sim.py \
      [--pool ~/.cache/whosaid/diarize-eval/pool.json]
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "lib"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", default=os.path.expanduser("~/.cache/whosaid/diarize-eval/pool.json"))
    args = ap.parse_args()
    import diarize_sherpa as D
    import sherpa_onnx
    D.ensure_models()
    root = os.path.dirname(os.path.abspath(args.pool))
    voices = json.load(open(args.pool))["voices"]
    ids = sorted(v for v in voices if voices[v].get("enroll"))
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(D.EMB_MODEL)))
    embed = D.make_embed(ex)
    V = np.array([embed(D.load_audio(os.path.join(root, voices[v]["enroll"]["path"]))) for v in ids])
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    S = V @ V.T
    out = os.path.join(root, "voice_sim.json")
    with open(out, "w") as f:
        json.dump({"embedder": D.EMB_NAME, "ids": ids, "sim": np.round(S, 4).tolist()}, f)
    iu = np.triu_indices(len(ids), 1)
    print(f"wrote {out}: {len(ids)} voices, {len(iu[0])} pairs, "
          f"{int((S[iu] >= 0.60).sum())} at cosine >= 0.60")


if __name__ == "__main__":
    main()
