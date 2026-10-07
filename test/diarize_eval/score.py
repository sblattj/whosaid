"""Scorer for the synthetic-meeting diarization eval (stdlib only, Python 3.9+).

Pure functions; the only file I/O is the optional load_truth / load_hyp pair.

Definitions
-----------
Frames: 10 ms. Every truth turn and hyp segment is snapped to frame
boundaries with round(t * 100); the timeline is swept exactly over the
elementary intervals between boundaries (no per-frame arrays).

Speakers: n_true = distinct speaker names across truth turns. n_hyp = distinct
hyp labels with >= 0.5 s total talk, minus any label the run's sidecar lists as
`uncounted` (#69: a nameless cluster with under 1 s of talk; absent key = none,
so older outputs score as before); n_hyp_raw = all distinct labels. DER keeps an
uncounted label's time (it maps to no truth speaker).

Mapping (hyp label -> truth name or None): an optimal 1:1 assignment that
maximizes overlap seconds (Hungarian algorithm). blind: every hyp label is
assignable. named: a hyp label equal to a truth speaker name IS that speaker;
every other label is assigned 1:1 over the truth speakers not already claimed
by a name. Labels with zero overlap, or left over, map to None.

DER (standard, per-speaker, on 10 ms frames): for every frame, n_ref = number
of truth speakers, n_hyp = number of distinct hyp labels, n_correct = number of
truth speakers covered by a mapped hyp label.
    miss      = max(0, n_ref - n_hyp)
    false_alarm = max(0, n_hyp - n_ref)
    confusion = min(n_ref, n_hyp) - n_correct
    error     = max(n_ref, n_hyp) - n_correct = miss + false_alarm + confusion
Only frames with truth or hyp speech contribute. The denominator is
truth_time = sum over frames of n_ref (in seconds), so overlapped truth speech
counts once per speaker. miss, false_alarm, confusion and der are fractions of
truth_time.

turn_acc: per truth turn, the hyp label with the most overlap inside
[start, end] (ties -> alphabetically first) is checked against the mapping; it
is correct iff the mapping gives the turn's speaker. No hyp overlap = wrong.
turn_acc_short covers turns shorter than 1.5 s (None when there are none;
turn_acc is None when there are no turns).

named mode extras: over frames with truth speech (truth_speech_time = union
time, seconds), named_correct_time / named_wrong_time are the fractions where
a hyp label that is a NAME (does not match ^SPEAKER_\\d+$) is present and equals
a truth speaker active in the frame (correct) or not (wrong). In blind mode both
are None. wrong_name_labels (named mode only, else []) lists hyp labels that
are names and either are not a truth speaker, or are one while `enrolled` is
given and does not contain them.

wer: lowercase, drop punctuation (apostrophes included), keep digits, split on
whitespace; word-level Levenshtein distance / number of reference words, where
the reference is the truth turns' text joined in start order. None when
hyp_text is None or the reference has no words.

Floats are rounded to 4 dp.
"""

import json
import re

FRAME = 100  # frames per second
MIN_TALK = 0.5  # seconds for a hyp label to count as a speaker
SHORT_TURN = 1.5
_ANON = re.compile(r"^SPEAKER_\d+$")


def _r(x):
    return None if x is None else round(float(x), 4)


def _fr(t):
    return int(round(float(t) * FRAME + 1e-9))


# --------------------------------------------------------------------------
# Hungarian algorithm
# --------------------------------------------------------------------------

def hungarian(cost):
    """Minimum-cost assignment on a rectangular matrix (list of rows).

    Returns [(row, col), ...] with min(n_rows, n_cols) pairs, every row (or
    every column, if there are more rows) used exactly once.
    """
    n = len(cost)
    if n == 0 or len(cost[0]) == 0:
        return []
    m = len(cost[0])
    if n > m:
        pairs = hungarian([[cost[i][j] for i in range(n)] for j in range(m)])
        return sorted((i, j) for j, i in pairs)
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j] = row (1-based) matched to column j
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted((p[j] - 1, j - 1) for j in range(1, m + 1) if p[j])


def hungarian_max(weights):
    """Maximum-weight assignment: same as hungarian() on negated weights."""
    return hungarian([[-w for w in row] for row in weights])


# --------------------------------------------------------------------------
# WER
# --------------------------------------------------------------------------

