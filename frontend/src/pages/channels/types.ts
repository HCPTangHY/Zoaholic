/* eslint-disable @typescript-eslint/no-explicit-any */

// 修改原因：Channels.tsx 被拆分后，原先的页面内类型需要成为跨文件共享契约。
// 修改方式：保留原有字段与注释，仅把 interface/type 改为 export，并补充列表分段类型。
// 目的：让 hooks 和组件按同一份类型编译，避免迁移过程中出现结构漂移。
// ========== Types ==========
export interface ApiKeyObj {
  key: string;
  disabled: boolean;
  label?: string;
}

export interface ModelMapping {
  from: string;
  to: string;
}

export interface HeaderEntry {
  key: string;
  value: string;
}

export interface SubChannelFormData {
  engine: string;
  models: string[];
  mappings: ModelMapping[];
  preferences: Record<string, any>;
  enabled?: boolean;
  remark?: string;
  base_url?: string;
  // 修改原因：OAuth 子渠道在完整编辑时也会复用同一份表单结构，需要保留独立 token endpoint 字段。
  // 修改方式：在子渠道表单数据中加入可选 token_url，并在序列化时只保存显式填写的值。
  // 目的：避免子渠道编辑时丢失用户配置的 OAuth token exchange/refresh 地址。
  token_url?: string;
  model_prefix?: string;
  _collapsed?: boolean;
}

export interface ProviderFormData {
  provider: string;
  remark: string;
  engine: string;
  base_url: string;
  // 修改原因：OAuth 渠道的 API 地址和 token endpoint 需要分开保存，不能继续复用 base_url。
  // 修改方式：在主渠道表单数据中加入 token_url，保存时随 provider payload 一起提交。
  // 目的：编辑已有渠道可以回显 token_url，新建或保存渠道时也能持久化该字段。
  token_url: string;
  api_keys: ApiKeyObj[];
  model_prefix: string;
  enabled: boolean;
  groups: string[];
  models: string[];
  mappings: ModelMapping[];
  // 注意：preferences 允许包含任意插件的 per-provider 配置。
  // 因此这里用 Record<string, any>，避免为每个插件都在 Channels 页面硬编码字段。
  preferences: Record<string, any>;
  sub_channels: SubChannelFormData[];
  // 修改原因：复制 OAuth 渠道保存后需要知道来源 provider 才能复制后端 token state。
  // 修改方式：在表单态保留一个以下划线开头的临时字段，正式保存 payload 手工组装时不写入该字段。
  // 目的：让复制流程能调用后端 copy-provider API，同时避免把前端临时标记持久化到 api.yaml。
  _copiedFrom?: string;
}

export type UiSlotValue = string | { script?: string; requires_plugin?: string };

export interface ChannelOption {
  id: string;
  type_name: string;
  default_base_url: string;
  default_token_url?: string;
  description?: string;
  // 修改原因：后端渠道注册表新增 OAuth 标记，前端应优先使用服务端返回值判断管理 UI 分支。
  // 修改方式：在 ChannelOption 中加入可选 is_oauth 字段，兼容旧后端未返回该字段的情况。
  // 目的：余额按钮和配置面板不再只依赖硬编码 OAuth 引擎集合。
  is_oauth?: boolean;
  // 修改原因：后端会把需要 provider 插件开关的 slot 输出为 {script, requires_plugin}，不再只返回脚本字符串。
  // 修改方式：把 ui_slots 值类型扩展为 UiSlotValue，导入弹窗仍只把纯字符串 import_placeholder 当文本使用。
  // 目的：让前端既兼容旧无条件 slot，又能按当前 provider.enabled_plugins 判断插件门控 slot。
  ui_slots?: Record<string, UiSlotValue>;
  // 修改原因：渠道级布尔配置开关（如 WebSocket 传输）由后端渠道注册声明，前端不再硬编码 engine 白名单。
  // 修改方式：ChannelOption 增加 preference_toggles 元数据数组。
  // 目的：渠道编辑器按当前 engine 的声明动态渲染开关。
  preference_toggles?: { key: string; label: string; tip?: string }[];
  source?: string;
}

export interface PluginOption {
  plugin_name: string;
  version: string;
  description: string;
  enabled: boolean;
  // 修改原因：后端新增 channel_inbound、channel_outbound 和 key_outbound 阶段，插件类型需要包含完整阶段字段。
  // 修改方式：在旧 request/response 字段之外增加新阶段数组，并保留 metadata 兼容插件参数 schema。
  // 目的：让渠道编辑器、完整配置面板和 Pipeline 共享同一份插件能力结构。
  inbound_interceptors?: any[];
  channel_inbound_interceptors?: any[];
  request_interceptors: any[];
  response_interceptors: any[];
  channel_outbound_interceptors?: any[];
  key_outbound_interceptors?: any[];
  balance_enrichers?: any[];
  metadata?: any;
}

