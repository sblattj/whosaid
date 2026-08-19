#!/usr/bin/env python3
"""
Pinning test for the whosaid MCP server (frozen spec v1.1.0).

Enumerates the tools actually registered on the `mcp` server object and asserts
the load-bearing tool names, description substrings, description lengths, param
enums, required-param sets, and readOnlyHint annotations. If any of these drift
from the frozen spec, this test fails.

Run:
    uv run --with "mcp[cli]" python test/mcp_descriptions_test.py

The `mcp` Python SDK exposes registered tools via the async `mcp.list_tools()`
(returns Tool objects). Across SDK 1.x/2.x the Tool attribute names differ
(`inputSchema`/`readOnlyHint` vs `input_schema`/`read_only_hint`), but the wire
schema is stable, so we read the by-alias model dump to get the canonical
camelCase field names (`inputSchema`, `readOnlyHint`) the spec pins on.
"""

import asyncio
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import mcp_server  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def wire(tool) -> dict:
    """Canonical wire-format dict (camelCase keys) for a registered Tool object."""
    return tool.model_dump(by_alias=True)


def main() -> None:
    assert mcp_server.__version__ == "1.1.0", (
        f"__version__ must be 1.1.0, got {mcp_server.__version__!r}"
    )

    tools = asyncio.run(mcp_server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    dumps = {name: wire(t) for name, t in by_name.items()}

    expected = {
        "whosaid_transcribe",
        "whosaid_relabel",
        "whosaid_list_speakers",
        "whosaid_doctor",
        "whosaid_enroll_from_file",
    }

    # --- tool-name set + prefix ---
    check(set(by_name) == expected, f"tool set mismatch: {sorted(by_name)}")
    check(
        all(n.startswith("whosaid_") for n in by_name),
        "every tool name must start with 'whosaid_'",
    )

    # --- every description under 2048 chars ---
    check(
        all(len(t.description or "") < 2048 for t in tools),
        "every tool description must be < 2048 chars",
    )

    d_transcribe = by_name["whosaid_transcribe"].description or ""
    d_relabel = by_name["whosaid_relabel"].description or ""
    d_doctor = by_name["whosaid_doctor"].description or ""
    d_enroll = by_name["whosaid_enroll_from_file"].description or ""

    # --- transcribe description substrings ---
    check("Apple-Silicon" in d_transcribe, "transcribe desc missing 'Apple-Silicon'")
    check("SPEAKER" in d_transcribe, "transcribe desc missing 'SPEAKER'")
    check("whosaid_relabel" in d_transcribe, "transcribe desc missing 'whosaid_relabel'")

    # --- relabel description substrings ---
    check("REMEMBER" in d_relabel, "relabel desc missing 'REMEMBER'")
    check("EVERY future transcript" in d_relabel, "relabel desc missing 'EVERY future transcript'")

    # --- doctor description substring ---
    check("Read-only readiness" in d_doctor, "doctor desc missing 'Read-only readiness'")

    # --- enroll description substrings ---
    check("EXISTING audio clip" in d_enroll, "enroll desc missing 'EXISTING audio clip'")
    check("does NOT record from the mic" in d_enroll, "enroll desc missing 'does NOT record from the mic'")

    # --- transcribe input schema: enums + required ---
    tprops = dumps["whosaid_transcribe"]["inputSchema"]["properties"]
    treq = set(dumps["whosaid_transcribe"]["inputSchema"].get("required", []))
    check(
        set(tprops["format"]["enum"]) == {"txt", "srt", "vtt", "tsv", "json", "all"},
        f"transcribe format enum mismatch: {tprops['format'].get('enum')}",
    )
    check(
        set(tprops["accuracy"]["enum"]) == {"fast", "accurate"},
        f"transcribe accuracy enum mismatch: {tprops['accuracy'].get('enum')}",
    )
    check("audio" in treq, f"transcribe 'audio' must be required, required={treq}")

    # --- relabel required set ---
    rreq = set(dumps["whosaid_relabel"]["inputSchema"].get("required", []))
    check(rreq == {"base", "assignments"}, f"relabel required mismatch: {rreq}")

    # --- readOnlyHint annotations ---
    doc_ann = dumps["whosaid_doctor"].get("annotations") or {}
    ls_ann = dumps["whosaid_list_speakers"].get("annotations") or {}
    check(doc_ann.get("readOnlyHint") is True, f"doctor readOnlyHint must be True: {doc_ann}")
    check(
        ls_ann.get("readOnlyHint") is True,
        f"list_speakers readOnlyHint must be True: {ls_ann}",
    )

    print(f"PASS: {CHECKS} assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
