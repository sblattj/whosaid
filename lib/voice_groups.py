#!/usr/bin/env python3
"""
voice_groups.py: the same unnamed voice across meetings, grouped, so it is named once.

Every meeting's diarization leaves its own SPEAKER_NN clusters, so one unknown
colleague shows up as a fresh "unnamed voice" in each meeting. This reads the
cluster voiceprints from every sidecar in a workspace, keeps the unnamed ones,
and joins them with average linkage: two groups merge only while the mean cosine
across all their pairs stays at or above GROUP. Each group carries how many
meetings it spans, its talk time, when it was last heard, and the nearest
registry voice as a hint (never applied on its own).

Naming a group relabels each member's sidecar: a name new to the registry saves
the largest member's voiceprint (so later recordings are named on their own),
every other member is a transcript-only label (--no-save). A name already in the
registry never touches its print. The workspace index is rebuilt once at the end.

Why 0.65: on named clusters, the same person across meetings scored a cosine of
about 0.62 at the 5th percentile and different people about 0.63 at the 99th, so
0.65 keeps different people apart and splits a person into a couple of groups at
worst. Centroid linkage chained everything into one blob; average linkage does not.

CLI (via `whosaid voices`):
  python3 lib/voice_groups.py groups <ws> [--json] [--min-seconds S] [--threshold T]
  python3 lib/voice_groups.py name <ws> <group-id> <Name> [--role ROLE]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diarize_sherpa as ds  # noqa: E402  (numpy only at import; sherpa-onnx is deferred)
import wsconfig  # noqa: E402

GROUP = 0.65         # mean pairwise cosine to stay one group (see the docstring)
MIN_SECONDS = 5.0    # a cluster with less talk than this is too thin to group
HINT_MIN = 0.55      # nearest registry voice worth showing as a hint
RECENT_DAYS = 14
CACHE = "_voice-groups.json"
CACHE_VERSION = 1
UNNAMED = re.compile(r"^SPEAKER_\d+$")
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
REPO = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(f"voices: {msg}", file=sys.stderr, flush=True)


def _sidecars(ws: Path) -> list[Path]:
    return [p for folder, _ in wsconfig.iter_meetings(ws)
            for p in sorted((ws / folder).glob("*.diarization.json"))]


def _signature(ws: Path, sidecars: list[Path], min_seconds: float, threshold: float) -> str:
    h = hashlib.sha1(f"{CACHE_VERSION}|{ds.EMB_NAME}|{min_seconds}|{threshold}".encode())
    for p in sidecars:
        h.update(f"{p.relative_to(ws)}|{p.stat().st_mtime_ns}".encode())
    try:
        h.update(str(ds.SPEAKER_DB.stat().st_mtime_ns).encode())
    except FileNotFoundError:
        pass
    return h.hexdigest()


def collect(ws: Path, sidecars: list[Path], min_seconds: float) -> tuple[list[dict], int]:
    """Unnamed clusters with a voiceprint from the current model: [{id, folder, sidecar,
    cluster, seconds, date, vec}], plus how many were skipped as too short."""
    rows, short = [], 0
    for p in sidecars:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if d.get("emb_model", ds.EMB_NAME) != ds.EMB_NAME:
            continue
        names = d.get("names") or {}
        local = d.get("local_labels") or {}
        secs: dict[str, float] = {}
        for s in d.get("segments") or []:
            c = s.get("speaker")
            if c:
                secs[c] = secs.get(c, 0.0) + max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
        folder = p.parent.name
        m = DATE.match(folder)
        for c, e in (d.get("cluster_emb") or {}).items():
            if not UNNAMED.match(names.get(c, c)) or c in local:
                continue
            v = np.asarray(e, dtype=np.float32)
            n = float(np.linalg.norm(v))
            if not n:
                continue
            if secs.get(c, 0.0) < min_seconds:
                short += 1
                continue
            rows.append({"id": f"{p.relative_to(ws)}#{c}", "folder": folder, "sidecar": str(p.relative_to(ws)),
                         "cluster": c, "seconds": round(secs.get(c, 0.0), 1),
                         "date": m.group(1) if m else None, "vec": v / n})
    return rows, short


def average_linkage(V: np.ndarray, threshold: float) -> list[list[int]]:
    """Groups of row indices: merge the closest pair while their mean pairwise cosine
    is at least threshold (Lance-Williams update on the similarity matrix)."""
    n = len(V)
    if n == 0:
        return []
    A = (V @ V.T).astype(np.float64)
    np.fill_diagonal(A, -np.inf)
    alive = np.ones(n, dtype=bool)
    size = np.ones(n)
    groups = [[i] for i in range(n)]
    while alive.sum() > 1:
        sub = np.where(np.outer(alive, alive), A, -np.inf)
        i, j = np.unravel_index(int(np.argmax(sub)), sub.shape)
        if sub[i, j] < threshold:
            break
        row = (A[i] * size[i] + A[j] * size[j]) / (size[i] + size[j])
        A[i, :] = row
        A[:, i] = row
        A[i, i] = -np.inf
        A[j, :] = -np.inf
        A[:, j] = -np.inf
        size[i] += size[j]
        groups[i] += groups[j]
        alive[j] = False
    return [groups[i] for i in range(n) if alive[i]]


def _today(ws: Path) -> date:
    """Today in the workspace's [workspace] tz (local time when unset or unknown)."""
    tz = (wsconfig.load_config(ws).get("workspace") or {}).get("tz") or ""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).date() if tz else date.today()
    except Exception:  # noqa: BLE001
        return date.today()


