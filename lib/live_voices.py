"""
live_voices.py: names on a live transcript's lines, from the local speaker registry.

The meeting app's leg mixes everyone else on the call, so each "them" line is embedded
(the same TitaNet model diarization uses) and grouped into running voices: a line joins
the closest voice when it is close enough, else starts a new one. Each voice is named
by matching its running centroid against the registry, best first: the best voice takes
the name, and another voice shares it only when it is at least SIBLING-similar to that
voice (one person whose first lines started separate voices before their centroid
settled; voices never merge), or when that voiceprint is unmistakably its nearest (SHARE_MIN,
SHARE_MARGIN: the same person on a phone or far mic). A name learned later moves back onto
earlier lines. "me" lines get the registry's self-roled name. Lines too short to trust join a
voice but never start one; a short reply under JOIN goes to the voice it is clearly closest to
(SHORT_MARGIN, as #216 does for boundary backchannels after the call), else it is flagged.

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
SIBLING = 0.75      # a voice this close to the voice holding a name is the same person, split early
MIN_SECONDS = 1.5   # a shorter line joins a voice but never starts one or moves it
LOW_CONFIDENCE = 0.6  # a named line whose voice matches its voiceprint less than this is flagged mixed
SHARE_MIN = 0.45    # a voice this close to a voiceprint...
SHARE_MARGIN = 0.2  # ...and this far ahead of every other one is that person on another mic or a phone
SHORT_SECONDS = 0.3  # shorter lines are not embedded at all
SHORT_MIN = 0.25    # a short reply goes to the running voice it matches best, at least this well...
SHORT_MARGIN = 0.10  # ...and this far ahead of the next voice (diarize_sherpa.BACKCHANNEL_MARGIN, #216)
SANDWICH_MARGIN = 0.05  # the margin once the voice talking on both sides of it is left out
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
        self.last_short = False  # the last place(): a short line no running voice clearly won
        self.unsure: dict = {}   # key -> embedding of a short line no voice won yet (voted again later)
        self.close: dict = {}    # key -> the running voices a "close voices" line matched

    def short_vote(self, v, around: str | None = None) -> str | None:
        """The running voice a short line's embedding is clearly closest to, or None.
        `around` is the voice talking on both sides of the line: someone doesn't answer
        "Right." to their own sentence, so that voice is left out (#216 reassigns a
        boundary backchannel away from the turn it interrupts the same way) and the
        margin needed drops to SANDWICH_MARGIN. Fewer than two candidates is no vote."""
        np = self.np
        sims = {f"v{i + 1}": float(v @ (s / (np.linalg.norm(s) + 1e-9))) for i, s in enumerate(self.sums)}
        margin = SHORT_MARGIN
        if around in sims:
            del sims[around]
            margin = SANDWICH_MARGIN
        if len(sims) < 2:
            return None  # one candidate is no vote: the speaker may not have a voice yet
        ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[0][0] if ranked[0][1] >= SHORT_MIN and ranked[0][1] - ranked[1][1] >= margin else None

    def place(self, samples, key=None) -> str | None:
        """The voice id ("v1", "v2", ...) for one line's samples, or None. A short line no
        voice clearly wins is kept under `key` and voted again by annotate() once more
        voices exist (its speaker may not have said anything long yet)."""
        np = self.np
        self.last_close = self.last_short = False
        dur = len(samples) / RATE
        if dur < SHORT_SECONDS:
            self.last_short = bool(self.sums)
            return None
        v = self.embed(samples)
        sims = [float(v @ (s / (np.linalg.norm(s) + 1e-9))) for s in self.sums]
        best = int(np.argmax(sims)) if sims else -1
        close = [f"v{i + 1}" for i, s in enumerate(sims) if s >= JOIN]
        self.last_close = len(close) >= 2
        if self.last_close and key is not None:
            self.close[key] = close
        if best >= 0 and sims[best] >= JOIN:
            if dur >= MIN_SECONDS:
                self.sums[best] = self.sums[best] + v * dur
            return f"v{best + 1}"
        if dur < MIN_SECONDS:
            # a short reply ("Right.", "Got it.") is too short to reach JOIN; like #216's
            # boundary backchannels it goes to the voice it is clearly closest to, else it
            # is left without one and flagged
            if (got := self.short_vote(v)):
                return got
            self.last_short = True
            if key is not None:
                self.unsure[key] = v
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
            lead = float(sims[order[0]]) - second
            clear = sims[order[0]] >= SHARE_MIN and lead >= SHARE_MARGIN
            if (sims[order[0]] >= NAME_MIN and lead >= MARGIN) or clear:
                cands.append((float(sims[order[0]]), i, int(order[0]), clear))
        out, holder = {}, {}
        unit = lambda s: s / (np.linalg.norm(s) + 1e-9)
        for _, i, k, clear in sorted(cands, reverse=True):
            name, role, _ = self.known[k]
            if self.me is not None and name == self.me[0]:
                continue
            # best voice takes the name; another voice shares it when it sounds like that
            # voice itself (one speaker split into several running voices early on), or when
            # the voiceprint is unmistakably its nearest (the same person dialed in by phone
            # or on a far mic sounds unlike their own near voice, but like nobody else)
            if name not in holder or clear or float(unit(self.sums[i]) @ unit(self.sums[holder[name]])) >= SIBLING:
                out[f"v{i + 1}"] = (name, role)
                holder.setdefault(name, i)
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
        when they are under CHANGE_SIM, a side being the windows before and after a middle
        stretch (A-B-A) or one end; the first boundary is returned, a caller wanting every
        change splits again. Shorter than two windows: never split."""
        np = self.np
        win = int(MIN_SECONDS * RATE)
        if len(samples) < 2 * win:
            return None
        hop = max(RATE // 2, (len(samples) - win) // CHANGE_WINDOWS)
        starts = list(range(0, len(samples) - win + 1, hop))
        embs = [self.embed(samples[s:s + win]) for s in starts]
        def cos(a, b) -> float:
            return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
        n, best = len(embs), None
        # windows [i, j) against the rest: i == 0 or j == n is a plain two-sided split; a
        # middle stretch finds A-B-A (someone talks over a long segment and the first
        # speaker carries on), where both halves hold A and look alike
        for i in range(n):
            for j in range(i + 1, n + 1):
                if (i, j) == (0, n):
                    continue
                inside = np.sum(embs[i:j], axis=0)
                rest = np.sum(embs[:i] + embs[j:], axis=0)
                sim = cos(inside, rest)
                if best is None or sim < best[0] - 1e-6:
                    best = (sim, i or j)
        if best is None or best[0] >= CHANGE_SIM:
            return None
        k = best[1]
        return round(((starts[k - 1] + starts[k]) / 2 + win / 2) / RATE, 2)  # between the two window centres

    def annotate(self, entries: list[dict]) -> None:
        """Set (or clear) "name", "role", "confidence", "nearest" and "mixed" on every
        line from the current voices. "mixed_why" (set when the line was transcribed:
        a change inside its segment, or two voices at once) is kept as is."""
        them = [e for e in entries if e.get("who") != "me"]
        for i, e in enumerate(them):
            key = (e.get("t"), e.get("text"))
            if e.get("voice") or key not in self.unsure:
                continue
            prev = them[i - 1].get("voice") if i else None
            nxt = them[i + 1].get("voice") if i + 1 < len(them) else None
            if (got := self.short_vote(self.unsure[key], prev if prev and prev == nxt else None)):
                e["voice"] = got
                del self.unsure[key]
                if e.get("mixed_why") == "short reply":
                    e.pop("mixed_why")
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
            why = e.get("mixed_why")
            ids = self.close.get((e.get("t"), e.get("text")))
            if why == "close voices" and ids and len({(names.get(i) or (i,))[0] for i in ids}) == 1:
                why = None  # the voices it matched are one person (near and far mic, #218)
            if why or (got and e.get("who") != "me" and e.get("confidence", 1.0) < LOW_CONFIDENCE):
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
