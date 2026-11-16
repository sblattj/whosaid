/* whosaid live explainer: live client. Served at /live.js only while a server is running.
   Never touches #xp-sections on an SSE event; only on a click of #xp-load. */
(function () {
  'use strict';

  const lib = (typeof window !== 'undefined' && window.xpLib) || (typeof require === 'function' ? require('./page.js') : null);

  // ---------- pure helpers (unit tested under bun) ----------

  // manifest: {sections:[{id,rev,hash}], recap:{id,rev,hash}|null}
  // rawById: {id: {rev,hash,html}}   shownIds: Set|array of ids currently on the page
  function pendingFrom(manifest, rawById, shownIds) {
    const shown = shownIds instanceof Set ? shownIds : new Set(shownIds || []);
    const raw = rawById || {};
    const newIds = [], changedIds = [];
    ((manifest && manifest.sections) || []).forEach((s) => {
      if (!shown.has(s.id)) newIds.push(s.id);
      else if (raw[s.id] && raw[s.id].hash !== s.hash) changedIds.push(s.id);
    });
    let recap = null;
    const r = manifest && manifest.recap;
    if (r) {
      if (!shown.has('recap')) recap = 'new';
      else if (raw.recap && raw.recap.hash !== r.hash) recap = 'changed';
    }
    return { newIds, changedIds, recap, count: newIds.length + changedIds.length + (recap ? 1 : 0) };
  }

  // del + ins runs (an "eq" run is not a change).
  function countChanges(ops) {
    return (ops || []).filter((o) => o.op !== 'eq').length;
  }

  function loadLabel(n) {
    return '↑ ' + n + ' new ' + (n === 1 ? 'update' : 'updates') + ', click to load';
  }

  // Character ranges [start,end) in the old text (kind 'del') or new text (kind 'ins').
  function rangesFromOps(ops, kind) {
    const out = [];
    let pos = 0;
    (ops || []).forEach((o) => {
      if (o.op === 'eq') pos += o.text.length;
      else if (o.op === kind) {
        const last = out[out.length - 1];
        if (last && last[1] === pos) last[1] = pos + o.text.length; else out.push([pos, pos + o.text.length]);
        pos += o.text.length;
      }
    });
    return out;
  }

  // Align blocks: identical texts are matched by LCS first (so one inserted block does not
  // shift every later pair), then unmatched blocks inside each gap are paired by order.
  // Each entry: {oi, ni, ops, changes, hasOld, hasNew}; oi/ni = -1 when that side is missing.
  function diffBlockLists(oldTexts, newTexts) {
    const a = oldTexts, b = newTexts, n = a.length, m = b.length;
    const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--)
      for (let j = m - 1; j >= 0; j--)
        L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const out = [];
    let ga = [], gb = [];
    const flush = () => {
      const k = Math.max(ga.length, gb.length);
      for (let x = 0; x < k; x++) {
        const oi = x < ga.length ? ga[x] : -1, ni = x < gb.length ? gb[x] : -1;
        const o = oi >= 0 ? a[oi] : '', w = ni >= 0 ? b[ni] : '';
        const ops = lib.wordDiff(o, w);
        out.push({ oi, ni, ops, changes: countChanges(ops), hasOld: oi >= 0, hasNew: ni >= 0 });
      }
      ga = []; gb = [];
    };
    let i = 0, j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && a[i] === b[j]) {
        flush();
        out.push({ oi: i, ni: j, ops: a[i] ? [{ op: 'eq', text: a[i] }] : [], changes: 0, hasOld: true, hasNew: true });
        i++; j++;
      } else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) gb.push(j++);
      else ga.push(i++);
    }
    flush();
    return out;
  }

  // Index in a newest-first list of numeric-ish ids where newId belongs (before the first smaller id).
  function insertIndex(idsNewestFirst, newId) {
    const n = parseInt(newId, 10);
    for (let i = 0; i < idsNewestFirst.length; i++) {
      if (parseInt(idsNewestFirst[i], 10) < n) return i;
    }
    return idsNewestFirst.length;
  }

  const pure = { pendingFrom, countChanges, rangesFromOps, diffBlockLists, insertIndex, loadLabel };

  // ---------- client ----------

  function start() {
    const doc = document, win = window;
    const $ = (s, r) => (r || doc).querySelector(s);
    const $$ = (s, r) => Array.from((r || doc).querySelectorAll(s));
    const busy = (fn) => (win.xpBusy ? win.xpBusy(fn) : fn());
    const enhance = () => { if (win.xpEnhance) win.xpEnhance(); };
    const BLOCKS = 'h2.xs-title, p, li';

    const main = $('#xp-sections');
    const btn = $('#xp-load');
    if (!main || !btn) return;

    const rawById = {};
    const shown = new Set();
    let manifest = null, seeded = false, loading = false;
    const swaps = {}; // id -> swap element awaiting the user's click

    function currentSections() {
      return $$('section.xs', main).filter((s) => !s.closest('.xs-swap-new'));
    }
    currentSections().forEach((s) => shown.add(s.dataset.id));

    function parse(html) {
      const tpl = doc.createElement('template');
      tpl.innerHTML = html;
      const el = tpl.content.querySelector('section.xs');
      return el ? doc.importNode(el, true) : null;
    }
    function leafBlocks(root) {
      return $$(BLOCKS, root).filter((b) => !b.closest('svg') && !b.querySelector(BLOCKS));
    }
    function textsOfHtml(html) {
      const tpl = doc.createElement('template');
      tpl.innerHTML = html;
      return leafBlocks(tpl.content).map((b) => b.textContent);
    }

    async function getJson(url) {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error(url + ' ' + r.status);
      return r.json();
    }
    async function getText(url) {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error(url + ' ' + r.status);
      return r.text();
    }

    async function seed() {
      const m = await getJson('/manifest.json');
      const entries = (m.sections || []).concat(m.recap ? [m.recap] : []);
      await Promise.all(entries.map(async (e) => {
        try { rawById[e.id] = { rev: e.rev, hash: e.hash, html: await getText('/section/' + e.id) }; } catch (err) { /* retried on load */ }
      }));
      seeded = true;
      return m;
    }

    function setEnded(ended) {
      const st = $('.xp-state');
      if (!st) return;
      st.dataset.state = ended ? 'ended' : 'live';
      st.textContent = ended ? 'ended' : 'live';
    }

    function refreshButton() {
      if (!manifest || !seeded) return;
      const p = pendingFrom(manifest, rawById, shown);
      if (p.count > 0 && !loading) {
        btn.textContent = loadLabel(p.count);
        btn.hidden = false;
      } else if (p.count === 0) btn.hidden = true;
    }

    function onManifest(m) {
      manifest = m;
      setEnded(!!m.ended);
      refreshButton();
    }

    // ----- new sections: grow open, staggered -----
    function placeNew(el, id, delay) {
      const wrap = doc.createElement('div');
      wrap.className = 'xs-grow xs-entering';
      const inner = doc.createElement('div');
      inner.className = 'xs-grow-in';
      inner.appendChild(el);
      wrap.appendChild(inner);
      const ids = Array.from(main.children).map((c) => {
        const s = c.matches('section.xs') ? c : c.querySelector('section.xs');
        return s && !s.classList.contains('xs-recap') ? s.dataset.id : null;
      });
      const live = ids.filter((x) => x != null);
      const at = insertIndex(live, id);
      let ref = null, seen = 0;
      for (let i = 0; i < main.children.length; i++) {
        if (ids[i] == null) continue;
        if (seen === at) { ref = main.children[i]; break; }
        seen++;
      }
      if (!ref) {
        // after the last real section (or after the recap / at top when none)
        ref = null;
        if (!live.length) {
          const recap = main.querySelector(':scope > section.xs-recap');
          ref = recap ? recap.nextSibling : main.firstChild;
        }
      }
      main.insertBefore(wrap, ref);
      return { wrap, el, delay };
    }
    function animateOpen(items) {
      void main.offsetHeight; // commit the 0fr start state
      items.forEach((it) => {
        it.wrap.style.transitionDelay = it.delay + 'ms';
        it.wrap.firstChild.style.transitionDelay = it.delay + 'ms';
        it.wrap.classList.remove('xs-entering');
        setTimeout(() => {
          busy(() => {
            const p = it.wrap.parentNode;
            if (p) { p.insertBefore(it.el, it.wrap); it.wrap.remove(); }
            it.el.classList.add('xs-glow');
          });
          setTimeout(() => it.el.classList.remove('xs-glow'), 3000);
          enhance();
        }, 1800 + it.delay + 60);
      });
    }

    // ----- changed section: side-by-side swap -----
    function markRanges(block, ranges, tag, cls) {
      if (!ranges.length) return;
      const nodes = [];
      let pos = 0;
      const w = doc.createTreeWalker(block, 4, null);
      for (let n = w.nextNode(); n; n = w.nextNode()) { nodes.push({ n, s: pos, e: pos + n.data.length }); pos += n.data.length; }
      nodes.forEach((ent) => {
        let cur = ent.n, curStart = ent.s;
        ranges.forEach((r) => {
          if (!cur) return;
          const s = Math.max(r[0], curStart), e = Math.min(r[1], curStart + cur.data.length);
          if (e <= s) return;
          if (s > curStart) { cur = cur.splitText(s - curStart); curStart = s; }
          const rem = (e - s) < cur.data.length ? cur.splitText(e - s) : null;
          const el = doc.createElement(tag);
          el.className = cls;
          cur.parentNode.insertBefore(el, cur);
          el.appendChild(cur);
          cur = rem;
          curStart = e;
        });
      });
    }

    function finalizeSwap(swap, id) {
      const newEl = swap.querySelector('.xs-swap-new section.xs');
      busy(() => {
        if (newEl) {
          if (newEl.dataset.xpId) newEl.id = newEl.dataset.xpId;
          delete newEl.dataset.xpId;
          swap.replaceWith(newEl);
        } else swap.remove();
      });
      delete swaps[id];
      return newEl;
    }

    function fadeAndClean(el) {
      setTimeout(() => {
        $$('ins.xs-ins', el).forEach((i) => i.classList.add('xs-ins-fade'));
        setTimeout(() => {
          busy(() => {
            const parents = new Set();
            $$('ins.xs-ins', el).forEach((i) => {
              const p = i.parentNode;
              while (i.firstChild) p.insertBefore(i.firstChild, i);
              p.removeChild(i);
              parents.add(p);
            });
            parents.forEach((p) => p.normalize());
            $$('.xs-changed', el).forEach((b) => b.classList.remove('xs-changed'));
          });
          enhance();
        }, 4500);
      }, 4000);
    }

    function buildSwap(id, oldSection, oldHtml, newHtml) {
      const newEl = parse(newHtml);
      if (!newEl) return null;
      const oldTexts = textsOfHtml(oldHtml);
      const newBlocks = leafBlocks(newEl);
      const diffs = diffBlockLists(oldTexts, newBlocks.map((b) => b.textContent));
      const oldBlocks = leafBlocks(oldSection);
      let total = 0;
      diffs.forEach((d) => {
        if (!d.changes) return;
        total += d.changes;
        const ob = d.hasOld ? oldBlocks[d.oi] : null, nb = d.hasNew ? newBlocks[d.ni] : null;
        // only mark old blocks whose live text still equals the stored raw text
        if (ob && ob.textContent === oldTexts[d.oi]) { markRanges(ob, rangesFromOps(d.ops, 'del'), 'del', 'xs-del'); ob.classList.add('xs-changed'); }
        if (nb) { markRanges(nb, rangesFromOps(d.ops, 'ins'), 'ins', 'xs-ins'); nb.classList.add('xs-changed'); }
      });
      if (!total) return { newEl, swap: null };
      const swap = doc.createElement('div');
      swap.className = 'xs-swap';
      swap.dataset.swapFor = id;
      const oldPane = doc.createElement('div');
      oldPane.className = 'xs-swap-old';
      const newPane = doc.createElement('div');
      newPane.className = 'xs-swap-new';
      const head = doc.createElement('div');
      head.className = 'xs-swap-head';
      const label = doc.createElement('span');
      label.textContent = 'corrected version: ' + total + ' change' + (total === 1 ? '' : 's') + ' highlighted';
      const rb = doc.createElement('button');
      rb.type = 'button';
      rb.className = 'xs-swap-replace';
      rb.textContent = 'replace with the correction';
      head.append(label, rb);
      if (newEl.id) { newEl.dataset.xpId = newEl.id; newEl.removeAttribute('id'); } // one live id on the page
      newPane.append(head, newEl);
      oldSection.replaceWith(swap);
      oldPane.appendChild(oldSection);
      swap.append(oldPane, newPane);
      rb.addEventListener('click', () => {
        if (swap.classList.contains('xs-replacing')) return;
        swap.classList.add('xs-replacing');
        rb.disabled = true;
        setTimeout(() => {
          if (!swap.isConnected) return;
          const el = finalizeSwap(swap, id);
          enhance();
          if (el) fadeAndClean(el);
        }, 2600);
      });
      swaps[id] = swap;
      return { newEl, swap };
    }

    function replaceInstant(oldSection, newEl) {
      newEl.classList.add('xs-glow');
      oldSection.replaceWith(newEl);
      setTimeout(() => newEl.classList.remove('xs-glow'), 3000);
    }

    // ----- the click -----
    async function load() {
      if (loading || !manifest) return;
      loading = true;
      btn.disabled = true;
      try {
        const p = pendingFrom(manifest, rawById, shown);
        const ids = p.newIds.concat(p.changedIds);
        if (p.recap) ids.push('recap');
        const entry = {};
        (manifest.sections || []).concat(manifest.recap ? [manifest.recap] : []).forEach((e) => { entry[e.id] = e; });
        const frag = {};
        await Promise.all(ids.map(async (id) => { frag[id] = await getText('/section/' + id); }));

        // settle any unanswered swap for a section that changed again
        p.changedIds.forEach((id) => { if (swaps[id]) { const el = finalizeSwap(swaps[id], id); if (el) fadeAndClean(el); } });

        busy(() => {
          const empty = $('.xp-empty', main);
          if (empty && p.newIds.length) empty.remove();

          // new sections, newest id first so the stagger runs top to bottom
          const fresh = p.newIds.slice().sort((a, b) => parseInt(b, 10) - parseInt(a, 10));
          const items = [];
          fresh.forEach((id, k) => {
            const el = parse(frag[id]);
            if (!el) return;
            items.push(placeNew(el, id, k * 300));
          });
          if (items.length) animateOpen(items);

          // changed sections
          p.changedIds.forEach((id) => {
            const cur = realSection(id);
            const old = rawById[id] && rawById[id].html;
            if (!cur || old == null) { const el = parse(frag[id]); if (el && cur) replaceInstant(cur, el); return; }
            const res = buildSwap(id, cur, old, frag[id]);
            if (res && !res.swap) replaceInstant(cur, res.newEl);
          });

          // recap on top
          if (p.recap) {
            const el = parse(frag.recap);
            if (el) {
              const cur = $(':scope > section.xs-recap', main);
              if (cur) replaceInstant(cur, el);
              else { el.classList.add('xs-glow'); main.insertBefore(el, main.firstChild); setTimeout(() => el.classList.remove('xs-glow'), 3000); }
            }
          }
        });

        ids.forEach((id) => {
          rawById[id] = { rev: entry[id] && entry[id].rev, hash: entry[id] && entry[id].hash, html: frag[id] };
          shown.add(id);
        });
        btn.hidden = true;
        enhance();
      } catch (err) {
        if (win.console) console.error('live explainer load failed', err);
      } finally {
        loading = false;
        btn.disabled = false;
        refreshButton();
      }
    }
    function realSection(id) {
      return currentSections().find((s) => s.dataset.id === id);
    }

    btn.addEventListener('click', load);

    seed().then((m) => {
      onManifest(m);
      const es = new EventSource('/events');
      es.addEventListener('manifest', (e) => {
        try { onManifest(JSON.parse(e.data)); } catch (err) { /* ignore malformed */ }
      });
    }).catch((err) => { if (win.console) console.error('live explainer seed failed', err); });
  }

  if (typeof document !== 'undefined' && typeof window !== 'undefined' && typeof EventSource !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
  if (typeof module !== 'undefined') module.exports = pure;
})();
