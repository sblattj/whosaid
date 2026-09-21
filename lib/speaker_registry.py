#!/usr/bin/env python3
"""Portable, private backups for Whosaid's local speaker registry.

This module deliberately has no diarization or numerical dependencies: moving a
registry must not fetch a model or initialise an ML runtime. Exports add a
small, versioned envelope; the live registry stays compatible with diarization.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "whosaid-speaker-registry"
SCHEMA_VERSION = 1
DEFAULT_DB = Path.home() / ".config" / "whosaid" / "speakers.json"
REQUIRED_ENTRY_KEYS = frozenset(("name", "model", "embedding", "added"))


class RegistryError(ValueError):
    """A registry that must not be written or imported."""


def registry_path() -> Path:
    return Path(os.environ.get("WHOSAID_SPEAKER_DB", DEFAULT_DB)).expanduser()


def _json_constant(value: str) -> None:
    raise RegistryError(f"invalid JSON constant {value}")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RegistryError(f"invalid non-finite JSON number {value}")
    return number


def _require_string(value: Any, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise RegistryError(f"{field} must be a non-empty string")
    return value


def _validate_model(value: Any, field: str) -> str:
    """Accept custom model basenames while rejecting malformed identifiers."""
    model = _require_string(value, field)
    if (not model.endswith(".onnx") or Path(model).name != model
            or any(ord(char) < 32 for char in model)):
        raise RegistryError(f"{field} must be an ONNX model basename")
    return model


def _validate_embedding(value: Any, field: str) -> int:
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{field} must be a non-empty array of finite numbers")
    squared_norm = 0.0
    for index, number in enumerate(value):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise RegistryError(f"{field}[{index}] must be a finite number")
        try:
            floating = float(number)
        except OverflowError:
            raise RegistryError(f"{field}[{index}] must be a finite number") from None
        if not math.isfinite(floating):
            raise RegistryError(f"{field}[{index}] must be a finite number")
        squared_norm += floating * floating
        if not math.isfinite(squared_norm):
            raise RegistryError(f"{field} has an unusably large vector norm")
    if squared_norm == 0.0:
        raise RegistryError(f"{field} must not be a zero vector")
    return len(value)


def validate_registry(value: Any, *, label: str) -> dict[str, Any]:
    """Strictly validate while retaining unknown top-level and entry metadata."""
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be a JSON object")
    speakers = value.get("speakers")
    if not isinstance(speakers, list):
        raise RegistryError(f"{label}.speakers must be an array")
    dimensions: dict[str, int] = {}
    identities: set[tuple[str, str]] = set()
    for index, entry in enumerate(speakers):
        prefix = f"{label}.speakers[{index}]"
        if not isinstance(entry, dict):
            raise RegistryError(f"{prefix} must be an object")
        missing = REQUIRED_ENTRY_KEYS.difference(entry)
        if missing:
            raise RegistryError(f"{prefix} missing required key(s): {', '.join(sorted(missing))}")
        name = _require_string(entry["name"], f"{prefix}.name")
        model = _validate_model(entry["model"], f"{prefix}.model")
        dimension = _validate_embedding(entry["embedding"], f"{prefix}.embedding")
        _require_string(entry["added"], f"{prefix}.added")
        if "role" in entry:
            role = _require_string(entry["role"], f"{prefix}.role")
            if role != role.strip().lower():
                raise RegistryError(f"{prefix}.role must be normalized lowercase text")
        identity = (name, model)
        if identity in identities:
            raise RegistryError(f"{label} has duplicate speaker '{name}' for model '{model}'")
        identities.add(identity)
        known_dimension = dimensions.setdefault(model, dimension)
        if known_dimension != dimension:
            raise RegistryError(
                f"{label} has inconsistent embedding dimensions for model '{model}' "
                f"({known_dimension} and {dimension})"
            )
    return value


def read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_json_constant, parse_float=_json_float)
    except FileNotFoundError:
        raise RegistryError(f"{label} does not exist: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not read {label}: {exc}") from None
    return validate_registry(value, label=label)


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    """Write atomically beside the target with owner-only permissions."""
    encoded = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_new(path: Path, value: dict[str, Any]) -> None:
    """Create a private export without ever replacing a concurrent backup."""
    encoded = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) is an atomic create-if-absent operation in this directory;
        # unlike an exists() check followed by replace(), it closes the race.
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            raise RegistryError(f"destination already exists: {path}; concurrent creation was preserved") from None
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def export_registry(destination: Path) -> int:
    source = registry_path()
    registry = read_json(source, label="source registry")
    if destination.expanduser().resolve() == source.expanduser().resolve():
        raise RegistryError("export destination must differ from the active registry")
    # Keep the entire live registry nested: users may already have arbitrary
    # root keys named ``schema`` or ``schema_version`` and those are data, not
    # migration controls.
    export = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "registry": registry}
    atomic_write_new(destination, export)
    print(f"exported {len(registry['speakers'])} speaker(s) from {source} to {destination}")
    return 0


def read_export(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_json_constant, parse_float=_json_float)
    except FileNotFoundError:
        raise RegistryError(f"import file does not exist: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not read import file: {exc}") from None
    if not isinstance(value, dict):
        raise RegistryError("import file must be a JSON object")
    if value.get("schema") != SCHEMA:
        raise RegistryError(f"unsupported import schema (expected '{SCHEMA}')")
    if (isinstance(value.get("schema_version"), bool)
            or not isinstance(value.get("schema_version"), int)
            or value["schema_version"] != SCHEMA_VERSION):
        raise RegistryError(f"unsupported import schema_version (expected {SCHEMA_VERSION})")
    registry = value.get("registry")
    return validate_registry(registry, label="import registry")


def merge_registries(destination: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    existing = {(entry["name"], entry["model"]) for entry in destination["speakers"]}
    collisions = [(entry["name"], entry["model"]) for entry in incoming["speakers"]
                  if (entry["name"], entry["model"]) in existing]
    if collisions:
        rendered = ", ".join(f"{name} [{model}]" for name, model in collisions)
        raise RegistryError(f"merge conflict for existing speaker(s): {rendered}; use --overwrite to replace")
    merged = dict(destination)
    for key, value in incoming.items():
        if key == "speakers":
            continue
        if key in merged and merged[key] != value:
            raise RegistryError(f"merge conflict for registry metadata '{key}'; use --overwrite to replace")
        merged.setdefault(key, value)
    merged["speakers"] = [*destination["speakers"], *incoming["speakers"]]
    return validate_registry(merged, label="merged registry")


def import_registry(source: Path, *, merge: bool, overwrite: bool) -> int:
    incoming = read_export(source)
    destination_path = registry_path()
    # Hold a stable, separately named lock around state inspection and mutation.
    # The target itself is atomically replaced, so locking its file descriptor
    # would not protect later openers from a new inode.
    lock_path = destination_path.with_name(f".{destination_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        exists = destination_path.exists()
        if exists and not merge and not overwrite:
            raise RegistryError(
                f"destination registry exists: {destination_path}; pass --merge or --overwrite explicitly"
            )
        if merge and exists:
            destination = read_json(destination_path, label="destination registry")
            result = merge_registries(destination, incoming)
            atomic_write(destination_path, result)
        elif exists:
            # --overwrite was explicit, under the same lock that guards merges.
            result = incoming
            atomic_write(destination_path, result)
        else:
            result = incoming
            # A non-cooperating process can still create this path. Never turn
            # that race into an implicit overwrite; create-if-absent detects it.
            atomic_write_new(destination_path, result)
    action = "merged" if merge and exists else "imported"
    print(f"{action} {len(incoming['speakers'])} speaker(s) into {destination_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whosaid speakers", description="Export or import the private speaker registry.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write a portable private registry backup")
    export.add_argument("--out", required=True, type=Path, metavar="FILE",
                        help="new backup file (refuses to overwrite one)")
    importer = commands.add_parser("import", help="restore a portable private registry backup")
    importer.add_argument("file", type=Path, metavar="FILE")
    policy = importer.add_mutually_exclusive_group()
    policy.add_argument("--merge", action="store_true", help="add only non-conflicting speakers")
    policy.add_argument("--overwrite", action="store_true",
                        help="replace the whole destination registry (removes its existing voices)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            return export_registry(args.out)
        return import_registry(args.file, merge=args.merge, overwrite=args.overwrite)
    except RegistryError as exc:
        print(f"speakers: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"speakers: could not write registry: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
