#!/usr/bin/env python3
"""
live_explain.py: a live "explainer" page for the call being recorded (`whosaid live
explain`, GitHub issue #220).

Every few minutes of substantive talk becomes one section: a time range, a plain
explanation, a hand-drawn style SVG sketch, "S-T:" one-line simple translations,
claims attributed to the named speaker, open questions, what the user is asked or
owes, a "For you" answer for an ask aimed at the user, a "Suggested" box (the
explainer's own ideas, never something said on the call) and a low-confidence
footnote. Newest first; a final recap goes on top when the call ends.

  serve    Serve the page on localhost (default 127.0.0.1:8765) and push changes
           over server-sent events. The served copy gets the live client
           (explain_assets/live.js) injected and its 30 s meta refresh removed; the
           file on disk stays a plain static page that still works offline.
           --lan binds all interfaces, but a non-loopback client needs the page's
           one-time token (?token=..., then an HttpOnly cookie) or gets 403.
           --ws WS: a workspace with `[live] explain = "claude"` also gets sections
           written by Claude every few minutes (CLOUD, opt-in, budgeted).
  add      Add a section (JSON on stdin or --file).            } what an agent
  correct  Correct a section in place, keeping a before/after.  } without MCP
  recap    Write the final recap (decisions, questions, owes).  } calls
  render   Rebuild index.html from the section files.
  status   Where the page lives and how many sections it has.

Storage: <into>/.live/<stem>.explain/ next to the live transcript it explains.
One section is NNN-<slug>.json (the source, with every earlier revision) plus
NNN-<slug>.html (its rendered fragment); ids never change, so a correction updates
one section in place. index.html is rebuilt (tmp + os.replace) after every write.

Content comes only from whoever is configured: an agent over MCP
(whosaid_live_explain_add / _correct / _recap), these CLI verbs, or the opt-in
`[live] explain = "claude"` engine. Without one the page is empty and nothing is
sent anywhere. The page loads no remote resources: sketches are inline SVG,
sanitized on write (an allowlist of shapes and attributes, no scripts, no event
handlers, no external references), and a CSP forbids everything else. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import ipaddress
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402

ASSETS = Path(__file__).resolve().parent / "explain_assets"
SUFFIX = ".explain"
DEFAULT_PORT = 8765
REFRESH_SECONDS = 30
SSE_POLL = 0.5
SSE_PING = 15.0
MAX_SECTIONS = 999
TOKEN_FILE = ".token"
STATE_FILE = "engine.json"
CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline' 'self'; "
       "script-src 'unsafe-inline' 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
LIST_FIELDS = ("questions", "suggested", "low_confidence")
FIELDS = ("title", "from", "to", "explain", "sketch", "claims", "jargon", "questions", "asks",
          "for_you", "suggested", "low_confidence")


class ExplainError(ValueError):
    """Bad input from a producer (unknown id, unsafe sketch, missing title)."""


def log(msg: str) -> None:
    print(f"whosaid live explain: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- where the page lives

def store_dir(into: Path, session: str | None = None) -> Path:
    """<into>/.live/<stem>.explain for `session` (a stem) or the newest live transcript."""
    into = Path(into).expanduser()
    if session:
        stem = Path(session).name
        for suffix in (".jsonl", ".md", SUFFIX):
            stem = stem[: -len(suffix)] if stem.endswith(suffix) else stem
        if not re.fullmatch(r"[\w.-]+", stem):
            raise ExplainError(f"bad session name {session!r}")
        return into / live.LIVE_DIR / f"{stem}{SUFFIX}"
    found = live.sessions(into)
    if not found:
        raise ExplainError(f"no live transcript under {into / live.LIVE_DIR}: start `whosaid live` "
                           "while a call is recorded, or pass a session")
    return found[-1].with_name(found[-1].name[: -len(".jsonl")] + SUFFIX)


def stem_of(d: Path) -> str:
    return d.name[: -len(SUFFIX)] if d.name.endswith(SUFFIX) else d.name


def transcript_meta(d: Path) -> dict:
    """The live transcript's meta (started, ended), {} when there is none."""
    jl = d.with_name(stem_of(d) + ".jsonl")
    if not jl.is_file():
        return {}
    try:
        meta, _ = live.read_entries(jl)
        return meta
    except (OSError, ValueError):
        return {}


def write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:40].rstrip("-") or "section")


def section_files(d: Path) -> dict[str, Path]:
    """id -> NNN-<slug>.json, for every section in the store."""
    out = {}
    if d.is_dir():
        for p in sorted(d.glob("[0-9][0-9][0-9]-*.json")):
            out[p.name[:3]] = p
    return out


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- sketch sanitizer

