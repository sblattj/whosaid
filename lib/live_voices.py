"""
live_voices.py: names on a live transcript's lines, from the local speaker registry.

The meeting app's leg mixes everyone else on the call, so each "them" line is embedded
(the same TitaNet model diarization uses) and grouped into running voices: a line joins
the closest voice when it is close enough, else starts a new one. Each voice is named
by matching its running centroid against the registry, one voice per name, best first,
so a name learned later moves back onto that voice's earlier lines. "me" lines get the
registry's self-roled name. Lines too short to trust join a voice but never start one.

Each "them" line also gets a "confidence": its voice's cosine to the voiceprint that
named it or, for a voice left unnamed, to the "nearest" registry voice (so a second
cluster of a known person shows as nearest that person). A line is "mixed" when its
segment held a speaker change (change_point), sounded like two running voices at once,
or its name is a weak match (under LOW_CONFIDENCE).

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
LOW_CONFIDENCE = 0.6  # a named line whose voice matches its voiceprint less than this is flagged mixed
CHANGE_SIM = 0.5    # the two sides of a segment this unlike (cosine) are two people
CHANGE_WINDOWS = 16  # at most this many embedding windows per segment when looking for a change
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
        self.last_close = False  # the last place(): two running voices both matched it

    def place(self, samples) -> str | None:
        """The voice id ("v1", "v2", ...) for one line's samples, or None."""
        np = self.np
        self.last_close = False
        dur = len(samples) / RATE
        if dur < 0.5:
            return None
        v = self.embed(samples)
        sims = [float(v @ (s / (np.linalg.norm(s) + 1e-9))) for s in self.sums]
        best = int(np.argmax(sims)) if sims else -1
        self.last_close = sum(s >= JOIN for s in sims) >= 2
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

    def nearest(self) -> dict[str, tuple[str, float]]:
        """{voice id: (registry name, cosine)} for every voice: the closest voiceprint other
        than the self one, named or not (a voice whose best name another voice took still
        shows it here)."""
        np = self.np
        if self.K is None or not self.sums:
            return {}
        out = {}
        for i, s in enumerate(self.sums):
            sims = self.K @ (s / (np.linalg.norm(s) + 1e-9))
            for k in np.argsort(-sims):
                name = self.known[int(k)][0]
                if self.me is None or name != self.me[0]:
                    out[f"v{i + 1}"] = (name, float(sims[int(k)]))
                    break
        return out

    def change_point(self, samples) -> float | None:
        """Seconds into one segment where the speaker changes, or None. The segment is
        embedded in MIN_SECONDS windows; the split whose two sides are least alike wins
        when they are under CHANGE_SIM. Shorter than two windows: never split."""
        np = self.np
        win = int(MIN_SECONDS * RATE)
        if len(samples) < 2 * win:
            return None
        hop = max(RATE // 2, (len(samples) - win) // CHANGE_WINDOWS)
        starts = list(range(0, len(samples) - win + 1, hop))
        embs = [self.embed(samples[s:s + win]) for s in starts]
        best = None
        for k in range(1, len(embs)):
            left, right = np.sum(embs[:k], axis=0), np.sum(embs[k:], axis=0)
            sim = float(left @ right / (np.linalg.norm(left) * np.linalg.norm(right) + 1e-9))
            if best is None or sim < best[0]:
                best = (sim, k)
        if best is None or best[0] >= CHANGE_SIM:
            return None
        k = best[1]
        return round(((starts[k - 1] + starts[k]) / 2 + win / 2) / RATE, 2)  # between the two window centres

    def annotate(self, entries: list[dict]) -> None:
        """Set (or clear) "name", "role", "confidence", "nearest" and "mixed" on every
        line from the current voices. "mixed_why" (set when the line was transcribed:
        a change inside its segment, or two voices at once) is kept as is."""
        names, near = self.names(), self.nearest()
        for e in entries:
            vid = e.get("voice") or ""
            got = self.me if e.get("who") == "me" else names.get(vid)
            for k in ("name", "role", "confidence", "nearest", "mixed"):
                e.pop(k, None)
            if got:
                e["name"] = display(got[0])
                if got[1]:
                    e["role"] = got[1]
            if e.get("who") != "me" and vid in near:
                e["confidence"] = round(near[vid][1], 2)
                if not got:
                    e["nearest"] = display(near[vid][0])
            if e.get("mixed_why") or (got and e.get("who") != "me" and e.get("confidence", 1.0) < LOW_CONFIDENCE):
                e["mixed"] = True


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
