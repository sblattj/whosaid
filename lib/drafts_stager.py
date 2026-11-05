#!/usr/bin/env python3
"""
drafts_stager.py: the built-in stager for `whosaid drafts stage` (GitHub issue #168).

Opens a chat in the Teams web app, pastes draft.md as rich text, attaches pages and focuses the
tab, in a Chromium browser started with --remote-debugging-port (the port `whosaid record
--browser` uses). It NEVER sends: no Enter/Return key event is ever dispatched (one choke point,
CDP.send, raises on it), and no click lands on anything that looks like a send button (click()
hit-tests the point first). Ported from the proven teams-draft stage.ts.

Usage (the contract in lib/drafts.py):
  drafts_stager.py --chat NAME --md FILE [--attach PAGE ...] [--mention NAME ...]
                   [--replace --only-these-attachments] [--new-chat] [--tab ID]
                   [--cdp-port N] [--bypass-outbox]
--cdp-port defaults to $WHOSAID_CDP_PORT, else 9222. Refuses (exit 2) unless $TEAMS_OUTBOX_LOCK
names an existing path (`drafts stage` sets it), unless --bypass-outbox.
Wave 1 does not do --mention, --new-chat or --only-these-attachments: they exit 2 "not implemented yet".

Output: the last stdout line is {"sent": false, "chat": <title>, "chars": n, "links": n,
"attachments": n}. Errors go to stderr, exit 1; usage errors exit 2.
Runtime dep: websockets (`uv run --with websockets`). Poll intervals scale by
$WHOSAID_STAGER_TIME_SCALE (float, default 1; tests use a small one).

FAKE-CDP CONTRACT (frozen; wave 2 extends it). Every Runtime.evaluate expression starts with
`/*op:NAME*/` followed by `/*args:JSON*/` (the JSON of the op's arguments, "*/" escaped as "*\\/",
so it stays valid JSON). A fake dispatches on NAME. Ops and their return values:
  rail_find    args {name}        {"total": n treeitems in the left rail, "matches": [{x,y}] of items whose
                                  first line equals name exactly}
  title        {}                 document.title (string)
  editor_ready {}                 bool, the compose box exists
  compose_len  {}                 int, trimmed innerText length of the compose box
  editor_focus {}                 null
  editor_select {}                null, selects the compose box contents with a range
  paste        {html, plain}      {"len": int, "links": int} after a synthetic paste event
  search_box   {}                 {x,y} of the search box, or null
  search_find  {name}             {"total": n options, "matches": [{x,y}] with first line == name exactly}
  hit_test     {x, y}             bool, true when the element at x,y is (inside) a send button
  attach_btn   {}                 {x,y} of the Attach files button, or null
  menu_upload  {}                 {x,y} of the "Upload from this device" menu item, or null
  chip_seen    {name}             bool, an attachment chip for the file name is visible
  caret_end    {}                 null, puts the caret at the end of the draft
  final        {}                 {"len": int, "links": int}
Other CDP calls: Target.getTargets, setDiscoverTargets, createTarget, attachToTarget,
activateTarget, Page.enable/bringToFront/setInterceptFileChooserDialog, DOM.enable/getDocument/
querySelector/setFileInputFiles, Input.dispatchMouseEvent/dispatchKeyEvent/insertText.
"""

from __future__ import annotations

import argparse
import asyncio
import html as htmllib
import json
import os
import re
import sys
import urllib.request

IS_TEAMS = re.compile(r"teams\.(cloud\.)?microsoft(\.com)?/")
TEAMS_URL = "https://teams.cloud.microsoft/"


class StagerError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def scale() -> float:
    try:
        return float(os.environ.get("WHOSAID_STAGER_TIME_SCALE") or 1)
    except ValueError:
        return 1.0


async def nap(seconds: float) -> None:
    await asyncio.sleep(seconds * scale())


# ---- markdown to HTML (stdlib only) ----------------------------------------------------

