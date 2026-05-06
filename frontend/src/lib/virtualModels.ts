export interface VirtualModelChainNode {
  type: 'model' | 'channel';
  value: string;
  model?: string;
}

export interface VirtualModelConfig {
  enabled?: boolean;
  chain?: VirtualModelChainNode[];
}

export interface VirtualProviderEntry {
  _isVirtual: true;
  provider: string;
  enabled: boolean;
  chain: VirtualModelChainNode[];
  engine: '虚拟路由';
  groups: string[];
  preferences: { weight: number };
}

export interface ProviderListItem<TProvider = any> {
  p: TProvider | VirtualProviderEntry;
  idx: number;
}

// 修改原因：虚拟模型现在要作为伪渠道混入渠道列表，页面多处都需要一致地读取渠道权重。
// 修改方式：优先读取 preferences.weight，其次读取 provider.weight，最后回退为 0。
// 目的：保证列表排序、测试和后续 UI 展示使用同一套权重规则。
export function getProviderWeight(provider: any): number {
  const weight = Number(provider?.preferences?.weight ?? provider?.weight ?? 0);
  return Number.isFinite(weight) ? weight : 0;
}

function readEnabledFlag(value: any, defaultValue = true): boolean {
  // 修改原因：子渠道的 enabled 可能继承主渠道，也可能以字符串形式从配置源进入前端。
  // 修改方式：统一解析 true/false 字符串，空值回退到调用方传入的默认值。
  // 目的：让虚拟模型左栏展示的可用状态尽量贴近后端 update_config 展开后的结果。
  if (value == null) return defaultValue;
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase();
    if (['false', '0', 'no', 'off'].includes(lowered)) return false;
    if (['true', '1', 'yes', 'on'].includes(lowered)) return true;
  }
  return value !== false;
}

function buildSubChannelProviderName(parentName: string, subChannel: any, subIdx: number, seenNames: Set<string>): string {
  // 修改原因：后端把子渠道展开成独立 provider，chain 节点必须保存同一个 provider 名才能命中路由。
  // 修改方式：复刻 utils._expand_sub_channels 的命名规则，优先使用 sub.provider，否则使用“主渠道:子渠道引擎”。
  // 目的：避免前端显示名和后端运行时 provider 名不一致，导致拖拽后的 channel 节点无法匹配。
  const subEngine = String(subChannel?.engine || '').trim();
  const baseName = String(subChannel?.provider || `${parentName}:${subEngine}`).trim();
  const providerName = seenNames.has(baseName) ? `${baseName}:${subIdx}` : baseName;
  seenNames.add(providerName);
  return providerName;
}

export function buildVirtualRoutingProviderItems<TProvider extends Record<string, any> = any>(
  providers: TProvider[] = [],
): any[] {
  // 修改原因：管理接口返回的是可持久化配置，已经移除了运行时展开的 _is_sub_channel provider。
  // 修改方式：在前端按后端规则把每个 provider.sub_channels 展开成 provider-like 条目，并保留主渠道条目。
  // 目的：虚拟模型编辑抽屉可以直接选择、拖拽子渠道，保存后 chain 使用后端能识别的 provider 名。
  return providers.flatMap(provider => {
    const items: any[] = [provider];
    const parentName = String(provider?.provider || '').trim();
    const subChannels = Array.isArray(provider?.sub_channels) ? provider.sub_channels : [];
    const seenSubProviderNames = new Set<string>();

    subChannels.forEach((subChannel: any, subIdx: number) => {
      if (!subChannel || typeof subChannel !== 'object') return;
      const subEngine = String(subChannel.engine || '').trim();
      if (!subEngine) return;

      const providerName = buildSubChannelProviderName(parentName, subChannel, subIdx, seenSubProviderNames);
      const parentPreferences = provider?.preferences && typeof provider.preferences === 'object' ? provider.preferences : {};
      const subPreferences = subChannel.preferences && typeof subChannel.preferences === 'object' ? subChannel.preferences : {};
      const rawSubModels = Array.isArray(subChannel.model)
        ? subChannel.model
        : Array.isArray(subChannel.models)
          ? subChannel.models
          : [];

      items.push({
        ...provider,
        ...subChannel,
        provider: providerName,
        engine: subEngine,
        api: subChannel.api || provider?.api,
        base_url: subChannel.base_url || provider?.base_url || '',
        model: rawSubModels,
        model_prefix: subChannel.model_prefix || provider?.model_prefix || '',
        preferences: { ...parentPreferences, ...subPreferences },
        groups: subChannel.groups || provider?.groups || provider?.group || ['default'],
        enabled: readEnabledFlag(subChannel.enabled, readEnabledFlag(provider?.enabled, true)),
        remark: subChannel.remark || `[子渠道] ${parentName} → ${subEngine}`,
        sub_channels: [],
        _parent_provider: parentName,
        _is_sub_channel: true,
      });
    });

    return items;
  });
}