def _registry() -> tuple[list[dict], np.ndarray | None]:
    reg = ds.registry_entries_for_model(ds.load_registry())
    if not reg:
        return [], None
    K = np.stack([np.asarray(e["embedding"], np.float32) for e in reg])
    return reg, K / (np.linalg.norm(K, axis=1, keepdims=True) + 1e-9)


def build(ws: Path, min_seconds: float = MIN_SECONDS, threshold: float = GROUP,
          today: date | None = None) -> dict:
    """The groups, biggest (by meetings, then talk time) first."""
    sidecars = _sidecars(ws)
    rows, short = collect(ws, sidecars, min_seconds)
    reg, K = _registry()
    cutoff = ((today or _today(ws)) - timedelta(days=RECENT_DAYS)).isoformat()
    groups = []
    for g in average_linkage(np.stack([r["vec"] for r in rows]) if rows else np.zeros((0, 1)), threshold):
        members = sorted((rows[k] for k in g), key=lambda r: (-r["seconds"], r["id"]))
        c = sum(r["vec"] * max(r["seconds"], 1.0) for r in members)
        c = c / (np.linalg.norm(c) + 1e-9)
        hint = None
        if K is not None:
            s = K @ c
            o = np.argsort(-s)
            best = {"name": reg[o[0]]["name"], "similarity": round(float(s[o[0]]), 3)}
            if reg[o[0]].get("role"):
                best["role"] = reg[o[0]]["role"]
            if best["similarity"] >= HINT_MIN:
                hint = best
                if len(o) > 1:
                    hint["next"] = {"name": reg[o[1]]["name"], "similarity": round(float(s[o[1]]), 3)}
        dates = sorted(r["date"] for r in members if r["date"])
        inner = [float(members[a]["vec"] @ members[b]["vec"])
                 for a in range(len(members)) for b in range(a + 1, len(members))]
        groups.append({
            "id": "vg-" + hashlib.sha1("|".join(sorted(r["id"] for r in members)).encode()).hexdigest()[:10],
            "meetings": len({r["folder"] for r in members}),
            "clusters": len(members),
            "seconds": round(sum(r["seconds"] for r in members), 1),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "recent": bool(dates and dates[-1] >= cutoff),
            "cohesion": round(float(np.mean(inner)), 3) if inner else 1.0,
            "hint": hint,
            "members": [{k: r[k] for k in ("id", "folder", "cluster", "seconds", "date")} for r in members],
        })
    groups.sort(key=lambda g: (-g["meetings"], -g["seconds"], g["id"]))
    return {"ok": True, "groups": groups, "clusters": len(rows), "short_skipped": short,
            "min_seconds": min_seconds, "threshold": threshold, "recent_days": RECENT_DAYS,
            "registry_voices": len(reg)}


def groups(ws: Path, min_seconds: float = MIN_SECONDS, threshold: float = GROUP,
           refresh: bool = False) -> dict:
    """build(), cached in <ws>/_voice-groups.json until a sidecar or the registry changes."""
    ws = Path(ws)
    sig = _signature(ws, _sidecars(ws), min_seconds, threshold)
    path = ws / CACHE
    if not refresh:
        try:
            cached = json.loads(path.read_text())
            if cached.get("signature") == sig and cached.get("day") == _today(ws).isoformat():
                return cached["result"]
        except (OSError, ValueError, KeyError):
            pass
    result = build(ws, min_seconds, threshold)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"signature": sig, "day": _today(ws).isoformat(),
                               "built": datetime.now().isoformat(timespec="seconds"), "result": result}))
    tmp.rename(path)
    return result


