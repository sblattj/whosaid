"""
live_voices.py: names on a live transcript's lines, from the local speaker registry.

The meeting app's leg mixes everyone else on the call, so each "them" line is embedded
(the same TitaNet model diarization uses) and grouped into running voices: a line joins
the closest voice when it is close enough, else starts a new one. Each voice is named
by matching its running centroid against the registry, one voice per name, best first,
so a name learned later moves back onto that voice's earlier lines. "me" lines get the
registry's self-roled name. Lines too short to trust join a voice but never start one.

A live guess: the ingest after the call still diarizes the whole recording and decides
the names that stick. Without sherpa-onnx, the embedding model or a registry, lines
stay "me"/"them" as before. WHOSAID_LIVE_NAMES=0 turns it off.

Measured on four recorded calls (per Whisper segment, app leg only): lines whose voice
the ingest named came out about 98% right; voices the ingest left unnamed often got a
name here (most look like a second cluster of a known voice, unconfirmed).
"""
from __future__ import annotations

import os
import sys

JOIN = 0.6          # cosine to a running voice's centroid to count as that voice
NAME_MIN = 0.55     # centroid vs registry voiceprint to put a name on a voice
MARGIN = 0.05       # ...and at least this far ahead of the next registry voice
MIN_SECONDS = 1.5   # a shorter line joins a voice but never starts one or moves it
RATE = 16000


def display(name: str) -> str:
    return name.replace("_", " ").strip()


class Voices:
    """Running voices for one call. `known` is [(name, role, unit vector)]; `embed` maps
    float32 samples at 16 kHz to a unit vector; `me` is the self-roled (name, role)."""

    def __init__(self, known: list, embed, me: tuple[str, str] | None = None):
        import numpy as np
        self.np = np
        self.known = known
        self.K = np.stack([k[2] for k in known]) if known else None
        self.embed = embed
        self.me = me
        self.sums: list = []   # duration-weighted embedding sum per voice
        self._names: dict[str, tuple[str, str]] = {}

    def place(self, samples) -> str | None:
        """The voice id ("v1", "v2", ...) for one line's samples, or None."""
        np = self.np
        dur = len(samples) / RATE
        if dur < 0.5:
            return None
        v = self.embed(samples)
        sims = [float(v @ (s / (np.linalg.norm(s) + 1e-9))) for s in self.sums]
        best = int(np.argmax(sims)) if sims else -1
        if best >= 0 and sims[best] >= JOIN:
            if dur >= MIN_SECONDS:
                self.sums[best] = self.sums[best] + v * dur
            return f"v{best + 1}"
        if dur < MIN_SECONDS:
            return None
        self.sums.append(v * dur)
        return f"v{len(self.sums)}"

    def names(self) -> dict[str, tuple[str, str]]:
        """{voice id: (name, role)} for the voices a registry voiceprint claims."""
        np = self.np
        if self.K is None or not self.sums:
            return {}
        cands = []
        for i, s in enumerate(self.sums):
            sims = self.K @ (s / (np.linalg.norm(s) + 1e-9))
            order = np.argsort(-sims)
            second = float(sims[order[1]]) if len(order) > 1 else -1.0
            if sims[order[0]] >= NAME_MIN and sims[order[0]] - second >= MARGIN:
                cands.append((float(sims[order[0]]), i, int(order[0])))
        out, used = {}, set()
        for _, i, k in sorted(cands, reverse=True):
            name, role, _ = self.known[k]
            if name not in used and (self.me is None or name != self.me[0]):
                out[f"v{i + 1}"] = (name, role)
                used.add(name)
        return out

    def annotate(self, entries: list[dict]) -> None:
        """Set (or clear) "name" and "role" on every line from the current voices."""
        names = self.names()
        for e in entries:
            got = self.me if e.get("who") == "me" else names.get(e.get("voice") or "")
            e.pop("name", None)
            e.pop("role", None)
            if got:
                e["name"] = display(got[0])
                if got[1]:
                    e["role"] = got[1]


def load(log=lambda m: print(m, file=sys.stderr)) -> Voices | None:
    """Voices over the registry, or None when names can't be had (logged once)."""
    if os.environ.get("WHOSAID_LIVE_NAMES", "1").strip() == "0":
        return None
    try:
        import numpy as np
        import sherpa_onnx
        import diarize_sherpa as ds
    except ImportError as e:
        log(f"live names off: {e}")
        return None
    reg = ds.registry_entries_for_model(ds.load_registry())
    if not reg or not ds.EMB_MODEL.exists():
        log("live names off: no registry voices" if not reg else f"live names off: no model at {ds.EMB_MODEL}")
        return None
    known = []
    for e in reg:
        v = np.asarray(e["embedding"], dtype=np.float32)
        known.append((e["name"], (e.get("role") or ""), v / (np.linalg.norm(v) + 1e-9)))
    me = next(((n, r) for n, r, _ in known if r == "self"), None)
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(ds.EMB_MODEL)))
    log(f"live names on: {len(known)} registry voice(s)")
    return Voices(known, ds.make_embed(ex), me)