export function buildVirtualProviderPanelItems<TProvider extends Record<string, any> = any>(
  providers: TProvider[] = [],
): any[] {
  // 修改原因：移动端重新显示渠道面板后，单纯全局按 weight 排序会把子渠道从主渠道旁边拆走。
  // 修改方式：先按主渠道 weight 降序排列分组，再把同一主渠道下的子渠道按自身 weight 降序追加在主渠道后。
  // 目的：让渠道面板和渠道下拉既符合权重优先级，又保留主渠道与子渠道的层级关系。
  return providers
    .map(provider => {
      const [parent, ...children] = buildVirtualRoutingProviderItems([provider]);
      return {
        parent,
        children: children.sort((a, b) => getProviderWeight(b) - getProviderWeight(a)),
      };
    })
    .filter(group => group.parent)
    .sort((a, b) => getProviderWeight(b.parent) - getProviderWeight(a.parent))
    .flatMap(group => [group.parent, ...group.children].filter(item => item?.enabled !== false));
}

// 修改原因：虚拟模型不是真实渠道，但手风琴渲染和测试弹窗仍需要稳定的 provider-like 数据形状。
// 修改方式：把 preferences.virtual_models 的每个条目转换成带 _isVirtual 标记的伪 provider，但不再混入主渠道列表。
// 目的：让虚拟模型由独立手风琴置顶展示，同时保留名称排序、启用状态和 chain 摘要的数据来源。
export function buildVirtualProviderEntries(virtualModels: Record<string, VirtualModelConfig> = {}): VirtualProviderEntry[] {
  return Object.entries(virtualModels)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, config]) => ({
      _isVirtual: true as const,
      provider: name,
      enabled: config?.enabled !== false,
      chain: Array.isArray(config?.chain) ? config.chain : [],
      engine: '虚拟路由' as const,
      groups: ['虚拟模型'],
      preferences: { weight: Infinity },
    }));
}

// 修改原因：ChannelTestDialog 只认识 provider.model 列表，虚拟路由手风琴需要把一个或多个虚拟模型交给同一个测试弹窗。
// 修改方式：把虚拟 entries 转成临时测试 provider，并附加 _virtual_route_test 标记和虚拟模型快照供后端识别。
// 目的：标题行可以批量列出所有虚拟模型，子行也可以只用单个虚拟模型名发起完整虚拟路由测试。
export function buildVirtualRouteTestProvider(virtualEntries: VirtualProviderEntry[] = []): any | null {
  const entries = virtualEntries.filter(entry => entry?.provider);
  if (entries.length === 0) return null;

  const virtualModels = Object.fromEntries(entries.map(entry => [
    entry.provider,
    { enabled: entry.enabled !== false, chain: Array.isArray(entry.chain) ? entry.chain : [] },
  ]));
  const modelNames = entries.map(entry => entry.provider);

  return {
    provider: entries.length === 1 ? entries[0].provider : `虚拟路由 (${entries.length})`,
    engine: 'openai',
    model: modelNames,
    models: modelNames,
    enabled: true,
    groups: ['虚拟模型'],
    preferences: { virtual_models: virtualModels },
    _virtual_route_test: true,
  };
}

// 修改原因：虚拟模型已改为列表顶部独立手风琴，不能再进入主列表 segments 的活跃/不活跃分段。
// 修改方式：buildProviderListItems 只排序真实渠道并保留原始 providers 下标；第二个参数仅为兼容旧调用保留。
// 目的：普通渠道的编辑、删除、调权和不活跃折叠都只作用于真实渠道，虚拟模型由专用渲染函数处理。
export function buildProviderListItems<TProvider = any>(
  providers: TProvider[] = [],
  _virtualModels: Record<string, VirtualModelConfig> = {},
): ProviderListItem<TProvider>[] {
  const realItems = providers
    .map((p, idx) => ({ p, idx }))
    .sort((a, b) => getProviderWeight(b.p) - getProviderWeight(a.p));
  return realItems;
}

// 修改原因：虚拟模型卡片折叠态需要一行展示 chain 摘要，不能复用弹窗中的节点表单。
// 修改方式：model 节点展示模型名，channel 节点展示渠道名，若指定模型则继续追加该模型名。
// 目的：让用户在渠道列表中不展开编辑器也能快速判断虚拟路由顺序。
export function summarizeVirtualChain(
  chain: VirtualModelChainNode[] = [],
  virtualName = '',
  maxParts = 6,
): string {
  const parts = chain.flatMap(node => {
    const value = String(node?.value || '').trim();
    if (!value) return [];
    if (node.type !== 'channel') return [value];
    const model = String(node.model || '').trim();
    if (!model || model === virtualName) return [value];
    return [value, model];
  });

  if (parts.length === 0) return '未配置链条';
  const visible = parts.slice(0, Math.max(1, maxParts));
  return `${visible.join(' → ')}${parts.length > visible.length ? ' …' : ''}`;
}

// 修改原因：渲染和事件分支需要快速判断当前行是不是虚拟模型伪渠道。
// 修改方式：检查 _isVirtual 标记是否为 true。
// 目的：让 TypeScript 在虚拟行分支中获得更明确的数据形状。
export function isVirtualProviderEntry(provider: any): provider is VirtualProviderEntry {
  return provider?._isVirtual === true;
}
