/*
 * teams-chat-scraper.js — harvest MS Teams web chat history into whosaid-ingestible JSON.
 *
 * Runs ENTIRELY in the Teams web tab (teams.cloud.microsoft) on an already
 * authenticated session. Driven over Chrome DevTools Protocol (CDP), but the
 * logic below is plain page JS — paste it into the tab console to try by hand.
 *
 * WHY THE DETACHED/POLL SHAPE: a single CDP Runtime.evaluate call is capped at
 * ~15 s, but a full multi-chat back-scroll runs minutes. So we DO NOT await the
 * scrape in one call. Instead we install window.__ts, kick run() as a detached
 * promise that stashes progress on window.__ts, and the driver POLLS
 * window.__ts.{done,progress,result} with short calls until done. One long
 * awaited call would time out and lose the return.
 *
 * OUTPUT (window.__ts.result): raw_by_chat, a map keyed by chat display name:
 *   { "<chat name>": { chat, harvested, kept, reachedCutoff, messages:[
 *       { chat, author, ts, epoch, text } ... ] } }
 * The driver then flattens every chat's messages into one chronological list
 * (messages.json / messages.ndjson) and renders rollup.md. epoch (ms, from the
 * message data-mid) is the stable dedup + cutoff key.
 *
 * VERIFIED DOM CONTRACT (Teams web, 2026-09):
 *   chat list rows : [role="treeitem"][data-item-type="chat"]   (real 1:1/group chats;
 *                    folders are data-item-type="custom-folder"/"chats" — skip)
 *   chat list is virtualized -> scroll the tree to enumerate every chat.
 *   open thread row: [data-tid="chat-pane-item"] wrapping [data-tid="chat-pane-message"]
 *   timestamp      : data-mid on the message element is EPOCH-MS
 *                    (a child <time datetime="ISO"> agrees)
 *   author         : [id^="author-"].innerText; grouped consecutive messages share
 *                    one header, so carry the last-seen author forward
 *   body           : [id^="content-"] (fall back to the message innerText)
 *   history scroll : [data-tid="message-pane-list-viewport"]; scrolling up lazy-loads
 *                    older messages and evicts off-screen ones -> harvest EVERY frame
 *                    into a Map keyed by data-mid, never once at the end.
 *
 * SCOPE: left-rail 1:1 and group chats only. Teams CHANNEL posts are a different
 * surface and are not covered. Text bodies only (reactions/attachments/images and
 * quoted-reply bodies are captured as plain text or dropped).
 */