def tokenize(text):
    text = re.sub(r"[^\w\s]|_", "", (text or "").lower())
    return text.split()


def word_error_rate(ref_text, hyp_text):
    ref = tokenize(ref_text)
    hyp = tokenize(hyp_text)
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hw in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / float(len(ref))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _intervals(truth_iv, hyp_iv):
    """Sweep -> [(n_frames, frozenset truth names, frozenset hyp labels)]."""
    events = {}
    for kind, ivs in ((0, truth_iv), (1, hyp_iv)):
        for s, e, lab in ivs:
            events.setdefault(s, []).append((kind, lab, 1))
            events.setdefault(e, []).append((kind, lab, -1))
    cnt = ({}, {})
    out = []
    pts = sorted(events)
    for k, pt in enumerate(pts):
        for kind, lab, d in events[pt]:
            cnt[kind][lab] = cnt[kind].get(lab, 0) + d
        if k + 1 < len(pts):
            t = frozenset(l for l, c in cnt[0].items() if c > 0)
            h = frozenset(l for l, c in cnt[1].items() if c > 0)
            if t or h:
                out.append((pts[k + 1] - pt, t, h))
    return out


def _assign(overlap, hyp_labels, truth_names, fixed):
    """Return {hyp: truth|None}. `fixed` = {hyp: truth} pre-bound labels."""
    mapping = {h: None for h in hyp_labels}
    mapping.update(fixed)
    claimed = set(fixed.values())
    rows = [h for h in sorted(hyp_labels) if h not in fixed]
    cols = [t for t in sorted(truth_names) if t not in claimed]
    if rows and cols:
        w = [[overlap.get((h, t), 0) for t in cols] for h in rows]
        for i, j in hungarian_max(w):
            if w[i][j] > 0:
                mapping[rows[i]] = cols[j]
    return mapping


def score_meeting(truth, hyp_segments, mode, enrolled=None, hyp_text=None, uncounted=None):
    if mode not in ("blind", "named"):
        raise ValueError("mode must be 'blind' or 'named', got %r" % (mode,))
    turns = sorted(truth.get("turns", []), key=lambda t: (t["start"], t["end"]))
    truth_names = sorted({t["speaker"] for t in turns})
    truth_iv = [(_fr(t["start"]), _fr(t["end"]), t["speaker"]) for t in turns]
    truth_iv = [x for x in truth_iv if x[1] > x[0]]
    segs = [s for s in (hyp_segments or [])]
    hyp_iv = [(_fr(s["start"]), _fr(s["end"]), str(s["speaker"])) for s in segs]
    hyp_iv = [x for x in hyp_iv if x[1] > x[0]]

    hyp_labels = sorted({x[2] for x in hyp_iv} | {str(s["speaker"]) for s in segs})
    talk = {}
    for s, e, lab in hyp_iv:
        talk[lab] = talk.get(lab, 0) + (e - s)
    skip = {str(x) for x in (uncounted or [])}
    n_hyp = sum(1 for lab in hyp_labels
                if lab not in skip and talk.get(lab, 0) / float(FRAME) >= MIN_TALK)
    n_true = len(truth_names)

    ivs = _intervals(truth_iv, hyp_iv)
    overlap = {}
    for n, t, h in ivs:
        for hl in h:
            for tn in t:
                overlap[(hl, tn)] = overlap.get((hl, tn), 0) + n

    fixed = {}
    if mode == "named":
        fixed = {h: h for h in hyp_labels if h in truth_names}
    mapping = _assign(overlap, hyp_labels, truth_names, fixed)

    ref_f = miss_f = fa_f = conf_f = speech_f = 0
    named_ok_f = named_bad_f = 0
    for n, t, h in ivs:
        n_ref, n_h = len(t), len(h)
        mapped = {mapping[x] for x in h if mapping[x] is not None}
        n_correct = len(mapped & t)
        ref_f += n_ref * n
        miss_f += max(0, n_ref - n_h) * n
        fa_f += max(0, n_h - n_ref) * n
        conf_f += (min(n_ref, n_h) - n_correct) * n
        if n_ref:
            speech_f += n
            names = [x for x in h if not _ANON.match(x)]
            if names:
                if any(x in t for x in names):
                    named_ok_f += n
                else:
                    named_bad_f += n

    def frac(x, den):
        return round(x / float(den), 4) if den else 0.0

    # per-turn accuracy
    n_turns = n_short = ok_turns = ok_short = 0
    for tr in turns:
        a, b = float(tr["start"]), float(tr["end"])
        per = {}
        for s in segs:
            ov = min(b, float(s["end"])) - max(a, float(s["start"]))
            if ov > 0:
                per[str(s["speaker"])] = per.get(str(s["speaker"]), 0.0) + ov
        correct = False
        if per:
            best = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            correct = mapping.get(best) == tr["speaker"]
        n_turns += 1
        ok_turns += correct
        if b - a < SHORT_TURN:
            n_short += 1
            ok_short += correct

    wrong = []
    if mode == "named":
        for h in hyp_labels:
            if _ANON.match(h):
                continue
            if h not in truth_names or (enrolled is not None and h not in enrolled):
                wrong.append(h)

    wer = None
    if hyp_text is not None:
        wer = word_error_rate(" ".join(t.get("text", "") for t in turns), hyp_text)

    named = mode == "named"
    return {
        "n_true": n_true,
        "n_hyp": n_hyp,
        "n_hyp_raw": len(hyp_labels),
        "count_ok": n_hyp == n_true,
        "count_delta": n_hyp - n_true,
        "der": frac(miss_f + fa_f + conf_f, ref_f),
        "miss": frac(miss_f, ref_f),
        "false_alarm": frac(fa_f, ref_f),
        "confusion": frac(conf_f, ref_f),
        "truth_time": round(ref_f / float(FRAME), 4),
        "truth_speech_time": round(speech_f / float(FRAME), 4),
        "turn_acc": _r(ok_turns / float(n_turns)) if n_turns else None,
        "turn_acc_short": _r(ok_short / float(n_short)) if n_short else None,
        "n_turns": n_turns,
        "n_short": n_short,
        "n_turns_correct": ok_turns,
        "n_short_correct": ok_short,
        "mapping": mapping,
        "named_correct_time": frac(named_ok_f, speech_f) if named else None,
        "named_wrong_time": frac(named_bad_f, speech_f) if named else None,
        "wrong_name_labels": wrong,
        "wer": _r(wer),
    }


