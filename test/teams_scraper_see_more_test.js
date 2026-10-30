// Stub-DOM test: the Chats folder's "See more" row (data-testid="load-next-page-button")
// must be clicked until every chat is loaded. Never touches real Teams.
// Run: node test/teams_scraper_see_more_test.js [scraper.js]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const scraperPath = process.argv[2] || process.env.TEAMS_SCRAPER_PATH ||
  path.join(__dirname, '..', 'contrib', 'teams-scraper', 'teams-chat-scraper.js');
const source = fs.readFileSync(scraperPath, 'utf8');

// 7 chats, 3 per page: the rail shows 3, then "See more" adds 3 at a time.
function buildWorld(total, perPage, { inert = false } = {}) {
  const names = Array.from({ length: total }, (_, i) => 'Chat ' + (i + 1));
  let shown = Math.min(perPage, total);
  let clicks = 0;
  const tree = { scrollTop: 0, clientHeight: 0, scrollHeight: 0 };
  const loadMore = {
    click() { clicks++; if (!inert) shown = Math.min(shown + perPage, total); },
    scrollIntoView() {},
  };
  const rows = () => names.slice(0, shown).map((name) => ({
    innerText: name,
    getAttribute: (a) => (a === 'aria-label' ? name : null),
    click() {},
    closest: () => tree,
  }));
  const document = {
    querySelector: (sel) => (sel === '[data-testid="load-next-page-button"]' && shown < total ? loadMore : null),
    querySelectorAll: (sel) => (sel === '[role="treeitem"][data-item-type="chat"]' ? rows() : []),
  };
  const window = { __TS_CUTOFF_DAYS: 7, __TS_PILL_SETTLE_MS: 5, __TS_LOAD_MORE_WAIT_MS: 20,
    __TS_RAIL_STABLE_MS: 3, __TS_RAIL_POLL_MS: 1, __TS_READY_FLOOR_MS: 0 };
  const ctx = vm.createContext({
    window, document, Date, Math, Number, Array, Set, Map, String, Infinity, Promise,
    setTimeout: (f) => setTimeout(f, 0),
  });
  return { ctx, window, clicks: () => clicks };
}

async function run(total, perPage, opts) {
  const w = buildWorld(total, perPage, opts);
  vm.runInContext(source, w.ctx);
  for (let i = 0; i < 4000 && !w.window.__ts.done; i++) await new Promise((r) => setTimeout(r, 2));
  assert.ok(w.window.__ts.done, 'scraper never finished');
  assert.strictEqual(w.window.__ts.error, null, 'scraper errored: ' + w.window.__ts.error);
  return w;
}

(async () => {
  const a = await run(7, 3);
  assert.strictEqual(a.window.__ts.progress.total, 7, 'all 7 chats enumerated, got ' + a.window.__ts.progress.total);
  assert.strictEqual(a.clicks(), 2, '"See more" clicked twice (3 -> 6 -> 7)');
  assert.strictEqual(a.window.__ts.loadMorePages, 2, 'pages loaded recorded on __ts.loadMorePages');
  console.log('ok A: "See more" paged until every chat loaded');

  const b = await run(3, 3);
  assert.strictEqual(b.clicks(), 0, 'no "See more" row: nothing clicked');
  assert.strictEqual(b.window.__ts.progress.total, 3);
  console.log('ok B: no "See more" row -> no click');

  const c = await run(7, 3, { inert: true });
  assert.strictEqual(c.window.__ts.progress.total, 3, 'inert button: the 3 visible chats still scraped');
  assert.ok(c.clicks() <= 3 * 41, 'inert button must not spin, clicks=' + c.clicks());
  console.log('ok C: a "See more" that loads nothing does not hang the run');
  console.log('PASS');
})().catch((e) => { console.error('FAIL: ' + (e && e.stack || e)); process.exit(1); });
