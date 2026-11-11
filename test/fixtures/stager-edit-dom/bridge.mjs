// Runs chat.html in jsdom and answers one JSON request per stdin line (#209 stager DOM test):
//   {"eval": expr}              -> {"value": ...} or {"error": msg}   (the stager's Runtime.evaluate)
//   {"mouse": {type, x, y}}     -> {}  mouseMoved = mouseover, mousePressed/Released = down/up + click
//   {"key": {type, key, text}}  -> {}  keyDown -> keydown on the focused element, then its text or Backspace
//   {"insert": text}            -> {}  Input.insertText: the text at the selection
//   {"state": true}             -> {"value": window.__fx plus the edit box html}
// jsdom has no layout, innerText or clipboard: the shims below map them onto data-rect and textContent.
// Usage: NODE_PATH=<dir with jsdom> bun bridge.mjs chat.html [later]
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const [file, later] = process.argv.slice(2);
const html = readFileSync(file, "utf8");
const dom = new JSDOM(html, {
  url: "https://teams.cloud.microsoft/v2/" + (later ? "?later=1" : ""), runScripts: "dangerously",
  beforeParse(w) {
    const R = el => (el.getAttribute && el.getAttribute("data-rect") || "0,0,0,0").split(",").map(Number);
    w.Element.prototype.getBoundingClientRect = function () {
      const [x, y, width, height] = R(this);
      return { x, y, width, height, left: x, top: y, right: x + width, bottom: y + height };
    };
    w.Element.prototype.scrollIntoView = function () {};
    w.document.elementFromPoint = (px, py) => {
      let best = null, area = Infinity;
      for (const el of w.document.querySelectorAll("[data-rect]")) {
        const [x, y, ww, hh] = R(el);
        if (ww > 0 && hh > 0 && px >= x && px <= x + ww && py >= y && py <= y + hh && ww * hh <= area) {
          best = el; area = ww * hh;
        }
      }
      return best;
    };
    const BLOCK = /^(DIV|P|LI|UL|OL|BR|H\d)$/;
    const text = n => n.nodeType === 3 ? n.data : n.nodeName === "BR" ? "\n" :
      [...n.childNodes].map(text).join("") + (BLOCK.test(n.nodeName) && n.nextSibling ? "\n" : "");
    Object.defineProperty(w.HTMLElement.prototype, "innerText",
      { get() { return text(this); }, set(v) { this.textContent = v; } });
    w.DataTransfer = class { constructor() { this.d = {}; } setData(k, v) { this.d[k] = v; } getData(k) { return this.d[k] || ""; } };
    w.ClipboardEvent = class extends w.Event { constructor(t, i = {}) { super(t, i); this.clipboardData = i.clipboardData; } };
  },
});
const w = dom.window;
const out = o => process.stdout.write(JSON.stringify(o) + "\n");
// Text input at the selection, the way an editor takes it: replace a selection, else insert at the caret.
const editable = n => n && (n.nodeType === 1 ? n : n.parentElement)?.closest('[contenteditable="true"]');
const inputAt = n => { const e = editable(n); if (e) e.dispatchEvent(new w.Event("input", { bubbles: true })); };
function typeText(txt) {
  const s = w.getSelection();
  if (!s.rangeCount || !editable(s.getRangeAt(0).startContainer)) return;
  const r = s.getRangeAt(0);
  r.deleteContents();
  const node = w.document.createTextNode(txt);
  r.insertNode(node);
  r.setStartAfter(node); r.collapse(true);
  s.removeAllRanges(); s.addRange(r);
  node.parentNode.normalize();
  inputAt(r.startContainer);
}
function backspace() {
  const s = w.getSelection();
  if (!s.rangeCount) return;
  const r = s.getRangeAt(0), start = r.startContainer;
  if (!editable(start)) return;
  if (!r.collapsed) { r.deleteContents(); inputAt(start); return; }
  // the caret's text: the text node it is in, else the last text node before it
  let n = start.nodeType === 3 ? start : null, off = r.startOffset;
  if (!n) {
    const kids = [...start.childNodes].slice(0, off).reverse();
    n = kids.find(k => k.nodeType === 3 && k.data.length);
    off = n ? n.data.length : 0;
  }
  if (!n || off === 0) return;
  n.deleteData(off - 1, 1);
  r.setStart(n, off - 1); r.collapse(true);
  s.removeAllRanges(); s.addRange(r);
  inputAt(n);
}
const fire = (el, type, Ctor = w.MouseEvent, init = {}) => el && el.dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, ...init }));
for await (const line of createInterface({ input: process.stdin })) {
  if (!line.trim()) continue;
  const q = JSON.parse(line);
  try {
    if (q.eval !== undefined) {
      out({ value: await w.eval(q.eval) ?? null });
    } else if (q.mouse) {
      const el = w.document.elementFromPoint(q.mouse.x, q.mouse.y);
      if (q.mouse.type === "mouseMoved") fire(el, "mouseover");
      if (q.mouse.type === "mousePressed") { w.__fx.clicks.push([q.mouse.x, q.mouse.y]); fire(el, "mousedown"); }
      if (q.mouse.type === "mouseReleased") { fire(el, "mouseup"); fire(el, "click"); }
      out({});
    } else if (q.key) {
      // jsdom may not focus a contenteditable: fall back to where the selection is
      let t = w.document.activeElement;
      if (!t || t === w.document.body) {
        const s = w.getSelection();
        const n = s.rangeCount ? s.getRangeAt(0).startContainer : null;
        t = n ? (n.nodeType === 1 ? n : n.parentElement) : w.document.body;
      }
      if (q.key.type === "keyDown") {
        const ok = fire(t, "keydown", w.KeyboardEvent, { key: q.key.key });
        if (ok && q.key.text) typeText(q.key.text);
        else if (ok && q.key.key === "Backspace") backspace();
      }
      out({});
    } else if (q.insert !== undefined) {
      typeText(q.insert);
      out({});
    } else if (q.state) {
      const eds = [...w.document.querySelectorAll('[data-tid="message-pane-body"] [data-tid="ckeditor"]')];
      out({ value: { ...w.__fx, list: !!w.document.querySelector("[role=listbox]"), edit_html: eds.map(e => e.innerHTML), title: w.document.title,
                     compose: w.document.querySelector('#compose [data-tid="ckeditor"]').innerHTML } });
    }
  } catch (e) {
    out({ error: String(e && e.message || e) });
  }
}
