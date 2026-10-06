// Stub-DOM test for the Teams scraper's sticky filter pill handling (issue #57).
// Never touches real Teams. Run: node test/teams_scraper_pills_test.js [scraper.js]
// Optional argv[2] / TEAMS_SCRAPER_PATH points at another scraper copy (RED proof).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const scraperPath = process.argv[2] || process.env.TEAMS_SCRAPER_PATH ||
  path.join(__dirname, '..', 'contrib', 'teams-scraper', 'teams-chat-scraper.js');
const source = fs.readFileSync(scraperPath, 'utf8');

// Rail contents: 'Chats' pill hides meeting chats; 'Unread' pill hides read chats.
const CHATS = [
  { name: 'Alice', meeting: false, unread: true },
  { name: 'Bob', meeting: false, unread: false },
  { name: 'Standup (meeting chat)', meeting: true, unread: true },
  { name: 'Retro (meeting chat)', meeting: true, unread: false },
];

function buildWorld(initiallyPressed) {
  const clicks = [];
  const pressed = new Set(initiallyPressed);
  const pills = ['Unread', 'Channels', 'Chats', 'Meeting chats'].map((label) => ({
    innerText: ' ' + label + ' ',
    getAttribute: (a) => (a === 'aria-pressed' ? String(pressed.has(label)) : null),
    click() { clicks.push(label); pressed.has(label) ? pressed.delete(label) : pressed.add(label); },
  }));
  const tree = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  function visibleRows() {
    return CHATS.filter((c) => !(pressed.has('Chats') && c.meeting) && !(pressed.has('Unread') && !c.unread))
      .map((c) => ({
        innerText: c.name,
        getAttribute: (a) => (a === 'aria-label' ? c.name : null),
        click() {},
        closest: () => tree,
      }));
  }
  const document = {
    querySelector: () => null,
    querySelectorAll(sel) {
      if (sel === '[role="treeitem"][data-item-type="chat"]') return visibleRows();
      if (sel === 'button[aria-pressed="true"]') return pills.filter((p) => p.getAttribute('aria-pressed') === 'true');
      if (sel === 'button[aria-pressed]') return pills;
      return [];
    },
  };
  const window = { __TS_CUTOFF_DAYS: 7, __TS_PILL_SETTLE_MS: 5 };
  const ctx = vm.createContext({
    window, document, Date, Math, Number, Array, Set, Map, String, Infinity, Promise,
    setTimeout: (f) => setTimeout(f, 0),   // collapse all scraper waits
  });
  return { ctx, window, clicks, pressed };
}

async function runScraper(initiallyPressed) {
  const w = buildWorld(initiallyPressed);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 2000 && !w.window.__ts.done; i++) await new Promise((r) => setTimeout(r, 2));
  assert.ok(w.window.__ts.done, 'scraper never finished');
  assert.strictEqual(w.window.__ts.error, null, 'scraper errored: ' + w.window.__ts.error);
  return w;
}

(async () => {
  // Scenario A: "Chats" pill pressed, meeting chats hidden (3 -> wait: 2 visible, 4 after).
  const a = await runScraper(['Chats']);
  const ts = a.window.__ts;
  assert.deepStrictEqual(a.clicks.slice(0, 1), ['Chats'], 'pill must be cleared first');
  assert.ok(ts.pills && ts.pills.cleared.includes('Chats'), 'cleared pill names recorded on __ts.pills.cleared');
  assert.strictEqual(ts.progress.total, 4, 'enumeration must happen AFTER clearing (4 chats, not 2)');
  assert.strictEqual(ts.pills.countBefore, 2, 'countBefore recorded');
  assert.strictEqual(ts.pills.countAfter, 4, 'countAfter recorded');
  assert.ok(/2/.test(ts.progress.warning || '') && /4/.test(ts.progress.warning || ''),
    'progress warning with both counts expected, got: ' + ts.progress.warning);
  assert.deepStrictEqual(a.clicks, ['Chats', 'Chats'], 'pill clicked off then restored');
  assert.ok(a.pressed.has('Chats'), 'pill pressed again at the end');
  // JSON compare: arrays built inside the vm context have a different prototype.
  assert.strictEqual(JSON.stringify(ts.pills.restored), '["Chats"]', 'restored pill recorded');
  console.log('ok A: pressed pill cleared, recorded, warned, restored');

  // Scenario B (control): nothing pressed -> no click, no warning.
  const b = await runScraper([]);
  assert.deepStrictEqual(b.clicks, [], 'no pill pressed: nothing may be clicked');
  assert.ok(!b.window.__ts.progress.warning, 'no pill pressed: no warning');
  assert.strictEqual(b.window.__ts.progress.total, 4);
  assert.ok(!b.window.__ts.pills || b.window.__ts.pills.cleared.length === 0, 'nothing recorded as cleared');
  console.log('ok B: no pill pressed -> no click, no warning');

  // Scenario C: pill pressed but count unchanged ("Meeting chats" hides nothing in the stub) -> no warning, still restored.
  const c = await runScraper(['Meeting chats']);
  assert.deepStrictEqual(c.clicks, ['Meeting chats', 'Meeting chats']);
  assert.ok(!c.window.__ts.progress.warning, 'unchanged count must not warn');
  console.log('ok C: unchanged count -> no warning, pill restored');
  console.log('PASS');
})().catch((e) => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
