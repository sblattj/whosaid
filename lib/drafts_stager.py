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
Port: --cdp-port N, else $WHOSAID_CDP_PORT, else the Teams desktop app's port ($WHOSAID_TEAMS_CDP_PORT,
default 9333) when it answers as Edg/ (app mode: picks the chat window, never opens a tab), else 9222
(browser mode). Refuses (exit 2) unless $TEAMS_OUTBOX_LOCK
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
--new-chat: presses "New message" (formerly "New chat"), types NAME in the To picker, picks the suggestion whose first line is
NAME exactly (none: exit 1 "no person named"; several: exit 1 ambiguous) and checks the To field now
shows a chip with NAME (a new chat's title need not contain it), then continues to the compose box.
--mention NAME (repeatable) makes REAL @mentions. PLACEMENT RULE: mentions go at the START of the
message, in the given order, each followed by one space, before the pasted body. A literal `@NAME` in
the draft text is stripped from the pasted text (html and plain, plus one blank after it) so it isn't
duplicated. Each mention: type "@NAM" with Input.insertText and the last letter as a real key event (the
picker only filters on key events, #183), wait for the suggestion list, click the
option whose first line is NAME exactly (whitespace normalised; several: exit 1 ambiguous; never Enter),
then check the compose box gained one mention element. A NAME with a one-letter word ("Ana B") gets a
second query without it ("@Ana") when the full one lists nobody, still picking "Ana B" exactly. A query
that finds nobody is taken back (Escape, one Backspace per typed character) before the next; none
left: exit 1 "no mention suggestion for NAME", after clearing the compose box (and saying so if it
didn't clear). Mentions are inserted after any --replace clearing and before the paste.

--edit-sent --match-file F (#176): edit one of YOUR messages already in the chat instead of composing.
Finds the own message whose text matches F (same rule as `drafts mark-sent --check`; none: exit 1, several:
exit 1 ambiguous), hovers it, clicks its "More options", then the "Edit" menu item, waits for the edit box
(an editor that is not the compose box), clears it and pastes --md. It never saves: no Enter, and in this
mode a click on a Done/Save control is refused too. Messages from others after it in the chat view stop
it (exit 1) unless --allow-newer (#209). Not combinable with --attach/--mention/--replace/--new-chat (exit 2). Output: {"sent": false, "saved": false, "edit": true, "chat", "chars", "links"}.

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
Wave 3 ops (--edit-sent):
  own_msgs     {}                 [{text}] your messages rendered in the chat, oldest first
  msg_more     {key}              null when none of your messages starts with key, else scrolls it into view and
                                  returns {"at": {x,y} of its body, "more": {x,y} of its "More options" or null}
  menu_item    {label}            {x,y} of the menu item whose first line is label, or null
  newer_others {key}              int, chat items after our message's item with a message and none of ours
                                  (-1 no such message, 0 when the view has no chat items)
  edit_ready   {}                 {"editors": n edit boxes (editors outside the compose root), "text": str}
  edit_len     {}                 int, trimmed text length of the one edit box (-1 when not exactly one)
  edit_select  {}                 null, selects the edit box contents with a range
  edit_paste   {html, plain}      {"len": int, "links": int} after a synthetic paste into the edit box
(caret_end also means: the compose box has focus with the caret at its end.)
Other CDP calls: Target.getTargets, setDiscoverTargets, createTarget, attachToTarget, detachFromTarget,
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

# Anchored to the URL's host: a Teams URL inside another page's path or query must not match.
IS_TEAMS = re.compile(r"^https://teams\.(cloud\.)?microsoft(\.com)?(:\d+)?/", re.I)
TEAMS_URL = "https://teams.cloud.microsoft/"


# ---- selectors: CHECKED = matched on a signed-in Teams web tab (read-only probe, 2026-11-06);
# UNVERIFIED = still a guess (needs typing or an attachment to appear) ------

# Real send controls only. NOT a substring match: the Attach button is data-tid
# sendMessageCommands-FilePicker and must stay clickable. CHECKED: the real Send button is
# data-tid sendMessageCommands-send, aria-label "Send"; the Attach button does not match.
SEND_TIDS = ("sendMessageCommands-send", "newMessageCommands-send", "send-button")
SEND_GUARD = ",".join([f'[data-tid="{t}"]' for t in SEND_TIDS]
                      + ['[aria-label="Send" i]', '[aria-label^="Send (" i]', '[aria-label^="Send message" i]'])
# PROVEN on real Teams (stage.ts) and CHECKED: the editor, and the Attach button. The compose root is derived from
# these two only: the nearest ancestor of the editor that also contains the Attach button.
EDITOR_SEL = '[data-tid="ckeditor"], [contenteditable=true][role=textbox]'
ATTACH_SEL = '[data-tid=sendMessageCommands-FilePicker],[data-tid=newMessageCommands-FilePicker]'
# One attachment chip, and its remove control inside it. Only ever queried INSIDE the compose root.
# PROVEN on real Teams (2026-11-06): each chip is a role=group (aria-label = file name) directly under
# data-tid=file-well; its remove control is button[aria-label="Remove attachment"]. The rest are UNVERIFIED guesses.
CHIP_PROVEN = '[data-tid="file-well"] > [role="group"]'
CHIP_SEL = CHIP_PROVEN + ',[data-tid*="attachment" i],[data-tid*="file-card" i],[data-tid*="file-chip" i]'
CHIP_REMOVE_SEL = 'button[aria-label^="Remove" i],button[aria-label^="Dismiss" i],[data-tid*="remove" i]'
# Your own sent messages, the "More options" control on one, and the Edit menu item (UNVERIFIED:
# Teams web renders own messages with Fluent's ChatMyMessage; the toolbar shows on hover).
OWN_MSG_SEL = '.fui-ChatMyMessage,[data-tid="chat-pane-message"][data-is-mine="true"]'
OWN_BODY_SEL = '[data-tid="chat-pane-message"],.fui-ChatMyMessage__body'
# CHECKED in the Teams desktop app (2026-11-11): Edit is a button on the floating hover toolbar
# ("Reactions and message actions", outside the message's chat-pane-item), next to its "More options";
# the More menu has no Edit. Each message also holds a hidden 4px "More message options" button.
EDIT_BTN_SEL = '[data-tid="message-actions-edit"],button[aria-label="Edit" i]'
MORE_SEL = 'button[aria-label="More options" i],button[aria-label^="More message options" i]'
# Saving an edit. Refused as a click target in --edit-sent mode (UNVERIFIED names).
SAVE_GUARD = '[aria-label="Done" i],[aria-label^="Save" i],[aria-label="Update" i],[data-tid*="save" i]'
# The search box. CHECKED: data-tid AUTOSUGGEST_INPUT, a combobox input.
SEARCH_SEL = ('[data-tid="AUTOSUGGEST_INPUT"],input[role=combobox][aria-label*="Search" i],[role=combobox][aria-label*="Search" i],'
              'input[type=search]')
# The new-chat control. CHECKED: Teams now labels it "New message" (no data-tid); "New chat" matched nothing.
NEW_CHAT_SEL = '[aria-label="New message" i],[aria-label="New chat" i],[data-tid*="new-chat" i]'
# The To / people picker input in a new chat, and a chosen recipient chip in it (UNVERIFIED).
# Verified live 2026-11-10: the To input is itself input[data-tid=people-picker-search].
PEOPLE_PICKER_SEL = ('input[data-tid="people-picker-search"],input[aria-label^="To" i],'
                     '[data-tid*="people-picker" i] input,[role=combobox][aria-label*="To:" i]')
# The To label's aria-label lists the picked people: "To: Ann Lee, Bo Ray" (verified live 2026-11-10).
TO_LABEL_SEL = '[data-tid="people-picker-label-id"]'
TO_CHIP_SEL = '[data-tid*="people-picker" i] [data-tid*="pill" i],[data-tid*="people-picker" i] [data-tid*="persona" i]'
# Suggestion rows of any open suggestion list (people picker and @mention). Verified live 2026-11-10: the
# @ list is ul#autocomplete-picker-list[role=listbox] > li[role=option], first line the name.
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
        self.edit_mode = False
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self.session: str | None = None
        self.opened_names: list[str] = []  # rail/search row names open_chat accepted by thread id
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

    async def type_char(self, ch: str):
        """One printable character as a real keyDown/keyUp pair, so the page's key listeners fire."""
        plain = ch.isascii() and ch.isalnum()
        p = {"key": ch, "code": (("Key" if ch.isalpha() else "Digit") + ch.upper()) if plain else "",
             "windowsVirtualKeyCode": ord(ch.upper()) if plain else 0}
        await self.s("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch, **p})
        await self.s("Input.dispatchKeyEvent", {"type": "keyUp", **p})

    async def key(self, k: str, code: str, vk: int, mod: int = 0):
        for typ in ("keyDown", "keyUp"):
            await self.s("Input.dispatchKeyEvent", {"type": typ, "key": k, "code": code,
                                                     "windowsVirtualKeyCode": vk, "modifiers": mod})

    async def click(self, x: float, y: float):
        """A real mouse press (Teams ignores untrusted JS .click()), after a hit test that refuses send buttons."""
        if await self.ev(op("hit_test", "return !!document.elementFromPoint(A.x,A.y)?.closest(A.sel)",
                            x=x, y=y, sel=SEND_GUARD)):
            raise StagerError("refusing to click a send button: this stager never sends", 1)
        if self.edit_mode and await self.ev(op("hit_test", "return !!document.elementFromPoint(A.x,A.y)?.closest(A.sel)",
                                               x=x, y=y, sel=SAVE_GUARD)):
            raise StagerError("refusing to click a save control: edit-sent never saves the edit", 1)
        for typ in ("mousePressed", "mouseReleased"):
            await self.s("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left",
                                                       "clickCount": 1})


# ---- ops ---------------------------------------------------------------------------------

JS_RAIL = """const items=[...document.querySelectorAll('[role=treeitem]')];
const tid=e=>e.getAttribute('data-fui-tree-item-value')||e.querySelector('[data-fui-tree-item-value]')?.getAttribute('data-fui-tree-item-value');
const byId=A.id?items.filter(e=>tid(e)===A.id):[];
const m=byId.length?byId:items.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);
if(m.length===1)m[0].scrollIntoView({block:'center'});
return {total:items.length,matches:m.map(t=>{const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,name:t.innerText.trim().split('\\n')[0].trim()}})}"""
JS_SEARCH_BOX = ("const b=document.querySelector(" + json.dumps(SEARCH_SEL) + ");"
                 "if(!b)return null;const r=b.getBoundingClientRect();if(!r.width)return null;"
                 "return {x:r.x+r.width/2,y:r.y+r.height/2}")
# CHECKED in the Teams desktop app (2026-11-11): the compose box's Send button carries the open chat's
# thread id (data-track-thread-id="19:...@thread.v2", "48:notes" for the self-chat); rail rows carry theirs
# in data-fui-tree-item-value, and search hits in their data-tid.
JS_OPEN_THREAD_ID = ("const r=ROOT();const b=(r&&r.querySelector('[data-track-thread-id]'))||"
                     "document.querySelector('[data-tid$=\"-send\"][data-track-thread-id]');"
                     "return b?b.getAttribute('data-track-thread-id'):null")
# Every distinct thread id on the page: when the compose box has none of its own, the page-wide fallback
# counts only if it is the one id there (a calling side panel once carried another chat's id).
JS_PAGE_THREAD_IDS = "return [...new Set([...document.querySelectorAll('[data-track-thread-id]')].map(e=>e.getAttribute('data-track-thread-id')))]"
JS_SEARCH_SELECT = ("const b=document.querySelector(A.sel);if(!b)return false;b.focus();"
                    "if(b.select)b.select();else{const r=document.createRange();r.selectNodeContents(b);"
                    "const s=getSelection();s.removeAllRanges();s.addRange(r)}return true")
JS_FOCUS_IS = "const b=document.querySelector(A.sel);const f=document.activeElement;return !!b&&!!f&&(f===b||b.contains(f))"
JS_SEARCH_FIND = """const o=[...document.querySelectorAll('[role=option]')];
const byId=A.id?o.filter(e=>(e.getAttribute('data-tid')||'').includes(A.id)):[];
const m=byId.length?byId:o.filter(e=>e.innerText.trim().split('\\n')[0].trim()===A.name);
return {total:o.length,matches:m.map(t=>{const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,name:t.innerText.trim().split('\\n')[0].trim()}})}"""
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
         "const nm=t=>(t||'').replace(/\\s+/g,' ').trim();"
         "const m=o.filter(e=>nm(e.innerText.trim().split('\\n')[0])===nm(A.name));"
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
# CHECKED in the Teams desktop app (2026-11-11): "New message" exists only in the Chat view; on Calendar
# (or any other app) the app bar's Chat button (aria-label "Chat", maybe ", N unread") switches to it.
JS_CHAT_APP_BTN = _XY + ("const b=[...document.querySelectorAll('button,[role=button]')]"
                         ".find(b=>/^chat(,|$)/i.test((b.getAttribute('aria-label')||'').trim()));return b?xy(b):null")
JS_PEOPLE_PICKER = _XY + f"const b=document.querySelector({json.dumps(PEOPLE_PICKER_SEL)});return b?xy(b):null"
JS_PEOPLE_FIND = _XY + _OPTS
JS_TO_CHIP = (f"const l=document.querySelector({json.dumps(TO_LABEL_SEL)})?.getAttribute('aria-label')||'';"
              "if(l.replace(/^To:\\s*/i,'').split(',').map(x=>x.trim()).includes(A.name))return true;"
              f"return [...document.querySelectorAll({json.dumps(TO_CHIP_SEL)})]"
              ".some(e=>e.innerText.trim().split('\\n')[0].trim()===A.name)")
JS_MENTION_COUNT = f"return (ED()?.querySelectorAll({json.dumps(MENTION_SEL)}).length)||0"
JS_MENTION_FIND = _XY + _OPTS
_OWN = (f"const own=()=>[...document.querySelectorAll({json.dumps(OWN_MSG_SEL)})];"
        f"const body=m=>m.querySelector({json.dumps(OWN_BODY_SEL)})||m;"
        "const norm=t=>(t||'').replace(/\\s+/g,' ').trim();")
# Edit boxes: editors that are not the compose box (no Attach button around them).
# The app keeps the message list and the compose box under one ancestor that holds the Attach button,
# so an editor inside the message list is an edit box outright; elsewhere the walk-up rule decides.
_EDITS = (f"const EDS=()=>[...document.querySelectorAll({json.dumps(EDITOR_SEL)})].filter(e=>{{"
          "if(e.closest('[data-tid=\"chat-pane-item\"],[data-tid=\"message-pane-body\"]'))return true;let n=e.parentElement;"
          f"while(n&&n!==document.documentElement){{if(n.querySelector({json.dumps(ATTACH_SEL)}))return false;"
          "n=n.parentElement}return true});const EE=()=>{const l=EDS();return l.length===1?l[0]:null};")
JS_OWN_MSGS = _OWN + "return own().map(m=>({text:body(m).innerText}))"
# The message's own Edit / More controls: visible ones only (the hidden 4px button opens nothing),
# inside the message's item, else on a toolbar anchored at the item's top edge (the app's floating
# hover toolbar sits outside the item and follows the pointer, so geometry ties it to this message).
JS_MSG_ACTIONS = (_XY + _OWN + "const m=own().find(m=>norm(body(m).innerText).startsWith(A.key));if(!m)return null;"
                  "const s=m.closest('[data-tid=\"chat-pane-item\"]')||m.parentElement||m;"
                  "body(m).scrollIntoView({block:'center'});const S=s.getBoundingClientRect();"
                  "const vis=e=>{const r=e.getBoundingClientRect();return r.width>=12&&r.height>=12};"
                  "const anchored=e=>{const r=e.getBoundingClientRect();const cy=r.top+r.height/2;"
                  "return !!e.closest('[role=toolbar]')&&Math.abs(cy-S.top)<=40&&r.left<S.right+80};"
                  "const pick=sel=>[...s.querySelectorAll(sel)].find(vis)||[...document.querySelectorAll(sel)]"
                  ".find(e=>!s.contains(e)&&vis(e)&&anchored(e))||null;"
                  f"const ed=pick({json.dumps(EDIT_BTN_SEL)}),mo=pick({json.dumps(MORE_SEL)});"
                  "return {at:xy(body(m)),edit:ed?xy(ed):null,more:mo?xy(mo):null}")
# Messages after ours from someone else (#209): chat items after the one holding our message, that hold a
# message and none of ours. Best effort: 0 when the chat list has no items (the outbox checked ingest too).
JS_NEWER_OTHERS = (_OWN + "const m=own().find(m=>norm(body(m).innerText).startsWith(A.key));if(!m)return -1;"
                   "const items=[...document.querySelectorAll('[data-tid=\"chat-pane-item\"]')];"
                   "const i=items.findIndex(it=>it.contains(m));if(i<0)return 0;"
                   f"return items.slice(i+1).filter(it=>!it.querySelector({json.dumps(OWN_MSG_SEL)})"
                   "&&it.querySelector('[data-tid=\"chat-pane-message\"]')).length")
JS_MENU_ITEM = (_XY + "const m=[...document.querySelectorAll('[role=menuitem]')]"
                ".find(e=>e.innerText.trim().split('\\n')[0].trim()===A.label);return m?xy(m):null")
JS_EDIT_READY = _EDITS + "const l=EDS();return {editors:l.length,text:l.length===1?l[0].innerText:''}"
JS_EDIT_LEN = _EDITS + "const e=EE();return e?e.innerText.trim().length:-1"
JS_EDIT_SELECT = (_EDITS + "const e=EE();e.focus();const r=document.createRange();r.selectNodeContents(e);"
                  "const s=getSelection();s.removeAllRanges();s.addRange(r);return null")
JS_EDIT_PASTE = (_EDITS + "const e=EE();e.focus();const dt=new DataTransfer();dt.setData('text/html',A.html);"
                 "dt.setData('text/plain',A.plain);e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,"
                 "bubbles:true,cancelable:true}));return {len:e.innerText.trim().length,links:e.querySelectorAll('a').length}")


async def has_search_box(c: CDP, tid: str) -> bool:
    """Attach to a candidate, look for the stager's search box, detach again."""
    sid = (await c.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
    try:
        r = await c.send("Runtime.evaluate", {"expression": op("search_box", JS_SEARCH_BOX), "awaitPromise": True,
                                              "returnByValue": True}, sid)
        return bool((r.get("result") or {}).get("value"))
    except StagerError:
        return False
    finally:
        try:
            await c.send("Target.detachFromTarget", {"sessionId": sid})
        except StagerError:
            pass


async def find_tab(c: CDP, want: str | None, chat: str | None = None, app: bool = False) -> str:
    """--tab wins. App mode (the Teams desktop app's own port): pick the chat window among the Teams pages,
    never open a tab. Browser mode: the first Teams page, else a new tab."""
    tabs = (await c.send("Target.getTargets")).get("targetInfos", [])
    if want:
        if not any(t["targetId"] == want for t in tabs):
            raise StagerError(f"no tab {want}")
        return want
    pages = [t for t in tabs if t.get("type") == "page" and IS_TEAMS.search(t.get("url", ""))]
    if app:
        chats = [t for t in pages if str(t.get("title", "")).startswith("Chat |")]
        named = [t for t in chats if chat and chat.lower() in str(t.get("title", "")).lower()]
        if named:
            return named[0]["targetId"]
        if chats:
            return chats[0]["targetId"]
        for t in pages:
            if await has_search_box(c, t["targetId"]):
                return t["targetId"]
        raise StagerError("Teams app is up but no chat window found", 1)
    if pages:
        return pages[0]["targetId"]
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


def title_matches(title: str, chat: str) -> bool:
    """The window title names exactly this chat. Teams titles read "Chat | <name or members> | Microsoft
    Teams"; a 1:1 must show only that person (a substring check once let "Chat | Ann Lee, Bo Chen" pass
    for Bo Chen and the draft landed in the group). A group's rail name ("Ann and Bo Chen") matches a
    title listing the same members ("Ann Lee, Bo Chen"): same count, each a full name or first name."""
    parts = [p.strip() for p in (title or "").split(" | ")]
    if len(parts) >= 2 and parts[-1].lower() == "microsoft teams":
        parts = parts[:-1]  # the web tab adds it; the desktop app's window title is just "Chat | <name>"
    mid = parts[1] if len(parts) >= 2 else parts[0]
    want_s = (chat or "").strip()
    if not want_s:
        return False
    if mid.lower() == want_s.lower():
        return True
    want = [w.strip().lower() for w in re.split(r",\s*|\s+and\s+", want_s) if w.strip()]
    members = [m.strip().lower() for m in mid.split(",") if m.strip()]
    if any(re.search(r"\s\+\d+$", m) for m in members):
        return False  # "Ann Lee, Bo Chen +1": more members than the title lists, so another chat
    if len(want) < 2 or len(members) != len(want):
        return False
    left = list(members)
    for w in want:
        hit = next((m for m in left if m == w or m.startswith(w + " ")), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


def title_names(title: str, chat: str) -> bool:
    """The window title could be CHAT's: title_matches, or a group title cut short with "+N" whose listed
    members all belong to it ("Ann Lee, Bo Chen +1" for "Ann, Bo Chen and Cy Dee"). With a thread id
    the id proves which chat it is; this only refuses a title that names ANOTHER chat ("Chat | Bo Chen"
    while the id says Ann Lee), which once meant the page's id came from somewhere else."""
    if title_matches(title, chat):
        return True
    parts = [p.strip() for p in (title or "").split(" | ")]
    if len(parts) >= 2 and parts[-1].lower() == "microsoft teams":
        parts = parts[:-1]
    raw = (parts[1] if len(parts) >= 2 else parts[0]).strip()
    cut = re.search(r"\s\+\d+$", raw)  # only a title cut short with "+N" may list a subset of the members
    mid = re.sub(r"\s\+\d+$", "", raw).strip().lower()
    want_s = (chat or "").strip().lower()
    if not mid or not want_s:
        return False
    if mid == want_s:
        return True
    if not cut:
        return False
    want = [w.strip() for w in re.split(r",\s*|\s+and\s+", want_s) if w.strip()]
    members = [m.strip() for m in mid.split(",") if m.strip()]
    if len(want) < 2 or len(members) > len(want):
        return False
    left = list(want)
    for m in members:
        hit = next((w for w in left if m == w or m.startswith(w + " ")), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


async def open_thread_id(c: CDP) -> str | None:
    """The open chat's thread id: the compose box's own, else the page's only one (None if several)."""
    got = await c.ev(op("open_thread_id", JS_OPEN_THREAD_ID))
    if got:
        ids = await c.ev(op("page_thread_ids", JS_PAGE_THREAD_IDS))
        if isinstance(ids, list) and len(ids) > 1:
            root_has = await c.ev(op("root_thread_id", "const r=ROOT();const b=r&&r.querySelector('[data-track-thread-id]');"
                                     "return b?b.getAttribute('data-track-thread-id'):null"))
            return root_has  # several ids on the page: only the compose box's own counts
    return got


def names_ok(title: str, names: list[str]) -> bool:
    return any(n and title_names(title, n) for n in names)


async def check_open(c: CDP, chat: str, thread_id: str | None) -> str:
    """Right before the compose box is cleared or typed into: the chat that opened is still the one.
    Returns the title. Raises naming what is open instead."""
    title = await c.ev(op("title", "return document.title"))
    got = await open_thread_id(c) if thread_id else None
    if thread_id and got:
        if got != thread_id:
            raise StagerError(f"the open chat's thread id is {got}, not {thread_id} ({chat}); stopping before "
                              "touching the compose box")
        if not names_ok(title, [chat] + list(getattr(c, "opened_names", []))):
            raise StagerError(f'the window title is "{title}", not {chat}, though the thread id matches; '
                              "stopping before touching the compose box")
    elif not any(title_matches(title, n) for n in [chat] + list(getattr(c, "opened_names", []))):
        raise StagerError(f'title is "{title}", not {chat}; stopping before touching the compose box')
    return title


def search_queries(chat: str) -> list[str]:
    """What to type into Teams search for CHAT: the name itself, then for a group's rail name
    ("Ann and Bo Chen") each member, full names first. Teams search lists an unnamed group chat
    under its members' names, not under the rail name, so a group off the rail is found that way."""
    out = [chat]
    members = [w.strip() for w in re.split(r",\s*|\s+and\s+", chat) if w.strip()]
    if len(members) >= 2:
        for m in sorted(members, key=lambda w: (" " not in w, -len(w))):
            if m not in out:
                out.append(m)
    return out


async def search_type(c: CDP, text: str):
    """Replace whatever the search box holds with TEXT (Teams keeps the last query there, so a bare
    insertText once searched for "<old query><name>")."""
    if not await c.ev(op("search_select", JS_SEARCH_SELECT, sel=SEARCH_SEL)):
        raise StagerError("couldn't focus the search box")
    await type_into(c, SEARCH_SEL, text, "search box")


async def search_clear(c: CDP):
    """Leave the search box empty after a failed search, so the user's next search starts clean."""
    try:
        if await c.ev(op("search_select", JS_SEARCH_SELECT, sel=SEARCH_SEL)):
            await c.key("Backspace", "Backspace", 8)
        await c.key("Escape", "Escape", 27)
    except StagerError:
        pass


async def open_chat(c: CDP, chat: str, thread_id: str | None = None) -> str:
    """Open CHAT and prove it is the one: with a thread id, the open chat's id must equal it AND the window
    title must not name another chat (it may name CHAT, the row the id found, or a "+1" cut of either; the id
    can switch before the title does, so it waits for both); without one, the title must name exactly this
    chat. The title returned is read once both agree, so the result's "chat" is where the text went."""
    matches: list[dict] = []
    rendered = 0  # polls since the rail first showed any item
    for _ in range(45):  # wait for the rail, then give the exact row up to 15 polls (~15s) to appear
        r = await c.ev(op("rail_find", JS_RAIL, name=chat, id=thread_id))
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
        queries = search_queries(chat)
        for q in queries:
            await search_type(c, q)
            last = -1
            for _ in range(20):
                await nap(0.5)
                r = await c.ev(op("search_find", JS_SEARCH_FIND, name=chat, id=thread_id))
                matches = r["matches"]
                if matches and len(matches) == last:
                    break
                last = len(matches)
            if matches:
                break
        if not matches:
            await search_clear(c)
            tried = ", ".join(f'"{q}"' for q in queries)
            raise StagerError(f"no chat named {chat} (rail or search; searched {tried})")
    if len(matches) > 1:
        raise StagerError(f"ambiguous: {len(matches)} chats named {chat}")
    title = ""
    for attempt in range(2):  # the rail can still be scrolling or re-rendering: a click once landed on the next chat
        if source == "left rail":
            matches = await settled_rail_xy(c, chat, matches, thread_id)
        await c.click(matches[0]["x"], matches[0]["y"])
        id_ok = False
        # with a thread id the row it found may be named otherwise (the group was renamed since the draft):
        # the title may name that row too, but never a chat that is neither
        c.opened_names = [n for n in [matches[0].get("name")] if n and thread_id]
        for _ in range(20):
            title = await c.ev(op("title", "return document.title"))
            got = await open_thread_id(c) if thread_id else None
            if thread_id and got:
                # the id can switch before the title does: wait for both, so the title returned (and reported
                # as the result's "chat") is the chat's own, and a title naming another chat is not let by
                if got == thread_id:
                    id_ok = True
                    if names_ok(title, [chat] + c.opened_names):
                        return title
            elif title_matches(title, chat):  # no id readable (or none given): the exact title, as without one
                return title
            await nap(0.5)
        if source != "left rail" or id_ok:
            break
    if thread_id and id_ok:
        raise StagerError(f'the open chat\'s thread id is {thread_id}, but the window title is "{title}", not '
                          f'{chat}; stopping before touching the compose box ({source})')
    if thread_id and got and got != thread_id:
        raise StagerError(f'the open chat\'s thread id is {got}, not {thread_id} ({chat}); stopping ({source})')
    raise StagerError(f'title is "{title}", not {chat}; stopping ({source})')


async def settled_rail_xy(c: CDP, chat: str, matches: list[dict], thread_id: str | None = None) -> list[dict]:
    """rail_find scrolls the row into view; read its position again until two reads agree (max ~2s)."""
    for _ in range(8):
        await nap(0.25)
        r = await c.ev(op("rail_find", JS_RAIL, name=chat, id=thread_id))
        now = r["matches"]
        if len(now) != 1:
            raise StagerError(f"chat {chat} left the rail while scrolling to it ({len(now)} rows match)")
        if abs(now[0]["x"] - matches[0]["x"]) < 1 and abs(now[0]["y"] - matches[0]["y"]) < 1:
            return now
        matches = now
    return matches


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
    """New message to NAME, or to every person in "First Last, Other Person" (a new group chat)."""
    people = [p.strip() for p in name.split(",") if p.strip()]
    btn = await c.ev(op("new_chat_btn", JS_NEW_CHAT_BTN))
    if not btn:  # Teams is on another view (Calendar, Activity...): switch to Chat first
        app_btn = await c.ev(op("chat_app_btn", JS_CHAT_APP_BTN))
        if app_btn:
            await c.click(app_btn["x"], app_btn["y"])
            for _ in range(20):
                await nap(0.5)
                btn = await c.ev(op("new_chat_btn", JS_NEW_CHAT_BTN))
                if btn:
                    break
    if not btn:
        raise StagerError("no New chat control found (signed in?)")
    await c.click(btn["x"], btn["y"])
    for person in people:
        box = None
        for _ in range(20):
            box = await c.ev(op("people_picker", JS_PEOPLE_PICKER))
            if box:
                break
            await nap(0.5)
        if not box:
            raise StagerError("no To field in the new chat")
        await c.click(box["x"], box["y"])
        await type_into(c, PEOPLE_PICKER_SEL, person, "To field")
        m = await pick_exact(c, "people_find", JS_PEOPLE_FIND, person, f"no person named {person}", "people")
        await c.click(m["x"], m["y"])
        for _ in range(20):
            if await c.ev(op("to_chip", JS_TO_CHIP, name=person)):
                break
            await nap(0.5)
        else:
            raise StagerError(f"the To field doesn't show {person}; stopping")
    return name


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


async def settled_chip_names(c: CDP, tries: int = 12, gap: float = 0.25) -> list[str] | None:
    """chip_names, but a missing root is re-read for ~3s first: right after a clear or a paste Teams
    re-renders the compose area and the editor briefly has no ancestor holding the Attach button."""
    names = await chip_names(c)
    for _ in range(tries):
        if names is not None:
            return names
        await nap(gap)
        names = await chip_names(c)
    return names


async def remove_chips(c: CDP, have_pages: bool):
    """Remove every attachment chip inside the compose root (and nothing outside it). No root: skip with a
    warning when no pages are going in, else fail (the check afterwards would be meaningless)."""
    names = await settled_chip_names(c)
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
            if names is not None and len(names) < before:
                break
        if names is None:
            names = await settled_chip_names(c)
        if names is None or len(names) >= before:
            raise StagerError("clicking an attachment's remove control didn't remove it")
    if names:
        raise StagerError("couldn't remove the old attachments")


async def mention_list_open(c: CDP, name: str, secs: float = 2.5) -> bool:
    """True once the @ suggestion list shows any option, within secs."""
    for _ in range(int(secs / 0.25)):
        await nap(0.25)
        if (await c.ev(op("mention_find", JS_MENTION_FIND, name=name)))["total"] > 0:
            return True
    return False


def mention_queries(name: str) -> list[str]:
    """What to type after @ for NAME: the full name, then (when it has a one-letter token, like "Ana B")
    the name without those tokens, since the picker finds nothing for a one-letter word. The pick is
    always the suggestion named NAME exactly."""
    full = " ".join(name.split())
    short = " ".join(t for t in full.split(" ") if len(t.rstrip(".")) > 1)
    return [full] + ([short] if short and short != full else [])


async def type_mention_query(c: CDP, query: str, name: str) -> list[dict]:
    """Type @QUERY at the caret and return the exact matches for NAME once the list settles ([] if none)."""
    # The picker only filters on real key events (#183): insert all but the last letter, then key it.
    await c.s("Input.insertText", {"text": "@" + query[:-1]})
    await c.type_char(query[-1])
    # Right after a clear Teams often ignores that first key and no list opens (seen live 6 of 6):
    # Backspace the last letter and key it again, which opens it.
    for _ in range(2):
        if await mention_list_open(c, name):
            break
        await c.key("Backspace", "Backspace", 8)
        await nap(0.3)
        await c.type_char(query[-1])
    try:
        return [await pick_exact(c, "mention_find", JS_MENTION_FIND, name, "", "mention suggestions")]
    except StagerError as exc:
        if str(exc).startswith("ambiguous"):
            raise
        return []


async def erase_typed(c: CDP, n: int):
    """Take back n typed characters (an @query that found no suggestion) and close the list."""
    for typ in ("keyDown", "keyUp"):
        await c.s("Input.dispatchKeyEvent", {"type": typ, "key": "Escape", "code": "Escape",
                                              "windowsVirtualKeyCode": 27})
    for _ in range(n):
        await c.key("Backspace", "Backspace", 8)


async def insert_mentions(c: CDP, names: list[str]):
    for name in names:
        before = await c.ev(op("mention_count", JS_MENTION_COUNT))
        m = None
        for q in mention_queries(name):
            await c.ev(op("caret_end", JS_CARET))
            try:
                hits = await type_mention_query(c, q, name)
            except StagerError:
                await erase_typed(c, len(q) + 1)
                raise
            if hits:
                m = hits[0]
                break
            await erase_typed(c, len(q) + 1)  # never leave "@Name" behind as plain text
        if m is None:
            raise StagerError(f"no mention suggestion for {name}")
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
    """Drop a literal @NAME (and one following blank) from the draft text: the real mention replaces it.
    Fenced code blocks and `inline code` are left alone."""
    out, fenced = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        parts = re.split(r"(`[^`]*`)", line)  # odd indexes are inline code spans
        for i in range(0, len(parts), 2):
            for n in names:
                parts[i] = re.sub(r"(?<!\w)@" + re.escape(n) + r"(?!\w)[ \t]?", "", parts[i])
        out.append("".join(parts))
    return "\n".join(out)


async def stage(a) -> dict:
    app = resolve_port(a)
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
            tid = await find_tab(c, a.tab, a.chat, app)
            c.session = (await c.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
            step("bringing the tab to the front")  # a background tab (document.hidden) ignores rail clicks
            await bring_to_front(c, tid)
            step("opening the chat")
            title = await (new_chat(c, a.chat) if a.new_chat else open_chat(c, a.chat, a.thread_id))
            if a.edit_sent:
                return await edit_sent(c, a, title, html, plain)

            step("waiting for the compose box")
            for _ in range(20):
                if await c.ev(op("editor_ready", "return !!ED()")):
                    break
                await nap(0.5)
            step("clearing the compose box")
            if not a.new_chat:
                title = await check_open(c, a.chat, a.thread_id)
            existing = await c.ev(op("compose_len", JS_LEN))
            if existing > 0 and not a.replace:
                md = getattr(a, "md", None) or ""
                did = (os.path.basename(os.path.dirname(md)) if os.path.basename(md) == "draft.md" else "") or "<id>"
                raise StagerError(f"compose box already holds {existing} chars; rerun with "
                                  f"`whosaid drafts stage {did} --replace` to overwrite")
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
                except StagerError as exc:
                    left = -1
                    try:
                        await clear_compose(c)  # don't leave a half-typed "@NAME" behind
                        left = await c.ev(op("compose_len", JS_LEN))
                    except StagerError:
                        pass
                    if left != 0:
                        raise StagerError(f"{exc}; the compose box may still hold the typed @ text, clear it in "
                                          "Teams before staging again") from exc
                    raise
                await c.ev(op("caret_end", JS_CARET))

            step("pasting the draft")
            if not a.new_chat:
                title = await check_open(c, a.chat, a.thread_id)
            pasted = await c.ev(op("paste", JS_PASTE, html=html, plain=plain))
            if not pasted or not pasted.get("len"):
                raise StagerError("paste left the compose box empty")

            if a.attach:
                step("attaching pages")
                try:
                    await attach(c, a.attach, app)
                except StagerError as e:  # the paste already happened: say the chat is half-staged
                    raise StagerError(f"{e} (the draft text is already in the compose box, without the "
                                      f"attachment; rerun the stage to replace it)") from None

            step("focusing the tab")
            await bring_to_front(c, tid)
            await c.ev(op("caret_end", JS_CARET))
            fin = await c.ev(op("final", JS_FINAL))
            measured = len(await chip_names(c) or [])  # chips actually in the compose box, not what was asked
            if not a.new_chat:
                title = await c.ev(op("title", "return document.title")) or title  # "chat": where the text is now
            out = {"sent": False, "chat": title, "chars": fin["len"], "links": fin["links"],
                   "attachments": measured, "mentions": len(a.mention)}
            if a.thread_id:
                out["thread_id"] = a.thread_id
            return out
        finally:
            c.reader.cancel()


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


async def edit_sent(c: CDP, a, title: str, html: str, plain: str) -> dict:
    """Load the new text into Teams' Edit box on our matching sent message. Never saves."""
    from drafts import same_message  # the outbox's own "this chat message is that draft" rule
    c.edit_mode = True
    want = open(a.match_file, encoding="utf-8").read()
    step("finding your message")
    hits: list[dict] = []
    for _ in range(20):
        msgs = await c.ev(op("own_msgs", JS_OWN_MSGS)) or []
        hits = [m for m in msgs if same_message(want, m.get("text") or "")]
        if hits:
            break
        await nap(0.5)
    if not hits:
        raise StagerError(f"no message of yours in {a.chat} matches the sent text (is it loaded in the chat?)")
    if len(hits) > 1:
        raise StagerError(f"ambiguous: {len(hits)} of your messages in {a.chat} match the sent text")
    key = _norm_text(hits[0]["text"])[:60]
    newer = await c.ev(op("newer_others", JS_NEWER_OTHERS, key=key)) or 0
    if newer > 0 and not a.allow_newer:
        raise StagerError(f"{newer} newer message(s) from others after yours in {a.chat}; editing an answered "
                          "message can confuse the thread (--allow-newer to edit anyway)")
    step("opening Edit on it")
    got: dict | None = None
    for _ in range(10):
        got = await c.ev(op("msg_actions", JS_MSG_ACTIONS, key=key))
        if not got or not got.get("at"):
            raise StagerError("that message left the chat view; rerun")
        # hover first, then read: the one floating toolbar may still show for the last message hovered
        await c.s("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": got["at"]["x"], "y": got["at"]["y"]})
        await nap(0.3)
        got = await c.ev(op("msg_actions", JS_MSG_ACTIONS, key=key))
        if got and (got.get("edit") or got.get("more")):
            break
    if not got or not got.get("at"):
        raise StagerError("that message left the chat view; rerun")
    if got.get("edit"):  # the desktop app: an Edit button on the hover toolbar
        await c.click(got["edit"]["x"], got["edit"]["y"])
    else:
        more = got.get("more")
        if not more:
            raise StagerError("no Edit or More options control on that message")
        await c.click(more["x"], more["y"])
        item = None
        for _ in range(10):
            await nap(0.3)
            item = await c.ev(op("menu_item", JS_MENU_ITEM, label="Edit"))
            if item:
                break
        if not item:
            raise StagerError("no Edit item in that message's menu (only your own messages can be edited)")
        await c.click(item["x"], item["y"])
    step("waiting for the edit box")
    ready = {"editors": 0, "text": ""}
    for _ in range(20):
        ready = await c.ev(op("edit_ready", JS_EDIT_READY))
        if ready["editors"] == 1:
            break
        await nap(0.3)
    if ready["editors"] != 1:
        raise StagerError(f"expected one edit box after Edit, found {ready['editors']}")
    if not _norm_text(ready["text"]).startswith(key[:40]):
        raise StagerError("the edit box doesn't hold the message we picked; stopping (press Esc in Teams)")
    step("replacing the text")
    for _ in range(3):
        if await c.ev(op("edit_len", JS_EDIT_LEN)) <= 0:
            break
        await c.ev(op("edit_select", JS_EDIT_SELECT))
        await c.key("Backspace", "Backspace", 8)
        await nap(0.4)
    if await c.ev(op("edit_len", JS_EDIT_LEN)) != 0:
        raise StagerError("couldn't clear the edit box; press Esc in Teams to cancel the edit")
    res = await c.ev(op("edit_paste", JS_EDIT_PASTE, html=html, plain=plain))
    if not res or not res.get("len"):
        raise StagerError("paste left the edit box empty; press Esc in Teams to cancel the edit")
    return {"sent": False, "saved": False, "edit": True, "chat": title, "chars": res["len"], "links": res["links"]}


async def bring_to_front(c: CDP, tid: str):
    await c.send("Target.activateTarget", {"targetId": tid})
    try:
        await c.s("Page.bringToFront")
    except StagerError:
        pass


async def stage_with_deadline(a) -> dict:
    """stage() under one global cap; a hang becomes a clean error naming the step it was in."""
    step("starting")
    try:
        return await asyncio.wait_for(stage(a), deadline())
    except (asyncio.TimeoutError, TimeoutError):
        raise StagerError(f"timed out during {STEP[0]} (global limit {deadline():g}s)")


TEAMS_CONTAINER = os.path.expanduser("~/Library/Containers/com.microsoft.teams2/Data")


def app_stage_dir() -> str | None:
    """Where app mode copies pages before uploading them. The Teams desktop app is sandboxed: a path handed
    over CDP outside its container can't be read, so Teams shows the chip and drops it (proven 2026-11-11;
    a copy under the container's tmp uploads and stays). None when there is no container."""
    d = os.environ.get("WHOSAID_TEAMS_STAGE_DIR")
    if d:
        return d
    return os.path.join(TEAMS_CONTAINER, "tmp", "whosaid-stage") if os.path.isdir(TEAMS_CONTAINER) else None


async def attach(c: CDP, files: list[str], app: bool = False):
    """Upload the files, then prove it: the chips INSIDE the compose root grew by len(files) and each file
    name shows there, and stays. In app mode the upload is a same-named copy inside the Teams sandbox
    (copies older than an hour are pruned on the next run)."""
    root = app_stage_dir() if app else None
    if not root:
        return await _attach(c, files)
    import shutil
    import tempfile
    import time
    os.makedirs(root, exist_ok=True)
    for old in os.listdir(root):  # earlier runs' copies: kept an hour so a slow upload can finish reading
        p = os.path.join(root, old)
        if old.startswith("stage-") and time.time() - os.path.getmtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)
    run = tempfile.mkdtemp(prefix="stage-", dir=root)
    copies = []
    for f in files:
        dst = os.path.join(run, os.path.basename(f))
        shutil.copyfile(f, dst)
        copies.append(dst)
    return await _attach(c, copies)


async def _attach(c: CDP, files: list[str]):
    """The file-chooser intercept is always switched off again, even on failure."""
    before = await settled_chip_names(c)
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
            if len(names) >= len(before) + len(files) and not unmatched(files, names):
                await chips_stay(c, files, len(before) + len(files))
                return
            await nap(0.5)
        missing = unmatched(files, names)
        raise StagerError(f"attachment chip for {(missing or files)[0]} never showed up in the compose box "
                          f"(chips {len(before)} -> {len(names)}, wanted +{len(files)})")
    finally:
        try:
            await asyncio.wait_for(c.s("Page.setInterceptFileChooserDialog", {"enabled": False}), 3)
        except Exception:
            pass  # the connection may be gone; nothing more to do


def chip_shows(name: str, text: str) -> bool:
    """The chip text names this file. OneDrive renames an upload whose name is already taken in the chat
    files folder ("page.html" -> "page 3.html", seen 2026-11-11 on a restage), and the chip then shows the
    new name, so a number before the extension still counts as this file."""
    stem, ext = os.path.splitext(name)
    return re.search(re.escape(stem) + r"(?: \d+)?" + re.escape(ext), text) is not None


def unmatched(files: list[str], names: list[str]) -> list[str]:
    """The files no chip names, each chip counted for one file only (exact names claimed first, so
    "a.html" and "a 2.html" in one stage can't share a chip)."""
    left = list(names)
    missing = []
    order = sorted(files, key=lambda f: not any(os.path.basename(f) in n for n in names))
    for f in order:
        base = os.path.basename(f)
        hit = next((n for n in left if base in n), None) or next((n for n in left if chip_shows(base, n)), None)
        if hit is None:
            missing.append(f)
        else:
            left.remove(hit)
    return [f for f in files if f in missing]


async def chips_stay(c: CDP, files: list[str], want: int, reads: int = 6, gap: float = 0.5):
    """A chip seen once is not an attachment: the Teams desktop app shows the chip for a moment and then
    removes it when the upload doesn't land (seen 2026-11-11: gone within 0.3s, no message). Every read
    over ~3s has to still hold WANT chips and one naming each file."""
    for _ in range(reads):
        await nap(gap)
        names = await chip_names(c)
        if names is None:
            continue  # the compose root re-rendering, not an answer
        gone = unmatched(files, names)
        if gone or len(names) < want:
            name = os.path.basename((gone or files)[0])
            raise StagerError(f"Teams showed the attachment chip for {name}, then removed it "
                              "again: the upload didn't land (attach the page by hand in Teams)")


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
    p.add_argument("--thread-id", help="the chat's Teams thread id; the open chat must carry it")
    p.add_argument("--cdp-port", type=int, default=None)
    p.add_argument("--bypass-outbox", action="store_true")
    p.add_argument("--edit-sent", action="store_true", help="edit your sent message matching --match-file")
    p.add_argument("--match-file", help="text of the sent message to edit (--edit-sent)")
    p.add_argument("--allow-newer", action="store_true",
                   help="--edit-sent: edit even when others posted after the message")
    a = p.parse_args(argv)  # argparse exits 2 on usage errors
    a.app_port = None
    if a.cdp_port is None and os.environ.get("WHOSAID_CDP_PORT"):  # only read the env var when the flag is absent
        raw = os.environ["WHOSAID_CDP_PORT"]
        try:
            a.cdp_port = int(raw)
        except ValueError:
            raise StagerError(f"WHOSAID_CDP_PORT is not a port number: {raw!r}", 2)
    if a.cdp_port is None:  # neither set: the app port is probed in resolve_port()
        raw = os.environ.get("WHOSAID_TEAMS_CDP_PORT") or "9333"
        try:
            a.app_port = int(raw)
        except ValueError:
            raise StagerError(f"WHOSAID_TEAMS_CDP_PORT is not a port number: {raw!r}", 2)
    return a


def probe_browser(port: int, timeout: float = 1.0) -> str | None:
    """The "Browser" string of /json/version on port, or None when nothing answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return str(json.load(r).get("Browser", ""))
    except Exception:
        return None


def resolve_port(a) -> bool:
    """Settle a.cdp_port; True when it is the Teams desktop app's port (app mode). An explicit port
    (flag or WHOSAID_CDP_PORT) is never probed and means browser mode."""
    if a.cdp_port is not None:
        return False
    b = probe_browser(a.app_port)
    if b is not None and b.startswith("Edg/"):
        a.cdp_port = a.app_port
        return True
    if b is not None:
        print(f"port {a.app_port} is held by {b or 'an unknown program'}, not the Teams app; using the browser port",
              file=sys.stderr)
    a.cdp_port = 9222
    return False


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
    if a.edit_sent:
        if not a.match_file or not os.path.isfile(a.match_file):
            raise StagerError("--edit-sent needs --match-file with the sent message's text", 2)
        if a.attach or a.mention or a.replace or a.new_chat:
            raise StagerError("--edit-sent can't be combined with --attach/--mention/--replace/--new-chat", 2)
    elif a.match_file:
        raise StagerError("--match-file only goes with --edit-sent", 2)
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
