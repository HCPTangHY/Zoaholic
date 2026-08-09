let apiKeyClientIdCounter = 0;

/**
 * 为渠道编辑表单中的 Key 创建仅前端使用的稳定身份。
 *
 * 该值不会进入渠道保存 payload；它只用于 React 列表和异步测试结果关联，
 * 避免删除中间项后后继 Key 继承被删除项的组件/测试状态。
 */
export function createApiKeyClientId(): string {
  apiKeyClientIdCounter += 1;
  return `api-key-${Date.now().toString(36)}-${apiKeyClientIdCounter.toString(36)}`;
}
