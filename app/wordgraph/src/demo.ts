// An invented, nameless sample scene for `index.html?demo`: 4 topics, 60 nodes.
const SAMPLE: Array<[string, Array<[string, string[]]>]> = [
  ['Release plan', [
    ['rollback', ['feature switch', 'smoke test', 'cache']],
    ['beta group', ['sign-up form', 'feedback loop']],
    ['changelog', ['wording', 'screenshots', 'version bump']],
    ['freeze date', ['buffer week', 'open bugs']],
  ]],
  ['Search latency', [
    ['index size', ['sharding', 'cold start', 'cache']],
    ['query planner', ['slow joins', 'explain output']],
    ['rollback', ['old ranking', 'traffic split']],
    ['dashboards', ['p95 chart', 'alert noise', 'error budget']],
  ]],
  ['Onboarding', [
    ['first run', ['empty state', 'sample data', 'tooltips']],
    ['permissions', ['microphone prompt', 'retry copy']],
    ['docs', ['quick start', 'screenshots', 'faq']],
    ['support inbox', ['triage rota', 'canned replies']],
  ]],
  ['Budget', [
    ['hardware', ['test devices', 'spare laptops']],
    ['vendors', ['renewal date', 'seat count', 'error budget']],
    ['travel', ['offsite', 'visa timing']],
    ['headcount', ['contractor', 'intern plan', 'feedback loop']],
  ]],
];

const slug = (label: string) => label.toLowerCase().replace(/[^a-z0-9]+/g, '-');

export function demoInsights() {
  const topics: unknown[] = [];
  const nodes: unknown[] = [];
  const shares = [0.34, 0.27, 0.22, 0.17];
  SAMPLE.forEach(([topicLabel, ideas], index) => {
    const id = `${slug(topicLabel)}-${index + 1}`;
    topics.push({ id, rootId: id, label: topicLabel, rank: index + 1, speakingMs: Math.round(shares[index]! * 1_800_000), visits: 1, share: shares[index] });
    nodes.push({ id, topicId: id, parentId: null, depth: 0, label: topicLabel, description: '', source: 'topic', evidence: [] });
    for (const [idea, details] of ideas) {
      const ideaId = `${id}/${slug(idea)}`;
      nodes.push({ id: ideaId, topicId: id, parentId: id, depth: 1, label: idea, description: '', source: 'meeting', evidence: [] });
      for (const detail of details) nodes.push({ id: `${ideaId}/${slug(detail)}`, topicId: id, parentId: ideaId, depth: 2, label: detail, description: '', source: 'meeting', evidence: [] });
    }
  });
  return { version: 1, topics, nodes };
}
