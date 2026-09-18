#!/usr/bin/env python3
"""
Pinning test for the whosaid MCP server (frozen spec v1.2.0).

Enumerates the tools actually registered on the `mcp` server object and asserts
the load-bearing tool names, description substrings, description lengths, param
enums, required-param sets, and readOnlyHint annotations. If any of these drift
from the frozen spec, this test fails. The meeting-workspace tools and
whosaid://workspace/... resources (GitHub issue #14) are pinned the same way;
their behavior is covered offline by test/mcp_workspace_tools_test.py.

v1.2.0 additions pinned here: whosaid_relabel's optional `roles` param, the
"role(s)" description substrings on transcribe/relabel/list_speakers, and the
roles + dev-commitments coverage in SERVER_INSTRUCTIONS. The tool-name SET
stays the six v1.1.0 names.

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
    assert mcp_server.__version__ == "1.2.0", (
        f"__version__ must be 1.2.0, got {mcp_server.__version__!r}"
    )

    tools = asyncio.run(mcp_server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    dumps = {name: wire(t) for name, t in by_name.items()}

    workspace_tools = {
        "whosaid_search",
        "whosaid_context",
        "whosaid_items",
        "whosaid_item",
        "whosaid_person",
        "whosaid_meetings",
        "whosaid_prs",
        "whosaid_speakers",
        "whosaid_workspace_status",
        "whosaid_worklist",
    }
    expected = {
        "whosaid_transcribe",
        "whosaid_relabel",
        "whosaid_list_speakers",
        "whosaid_doctor",
        "whosaid_enroll_from_file",
        "whosaid_samples",
    } | workspace_tools

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
    d_list_speakers = by_name["whosaid_list_speakers"].description or ""

    # --- transcribe description substrings ---
    check("Apple-Silicon" in d_transcribe, "transcribe desc missing 'Apple-Silicon'")
    check("SPEAKER" in d_transcribe, "transcribe desc missing 'SPEAKER'")
    check("whosaid_relabel" in d_transcribe, "transcribe desc missing 'whosaid_relabel'")

    # --- relabel description substrings ---
    check("REMEMBER" in d_relabel, "relabel desc missing 'REMEMBER'")
    check("EVERY future transcript" in d_relabel, "relabel desc missing 'EVERY future transcript'")

    # --- v1.2.0 roles coverage in the descriptions ---
    check("role" in d_transcribe.lower(), "transcribe desc missing roles coverage ('role')")
    check("`roles`" in d_relabel, "relabel desc missing the `roles` param mention")
    check("role" in d_list_speakers.lower(), "list_speakers desc missing roles coverage ('role')")
    check(
        "role" in mcp_server.SERVER_INSTRUCTIONS.lower()
        and "commitment" in mcp_server.SERVER_INSTRUCTIONS.lower(),
        "SERVER_INSTRUCTIONS must cover both roles and dev-commitments",
    )

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
    check(
        treq == {"audio"},
        f"transcribe required set must stay exactly {{'audio'}}, got {treq}",
    )

    # --- relabel input schema: v1.2.0 adds an OPTIONAL `roles` param; the
    # --- required set must not change ---
    rprops = dumps["whosaid_relabel"]["inputSchema"]["properties"]
    rreq = set(dumps["whosaid_relabel"]["inputSchema"].get("required", []))
    check(rreq == {"base", "assignments"}, f"relabel required mismatch: {rreq}")
    check("roles" in rprops, f"relabel must expose an optional 'roles' param: {sorted(rprops)}")
    check("roles" not in rreq, f"'roles' must stay optional, required={rreq}")

    # --- list_speakers stays parameter-free ---
    ls_schema = dumps["whosaid_list_speakers"]["inputSchema"]
    check(
        not ls_schema.get("properties") and not ls_schema.get("required"),
        f"list_speakers must keep no params: {ls_schema}",
    )

    # --- relabel transcript-only mode (GitHub issue #19) ---
    rprops = dumps["whosaid_relabel"]["inputSchema"]["properties"]
    check(
        rprops["no_save"].get("type") == "boolean" and rprops["no_save"].get("default") is False,
        f"relabel no_save must be boolean defaulting to false: {rprops.get('no_save')}",
    )
    check(
        rprops["force"].get("type") == "boolean" and rprops["force"].get("default") is False,
        f"relabel force must be boolean defaulting to false: {rprops.get('force')}",
    )
    check(
        "note" in rprops and "note" not in rreq,
        f"relabel note must be an optional string param: {rprops.get('note')}",
    )
    check(
        "no_save=true" in d_relabel and "transcript-only" in d_relabel,
        "relabel desc must describe the transcript-only (no_save) mode",
    )
    check("provenance" in d_relabel, "relabel desc must describe note as provenance")
    check("force=true" in d_relabel, "relabel desc must describe what force overrides")

    # --- readOnlyHint annotations ---
    doc_ann = dumps["whosaid_doctor"].get("annotations") or {}
    ls_ann = dumps["whosaid_list_speakers"].get("annotations") or {}
    check(doc_ann.get("readOnlyHint") is True, f"doctor readOnlyHint must be True: {doc_ann}")
    check(
        ls_ann.get("readOnlyHint") is True,
        f"list_speakers readOnlyHint must be True: {ls_ann}",
    )

    # --- meeting-workspace tools (GitHub issue #14): all read-only, all take `workspace` ---
    for name in sorted(workspace_tools):
        ann = dumps[name].get("annotations") or {}
        check(ann.get("readOnlyHint") is True, f"{name} readOnlyHint must be True: {ann}")
        check(ann.get("destructiveHint") is False, f"{name} destructiveHint must be False: {ann}")
        check(ann.get("idempotentHint") is True, f"{name} idempotentHint must be True: {ann}")
        check(ann.get("openWorldHint") is False, f"{name} openWorldHint must be False: {ann}")
        props = dumps[name]["inputSchema"]["properties"]
        req = set(dumps[name]["inputSchema"].get("required", []))
        check("workspace" in props and "workspace" not in req, f"{name} must take an optional `workspace`: {req}")
        desc = by_name[name].description or ""
        check("Read-only" in desc or "read-only" in desc, f"{name} desc must say it is read-only")
        check("\u2014" not in desc, f"{name} desc must not use an em dash")
        check(bool(getattr(mcp_server, name).__doc__), f"{name} must have a docstring")

    d_search = by_name["whosaid_search"].description or ""
    d_context = by_name["whosaid_context"].description or ""
    d_person = by_name["whosaid_person"].description or ""
    d_status = by_name["whosaid_workspace_status"].description or ""
    check("whosaid_context" in d_search, "search desc must hand off to whosaid_context")
    check("whosaid index" in d_search, "search desc must say the CLI builds the index")
    check("WHOSAID_WORKSPACE" in d_search, "search desc must name WHOSAID_WORKSPACE")
    check("instead of loading a whole transcript" in d_context, "context desc must discourage whole-transcript reads")
    check("GitHub issue #13" in d_person, "person desc must cite the per-owner commitments view")
    check("whosaid index" in d_status, "status desc must point at whosaid index")
    d_worklist = by_name["whosaid_worklist"].description or ""
    check("GitHub issue #13" in d_worklist and "P1" in d_worklist and "_WORKLIST-" in d_worklist,
          "worklist desc must cite issue #13, the tiers and the rendered file")
    wprops = dumps["whosaid_worklist"]["inputSchema"]["properties"]
    check(set(wprops) == {"owner", "workspace"}, f"worklist params: {sorted(wprops)}")
    check(wprops["owner"].get("default") == "me", f"worklist owner default must be me: {wprops['owner']}")
    check(not dumps["whosaid_worklist"]["inputSchema"].get("required"), "worklist has no required params")
    check("whosaid_worklist" in (mcp_server.SERVER_INSTRUCTIONS or ""), "instructions must mention whosaid_worklist")

    sprops = dumps["whosaid_search"]["inputSchema"]["properties"]
    check(set(sprops["mode"]["enum"]) == {"exact", "meaning", "hybrid"}, f"search mode enum: {sprops['mode'].get('enum')}")
    check(sprops["mode"].get("default") == "hybrid", f"search mode default must be hybrid: {sprops['mode']}")
    check(sprops["k"].get("default") == 10, f"search k default must be 10: {sprops['k']}")
    check(set(dumps["whosaid_search"]["inputSchema"].get("required", [])) == {"query"}, "search requires only query")
    check(set(dumps["whosaid_context"]["inputSchema"].get("required", [])) == {"meeting", "at"}, "context requires meeting + at")
    cprops = dumps["whosaid_context"]["inputSchema"]["properties"]
    check(cprops["before"].get("default") == 60 and cprops["after"].get("default") == 120, "context window defaults 60/120")
    check(set(dumps["whosaid_item"]["inputSchema"].get("required", [])) == {"id"}, "item requires id")
    check(set(dumps["whosaid_items"]["inputSchema"]["properties"]) == {"owner", "requester", "status", "type", "workspace"}, "items filters")
    check(set(dumps["whosaid_person"]["inputSchema"].get("required", [])) == set(), "person name is optional")
    for name in ("whosaid_meetings", "whosaid_prs", "whosaid_speakers", "whosaid_workspace_status"):
        check(set(dumps[name]["inputSchema"]["properties"]) == {"workspace"}, f"{name} takes only workspace")

    # --- server instructions gained the workspace paragraph ---
    instr = mcp_server.SERVER_INSTRUCTIONS
    check("Meeting workspace" in instr, "instructions must have a 'Meeting workspace' paragraph")
    check("WHOSAID_WORKSPACE" in instr and "whosaid index" in instr, "instructions must name the env var and the index command")
    check("whosaid_search" in instr and "whosaid_context" in instr, "instructions must describe search -> context")

    # --- workspace resources: static URIs + {folder} templates, under this SDK major ---
    resources = {str(r.uri) for r in asyncio.run(mcp_server.mcp.list_resources())}
    templates = {t.uri_template for t in asyncio.run(mcp_server.mcp.list_resource_templates())}
    check({"whosaid://guide", "whosaid://workspace/wiki", "whosaid://workspace/action-items",
           "whosaid://workspace/index"} <= resources, f"static resources: {sorted(resources)}")
    check({"whosaid://workspace/meeting/{folder}/transcript",
           "whosaid://workspace/meeting/{folder}/action-items"} <= templates, f"templates: {sorted(templates)}")

    # A template read goes through the SDK's matcher into our function.
    import os
    import tempfile
    saved = os.environ.get("WHOSAID_WORKSPACE")
    with tempfile.TemporaryDirectory(prefix="whosaid-mcp-res-") as d:
        ws = Path(d)
        (ws / "2026-02-01-0900").mkdir()
        (ws / "2026-02-01-0900" / "m.speakers.txt").write_text("[00:00:01] Alice_Example: hello\n")
        os.environ["WHOSAID_WORKSPACE"] = str(ws)
        try:
            got = asyncio.run(mcp_server.mcp.read_resource("whosaid://workspace/meeting/2026-02-01-0900/transcript"))
            body = "".join(str(c.content) for c in got)
            check("Alice_Example: hello" in body, f"template read must return the transcript: {body!r}")
            got = asyncio.run(mcp_server.mcp.read_resource("whosaid://workspace/wiki"))
            body = "".join(str(c.content) for c in got)
            check("_WIKI.md" in body and "whosaid index" in body, f"missing wiki must explain, not raise: {body!r}")
        finally:
            if saved is None:
                os.environ.pop("WHOSAID_WORKSPACE", None)
            else:
                os.environ["WHOSAID_WORKSPACE"] = saved

    print(f"PASS: {CHECKS} assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