// 修改原因：前端需要编辑 preferences.virtual_models 中的优先级链条结构。
// 修改方式：为虚拟模型配置和链条节点补充本页面内使用的类型定义。
// 目的：让列表展示、弹窗编辑和保存 payload 使用同一份数据形状。
export interface VirtualModelChainNode {
  type: 'model' | 'channel';
  value: string;
  model?: string;
}

export interface VirtualModelConfig {
  enabled: boolean;
  chain: VirtualModelChainNode[];
}

// 修改原因：新的虚拟模型画布需要展示渠道模型的对外名和上游名。
// 修改方式：把 provider.model 中的 string 与 {upstream: alias} 统一整理为页面可直接渲染的结构。
// 目的：拖拽模型节点、渠道节点模型选择和链条说明都使用同一套模型名称解释。
export interface ProviderModelOption {
  displayName: string;
  upstreamName: string;
  hasMapping: boolean;
}

// 修改原因：原生 Drag and Drop API 只能通过字符串传递拖拽数据。
// 修改方式：为左侧模型、左侧渠道和链条内部节点定义统一 payload 类型。
// 目的：drop 处理函数可以区分新建节点和链条排序，避免误把外部拖入当作内部排序。
export type VirtualDragPayload =
  | { source: 'panel-model'; modelName: string }
  | { source: 'panel-channel'; providerName: string }
  | { source: 'chain-node'; virtualName: string; fromIndex: number };


export interface QuotaGauge {
  // 修改原因：P2 阶段前端圆环需要消费后端统一 gauges，而不是继续按 OAuth 或普通余额写两套展示字段。
  // 修改方式：在页面内补齐 QuotaGauge 类型，保留后端可选字段，并额外允许 legacy fallback 写入 displayLabel。
  // 目的：让 QuotaRings 能直接渲染新 API 字段，也能兼容旧 BalanceResult 计算出的兜底圆环。
  id: string;
  label: string;
  role?: string | null;
  percent?: number | null;
  total?: number | null;
  available?: number | null;
  used?: number | null;
  tone?: string | null;
  resets_at?: string | null;
  unit?: string | null;
  display_mode?: 'percent' | 'amount' | 'quota';
  displayLabel?: string | null;
}

export interface RowQuota {
  // 修改原因：tier/plan 等标签必须由 quota_display 插槽决定，RowQuota 不应再承载可直接渲染的 badge 列表。
  // 修改方式：RowQuota 只保留圆环需要的 gauges，BalanceResult 原始字段仍会原样传给 slot 脚本。
  // 目的：删除通用 badge 展示路径，避免通用前端再次硬编码展示插件或渠道标签。
  gauges: QuotaGauge[];
}

export interface BalanceResult {
  supported: boolean;
  status?: string | null;
  value_type?: 'amount' | 'percent' | 'quota';
  total?: number | null;
  used?: number | null;
  available?: number | null;
  percent?: number | null;
  currency?: string | null;
  expires_at?: string | null;
  // 修改原因：P0 后端会在旧 BalanceResult 旁追加统一额度 UI 字段，前端 P2 需要优先读取 gauges。
  // 修改方式：在 BalanceResult 中保留 gauges、badges、metrics 和 extensions 的兼容字段，但渲染层不再消费 badges。
  // 目的：slot 脚本仍能读取后端原始扩展数据，通用前端不再硬编码 tier 或 plan 标签。
  gauges?: QuotaGauge[];
  badges?: any[];
  metrics?: Record<string, any>;
  extensions?: Record<string, any>;
  // 修改原因：双额度不再只属于 OAuth，普通 balance 结果也可能返回两个标准 quota 百分比。
  // 修改方式：把 quota_inner、quota_outer 和逐账号 results 保留在通用 BalanceResult 中。
  // 目的：让同一条渲染路径同时服务 OAuth 账号和普通 Key 的双弧展示。
  quota_inner?: number | null;
  quota_outer?: number | null;
  results?: Record<string, BalanceResult>;
  // 修改原因：oai_tier 会通过 balance_enricher 在普通余额结果中补充被动检测到的 OpenAI Tier 信息。
  // 修改方式：在通用 BalanceResult 类型中保留 tier、tpm、rpm 和检测元数据字段，并只交给 slot 脚本展示。
  // 目的：兼容旧余额结果，同时避免 Channels.tsx 直接把 Tier 渲染成通用标签。
  tier?: string | null;
  tpm?: number | null;
  rpm?: number | null;
  tier_detected_at?: number | null;
  tier_model?: string | null;
  raw?: any;
  error?: string | null;
}

// 修改原因：双弧模板只需要两个标准 quota 百分比和可选原始数据，不应绑定具体渠道字段。
// 修改方式：保留轻量结构供 OAuth 账号和普通 balance 结果共同归一化使用。
// 目的：为 RackOAuthRings、QuotaBorderOverlay 和 quota_display 插槽提供统一输入。
export interface OAuthQuota {
  quota_inner?: number;
  quota_outer?: number;
  raw?: any;
}

export interface ProviderListItem {
  p: any;
  idx: number;
}

export type Segment =
  | { type: 'active'; item: ProviderListItem }
  | { type: 'inactive'; items: ProviderListItem[]; startIndex: number };
