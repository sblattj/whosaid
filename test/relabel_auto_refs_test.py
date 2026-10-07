#!/usr/bin/env python3
"""
Offline test for GitHub issue #77, part B: `relabel --auto` and enrollment state.

  * voices/ refs: `relabel --auto` loads the same enrollment clips `transcribe`
    passes as --ref (NAME=CLIP), so a clip enrolled with `enroll --from FILE`
    (a voices/ file, NOT a registry entry) names a cluster. In-process: the model
    cannot run offline, so the clip loader/embedder are stubbed; the launcher
    wiring (voices dir -> --ref) is checked separately with a stub `uv`.
  * orphan names: a name that an automatic pass (registry/ref/absorb) gave a
    cluster is dropped when it has neither a registry entry nor a voices/ ref
    any more (and the cluster can be re-named to a better match). Names the user
    set explicitly (relabel spec, --no-save local label) are never auto-dropped.
  * --forget NAME (repeatable, requires --auto): clears NAME from this meeting's
    clusters, then naming re-evaluates; it can come back if it still matches.

Synthetic 8-dim one-hot embeddings stand in for voiceprints: no models, audio
or network.

Run:
    uv run --with numpy python test/relabel_auto_refs_test.py
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
DIARIZE = REPO_DIR / "lib" / "diarize_sherpa.py"
WHOSAID = REPO_DIR / "whosaid"

EMB_MODEL = "nemo_en_titanet_small.onnx"
DIM = 8


def onehot(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


E00, E01, E02 = onehot(0), onehot(1), onehot(2)

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def clean_env(speaker_db: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("WHOSAID_") and k != "DIARIZE_EMB_NAME"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["WHOSAID_SPEAKER_DB"] = str(speaker_db)
    env.update(extra)
    return env


def run_cli(args: list, speaker_db: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DIARIZE)] + args,
                          capture_output=True, text=True, env=clean_env(speaker_db))


def write_sidecar(tmp: Path, names=None, matches=None, local=None) -> Path:
    sidecar = tmp / "m.diarization.json"
    sidecar.write_text(json.dumps({
        "base": "m",
        "whosaid_version": "1.11.2",
        "emb_model": EMB_MODEL,
        "num_speakers": 2,
        "names": names or {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
        "local_labels": local or {},
        "registry_matches": matches or [],
        "source": {"path": "/recordings/m.m4a", "duration_seconds": 9.0,
                   "creation_time": "2026-09-17T10:00:00Z"},
        "detect_mode": "2 speakers",
        "count_warning": None,
        "count_estimate": 2,
        "anchors": None,
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.0},
            {"speaker": "SPEAKER_01", "start": 5.0, "end": 9.0},
        ],
        "cluster_emb": {"SPEAKER_00": E00, "SPEAKER_01": E01},
        "whisper_json": None,
    }, indent=2))
    return sidecar


def seed_registry(path: Path, entries: list) -> None:
    path.write_text(json.dumps({"speakers": [
        {"name": n, "model": EMB_MODEL, "embedding": v, "added": "seed"} for n, v in entries
    ]}, indent=2))


def auto(sidecar: Path, reg: Path, *extra: str) -> subprocess.CompletedProcess:
    return run_cli(["--relabel", str(sidecar), "--outdir", str(sidecar.parent / "out"),
                    "--auto", *extra], reg)


def names_of(sidecar: Path) -> dict:
    return json.loads(sidecar.read_text())["names"]


def reg_match(cluster, name, which, matched=True):
    return {"cluster": cluster, "name": name, "similarity": 0.99, "threshold": 0.5,
            "matched": matched, "pass": which}


def test_auto_uses_voices_refs():
    sys.path.insert(0, str(REPO_DIR / "lib"))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = tmp / "speakers.json"          # absent: nobody is in the registry
        os.environ["WHOSAID_SPEAKER_DB"] = str(reg)
        import diarize_sherpa as d
        check(d.SPEAKER_DB == reg, "in-process module must use the isolated registry")
        sidecar = write_sidecar(tmp)
        clips = {str(tmp / "Alice_Example.wav"): np.array(E00, dtype=np.float32),
                 str(tmp / "Ghost.wav"): np.array(E02, dtype=np.float32)}
        d.load_audio = lambda p, *a, **k: clips[p]
        d.make_ref_embedder = lambda: (lambda wave: wave)  # the model is unavailable offline
        args = argparse.Namespace(
            relabel=str(sidecar), outdir=str(tmp / "out"), save_speaker=[], save_role=[],
            no_save=False, note=None, force=False, blend=False, auto=True,
            fold_unknown=False, no_registry=False, ref_threshold=0.5, absorb_threshold=0.85, placed_absorb_threshold=0.70,
            snippets=3, forget=[],
            ref=[f"Alice_Example={tmp / 'Alice_Example.wav'}", f"Ghost={tmp / 'Ghost.wav'}"])
        d.do_relabel(args)
        data = json.loads(sidecar.read_text())
        check(data["names"] == {"SPEAKER_00": "Alice_Example", "SPEAKER_01": "SPEAKER_01"},
              f"a voices/ ref must name its matching cluster, got {data['names']}")
        passes = {(m["cluster"], m["name"], m["pass"]) for m in data["registry_matches"] if m["matched"]}
        check(("SPEAKER_00", "Alice_Example", "ref") in passes,
              f"the match must be recorded as pass=ref, got {data['registry_matches']}")
        check(not reg.exists(), "a ref match must never write the registry")


STUB_UV = """#!/bin/bash
printf '%s\\n' "$@" > "$UV_ARGS_FILE"
"""


def run_launcher(tmp: Path, voices: Path, args: list) -> tuple:
    stub_dir = tmp / "bin"
    stub_dir.mkdir(exist_ok=True)
    uv = stub_dir / "uv"
    uv.write_text(STUB_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    argfile = tmp / "uv.args"
    env = clean_env(tmp / "speakers.json", WHOSAID_VOICE_REFS=str(voices),
                    UV_ARGS_FILE=str(argfile))
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    r = subprocess.run([str(WHOSAID), "relabel", *args], capture_output=True, text=True, env=env)
    lines = argfile.read_text().splitlines() if argfile.exists() else []
    return r, lines


def test_launcher_passes_refs_and_forget():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        voices = tmp / "voices"
        voices.mkdir()
        (voices / "Alice_Example.wav").write_bytes(b"")
        (voices / "Bob_Example.m4a").write_bytes(b"")
        r, lines = run_launcher(tmp, voices, [str(sidecar), "--auto",
                                              "--forget", "Zed", "--forget", "Yan"])
        check(r.returncode == 0, f"launcher must exit 0, got {r.returncode}: {r.stderr}")
        pairs = {(lines[i], lines[i + 1]) for i in range(len(lines) - 1)}
        check(("--ref", f"Alice_Example={voices}/Alice_Example.wav") in pairs,
              f"--auto must pass the voices/ wav as --ref, got {lines}")
        check(("--ref", f"Bob_Example={voices}/Bob_Example.m4a") in pairs,
              f"--auto must pass the voices/ m4a as --ref, got {lines}")
        check(("--forget", "Zed") in pairs and ("--forget", "Yan") in pairs,
              f"--forget must be repeatable and reach the diarizer, got {lines}")

        r, lines = run_launcher(tmp, voices, [str(sidecar), "SPEAKER_00=Jane"])
        check("--ref" not in lines, f"a plain relabel must not embed refs, got {lines}")

        r, lines = run_launcher(tmp, voices, [str(sidecar), "SPEAKER_00=Jane", "--forget", "Zed"])
        check(r.returncode != 0, "--forget without --auto must be refused")
        check("--forget requires --auto" in r.stderr, f"must say why, got: {r.stderr}")


def test_orphan_registry_name_dropped_local_kept():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = tmp / "speakers.json"
        sidecar = write_sidecar(tmp)
        # 1) enroll Alice (registry), auto names SPEAKER_00 and records provenance
        seed_registry(reg, [("Alice_Example", E00)])
        r = auto(sidecar, reg)
        check(r.returncode == 0, f"auto must exit 0: {r.stderr}")
        check(names_of(sidecar)["SPEAKER_00"] == "Alice_Example", "registry pass must name Alice")
        # 2) a second auto with the entry still there keeps the name (provenance carried)
        r = auto(sidecar, reg)
        check(names_of(sidecar)["SPEAKER_00"] == "Alice_Example", "still enrolled: kept")
        # 3) delete the entry: the name goes away on the next auto
        seed_registry(reg, [])
        r = auto(sidecar, reg)
        check(r.returncode == 0, f"auto must exit 0: {r.stderr}")
        check(names_of(sidecar)["SPEAKER_00"] == "SPEAKER_00",
              f"a registry-derived name with no registry entry must be dropped, got {names_of(sidecar)}")

        # 4) dropped cluster is re-named to a better match when one exists
        sc2 = write_sidecar(tmp, names={"SPEAKER_00": "Alice_Example", "SPEAKER_01": "SPEAKER_01"},
                            matches=[reg_match("SPEAKER_00", "Alice_Example", "registry")])
        seed_registry(reg, [("Dana_Example", E00)])
        auto(sc2, reg)
        check(names_of(sc2)["SPEAKER_00"] == "Dana_Example",
              f"orphan cluster must re-name to the better match, got {names_of(sc2)}")

        # 5) controls: local label and explicit relabel name, no registry entry, are kept
        seed_registry(reg, [])
        sc3 = write_sidecar(
            tmp, names={"SPEAKER_00": "Local_Person", "SPEAKER_01": "Explicit_Person"},
            local={"SPEAKER_00": {"name": "Local_Person", "note": None}},
            matches=[reg_match("SPEAKER_00", "Local_Person", "registry"),
                     reg_match("SPEAKER_01", "Explicit_Person", "registry", matched=False)])
        auto(sc3, reg)
        check(names_of(sc3) == {"SPEAKER_00": "Local_Person", "SPEAKER_01": "Explicit_Person"},
              f"local/explicit names must survive auto, got {names_of(sc3)}")
        check(json.loads(sc3.read_text())["local_labels"], "local_labels must be preserved")

        # 6) a name with NO provenance record (older sidecar) is kept
        sc4 = write_sidecar(tmp, names={"SPEAKER_00": "Old_Name", "SPEAKER_01": "SPEAKER_01"})
        auto(sc4, reg)
        check(names_of(sc4)["SPEAKER_00"] == "Old_Name", "no provenance: keep the name")


def test_ref_name_kept_when_clip_not_in_voices():
    # A transcribe --ref NAME=/any/path clip need not live in voices/, so its
    # absence there is not evidence the voiceprint was deleted: keep the name.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = tmp / "speakers.json"
        seed_registry(reg, [])
        prov = [reg_match("SPEAKER_00", "Clip_Person", "ref"),
                reg_match("SPEAKER_01", "Clip_Person", "absorb")]
        sc = write_sidecar(tmp, names={"SPEAKER_00": "Clip_Person", "SPEAKER_01": "Clip_Person"},
                           matches=list(prov))
        auto(sc, reg)  # no --ref passed
        check(names_of(sc)["SPEAKER_00"] == "Clip_Person"
              and names_of(sc)["SPEAKER_01"] == "Clip_Person",
              f"a ref-derived name (and its absorbed split) must be kept, got {names_of(sc)}")
        sc = write_sidecar(tmp, names={"SPEAKER_00": "Clip_Person", "SPEAKER_01": "Clip_Person"},
                           matches=list(prov))
        auto(sc, reg, "--forget", "Clip_Person")
        check(names_of(sc)["SPEAKER_00"] == "SPEAKER_00",
              f"--forget clears a ref-derived name, got {names_of(sc)}")


def test_forget():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reg = tmp / "speakers.json"
        named = {"SPEAKER_00": "Alice_Example", "SPEAKER_01": "SPEAKER_01"}
        prov = [reg_match("SPEAKER_00", "Alice_Example", "registry")]

        # still enrolled and still matching: cleared, then re-evaluated, comes back
        seed_registry(reg, [("Alice_Example", E00)])
        sc = write_sidecar(tmp, names=dict(named), matches=list(prov))
        r = auto(sc, reg, "--forget", "Alice_Example")
        check(r.returncode == 0, f"--forget must be accepted: {r.stderr}")
        check(names_of(sc)["SPEAKER_00"] == "Alice_Example", "matching voice must come back")

        # enrolled but this meeting's voice does not match: ends unnamed
        seed_registry(reg, [("Alice_Example", E02)])
        sc = write_sidecar(tmp, names=dict(named), matches=list(prov))
        r = auto(sc, reg, "--forget", "Alice_Example")
        check(r.returncode == 0, f"--forget must be accepted: {r.stderr}")
        check(names_of(sc)["SPEAKER_00"] == "SPEAKER_00",
              f"a non-matching forgotten name must leave the cluster unnamed, got {names_of(sc)}")
        check(json.loads(seed := reg.read_text()) and "Alice_Example" in seed,
              "--forget never edits the registry")

        # forget also clears a transcript-only local label, and is repeatable
        seed_registry(reg, [])
        sc = write_sidecar(
            tmp, names={"SPEAKER_00": "Local_Person", "SPEAKER_01": "Other_Person"},
            local={"SPEAKER_00": {"name": "Local_Person", "note": "n"}})
        r = auto(sc, reg, "--forget", "Local_Person", "--forget", "Other_Person")
        data = json.loads(sc.read_text())
        check(data["names"] == {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
              f"forget must clear explicit/local names on request, got {data['names']}")
        check(data["local_labels"] == {}, "forgotten local label must be removed")

        # an unknown name is a warning, not a failure
        sc = write_sidecar(tmp)
        r = auto(sc, reg, "--forget", "Nobody")
        check(r.returncode == 0 and "Nobody" in r.stderr, f"unknown forget: warn, rc 0: {r.stderr}")


def main() -> None:
    failed = []
    for fn in (test_auto_uses_voices_refs, test_launcher_passes_refs_and_forget,
               test_orphan_registry_name_dropped_local_kept,
               test_ref_name_kept_when_clip_not_in_voices, test_forget):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed.append(f"{fn.__name__}: {type(e).__name__}: {e}")
    for f in failed:
        print(f"FAIL {f}", file=sys.stderr)
    if failed:
        sys.exit(1)
    print(f"relabel_auto_refs_test: {CHECKS} checks passed")


if __name__ == "__main__":
    main()
