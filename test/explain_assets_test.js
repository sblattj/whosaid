import { describe, expect, test } from 'bun:test';

const page = require('../lib/explain_assets/page.js');
const live = require('../lib/explain_assets/live.js');
const { tokenize, wordDiff, findTermOnce, escapeRegExp } = page;
const { pendingFrom, countChanges, rangesFromOps, diffBlockLists, insertIndex, loadLabel } = live;

describe('tokenize', () => {
  test('keeps words with the letter s intact (regex must stay \\s)', () => {
    expect(tokenize('pass the class')).toEqual(['pass', ' ', 'the', ' ', 'class']);
  });
  test('splits punctuation and keeps whitespace runs', () => {
    expect(tokenize("it's  ok, yes.")).toEqual(["it's", '  ', 'ok', ',', ' ', 'yes', '.']);
    expect(tokenize('')).toEqual([]);
  });
  test('round-trips', () => {
    const s = 'Ana said: "ship it" (today) … maybe?';
    expect(tokenize(s).join('')).toBe(s);
  });
});

describe('wordDiff', () => {
  test('one-word fix', () => {
    expect(wordDiff('the Acme account', 'the Acme accounts')).toEqual([
      { op: 'eq', text: 'the Acme ' },
      { op: 'del', text: 'account' },
      { op: 'ins', text: 'accounts' },
    ]);
  });
  test('misheard name swap', () => {
    expect(wordDiff('ask Dana about the invoice', 'ask Dane about the invoice')).toEqual([
      { op: 'eq', text: 'ask ' },
      { op: 'del', text: 'Dana' },
      { op: 'ins', text: 'Dane' },
      { op: 'eq', text: ' about the invoice' },
    ]);
  });
  test('adjacent changed words fold into one del and one ins', () => {
    const ops = wordDiff('send it to Ana Cole today', 'send it to Ben Fox today');
    expect(ops.filter((o) => o.op === 'del')).toEqual([{ op: 'del', text: 'Ana Cole' }]);
    expect(ops.filter((o) => o.op === 'ins')).toEqual([{ op: 'ins', text: 'Ben Fox' }]);
  });
  test('identical text has no changes; pure insert and delete', () => {
    expect(countChanges(wordDiff('same words', 'same words'))).toBe(0);
    expect(wordDiff('a b', 'a b c').map((o) => o.op)).toEqual(['eq', 'ins']);
    expect(wordDiff('a b c', 'a b').map((o) => o.op)).toEqual(['eq', 'del']);
  });
  test('old text and new text are reconstructible', () => {
    const o = 'the cache is off by default, pass the class';
    const n = 'the cache is on by default, so pass the class';
    const ops = wordDiff(o, n);
    expect(ops.filter((x) => x.op !== 'ins').map((x) => x.text).join('')).toBe(o);
    expect(ops.filter((x) => x.op !== 'del').map((x) => x.text).join('')).toBe(n);
  });
});

describe('findTermOnce', () => {
  test('whole word only, case-insensitive', () => {
    expect(findTermOnce('The SSE stream', 'sse')).toBe(4);
    expect(findTermOnce('classes pass', 'class')).toBe(-1);
    expect(findTermOnce('a class here', 'class')).toBe(2);
    expect(findTermOnce('nothing', 'sse')).toBe(-1);
  });
  test('regex metacharacters in the term are literal', () => {
    expect(findTermOnce('use C++ here', 'C++')).toBe(4);
    expect(findTermOnce('a.b', 'a.b')).toBe(0);
    expect(findTermOnce('axb', 'a.b')).toBe(-1);
    expect(escapeRegExp('a.b(c)')).toBe('a\\.b\\(c\\)');
  });
});

describe('pendingFrom', () => {
  const raw = { '001': { hash: 'a' }, '002': { hash: 'b' }, recap: { hash: 'r1' } };
  test('new and changed are found, unchanged ignored', () => {
    const m = { sections: [{ id: '003', hash: 'c' }, { id: '002', hash: 'b2' }, { id: '001', hash: 'a' }], recap: null };
    const p = pendingFrom(m, raw, new Set(['001', '002']));
    expect(p.newIds).toEqual(['003']);
    expect(p.changedIds).toEqual(['002']);
    expect(p.recap).toBe(null);
    expect(p.count).toBe(2);
  });
  test('nothing pending', () => {
    const m = { sections: [{ id: '001', hash: 'a' }], recap: { id: 'recap', hash: 'r1' } };
    expect(pendingFrom(m, raw, ['001', 'recap']).count).toBe(0);
  });
  test('recap new and recap changed', () => {
    const m = { sections: [], recap: { id: 'recap', hash: 'r2' } };
    expect(pendingFrom(m, raw, new Set(['001'])).recap).toBe('new');
    expect(pendingFrom(m, raw, new Set(['recap'])).recap).toBe('changed');
  });
});

describe('countChanges and helpers', () => {
  test('countChanges counts del+ins runs', () => {
    expect(countChanges(wordDiff('the Acme account', 'the Acme accounts'))).toBe(2);
    expect(countChanges([{ op: 'eq', text: 'x' }])).toBe(0);
    expect(countChanges([])).toBe(0);
  });
  test('rangesFromOps', () => {
    const ops = wordDiff('the Acme account', 'the Acme accounts');
    expect(rangesFromOps(ops, 'del')).toEqual([[9, 16]]);
    expect(rangesFromOps(ops, 'ins')).toEqual([[9, 17]]);
  });
  test('diffBlockLists pairs by order and handles extra blocks', () => {
    const d = diffBlockLists(['one', 'two'], ['one', 'too', 'three']);
    expect(d.map((x) => x.changes)).toEqual([0, 2, 1]);
    expect(d[2].hasOld).toBe(false);
  });
  test('diffBlockLists: an inserted block does not shift later unchanged blocks', () => {
    const d = diffBlockLists(['a b', 'claim on', 'idea', 'low'], ['a b', 'claim off', 'NEW box', 'idea', 'low']);
    expect(d.reduce((t, x) => t + x.changes, 0)).toBe(3); // on->off (2) + new box (1)
    const idea = d.find((x) => x.oi === 2);
    expect(idea.ni).toBe(3);
    expect(idea.changes).toBe(0);
  });
  test('insertIndex keeps newest first', () => {
    expect(insertIndex(['005', '003', '001'], '004')).toBe(1);
    expect(insertIndex(['005', '003'], '006')).toBe(0);
    expect(insertIndex(['005', '003'], '002')).toBe(2);
  });
  test('loadLabel', () => {
    expect(loadLabel(1)).toBe('↑ 1 new update, click to load');
    expect(loadLabel(3)).toBe('↑ 3 new updates, click to load');
  });
});