def _inline(text: str) -> str:
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append("<code>" + htmllib.escape(m.group(1), quote=False) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = htmllib.escape(text, quote=False)

    def link(m: re.Match) -> str:
        url = htmllib.unescape(m.group(2)).strip()
        if re.match(r"\s*(javascript|data|vbscript):", url, re.I):
            return m.group(0)
        return f'<a href="{htmllib.escape(url, quote=True)}">{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    para: list[str] = []
    items: list[str] = []
    list_tag = ""

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_tag
        if items:
            out.append(f"<{list_tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{list_tag}>")
            items.clear()
        list_tag = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*(```|~~~)", line):
            flush_para()
            flush_list()
            fence = re.match(r"^\s*(```|~~~)", line).group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence):
                body.append(lines[i])
                i += 1
            out.append("<pre><code>" + htmllib.escape("\n".join(body), quote=False) + "</code></pre>")
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if not line.strip():
            flush_para()
            flush_list()
        elif h:
            flush_para()
            flush_list()
            out.append(f"<h{len(h.group(1))}>{_inline(h.group(2))}</h{len(h.group(1))}>")
        elif ul or ol:
            flush_para()
            tag = "ul" if ul else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append((ul or ol).group(1))
        else:
            flush_list()
            para.append(line.strip())
        i += 1
    flush_para()
    flush_list()
    return "".join(out)


# ---- CDP -------------------------------------------------------------------------------

def _is_enter(params: dict) -> bool:
    return (str(params.get("key", "")).lower() in ("enter", "return", "\r", "\n")
            or str(params.get("code", "")).lower() in ("enter", "numpadenter")
            or params.get("windowsVirtualKeyCode") == 13
            or "\r" in str(params.get("text", "")) or "\n" in str(params.get("text", "")))


def op(_op: str, _body: str, **args) -> str:
    """A Runtime.evaluate expression carrying the fake-CDP marker and its args."""
    a = json.dumps(args).replace("*/", "*\\/")
    return (f"/*op:{_op}*//*args:{a}*/(()=>{{const A={json.dumps(args)};"
            "const ED=()=>document.querySelector('[data-tid=\"ckeditor\"], [contenteditable=true][role=textbox]');"
            f"{_body}}})()")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self.session: str | None = None
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                d = json.loads(raw)
                if d.get("id") in self.pending:
                    self.pending.pop(d["id"]).set_result(d)
                elif d.get("method"):
                    self.events.append(d)
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(StagerError("browser connection closed"))

    async def send(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        params = params or {}
        # The single choke point: nothing that reads as Enter/Return ever leaves this process.
        if method == "Input.dispatchKeyEvent" and _is_enter(params):
            raise StagerError("refusing to dispatch an Enter key: this stager never sends", 1)
        if method == "Input.insertText" and ("\n" in params.get("text", "") or "\r" in params.get("text", "")):
            raise StagerError("refusing to insert a newline: this stager never sends", 1)
        self.seq += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = fut
        msg = {"id": self.seq, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        d = await asyncio.wait_for(fut, 60)
        if d.get("error"):
            raise StagerError(f"{method}: {d['error'].get('message')}")
        return d.get("result") or {}

    async def s(self, method: str, params: dict | None = None) -> dict:
        return await self.send(method, params, self.session)

    async def ev(self, expression: str):
        r = await self.s("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                              "returnByValue": True})
        if r.get("exceptionDetails"):
            ex = r["exceptionDetails"]
            raise StagerError((ex.get("exception") or {}).get("description") or ex.get("text", "script error"))
        return (r.get("result") or {}).get("value")

    async def key(self, k: str, code: str, vk: int, mod: int = 0):
        for typ in ("keyDown", "keyUp"):
            await self.s("Input.dispatchKeyEvent", {"type": typ, "key": k, "code": code,
                                                     "windowsVirtualKeyCode": vk, "modifiers": mod})

    async def click(self, x: float, y: float):
        """A real mouse press (Teams ignores untrusted JS .click()), after a hit test that refuses send buttons."""
        if await self.ev(op("hit_test", """return !!document.elementFromPoint(A.x,A.y)?.closest(
          '[data-tid*="send" i],[aria-label="Send" i],[aria-label^="Send " i]')""", x=x, y=y)):
            raise StagerError("refusing to click a send button: this stager never sends", 1)
        for typ in ("mousePressed", "mouseReleased"):
            await self.s("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left",
                                                       "clickCount": 1})


# ---- ops ---------------------------------------------------------------------------------

JS_RAIL = """const items=[...document.querySelectorAll('[role=treeitem]')];
const m=items.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);
if(m.length===1)m[0].scrollIntoView({block:'center'});
return {total:items.length,matches:m.map(t=>{const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})}"""
JS_SEARCH_BOX = """const b=document.querySelector('input[role=combobox][aria-label*="Search" i],[role=combobox][aria-label*="Search" i],input[type=search]');
if(!b)return null;const r=b.getBoundingClientRect();if(!r.width)return null;return {x:r.x+r.width/2,y:r.y+r.height/2}"""
JS_SEARCH_FIND = """const o=[...document.querySelectorAll('[role=option]')];
const m=o.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);
return {total:o.length,matches:m.map(t=>{const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})}"""
JS_SELECT = """const e=ED();e.focus();const r=document.createRange();r.selectNodeContents(e);
const x=getSelection();x.removeAllRanges();x.addRange(r);return null"""
JS_PASTE = """const e=ED();e.focus();const dt=new DataTransfer();dt.setData('text/html',A.html);dt.setData('text/plain',A.plain);
e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));
return new Promise(r=>setTimeout(()=>r({len:e.innerText.trim().length,links:e.querySelectorAll('a').length}),800))"""
JS_ATTACH_BTN = """return (async()=>{for(let i=0;i<20;i++){const b=document.querySelector('[data-tid=sendMessageCommands-FilePicker],[data-tid=newMessageCommands-FilePicker]')||[...document.querySelectorAll('button')].find(b=>/attach files/i.test(b.getAttribute('aria-label')||''));
if(b){b.scrollIntoView({block:'nearest'});const r=b.getBoundingClientRect();if(r.width)return {x:r.x+r.width/2,y:r.y+r.height/2}}
await new Promise(r=>setTimeout(r,500))}return null})()"""
JS_MENU = """return (async()=>{await new Promise(r=>setTimeout(r,1200));const m=[...document.querySelectorAll('[role=menuitem]')].find(m=>/upload from this device/i.test(m.innerText));
if(!m)return null;const r=m.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()"""
JS_CHIP = """const n=A.name;return [...document.querySelectorAll('[aria-label],[title]')].some(e=>((e.getAttribute('aria-label')||'')+(e.getAttribute('title')||'')).includes(n))||document.body.innerText.includes(n)"""
JS_CARET = """const e=ED();e.focus();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);
const x=getSelection();x.removeAllRanges();x.addRange(r);return null"""
JS_FINAL = "const e=ED();return {len:e.innerText.trim().length,links:e.querySelectorAll('a').length}"
JS_LEN = "return (ED()?.innerText||'').trim().length"


async def find_tab(c: CDP, want: str | None) -> str:
    tabs = (await c.send("Target.getTargets")).get("targetInfos", [])
    if want:
        if not any(t["targetId"] == want for t in tabs):
            raise StagerError(f"no tab {want}")
        return want
    for t in tabs:
        if t.get("type") == "page" and IS_TEAMS.search(t.get("url", "")):
            return t["targetId"]
    # Vivaldi wedges a tab attached in its first milliseconds: wait for its first
    # Target.targetInfoChanged (capped at 2s) before attaching.
    await c.send("Target.setDiscoverTargets", {"discover": True})
    tid = (await c.send("Target.createTarget", {"url": TEAMS_URL}))["targetId"]
    for _ in range(20):
        if any(e["method"] == "Target.targetInfoChanged" and e["params"].get("targetInfo", {}).get("targetId") == tid
               for e in c.events):
            break
        await nap(0.1)
    await c.send("Target.setDiscoverTargets", {"discover": False})
    print("opened a Teams tab", file=sys.stderr)
    return tid


async def open_chat(c: CDP, chat: str) -> str:
    matches: list[dict] = []
    for _ in range(30):  # wait for the rail to load, then trust what it shows
        r = await c.ev(op("rail_find", JS_RAIL, name=chat))
        matches = r["matches"]
        if matches or r["total"] > 0:
            break
        await nap(1)
    source = "left rail"
    if not matches:
        source = "search"
        box = await c.ev(op("search_box", JS_SEARCH_BOX))
        if not box:
            raise StagerError(f"no chat named {chat} in the left rail and no search box found (signed in?)")
        await c.click(box["x"], box["y"])
        await c.s("Input.insertText", {"text": chat})
        last = -1
        for _ in range(20):
            await nap(0.5)
            r = await c.ev(op("search_find", JS_SEARCH_FIND, name=chat))
            matches = r["matches"]
            if matches and len(matches) == last:
                break
            last = len(matches)
        if not matches:
            raise StagerError(f"no chat named {chat} (rail or search)")
    if len(matches) > 1:
        raise StagerError(f"ambiguous: {len(matches)} chats named {chat}")
    await c.click(matches[0]["x"], matches[0]["y"])
    title = ""
    for _ in range(20):
        title = await c.ev(op("title", "return document.title"))
        if chat in title:
            break
        await nap(0.5)
    if chat not in title:
        raise StagerError(f'title is "{title}", not {chat}; stopping ({source})')
    return title


async def stage(a) -> dict:
    html = md_to_html(open(a.md, encoding="utf-8").read())
    plain = open(a.md, encoding="utf-8").read()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.cdp_port}/json/version", timeout=5) as r:
            ws_url = json.load(r)["webSocketDebuggerUrl"]
    except Exception:
        raise StagerError(f"no browser on DevTools port {a.cdp_port}; start one with --remote-debugging-port")
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # older websockets
        from websockets import connect
    async with connect(ws_url, max_size=None, ping_interval=None) as ws:
        c = CDP(ws)
        try:
            tid = await find_tab(c, a.tab)
            c.session = (await c.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
            title = await open_chat(c, a.chat)

            for _ in range(20):
                if await c.ev(op("editor_ready", "return !!ED()")):
                    break
                await nap(0.5)
            existing = await c.ev(op("compose_len", JS_LEN))
            if existing > 0 and not a.replace:
                raise StagerError(f"compose box already holds {existing} chars; rerun with --replace to overwrite")
            await c.ev(op("editor_focus", "ED().focus();return null"))
            # Meta+A doesn't select over CDP on macOS: select with a range, then a real Backspace so the
            # editor's own model updates.
            for _ in range(3):
                if await c.ev(op("compose_len", JS_LEN)) <= 0:
                    break
                await c.ev(op("editor_select", JS_SELECT))
                await c.key("Backspace", "Backspace", 8)
                await nap(0.4)
            if await c.ev(op("compose_len", JS_LEN)) > 0:
                raise StagerError("couldn't clear the compose box; clear it by hand and rerun")

            pasted = await c.ev(op("paste", JS_PASTE, html=html, plain=plain))
            if not pasted or not pasted.get("len"):
                raise StagerError("paste left the compose box empty")

            if a.attach:
                await attach(c, a.attach)

            await c.send("Target.activateTarget", {"targetId": tid})
            try:
                await c.s("Page.bringToFront")
            except StagerError:
                pass
            await c.ev(op("caret_end", JS_CARET))
            fin = await c.ev(op("final", JS_FINAL))
            return {"sent": False, "chat": title, "chars": fin["len"], "links": fin["links"],
                    "attachments": len(a.attach)}
        finally:
            c.reader.cancel()


async def attach(c: CDP, files: list[str]):
    await c.s("Page.enable")
    await c.s("DOM.enable")
    await c.s("Page.setInterceptFileChooserDialog", {"enabled": True})

    async def input_node() -> int:
        doc = await c.s("DOM.getDocument", {"depth": -1, "pierce": True})
        return (await c.s("DOM.querySelector", {"nodeId": doc["root"]["nodeId"],
                                                 "selector": "input[type=file]"})).get("nodeId") or 0

    node = await input_node()
    if node:
        await c.s("DOM.setFileInputFiles", {"nodeId": node, "files": files})
    else:
        # Teams mounts its file input only once the Attach menu is open, and ignores untrusted clicks.
        pin = await c.ev(op("attach_btn", JS_ATTACH_BTN))
        if not pin:
            raise StagerError("no Attach files button in the compose box")
        await c.click(pin["x"], pin["y"])
        box = await c.ev(op("menu_upload", JS_MENU))
        node = await input_node()
        if node:
            await c.s("DOM.setFileInputFiles", {"nodeId": node, "files": files})
        else:
            if not box:
                raise StagerError("Attach menu has no 'Upload from this device'")
            await c.click(box["x"], box["y"])
            fc = None
            for _ in range(20):
                fc = next((e for e in c.events if e["method"] == "Page.fileChooserOpened"), None)
                if fc:
                    break
                await nap(0.25)
            if not fc:
                raise StagerError("couldn't open the attach file chooser")
            await c.s("DOM.setFileInputFiles", {"backendNodeId": fc["params"]["backendNodeId"], "files": files})
        for typ in ("keyDown", "keyUp"):
            await c.s("Input.dispatchKeyEvent", {"type": typ, "key": "Escape", "code": "Escape",
                                                  "windowsVirtualKeyCode": 27})
    await c.s("Page.setInterceptFileChooserDialog", {"enabled": False})
    for f in files:
        name = os.path.basename(f)
        for _ in range(40):
            if await c.ev(op("chip_seen", JS_CHIP, name=name)):
                break
            await nap(0.5)
        else:
            raise StagerError(f"attachment chip for {f} never showed up")


# ---- CLI ---------------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="drafts_stager.py", description="Stage a draft in a Teams web chat. Never sends.")
    p.add_argument("--chat", required=True)
    p.add_argument("--md", required=True)
    p.add_argument("--attach", action="append", default=[])
    p.add_argument("--mention", action="append", default=[])
    p.add_argument("--replace", action="store_true")
    p.add_argument("--only-these-attachments", action="store_true")
    p.add_argument("--new-chat", action="store_true")
    p.add_argument("--tab")
    p.add_argument("--cdp-port", type=int, default=int(os.environ.get("WHOSAID_CDP_PORT") or 9222))
    p.add_argument("--bypass-outbox", action="store_true")
    return p.parse_args(argv)  # argparse exits 2 on usage errors


def check_args(a: argparse.Namespace, env=os.environ) -> None:
    """Raise StagerError(code 2) for anything that must not reach the browser."""
    if not a.chat.strip() or "\n" in a.chat or "\r" in a.chat:
        raise StagerError("--chat must be a single-line name", 2)
    if not os.path.isfile(a.md):
        raise StagerError(f"--md file not found: {a.md}", 2)
    for f in a.attach:
        if not os.path.isabs(f) or not os.path.exists(f):
            raise StagerError(f"attach needs an existing absolute path: {f}", 2)
    lock = env.get("TEAMS_OUTBOX_LOCK", "")
    if not a.bypass_outbox and not (lock and os.path.exists(lock)):
        raise StagerError("refusing to run outside the outbox: TEAMS_OUTBOX_LOCK is unset or missing "
                          "(use `whosaid drafts stage`, or --bypass-outbox)", 2)
    for flag, on in (("--mention", a.mention), ("--new-chat", a.new_chat),
                     ("--only-these-attachments", a.only_these_attachments)):
        if on:
            raise StagerError(f"{flag} is not implemented yet", 2)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        check_args(a)
        res = asyncio.run(stage(a))
    except StagerError as e:
        print(f"drafts_stager: {e}", file=sys.stderr)
        return e.code
    except OSError as e:
        print(f"drafts_stager: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