def _relabel(ws: Path, member: dict, name: str, save: bool, role: str, timeout: float) -> tuple[str, str | None]:
    args = [str(REPO / "whosaid"), "relabel", str(ws / member["id"].split("#")[0]), f"{member['cluster']}={name}"]
    if not save:
        args += ["--no-save", "--note", "named as a voice group"]
    if role:
        args += ["--role", f"{name}={role}"]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return member["id"], f"timed out after {timeout:.0f}s"
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()
        return member["id"], tail.splitlines()[-1] if tail else f"exit {p.returncode}"
    return member["id"], None


def name(ws: Path, group_id: str, person: str, role: str = "", workers: int = 4,
         timeout: float = 180, index: bool = True) -> dict:
    """Put `person` on every member of a group. See the module docstring for what is saved."""
    ws = Path(ws)
    person = person.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", person) or UNNAMED.match(person):
        return {"ok": False, "error": f"not a name: {person!r} (letters, digits, _ and -, e.g. Ada_Lovelace)"}
    cur = groups(ws)
    g = next((x for x in cur["groups"] if x["id"] == group_id), None)
    if g is None:
        return {"ok": False, "error": f"no voice group {group_id} (the groups changed: list them again)"}
    known = {e["name"] for e in ds.registry_entries_for_model(ds.load_registry())}
    new = person not in known
    members = g["members"]
    done, failed = [], []
    t0 = time.time()
    # the first (largest) member goes alone: it is the only call that may write the
    # registry (a new name's voiceprint, or a role); the rest are transcript-only
    mid, err = _relabel(ws, members[0], person, new, role, timeout)
    if err:
        return {"ok": False, "error": f"labeling {mid} failed: {err}", "named": [], "failed": [{"id": mid, "error": err}]}
    done.append(mid)
    if new:
        log(f"saved {person}'s voiceprint from {mid}")
    rest = members[1:]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_relabel, ws, m, person, False, "", timeout) for m in rest]
        for n, f in enumerate(concurrent.futures.as_completed(futs), 1):
            mid, err = f.result()
            (failed if err else done).append(mid if not err else {"id": mid, "error": err})
            if n % 5 == 0 or n == len(futs):
                log(f"{n}/{len(futs)} labeled")
    out = {"ok": not failed, "group": group_id, "name": person, "enrolled": new,
           "named": sorted(done), "failed": failed, "seconds": round(time.time() - t0, 1)}
    if index and done:
        try:
            p = subprocess.run([str(REPO / "whosaid"), "index", str(ws)], capture_output=True, text=True, timeout=1800)
            out["indexed"] = p.returncode == 0
            if p.returncode != 0:
                out["index_error"] = (p.stderr or "").strip()[-400:]
        except (subprocess.TimeoutExpired, OSError) as e:
            out["indexed"] = False
            out["index_error"] = f"index did not finish: {e}"[:400]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="whosaid voices", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("groups", help="list unnamed voice groups across meetings")
    g.add_argument("workspace")
    g.add_argument("--json", action="store_true")
    g.add_argument("--min-seconds", type=float, default=MIN_SECONDS)
    g.add_argument("--threshold", type=float, default=GROUP)
    g.add_argument("--refresh", action="store_true", help="ignore the cache")
    n = sub.add_parser("name", help="name every member of one group")
    n.add_argument("workspace")
    n.add_argument("group")
    n.add_argument("name")
    n.add_argument("--role", default="")
    n.add_argument("--no-index", action="store_true")
    a = ap.parse_args(argv)
    ws = Path(os.path.expanduser(a.workspace))
    if not ws.is_dir():
        print(json.dumps({"ok": False, "error": f"not a workspace folder: {ws}"}))
        return 1
    if a.cmd == "groups":
        r = groups(ws, a.min_seconds, a.threshold, a.refresh)
        if a.json:
            print(json.dumps(r))
            return 0
        print(f"{len(r['groups'])} voice groups from {r['clusters']} unnamed clusters "
              f"({r['short_skipped']} under {r['min_seconds']:g}s skipped)")
        for x in r["groups"]:
            h = x["hint"]
            near = f"  near {h['name']} {h['similarity']:.2f}" if h else ""
            print(f"{x['id']}  {x['meetings']:3d} meetings  {x['seconds'] / 60:6.1f} min  last {x['last']}{near}")
        return 0
    r = name(ws, a.group, a.name, a.role, index=not a.no_index)
    print(json.dumps(r))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
