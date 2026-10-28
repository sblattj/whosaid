#!/usr/bin/env python3
"""
Wire-schema and description contracts for the whosaid MCP server.

Enumerates the tools actually registered on the `mcp` server object and asserts
the load-bearing tool names, description substrings, description lengths, param
enums, required-param sets, and readOnlyHint annotations. If any of these drift
from the intended API, this test fails. The meeting-workspace tools and
whosaid://workspace/... resources (GitHub issue #14) are pinned the same way;
their behavior is covered offline by test/mcp_workspace_tools_test.py.

Includes tracking filters, historical reads, priority assertions, review-only
evidence suggestions, bounded pagination and cache compatibility contracts.

Run:
    uv run --with "mcp[cli]" python test/mcp_descriptions_test.py

The `mcp` Python SDK exposes registered tools via the async `mcp.list_tools()`
(returns Tool objects). Across SDK 1.x/2.x the Tool attribute names differ
(`inputSchema`/`readOnlyHint` vs `input_schema`/`read_only_hint`), but the wire
schema is stable, so we read the by-alias model dump to get the canonical
camelCase field names (`inputSchema`, `readOnlyHint`) the spec pins on.
"""

import asyncio
import re
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
    # Drift-catcher, not a frozen number: the server's __version__ must equal
    # the launcher's WHOSAID_VERSION (the two release-time bump sites; see
    # CHANGELOG "PyPI packaging" for the single-source plan).
    launcher = (REPO_DIR / "whosaid").read_text()
    m = re.search(r'^WHOSAID_VERSION="([^"]+)"', launcher, re.M)
    assert m, "WHOSAID_VERSION not found in the whosaid launcher"
    assert mcp_server.__version__ == m.group(1), (
        f"mcp_server.__version__ ({mcp_server.__version__!r}) != launcher "
        f"WHOSAID_VERSION ({m.group(1)!r}) — bump both at release time"
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
        "whosaid_priorities",
        "whosaid_tracking_suggestions",
    }
    expected = {
        "whosaid_transcribe",
        "whosaid_relabel",
        "whosaid_list_speakers",
        "whosaid_doctor",
        "whosaid_enroll_from_file",
        "whosaid_samples",
        "whosaid_reprocess",
        "whosaid_record_browser",
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
        set(tprops["accuracy"]["enum"]) == {"default", "fast", "accurate"},
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
    check(
        rprops["fold_unknown"].get("type") == "boolean"
        and rprops["fold_unknown"].get("default") is False
        and "fold_unknown" not in rreq,
        f"relabel fold_unknown must be optional boolean defaulting false: {rprops.get('fold_unknown')}",
    )
    check(
        "fold_unknown=true" in d_relabel,
        "relabel description must document cached anonymous-cluster repair",
    )

    # --- relabel --forget (GitHub issue #77): optional list, not required ---
    check("forget" in rprops and "forget" not in rreq,
          f"relabel must expose an optional 'forget' param: {sorted(rprops)}")
    check("forget=[" in d_relabel and "voices/" in d_relabel,
          "relabel desc must document forget and the voices/ refs used by auto")

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
    check("completed" in d_worklist and "P1" in d_worklist and "_WORKLIST-" in d_worklist,
          "worklist desc must explain completed history, tiers and the cached file")
    wprops = dumps["whosaid_worklist"]["inputSchema"]["properties"]
    check(set(wprops) == {"owner", "workspace", "tier", "limit", "offset", "compact", "use_cache",
                          "kind", "status", "accepted_only", "as_of"},
          f"worklist params: {sorted(wprops)}")
    check(wprops["use_cache"].get("default") is False, f"use_cache defaults off (#113): {wprops['use_cache']}")
    check(all(t in d_worklist for t in ("use_cache", "missing_corpora", "timeout", "stale", "_WORKLIST-<Owner>.json")),
          "worklist desc must document use_cache, the error categories and the cache file (#113)")
    check(wprops["owner"].get("default") == "me", f"worklist owner default must be me: {wprops['owner']}")
    check(wprops["offset"].get("default") == 0 and wprops["compact"].get("default") is False,
          f"worklist paging defaults: offset={wprops.get('offset')} compact={wprops.get('compact')}")
    check(all(p in d_worklist.lower() for p in ("tier", "limit", "offset", "compact")),
          "worklist desc must document the tier/limit/offset/compact params")
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
    check(set(dumps["whosaid_items"]["inputSchema"]["properties"])
          == {"owner", "requester", "status", "type", "workspace", "limit", "offset", "compact",
              "kind", "accepted_only", "as_of"}, "items filters + paging + historical reconciliation")
    check(set(dumps["whosaid_person"]["inputSchema"].get("required", [])) == set(), "person name is optional")
    check(set(dumps["whosaid_person"]["inputSchema"]["properties"]) == {"name", "workspace", "as_of"},
          "person exposes an optional historical date")
    check(set(dumps["whosaid_item"]["inputSchema"]["properties"]) == {"id", "workspace", "as_of"},
          "item exposes an optional historical date")
    check("CM-NNN" in dumps["whosaid_item"]["description"], "item documents commitment IDs alongside action items")
    for name in ("whosaid_items", "whosaid_worklist"):
        schema = dumps[name]["inputSchema"]
        props = schema["properties"]
        check(props["accepted_only"].get("type") == "boolean"
              and props["accepted_only"].get("default") is False, f"{name} acceptance filter defaults false")
        check({"kind", "status", "accepted_only", "as_of"}.isdisjoint(schema.get("required", [])),
              f"{name} tracking filters remain optional")
        desc = dumps[name]["description"]
        check(all(term in desc for term in ("explicit_commitment", "request", "standing_directive",
                                           "suggestion", "inferred_next_step", "evidence_available", "snapshot_token")),
              f"{name} documents obligation distinctions, evidence and snapshot identity")
    check("incompatible_cache" in d_worklist and "Historical" in d_worklist and "live reconciliation" in d_worklist,
          "worklist documents historical cache incompatibility")
    p_schema = dumps["whosaid_priorities"]["inputSchema"]
    check(set(p_schema["properties"]) == {"group", "as_of", "workspace"} and not p_schema.get("required"),
          "priority group and historical filters are optional")
    d_priorities = dumps["whosaid_priorities"]["description"]
    check(all(term in d_priorities for term in ("group", "history", "evidence", "conflicts", "consensus")),
          "priority description preserves provenance and conflicting assertions")
    suggestion_schema = dumps["whosaid_tracking_suggestions"]["inputSchema"]
    suggestion_props = suggestion_schema["properties"]
    check(set(suggestion_props) == {"owner", "limit", "workspace"} and not suggestion_schema.get("required"),
          "suggestions only require configured workspace context")
    check(suggestion_props["owner"].get("default") == "me"
          and suggestion_props["limit"].get("default") == 50, "suggestion owner/limit defaults")
    d_suggestions = dumps["whosaid_tracking_suggestions"]["description"]
    check(all(term in d_suggestions for term in ("1000", "review_required", "never update status", "no LLM")),
          "suggestions are bounded, unapproved and read-only")
    check(set(dumps["whosaid_workspace_status"]["inputSchema"]["properties"]) == {"workspace"}, "workspace_status takes only workspace")
    for name in ("whosaid_prs", "whosaid_speakers"):
        check(set(dumps[name]["inputSchema"]["properties"]) == {"workspace", "limit", "offset"}, f"{name} pages")
    check(set(dumps["whosaid_meetings"]["inputSchema"]["properties"])
          == {"workspace", "kind", "since", "until", "query", "limit", "offset", "compact"}, "meetings filters + paging")
    check("teams-chat" in dumps["whosaid_meetings"]["description"], "meetings description names the teams-chat kind")

    # --- whosaid_reprocess: mutating, dry run by default ---
    rp = dumps["whosaid_reprocess"]
    rp_ann = rp.get("annotations") or {}
    rp_props = rp["inputSchema"]["properties"]
    d_rp = by_name["whosaid_reprocess"].description or ""
    check(rp_ann.get("readOnlyHint") is False, f"reprocess must not be read-only: {rp_ann}")
    check(not rp["inputSchema"].get("required"), "reprocess has no required params")
    check(set(rp_props) == {"workspace", "meetings", "dry_run", "all_meetings", "no_items", "speakers",
                            "min_speakers", "max_speakers", "expected_speakers", "match_threshold",
                            "engine", "hook"},
          f"reprocess params: {sorted(rp_props)}")
    check(rp_props["dry_run"].get("default") is True, f"reprocess dry_run must default true: {rp_props['dry_run']}")
    check(rp_props["all_meetings"].get("default") is False, "reprocess all_meetings must default false")
    check("DEFAULT IS A DRY RUN" in d_rp and "_reprocess-backups" in d_rp and "ASR is NOT re-run" in d_rp,
          "reprocess desc must state dry-run default, backup location, and no ASR")
    check("\u2014" not in d_rp and bool(mcp_server.whosaid_reprocess.__doc__), "reprocess desc/docstring hygiene")

    # --- whosaid_record_browser: mutating start/stop/status, no permission needed ---
    rb = dumps["whosaid_record_browser"]
    rb_ann = rb.get("annotations") or {}
    d_rb = by_name["whosaid_record_browser"].description or ""
    check(rb_ann.get("readOnlyHint") is False and rb_ann.get("destructiveHint") is False
          and rb_ann.get("openWorldHint") is False, f"record_browser annotations: {rb_ann}")
    check(rb["inputSchema"].get("required") == ["action"], f"record_browser requires only action: {rb['inputSchema']}")
    check(set(rb["inputSchema"]["properties"]) == {"action", "cdp_port", "target", "leave_selector",
                                                   "max_seconds", "no_mic", "leave", "into", "label"},
          f"record_browser params: {sorted(rb['inputSchema']['properties'])}")
    check(set(rb["inputSchema"]["properties"]["action"].get("enum") or []) == {"start", "stop", "status"},
          "record_browser action enum")
    check("remote-debugging-port" in d_rb and "Chromium" in d_rb and "only that tab" in d_rb
          and "Screen & System Audio Recording" in d_rb and "start" in d_rb and "stop" in d_rb
          and "status" in d_rb, "record_browser desc must state CDP port, tab-only, no permission, actions")
    check("\u2014" not in d_rb and bool(mcp_server.whosaid_record_browser.__doc__), "record_browser hygiene")

    # --- record_browser process helpers: start refuses while a recorded pid is alive ---
    import os
    import signal
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="whosaid-rb-") as d:
        saved_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = d
        try:
            sp = mcp_server._rb_state_path()
            check(sp == Path(d) / "whosaid" / "record-browser.json", f"state path honors XDG_CACHE_HOME: {sp}")
            check(mcp_server._rb_status(sp)["running"] is False, "status with no state is not running")
            first = mcp_server._rb_start(["sleep", "30"], {"argv": ["sleep", "30"]}, sp)
            try:
                check(first["status"] == "recording" and mcp_server._rb_pid_alive(first["pid"]), f"start: {first}")
                second = mcp_server._rb_start(["sleep", "30"], {}, sp)
                check(second["status"] == "already_recording" and second["pid"] == first["pid"],
                      f"second start must refuse while the pid is alive: {second}")
                check(mcp_server._rb_status(sp)["running"] is True, "status sees the live pid")
                # stop: SIGINT kills sleep; no `captured` line in the log, so no output
                stopped = mcp_server._rb_stop(sp, wait=10)
                check(stopped["status"] == "stopped_no_output" and not mcp_server._rb_pid_alive(first["pid"]),
                      f"stop ends the process: {stopped}")
                check(not sp.exists(), "stop clears the state file")
                # a log with a captured line yields the output path
                lg = Path(d) / "x.log"
                lg.write_text("whosaid: captured 12.3s -> /tmp/a b/out.m4a\n")
                check(mcp_server._rb_output_from_log(str(lg)) == "/tmp/a b/out.m4a", "output parsed from captured line")
                third = mcp_server._rb_start(["sleep", "30"], {}, sp)
                check(third["status"] == "recording", f"start works again after stop: {third}")
                mcp_server._rb_stop(sp, wait=10)
            finally:
                for pid in (first.get("pid"),):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (OSError, TypeError):
                        pass
        finally:
            if saved_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = saved_xdg

    # --- server instructions gained the workspace paragraph ---
    instr = mcp_server.SERVER_INSTRUCTIONS
    check("Meeting workspace" in instr, "instructions must have a 'Meeting workspace' paragraph")
    check("WHOSAID_WORKSPACE" in instr and "whosaid index" in instr, "instructions must name the env var and the index command")
    check("whosaid_search" in instr and "whosaid_context" in instr, "instructions must describe search -> context")
    check(all(term in instr for term in ("accepted_owner", "whosaid_priorities", "whosaid_tracking_suggestions",
                                        "Historical as_of queries cannot use a current cache")),
          "instructions describe accepted obligations, priorities, suggestions and historical cache limits")

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
