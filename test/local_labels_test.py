#!/usr/bin/env python3
"""
Offline test for transcript-only labels (GitHub issue #19): --no-save,
--force, --note, save_print_guarded, and local_labels in the sidecar.

Drives the REAL CLI via subprocess (uv run --with numpy python
test/local_labels_test.py) so the argparse wiring is exercised:
  * --no-save --note: names the cluster in sidecar + cards, leaves the
    registry byte-identical, records a local_labels entry, and the card
    carries the "(transcript-only label; registry untouched)" marker.
  * default: saves the print to the registry, no local_labels entry.
  * the guard blocks a registry replacement when the new cluster is
    orthogonal to the print it would overwrite; --force overrides; a
    high-similarity replacement stays silent.
  * --auto honors local labels (explicit names win, labels preserved).
  * test/check_sidecar_schema.py accepts the local_labels payload.

Offline: no models, no audio, no network — synthetic one-hot embeddings
(8-dim) stand in for TitaNet voiceprints.

Run:
    uv run --with numpy python test/local_labels_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DIARIZE = REPO_DIR / "lib" / "diarize_sherpa.py"
SCHEMA = REPO_DIR / "test" / "check_sidecar_schema.py"

EMB_MODEL = "nemo_en_titanet_small.onnx"
DIM = 8
E00 = [1.0] + [0.0] * (DIM - 1)                     # SPEAKER_00's cluster vector
E01 = [0.0, 1.0] + [0.0] * (DIM - 2)                # SPEAKER_01's cluster vector
ORTH = [0.0, 0.0, 1.0] + [0.0] * (DIM - 3)          # orthogonal to E00 (sim 0.00)

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def run_cli(args: list, speaker_db: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("WHOSAID_") and k != "DIARIZE_EMB_NAME"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["WHOSAID_SPEAKER_DB"] = str(speaker_db)
    return subprocess.run([sys.executable, str(DIARIZE)] + args,
                          capture_output=True, text=True, env=env)


def write_sidecar(tmp: Path) -> Path:
    sidecar = tmp / "m.diarization.json"
    sidecar.write_text(json.dumps({
        "base": "m",
        "emb_model": EMB_MODEL,
        "num_speakers": 2,
        "names": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
        "registry_matches": [],
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
    path.write_text(json.dumps({"speakers": entries}, indent=2))


def print_entry(name: str, vec: list) -> dict:
    return {"name": name, "model": EMB_MODEL, "embedding": vec, "added": "seed"}


def saved_print(reg_path: Path, name: str) -> dict | None:
    if not reg_path.exists():
        return None
    hits = [s for s in json.loads(reg_path.read_text())["speakers"] if s["name"] == name]
    return hits[0] if len(hits) == 1 else None


def test_no_save_keeps_registry_untouched():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"
        seed_registry(reg, [print_entry("Bob_Example", E01)])
        before = reg.read_bytes()

        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example",
                     "--no-save", "--note", "answered when the host said Alice"], reg)
        check(r.returncode == 0, f"--no-save must exit 0, got {r.returncode}: {r.stderr}")
        check(reg.read_bytes() == before, "--no-save must leave the registry byte-identical")

        data = json.loads(sidecar.read_text())
        check(data["names"]["SPEAKER_00"] == "Alice_Example",
              f"sidecar must name SPEAKER_00, got {data['names']}")
        check(data["local_labels"] ==
              {"SPEAKER_00": {"name": "Alice_Example",
                              "note": "answered when the host said Alice"}},
              f"local_labels must carry the note, got {data.get('local_labels')}")
        cards = (out / "m.speaker-cards.txt").read_text()
        check("(transcript-only label; registry untouched)" in cards,
              f"cards must flag the transcript-only label: {cards[:400]}")

        sc = subprocess.run([sys.executable, str(SCHEMA), str(sidecar)],
                            capture_output=True, text=True)
        check(sc.returncode == 0 and "sidecar schema OK" in sc.stdout,
              f"schema checker must accept the local_labels sidecar: {sc.stderr or sc.stdout}")


def test_default_saves_print_no_local_label():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"

        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example"], reg)
        check(r.returncode == 0, f"default save must exit 0: {r.stderr}")

        hit = saved_print(reg, "Alice_Example")
        check(hit is not None and hit["model"] == EMB_MODEL and hit["embedding"] == E00,
              f"registry must gain the cluster's exact print, got {hit}")

        data = json.loads(sidecar.read_text())
        check(data.get("local_labels", {}).get("SPEAKER_00") is None,
              f"a saved print must leave no local label, got {data.get('local_labels')}")
        cards = (out / "m.speaker-cards.txt").read_text()
        check("transcript-only" not in cards, "a saved print must not be flagged transcript-only")


def test_guard_blocks_orthogonal_replacement():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"
        seed_registry(reg, [print_entry("Alice_Example", ORTH)])
        before = reg.read_bytes()

        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example"], reg)
        check(r.returncode != 0, "the guard must exit non-zero on an orthogonal swap")
        check("matches 'Alice_Example' current print at 0." in r.stderr,
              f"stderr must report the similarity: {r.stderr}")
        check("--force" in r.stderr, f"stderr must point at --force: {r.stderr}")
        check(reg.read_bytes() == before, "a blocked replacement must not touch the registry")


def test_force_overrides_guard():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"
        seed_registry(reg, [print_entry("Alice_Example", ORTH)])

        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example", "--force"], reg)
        check(r.returncode == 0, f"--force must exit 0: {r.stderr}")
        hit = saved_print(reg, "Alice_Example")
        check(hit is not None and hit["embedding"] == E00,
              f"--force must replace the print with the cluster vector, got {hit}")


def test_high_similarity_replacement_is_silent():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"
        seed_registry(reg, [print_entry("Alice_Example", E00)])  # sim 1.00 to the cluster

        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example"], reg)
        check(r.returncode == 0 and "--force" not in r.stderr,
              f"a high-sim replacement must stay silent, got: {r.stderr}")
        hit = saved_print(reg, "Alice_Example")
        check(hit is not None and hit["embedding"] == E00,
              f"the high-sim replacement must still save the print, got {hit}")


def test_auto_honors_local_labels():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = write_sidecar(tmp)
        reg, out = tmp / "speakers.json", tmp / "out"

        # Stage 1: label SPEAKER_00 transcript-only (case a), leaving Bob unknown.
        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out),
                     "--save-speaker", "SPEAKER_00=Alice_Example",
                     "--no-save", "--note", "answered when the host said Alice"], reg)
        check(r.returncode == 0, f"--no-save stage must exit 0: {r.stderr}")

        # Stage 2: --auto with a registry whose Bob matches SPEAKER_01 exactly.
        seed_registry(reg, [print_entry("Bob_Example", E01)])
        r = run_cli(["--relabel", str(sidecar), "--outdir", str(out), "--auto"], reg)
        check(r.returncode == 0, f"--auto must exit 0: {r.stderr}")

        data = json.loads(sidecar.read_text())
        check(data["names"]["SPEAKER_00"] == "Alice_Example",
              f"--auto must keep the explicit local label, got {data['names']}")
        check(data["names"]["SPEAKER_01"] == "Bob_Example",
              f"--auto must name SPEAKER_01 from the registry, got {data['names']}")
        check(data["local_labels"] ==
              {"SPEAKER_00": {"name": "Alice_Example",
                              "note": "answered when the host said Alice"}},
              f"--auto must preserve local_labels, got {data.get('local_labels')}")


def main() -> None:
    test_no_save_keeps_registry_untouched()
    test_default_saves_print_no_local_label()
    test_guard_blocks_orthogonal_replacement()
    test_force_overrides_guard()
    test_high_similarity_replacement_is_silent()
    test_auto_honors_local_labels()
    print(f"\nPASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
