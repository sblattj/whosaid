#!/usr/bin/env python3
"""Offline acceptance tests for ``whosaid speakers`` registry migration."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WHOSAID = ROOT / "whosaid"
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(work: Path, *args: str, db: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["WHOSAID_SPEAKER_DB"] = str(db)
    return subprocess.run([str(WHOSAID), *args], cwd=work, env=env,
                          text=True, capture_output=True, check=False)


def entry(name: str, vector: list[float], *, model: str = "custom_voice.onnx") -> dict:
    return {"name": name, "model": model, "embedding": vector,
            "added": "2026-09-21T12:00:00Z", "role": "collaborator",
            "note": f"private note for {name}", "future_metadata": {"keep": True}}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="whosaid-registry-test-") as temporary:
        work = Path(temporary)
        source_db = work / "source" / "speakers.json"
        source_db.parent.mkdir()
        original = {"speakers": [entry("Alice", [0.1, 0.2, 0.3])],
                    "root_metadata": {"retained": "yes"}}
        write_json(source_db, original)
        backup = work / "private-backup.json"

        exported = run(work, "speakers", "export", "--out", str(backup), db=source_db)
        check(exported.returncode == 0, f"export failed: {exported.stderr}")
        payload = read_json(backup)
        check(payload["schema"] == "whosaid-speaker-registry" and payload["schema_version"] == 1,
              f"export envelope missing: {payload}")
        check(payload["registry"]["root_metadata"] == original["root_metadata"], "root metadata was not preserved")
        check(payload["registry"]["speakers"][0]["note"] == "private note for Alice", "entry note was not preserved")
        check(payload["registry"]["speakers"][0]["role"] == "collaborator", "free-form role was not preserved")
        check(stat.S_IMODE(backup.stat().st_mode) == 0o600,
              f"export mode must be 0600, got {oct(stat.S_IMODE(backup.stat().st_mode))}")

        restored_db = work / "restored" / "speakers.json"
        restored = run(work, "speakers", "import", str(backup), db=restored_db)
        check(restored.returncode == 0, f"empty-destination import failed: {restored.stderr}")
        check(read_json(restored_db) == original, "round trip changed registry data")
        check(stat.S_IMODE(restored_db.stat().st_mode) == 0o600,
              f"import mode must be 0600, got {oct(stat.S_IMODE(restored_db.stat().st_mode))}")

        before = restored_db.read_bytes()
        refused = run(work, "speakers", "import", str(backup), db=restored_db)
        check(refused.returncode != 0 and "--merge or --overwrite" in refused.stderr,
              f"existing destination must require policy: {refused.stderr}")
        check(restored_db.read_bytes() == before, "refused import changed destination")

        other_db = work / "other.json"
        write_json(other_db, {"speakers": [entry("Bob", [0.4, 0.5, 0.6])], "destination_only": 1})
        payload["registry"]["incoming_only"] = {"keep": "this too"}
        backup.unlink()
        write_json(backup, payload)
        merged = run(work, "speakers", "import", str(backup), "--merge", db=other_db)
        check(merged.returncode == 0, f"non-conflicting merge failed: {merged.stderr}")
        merged_data = read_json(other_db)
        check([speaker["name"] for speaker in merged_data["speakers"]] == ["Bob", "Alice"],
              f"merge did not retain both speakers: {merged_data}")
        check(merged_data["destination_only"] == 1, "merge did not retain destination metadata")
        check(merged_data["incoming_only"] == {"keep": "this too"},
              "merge did not retain incoming metadata")

        collision_before = other_db.read_bytes()
        collision = run(work, "speakers", "import", str(backup), "--merge", db=other_db)
        check(collision.returncode != 0 and "merge conflict" in collision.stderr,
              f"collision must fail explicitly: {collision.stderr}")
        check(other_db.read_bytes() == collision_before, "failed merge was not atomic")

        overwritten = run(work, "speakers", "import", str(backup), "--overwrite", db=other_db)
        check(overwritten.returncode == 0, f"explicit overwrite failed: {overwritten.stderr}")
        check(read_json(other_db) == payload["registry"], "overwrite did not replace destination")

        # Each malformed fixture must leave a pre-existing target byte-identical.
        malformed_cases = {
            "unknown-schema": {"schema": "some-other-schema", "schema_version": 1, "registry": {"speakers": []}},
            "boolean-version": {"schema": "whosaid-speaker-registry", "schema_version": True, "registry": {"speakers": []}},
            "bad-model": {"schema": "whosaid-speaker-registry", "schema_version": 1,
                          "registry": {"speakers": [entry("Bad", [1.0], model="../escape.onnx")]}},
            "nan-vector": {"schema": "whosaid-speaker-registry", "schema_version": 1,
                           "registry": {"speakers": [entry("Bad", [float("nan")])]}},
            "zero-vector": {"schema": "whosaid-speaker-registry", "schema_version": 1,
                            "registry": {"speakers": [entry("Bad", [0.0, 0.0])]}},
            "overflow-vector": {"schema": "whosaid-speaker-registry", "schema_version": 1,
                                "registry": {"speakers": [entry("Bad", [1e400])]}},
            "dimension": {"schema": "whosaid-speaker-registry", "schema_version": 1,
                          "registry": {"speakers": [entry("A", [1.0, 2.0]), entry("B", [1.0])]}},
        }
        for label, malformed in malformed_cases.items():
            bad_file = work / f"{label}.json"
            write_json(bad_file, malformed)
            target = work / f"{label}-target.json"
            write_json(target, original)
            unchanged = target.read_bytes()
            result = run(work, "speakers", "import", str(bad_file), "--overwrite", db=target)
            check(result.returncode != 0, f"{label} must be rejected")
            check(target.read_bytes() == unchanged, f"{label} changed target despite rejection")

        literal_nan = work / "literal-nan.json"
        literal_nan.write_text('{"schema":"whosaid-speaker-registry","schema_version":1,"registry":{"speakers":[],"note":NaN}}')
        target = work / "literal-nan-target.json"
        write_json(target, original)
        unchanged = target.read_bytes()
        result = run(work, "speakers", "import", str(literal_nan), "--overwrite", db=target)
        check(result.returncode != 0 and target.read_bytes() == unchanged,
              "literal NaN must be rejected without changing target")

        # Production paired race control: once the real CLI has created its
        # sibling temporary output, another writer claims the previously absent
        # destination. Import must fail, leaving that writer's data intact.
        race_backup = work / "race-backup.json"
        race_payload = {"schema": "whosaid-speaker-registry", "schema_version": 1,
                        "registry": {"speakers": [entry(f"Race-{index}", [0.1, 0.2, 0.3])
                                                  for index in range(50000)]}}
        write_json(race_backup, race_payload)
        race_db = work / "race-target.json"
        env = os.environ.copy()
        env["WHOSAID_SPEAKER_DB"] = str(race_db)
        process = subprocess.Popen([str(WHOSAID), "speakers", "import", str(race_backup)],
                                   cwd=work, env=env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while not list(work.glob(".race-target.json.*.part")) and time.monotonic() < deadline:
            time.sleep(0.001)
        check(list(work.glob(".race-target.json.*.part")), "race control did not observe real import temp file")
        sentinel = {"speakers": [], "claimed_by": "concurrent writer"}
        write_json(race_db, sentinel)
        _, race_stderr = process.communicate(timeout=30)
        check(process.returncode != 0 and "already exists" in race_stderr,
              f"raced absent destination must refuse, got: {race_stderr}")
        check(read_json(race_db) == sentinel, "raced import clobbered concurrent destination")

        help_result = run(work, "speakers", "--help", db=source_db)
        check(help_result.returncode == 0 and "{export,import}" in help_result.stdout,
              f"real launcher did not route speakers help: {help_result.stderr}")
        import_help = run(work, "speakers", "import", "--help", db=source_db)
        check(import_help.returncode == 0 and "replace the whole destination registry" in import_help.stdout,
              f"nested import help hid overwrite semantics: {import_help.stdout}")
        unknown = run(work, "speakers", "nonsense", db=source_db)
        check(unknown.returncode != 0 and "unknown subcommand" in unknown.stderr,
              f"unknown speakers subcommand was not rejected: {unknown.stderr}")
        wrong_route = run(work, "speakerz", "export", "--out", str(work / "nope"), db=source_db)
        check(wrong_route.returncode != 0 and "unknown option" in wrong_route.stderr,
              f"near-miss command must still hit transcribe's negative control: {wrong_route.stderr}")

    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
