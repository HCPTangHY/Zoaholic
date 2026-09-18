import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 修改原因：日志详情展开区按数据流方向分为请求链路（用户→上游）和响应链路（上游→用户）两段手风琴。
// 修改方式：直接读取 Logs.tsx 源码，断言展开区保留请求/响应两个分组和七个数据手风琴。
// 目的：防止后续调整日志详情时把展开内容塞回单一长列表。
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(__dirname, '..');
const source = readFileSync(path.resolve(frontendRoot, 'src/pages/Logs.tsx'), 'utf8');

const start = source.indexOf('按数据流方向分组');
assert.notEqual(start, -1, '日志详情应保留按数据流方向分组的展开区');
const detailSection = source.slice(start, start + 3000);

assert.match(detailSection, /请求 →/, '展开区应保留请求链路分组');
assert.match(detailSection, /← 响应/, '展开区应保留响应链路分组');
assert.match(detailSection, /<JsonAccordion title="客户端请求头" data=\{detailLog\.request_headers\}/, '请求链路应包含客户端请求头');
assert.match(detailSection, /<BodyAccordion title="客户端请求体" data=\{detailLog\.request_body\}/, '请求链路应包含客户端请求体');
assert.match(detailSection, /<JsonAccordion title="上游请求头" data=\{detailLog\.upstream_request_headers\}/, '请求链路应包含上游请求头');
assert.match(detailSection, /<BodyAccordion title="上游请求体" data=\{detailLog\.upstream_request_body\}/, '请求链路应包含上游请求体');
assert.match(detailSection, /<JsonAccordion title="上游响应头" data=\{detailLog\.upstream_response_headers\}/, '响应链路应包含上游响应头');
assert.match(detailSection, /<BodyAccordion title="上游响应体" data=\{detailLog\.upstream_response_body\}/, '响应链路应包含上游响应体');
assert.match(detailSection, /<BodyAccordion title="客户端响应体" data=\{detailLog\.response_body\}/, '响应链路应包含客户端响应体');

console.log('logs accordion layout regression passed');
process.exit(0);