(function installTeamsScraper() {
  const CUTOFF_DAYS = Number(window.__TS_CUTOFF_DAYS ?? 7);          // harvest window
  const CUTOFF_MS   = Date.now() - CUTOFF_DAYS * 24 * 60 * 60 * 1000;
  const SETTLE_MS   = 550;    // wait after each scroll for lazy-load to paint
  const STEP_FRAC   = 0.85;   // scroll up ~0.85 * clientHeight per frame
  const MAX_FRAMES  = 400;    // hard ceiling on scroll frames per chat
  const NOPROG_STOP = 6;      // stop a chat after this many no-new-message frames

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function listChatRows() {
    return qsa('[role="treeitem"][data-item-type="chat"]');
  }

  // Harvest every message currently in the DOM of the open thread into `store`
  // (Map data-mid -> record). Returns the count of NEW messages added this frame.
  function harvestOpenThread(chatName, store) {
    const items = qsa('[data-tid="chat-pane-item"]');
    let added = 0;
    let lastAuthor = null;
    for (const item of items) {
      const msg = item.querySelector('[data-tid="chat-pane-message"]') || item;
      const mid = msg.getAttribute('data-mid');
      const authorEl = item.querySelector('[id^="author-"]');
      if (authorEl && authorEl.innerText.trim()) lastAuthor = authorEl.innerText.trim();
      if (!mid) continue;                       // system/day-divider rows have no data-mid
      const epoch = Number(mid);
      if (!Number.isFinite(epoch)) continue;
      const contentEl = item.querySelector('[id^="content-"]');
      const text = (contentEl ? contentEl.innerText : msg.innerText || '').trim();
      if (!text) continue;
      if (store.has(mid)) continue;
      store.set(mid, {
        chat: chatName,
        author: lastAuthor || '(unknown)',
        ts: new Date(epoch).toISOString(),
        epoch,
        text,
      });
      added++;
    }
    return added;
  }

  // Back-scroll one open thread to the cutoff (or top), harvesting each frame.
  async function scrapeOpenThread(chatName) {
    const store = new Map();
    const viewport = document.querySelector('[data-tid="message-pane-list-viewport"]');
    if (!viewport) return { chat: chatName, harvested: 0, kept: 0, reachedCutoff: false, messages: [] };

    let reachedCutoff = false;
    let noProgress = 0;
    for (let frame = 0; frame < MAX_FRAMES; frame++) {
      harvestOpenThread(chatName, store);
      const oldest = Math.min(...Array.from(store.values(), (m) => m.epoch), Infinity);
      if (Number.isFinite(oldest) && oldest < CUTOFF_MS) { reachedCutoff = true; break; }

      const before = viewport.scrollTop;
      viewport.scrollTop = Math.max(0, before - viewport.clientHeight * STEP_FRAC);
      await sleep(SETTLE_MS);
      const added = harvestOpenThread(chatName, store);
      const atTop = viewport.scrollTop <= 0 && before <= 0;
      if (added === 0 && (viewport.scrollTop === before || atTop)) {
        if (++noProgress >= NOPROG_STOP) break;
      } else {
        noProgress = 0;
      }
    }

    const all = Array.from(store.values()).sort((a, b) => a.epoch - b.epoch);
    const kept = all.filter((m) => m.epoch >= CUTOFF_MS);
    return { chat: chatName, harvested: all.length, kept: kept.length, reachedCutoff, messages: kept };
  }

  async function run() {
    const ts = window.__ts;
    ts.done = false;
    ts.error = null;
    ts.result = {};
    try {
      // Enumerate chats; scroll the (virtualized) tree so every row mounts once.
      const seen = new Set();
      const tree = listChatRows()[0]?.closest('[role="tree"]') || null;
      for (let pass = 0; pass < 40; pass++) {
        for (const row of listChatRows()) {
          const name = row.getAttribute('aria-label') || row.innerText.trim().split('\n')[0];
          if (name) seen.add(name);
        }
        if (tree) { tree.scrollTop = Math.min(tree.scrollTop + tree.clientHeight * 0.8, tree.scrollHeight); await sleep(200); }
        else break;
        if (tree && tree.scrollTop + tree.clientHeight >= tree.scrollHeight) break;
      }
      const chatNames = Array.from(seen);
      ts.progress = { phase: 'chats-enumerated', total: chatNames.length, doneChats: 0 };

      for (const name of chatNames) {
        // Re-find the row each time (the tree re-virtualizes as it scrolls).
        let row = listChatRows().find((r) => (r.getAttribute('aria-label') || r.innerText).includes(name));
        if (!row) {
          // scroll the tree looking for it
          for (let s = 0; s < 40 && !row; s++) {
            if (tree) { tree.scrollTop += tree.clientHeight * 0.7; await sleep(150); }
            row = listChatRows().find((r) => (r.getAttribute('aria-label') || r.innerText).includes(name));
          }
        }
        if (!row) { ts.progress.doneChats++; continue; }
        row.click();
        await sleep(900);                         // let the thread pane mount
        const rec = await scrapeOpenThread(name);
        ts.result[name] = rec;
        ts.progress.doneChats++;
        ts.progress.lastChat = name;
      }
      ts.done = true;
    } catch (e) {
      ts.error = String(e && e.stack || e);
      ts.done = true;
    }
  }

  window.__ts = { done: false, error: null, result: {}, progress: {}, run };
  window.__ts.run();          // detached: driver polls window.__ts.{done,progress,result}
  return 'teams scraper installed; poll window.__ts.done / window.__ts.progress';
})();

/*
 * DRIVER (outside the page, e.g. CDP or manual):
 *
 * 1. evaluate this whole file in the Teams tab  -> installs + kicks window.__ts.run()
 * 2. poll every few seconds, short calls:
 *        JSON.stringify(window.__ts.progress)              // {doneChats,total,lastChat}
 *        window.__ts.done                                  // true when finished
 *        window.__ts.error                                 // non-null on failure
 * 3. when done, pull the result:
 *        JSON.stringify(window.__ts.result)                // raw_by_chat  -> raw_by_chat.json
 * 4. flatten + render (host side, any language):
 *        messages = Object.values(raw_by_chat).flatMap(c => c.messages)
 *                   .sort((a,b) => a.epoch - b.epoch)       // -> messages.json
 *        messages.ndjson = messages.map(JSON.stringify).join('\n')
 *        rollup.md       = group by chat, then by day, list "[HH:MM:SS] author: text"
 *
 * The whosaid adapter then turns each (chat, day) slice of `messages` into a
 * dated folder with teams.speakers.txt + teams.diarization.json
 * (source.kind="teams-chat"); see README.md here and whosaid issue #49.
 */
