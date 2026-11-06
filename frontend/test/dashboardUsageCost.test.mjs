import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8');
const summary = source.match(/const analysisTotalCost = (.*);/);
assert.ok(summary, '用量分析应汇总查询结果的历史费用');
const totalCost = new Function('analysisData', `return ${summary[1]};`);
const data = [
  { provider: 'a', model: 'model-a', recorded_cost: 0.002, total_cost: 999 },
  { provider: 'a:openai', model: 'model-b', recorded_cost: 0.003, total_cost: 999 },
];
assert.equal(totalCost(data), 0.005, '费用必须取筛选结果的历史价格，不能使用当前报价估算');
assert.equal(totalCost([data[1]]), 0.003, '更换筛选结果后应重新计算合计');
assert.equal(totalCost([]), 0, '空结果费用为零');

// 执行实际查询函数，确认费用与用量使用同一组时间、渠道及模型筛选。
const start = source.indexOf('  const fetchAnalysis = async');
const end = source.indexOf('\n  useEffect(', start);
assert.ok(start >= 0 && end > start);
const { outputText } = ts.transpileModule(source.slice(start, end), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
});
const createFetch = new Function('env', `
  const { token, timeRange, analysisStart, analysisEnd, analysisProviders, analysisModels, channelStats,
    apiFetch, setAnalysisData } = env;
  const setAnalysisLoading = () => {}, setAnalysisQueried = () => {}, setTrendData = () => {},
    setTokenBreakdown = () => {}, setTrendModelsRaw = () => {}, setTrendGranularity = () => {}, setTrendLoading = () => {};
  ${outputText}
  return fetchAnalysis;
`);
for (const customTime of [false, true]) {
  const updates = [];
  const urls = [];
  await createFetch({
    token: 'test', timeRange: 24, analysisProviders: ['a'], analysisModels: ['model-a', 'model-b'],
    analysisStart: customTime ? '2026-10-05T00:00:00Z' : '',
    analysisEnd: customTime ? '2026-10-07T00:00:00Z' : '',
    channelStats: [{ provider: 'a' }, { provider: 'a:openai' }, { provider: 'b' }],
    setAnalysisData: value => updates.push(value),
    apiFetch: async url => { urls.push(new URL(url, 'http://localhost')); return Response.json({ data }); },
  })();
  assert.deepEqual(updates, [[], data], '查询时清除旧结果，再展示新筛选结果');
  const params = urls.find(url => url.pathname.endsWith('/usage_analysis')).searchParams;
  assert.equal(params.get('provider'), 'a,a:openai');
  assert.equal(params.get('model'), 'model-a,model-b');
  assert.equal(params.get('hours'), customTime ? null : '24');
  if (customTime) {
    assert.equal(params.get('start_datetime'), '2026-10-05T00:00:00.000Z');
    assert.equal(params.get('end_datetime'), '2026-10-07T00:00:00.000Z');
  }
}
assert.match(source, /formatCost\(analysisTotalCost\)/, '应显示筛选后的费用合计');
assert.match(source, /formatCost\(entry\.recorded_cost\)/, '每个渠道和模型行应显示费用');
assert.match(source, /colSpan=\{7\}/, '空结果应覆盖新增费用列');
console.log('dashboard filtered usage cost checks passed');
