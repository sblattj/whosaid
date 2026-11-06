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
--only-these-attachments (needs --replace, else exit 2): after clearing the text, every attachment chip
already in the COMPOSE ROOT is removed (its remove control, a real click, hit-tested to be inside the
root; at most chips+1 clicks, each must lower the count) and 0 must remain (else exit 1) before the
--attach pages go in. The compose root is the nearest ancestor of the editor that also holds the Attach
button; history attachments outside it are never touched. If the root can't be found: a draft without
--attach pages skips the removal with a stderr warning, one with pages exits 1. Without the flag,
existing chips stay. After an upload the chips inside the root must have grown by the number of files
and each file name must show inside the root; "attachments" in the output is the measured chip count.
A global deadline (270s, $WHOSAID_STAGER_DEADLINE) turns a hang into exit 1 "timed out during <step>".
--new-chat: presses "New chat", types NAME in the To picker, picks the suggestion whose first line is
NAME exactly (none: exit 1 "no person named"; several: exit 1 ambiguous) and checks the To field now
shows a chip with NAME (a new chat's title need not contain it), then continues to the compose box.
--mention NAME (repeatable) makes REAL @mentions. PLACEMENT RULE: mentions go at the START of the
message, in the given order, each followed by one space, before the pasted body. A literal `@NAME` in
the draft text is stripped from the pasted text (html and plain, plus one blank after it) so it isn't
duplicated. Each mention: type "@NAME" with Input.insertText, wait for the suggestion list, click the
option whose first line is NAME exactly (none: exit 1 "no mention suggestion for NAME"; several:
exit 1 ambiguous; never Enter), then check the compose box gained one mention element. Mentions are
inserted after any --replace clearing and before the paste.

Output: the last stdout line is {"sent": false, "chat": <title>, "chars": n, "links": n,
"attachments": n, "mentions": n}. Errors go to stderr, exit 1; usage errors exit 2.
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
  hit_test     {x, y, sel}        bool, true when the element at x,y is (inside) a match of the CSS selector
                                  sel (SEND_GUARD: real send controls only, not the Attach button)
  attach_btn   {}                 {x,y} of the Attach files button, or null
  menu_upload  {}                 {x,y} of the "Upload from this device" menu item, or null
  chip_seen    {name}             LEGACY, unused: page-wide "name appears anywhere" (the old, unsound check)
  caret_end   {}                 null, puts the caret at the end of the draft
  final        {}                 {"len": int, "links": int}
Wave 2 ops:
  root_chip_removers {}           [{x,y}] remove control of each attachment chip INSIDE the compose root
  root_chip_names {}              [str] one text per attachment chip inside the compose root (null: no root)
  in_root      {x, y}             bool, the element at x,y is inside the compose root
  focus_is     {sel}              bool, document.activeElement is (inside) the first match of sel
  chip_removers {}                LEGACY, unused: removers page-wide (the old, over-broad query)
  new_chat_btn {}                 {x,y} of the New chat control, or null
  people_picker {}                {x,y} of the To input, or null
  people_find  {name}             {"total": n, "matches": [{x,y}]} suggestion rows, first line == name
  to_chip      {name}             bool, the To field shows a recipient chip with that name
  mention_count {}                int, mention elements in the compose box
  mention_find {name}             {"total": n, "matches": [{x,y}]} like people_find, for the @ list
(caret_end also means: the compose box has focus with the caret at its end.)
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


# ---- selectors that were NOT checked on real Teams (UNVERIFIED): all guesses live here ------

# Real send controls only. NOT a substring match: the Attach button is data-tid
# sendMessageCommands-FilePicker and must stay clickable. The tid names are UNVERIFIED.
SEND_TIDS = ("sendMessageCommands-send", "newMessageCommands-send", "send-button")
SEND_GUARD = ",".join([f'[data-tid="{t}"]' for t in SEND_TIDS]
                      + ['[aria-label="Send" i]', '[aria-label^="Send (" i]', '[aria-label^="Send message" i]'])
# PROVEN on real Teams (stage.ts): the editor, and the Attach button. The compose root is derived from
# these two only: the nearest ancestor of the editor that also contains the Attach button.
EDITOR_SEL = '[data-tid="ckeditor"], [contenteditable=true][role=textbox]'
ATTACH_SEL = '[data-tid=sendMessageCommands-FilePicker],[data-tid=newMessageCommands-FilePicker]'
# One attachment chip, and its remove control inside it. Only ever queried INSIDE the compose root (UNVERIFIED).
CHIP_SEL = '[data-tid*="attachment" i],[data-tid*="file-card" i],[data-tid*="file-chip" i]'
CHIP_REMOVE_SEL = 'button[aria-label^="Remove" i],button[aria-label^="Dismiss" i],[data-tid*="remove" i]'
# The search box (UNVERIFIED).
SEARCH_SEL = ('input[role=combobox][aria-label*="Search" i],[role=combobox][aria-label*="Search" i],'
              'input[type=search]')
# The "New chat" control (UNVERIFIED).
NEW_CHAT_SEL = '[aria-label="New chat" i],[data-tid*="new-chat" i]'
# The To / people picker input in a new chat, and a chosen recipient chip in it (UNVERIFIED).
PEOPLE_PICKER_SEL = ('input[aria-label^="To" i],[data-tid*="people-picker" i] input,'
                     '[role=combobox][aria-label*="To:" i]')
TO_CHIP_SEL = '[data-tid*="people-picker" i] [data-tid*="pill" i],[data-tid*="people-picker" i] [data-tid*="persona" i]'
# Suggestion rows of any open suggestion list (people picker and @mention) (UNVERIFIED).
SUGGEST_SEL = "[role=listbox] [role=option]"
# A real mention element inside the compose box (UNVERIFIED).
MENTION_SEL = '[itemtype*="Mention" i],[data-itemtype="mention"],span.mention,[data-mention]'


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
        url = htmllib.unescape(m.group(2))
        # Browsers drop leading C0/space and any tab/newline inside the scheme before parsing it.
        probe = re.sub(r"[\t\n\r]", "", re.sub(r"^[\x00-\x20]+", "", url))
        scheme = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", probe)
        if scheme and scheme.group(1).lower() not in ("http", "https", "mailto"):
            return m.group(1)  # unsafe scheme: the label as plain text, no <a>
        url = url.strip()
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
    """True for anything on Input.dispatchKeyEvent that could commit a message: Enter/Return by key, code,
    virtual key code (13 win, 36/76 mac), a newline in text/unmodifiedText, or ANY editing `commands`."""
    return (str(params.get("key", "")).lower() in ("enter", "return", "\r", "\n")
            or str(params.get("code", "")).lower() in ("enter", "numpadenter")
            or params.get("windowsVirtualKeyCode") in (13, 36, 76)
            or params.get("nativeVirtualKeyCode") in (13, 36, 76)
            or "commands" in params
            or any(c in str(params.get(k, "")) for k in ("text", "unmodifiedText") for c in "\r\n"))


# Which step the flow is in, for the "timed out during <step>" message.
STEP = ["starting"]


def step(name: str) -> None:
    STEP[0] = name


def deadline() -> float:
    try:
        return float(os.environ.get("WHOSAID_STAGER_DEADLINE") or 270)
    except ValueError:
        return 270.0


# The compose root: nearest ancestor of the editor that also contains the Attach button (null if none).
_ROOT_JS = ("const ROOT=()=>{const ed=ED();if(!ed)return null;"
            f"const has=n=>!!n.querySelector({json.dumps(ATTACH_SEL)})||[...n.querySelectorAll('button')]"
            ".some(b=>/attach files/i.test(b.getAttribute('aria-label')||''));"
            "let n=ed.parentElement;while(n&&n!==document.documentElement){if(has(n))return n;n=n.parentElement}return null};")


def op(_op: str, _body: str, **args) -> str:
    """A Runtime.evaluate expression carrying the fake-CDP marker and its args."""
    a = json.dumps(args).replace("*/", "*\\/")
    return (f"/*op:{_op}*//*args:{a}*/(()=>{{const A={json.dumps(args)};"
            f"const ED=()=>document.querySelector({json.dumps(EDITOR_SEL)});"
            f"{_ROOT_JS}{_body}}})()")


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
        seq = self.seq
        fut = asyncio.get_running_loop().create_future()
        self.pending[seq] = fut
        msg = {"id": seq, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        try:
            d = await asyncio.wait_for(fut, 60)
        except (asyncio.TimeoutError, TimeoutError):
            self.pending.pop(seq, None)
            raise StagerError(f"timed out after 60s waiting for the browser to answer {method}")
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
        if await self.ev(op("hit_test", "return !!document.elementFromPoint(A.x,A.y)?.closest(A.sel)",
                            x=x, y=y, sel=SEND_GUARD)):
            raise StagerError("refusing to click a send button: this stager never sends", 1)
        for typ in ("mousePressed", "mouseReleased"):
            await self.s("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left",
                                                       "clickCount": 1})


# ---- ops ---------------------------------------------------------------------------------

JS_RAIL = """const items=[...document.querySelectorAll('[role=treeitem]')];
const m=items.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);
if(m.length===1)m[0].scrollIntoView({block:'center'});
return {total:items.length,matches:m.map(t=>{const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})}"""
JS_SEARCH_BOX = ("const b=document.querySelector(" + json.dumps(SEARCH_SEL) + ");"
                 "if(!b)return null;const r=b.getBoundingClientRect();if(!r.width)return null;"
                 "return {x:r.x+r.width/2,y:r.y+r.height/2}")
JS_FOCUS_IS = "const b=document.querySelector(A.sel);const f=document.activeElement;return !!b&&!!f&&(f===b||b.contains(f))"
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
JS_CARET = """const e=ED();e.focus();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);
const x=getSelection();x.removeAllRanges();x.addRange(r);return null"""
JS_FINAL = "const e=ED();return {len:e.innerText.trim().length,links:e.querySelectorAll('a').length}"
JS_LEN = "return (ED()?.innerText||'').trim().length"
_XY = "const xy=e=>{const r=e.getBoundingClientRect();return r.width?{x:r.x+r.width/2,y:r.y+r.height/2}:null};"
_OPTS = ("const o=[...document.querySelectorAll(" + json.dumps(SUGGEST_SEL) + ")];"
         "const m=o.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);"
         "return {total:o.length,matches:m.map(xy).filter(Boolean)}")
# Chips are only ever looked up INSIDE the compose root (outermost match only, so nested cards count once).
_CHIPS = (f"const root=ROOT();const chips=root?[...root.querySelectorAll({json.dumps(CHIP_SEL)})]"
          f".filter(c=>{{const p=c.parentElement&&c.parentElement.closest({json.dumps(CHIP_SEL)});"
          "return !(p&&root.contains(p)&&p!==root)}):[];")
# root_chip_names: null when the compose root can't be located (callers decide what that means)
JS_ROOT_CHIP_NAMES = (_CHIPS + "if(!root)return null;return chips.map(c=>[c.innerText,c.getAttribute('aria-label'),"
                      "c.getAttribute('title'),...[...c.querySelectorAll('[aria-label],[title]')]"
                      ".map(e=>(e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||''))].filter(Boolean).join(' '))")
JS_ROOT_CHIP_REMOVERS = (_CHIPS + _XY + f"return chips.map(c=>c.querySelector({json.dumps(CHIP_REMOVE_SEL)}))"
                         ".filter(Boolean).map(xy).filter(Boolean)")
JS_IN_ROOT = "const r=ROOT();const e=document.elementFromPoint(A.x,A.y);return !!(r&&e&&r.contains(e))"
JS_NEW_CHAT_BTN = _XY + f"const b=document.querySelector({json.dumps(NEW_CHAT_SEL)});return b?xy(b):null"
JS_PEOPLE_PICKER = _XY + f"const b=document.querySelector({json.dumps(PEOPLE_PICKER_SEL)});return b?xy(b):null"
JS_PEOPLE_FIND = _XY + _OPTS
JS_TO_CHIP = (f"return [...document.querySelectorAll({json.dumps(TO_CHIP_SEL)})]"
              ".some(e=>e.innerText.trim().split('\\n')[0].trim()===A.name)")
JS_MENTION_COUNT = f"return (ED()?.querySelectorAll({json.dumps(MENTION_SEL)}).length)||0"
JS_MENTION_FIND = _XY + _OPTS


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


async def type_into(c: CDP, sel: str, text: str, what: str):
    """Input.insertText into the element matching sel, only after checking it really has focus
    (else the text would land in the compose box or nowhere)."""
    if not await c.ev(op("focus_is", JS_FOCUS_IS, sel=sel)):
        raise StagerError(f"couldn't focus the {what}")
    await c.s("Input.insertText", {"text": text})


async def open_chat(c: CDP, chat: str) -> str:
    matches: list[dict] = []
    rendered = 0  # polls since the rail first showed any item
    for _ in range(45):  # wait for the rail, then give the exact row up to 15 polls (~15s) to appear
        r = await c.ev(op("rail_find", JS_RAIL, name=chat))
        matches = r["matches"]
        if matches:
            break
        if r["total"] > 0:
            rendered += 1
            if rendered >= 15:
                break
        await nap(1)
    source = "left rail"
    if not matches:
        source = "search"
        box = await c.ev(op("search_box", JS_SEARCH_BOX))
        if not box:
            raise StagerError(f"no chat named {chat} in the left rail and no search box found (signed in?)")
        await c.click(box["x"], box["y"])
        await type_into(c, SEARCH_SEL, chat, "search box")
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


async def pick_exact(c: CDP, opname: str, js: str, name: str, none_msg: str, what: str) -> dict:
    """Poll a suggestion list until its exact-name matches settle; return the single match."""
    matches: list[dict] = []
    last = -1
    for _ in range(20):
        await nap(0.5)
        matches = (await c.ev(op(opname, js, name=name)))["matches"]
        if matches and len(matches) == last:
            break
        last = len(matches)
    if not matches:
        raise StagerError(none_msg)
    if len(matches) > 1:
        raise StagerError(f"ambiguous: {len(matches)} {what} named {name}")
    return matches[0]


async def new_chat(c: CDP, name: str) -> str:
    btn = await c.ev(op("new_chat_btn", JS_NEW_CHAT_BTN))
    if not btn:
        raise StagerError("no New chat control found (signed in?)")
    await c.click(btn["x"], btn["y"])
    box = None
    for _ in range(20):
        box = await c.ev(op("people_picker", JS_PEOPLE_PICKER))
        if box:
            break
        await nap(0.5)
    if not box:
        raise StagerError("no To field in the new chat")
    await c.click(box["x"], box["y"])
    await type_into(c, PEOPLE_PICKER_SEL, name, "To field")
    m = await pick_exact(c, "people_find", JS_PEOPLE_FIND, name, f"no person named {name}", "people")
    await c.click(m["x"], m["y"])
    for _ in range(20):
        if await c.ev(op("to_chip", JS_TO_CHIP, name=name)):
            return name
        await nap(0.5)
    raise StagerError(f"the To field doesn't show {name}; stopping")


async def clear_compose(c: CDP):
    # Meta+A doesn't select over CDP on macOS: select with a range, then a real Backspace so the
    # editor's own model updates.
    await c.ev(op("editor_focus", "ED().focus();return null"))
    for _ in range(3):
        if await c.ev(op("compose_len", JS_LEN)) <= 0:
            break
        await c.ev(op("editor_select", JS_SELECT))
        await c.key("Backspace", "Backspace", 8)
        await nap(0.4)


async def chip_names(c: CDP) -> list[str] | None:
    """The attachment chips inside the compose root, one text each; None when the root isn't found."""
    return await c.ev(op("root_chip_names", JS_ROOT_CHIP_NAMES))


async def remove_chips(c: CDP, have_pages: bool):
    """Remove every attachment chip inside the compose root (and nothing outside it). No root: skip with a
    warning when no pages are going in, else fail (the check afterwards would be meaningless)."""
    names = await chip_names(c)
    if names is None:
        if have_pages:
            raise StagerError("couldn't find the compose area to clear its old attachments")
        print("drafts_stager: warning: couldn't find the compose area; leaving old attachments alone",
              file=sys.stderr)
        return
    start = len(names)
    for _ in range(start + 1):  # never more clicks than chips (+1 slack)
        if not names:
            return
        btns = await c.ev(op("root_chip_removers", JS_ROOT_CHIP_REMOVERS))
        if not btns:
            break
        x, y = btns[0]["x"], btns[0]["y"]
        if not await c.ev(op("in_root", JS_IN_ROOT, x=x, y=y)):
            raise StagerError("a remove control is outside the compose box; stopping")
        before = len(names)
        await c.click(x, y)
        for _ in range(10):
            await nap(0.3)
            names = await chip_names(c)
            if names is None or len(names) < before:
                break
        if names is None or len(names) >= before:
            raise StagerError("clicking an attachment's remove control didn't remove it")
    if names:
        raise StagerError("couldn't remove the old attachments")


async def insert_mentions(c: CDP, names: list[str]):
    for name in names:
        before = await c.ev(op("mention_count", JS_MENTION_COUNT))
        await c.ev(op("caret_end", JS_CARET))
        await c.s("Input.insertText", {"text": "@" + name})
        m = await pick_exact(c, "mention_find", JS_MENTION_FIND, name,
                             f"no mention suggestion for {name}", "mention suggestions")
        await c.click(m["x"], m["y"])
        for _ in range(20):
            if await c.ev(op("mention_count", JS_MENTION_COUNT)) == before + 1:
                break
            await nap(0.3)
        else:
            raise StagerError(f"picking the suggestion for {name} did not insert a mention")
        await c.ev(op("caret_end", JS_CARET))
        await c.s("Input.insertText", {"text": " "})


def strip_mention_literals(md: str, names: list[str]) -> str:
    """Drop a literal @NAME (and one following blank) from the draft text: the real mention replaces it."""
    for n in names:
        md = re.sub(r"(?<!\w)@" + re.escape(n) + r"(?!\w)[ \t]?", "", md)
    return md


async def stage(a) -> dict:
    plain = strip_mention_literals(open(a.md, encoding="utf-8").read(), a.mention)
    html = md_to_html(plain)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.cdp_port}/json/version", timeout=5) as r:
            ws_url = json.load(r)["webSocketDebuggerUrl"]
    except Exception:
        raise StagerError(f"no browser on DevTools port {a.cdp_port}; start one with --remote-debugging-port")
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # older websockets
        from websockets import connect
    step("connecting to the browser")
    async with connect(ws_url, max_size=None, ping_interval=None) as ws:
        c = CDP(ws)
        try:
            step("finding the Teams tab")
            tid = await find_tab(c, a.tab)
            c.session = (await c.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
            step("opening the chat")
            title = await (new_chat(c, a.chat) if a.new_chat else open_chat(c, a.chat))

            step("waiting for the compose box")
            for _ in range(20):
                if await c.ev(op("editor_ready", "return !!ED()")):
                    break
                await nap(0.5)
            step("clearing the compose box")
            existing = await c.ev(op("compose_len", JS_LEN))
            if existing > 0 and not a.replace:
                raise StagerError(f"compose box already holds {existing} chars; rerun with --replace to overwrite")
            await clear_compose(c)
            if await c.ev(op("compose_len", JS_LEN)) > 0:
                raise StagerError("couldn't clear the compose box; clear it by hand and rerun")
            if a.only_these_attachments:
                step("removing old attachments")
                await remove_chips(c, bool(a.attach))

            if a.mention:  # after the clearing, so the mentions lead the message
                step("inserting mentions")
                try:
                    await insert_mentions(c, a.mention)
                except StagerError:
                    try:
                        await clear_compose(c)  # don't leave a half-typed "@NAME" behind
                    except StagerError:
                        pass
                    raise
                await c.ev(op("caret_end", JS_CARET))

            step("pasting the draft")
            pasted = await c.ev(op("paste", JS_PASTE, html=html, plain=plain))
            if not pasted or not pasted.get("len"):
                raise StagerError("paste left the compose box empty")

            if a.attach:
                step("attaching pages")
                await attach(c, a.attach)

            step("focusing the tab")
            await c.send("Target.activateTarget", {"targetId": tid})
            try:
                await c.s("Page.bringToFront")
            except StagerError:
                pass
            await c.ev(op("caret_end", JS_CARET))
            fin = await c.ev(op("final", JS_FINAL))
            measured = len(await chip_names(c) or [])  # chips actually in the compose box, not what was asked
            return {"sent": False, "chat": title, "chars": fin["len"], "links": fin["links"],
                    "attachments": measured, "mentions": len(a.mention)}
        finally:
            c.reader.cancel()


async def stage_with_deadline(a) -> dict:
    """stage() under one global cap; a hang becomes a clean error naming the step it was in."""
    step("starting")
    try:
        return await asyncio.wait_for(stage(a), deadline())
    except (asyncio.TimeoutError, TimeoutError):
        raise StagerError(f"timed out during {STEP[0]} (global limit {deadline():g}s)")


async def attach(c: CDP, files: list[str]):
    """Upload the files, then prove it: the chips INSIDE the compose root grew by len(files) and each file
    name shows there. The file-chooser intercept is always switched off again, even on failure."""
    before = await chip_names(c)
    if before is None:
        raise StagerError("couldn't find the compose area to check the attachments")
    await c.s("Page.enable")
    await c.s("DOM.enable")
    await c.s("Page.setInterceptFileChooserDialog", {"enabled": True})
    try:
        await upload(c, files)
        names: list[str] = []
        for _ in range(40):
            names = await chip_names(c) or []
            if len(names) >= len(before) + len(files) and all(
                    any(os.path.basename(f) in n for n in names) for f in files):
                return
            await nap(0.5)
        missing = [f for f in files if not any(os.path.basename(f) in n for n in names)]
        raise StagerError(f"attachment chip for {(missing or files)[0]} never showed up in the compose box "
                          f"(chips {len(before)} -> {len(names)}, wanted +{len(files)})")
    finally:
        try:
            await asyncio.wait_for(c.s("Page.setInterceptFileChooserDialog", {"enabled": False}), 3)
        except Exception:
            pass  # the connection may be gone; nothing more to do


async def upload(c: CDP, files: list[str]):
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
    p.add_argument("--cdp-port", type=int, default=None)
    p.add_argument("--bypass-outbox", action="store_true")
    a = p.parse_args(argv)  # argparse exits 2 on usage errors
    if a.cdp_port is None:  # only read the env var when the flag is absent
        raw = os.environ.get("WHOSAID_CDP_PORT") or "9222"
        try:
            a.cdp_port = int(raw)
        except ValueError:
            raise StagerError(f"WHOSAID_CDP_PORT is not a port number: {raw!r}", 2)
    return a


def check_args(a: argparse.Namespace, env=os.environ) -> None:
    """Raise StagerError(code 2) for anything that must not reach the browser."""
    if not a.chat.strip() or "\n" in a.chat or "\r" in a.chat:
        raise StagerError("--chat must be a single-line name", 2)
    for m in a.mention:
        if not m.strip() or "\n" in m or "\r" in m:
            raise StagerError("--mention must be a single-line name", 2)
    if not os.path.isfile(a.md):
        raise StagerError(f"--md file not found: {a.md}", 2)
    try:
        with open(a.md, encoding="utf-8") as fh:
            fh.read()
    except UnicodeDecodeError:
        raise StagerError(f"--md file is not valid UTF-8: {a.md}", 2)
    for f in a.attach:
        if not os.path.isabs(f) or not os.path.exists(f):
            raise StagerError(f"attach needs an existing absolute path: {f}", 2)
    lock = env.get("TEAMS_OUTBOX_LOCK", "")
    if not a.bypass_outbox and not (lock and os.path.exists(lock)):
        raise StagerError("refusing to run outside the outbox: TEAMS_OUTBOX_LOCK is unset or missing "
                          "(use `whosaid drafts stage`, or --bypass-outbox)", 2)
    if a.only_these_attachments and not a.replace:
        raise StagerError("--only-these-attachments requires --replace", 2)


def main(argv: list[str] | None = None) -> int:
    try:
        a = parse_args(sys.argv[1:] if argv is None else argv)
        check_args(a)
        res = asyncio.run(stage_with_deadline(a))
    except StagerError as e:
        print(f"drafts_stager: {e}", file=sys.stderr)
        return e.code
    except (asyncio.TimeoutError, TimeoutError) as e:
        print(f"drafts_stager: timed out: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"drafts_stager: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # never a traceback: websocket failures, bad values, odd browser replies
        try:
            from websockets.exceptions import WebSocketException
        except ImportError:
            WebSocketException = ()  # type: ignore
        if isinstance(e, UnicodeDecodeError):
            print(f"drafts_stager: input is not valid UTF-8: {e}", file=sys.stderr)
            return 2
        if isinstance(e, WebSocketException):
            print(f"drafts_stager: browser connection failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"drafts_stager: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