def aggregate(per_meeting):
    ms = list(per_meeting)

    def wmean(key, wkey, sub=None):
        pairs = [(m[key], m[wkey]) for m in ms if m.get(key) is not None]
        den = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / float(den), 4) if den else None

    def acc(key, nkey, ckey):
        den = sum(m[nkey] for m in ms if m.get(key) is not None)
        if not den:
            return None
        num = sum(m[ckey] if ckey in m else int(round(m[key] * m[nkey]))
                  for m in ms if m.get(key) is not None)
        return round(num / float(den), 4)

    hist = {}
    for m in ms:
        k = str(m["count_delta"])
        hist[k] = hist.get(k, 0) + 1
    wers = [m["wer"] for m in ms if m.get("wer") is not None]
    return {
        "n_meetings": len(ms),
        "truth_time": round(sum(m["truth_time"] for m in ms), 4),
        "der": wmean("der", "truth_time"),
        "miss": wmean("miss", "truth_time"),
        "false_alarm": wmean("false_alarm", "truth_time"),
        "confusion": wmean("confusion", "truth_time"),
        "turn_acc": acc("turn_acc", "n_turns", "n_turns_correct"),
        "turn_acc_short": acc("turn_acc_short", "n_short", "n_short_correct"),
        "n_turns": sum(m["n_turns"] for m in ms),
        "n_short": sum(m["n_short"] for m in ms),
        "count_accuracy": _r(sum(1 for m in ms if m["count_ok"]) / float(len(ms))) if ms else None,
        "count_delta_hist": hist,
        "named_correct_time": wmean("named_correct_time", "truth_speech_time"),
        "named_wrong_time": wmean("named_wrong_time", "truth_speech_time"),
        "wer": _r(sum(wers) / float(len(wers))) if wers else None,
    }


# --------------------------------------------------------------------------
# Convenience loaders
# --------------------------------------------------------------------------

def load_truth(path):
    with open(path) as f:
        return json.load(f)


def load_hyp(path):
    """Return the `segments` list of a whosaid <base>.diarization.json."""
    with open(path) as f:
        return json.load(f)["segments"]