SVG_NS = "http://www.w3.org/2000/svg"
SVG_TAGS = frozenset("""svg g path rect circle ellipse line polyline polygon text tspan title desc
defs marker use linearGradient radialGradient stop pattern clipPath mask symbol""".split())
SVG_ATTRS = frozenset("""viewBox width height x y x1 y1 x2 y2 cx cy r rx ry d points fill stroke
stroke-width stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset stroke-opacity
fill-opacity opacity transform font-size font-family font-weight font-style text-anchor
dominant-baseline letter-spacing id class role aria-label aria-hidden markerWidth markerHeight
refX refY orient markerUnits preserveAspectRatio offset stop-color stop-opacity gradientUnits
gradientTransform patternUnits clip-path mask marker-start marker-mid marker-end dx dy rotate
textLength lengthAdjust fill-rule clip-rule vector-effect xmlns href""".split())
_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]*)\1\s*\)", re.I)
_BAD_VALUE = re.compile(r"javascript:|data:|expression\(|@import|<", re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitize_svg(svg: str) -> str:
    """The sketch as safe inline SVG, or raise ExplainError. Allowlisted elements and
    attributes only; href and url() may only point inside the sketch (#id)."""
    svg = (svg or "").strip()
    if not svg:
        return ""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        raise ExplainError(f"sketch is not well-formed SVG: {e}") from e
    if _local(root.tag) != "svg":
        raise ExplainError("sketch must be one <svg> element")

    def clean(el: ET.Element) -> ET.Element:
        tag = _local(el.tag)
        if tag not in SVG_TAGS:
            raise ExplainError(f"sketch: <{tag}> is not allowed (shapes and text only)")
        out = ET.Element(tag)
        for k, v in el.attrib.items():
            name = _local(k)
            if name.lower().startswith("on") or name not in SVG_ATTRS:
                continue  # event handlers and anything unknown are dropped
            if name == "href" and not v.startswith("#"):
                raise ExplainError("sketch: href may only point inside the sketch (#id)")
            if _BAD_VALUE.search(v):
                raise ExplainError(f"sketch: unsafe value in {name}")
            for m in _URL.finditer(v):
                if not m.group(2).startswith("#"):
                    raise ExplainError("sketch: url() may only point inside the sketch (#id)")
            out.set(name, v)
        out.text, out.tail = el.text, el.tail
        for child in el:
            out.append(clean(child))
        return out

    safe = clean(root)
    safe.set("xmlns", SVG_NS)
    safe.set("role", safe.get("role") or "img")
    ET.register_namespace("", SVG_NS)
    text = ET.tostring(safe, encoding="unicode", short_empty_elements=True)
    return re.sub(r"\s+xmlns:ns\d+=\"[^\"]*\"", "", text)


# ---------------------------------------------------------------- normalize producer input

def _text(v) -> str:
    return " ".join(str(v or "").split())


def _secs(v, started: float | None) -> float | None:
    """t seconds since the call started from a number or an HH:MM[:SS] clock."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        raise ExplainError(f"bad time {v!r}: give seconds since the call started or HH:MM[:SS]")
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if not started:
        return float(h * 3600 + mi * 60 + se)
    base = datetime.fromtimestamp(started)
    at = base.replace(hour=h, minute=mi, second=se)
    delta = (at - base).total_seconds()
    return delta + 86400 if delta < -43200 else delta  # a call that crossed midnight


def _who_list(items, key="text") -> list[dict]:
    out = []
    for it in items or []:
        if isinstance(it, str):
            it = {key: it}
        if not isinstance(it, dict) or not _text(it.get(key)):
            continue
        out.append({"who": _text(it.get("who")), key: _text(it.get(key))})
    return out


def normalize(data: dict, started: float | None = None, base: dict | None = None) -> dict:
    """Producer input -> the stored section shape (base: the fields not given are kept)."""
    if not isinstance(data, dict):
        raise ExplainError("a section is a JSON object")
    unknown = set(data) - set(FIELDS) - {"id", "slug"}
    if unknown:
        raise ExplainError(f"unknown field(s): {', '.join(sorted(unknown))}; known: {', '.join(FIELDS)}")
    out = dict(base or {})
    if "title" in data or not base:
        out["title"] = _text(data.get("title"))
        if not out["title"]:
            raise ExplainError("a section needs a title")
    for k in ("from", "to"):
        if k in data or not base:
            out[k] = _secs(data.get(k), started)
    if "explain" in data or not base:
        ex = data.get("explain") or []
        paras = [p for p in (re.split(r"\n\s*\n", ex) if isinstance(ex, str) else ex)]
        out["explain"] = [_text(p) for p in paras if _text(p)]
    if "sketch" in data or not base:
        out["sketch"] = sanitize_svg(data.get("sketch") or "")
    if "claims" in data or not base:
        out["claims"] = _who_list(data.get("claims"))
    if "jargon" in data or not base:
        jg = data.get("jargon") or []
        if isinstance(jg, dict):
            jg = [{"term": k, "st": v} for k, v in jg.items()]
        out["jargon"] = [{"term": _text(j.get("term")), "st": _text(j.get("st") or j.get("text"))}
                         for j in jg if isinstance(j, dict) and _text(j.get("term"))]
    if "asks" in data or not base:
        out["asks"] = _who_list(data.get("asks"))
    if "for_you" in data or not base:
        fy = []
        for f in data.get("for_you") or []:
            if isinstance(f, dict) and _text(f.get("ask")):
                fy.append({"who": _text(f.get("who")), "ask": _text(f.get("ask")),
                           "answer": str(f.get("answer") or "").strip(), "source": _text(f.get("source"))})
        out["for_you"] = fy
    for k in LIST_FIELDS:
        if k in data or not base:
            v = data.get(k) or []
            out[k] = [_text(x) for x in ([v] if isinstance(v, str) else v) if _text(x)]
    return out


# ---------------------------------------------------------------- render

def clock(started: float | None, t: float | None) -> str:
    if t is None:
        return ""
    if started:
        return datetime.fromtimestamp(started + t).strftime("%H:%M")
    return live_insights_mmss(t)


def live_insights_mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}"


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _who(who: str) -> str:
    return f'<b class="xs-who">{e(who)}</b>: ' if who else ""


def answer_html(text: str) -> str:
    """A For-you answer as small, safe HTML: paragraphs, '- ' bullets and `code`."""
    blocks = []
    for chunk in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue

        def inline(s: str) -> str:
            return re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", e(s))
        if all(ln.lstrip().startswith(("- ", "* ")) for ln in lines):
            items = "".join(f"<li>{inline(ln.lstrip()[2:])}</li>" for ln in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append("<p>" + "<br>".join(inline(ln) for ln in lines) + "</p>")
    return "".join(blocks)


def flags(sec: dict) -> list[str]:
    out = []
    if sec.get("questions"):
        out.append("questions")
    if sec.get("asks") or sec.get("for_you"):
        out.append("asks")
    if sec.get("suggested"):
        out.append("suggested")
    if sec.get("corrections"):
        out.append("corrected")
    if sec.get("for_you"):
        out.append("foryou")
    return out


def render_section(sec: dict, started: float | None = None) -> str:
    """One stored section -> its <section class="xs"> fragment (the DOM contract)."""
    sid = sec["id"]
    t0, t1 = sec.get("from"), sec.get("to")
    span = f"{clock(started, t0)}–{clock(started, t1)}" if t0 is not None and t1 is not None else clock(started, t0)
    minutes = max(1, round(((t1 or 0) - (t0 or 0)) / 60)) if t0 is not None and t1 is not None else 1
    attrs = (f'class="xs" id="s-{sid}" data-id="{sid}" data-rev="{sec.get("rev", 1)}" '
             f'data-from="{"" if t0 is None else int(t0)}" data-to="{"" if t1 is None else int(t1)}" '
             f'data-minutes="{minutes}" data-flags="{" ".join(flags(sec))}"')
    parts = [f"<section {attrs}>",
             f'<header class="xs-head"><span class="xs-time">{e(span)}</span>'
             f'<h2 class="xs-title">{e(sec["title"])}</h2></header>', '<div class="xs-body">']
    parts.append('<div class="xs-explain">' + "".join(f"<p>{e(p)}</p>" for p in sec.get("explain") or []) + "</div>")
    if sec.get("sketch"):
        parts.append(f'<figure class="xs-sketch">{sec["sketch"]}</figure>')
    if sec.get("claims"):
        parts.append('<ul class="xs-claims">' + "".join(
            f"<li>{_who(c['who'])}{e(c['text'])}</li>" for c in sec["claims"]) + "</ul>")
    if sec.get("jargon"):
        parts.append('<div class="xs-st">' + "".join(
            f'<p class="xs-st-line"><b>S-T:</b> <dfn>{e(j["term"])}</dfn>: {e(j["st"])}</p>'
            for j in sec["jargon"]) + "</div>")
    if sec.get("questions"):
        parts.append('<div class="xs-questions"><h3>Open questions</h3><ul>' + "".join(
            f'<li class="xs-q" data-key="{sid}-q{i}">{e(q)}</li>' for i, q in enumerate(sec["questions"]))
            + "</ul></div>")
    if sec.get("asks"):
        parts.append('<div class="xs-asks"><h3>Asked of you / you owe</h3><ul>' + "".join(
            f'<li class="xs-ask" data-key="{sid}-a{i}">{_who(a["who"])}{e(a["text"])}</li>'
            for i, a in enumerate(sec["asks"])) + "</ul></div>")
    if sec.get("for_you"):
        boxes = []
        for i, f in enumerate(sec["for_you"]):
            asked = f'<b class="xs-who">{e(f["who"])}</b> asked: ' if f["who"] else "Asked: "
            boxes.append(
                f'<div class="xs-fy" data-key="{sid}-f{i}"><p class="xs-fy-ask">{asked}{e(f["ask"])}</p>'
                f'<div class="xs-fy-answer">{answer_html(f["answer"]) or "<p>(no answer found yet)</p>"}</div>'
                + (f'<p class="xs-fy-src">From: {e(f["source"])}</p>' if f["source"] else "")
                + '<button class="xs-copy" type="button">Copy reply</button></div>')
        parts.append('<div class="xs-foryou"><h3>For you</h3>' + "".join(boxes) + "</div>")
    if sec.get("corrections"):
        rows = []
        for c in sec["corrections"]:
            at = clock(started, c.get("t"))
            quote = (f'<blockquote class="xs-quote">“{e(c["quote"])}”'
                     + (f' <cite>{e(", ".join(x for x in (c.get("who"), at) if x))}</cite>' if c.get("who") or at else "")
                     + "</blockquote>") if c.get("quote") else ""
            rows.append(f'<div class="xs-correction" data-rev="{c["rev"]}">'
                        f'<p class="xs-old"><span class="xs-label">Before</span> <s>{e(c["before"])}</s></p>'
                        f'<p class="xs-new"><span class="xs-label">Corrected{" at " + e(at) if at else ""}</span> '
                        f'{e(c["after"])}</p>{quote}</div>')
        parts.append('<div class="xs-corrections">' + "".join(rows) + "</div>")
    if sec.get("suggested"):
        parts.append('<aside class="xs-suggested"><h3>Suggested <small>my ideas, not said on the call</small></h3><ul>'
                     + "".join(f"<li>{e(s)}</li>" for s in sec["suggested"]) + "</ul></aside>")
    if sec.get("low_confidence"):
        parts.append('<p class="xs-lowconf"><small>Low confidence: '
                     + "; ".join(e(x) for x in sec["low_confidence"]) + "</small></p>")
    parts.append("</div></section>")
    return "\n".join(parts) + "\n"


def render_recap(rc: dict) -> str:
    def ul(cls, items, key):
        return "".join(f'<li class="{cls}" data-key="recap-{key}{i}">{x}</li>' for i, x in enumerate(items))
    fl = [f for f, k in (("questions", "questions"), ("asks", "owes")) if rc.get(k)]
    parts = [f'<section class="xs xs-recap" id="s-recap" data-id="recap" data-rev="{rc.get("rev", 1)}" '
             f'data-flags="{" ".join(fl)}">',
             '<header class="xs-head"><span class="xs-time">final recap</span>'
             f'<h2 class="xs-title">{e(rc.get("title") or "Recap")}</h2></header>', '<div class="xs-body">']
    if rc.get("decisions"):
        parts.append('<div class="xs-decisions"><h3>Decisions</h3><ul>'
                     + "".join(f"<li>{e(d)}</li>" for d in rc["decisions"]) + "</ul></div>")
    if rc.get("questions"):
        parts.append('<div class="xs-questions"><h3>Open questions</h3><ul>'
                     + ul("xs-q", [e(q) for q in rc["questions"]], "q") + "</ul></div>")
    if rc.get("owes"):
        parts.append('<div class="xs-owes"><h3>Who owes what</h3><ul>'
                     + ul("xs-ask", [_who(o["who"]) + e(o["text"]) for o in rc["owes"]], "o") + "</ul></div>")
    parts.append("</div></section>")
    return "\n".join(parts) + "\n"


def asset(name: str) -> str:
    p = ASSETS / name
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def inline_script(js: str) -> str:
    return js.replace("</", "<\\/")


def glossary(d: Path, ws: str | Path | None = None) -> dict:
    """Term map for the popovers: each section's S-T jargon (oldest first), then
    [live.explain_glossary] in whosaid.toml, then <dir>/glossary.json (later wins)."""
    terms: dict[str, str] = {}
    for _, jp in sorted(section_files(d).items()):
        try:
            for j in load(jp).get("jargon") or []:
                if _text(j.get("term")) and _text(j.get("st")):
                    terms[_text(j["term"])] = _text(j["st"])
        except (OSError, ValueError, AttributeError) as err:
            log(f"{jp}: {err}")
    if ws:
        try:
            import wsconfig
            raw = (wsconfig.load_config(Path(ws).expanduser()).get("live") or {}).get("explain_glossary") or {}
            terms.update({str(k): _text(v) for k, v in raw.items() if _text(v)})
        except Exception as err:  # noqa: BLE001  a bad config never breaks the page
            log(f"glossary from {ws}: {err}")
    gp = d / "glossary.json"
    if gp.is_file():
        try:
            terms.update({str(k): _text(v) for k, v in json.loads(gp.read_text()).items() if _text(v)})
        except (OSError, ValueError, AttributeError) as err:
            log(f"{gp}: {err}")
    return terms


def fragments(d: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """(recap html or None, [(id, section html)] newest first) from the files on disk."""
    recap = (d / "recap.html").read_text(encoding="utf-8") if (d / "recap.html").is_file() else None
    secs = []
    for sid, jp in sorted(section_files(d).items(), reverse=True):
        hp = jp.with_suffix(".html")
        if hp.is_file():
            secs.append((sid, hp.read_text(encoding="utf-8")))
    return recap, secs


def render_page(d: Path, ws: str | Path | None = None) -> str:
    """The static page: every fragment inlined (recap on top, newest first)."""
    stem = stem_of(d)
    meta = transcript_meta(d)
    recap, secs = fragments(d)
    ended = bool(meta.get("ended")) or recap is not None
    body = (recap or "") + "".join(h for _, h in secs)
    if not body:
        body = ('<p class="xp-empty">No sections yet. They appear here as the call goes, written by '
                "the configured explainer (an agent over MCP, or `[live] explain = \"claude\"`). "
                "Without one this page stays empty and nothing is sent anywhere.</p>\n")
    gl = json.dumps(glossary(d, ws), ensure_ascii=False).replace("</", "<\\/")
    state = "ended" if ended else "live"
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}" data-xp-refresh>
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<title>Live explainer: {e(stem)}</title>
<style>{asset("explain.css")}</style>
</head>
<body>
<header id="xp-top">
  <div class="xp-bar">
    <h1>Live explainer <small class="xp-stem">{e(stem)}</small></h1>
    <span class="xp-state" data-state="{state}">{state}</span>
    <input id="xp-search" type="search" placeholder="Search" autocomplete="off">
    <button id="xp-theme" type="button">Light</button>
  </div>
  <div class="xp-chips">
    <button class="xp-chip" data-filter="questions" type="button">Open questions</button>
    <button class="xp-chip" data-filter="asks" type="button">My asks</button>
    <button class="xp-chip" data-filter="suggested" type="button">Suggestions</button>
    <button class="xp-chip" data-filter="corrected" type="button">Corrections</button>
  </div>
  <nav id="xp-timeline" aria-label="Topic timeline"></nav>
  <aside id="xp-foryou" hidden></aside>
</header>
<button id="xp-load" type="button" hidden></button>
<main id="xp-sections">
{body}</main>
<script type="application/json" id="xp-glossary">{gl}</script>
<script>{inline_script(asset("page.js"))}</script>
</body>
</html>
"""


REFRESH_TAG = re.compile(r'<meta http-equiv="refresh"[^>]*data-xp-refresh>\n?')


def served_page(page: str) -> str:
    """The served copy: no meta refresh (SSE replaces it), the live client injected."""
    page = REFRESH_TAG.sub("", page, count=1)
    i = page.rfind("</body>")
    return page[:i] + '<script src="/live.js"></script>\n' + page[i:]


def rebuild(d: Path, ws: str | Path | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    out = d / "index.html"
    write_text(out, render_page(d, ws))
    return out


def manifest(d: Path) -> dict:
    """What the page shows now: ids, revisions and fragment hashes, newest first."""
    def entry(sid: str, hp: Path, rev: int) -> dict:
        return {"id": sid, "rev": rev, "hash": hashlib.sha1(hp.read_bytes()).hexdigest()}
    secs = []
    for sid, jp in sorted(section_files(d).items(), reverse=True):
        hp = jp.with_suffix(".html")
        if hp.is_file():
            try:
                rev = int(load(jp).get("rev") or 1)
            except (OSError, ValueError):
                rev = 1
            secs.append(entry(sid, hp, rev))
    recap = None
    if (d / "recap.html").is_file():
        try:
            rev = int(load(d / "recap.json").get("rev") or 1)
        except (OSError, ValueError):
            rev = 1
        recap = entry("recap", d / "recap.html", rev)
    meta = transcript_meta(d)
    return {"stem": stem_of(d), "ended": bool(meta.get("ended")) or recap is not None,
            "sections": secs, "recap": recap}


# ---------------------------------------------------------------- write (the producer API)

_LOCK = threading.Lock()


def _started(d: Path) -> float | None:
    s = transcript_meta(d).get("started")
    return float(s) if s else None


def _save(d: Path, sec: dict, started: float | None) -> Path:
    jp = d / f"{sec['id']}-{sec['slug']}.json"
    write_text(jp, json.dumps(sec, indent=1, ensure_ascii=False))
    write_text(jp.with_suffix(".html"), render_section(sec, started))
    return jp


def add(d: Path, data: dict, ws=None, copy=None) -> dict:
    """A new section at the top -> {"ok", "id", "file", "rev"}. `copy(html, plain)`: the
    rich-text clipboard writer for a For-you answer (None: the default, see clipboard())."""
    with _LOCK:
        d.mkdir(parents=True, exist_ok=True)
        started = _started(d)
        sec = normalize(data, started)
        have = section_files(d)
        n = (int(max(have)) + 1) if have else 1
        if n > MAX_SECTIONS:
            raise ExplainError(f"at most {MAX_SECTIONS} sections per call")
        sec.update(id=f"{n:03d}", slug=slugify(data.get("slug") or sec["title"]), rev=1,
                   corrections=[], revisions=[], updated=time.time())
        jp = _save(d, sec, started)
        rebuild(d, ws)
    copied = _copy_answers(sec, copy)
    return {"ok": True, "id": sec["id"], "rev": 1, "file": str(jp.with_suffix(".html")),
            "page": str(d / "index.html"), "copied": copied}


def correct(d: Path, sid: str, before: str, after: str, quote: str = "", who: str = "", t=None,
            changes: dict | None = None, ws=None, copy=None) -> dict:
    """Correct section `sid` in place: the earlier revision is kept, `changes` (any section
    fields) are applied, and a before/after box (old claim, corrected claim with its time,
    the source quote) is added. Never a silent rewrite."""
    sid = re.sub(r"^s-", "", str(sid).strip())
    sid = f"{int(sid):03d}" if sid.isdigit() else sid
    if not _text(before) or not _text(after):
        raise ExplainError("a correction needs the old claim (before) and the corrected one (after)")
    with _LOCK:
        files = section_files(d)
        if sid not in files:
            raise ExplainError(f"no section {sid!r}; known: {', '.join(sorted(files)) or 'none'}")
        started = _started(d)
        old = load(files[sid])
        sec = normalize(changes or {}, started, base={k: old[k] for k in FIELDS if k in old}) if changes else \
            {k: old[k] for k in FIELDS if k in old}
        prior = {k: old[k] for k in FIELDS if k in old}
        prior["rev"] = old.get("rev", 1)
        rev = int(old.get("rev") or 1) + 1
        corr = {"rev": rev, "before": _text(before), "after": _text(after), "quote": _text(quote),
                "who": _text(who), "t": _secs(t, started)}
        sec.update(id=sid, slug=old["slug"], rev=rev, revisions=old.get("revisions", []) + [prior],
                   corrections=old.get("corrections", []) + [corr], updated=time.time())
        jp = _save(d, sec, started)
        if jp != files[sid]:  # never: the slug is kept, but stay tidy
            files[sid].unlink(missing_ok=True)
        rebuild(d, ws)
    copied = _copy_answers(sec, copy) if changes and "for_you" in changes else False
    return {"ok": True, "id": sid, "rev": rev, "file": str(jp.with_suffix(".html")),
            "page": str(d / "index.html"), "copied": copied}


def recap(d: Path, data: dict, ws=None) -> dict:
    """The final recap on top: decisions, open questions, who owes what."""
    if not isinstance(data, dict):
        raise ExplainError("the recap is a JSON object")
    with _LOCK:
        d.mkdir(parents=True, exist_ok=True)
        prev = load(d / "recap.json") if (d / "recap.json").is_file() else {}
        rc = {"title": _text(data.get("title")) or "Recap",
              "decisions": [_text(x) for x in data.get("decisions") or [] if _text(x)],
              "questions": [_text(x) for x in data.get("questions") or [] if _text(x)],
              "owes": _who_list(data.get("owes")), "rev": int(prev.get("rev") or 0) + 1,
              "updated": time.time()}
        if not (rc["decisions"] or rc["questions"] or rc["owes"]):
            raise ExplainError("a recap needs decisions, questions or owes")
        write_text(d / "recap.json", json.dumps(rc, indent=1, ensure_ascii=False))
        write_text(d / "recap.html", render_recap(rc))
        rebuild(d, ws)
    return {"ok": True, "id": "recap", "rev": rc["rev"], "file": str(d / "recap.html"),
            "page": str(d / "index.html")}


# ---------------------------------------------------------------- rich-text clipboard

_JXA = ("function run(argv){ObjC.import('AppKit');var pb=$.NSPasteboard.generalPasteboard;"
        "pb.clearContents;pb.setStringForType($(argv[0]),$.NSPasteboardTypeHTML);"
        "pb.setStringForType($(argv[1]),$.NSPasteboardTypeString);return 'ok';}")


def mac_clipboard(html_text: str, plain: str) -> bool:
    """Put HTML + plain text on the macOS pasteboard (pastes as rich text in a call chat)."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA, html_text, plain],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def clipboard():
    """The default writer: macOS pasteboard, off with WHOSAID_EXPLAIN_CLIPBOARD=0."""
    if os.environ.get("WHOSAID_EXPLAIN_CLIPBOARD", "1") == "0" or sys.platform != "darwin":
        return None
    return mac_clipboard


def _copy_answers(sec: dict, copy) -> bool:
    answers = [f for f in sec.get("for_you") or [] if f.get("answer")]
    if not answers:
        return False
    writer = clipboard() if copy is None else copy
    if not writer:
        return False
    last = answers[-1]  # the newest ask is the one waiting in the call chat
    return bool(writer(answer_html(last["answer"]), last["answer"]))


# ---------------------------------------------------------------- LAN token

def is_loopback(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def page_token(d: Path) -> str:
    """The page's one-time LAN token, created on first use with mode 600."""
    p = d / TOKEN_FILE
    d.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        tok = p.read_text().strip()
        if tok:
            os.chmod(p, 0o600)
            return tok
    tok = secrets.token_urlsafe(24)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    os.chmod(p, 0o600)
    return tok


def authorize(client: str, query_token: str | None, cookie_token: str | None, token: str | None) -> str:
    """'ok' | 'set-cookie' (valid ?token=: answer with the HttpOnly cookie) | 'forbidden'.
    Loopback is always ok; with no token (LAN mode off) nothing else is."""
    if is_loopback(client):
        return "ok"
    if not token:
        return "forbidden"
    if cookie_token and secrets.compare_digest(cookie_token, token):
        return "ok"
    if query_token and secrets.compare_digest(query_token, token):
        return "set-cookie"
    return "forbidden"


# ---------------------------------------------------------------- server

class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(d: Path, token: str | None, ws=None, stop: threading.Event | None = None):
    stop = stop or threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "whosaid-explain"

        def log_message(self, fmt, *args):  # quiet: one line per error only
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                log(f"{self.address_string()} {fmt % args}")

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(url.query)
            cookies = {}
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                cookies[k] = v
            verdict = authorize(self.client_address[0], (q.get("token") or [None])[0],
                                cookies.get("xp_token"), token)
            if verdict == "forbidden":
                return self._send(403, b"forbidden: open the link with its token\n", "text/plain; charset=utf-8")
            extra = {}
            if verdict == "set-cookie":
                extra["Set-Cookie"] = f"xp_token={token}; HttpOnly; SameSite=Strict; Path=/"
            path = url.path
            if path in ("/", "/index.html"):
                body = served_page(render_page(d, ws)).encode()
                return self._send(200, body, "text/html; charset=utf-8", extra)
            if path == "/live.js":
                return self._send(200, asset("live.js").encode(), "text/javascript; charset=utf-8", extra)
            if path == "/manifest.json":
                return self._send(200, json.dumps(manifest(d)).encode(), "application/json", extra)
            m = re.fullmatch(r"/section/(\d{3}|recap)", path)
            if m:
                sid = m.group(1)
                hp = d / "recap.html" if sid == "recap" else (
                    section_files(d)[sid].with_suffix(".html") if sid in section_files(d) else None)
                if hp is None or not hp.is_file():
                    return self._send(404, b"no such section\n", "text/plain; charset=utf-8")
                return self._send(200, hp.read_bytes(), "text/html; charset=utf-8", extra)
            if path == "/events":
                return self._events(extra)
            return self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def _events(self, extra: dict):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            last, pinged = None, time.time()
            try:
                while not stop.is_set():
                    m = manifest(d)
                    blob = json.dumps(m, sort_keys=True)
                    if blob != last:
                        self.wfile.write(f"event: manifest\ndata: {blob}\n\n".encode())
                        self.wfile.flush()
                        last, pinged = blob, time.time()
                    elif time.time() - pinged >= SSE_PING:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        pinged = time.time()
                    stop.wait(SSE_POLL)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def serve(d: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT, lan: bool = False, ws=None,
          stop: threading.Event | None = None):
    """Start the server; returns (server, url, token). The caller runs serve_forever()."""
    d.mkdir(parents=True, exist_ok=True)
    rebuild(d, ws)
    token = page_token(d) if lan else None
    srv = _Server(("0.0.0.0" if lan else host, port), make_handler(d, token, ws, stop))
    url = f"http://{'127.0.0.1' if not lan else host}:{srv.server_address[1]}/"
    return srv, url, token


# ---------------------------------------------------------------- opt-in engine ([live] explain)

ENGINE_DEFAULTS = {"explain": "", "explain_model": "sonnet", "explain_every": 240, "explain_max_calls": 30,
                   "explain_timeout": 120, "explain_max_tokens": 3000}
ENGINE_MIN_LINES = 8
ENGINE_MAX_FAILURES = 3
ENGINE_PROMPT_CHARS = 20000
ENGINE_SYSTEM = (
    "You keep a live explainer page for a meeting in progress. 'me' is the user (their mic); "
    "'them' is the other side, and a line from a recognized voice shows that person's name (a live "
    "guess; a line marked '?' is a weak match). You get the sections written so far (id, title, "
    "time range, explanation) and the transcript lines since the last section, each as "
    "[seconds] who: text. Reply with JSON only:\n"
    '{"sections": [{"title": "...", "from": <seconds>, "to": <seconds>, "explain": ["<plain '
    'paragraph>"], "sketch": "<svg ...>...</svg>", "claims": [{"who": "<name>", "text": "..."}], '
    '"jargon": [{"term": "...", "st": "<one-line simple translation>"}], "questions": ["..."], '
    '"asks": [{"who": "...", "text": "<asked of the user or the user owes>"}], "for_you": [{"who": '
    '"...", "ask": "...", "answer": "<a reply the user can paste>", "lookup": "<search words>"}], '
    '"suggested": ["<your own idea>"], "low_confidence": ["<weak voice match or likely mis-hear>"]}], '
    '"corrections": [{"id": "<NNN>", "before": "<old claim>", "after": "<corrected claim>", '
    '"quote": "<the words that corrected it>", "who": "...", "t": <seconds>, "changes": {<fields to '
    'replace>}}], "recap": null or {"decisions": [], "questions": [], "owes": [{"who": "", "text": ""}]}}\n'
    "Write a new section only for a few minutes of substantive talk; none is fine. Explain in plain "
    "words for a smart outsider. The sketch is a small hand-drawn style inline SVG (viewBox about "
    "0 0 600 220, shapes and text only, stroke='currentColor', no scripts or links) showing a flow, a "
    "before/after or who-does-what. Attribute claims to the named speaker. 'suggested' is your own "
    "ideas, never something said. When later talk corrects an earlier section, add a correction "
    "with its id instead of a new section. Give a recap only when told the call ended. Use only "
    "what the transcript says.")


def engine_config(ws) -> dict | None:
    """The [live] explain settings when the workspace opted in (explain = "claude"), else None."""
    if not ws:
        return None
    import wsconfig
    conf = wsconfig.load_config(Path(ws).expanduser())
    raw = conf.get("live") or {}
    if str(raw.get("explain") or "").strip().lower() != "claude":
        return None
    cfg = dict(ENGINE_DEFAULTS)
    for k in ENGINE_DEFAULTS:
        if k in raw and k != "explain":
            cfg[k] = type(ENGINE_DEFAULTS[k])(raw[k])
    cfg["explain"] = "claude"
    cfg["claude_bin"] = str(raw.get("claude_bin") or (conf.get("summarizer") or {}).get("claude_bin") or "")
    cfg["ws"] = str(Path(ws).expanduser())
    return cfg


def _engine_client(cfg: dict):
    """The Claude CLI client; tests replace this."""
    from claude_engine import ClaudeCLI
    return ClaudeCLI(cfg["explain_model"], binary=cfg.get("claude_bin", ""),
                     timeout=int(cfg["explain_timeout"]), max_tokens=int(cfg["explain_max_tokens"]))


def _lookup(ws: str, words: str) -> str:
    """Local workspace search for a For-you ask -> 'meeting t: text' lines ('' when none)."""
    if not ws or not _text(words):
        return ""
    try:
        import search
        _, hits, _ = search.query(Path(ws), " OR ".join(re.findall(r"[A-Za-z0-9]{3,}", words)[:8]), k=3)
    except Exception:  # noqa: BLE001  no index, a bad query: just no lookup
        return ""
    return "\n".join(f"- {h.meeting} {h.t_str} {h.speaker}: {h.text[:200]}" for h in hits)


def engine_state(d: Path) -> dict:
    try:
        s = json.loads((d / STATE_FILE).read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def engine_due(state: dict, meta: dict, entries: list[dict], cfg: dict) -> bool:
    if (len(entries) < ENGINE_MIN_LINES or int(state.get("calls") or 0) >= int(cfg["explain_max_calls"])
            or int(state.get("failures") or 0) >= ENGINE_MAX_FAILURES):
        return False
    last_t = max(float(x["t"]) for x in entries)
    seen = float(state.get("t") if state.get("t") is not None else -1e9)
    if meta.get("ended"):
        return last_t > seen or not state.get("final")
    return last_t - seen >= float(cfg["explain_every"])


def engine_prompt(d: Path, entries: list[dict], seen: float, ended: bool) -> str:
    parts = ["Sections so far:"]
    for sid, jp in sorted(section_files(d).items()):
        s = load(jp)
        parts.append(f"- id {sid} [{s.get('from')}-{s.get('to')}] {s['title']}: {' '.join(s.get('explain') or [])[:400]}")
    lines, size = [], 0
    for x in reversed(sorted(entries, key=lambda x: float(x["t"]))):
        if float(x["t"]) <= seen:
            break
        line = f"[{int(float(x['t']))}] {live.speaker(x)}{'?' if x.get('voice') and not x.get('name') else ''}: {x.get('text') or ''}"
        size += len(line) + 1
        if size > ENGINE_PROMPT_CHARS:
            break
        lines.append(line)
    parts += ["", "The call has ENDED: write the recap." if ended else "The call is in progress.", "",
              "Transcript since the last section:", *reversed(lines)]
    return "\n".join(parts)


def engine_step(d: Path, jl: Path, cfg: dict, copy=None) -> dict | None:
    """One Claude pass when due -> the state written, else None. Malformed sections or
    corrections are skipped one by one (logged), never the whole reply."""
    meta, entries = live.read_entries(jl)
    state = engine_state(d)
    if not engine_due(state, meta, entries, cfg):
        return None
    seen = float(state.get("t") if state.get("t") is not None else -1e9)
    last_t = max(float(x["t"]) for x in entries)
    out = {"t": last_t, "final": bool(meta.get("ended")), "calls": int(state.get("calls") or 0) + 1,
           "tokens": int(state.get("tokens") or 0), "failures": 0, "error": "", "added": [], "corrected": [],
           "skipped": [], "updated": time.time()}
    client = None
    try:
        client = _engine_client(cfg)
        reply = client.chat(ENGINE_SYSTEM, engine_prompt(d, entries, seen, bool(meta.get("ended"))))
        m = re.search(r"\{.*\}", reply, re.S)
        got = json.loads(m.group(0)) if m else {}
        for sec in (got.get("sections") or [])[:4]:
            try:
                for f in sec.get("for_you") or []:
                    found = _lookup(cfg.get("ws", ""), f.pop("lookup", "") or f.get("ask", ""))
                    if found:
                        f["answer"] = (str(f.get("answer") or "").strip() + "\n\n" + found).strip()
                        f["source"] = f.get("source") or "earlier meetings and chats (local search)"
                out["added"].append(add(d, sec, ws=cfg.get("ws"), copy=copy)["id"])
            except (ExplainError, AttributeError, TypeError) as err:
                out["skipped"].append(f"section: {err}"[:200])
        for c in (got.get("corrections") or [])[:4]:
            try:
                out["corrected"].append(correct(d, str(c.get("id")), c.get("before"), c.get("after"),
                                                c.get("quote") or "", c.get("who") or "", c.get("t"),
                                                c.get("changes") or None, ws=cfg.get("ws"), copy=copy)["id"])
            except (ExplainError, AttributeError, TypeError, ValueError) as err:
                out["skipped"].append(f"correction: {err}"[:200])
        if meta.get("ended") and isinstance(got.get("recap"), dict):
            try:
                recap(d, got["recap"], ws=cfg.get("ws"))
            except ExplainError as err:
                out["skipped"].append(f"recap: {err}"[:200])
    except Exception as err:  # noqa: BLE001  any failure: keep what we have, wait explain_every
        out["error"] = str(err)[:300]
        out["failures"] = int(state.get("failures") or 0) + 1
        out["t"] = state.get("t")
        log(f"{jl.name}: {out['error']}")
    out["tokens"] += int(getattr(client, "tokens", 0) or 0)
    d.mkdir(parents=True, exist_ok=True)
    write_text(d / STATE_FILE, json.dumps(out, indent=1))
    return out


class EngineWorker(threading.Thread):
    def __init__(self, d: Path, jl: Path, cfg: dict, stop: threading.Event, poll: float = 5.0):
        super().__init__(daemon=True, name="whosaid-live-explain")
        self.d, self.jl, self.cfg, self.stop, self.poll = d, jl, cfg, stop, poll

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                res = engine_step(self.d, self.jl, self.cfg)
                if res is not None:
                    log(f"call {res['calls']}/{self.cfg['explain_max_calls']}, {res['tokens']} tokens so far, "
                        f"added {res['added'] or 'none'}, corrected {res['corrected'] or 'none'}"
                        + (f", failed: {res['error']}" if res["error"] else ""))
            except (OSError, ValueError) as err:
                log(f"{self.jl.name}: {err}")
            self.stop.wait(self.poll)


# ---------------------------------------------------------------- CLI

def _read_json(a) -> dict:
    raw = Path(a.file).read_text() if getattr(a, "file", None) else sys.stdin.read()
    try:
        return json.loads(raw)
    except ValueError as err:
        raise ExplainError(f"not JSON: {err}") from err


def _dir(a) -> Path:
    return Path(a.dir).expanduser() if getattr(a, "dir", None) else store_dir(Path(a.into), a.session)


def cmd_serve(a) -> int:
    d = _dir(a)
    stop = threading.Event()
    srv, url, token = serve(d, a.host, a.port, a.lan, a.ws, stop)
    cfg = engine_config(a.ws)
    jl = d.with_name(stem_of(d) + ".jsonl")
    if cfg and jl.is_file():
        log(f"[live] explain = \"claude\": CLOUD, transcript lines go to Anthropic every "
            f"{cfg['explain_every']} s of talk (at most {cfg['explain_max_calls']} calls)")
        EngineWorker(d, jl, cfg, stop).start()
    else:
        log("no explainer engine configured: sections come only from an agent (MCP) or "
            "`whosaid live explain add`; nothing is sent anywhere")
    print(f"serving {d / 'index.html'} at {url}", flush=True)
    if token:
        print(f"LAN mode: other devices open http://<this-mac>:{srv.server_address[1]}/?token={token}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


def _emit(res: dict) -> int:
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid live explain",
                                description="A live explainer page for the call being recorded (#220).")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(q):
        q.add_argument("--into", default=str(live.DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
        q.add_argument("--session", help="the live session stem (default: the newest)")
        q.add_argument("--dir", help="the explainer folder itself (overrides --into/--session)")
        q.add_argument("--ws", help="workspace: its whosaid.toml [live] glossary and explain engine")
    s = sub.add_parser("serve", help="serve the page on localhost and push changes over SSE")
    common(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}; 0 = any free port")
    s.add_argument("--lan", action="store_true", help="bind all interfaces; other devices need the token link")
    for name, hlp in (("add", "add a section (JSON on stdin or --file)"),
                      ("recap", "write the final recap (JSON: decisions, questions, owes)")):
        q = sub.add_parser(name, help=hlp)
        common(q)
        q.add_argument("--file", help="read the JSON from this file instead of stdin")
        if name == "add":
            q.add_argument("--no-copy", action="store_true", help="never put a For-you answer on the clipboard")
    c = sub.add_parser("correct", help="correct a section in place, keeping a before/after box")
    common(c)
    c.add_argument("id", help="section id (NNN)")
    c.add_argument("--before", required=True, help="the old claim")
    c.add_argument("--after", required=True, help="the corrected claim")
    c.add_argument("--quote", default="", help="the words that corrected it")
    c.add_argument("--who", default="", help="who corrected it")
    c.add_argument("--at", default=None, help="when: seconds since the call started or HH:MM[:SS]")
    c.add_argument("--file", help="JSON of section fields to replace (optional)")
    for name in ("render", "status"):
        common(sub.add_parser(name))
    a = p.parse_args(argv)
    try:
        if a.cmd == "serve":
            return cmd_serve(a)
        d = _dir(a)
        if a.cmd == "add":
            return _emit(add(d, _read_json(a), ws=a.ws, copy=(lambda *_: False) if a.no_copy else None))
        if a.cmd == "correct":
            changes = _read_json(a) if a.file else None
            return _emit(correct(d, a.id, a.before, a.after, a.quote, a.who, a.at, changes, ws=a.ws))
        if a.cmd == "recap":
            return _emit(recap(d, _read_json(a), ws=a.ws))
        if a.cmd == "render":
            return _emit({"ok": True, "page": str(rebuild(d, a.ws))})
        m = manifest(d)
        return _emit({"ok": True, "dir": str(d), "page": str(d / "index.html"), "exists": d.is_dir(),
                      "sections": len(m["sections"]), "recap": m["recap"] is not None, "ended": m["ended"],
                      "engine": "claude" if engine_config(a.ws) else "none"})
    except ExplainError as err:
        print(f"whosaid live explain: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
