/* eslint-disable @typescript-eslint/no-explicit-any */

export type OAuthBatchSourceFormat = 'sub2api' | 'CPA-codex' | 'CPA-claude' | 'CPA-gemini' | 'unknown';
export type PlainKeyDelimiterMode = 'newline' | 'comma' | 'semicolon' | 'custom';

export interface OAuthBatchPreviewItem {
  index: number;
  keyId: string;
  sourceFormat: OAuthBatchSourceFormat;
  hasRefreshToken: boolean;
  expiresAt: string;
}

export interface OAuthBatchDetection {
  label: string;
  count: number;
  preview: OAuthBatchPreviewItem[];
}

export interface PlainKeyBatchPreview {
  total: number;
  unique: string[];
}

function firstString(...values: unknown[]): string {
  // 修改原因：不同导出格式把账号标识放在不同字段，预览表必须用统一兜底规则展示 key_id。
  // 修改方式：依次检查候选值，转换为字符串并 trim，返回第一个非空值。
  // 目的：让 sub2api、CPA Codex、Claude、Gemini 的预览结果与后端 key_id 选择尽量一致。
  for (const value of values) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return '';
}

function formatUnixSeconds(value: unknown): string {
  // 修改原因：sub2api 的 expires_at 是 unix 秒，直接展示数字不利于管理员判断是否过期。
  // 修改方式：可解析为数字时转换成 ISO 字符串；无法解析则保留原文本。
  // 目的：预览表中的过期时间和后端保存格式保持接近。
  if (value === null || value === undefined || value === '') return '';
  const num = Number(value);
  if (Number.isFinite(num)) {
    try {
      return new Date(num * 1000).toISOString();
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function formatExpiresIn(value: unknown): string {
  // 修改原因：CPA Claude 的 expires_in 是相对秒数，直接展示会让用户无法判断绝对过期时间。
  // 修改方式：将秒数加到当前时间后转换为 ISO 字符串，无法解析时返回空字符串。
  // 目的：预览表和后端导入逻辑都使用绝对过期时间语义。
  const seconds = Number(value || 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  return new Date(Date.now() + seconds * 1000).toISOString();
}

function detectCpaItem(item: any): OAuthBatchPreviewItem {
  // 修改原因：CPA 单文件有三种结构，前端预览需要给出具体格式类型和账号标识。
  // 修改方式：按 Gemini expiry、Claude account/organization/expires_in、Codex user/expires_at 的顺序检测。
  // 目的：用户在导入前可以确认 JSON 被识别为预期渠道格式。
  if (!item || typeof item !== 'object') {
    return { index: 0, keyId: 'unknown', sourceFormat: 'unknown', hasRefreshToken: false, expiresAt: '' };
  }

  if ('expiry' in item) {
    return {
      index: 0,
      keyId: firstString(item.email) || 'unknown',
      sourceFormat: 'CPA-gemini',
      hasRefreshToken: Boolean(item.refresh_token),
      expiresAt: firstString(item.expiry),
    };
  }

  const account = item.account && typeof item.account === 'object' ? item.account : {};
  const organization = item.organization && typeof item.organization === 'object' ? item.organization : {};
  if (account.email_address || account.uuid || organization.uuid || 'expires_in' in item) {
    return {
      index: 0,
      keyId: firstString(account.email_address, item.email, organization.uuid, account.uuid) || 'unknown',
      sourceFormat: 'CPA-claude',
      hasRefreshToken: Boolean(item.refresh_token),
      expiresAt: formatExpiresIn(item.expires_in),
    };
  }

  const user = item.user && typeof item.user === 'object' ? item.user : {};
  if (user.email || user.id || 'expires_at' in item) {
    return {
      index: 0,
      keyId: firstString(user.email, item.email, user.id) || 'unknown',
      sourceFormat: 'CPA-codex',
      hasRefreshToken: Boolean(item.refresh_token),
      expiresAt: firstString(item.expires_at),
    };
  }

  return {
    index: 0,
    keyId: firstString(item.key_id, item.email, item.name) || 'unknown',
    sourceFormat: 'unknown',
    hasRefreshToken: Boolean(item.refresh_token),
    expiresAt: firstString(item.expires_at, item.expiry),
  };
}

function previewSub2apiAccount(account: any): OAuthBatchPreviewItem {
  // 修改原因：sub2api 的账号数组每一项把凭据包在 credentials 下，预览不能只读取顶层字段。
  // 修改方式：从 name、credentials.email 和 chatgpt_account_id 中推断 key_id，并读取 credentials.refresh_token/expires_at。
  // 目的：让 sub2api 导入前能准确显示账号数量和关键字段完整性。
  const credentials = account?.credentials && typeof account.credentials === 'object' ? account.credentials : {};
  return {
    index: 0,
    keyId: firstString(account?.name, credentials.email, credentials.chatgpt_account_id, credentials.account_id) || 'unknown',
    sourceFormat: 'sub2api',
    hasRefreshToken: Boolean(credentials.refresh_token),
    expiresAt: formatUnixSeconds(credentials.expires_at),
  };
}

export function detectOAuthBatchFormat(data: unknown): OAuthBatchDetection {
  // 修改原因：前端批量导入面板需要在提交前快速告诉用户检测到了哪类导出格式。
  // 修改方式：按后端约定识别 accounts 数组、顶层 access_token 和顶层数组，并生成统一预览列表。
  // 目的：减少粘贴错误 JSON 或选错文件后才由后端报错的情况。
  if (data && typeof data === 'object' && !Array.isArray(data) && Array.isArray((data as any).accounts)) {
    const preview = (data as any).accounts.map((account: any, idx: number) => ({ ...previewSub2apiAccount(account), index: idx + 1 }));
    return { label: `检测到 sub2api 格式，共 ${preview.length} 个账号`, count: preview.length, preview };
  }

  if (Array.isArray(data)) {
    const preview = data.map((item, idx) => ({ ...detectCpaItem(item), index: idx + 1 }));
    return { label: `检测到 CPA 批量文件，共 ${preview.length} 个`, count: preview.length, preview };
  }

  if (data && typeof data === 'object' && 'access_token' in (data as any)) {
    return { label: '检测到 CPA 单文件', count: 1, preview: [{ ...detectCpaItem(data), index: 1 }] };
  }

  return { label: '未识别到支持的 OAuth 导出格式', count: 0, preview: [] };
}

function splitByDelimiter(text: string, delimiterMode: PlainKeyDelimiterMode, customDelimiter: string): string[] {
  // 修改原因：普通 Key 批量粘贴既要支持最常见的逐行，也要兼容逗号、分号和用户自定义分隔符。
  // 修改方式：按模式选择分隔表达式；自定义分隔符为空时回退为换行，避免空字符串导致逐字符拆分。
  // 目的：让批量粘贴解析行为明确可控。
  if (delimiterMode === 'comma') return text.split(',');
  if (delimiterMode === 'semicolon') return text.split(';');
  if (delimiterMode === 'custom') return customDelimiter ? text.split(customDelimiter) : text.split(/\r?\n|\r/);
  return text.split(/\r?\n|\r/);
}

export function parsePlainKeyBatch(text: string, delimiterMode: PlainKeyDelimiterMode, customDelimiter = ''): PlainKeyBatchPreview {
  // 修改原因：普通 Key 批量粘贴需要在确认前显示总数和去重后数量。
  // 修改方式：先按所选分隔符拆分、trim 和过滤空值，再用 Set 保序去重。
  // 目的：避免重复 Key 被批量加入表单，同时让用户知道实际会追加多少条。
  const items = splitByDelimiter(text, delimiterMode, customDelimiter).map(item => item.trim()).filter(Boolean);
  return { total: items.length, unique: Array.from(new Set(items)) };
}
