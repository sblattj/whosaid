/* whosaid live explainer: offline enhancements (inlined into the static page; works as file://). */
(function () {
  'use strict';

  // ---------- pure helpers (unit tested under bun) ----------

  function escapeRegExp(s) {
    // No hyphen escape: under the u flag "\-" is a syntax error outside a class.
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // Word / space / punctuation tokens. \s and \p{L} here are real regex escapes (one backslash,
  // inside a regex literal), never built from a string.
  function tokenize(text) {
    return String(text == null ? '' : text).match(/\s+|[\p{L}\p{N}_]+(?:['\u2019][\p{L}\p{N}_]+)*|[^\s\p{L}\p{N}_]/gu) || [];
  }

  function isWs(t) { return /^\s+$/.test(t); }

  // LCS word diff. Returns [{op:'eq'|'del'|'ins', text}], whitespace kept. A whitespace-only
  // equal run sitting between two changes is folded into both sides so "Ana Cole" -> "Ben Fox"
  // is one del and one ins, not two of each.
  function wordDiff(oldText, newText) {
    const a = tokenize(oldText), b = tokenize(newText);
    let pre = 0;
    while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
    let suf = 0;
    while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;
    const am = a.slice(pre, a.length - suf), bm = b.slice(pre, b.length - suf);
    const raw = [];
    for (let i = 0; i < pre; i++) raw.push({ op: 'eq', text: a[i] });
    const n = am.length, m = bm.length;
    if (n && m && n * m <= 4000000) {
      const w = m + 1;
      const dp = new Uint32Array((n + 1) * w);
      for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
          dp[i * w + j] = am[i] === bm[j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
        }
      }
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (am[i] === bm[j]) { raw.push({ op: 'eq', text: am[i] }); i++; j++; }
        else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) { raw.push({ op: 'del', text: am[i++] }); }
        else { raw.push({ op: 'ins', text: bm[j++] }); }
      }
      while (i < n) raw.push({ op: 'del', text: am[i++] });
      while (j < m) raw.push({ op: 'ins', text: bm[j++] });
    } else {
      for (let i = 0; i < n; i++) raw.push({ op: 'del', text: am[i] });
      for (let j = 0; j < m; j++) raw.push({ op: 'ins', text: bm[j] });
    }
    for (let k = a.length - suf; k < a.length; k++) raw.push({ op: 'eq', text: a[k] });

    const out = [];
    let d = '', ins = '';
    const push = (op, text) => {
      if (!text) return;
      const last = out[out.length - 1];
      if (last && last.op === op) last.text += text; else out.push({ op, text });
    };
    const flush = () => { push('del', d); push('ins', ins); d = ''; ins = ''; };
    for (let k = 0; k < raw.length; k++) {
      const t = raw[k];
      if (t.op === 'eq') {
        const next = raw[k + 1];
        if ((d || ins) && isWs(t.text) && next && next.op !== 'eq') { d += t.text; ins += t.text; continue; }
        flush();
        push('eq', t.text);
      } else if (t.op === 'del') d += t.text;
      else ins += t.text;
    }
    flush();
    return out;
  }

  // Index of the first case-insensitive whole-word occurrence of term in text, or -1.
  function findTermOnce(text, term) {
    if (!term) return -1;
    const re = new RegExp('(?<![\\p{L}\\p{N}_])' + escapeRegExp(term) + '(?![\\p{L}\\p{N}_])', 'iu');
    const m = re.exec(String(text));
    return m ? m.index : -1;
  }

  const lib = { tokenize, wordDiff, findTermOnce, escapeRegExp };

  // ---------- DOM work (browser only) ----------

  function initDom() {
    const doc = document, win = window;
    let observer = null, depth = 0, debounce = null, runs = 0;
    const timers = {};
    win.xpEnhanceRuns = 0;
    win.xpLib = lib;

    const $ = (s, r) => (r || doc).querySelector(s);
    const $$ = (s, r) => Array.from((r || doc).querySelectorAll(s));
    const store = {
      get(k, d) { try { const v = win.localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
      set(k, v) { try { win.localStorage.setItem(k, v); } catch (e) { /* file:// may refuse */ } },
    };
    const BAD = 'svg, script, style, mark.xp-mark, .xp-pop, button, textarea';

    function connect() {
      const main = $('#xp-sections');
      if (main && observer) { observer.observe(main, { childList: true, subtree: true, characterData: true }); observer.takeRecords(); }
    }
    // Run a DOM mutation without the observer seeing it (no retrigger loop).
    function xpBusy(fn) {
      if (observer && depth === 0) observer.disconnect();
      depth++;
      try { return fn(); } finally {
        depth--;
        if (depth === 0 && observer) { connect(); observer.takeRecords(); }
      }
    }

    function unwrapEl(el) {
      const p = el.parentNode;
      if (!p) return null;
      while (el.firstChild) p.insertBefore(el.firstChild, el);
      p.removeChild(el);
      return p;
    }
    function unwrapOwn(root) {
      const parents = new Set();
      $$('mark.xp-mark, span.xp-term', root).forEach((el) => { const p = unwrapEl(el); if (p) parents.add(p); });
      $$('.xp-pop, [data-xp-added]', root).forEach((el) => el.remove());
      parents.forEach((p) => { if (p.isConnected !== false) p.normalize(); });
    }

    // HTML with this script's wrappers removed. Element input yields its outerHTML.
    function stripped(x) {
      const tpl = doc.createElement('template');
      tpl.innerHTML = typeof x === 'string' ? x : (x && x.outerHTML) || '';
      unwrapOwn(tpl.content);
      return tpl;
    }
    function xpStrip(x) { return stripped(x).innerHTML; }

    function textNodes(root, skip) {
      const out = [];
      const w = doc.createTreeWalker(root, 4, null);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const p = n.parentElement;
        if (!n.data || (p && p.closest(skip))) continue;
        out.push(n);
      }
      return out;
    }

    function sections() {
      return $$('section.xs');
    }
    function realSection(id) {
      return sections().find((s) => s.dataset.id === id && !s.closest('.xs-swap-new'));
    }

    // ----- theme -----
    function applyTheme(t) {
      doc.documentElement.dataset.theme = t;
      const b = $('#xp-theme');
      if (b) b.textContent = t === 'dark' ? 'Light' : 'Dark';
    }

    // ----- timeline -----
    function buildTimeline() {
      const nav = $('#xp-timeline');
      if (!nav) return;
      nav.textContent = '';
      const secs = sections().filter((s) => !s.classList.contains('xs-recap') && !s.closest('.xs-swap-new')).reverse();
      secs.forEach((s) => {
        const id = s.dataset.id || '';
        const a = doc.createElement('a');
        a.className = 'xp-seg';
        a.href = '#s-' + id;
        a.style.flexGrow = String(Math.max(1, Number(s.dataset.minutes) || 1));
        const t = s.querySelector('.xs-time'), h = s.querySelector('.xs-title');
        a.title = ((t ? t.textContent.trim() : '') + ' ' + (h ? h.textContent.trim() : '')).trim();
        a.addEventListener('click', (e) => {
          e.preventDefault();
          const target = realSection(id) || s;
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        nav.appendChild(a);
      });
    }

    // ----- for-you strip -----
    function buildForYou() {
      const box = $('#xp-foryou');
      if (!box) return;
      const secs = sections().filter((s) => !s.closest('.xs-swap-old'));
      const items = [];
      const take = (sel, kind) => secs.forEach((s) => $$(sel, s).forEach((el, i) => {
        if (el.closest('.xs-swap-old')) return;
        let text;
        if (el.classList.contains('xs-fy')) { const q = el.querySelector('.xs-fy-ask'); text = (q || el).textContent; }
        else text = el.textContent;
        items.push({ key: el.dataset.key || (s.dataset.id + '-' + kind + i), text: text.replace(/\s+/g, ' ').trim(), kind, sec: s });
      }));
      take('.xs-ask', 'ask');
      take('.xs-fy', 'for you');
      take('.xs-q', 'question');
      const rank = { ask: 0, 'for you': 0, question: 1 };
      items.sort((x, y) => rank[x.kind] - rank[y.kind]); // stable: keeps document order inside a rank
      box.textContent = '';
      if (!items.length) { box.hidden = true; return; }
      const h = doc.createElement('h2');
      h.textContent = 'For you (' + items.length + ')';
      const ul = doc.createElement('ul');
      items.forEach((it) => {
        const li = doc.createElement('li');
        const cb = doc.createElement('input');
        cb.type = 'checkbox';
        cb.checked = store.get('xp-done:' + it.key, '') === '1';
        li.classList.toggle('xp-done', cb.checked);
        cb.addEventListener('change', () => {
          store.set('xp-done:' + it.key, cb.checked ? '1' : '');
          li.classList.toggle('xp-done', cb.checked);
        });
        const k = doc.createElement('span');
        k.className = 'xp-fy-kind';
        k.textContent = it.kind;
        const tx = doc.createElement('span');
        tx.className = 'xp-fy-text';
        tx.textContent = it.text;
        tx.addEventListener('click', () => {
          const sec = it.sec.isConnected ? it.sec : null;
          const target = (sec && realSection(sec.dataset.id)) || sec;
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        li.append(cb, k, tx);
        ul.appendChild(li);
      });
      box.append(h, ul);
      box.hidden = false;
    }

    // ----- glossary -----
    function glossaryMap() {
      const map = {};
      const g = $('#xp-glossary');
      if (g) { try { Object.assign(map, JSON.parse(g.textContent || '{}')); } catch (e) { /* ignore bad JSON */ } }
      $$('.xs-st-line').forEach((line) => {
        const dfn = line.querySelector('dfn');
        if (!dfn) return;
        const term = dfn.textContent.trim();
        const c = line.cloneNode(true);
        $$('b, dfn', c).forEach((n) => n.remove());
        const def = c.textContent.replace(/^[\s:\-]+/, '').replace(/\s+/g, ' ').trim();
        if (term && def) map[term] = def;
      });
      return map;
    }
    function applyGlossary() {
      const map = glossaryMap();
      const terms = Object.keys(map).filter(Boolean).sort((x, y) => y.length - x.length);
      if (!terms.length) return;
      sections().forEach((sec) => {
        const roots = $$('.xs-explain, .xs-claims', sec);
        if (!roots.length) return;
        terms.forEach((term) => {
          for (const root of roots) {
            let done = false;
            for (const node of textNodes(root, BAD + ', .xp-term')) {
              const idx = findTermOnce(node.data, term);
              if (idx < 0) continue;
              const mid = node.splitText(idx);
              mid.splitText(term.length);
              const span = doc.createElement('span');
              span.className = 'xp-term';
              span.tabIndex = 0;
              span.dataset.def = map[term];
              mid.parentNode.insertBefore(span, mid);
              span.appendChild(mid);
              done = true;
              break;
            }
            if (done) break;
          }
        });
      });
    }

    // ----- search + chips -----
    function markText(root, query) {
      const re = new RegExp(escapeRegExp(query), 'gi');
      let count = 0;
      textNodes(root, BAD).forEach((node) => {
        const hits = [];
        let m;
        re.lastIndex = 0;
        while ((m = re.exec(node.data)) && m[0].length) hits.push({ i: m.index, len: m[0].length });
        for (let k = hits.length - 1; k >= 0; k--) {
          const mid = node.splitText(hits[k].i);
          mid.splitText(hits[k].len);
          const mk = doc.createElement('mark');
          mk.className = 'xp-mark';
          mid.parentNode.insertBefore(mk, mid);
          mk.appendChild(mid);
          count++;
        }
      });
      return count;
    }
    function applyFilters() {
      const query = ((($('#xp-search') || {}).value) || '').trim();
      const pressed = $$('.xp-chip[aria-pressed="true"]').map((c) => c.dataset.filter);
      sections().forEach((sec) => {
        const matched = query ? markText(sec, query) > 0 : true;
        let flagOk = true;
        if (pressed.length) {
          const flags = (sec.dataset.flags || '').split(/\s+/);
          flagOk = pressed.some((f) => flags.indexOf(f) >= 0);
        }
        sec.classList.toggle('xp-hide', !(matched && flagOk));
      });
    }

    function updateTop() {
      const h = $('#xp-top');
      // a header that scrolls away (phone layout) reserves no space above anchors or the load button
      const pinned = h && win.getComputedStyle(h).position === 'sticky';
      if (h) doc.documentElement.style.setProperty('--xp-top-h', (pinned ? h.offsetHeight : 0) + 'px');
    }

    function xpEnhance() {
      clearTimeout(debounce);
      runs++;
      win.xpEnhanceRuns = runs;
      xpBusy(() => {
        const main = $('#xp-sections');
        if (main) unwrapOwn(main);
        buildTimeline();
        buildForYou();
        applyGlossary();
        applyFilters();
      });
      updateTop();
    }
    function schedule() {
      clearTimeout(debounce);
      debounce = setTimeout(xpEnhance, 200);
    }

    // ----- popover -----
    let pop = null;
    function hidePop() { if (pop) { pop.remove(); pop = null; } }
    function showPop(term) {
      hidePop();
      pop = doc.createElement('div');
      pop.className = 'xp-pop';
      pop.setAttribute('role', 'tooltip');
      pop.textContent = term.dataset.def || '';
      doc.body.appendChild(pop);
      const r = term.getBoundingClientRect();
      const left = Math.max(8, Math.min(r.left, win.innerWidth - pop.offsetWidth - 8));
      let top = r.bottom + 6;
      if (top + pop.offsetHeight > win.innerHeight - 4) top = Math.max(4, r.top - pop.offsetHeight - 6);
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }

    // ----- copy -----
    function copyRich(ans) {
      const tpl = stripped(ans);
      const root = tpl.content.firstElementChild;
      const html = root ? root.innerHTML : '';
      const plain = ans.innerText || ans.textContent || '';
      const nav = win.navigator;
      const fallback = () => (nav.clipboard && nav.clipboard.writeText ? nav.clipboard.writeText(plain) : Promise.reject(new Error('no clipboard')));
      if (nav.clipboard && nav.clipboard.write && win.ClipboardItem) {
        try {
          return nav.clipboard.write([new win.ClipboardItem({
            'text/html': new Blob([html], { type: 'text/html' }),
            'text/plain': new Blob([plain], { type: 'text/plain' }),
          })]).catch(fallback);
        } catch (e) { return fallback(); }
      }
      return fallback();
    }
    function onCopy(btn) {
      const ans = btn.parentElement && btn.parentElement.querySelector('.xs-fy-answer');
      if (!ans) return;
      const label = btn.dataset.xpLabel || btn.textContent;
      btn.dataset.xpLabel = label;
      const done = (text) => {
        xpBusy(() => { btn.textContent = text; });
        clearTimeout(timers.copy);
        timers.copy = setTimeout(() => xpBusy(() => { btn.textContent = label; }), 1500);
      };
      copyRich(ans).then(() => done('Copied'), () => done('Copy failed'));
    }

    // ----- one-time wiring -----
    function bind() {
      if (doc.documentElement.dataset.xpBound) return;
      doc.documentElement.dataset.xpBound = '1';
      const tb = $('#xp-theme');
      if (tb) tb.addEventListener('click', () => {
        const next = doc.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        store.set('xp-theme', next);
        applyTheme(next);
      });
      const search = $('#xp-search');
      if (search) search.addEventListener('input', () => {
        clearTimeout(timers.search);
        timers.search = setTimeout(() => xpBusy(() => { unwrapOwnMarks(); applyFilters(); }), 150);
      });
      $$('.xp-chip').forEach((c) => {
        if (!c.hasAttribute('aria-pressed')) c.setAttribute('aria-pressed', 'false');
        c.addEventListener('click', () => {
          c.setAttribute('aria-pressed', c.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
          xpBusy(() => { unwrapOwnMarks(); applyFilters(); });
        });
      });
      doc.addEventListener('click', (e) => {
        const t = e.target;
        if (!t || !t.closest) return;
        const cp = t.closest('.xs-copy');
        if (cp) { onCopy(cp); return; }
        const term = t.closest('.xp-term');
        if (term) { if (pop && pop._for === term) hidePop(); else { showPop(term); pop._for = term; } return; }
        if (!t.closest('.xp-pop')) hidePop();
      });
      doc.addEventListener('mouseover', (e) => {
        const term = e.target.closest && e.target.closest('.xp-term');
        if (term && !(pop && pop._for === term)) { showPop(term); pop._for = term; }
      });
      doc.addEventListener('mouseout', (e) => {
        if (e.target.closest && e.target.closest('.xp-term')) hidePop();
      });
      doc.addEventListener('focusin', (e) => {
        const term = e.target.closest && e.target.closest('.xp-term');
        if (term) { showPop(term); pop._for = term; }
      });
      doc.addEventListener('focusout', hidePop);
      doc.addEventListener('keydown', (e) => { if (e.key === 'Escape') hidePop(); });
      win.addEventListener('resize', () => { hidePop(); updateTop(); });
      win.addEventListener('scroll', hidePop, { passive: true }); // a fixed popover would float off its term
      if (win.ResizeObserver && $('#xp-top')) new win.ResizeObserver(updateTop).observe($('#xp-top'));
    }
    function unwrapOwnMarks() {
      const parents = new Set();
      $$('mark.xp-mark', $('#xp-sections') || doc).forEach((el) => { const p = unwrapEl(el); if (p) parents.add(p); });
      parents.forEach((p) => p.normalize());
    }

    win.xpBusy = xpBusy;
    win.xpStrip = xpStrip;
    win.xpEnhance = xpEnhance;

    function start() {
      applyTheme(store.get('xp-theme', 'dark') === 'light' ? 'light' : 'dark');
      bind();
      const main = $('#xp-sections');
      if (main && !observer && win.MutationObserver) observer = new win.MutationObserver(schedule);
      xpEnhance();
    }
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start);
    else start();
  }

  if (typeof document !== 'undefined' && typeof window !== 'undefined') initDom();
  if (typeof module !== 'undefined') module.exports = lib;
})();
