/**
 * Config View - Provider Configuration
 * 配置视图 - 渠道配置管理
 */
const ConfigView = {
    _apiConfig: null,
    _container: null,

    render(container) {
        ConfigView._container = container;
        const header = UI.el("div", "flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6");
        const titleSection = UI.el("div");
        titleSection.appendChild(UI.el("h2", "text-display-small text-md-on-surface", "渠道配置"));
        titleSection.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant mt-2", "管理上游 AI 服务提供商和模型映射"));
        header.appendChild(titleSection);

        const addBtn = UI.btn("添加渠道", () => ConfigView.openConfigSideSheet(null), "filled", "add");
        header.appendChild(addBtn);
        container.appendChild(header);

        const loading = UI.spinner();
        container.appendChild(loading);

        ConfigView._loadProviders(container, loading);
    },

    /**
     * 刷新视图并尝试恢复滚动位置
     */
    refresh() {
        const viewport = document.getElementById("content-viewport");
        const scrollTop = viewport ? viewport.scrollTop : 0;
        
        if (ConfigView._container) {
            ConfigView.render(ConfigView._container);
            
            // 恢复滚动位置
            if (viewport) {
                requestAnimationFrame(() => {
                    viewport.scrollTop = scrollTop;
                });
            }
        } else {
            Views.render("config");
        }
    },

    async _loadProviders(container, loading) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = adminKey ? { Authorization: `Bearer ${adminKey}` } : {};

        let apiConfig = null;
        let providers = [];

        try {
            const res = await fetch("/v1/api_config", { headers });
            if (res.ok) {
                const json = await res.json();
                apiConfig = json.api_config || json;
                if (apiConfig && Array.isArray(apiConfig.providers)) {
                    providers = apiConfig.providers;
                }
            }
        } catch (e) {
            console.error("Failed to load /v1/api_config:", e);
        }

        if ((!providers || !providers.length) && MockData?.providers) {
            providers = MockData.providers.slice();
            apiConfig = apiConfig || { providers: providers.slice() };
        }

        ConfigView._apiConfig = apiConfig || { providers: providers || [] };
        loading.remove();

        ConfigView._renderTable(container, providers);
    },

    _renderTable(container, providers) {
        // 桌面端：表格视图
        const tableWrapper = UI.el("div", "hidden md:block");
        const tableCard = UI.card("outlined", "overflow-hidden p-0");
        const table = UI.el("table", "w-full text-left");

        // 默认按优先级（weight）降序排序，高优先级在前；次级按名称升序
        const sortedProviders = (providers || []).slice().sort((a, b) => {
            const wa = (a.preferences?.weight ?? a.weight ?? 0);
            const wb = (b.preferences?.weight ?? b.weight ?? 0);
            if (wb !== wa) return wb - wa;
            const na = (a.provider || a.name || "").toLowerCase();
            const nb = (b.provider || b.name || "").toLowerCase();
            return na.localeCompare(nb);
        });

        const thead = UI.el("thead", "bg-md-surface-container-highest");
        thead.innerHTML = `<tr>
            <th class="px-4 py-3 text-label-large text-md-on-surface">名称</th>
            <th class="px-4 py-3 text-label-large text-md-on-surface">分组</th>
            <th class="px-4 py-3 text-label-large text-md-on-surface">类型</th>
            <th class="px-4 py-3 text-label-large text-md-on-surface">插件</th>
            <th class="px-4 py-3 text-center text-label-large text-md-on-surface">状态</th>
            <th class="px-4 py-3 text-center text-label-large text-md-on-surface">优先级</th>
            <th class="px-4 py-3 text-right text-label-large text-md-on-surface">操作</th>
        </tr>`;
        table.appendChild(thead);

        const tbody = UI.el("tbody", "divide-y divide-md-outline-variant");

        if (!sortedProviders || sortedProviders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center py-12 text-body-medium text-md-on-surface-variant">暂无渠道配置，点击"添加渠道"开始配置</td></tr>';
        } else {
            sortedProviders.forEach((provider) => {
                const originalIndex = (providers || []).indexOf(provider);
                const tr = ConfigView._createProviderRow(provider, originalIndex, providers);
                tbody.appendChild(tr);
            });
        }

        table.appendChild(tbody);
        tableCard.appendChild(table);
        tableWrapper.appendChild(tableCard);
        container.appendChild(tableWrapper);

        // 移动端：卡片列表视图
        const mobileWrapper = UI.el("div", "md:hidden flex flex-col gap-3");
        
        if (!sortedProviders || sortedProviders.length === 0) {
            const emptyCard = UI.card("outlined", "p-8 text-center");
            emptyCard.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant", "暂无渠道配置，点击\"添加渠道\"开始配置"));
            mobileWrapper.appendChild(emptyCard);
        } else {
            sortedProviders.forEach((provider) => {
                const originalIndex = (providers || []).indexOf(provider);
                const card = ConfigView._createProviderCard(provider, originalIndex, providers);
                mobileWrapper.appendChild(card);
            });
        }
        
        container.appendChild(mobileWrapper);
    },

    /**
     * 创建移动端渠道卡片
     */
    _createProviderCard(provider, index, providers) {
        const isEnabled = provider.enabled !== false;
        
        // 根据启用状态设置卡片样式 - 禁用时添加左侧边框和降低不透明度
        const cardClasses = isEnabled
            ? "p-4"
            : "p-4 opacity-60 border-l-4 border-l-md-error/50 bg-md-surface-container/30";
        const card = UI.card("outlined", cardClasses);
        
        // 头部：名称 + 状态
        const header = UI.el("div", "flex items-center justify-between mb-3");
        const nameSection = UI.el("div", "flex items-center gap-2");
        const nameIconClasses = isEnabled ? "text-md-primary text-xl" : "text-md-on-surface-variant/50 text-xl";
        const nameTextClasses = isEnabled
            ? "text-title-medium text-md-on-surface font-medium"
            : "text-title-medium text-md-on-surface-variant font-medium";
        nameSection.appendChild(UI.icon("dns", nameIconClasses));
        nameSection.appendChild(UI.el("span", nameTextClasses, provider.provider || provider.name || ""));
        header.appendChild(nameSection);
        
        // 状态标签 - 禁用时使用醒目的错误色系
        const statusChip = UI.el("span", `inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full text-label-medium font-medium ${
            isEnabled
                ? "bg-md-success-container text-md-on-success-container"
                : "bg-md-error-container text-md-on-error-container"
        }`);
        statusChip.appendChild(UI.icon(isEnabled ? "check_circle" : "block", "text-base"));
        statusChip.appendChild(document.createTextNode(isEnabled ? "启用" : "已禁用"));
        header.appendChild(statusChip);
        card.appendChild(header);
        
        // 信息区域：分组、类型、插件、优先级
        const infoSection = UI.el("div", "flex flex-wrap gap-2 mb-4");
        
        // 分组 - 多组支持
        const groups = Array.isArray(provider.groups)
            ? provider.groups
            : (provider.group ? [provider.group] : (provider.preferences?.group ? [provider.preferences.group] : ["默认"]));
        groups.forEach(g => {
            const chip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-medium");
            chip.appendChild(UI.icon("folder", "text-sm"));
            chip.appendChild(document.createTextNode(g));
            infoSection.appendChild(chip);
        });
        
        // 类型
        const engine = provider.engine || "openai";
        const typeChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-tertiary-container text-md-on-tertiary-container text-label-medium font-mono");
        typeChip.appendChild(UI.icon("memory", "text-sm"));
        typeChip.appendChild(document.createTextNode(engine));
        infoSection.appendChild(typeChip);
        
        // 插件 - 展示启用的插件数量和名称
        const enabledPlugins = provider.preferences?.enabled_plugins || [];
        if (enabledPlugins.length > 0) {
            const pluginChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-primary-container text-md-on-primary-container text-label-medium cursor-pointer hover:shadow-md-1 transition-all");
            pluginChip.appendChild(UI.icon("extension", "text-sm"));
            // 解析插件名称（去除 :options 部分）
            const pluginNames = enabledPlugins.map(p => {
                const colonIdx = p.indexOf(":");
                return colonIdx === -1 ? p : p.substring(0, colonIdx);
            });
            if (pluginNames.length <= 2) {
                pluginChip.appendChild(document.createTextNode(pluginNames.join(", ")));
            } else {
                pluginChip.appendChild(document.createTextNode(`${pluginNames[0]} +${pluginNames.length - 1}`));
            }
            pluginChip.title = `已启用插件: ${pluginNames.join(", ")}`;
            pluginChip.onclick = (e) => {
                e.stopPropagation();
                ConfigView.openConfigSideSheet({ index: originalIndex, provider });
            };
            infoSection.appendChild(pluginChip);
        }
        
        // 优先级 - 可编辑输入框
        const weight = (provider.preferences?.weight) || provider.weight || 0;
        const priorityChip = UI.el("div", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-surface-container-high text-md-on-surface-variant text-label-medium");
        priorityChip.appendChild(UI.icon("priority_high", "text-sm"));
        priorityChip.appendChild(document.createTextNode("优先级: "));
        const priorityInput = document.createElement("input");
        priorityInput.type = "number";
        priorityInput.min = "0";
        priorityInput.value = weight;
        priorityInput.className = "w-12 px-1 py-0.5 bg-transparent border-b border-md-outline text-body-small text-md-on-surface text-center focus:outline-none focus:border-md-primary focus:border-b-2 transition-all";
        priorityInput.onclick = (e) => e.stopPropagation();
        priorityInput.onchange = (e) => {
            const newWeight = parseInt(e.target.value) || 0;
            ConfigView._updateProviderWeight(index, providers, newWeight);
        };
        priorityChip.appendChild(priorityInput);
        infoSection.appendChild(priorityChip);
        
        card.appendChild(infoSection);
        
        // 操作按钮组
        const actionsSection = UI.el("div", "flex items-center justify-end gap-1 pt-2 border-t border-md-outline-variant");
        
        // 测试按钮
        const testBtn = UI.iconBtn("speed", () => ConfigView._testProvider(provider, index), "standard", { tooltip: "测试" });
        testBtn.classList.add("text-md-primary");
        actionsSection.appendChild(testBtn);
        
        // 删除按钮
        const deleteBtn = UI.iconBtn("delete", () => ConfigView._deleteProvider(index, providers), "standard", { tooltip: "删除" });
        deleteBtn.classList.add("text-md-error");
        actionsSection.appendChild(deleteBtn);
        
        // 启用/禁用按钮
        const toggleBtn = UI.iconBtn(
            isEnabled ? "toggle_on" : "toggle_off",
            () => ConfigView._toggleProvider(index, providers, !isEnabled),
            "standard",
            { tooltip: isEnabled ? "禁用" : "启用" }
        );
        toggleBtn.classList.add(isEnabled ? "text-md-success" : "text-md-on-surface-variant");
        actionsSection.appendChild(toggleBtn);
        
        // 编辑按钮
        const editBtn = UI.iconBtn("edit", () => ConfigView.openConfigSideSheet({ index, provider }), "standard", { tooltip: "编辑" });
        actionsSection.appendChild(editBtn);
        
        // 复制按钮
        const copyBtn = UI.iconBtn("content_copy", () => ConfigView._copyProvider(provider), "standard", { tooltip: "复制" });
        actionsSection.appendChild(copyBtn);
        
        card.appendChild(actionsSection);
        
        return card;
    },

    _createProviderRow(provider, index, providers) {
        const isEnabled = provider.enabled !== false;
        
        // 根据启用状态设置行样式
        const rowClasses = isEnabled
            ? "hover:bg-md-surface-container transition-colors group"
            : "bg-md-surface-container/30 opacity-60 transition-colors group border-l-4 border-l-md-error/50";
        const tr = UI.el("tr", rowClasses);

        // 渠道名称
        const name = provider.provider || provider.name || "";
        const nameTd = UI.el("td", "px-4 py-3");
        const nameContent = UI.el("div", "flex items-center gap-2");
        const nameIconClasses = isEnabled
            ? "text-md-on-surface-variant group-hover:text-md-primary transition-colors text-lg"
            : "text-md-on-surface-variant/50 text-lg";
        const nameTextClasses = isEnabled
            ? "text-body-large text-md-on-surface font-medium"
            : "text-body-large text-md-on-surface-variant font-medium";
        nameContent.appendChild(UI.icon("dns", nameIconClasses));
        nameContent.appendChild(UI.el("span", nameTextClasses, name));
        nameTd.appendChild(nameContent);

        // 分组 - 多组支持
        const groups = Array.isArray(provider.groups)
            ? provider.groups
            : (provider.group ? [provider.group] : (provider.preferences?.group ? [provider.preferences.group] : ["默认"]));
        const groupTd = UI.el("td", "px-4 py-3");
        groups.forEach(g => {
            const chip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 mr-1 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-medium");
            chip.appendChild(UI.icon("folder", "text-sm"));
            chip.appendChild(document.createTextNode(g));
            groupTd.appendChild(chip);
        });

        // 类型 (engine) - 使用带图标的 Chip 样式
        const engine = provider.engine || "openai";
        const typeTd = UI.el("td", "px-4 py-3");
        const typeChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-tertiary-container text-md-on-tertiary-container text-label-medium font-mono");
        typeChip.appendChild(UI.icon("memory", "text-sm"));
        typeChip.appendChild(document.createTextNode(engine));
        typeTd.appendChild(typeChip);

        // 插件 - 展示启用的插件
        const pluginsTd = UI.el("td", "px-4 py-3");
        const enabledPlugins = provider.preferences?.enabled_plugins || [];
        if (enabledPlugins.length > 0) {
            const pluginsWrapper = UI.el("div", "flex flex-wrap gap-1");
            // 解析插件名称（去除 :options 部分）
            const pluginNames = enabledPlugins.map(p => {
                const colonIdx = p.indexOf(":");
                return colonIdx === -1 ? p : p.substring(0, colonIdx);
            });
            // 最多显示 2 个插件 Chip，其余折叠
            const displayPlugins = pluginNames.slice(0, 2);
            const remainingCount = pluginNames.length - displayPlugins.length;
            
            displayPlugins.forEach(pluginName => {
                const chip = UI.el("span", "inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full bg-md-primary-container text-md-on-primary-container text-label-small cursor-pointer hover:shadow-md-1 transition-all");
                chip.appendChild(UI.icon("extension", "text-xs"));
                chip.appendChild(document.createTextNode(pluginName));
                chip.title = pluginName;
                chip.onclick = (e) => {
                    e.stopPropagation();
                    ConfigView.openConfigSideSheet({ index, provider });
                };
                pluginsWrapper.appendChild(chip);
            });
            
            if (remainingCount > 0) {
                const moreChip = UI.el("span", "inline-flex items-center px-2 py-0.5 rounded-md-full bg-md-surface-container-high text-md-on-surface-variant text-label-small cursor-pointer hover:bg-md-surface-container transition-colors");
                moreChip.appendChild(document.createTextNode(`+${remainingCount}`));
                moreChip.title = `还有 ${remainingCount} 个插件: ${pluginNames.slice(2).join(", ")}`;
                moreChip.onclick = (e) => {
                    e.stopPropagation();
                    ConfigView.openConfigSideSheet({ index, provider });
                };
                pluginsWrapper.appendChild(moreChip);
            }
            
            pluginsTd.appendChild(pluginsWrapper);
        } else {
            const emptyText = UI.el("span", "text-body-small text-md-on-surface-variant/50", "—");
            pluginsTd.appendChild(emptyText);
        }

        // 状态 - 禁用时使用更醒目的错误色系
        const statusTd = UI.el("td", "px-4 py-3 text-center");
        const statusChip = UI.el("span", `inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full text-label-medium font-medium ${
            isEnabled
                ? "bg-md-success-container text-md-on-success-container"
                : "bg-md-error-container text-md-on-error-container"
        }`);
        statusChip.appendChild(UI.icon(isEnabled ? "check_circle" : "block", "text-base"));
        statusChip.appendChild(document.createTextNode(isEnabled ? "启用" : "已禁用"));
        statusTd.appendChild(statusChip);

        // 优先级/权重 - 可编辑输入框
        const weight = (provider.preferences?.weight) || provider.weight || 0;
        const priorityTd = UI.el("td", "px-4 py-3 text-center");
        const priorityInput = document.createElement("input");
        priorityInput.type = "number";
        priorityInput.min = "0";
        priorityInput.value = weight;
        priorityInput.className = "w-16 md-input md-input-compact md-input-center md-input-mono text-body-medium";
        priorityInput.onclick = (e) => e.stopPropagation();
        priorityInput.onchange = (e) => {
            const newWeight = parseInt(e.target.value) || 0;
            ConfigView._updateProviderWeight(index, providers, newWeight);
        };
        priorityTd.appendChild(priorityInput);

        // 操作按钮组
        const actionsTd = UI.el("td", "px-4 py-3");
        const actionsGroup = UI.el("div", "flex items-center justify-end gap-1 md-hover-fade-in");

        // 测试按钮
        const testBtn = UI.iconBtn("speed", () => ConfigView._testProvider(provider, index), "standard", { tooltip: "测试连接" });
        testBtn.classList.add("text-md-primary");
        actionsGroup.appendChild(testBtn);

        // 删除按钮
        const deleteBtn = UI.iconBtn("delete", () => ConfigView._deleteProvider(index, providers), "standard", { tooltip: "删除" });
        deleteBtn.classList.add("text-md-error");
        actionsGroup.appendChild(deleteBtn);

        // 启用/禁用按钮
        const toggleBtn = UI.iconBtn(
            isEnabled ? "toggle_on" : "toggle_off",
            () => ConfigView._toggleProvider(index, providers, !isEnabled),
            "standard",
            { tooltip: isEnabled ? "禁用" : "启用" }
        );
        toggleBtn.classList.add(isEnabled ? "text-md-success" : "text-md-on-surface-variant");
        actionsGroup.appendChild(toggleBtn);

        // 编辑按钮
        const editBtn = UI.iconBtn("edit", () => ConfigView.openConfigSideSheet({ index, provider }), "standard", { tooltip: "编辑" });
        actionsGroup.appendChild(editBtn);

        // 复制按钮
        const copyBtn = UI.iconBtn("content_copy", () => ConfigView._copyProvider(provider), "standard", { tooltip: "复制" });
        actionsGroup.appendChild(copyBtn);

        actionsTd.appendChild(actionsGroup);

        tr.appendChild(nameTd);
        tr.appendChild(groupTd);
        tr.appendChild(typeTd);
        tr.appendChild(pluginsTd);
        tr.appendChild(statusTd);
        tr.appendChild(priorityTd);
        tr.appendChild(actionsTd);
        return tr;
    },

    /**
     * 测试渠道连接 - 打开测试模态框
     */
    async _testProvider(provider, index) {
        const providerName = provider.provider || provider.name || "未命名渠道";
        
        // 获取模型列表
        const modelArray = Array.isArray(provider.model) ? provider.model : (provider.models || []);
        const aliasToUpstream = new Map();
        
        modelArray.forEach(m => {
            if (typeof m === "string") {
                // 字符串模型：别名和上游都是自己
                aliasToUpstream.set(m, m);
            } else if (typeof m === "object") {
                // 映射格式: { 上游模型名(key): 别名(value) }
                Object.entries(m).forEach(([upstream, alias]) => {
                    if (typeof alias === "string") {
                        aliasToUpstream.set(alias, upstream);
                    }
                });
            }
        });
        
        // 最终模型列表：存储 { display: 别名, upstream: 上游模型名 }
        const models = [];
        aliasToUpstream.forEach((upstream, alias) => {
            models.push({ display: alias, upstream: upstream });
        });

        if (models.length === 0) {
            UI.snackbar("该渠道没有配置模型", null, null, { variant: "error" });
            return;
        }

        // 打开测试模态框
        ConfigView._openTestDialog(provider, providerName, models);
    },

    /**
     * 打开渠道测试模态框
     * @param {Object} provider - 渠道配置
     * @param {string} providerName - 渠道名称
     * @param {Array<{display: string, upstream: string}>} models - 模型列表（display=别名, upstream=上游模型名）
     */
    _openTestDialog(provider, providerName, models) {
        // 测试状态管理
        const testState = {
            concurrency: 3,
            searchKeyword: "",
            results: new Map(), // display -> { status: 'pending'|'testing'|'success'|'error', latency: number, error: string }
            isRunning: false,
            abortController: null,
        };

        // 初始化所有模型状态（按别名作为 key）
        models.forEach(m => {
            testState.results.set(m.display, { status: "pending", latency: null, error: null });
        });

        let dialogRef = null;

        const renderDialogContent = () => {
            const content = UI.el("div", "flex flex-col gap-3");

            // 统一复制方法（含回退）
            const copyModelName = async (text) => {
                const t = text || "";
                if (!t) return;
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(t);
                    } else {
                        const ta = document.createElement("textarea");
                        ta.value = t;
                        ta.style.position = "fixed";
                        ta.style.left = "-10000px";
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand("copy");
                        document.body.removeChild(ta);
                    }
                    UI.snackbar(`已复制: ${t}`, null, null, { variant: "success" });
                } catch (err) {
                    UI.snackbar("复制失败", null, null, { variant: "error" });
                }
            };

            // 顶部控制栏：全部测试 + 并发数 + 搜索（同一行，移动端自动换行）
            const controlSection = UI.el("div", "flex flex-wrap items-center gap-2");

            // 全部测试按钮
            const startAllBtn = UI.btn("全部测试", null, "filled", "play_arrow");
            startAllBtn.classList.add("flex-shrink-0");
            const stopBtn = UI.btn("停止", null, "outlined", "stop");
            stopBtn.classList.add("flex-shrink-0");
            stopBtn.style.display = "none";
            controlSection.appendChild(startAllBtn);
            controlSection.appendChild(stopBtn);

            // 并发数设置
            const concurrencyWrapper = UI.el("div", "flex items-center gap-1.5 flex-shrink-0");
            concurrencyWrapper.appendChild(UI.el("span", "text-body-small text-md-on-surface-variant", "并发:"));
            const concurrencyInput = document.createElement("input");
            concurrencyInput.type = "number";
            concurrencyInput.min = "1";
            concurrencyInput.max = "10";
            concurrencyInput.value = testState.concurrency;
            concurrencyInput.className = "w-12 md-input md-input-compact md-input-center text-body-small";
            concurrencyInput.oninput = (e) => {
                const val = parseInt(e.target.value) || 1;
                testState.concurrency = Math.max(1, Math.min(10, val));
            };
            concurrencyWrapper.appendChild(concurrencyInput);
            controlSection.appendChild(concurrencyWrapper);

            // 搜索框
            const searchWrapper = UI.el("div", "relative flex-1 min-w-[120px]");
            const searchIcon = UI.icon("search", "absolute left-2.5 top-1/2 -translate-y-1/2 text-md-on-surface-variant text-base");
            const searchInput = document.createElement("input");
            searchInput.type = "text";
            searchInput.placeholder = "搜索...";
            searchInput.className = "w-full pl-8 pr-3 md-input md-input-compact md-input-pill text-body-small";
            searchWrapper.appendChild(searchIcon);
            searchWrapper.appendChild(searchInput);
            controlSection.appendChild(searchWrapper);

            content.appendChild(controlSection);

            // MD3 List 容器 - 统一的列表视图
            const listContainer = UI.el("div", "max-h-[400px] overflow-y-auto");
            const list = UI.el("ul", "divide-y divide-md-outline-variant");
            listContainer.appendChild(list);
            content.appendChild(listContainer);

            // 存储行引用
            const rowRefs = new Map();

            // 渲染 MD3 List Items
            const renderRows = () => {
                list.innerHTML = "";
                const keyword = testState.searchKeyword.toLowerCase();
                
                models.forEach(modelInfo => {
                    const { display, upstream } = modelInfo;
                    // 搜索时匹配别名
                    if (keyword && !display.toLowerCase().includes(keyword)) {
                        return;
                    }

                    const result = testState.results.get(display);
                    
                    // MD3 List Item: 56dp 高度，标准结构
                    const listItem = UI.el("li", "flex items-center h-14 px-4 hover:bg-md-surface-container-low active:bg-md-surface-container transition-colors cursor-default");
                    // 行空白区域点击亦可复制（排除按钮/输入控件）
                    listItem.setAttribute("data-tooltip", "点击复制模型名");
                    listItem.onclick = (e) => {
                        if (!e.target.closest("button") && !e.target.closest("input")) {
                            copyModelName(display);
                        }
                    };
                    
                    // Leading: 状态图标（24x24）
                    const leading = UI.el("div", "w-10 h-10 flex items-center justify-center flex-shrink-0 -ml-2");
                    const statusIcon = ConfigView._createStatusIcon(result.status);
                    leading.appendChild(statusIcon);
                    listItem.appendChild(leading);
                    
                    // Content: 模型名称 + 辅助信息
                    const contentArea = UI.el("div", "flex-1 min-w-0 ml-2");
                    // 内容区域点击复制
                    contentArea.setAttribute("data-tooltip", "点击复制模型名");
                    contentArea.onclick = (e) => {
                        e.stopPropagation();
                        copyModelName(display);
                    };
                    
                    // Headline: 显示 别名(上游名)，如果相同只显示一个
                    const displayText = display !== upstream ? `${display}(${upstream})` : display;
                    const headline = UI.el("div", "font-mono text-body-medium text-md-on-surface truncate", displayText);
                    headline.title = displayText;
                    headline.setAttribute("data-tooltip", "点击复制模型名");
                    headline.onclick = (e) => {
                        e.stopPropagation();
                        copyModelName(display);
                    };
                    contentArea.appendChild(headline);
                    
                    // Supporting text: 耗时或错误信息
                    const supporting = UI.el("div", "text-body-small text-md-on-surface-variant truncate h-5");
                    supporting.appendChild(ConfigView._createSupportingText(result));
                    contentArea.appendChild(supporting);
                    
                    listItem.appendChild(contentArea);
                    
                    // Trailing: 测试按钮（测试时使用上游模型名）
                    const trailing = UI.el("div", "flex items-center gap-2 ml-4 flex-shrink-0");
                    const testBtn = UI.iconBtn("play_arrow", () => ConfigView._testSingleModel(provider, upstream, display, testState, updateRow), "standard", { tooltip: "测试此模型" });
                    testBtn.classList.add("text-md-primary");
                    if (result.status === "testing") {
                        testBtn.disabled = true;
                        testBtn.classList.add("opacity-50");
                    }
                    trailing.appendChild(testBtn);
                    listItem.appendChild(trailing);
                    
                    list.appendChild(listItem);
                    rowRefs.set(display, { listItem, leading, supporting, testBtn });
                });

                // 空状态
                if (list.children.length === 0) {
                    const emptyState = UI.el("li", "flex flex-col items-center justify-center py-12 text-md-on-surface-variant");
                    emptyState.appendChild(UI.icon("search_off", "text-4xl mb-2"));
                    emptyState.appendChild(UI.el("span", "text-body-medium", "没有匹配的模型"));
                    list.appendChild(emptyState);
                }
            };

            // 更新单行（按别名查找）
            const updateRow = (display) => {
                const ref = rowRefs.get(display);
                if (!ref) return;

                const result = testState.results.get(display);
                
                // 更新状态图标
                ref.leading.innerHTML = "";
                ref.leading.appendChild(ConfigView._createStatusIcon(result.status));

                // 更新辅助文本
                ref.supporting.innerHTML = "";
                ref.supporting.appendChild(ConfigView._createSupportingText(result));

                // 更新按钮状态
                ref.testBtn.disabled = result.status === "testing";
                if (result.status === "testing") {
                    ref.testBtn.classList.add("opacity-50");
                } else {
                    ref.testBtn.classList.remove("opacity-50");
                }
            };

            // 搜索事件
            searchInput.oninput = (e) => {
                testState.searchKeyword = e.target.value.trim();
                renderRows();
            };

            // 全部测试
            startAllBtn.onclick = async () => {
                if (testState.isRunning) return;
                
                testState.isRunning = true;
                testState.abortController = new AbortController();
                startAllBtn.style.display = "none";
                stopBtn.style.display = "";

                // 重置所有状态（按别名）
                models.forEach(m => {
                    testState.results.set(m.display, { status: "pending", latency: null, error: null });
                });
                renderRows();

                // 并发测试（使用上游模型名测试，按别名更新状态）
                const queue = [...models];
                const runNext = async () => {
                    while (queue.length > 0 && testState.isRunning) {
                        const modelInfo = queue.shift();
                        if (!modelInfo) break;
                        await ConfigView._testSingleModel(provider, modelInfo.upstream, modelInfo.display, testState, updateRow);
                    }
                };

                // 启动并发任务
                const tasks = [];
                for (let i = 0; i < testState.concurrency; i++) {
                    tasks.push(runNext());
                }
                await Promise.all(tasks);

                testState.isRunning = false;
                startAllBtn.style.display = "";
                stopBtn.style.display = "none";
            };

            // 停止测试
            stopBtn.onclick = () => {
                testState.isRunning = false;
                if (testState.abortController) {
                    testState.abortController.abort();
                }
                startAllBtn.style.display = "";
                stopBtn.style.display = "none";
            };

            renderRows();
            return content;
        };

        dialogRef = UI.dialog(
            `测试渠道: ${providerName}`,
            renderDialogContent,
            () => {
                // 关闭时停止测试
                testState.isRunning = false;
                if (testState.abortController) {
                    testState.abortController.abort();
                }
                return true;
            },
            "关闭",
            { width: "max-w-3xl", showCancel: false }
        );
    },

    /**
     * 创建状态图标 - MD3 List Item Leading Element
     */
    _createStatusIcon(status) {
        const configs = {
            pending: { icon: "radio_button_unchecked", class: "text-md-on-surface-variant" },
            testing: { icon: "sync", class: "text-md-tertiary animate-spin" },
            success: { icon: "check_circle", class: "text-md-success" },
            error: { icon: "error", class: "text-md-error" },
        };
        const config = configs[status] || configs.pending;
        return UI.icon(config.icon, `text-2xl ${config.class}`);
    },

    /**
     * 创建辅助文本 - MD3 List Item Supporting Text
     */
    _createSupportingText(result) {
        const container = document.createDocumentFragment();
        
        if (result.status === "pending") {
            container.appendChild(document.createTextNode("等待测试"));
        } else if (result.status === "testing") {
            container.appendChild(document.createTextNode("正在测试..."));
        } else if (result.status === "success" && result.latency !== null) {
            const latencySpan = UI.el("span", "text-md-success font-medium", `${result.latency}ms`);
            container.appendChild(latencySpan);
            container.appendChild(document.createTextNode(" · 测试通过"));
        } else if (result.status === "error") {
            const errorText = result.error || "测试失败";
            const truncated = errorText.length > 40 ? errorText.substring(0, 40) + "..." : errorText;
            const errorSpan = UI.el("span", "text-md-error", truncated);
            errorSpan.title = errorText;
            container.appendChild(errorSpan);
        }
        
        return container;
    },

    /**
     * 创建状态徽章（保留用于其他地方）
     */
    _createStatusBadge(status, error) {
        const configs = {
            pending: { icon: "schedule", text: "待测试", class: "bg-md-surface-container-high text-md-on-surface-variant" },
            testing: { icon: "sync", text: "测试中", class: "bg-md-tertiary-container text-md-on-tertiary-container" },
            success: { icon: "check_circle", text: "成功", class: "bg-md-success-container text-md-on-success-container" },
            error: { icon: "error", text: "失败", class: "bg-md-error-container text-md-on-error-container" },
        };
        const config = configs[status] || configs.pending;
        
        const badge = UI.el("span", `inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full text-label-small ${config.class}`);
        badge.appendChild(UI.icon(config.icon, "text-sm"));
        badge.appendChild(document.createTextNode(config.text));
        
        if (error) {
            badge.title = error;
        }
        
        return badge;
    },

    /**
     * 测试单个模型 - 使用专用渠道测试接口
     * @param {Object} provider - 渠道配置
     * @param {string} upstream - 上游模型名（用于实际测试请求）
     * @param {string} display - 别名（用于状态更新和显示）
     * @param {Object} testState - 测试状态对象
     * @param {Function} updateCallback - 更新回调
     */
    async _testSingleModel(provider, upstream, display, testState, updateCallback) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        // 设置为测试中（按别名更新状态）
        testState.results.set(display, { status: "testing", latency: null, error: null });
        if (updateCallback) updateCallback(display);

        // 获取 API Key（支持多种格式）
        let apiKey = "";
        if (provider.api) {
            apiKey = Array.isArray(provider.api) ? provider.api[0] : provider.api;
        } else if (provider.api_keys && provider.api_keys.length > 0) {
            apiKey = provider.api_keys[0];
        }

        try {
            const res = await fetch("/v1/channels/test", {
                method: "POST",
                headers,
                body: JSON.stringify({
                    engine: provider.engine || "openai",
                    base_url: provider.base_url,
                    api_key: apiKey,
                    model: upstream,  // 使用上游模型名进行测试
                    timeout: 30,
                }),
                signal: testState.abortController?.signal,
            });

            const data = await res.json().catch(() => ({}));

            if (res.ok && data.success) {
                testState.results.set(display, {
                    status: "success",
                    latency: data.latency_ms || null,
                    error: null
                });
            } else {
                const errorMsg = data.error || data.detail || data.message || `HTTP ${res.status}`;
                testState.results.set(display, { status: "error", latency: null, error: errorMsg });
            }
        } catch (e) {
            if (e.name === "AbortError") {
                testState.results.set(display, { status: "pending", latency: null, error: null });
            } else {
                testState.results.set(display, { status: "error", latency: null, error: e.message });
            }
        }

        if (updateCallback) updateCallback(display);
    },

    /**
     * 删除渠道
     */
    async _deleteProvider(index, providers) {
        const provider = providers[index];
        const name = provider?.provider || provider?.name || `渠道 ${index + 1}`;
        
        // 确认删除
        if (!confirm(`确定要删除渠道 "${name}" 吗？此操作不可撤销。`)) {
            return;
        }

        const apiConfig = ConfigView._apiConfig || { providers: [] };
        const newProviders = providers.filter((_, i) => i !== index);
        const bodyConfig = { ...apiConfig, providers: newProviders };

        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        try {
            const res = await fetch("/v1/api_config/update", {
                method: "POST",
                headers,
                body: JSON.stringify(bodyConfig),
            });
            if (!res.ok) {
                UI.snackbar(`删除失败: ${res.status}`, null, null, { variant: "error" });
                return;
            }
            ConfigView._apiConfig = bodyConfig;
            ConfigView.refresh();
            UI.snackbar(`已删除渠道 "${name}"`, null, null, { variant: "success" });
        } catch (e) {
            UI.snackbar(`删除失败: ${e.message}`, null, null, { variant: "error" });
        }
    },

    /**
     * 启用/禁用渠道
     */
    async _toggleProvider(index, providers, enabled) {
        const apiConfig = ConfigView._apiConfig || { providers: [] };
        const newProviders = providers.slice();
        newProviders[index] = { ...newProviders[index], enabled };
        const bodyConfig = { ...apiConfig, providers: newProviders };

        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        try {
            const res = await fetch("/v1/api_config/update", {
                method: "POST",
                headers,
                body: JSON.stringify(bodyConfig),
            });
            if (!res.ok) {
                UI.snackbar(`操作失败: ${res.status}`, null, null, { variant: "error" });
                return;
            }
            ConfigView._apiConfig = bodyConfig;
            ConfigView.refresh();
            UI.snackbar(enabled ? "渠道已启用" : "渠道已禁用", null, null, { variant: "success" });
        } catch (e) {
            UI.snackbar(`操作失败: ${e.message}`, null, null, { variant: "error" });
        }
    },

    /**
     * 更新渠道优先级/权重
     */
    async _updateProviderWeight(index, providers, newWeight) {
        const apiConfig = ConfigView._apiConfig || { providers: [] };
        const newProviders = providers.slice();
        
        // 确保 preferences 对象存在
        if (!newProviders[index].preferences) {
            newProviders[index].preferences = {};
        }
        newProviders[index] = {
            ...newProviders[index],
            preferences: {
                ...newProviders[index].preferences,
                weight: newWeight
            }
        };
        
        const bodyConfig = { ...apiConfig, providers: newProviders };

        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        try {
            const res = await fetch("/v1/api_config/update", {
                method: "POST",
                headers,
                body: JSON.stringify(bodyConfig),
            });
            if (!res.ok) {
                UI.snackbar(`更新优先级失败: ${res.status}`, null, null, { variant: "error" });
                return;
            }
            ConfigView._apiConfig = bodyConfig;
            // 更新列表显示以应用按优先级的默认排序
            ConfigView.refresh();
            UI.snackbar(`优先级已更新为 ${newWeight}`, null, null, { variant: "success" });
        } catch (e) {
            UI.snackbar(`更新优先级失败: ${e.message}`, null, null, { variant: "error" });
        }
    },

    /**
     * 复制渠道配置
     */
    _copyProvider(provider) {
        // 创建副本并修改名称
        const copy = JSON.parse(JSON.stringify(provider));
        const originalName = copy.provider || copy.name || "channel";
        copy.provider = `${originalName}_copy`;
        if (copy.name) copy.name = `${originalName}_copy`;
        
        // 打开侧边栏编辑副本
        ConfigView.openConfigSideSheet({ provider: copy });
        UI.snackbar("已复制渠道配置，请修改后保存", null, null, { variant: "info" });
    },

    /**
     * 打开渠道配置侧边栏
     * @param {Object|null} existingData - 已有的渠道数据（编辑时传入）
     */
    openConfigSideSheet(existingData = null) {
        const apiConfig = ConfigView._apiConfig || { providers: [] };
        const providers = Array.isArray(apiConfig.providers) ? apiConfig.providers : [];

        let providerIndex = -1;
        let originalProvider = null;

        if (existingData && typeof existingData.index === "number") {
            providerIndex = existingData.index;
            originalProvider = providers[providerIndex] || existingData.provider;
        } else if (existingData?.provider) {
            originalProvider = existingData.provider;
        }

        const providerData = ConfigView._initProviderData(originalProvider, providerIndex);

        UI.sideSheet(
            originalProvider ? `编辑渠道: ${originalProvider.provider || originalProvider.name || ""}` : "新增渠道",
            () => ConfigView._renderSideSheetContent(providerData),
            async () => ConfigView._saveProvider(providerData, apiConfig, providers, providerIndex),
            "保存配置"
        );
    },

    // 向后兼容的别名
    openConfigModal(existingData = null) {
        return ConfigView.openConfigSideSheet(existingData);
    },

    _initProviderData(originalProvider, providerIndex) {
        const rawPreferences = originalProvider?.preferences || {};
        const providerData = {
            index: providerIndex,
            name: originalProvider?.provider || originalProvider?.name || "",
            // 新建渠道时不自动填入具体 URL，留空并通过 placeholder 提示
            base_url: originalProvider?.base_url || "",
            api_keys: [],  // 格式: [{key: "sk-xxx", disabled: false}, ...]
            models: [],
            modelMappings: [],
            engine: originalProvider?.engine || "",
            image: originalProvider?.image !== false,
            model_prefix: originalProvider?.model_prefix || "",
            preferences: { ...rawPreferences },
        };

        // 解析 API key，支持 ! 前缀标记禁用的 key
        const parseApiKey = (keyStr) => {
            const trimmed = String(keyStr).trim();
            if (trimmed.startsWith('!')) {
                return { key: trimmed.substring(1), disabled: true };
            }
            return { key: trimmed, disabled: false };
        };

        if (originalProvider) {
            if (originalProvider.api !== undefined && originalProvider.api !== null) {
                if (Array.isArray(originalProvider.api)) {
                    providerData.api_keys = originalProvider.api.map(parseApiKey);
                } else if (typeof originalProvider.api === "string" && originalProvider.api.trim()) {
                    providerData.api_keys = [parseApiKey(originalProvider.api.trim())];
                }
            } else if (Array.isArray(originalProvider.api_keys)) {
                providerData.api_keys = originalProvider.api_keys.map(parseApiKey);
            }

            const modelArray = Array.isArray(originalProvider.model) ? originalProvider.model : originalProvider.models;
            if (Array.isArray(modelArray)) {
                modelArray.forEach(m => {
                    if (typeof m === "string") {
                        providerData.models.push(m);
                    } else if (typeof m === "object") {
                        Object.entries(m).forEach(([upstream, alias]) => {
                            providerData.modelMappings.push({ from: alias, to: upstream });
                        });
                    }
                });
            }
        }
        // 初始化分组：支持字符串或数组，默认 default
        (function () {
            let g = providerData.groups;
            if (!g) {
                const orig = originalProvider;
                g = (orig && Array.isArray(orig.groups)) ? orig.groups.slice()
                    : (orig && typeof orig.group === "string" && orig.group.trim() ? [orig.group.trim()]
                        : (orig && orig.preferences && typeof orig.preferences.group === "string" && orig.preferences.group.trim() ? [orig.preferences.group.trim()] : null));
            }
            if (!Array.isArray(g)) g = ["default"];
            g = g.map(x => (typeof x === "string" ? x.trim() : x)).filter(Boolean);
            if (!g.length) g = ["default"];
            providerData.groups = g;
        })();
        return providerData;
    },
};

// ConfigView 的扩展方法 - 侧边栏内容渲染
Object.assign(ConfigView, {
    _renderSideSheetContent(providerData) {
        const form = UI.el("div", "flex flex-col gap-4 max-h-[70vh] overflow-y-auto pr-2");
        const prefs = providerData.preferences || (providerData.preferences = {});
        if (!prefs.api_key_schedule_algorithm) prefs.api_key_schedule_algorithm = "round_robin";
        if (prefs.tools === undefined) prefs.tools = true;

        // Basic Config Section
        const basicHeader = UI.el("div", "flex items-center justify-between text-md-primary text-label-large mb-2");

        const basicTitle = UI.el("div", "inline-flex items-center gap-2");
        basicTitle.appendChild(UI.icon("settings", "text-lg", true));
        basicTitle.appendChild(UI.el("span", "", "基础配置"));
        basicHeader.appendChild(basicTitle);

        // 渠道级插件拦截器入口按钮：打开独立侧边抽屉管理本渠道插件
        const pluginEntryBtn = UI.iconBtn(
            "extension",
            () => {
                ConfigView._openInterceptorPluginsPanel(prefs, null);
            },
            "standard",
            { tooltip: "配置本渠道的插件拦截器" }
        );
        basicHeader.appendChild(pluginEntryBtn);

        form.appendChild(basicHeader);

        const basicSection = UI.el("div", "flex flex-col gap-3 bg-md-surface-container p-4 rounded-md-lg");
        const engineSelectWrapper = UI.el("div", "");
        const placeholder = UI.el("div", "flex items-center gap-2 text-body-medium text-md-on-surface-variant py-2");
        placeholder.appendChild(UI.spinner());
        placeholder.appendChild(UI.el("span", "", "加载渠道类型..."));
        engineSelectWrapper.appendChild(placeholder);
        basicSection.appendChild(engineSelectWrapper);

        ConfigView._loadChannelTypes(engineSelectWrapper, providerData, form);
 
        const nameWrap = UI.textField("渠道标识", "例如 openai", "text", providerData.name, { required: true });
        nameWrap.input.oninput = (e) => { providerData.name = e.target.value; };
        basicSection.appendChild(nameWrap.wrapper);
 
        // Base URL：占位文本会根据所选渠道的 default_base_url 动态更新
        // 留空时将使用渠道类型的默认 base_url
        const urlWrap = UI.textField("上游 Base URL", "", "text", providerData.base_url, { required: false, helperText: "留空则使用渠道类型的默认地址" });
        // 标记为配置用 Base URL 输入框，便于 _loadChannelTypes 动态更新 placeholder
        urlWrap.input.setAttribute("data-config-base-url-input", "1");
        urlWrap.input.oninput = (e) => { providerData.base_url = e.target.value; };
        basicSection.appendChild(urlWrap.wrapper);

        // 模型前缀：为该渠道的所有模型名添加前缀
        const prefixWrap = UI.textField("模型前缀", "例如 azure- 或 aws/", "text", providerData.model_prefix, {
            required: false,
            helperText: "可选：为该渠道的模型名添加前缀，请求带前缀的模型将只匹配此渠道"
        });
        prefixWrap.input.oninput = (e) => { providerData.model_prefix = e.target.value; };
        basicSection.appendChild(prefixWrap.wrapper);

        // 分组编辑 - 多组支持
        if (!Array.isArray(providerData.groups)) {
            providerData.groups = (typeof providerData.groups === "string" && providerData.groups.trim())
                ? [providerData.groups.trim()]
                : ["default"];
        }
        const groupsTitle = UI.el("div", "text-label-medium text-md-on-surface", "分组");
        basicSection.appendChild(groupsTitle);

        const groupsContainer = UI.el("div", "flex flex-wrap gap-2");
        const renderGroupChips = () => {
            groupsContainer.innerHTML = "";
            const groups = Array.isArray(providerData.groups) ? providerData.groups : [];
            if (!groups.length) providerData.groups = ["default"];
            providerData.groups.forEach((g, i) => {
                const chip = UI.el("div", "inline-flex items-center gap-2 pl-3 pr-1 py-1 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-medium group");
                chip.appendChild(UI.icon("folder", "text-sm"));
                const nameEl = UI.el("span", "", g);
                chip.appendChild(nameEl);
                const btnGroup = UI.el("div", "flex items-center gap-1 ml-1");
                const delBtn = UI.el("button", "w-5 h-5 rounded-full flex items-center justify-center hover:bg-md-on-secondary-container/12 transition-colors");
                delBtn.appendChild(UI.icon("close", "text-sm"));
                delBtn.onclick = (e) => {
                    e.stopPropagation();
                    providerData.groups.splice(i, 1);
                    if (providerData.groups.length === 0) providerData.groups = ["default"];
                    renderGroupChips();
                };
                btnGroup.appendChild(delBtn);
                chip.appendChild(btnGroup);
                groupsContainer.appendChild(chip);
            });
        };
        renderGroupChips();
        basicSection.appendChild(groupsContainer);

        const addGroupField = UI.textField("添加分组", "例如 default 或 premium", "text", "");
        const addGroupInput = addGroupField.input;
        addGroupInput.onkeydown = (e) => {
            if (e.key === "Enter") {
                const val = (addGroupInput.value || "").trim();
                if (!val) return;
                if (!Array.isArray(providerData.groups)) providerData.groups = [];
                if (!providerData.groups.includes(val)) {
                    providerData.groups.push(val);
                }
                addGroupInput.value = "";
                renderGroupChips();
            }
        };
        basicSection.appendChild(addGroupField.wrapper);

        form.appendChild(basicSection);

                // API Keys Section - 列表输入框
                const keysHeader = UI.el("div", "inline-flex items-center gap-2 text-md-secondary text-label-large mb-2 mt-4");
                keysHeader.appendChild(UI.icon("key", "text-lg", true));
                keysHeader.appendChild(UI.el("span", "", "API Keys"));
                form.appendChild(keysHeader);
        
                if (!Array.isArray(providerData.api_keys)) {
                    providerData.api_keys = [];
                }
        
                const keysSection = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");
        
                // 顶部统计 + 操作按钮
                const keysToolbar = UI.el("div", "flex items-center justify-between");
                const keysSummary = UI.el("span", "text-body-small text-md-on-surface-variant", "");
                const keysActions = UI.el("div", "flex items-center gap-2");
        
                const addKeyBtn = UI.btn("添加密钥", null, "text", "add");
                const copyAllBtn = UI.btn("复制全部", null, "text", "content_copy");
        
                keysActions.appendChild(copyAllBtn);
                keysActions.appendChild(addKeyBtn);
                keysToolbar.appendChild(keysSummary);
                keysToolbar.appendChild(keysActions);
                keysSection.appendChild(keysToolbar);
        
                // 列表容器（内部滚动）
                const listWrapper = UI.el("div", "mt-1 border border-md-outline rounded-md-xs bg-md-surface overflow-hidden");
                const listContainer = UI.el("div", "max-h-64 overflow-y-auto divide-y divide-md-outline-variant");
                listWrapper.appendChild(listContainer);
                keysSection.appendChild(listWrapper);
        
                // 底部提示
                keysSection.appendChild(
                    UI.el(
                        "div",
                        "mt-2 text-body-small text-md-on-surface-variant/80",
                        "提示: 输入回车可快速添加新密钥，点击开关按钮可禁用/启用单个密钥"
                    )
                );
        
                const renderKeyRows = () => {
                    listContainer.innerHTML = "";
        
                    const keys = providerData.api_keys;
                    if (!keys.length) {
                        keys.push({ key: "", disabled: false });
                    }
        
                    let nonEmptyCount = 0;
                    let disabledCount = 0;
        
                    keys.forEach((keyObj, index) => {
                        // 兼容旧格式（字符串）
                        if (typeof keyObj === "string") {
                            keyObj = { key: keyObj, disabled: false };
                            keys[index] = keyObj;
                        }
                        
                        const keyValue = keyObj.key || "";
                        const isDisabled = keyObj.disabled === true;
                        
                        if (keyValue.trim()) {
                            nonEmptyCount += 1;
                            if (isDisabled) disabledCount += 1;
                        }
        
                        // 行容器 - 禁用的 key 显示为灰色背景
                        const rowClasses = isDisabled
                            ? "flex items-center gap-2 px-3 py-2 bg-md-surface-container/50 opacity-60"
                            : "flex items-center gap-2 px-3 py-2";
                        const row = UI.el("div", rowClasses);
        
                        // 序号
                        const indexLabel = UI.el(
                            "span",
                            "w-6 text-right text-body-small text-md-on-surface-variant flex-shrink-0",
                            String(index)
                        );
        
                        // 输入框 - 禁用的 key 添加删除线样式
                        const input = document.createElement("input");
                        input.type = "text";
                        const inputClasses = isDisabled
                            ? "flex-1 px-2 py-1 bg-md-surface border border-transparent rounded-md-xs text-body-small font-mono text-md-on-surface-variant line-through focus:outline-none focus:border-md-primary focus:border-2"
                            : "flex-1 px-2 py-1 bg-md-surface border border-transparent rounded-md-xs text-body-small font-mono text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2";
                        input.className = inputClasses;
                        input.value = keyValue;
                        input.setAttribute("data-key-index", String(index));
        
                        input.oninput = (e) => {
                            providerData.api_keys[index].key = e.target.value;
                        };
        
                        // 回车快速添加/跳转
                        input.onkeydown = (e) => {
                            if (e.key === "Enter") {
                                e.preventDefault();
                                const lastIndex = providerData.api_keys.length - 1;
                                if (index === lastIndex) {
                                    providerData.api_keys.push({ key: "", disabled: false });
                                    renderKeyRows();
                                    setTimeout(() => {
                                        const inputs = listContainer.querySelectorAll('input[data-key-index]');
                                        const last = inputs[inputs.length - 1];
                                        if (last) last.focus();
                                    }, 0);
                                } else {
                                    const inputs = listContainer.querySelectorAll('input[data-key-index]');
                                    const next = inputs[index + 1];
                                    if (next) next.focus();
                                }
                            }
                        };

                        // 粘贴多行 key 时自动分割插入
                        input.onpaste = (e) => {
                            const clipboardData = e.clipboardData || window.clipboardData;
                            if (!clipboardData) return;

                            const pastedText = clipboardData.getData("text") || "";
                            // 按换行符分割，支持 \r\n、\n、\r
                            const lines = pastedText.split(/\r?\n|\r/).map(s => s.trim()).filter(s => s.length > 0);

                            // 如果只有一行或没有内容，走默认粘贴逻辑
                            if (lines.length <= 1) {
                                return;
                            }

                            // 阻止默认粘贴行为
                            e.preventDefault();

                            // 当前行设为第一个 key
                            providerData.api_keys[index].key = lines[0];

                            // 其余 key 插入到当前行之后
                            const remaining = lines.slice(1);
                            // 去重：过滤掉已存在的 key
                            const existingSet = new Set(providerData.api_keys.map(k => (k.key || "").trim()).filter(k => k));
                            const newKeys = remaining.filter(k => !existingSet.has(k));

                            if (newKeys.length > 0) {
                                // 在当前索引之后插入新 key（新 key 默认启用）
                                const newKeyObjs = newKeys.map(k => ({ key: k, disabled: false }));
                                providerData.api_keys.splice(index + 1, 0, ...newKeyObjs);
                            }

                            renderKeyRows();
                            UI.snackbar(`已粘贴 ${1 + newKeys.length} 个密钥`, null, null, { variant: "success" });
                        };

                        // 启用/禁用开关按钮
                        const toggleBtn = UI.iconBtn(
                            isDisabled ? "toggle_off" : "toggle_on",
                            () => {
                                providerData.api_keys[index].disabled = !isDisabled;
                                renderKeyRows();
                            },
                            "standard",
                            { tooltip: isDisabled ? "启用此密钥" : "禁用此密钥" }
                        );
                        toggleBtn.classList.add(isDisabled ? "text-md-on-surface-variant" : "text-md-success");
        
                        // 复制按钮
                        const copyBtn = UI.iconBtn(
                            "content_copy",
                            () => {
                                const text = (input.value || "").trim();
                                if (!text) {
                                    UI.snackbar("该行没有可复制的密钥", null, null, { variant: "error" });
                                    return;
                                }
                                navigator.clipboard
                                    .writeText(text)
                                    .then(() => {
                                        UI.snackbar("已复制密钥", null, null, { variant: "success" });
                                    })
                                    .catch(() => {
                                        UI.snackbar("复制失败", null, null, { variant: "error" });
                                    });
                            },
                            "standard",
                            { tooltip: "复制" }
                        );
                        copyBtn.classList.add("text-md-primary");
        
                        // 删除按钮
                        const deleteBtn = UI.iconBtn(
                            "delete",
                            () => {
                                providerData.api_keys.splice(index, 1);
                                renderKeyRows();
                            },
                            "standard",
                            { tooltip: "删除" }
                        );
                        deleteBtn.classList.add("text-md-error");
        
                        row.appendChild(indexLabel);
                        row.appendChild(input);
                        row.appendChild(toggleBtn);
                        row.appendChild(copyBtn);
                        row.appendChild(deleteBtn);
                        listContainer.appendChild(row);
                    });
        
                    // 更新统计信息
                    if (disabledCount > 0) {
                        keysSummary.textContent = `总计: ${nonEmptyCount} 个密钥 (${disabledCount} 个已禁用)`;
                    } else {
                        keysSummary.textContent = `总计: ${nonEmptyCount} 个密钥`;
                    }
                };
        
                // 按钮行为
                addKeyBtn.onclick = () => {
                    providerData.api_keys.push({ key: "", disabled: false });
                    renderKeyRows();
                    setTimeout(() => {
                        const inputs = listContainer.querySelectorAll('input[data-key-index]');
                        const last = inputs[inputs.length - 1];
                        if (last) last.focus();
                    }, 0);
                };
        
                copyAllBtn.onclick = () => {
                    const nonEmptyKeys = providerData.api_keys
                        .map((k) => (k.key || "").trim())
                        .filter((k) => k.length > 0);
                    if (!nonEmptyKeys.length) {
                        UI.snackbar("没有可复制的密钥", null, null, { variant: "error" });
                        return;
                    }
                    navigator.clipboard
                        .writeText(nonEmptyKeys.join("\n"))
                        .then(() => {
                            UI.snackbar("已复制所有密钥", null, null, { variant: "success" });
                        })
                        .catch(() => {
                            UI.snackbar("复制失败", null, null, { variant: "error" });
                        });
                };
        
                renderKeyRows();
                form.appendChild(keysSection);

        // Models Section - Chip 标签组
        const modelHeader = UI.el("div", "inline-flex items-center gap-2 text-md-tertiary text-label-large mb-2 mt-4");
        modelHeader.appendChild(UI.icon("psychology", "text-lg", true));
        modelHeader.appendChild(UI.el("span", "", "模型配置"));
        form.appendChild(modelHeader);

        const modelSection = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg");
        
        // 顶部操作按钮
        const modelActions = UI.el("div", "flex items-center gap-2 mb-3");
        const fetchModelsBtn = UI.btn("获取模型", null, "tonal", "sync");
        const clearModelsBtn = UI.btn("清空全部", null, "text", "delete");
        modelActions.appendChild(fetchModelsBtn);
        modelActions.appendChild(clearModelsBtn);
        modelSection.appendChild(modelActions);
        
        // 模型 Chip 容器
        const modelChipsContainer = UI.el("div", "flex flex-wrap gap-2 min-h-[48px] p-3 bg-md-surface-container-highest rounded-md-xs border border-dashed border-md-outline-variant");
        
        // 渲染模型 Chips
        const renderModelChips = () => {
            modelChipsContainer.innerHTML = "";
            
            if (providerData.models.length === 0) {
                const emptyHint = UI.el("div", "w-full text-center text-body-small text-md-on-surface-variant/60 py-2", "暂无模型，点击「获取模型」或「手动添加」");
                modelChipsContainer.appendChild(emptyHint);
                return;
            }
            
            // 构建别名映射：上游 -> 别名
            const aliasMap = new Map();
            providerData.modelMappings.forEach(m => {
                if (m.from && m.to) aliasMap.set(m.to, m.from);
            });

            providerData.models.forEach((model, index) => {
                const display = aliasMap.get(model) || model;

                const chip = UI.el("div", "inline-flex items-center gap-2 pl-3 pr-1 py-1 rounded-md-full bg-md-primary-container text-md-on-primary-container text-label-medium group cursor-pointer hover:shadow-md-1 transition-all");
                // 提示：点击复制模型名
                chip.setAttribute("data-tooltip", "点击复制模型名");

                // 统一复制方法（含回退）
                const copyModel = async () => {
                    const text = display || "";
                    if (!text) return;
                    try {
                        if (navigator.clipboard && navigator.clipboard.writeText) {
                            await navigator.clipboard.writeText(text);
                        } else {
                            const ta = document.createElement("textarea");
                            ta.value = text;
                            ta.style.position = "fixed";
                            ta.style.left = "-10000px";
                            document.body.appendChild(ta);
                            ta.select();
                            document.execCommand("copy");
                            document.body.removeChild(ta);
                        }
                        UI.snackbar(`已复制: ${text}`, null, null, { variant: "success" });
                    } catch (err) {
                        UI.snackbar("复制失败", null, null, { variant: "error" });
                    }
                };

                // 整个 Chip 点击复制（除删除按钮外）
                chip.onclick = () => {
                    copyModel();
                };
                
                // 模型名（点击复制）
                const modelName = UI.el("span", "font-mono select-none", display);
                modelName.onclick = (e) => {
                    e.stopPropagation();
                    copyModel();
                };
                chip.appendChild(modelName);

                // 按钮组：删除
                const btnGroup = UI.el("div", "flex items-center gap-1 ml-1");

                // 点击按钮组空白处也触发复制（除删除按钮外）
                btnGroup.onclick = (e) => {
                    if (!e.target.closest("button")) {
                        e.stopPropagation();
                        copyModel();
                    }
                };
                
                // 删除按钮
                const deleteBtn = UI.el("button", "w-5 h-5 rounded-full flex items-center justify-center hover:bg-md-on-primary-container/12 transition-colors");
                deleteBtn.appendChild(UI.icon("close", "text-sm"));
                deleteBtn.onclick = (e) => {
                    e.stopPropagation();
                    providerData.models.splice(index, 1);
                    renderModelChips();
                };
                btnGroup.appendChild(deleteBtn);
                
                chip.appendChild(btnGroup);
                
                modelChipsContainer.appendChild(chip);
            });
        };
        
        renderModelChips();
        modelSection.appendChild(modelChipsContainer);

        // 手动输入模型：多个用逗号或空格分隔
        const manualInputWrap = UI.textField(
            "手动输入模型名称",
            "例如 gpt-4o mini, claude-3-sonnet 或用空格分隔",
            "text",
            "",
            {
                helperText: "多个用逗号或空格分隔，按回车快速添加",
                variant: "outlined"
            }
        );
        const manualInput = manualInputWrap.input;

        const applyManualInput = () => {
            const raw = manualInput.value || "";
            const parts = raw
                .split(/[, \s]+/)
                .map((s) => s.trim())
                .filter((s) => s.length > 0);

            if (!parts.length) return;

            let added = 0;
            parts.forEach((name) => {
                if (!providerData.models.includes(name)) {
                    providerData.models.push(name);
                    added++;
                }
            });

            if (added > 0) {
                renderModelChips();
                UI.snackbar(`已添加 ${added} 个模型`, null, null, { variant: "success" });
            } else {
                UI.snackbar("输入的模型已在列表中", null, null, { variant: "info" });
            }

            manualInput.value = "";
        };

        manualInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                applyManualInput();
            }
        });

        modelSection.appendChild(manualInputWrap.wrapper);
        form.appendChild(modelSection);
        
        // 获取模型 - 打开模态框
        fetchModelsBtn.onclick = () => ConfigView._openFetchModelsDialog(providerData, renderModelChips, fetchModelsBtn);
        
        // 清空全部
        clearModelsBtn.onclick = () => {
            if (providerData.models.length === 0) return;
            if (confirm("确定要清空所有模型吗？")) {
                providerData.models = [];
                renderModelChips();
            }
        };

        // Model Mappings Section
        ConfigView._renderMappingsSection(form, providerData, renderModelChips);

        // Routing Section
        ConfigView._renderRoutingSection(form, prefs);

        // Advanced Section
        ConfigView._renderAdvancedSection(form, prefs);

        // 插件拦截器在单独的侧边抽屉中配置，这里不再追加大块区域

        return form;
    },

    async _loadChannelTypes(wrapper, providerData, form) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = adminKey ? { Authorization: `Bearer ${adminKey}` } : {};
        let channelOptions = [];
        
        try {
            const res = await fetch("/v1/channels", { headers });
            if (res.ok) {
                const data = await res.json();
                const channels = data.channels || [];
                channelOptions = channels.map(ch => ({
                    value: ch.id,
                    label: ch.description || `${ch.type_name} (${ch.id})`,
                    defaultBaseUrl: ch.default_base_url,
                }));
            }
        } catch (e) {
            console.error("Failed to load /v1/channels:", e);
        }
        
        wrapper.innerHTML = "";
        if (!providerData.engine && channelOptions.length > 0) {
            providerData.engine = channelOptions[0].value;
        }
        
        // 根据当前选中的渠道，更新 Base URL 输入框的 placeholder 为后端返回的 default_base_url
        const applyDefaultBaseUrlPlaceholder = (engineValue) => {
            const selected = channelOptions.find(ch => ch.value === engineValue);
            const urlInput = form.querySelector('input[data-config-base-url-input="1"]');
            if (!urlInput || !selected) return;
            const defaultUrl = selected.defaultBaseUrl || "";
            if (defaultUrl) {
                urlInput.dataset.defaultBaseUrl = defaultUrl;

                if (!urlInput.dataset.baseUrlPlaceholderBound) {
                    urlInput.dataset.baseUrlPlaceholderBound = "1";

                    urlInput.addEventListener("focus", () => {
                        if (!urlInput.value) {
                            urlInput.placeholder = urlInput.dataset.defaultBaseUrl || "";
                        }
                    });
                }
            }
        };
        
        const engineSelect = UI.select("渠道类型", channelOptions, providerData.engine, (val) => {
            providerData.engine = val;
            applyDefaultBaseUrlPlaceholder(val);
        });
        wrapper.appendChild(engineSelect.wrapper);
        
        // 初始化时也应用一次 placeholder（新建渠道时 base_url 为空，只展示示例）
        if (providerData.engine) {
            applyDefaultBaseUrlPlaceholder(providerData.engine);
        }
    },

    /**
     * 获取模型 - 打开 MD3 模态框，复选模型列表（含全选 / 全不选 / 模糊搜索）
     */
    async _openFetchModelsDialog(providerData, renderModelChips, fetchBtn) {
        if (!providerData.api_keys.length || !providerData.api_keys.some(k => k && k.key && k.key.trim())) {
            UI.snackbar("请先填写至少一个 API Key", null, null, { variant: "error" });
            return;
        }

        if (fetchBtn && typeof fetchBtn.setLoading === "function") {
            fetchBtn.setLoading(true);
        }

        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        let fetchedModels = [];

        try {
            // 获取第一个可用的 API key（对象格式，取 .key 属性）
            const firstKey = providerData.api_keys.find(k => k && k.key && k.key.trim());
            const res = await fetch("/v1/channels/fetch_models", {
                method: "POST",
                headers,
                body: JSON.stringify({
                    engine: providerData.engine,
                    base_url: providerData.base_url,
                    api_key: firstKey?.key || "",
                }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                UI.snackbar(`获取模型失败: ${err.detail || res.status}`, null, null, { variant: "error" });
                return;
            }
            const data = await res.json();
            let models = Array.isArray(data) ? data : data.models || (data.data || []).map(m => m.id).filter(Boolean);
            // 去重（上游名）
            fetchedModels = Array.from(new Set(models));

            // 根据重定向构建显示名集合（上游 -> 显示别名）
            var aliasMap = new Map();
            providerData.modelMappings.forEach(m => {
                if (m.from && m.to) aliasMap.set(m.to, m.from);
            });
            var displayModels = [];
            const seenDisplay = new Set();
            fetchedModels.forEach((name) => {
                const disp = aliasMap.get(name) || name;
                if (!seenDisplay.has(disp)) {
                    seenDisplay.add(disp);
                    displayModels.push({ display: disp, upstream: name });
                }
            });

            if (!displayModels.length) {
                UI.snackbar("未获取到任何模型", null, null, { variant: "error" });
                return;
            }
        } catch (e) {
            UI.snackbar(`获取模型失败: ${e.message}`, null, null, { variant: "error" });
            return;
        } finally {
            if (fetchBtn && typeof fetchBtn.setLoading === "function") {
                fetchBtn.setLoading(false);
            }
        }

        // 选中状态：默认选中已有模型（按重定向别名显示）
        const aliasDisplay = (name) => (aliasMap.get(name) || name);
        const displayNameSet = new Set(displayModels.map(d => d.display));
        const selected = new Set();
        providerData.models.forEach((m) => {
            const disp = aliasDisplay(m);
            if (displayNameSet.has(disp)) {
                selected.add(disp);
            }
        });

        // 搜索关键词
        let searchKeyword = "";

        const renderDialogContent = () => {
            const content = UI.el("div", "flex flex-col gap-4");

            // 统一复制方法（含回退）
            const copyModelName = async (text) => {
                const t = text || "";
                if (!t) return;
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(t);
                    } else {
                        const ta = document.createElement("textarea");
                        ta.value = t;
                        ta.style.position = "fixed";
                        ta.style.left = "-10000px";
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand("copy");
                        document.body.removeChild(ta);
                    }
                    UI.snackbar(`已复制: ${t}`, null, null, { variant: "success" });
                } catch (err) {
                    UI.snackbar("复制失败", null, null, { variant: "error" });
                }
            };

            // 搜索框
            const searchWrapper = UI.el("div", "relative");
            const searchIcon = UI.icon("search", "absolute left-3 top-1/2 -translate-y-1/2 text-md-on-surface-variant");
            const searchInput = document.createElement("input");
            searchInput.type = "text";
            searchInput.placeholder = "搜索模型名称...";
            searchInput.className = "w-full pl-10 pr-4 py-3 bg-md-surface-container border border-md-outline rounded-md-full text-body-medium text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2 transition-all";
            searchWrapper.appendChild(searchIcon);
            searchWrapper.appendChild(searchInput);
            content.appendChild(searchWrapper);

            // 顶部统计 + 全选/全不选
            const toolbar = UI.el("div", "flex items-center justify-between p-3 bg-md-surface-container-highest rounded-md-xs");
            const statsText = UI.el("span", "text-body-medium text-md-on-surface-variant", "");
            const actions = UI.el("div", "flex items-center gap-2");

            // 模型列表容器
            const listContainer = UI.el("div", "max-h-[360px] overflow-y-auto rounded-md-xs border border-md-outline-variant bg-md-surface");

            // 存储所有行的引用，用于搜索过滤
            const rowRefs = [];

            const updateStats = () => {
                const visibleCount = rowRefs.filter(r => r.row.style.display !== "none").length;
                if (searchKeyword) {
                    statsText.textContent = `显示 ${visibleCount} / ${displayModels.length} 个模型，已选 ${selected.size} 个`;
                } else {
                    statsText.textContent = `共 ${displayModels.length} 个模型，已选 ${selected.size} 个`;
                }
            };

            const filterModels = () => {
                const keyword = searchKeyword.toLowerCase();
                rowRefs.forEach(({ row, display }) => {
                    if (!keyword || display.toLowerCase().includes(keyword)) {
                        row.style.display = "";
                    } else {
                        row.style.display = "none";
                    }
                });
                updateStats();
            };

            // 全选当前可见的模型
            const selectAllBtn = UI.btn("全选", () => {
                rowRefs.forEach(({ display, row, setChecked }) => {
                    if (row.style.display !== "none") {
                        selected.add(display);
                        setChecked(true);
                    }
                });
                updateStats();
            }, "text", "select_all");

            // 全不选当前可见的模型
            const clearAllBtn = UI.btn("全不选", () => {
                rowRefs.forEach(({ display, row, setChecked }) => {
                    if (row.style.display !== "none") {
                        selected.delete(display);
                        setChecked(false);
                    }
                });
                updateStats();
            }, "text", "deselect");

            actions.appendChild(selectAllBtn);
            actions.appendChild(clearAllBtn);
            toolbar.appendChild(statsText);
            toolbar.appendChild(actions);
            content.appendChild(toolbar);

            // 渲染模型列表（按重定向别名显示，内部保留上游名用于提示）
            displayModels.forEach(({ display, upstream }) => {
                const row = UI.el("div", "px-4 py-2 flex items-center hover:bg-md-surface-container transition-colors border-b border-md-outline-variant last:border-b-0 cursor-pointer");
                
                // 自定义 checkbox（避免 UI.checkbox 的问题）
                const checked = selected.has(display);
                const checkboxWrapper = UI.el("label", "inline-flex items-center cursor-pointer");
                const checkboxInput = document.createElement("input");
                checkboxInput.type = "checkbox";
                checkboxInput.checked = checked;
                checkboxInput.className = "sr-only peer";
                
                const checkboxBox = UI.el("div", `w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center ${
                    checked
                        ? "bg-md-primary border-md-primary"
                        : "border-md-on-surface-variant hover:border-md-on-surface"
                }`);
                const checkIcon = UI.icon("check", `text-sm text-md-on-primary transition-transform ${checked ? "scale-100" : "scale-0"}`);
                checkboxBox.appendChild(checkIcon);
                
                // 切换选中状态的函数
                const toggleSelection = () => {
                    const newChecked = !checkboxInput.checked;
                    checkboxInput.checked = newChecked;
                    if (newChecked) {
                        selected.add(display);
                        checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center bg-md-primary border-md-primary";
                        checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-100";
                    } else {
                        selected.delete(display);
                        checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center border-md-on-surface-variant hover:border-md-on-surface";
                        checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-0";
                    }
                    updateStats();
                };
                
                checkboxInput.addEventListener("change", (e) => {
                    const isChecked = e.target.checked;
                    if (isChecked) {
                        selected.add(display);
                        checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center bg-md-primary border-md-primary";
                        checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-100";
                    } else {
                        selected.delete(display);
                        checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center border-md-on-surface-variant hover:border-md-on-surface";
                        checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-0";
                    }
                    updateStats();
                });
                
                checkboxWrapper.appendChild(checkboxInput);
                checkboxWrapper.appendChild(checkboxBox);

                // 行点击切换选中状态（排除 checkbox 本身）
                row.onclick = (e) => {
                    if (!e.target.closest("input") && !e.target.closest("label")) {
                        toggleSelection();
                    }
                };

                // 模型名
                const label = UI.el("span", "flex-1 ml-3 font-mono text-body-medium text-md-on-surface truncate", display);
                label.title = `${display} (${upstream})`;

                // 已存在标记（按显示名判断）
                let badge = null;
                const displayNamesSet = new Set(providerData.models.map(alias => (aliasMap.get(alias) || alias)));
                if (displayNamesSet.has(display)) {
                    badge = UI.el("span", "ml-2 px-2 py-0.5 rounded-md-xs bg-md-primary-container text-md-on-primary-container text-label-small flex-shrink-0", "已添加");
                }

                row.appendChild(checkboxWrapper);
                row.appendChild(label);
                if (badge) row.appendChild(badge);

                listContainer.appendChild(row);

                // 保存引用
                rowRefs.push({
                    row,
                    display,
                    upstream,
                    setChecked: (isChecked) => {
                        checkboxInput.checked = isChecked;
                        if (isChecked) {
                            checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center bg-md-primary border-md-primary";
                            checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-100";
                        } else {
                            checkboxBox.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center border-md-on-surface-variant hover:border-md-on-surface";
                            checkIcon.className = "material-symbols-outlined text-sm text-md-on-primary transition-transform scale-0";
                        }
                    }
                });
            });

            content.appendChild(listContainer);

            // 搜索事件
            searchInput.oninput = (e) => {
                searchKeyword = e.target.value.trim();
                filterModels();
            };

            updateStats();
            return content;
        };

        UI.dialog(
            "选择模型",
            renderDialogContent,
            () => {
                providerData.models = Array.from(selected);
                renderModelChips();
                UI.snackbar(`已选择 ${selected.size} 个模型`, null, null, { variant: "success" });
                return true;
            },
            "确认选择",
            { width: "max-w-2xl", cancelText: "取消" }
        );
    },

    _renderMappingsSection(form, providerData, rerenderCb) {
        const header = UI.el("div", "inline-flex items-center gap-2 text-md-secondary text-label-large mb-2 mt-4");
        header.appendChild(UI.icon("swap_horiz", "text-lg", true));
        header.appendChild(UI.el("span", "", "模型重定向"));
        form.appendChild(header);

        const section = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg");
        section.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant mb-3", "将请求的模型名重定向到上游实际模型名"));

        const listContainer = UI.el("div", "flex flex-col gap-2 mb-3");
        
        const renderList = () => {
            listContainer.innerHTML = "";
            if (providerData.modelMappings.length === 0) {
                listContainer.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant/60 py-4 text-center border border-dashed border-md-outline-variant rounded-md-xs", "暂无模型重定向配置"));
                return;
            }
            
            providerData.modelMappings.forEach((mapping, i) => {
                const row = UI.el("div", "flex items-center gap-2 p-3 bg-md-surface-container-highest rounded-md-xs group");
                
                const fromInput = document.createElement("input");
                fromInput.type = "text";
                fromInput.value = mapping.from || "";
                fromInput.placeholder = "请求模型名";
                fromInput.className = "flex-1 md-input md-input-compact text-body-medium";
                fromInput.oninput = (e) => { providerData.modelMappings[i].from = e.target.value.trim(); if (rerenderCb) rerenderCb(); };
                
                const toInput = document.createElement("input");
                toInput.type = "text";
                toInput.value = mapping.to || "";
                toInput.placeholder = "上游模型名";
                toInput.className = "flex-1 md-input md-input-compact text-body-medium";
                toInput.oninput = (e) => { providerData.modelMappings[i].to = e.target.value.trim(); if (rerenderCb) rerenderCb(); };
                
                const deleteBtn = UI.iconBtn("delete", () => {
                    providerData.modelMappings.splice(i, 1);
                    renderList();
                    if (rerenderCb) rerenderCb();
                }, "standard", { tooltip: "删除" });
                deleteBtn.classList.add("md-hover-fade-in");
                
                row.appendChild(fromInput);
                row.appendChild(UI.icon("arrow_forward", "text-md-on-surface-variant flex-shrink-0"));
                row.appendChild(toInput);
                row.appendChild(deleteBtn);
                listContainer.appendChild(row);
            });
        };
        
        renderList();
        section.appendChild(listContainer);
        section.appendChild(UI.btn("添加重定向", () => {
            providerData.modelMappings.push({ from: "", to: "" });
            renderList();
            if (rerenderCb) rerenderCb();
        }, "tonal", "add"));
        form.appendChild(section);
    },

    _renderRoutingSection(form, prefs) {
        const header = UI.el("div", "inline-flex items-center gap-2 text-md-warning text-label-large mb-2 mt-4");
        header.appendChild(UI.icon("route", "text-lg", true));
        header.appendChild(UI.el("span", "", "路由与限流"));
        form.appendChild(header);

        const section = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");

        const weightWrap = UI.textField("渠道权重", "例如 10", "number", prefs.weight != null ? String(prefs.weight) : "");
        weightWrap.input.oninput = (e) => { prefs.weight = e.target.value; };
        section.appendChild(weightWrap.wrapper);

        const cooldownWrap = UI.textField("错误冷却时间 (秒)", "默认 300", "number", prefs.cooldown_period != null ? String(prefs.cooldown_period) : "");
        cooldownWrap.input.oninput = (e) => { prefs.cooldown_period = e.target.value; };
        section.appendChild(cooldownWrap.wrapper);

        const scheduleSelect = UI.select("API Key 调度策略", [
            { value: "round_robin", label: "轮询 (round_robin)" },
            { value: "fixed_priority", label: "固定优先级 (fixed_priority)" },
            { value: "random", label: "随机 (random)" },
            { value: "smart_round_robin", label: "智能轮询 (smart_round_robin)" },
        ], prefs.api_key_schedule_algorithm || "round_robin", (val) => { prefs.api_key_schedule_algorithm = val; });
        section.appendChild(scheduleSelect.wrapper);
        form.appendChild(section);
    },

    _renderAdvancedSection(form, prefs) {
        const header = UI.el("div", "inline-flex items-center gap-2 text-md-on-surface-variant text-label-large mb-2 mt-4");
        header.appendChild(UI.icon("tune", "text-lg", true));
        header.appendChild(UI.el("span", "", "高级设置"));
        form.appendChild(header);

        const section = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");
        // 系统提示词配置
        const systemPromptInitial = prefs.system_prompt || "";
        prefs._systemPromptText = systemPromptInitial;
        const systemPromptArea = UI.textArea(
            "渠道系统提示词 (System Prompt)",
            "可选：配置该渠道的系统提示词，将会追加到每个请求的 system 消息前面",
            systemPromptInitial,
            4,
            {
                helperText: "该提示词会追加在用户请求的系统消息之前，用于为该渠道的所有请求添加统一的上下文或指令",
                variant: "outlined"
            }
        );
        systemPromptArea.input.oninput = (e) => { prefs._systemPromptText = e.target.value; };
        section.appendChild(systemPromptArea.wrapper);


        const proxyWrap = UI.textField("代理 (Proxy)", "http://127.0.0.1:7890", "text", prefs.proxy || "");
        proxyWrap.input.oninput = (e) => { prefs.proxy = e.target.value; };
        section.appendChild(proxyWrap.wrapper);

        // 自定义 HTTP 请求头 (headers) - 使用 JSON 对象形式编辑
        const headersInitial = prefs.headers ? JSON.stringify(prefs.headers, null, 2) : "";
        prefs._headersText = headersInitial;
        const headersArea = UI.textArea(
            "自定义请求头 (headers)",
            '{\n  "Custom-Header-1": "Value-1",\n  "Custom-Header-2": "Value-2"\n}',
            headersInitial,
            3,
            {
                helperText: "可选：以 JSON 对象形式填写，将合并到每次请求的 HTTP 头中",
                variant: "outlined"
            }
        );
        headersArea.input.classList.add("font-mono");
        headersArea.input.oninput = (e) => { prefs._headersText = e.target.value; };

        const formatHeadersJson = () => {
            const raw = headersArea.input.value?.trim() || "";
            if (!raw) return;
            try {
                const obj = JSON.parse(raw);
                const pretty = JSON.stringify(obj, null, 2);
                headersArea.input.value = pretty;
                prefs._headersText = pretty;
                UI.snackbar("Headers JSON 已格式化", null, null, { variant: "success" });
            } catch (err) {
                UI.snackbar(`Headers JSON 格式错误: ${err.message}`, null, null, { variant: "error" });
            }
        };
        headersArea.input.addEventListener("change", formatHeadersJson);
        section.appendChild(headersArea.wrapper);

        // 请求体参数覆写 (post_body_parameter_overrides) - 使用 JSON 对象形式编辑
        const overridesInitial = prefs.post_body_parameter_overrides
            ? JSON.stringify(prefs.post_body_parameter_overrides, null, 2)
            : "";
        prefs._postBodyOverridesText = overridesInitial;
        const overridesArea = UI.textArea(
            "请求体参数覆写 (post_body_parameter_overrides)",
            '{\n  "all": {\n    "temperature": 0.1,\n    "top_p": 0.7\n  },\n  "gemini-2.5-pro-search": {\n    "temperature": 1.0\n  }\n}',
            overridesInitial,
            4,
            {
                helperText: "可选：以 JSON 对象形式填写；all 字段对该渠道所有模型生效，单独模型名键会覆盖 all 中的同名字段",
                variant: "outlined"
            }
        );
        overridesArea.input.classList.add("font-mono");
        overridesArea.input.oninput = (e) => { prefs._postBodyOverridesText = e.target.value; };

        const formatOverridesJson = () => {
            const raw = overridesArea.input.value?.trim() || "";
            if (!raw) return;
            try {
                const obj = JSON.parse(raw);
                const pretty = JSON.stringify(obj, null, 2);
                overridesArea.input.value = pretty;
                prefs._postBodyOverridesText = pretty;
                UI.snackbar("Overrides JSON 已格式化", null, null, { variant: "success" });
            } catch (err) {
                UI.snackbar(`Overrides JSON 格式错误: ${err.message}`, null, null, { variant: "error" });
            }
        };
        overridesArea.input.addEventListener("change", formatOverridesJson);
        section.appendChild(overridesArea.wrapper);

        section.appendChild(UI.switch("启用 Tools 能力", prefs.tools !== false, (checked) => { prefs.tools = checked; }));
        form.appendChild(section);
    },

    /**
     * 渲染插件拦截器配置部分
     */
    _renderInterceptorsSection(form, prefs) {
        const header = UI.el("div", "inline-flex items-center gap-2 text-md-tertiary text-label-large mb-2 mt-4");
        header.appendChild(UI.icon("extension", "text-lg", true));
        header.appendChild(UI.el("span", "", "插件拦截器"));
        form.appendChild(header);

        const section = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");
        
        // 说明文字
        section.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant",
            "选择在本渠道启用的插件拦截器。只有启用的插件才会处理该渠道的请求和响应。"));

        // 已启用插件的 Chip 展示
        const enabledPluginsContainer = UI.el("div", "flex flex-wrap gap-2 min-h-[40px] p-3 bg-md-surface-container-highest rounded-md-xs border border-dashed border-md-outline-variant");
        
        // 初始化 enabled_plugins
        if (!Array.isArray(prefs.enabled_plugins)) {
            prefs.enabled_plugins = [];
        }

        const renderEnabledPlugins = () => {
            enabledPluginsContainer.innerHTML = "";
            
            if (prefs.enabled_plugins.length === 0) {
                const emptyHint = UI.el("div", "w-full text-center text-body-small text-md-on-surface-variant/60 py-1",
                    "未启用任何插件拦截器，点击下方按钮配置");
                enabledPluginsContainer.appendChild(emptyHint);
                return;
            }
            
            prefs.enabled_plugins.forEach((pluginName, index) => {
                const chip = UI.el("div", "inline-flex items-center gap-2 pl-3 pr-1 py-1 rounded-md-full bg-md-tertiary-container text-md-on-tertiary-container text-label-medium");
                chip.appendChild(UI.icon("extension", "text-sm"));
                chip.appendChild(UI.el("span", "", pluginName));
                
                // 删除按钮
                const deleteBtn = UI.el("button", "w-5 h-5 rounded-full flex items-center justify-center hover:bg-md-on-tertiary-container/12 transition-colors");
                deleteBtn.appendChild(UI.icon("close", "text-sm"));
                deleteBtn.onclick = (e) => {
                    e.stopPropagation();
                    prefs.enabled_plugins.splice(index, 1);
                    renderEnabledPlugins();
                };
                chip.appendChild(deleteBtn);
                
                enabledPluginsContainer.appendChild(chip);
            });
        };
        
        renderEnabledPlugins();
        section.appendChild(enabledPluginsContainer);

        // 配置按钮
        const configBtn = UI.btn("配置插件拦截器", () => {
            ConfigView._openInterceptorPluginsPanel(prefs, renderEnabledPlugins);
        }, "tonal", "settings");
        section.appendChild(configBtn);

        form.appendChild(section);
    },

    /**
     * 打开插件拦截器配置面板（嵌套侧边栏）
     */
    async _openInterceptorPluginsPanel(prefs, onUpdate) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = adminKey ? { Authorization: `Bearer ${adminKey}` } : {};

        // 加载拦截器插件列表
        let interceptorPlugins = [];
        try {
            const res = await fetch("/v1/plugins/interceptors", { headers });
            if (res.ok) {
                const data = await res.json();
                interceptorPlugins = data.interceptor_plugins || [];
            }
        } catch (e) {
            console.error("Failed to load interceptor plugins:", e);
            UI.snackbar("加载插件列表失败", null, null, { variant: "error" });
            return;
        }

        if (interceptorPlugins.length === 0) {
            UI.snackbar("暂无可用的插件拦截器", null, null, { variant: "info" });
            return;
        }

        // 解析已有的 enabled_plugins，提取插件名和参数
        // 格式：["plugin_name:options", "plugin_name", ...]
        const parsePluginEntry = (entry) => {
            if (!entry || typeof entry !== "string") return { name: "", options: "" };
            const colonIdx = entry.indexOf(":");
            if (colonIdx === -1) return { name: entry.trim(), options: "" };
            return {
                name: entry.substring(0, colonIdx).trim(),
                options: entry.substring(colonIdx + 1).trim()
            };
        };

        // 当前选中的插件：Map<plugin_name, options_string>
        const selected = new Map();
        (prefs.enabled_plugins || []).forEach(entry => {
            const { name, options } = parsePluginEntry(entry);
            if (name) selected.set(name, options);
        });

        const renderPanelContent = () => {
            const content = UI.el("div", "flex flex-col gap-4");

            // 说明
            content.appendChild(UI.el("div", "text-body-medium text-md-on-surface-variant",
                "勾选要在本渠道启用的插件拦截器。可为每个插件配置参数（格式：plugin:options）。"));

            // 统计栏 + 全选/全不选
            const statsBar = UI.el("div", "flex items-center justify-between p-3 bg-md-surface-container-highest rounded-md-xs");
            const statsText = UI.el("span", "text-body-medium text-md-on-surface-variant",
                `共 ${interceptorPlugins.length} 个插件，已选 ${selected.size} 个`);

            const actions = UI.el("div", "flex items-center gap-2");
            const selectAllBtn = UI.btn("全选", () => {
                interceptorPlugins.forEach(p => {
                    if (!selected.has(p.plugin_name)) {
                        selected.set(p.plugin_name, "");
                    }
                });
                renderList();
                updateStats();
            }, "text", "select_all");
            const clearAllBtn = UI.btn("全不选", () => {
                selected.clear();
                renderList();
                updateStats();
            }, "text", "deselect");
            actions.appendChild(selectAllBtn);
            actions.appendChild(clearAllBtn);

            statsBar.appendChild(statsText);
            statsBar.appendChild(actions);
            content.appendChild(statsBar);

            // 手风琴式插件列表容器
            const listContainer = UI.el("div", "max-h-[420px] overflow-y-auto flex flex-col gap-2");

            // 展开状态记录：手风琴哪几项是打开的
            const expanded = new Set();

            const updateStats = () => {
                statsText.textContent = `共 ${interceptorPlugins.length} 个插件，已选 ${selected.size} 个`;
            };

            const renderList = () => {
                listContainer.innerHTML = "";

                interceptorPlugins.forEach(plugin => {
                    const isSelected = selected.has(plugin.plugin_name);
                    const currentOptions = selected.get(plugin.plugin_name) || "";
                    const isExpanded = expanded.has(plugin.plugin_name);

                    // 外层卡片 = 手风琴面板
                    const panel = UI.el("div", "border border-md-outline-variant rounded-md-xs bg-md-surface-container overflow-hidden");

                    // 头部：勾选 + 标题 + 展开箭头
                    const headerRow = UI.el("div", "flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-md-surface-container-high");
                    panel.appendChild(headerRow);

                    const left = UI.el("div", "flex items-center gap-3");

                    // Checkbox
                    const checkboxWrapper = UI.el("label", "inline-flex items-center cursor-pointer");
                    const checkboxInput = document.createElement("input");
                    checkboxInput.type = "checkbox";
                    checkboxInput.checked = isSelected;
                    checkboxInput.className = "sr-only peer";

                    const checkboxBox = UI.el("div", `w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center flex-shrink-0 ${
                        isSelected
                            ? "bg-md-primary border-md-primary"
                            : "border-md-on-surface-variant hover:border-md-on-surface"
                    }`);
                    const checkIcon = UI.icon("check", `text-sm text-md-on-primary transition-transform ${isSelected ? "scale-100" : "scale-0"}`);
                    checkboxBox.appendChild(checkIcon);

                    checkboxInput.addEventListener("change", (e) => {
                        const nowChecked = e.target.checked;
                        if (nowChecked) {
                            selected.set(plugin.plugin_name, "");
                        } else {
                            selected.delete(plugin.plugin_name);
                        }
                        // 选中状态改变后，重新渲染手风琴列表，保持 UI 与数据一致
                        renderList();
                        updateStats();
                    });

                    checkboxWrapper.appendChild(checkboxInput);
                    checkboxWrapper.appendChild(checkboxBox);
                    left.appendChild(checkboxWrapper);

                    // 标题 + 简要信息
                    const titleCol = UI.el("div", "flex flex-col gap-0.5 min-w-0");

                    const nameRow = UI.el("div", "flex items-center gap-2");
                    // 显示插件名，如果有参数则显示 plugin:options 格式
                    const displayName = currentOptions ? `${plugin.plugin_name}:${currentOptions}` : plugin.plugin_name;
                    nameRow.appendChild(UI.el("span", "text-body-medium text-md-on-surface font-medium truncate", displayName));
                    if (plugin.version) {
                        nameRow.appendChild(UI.el("span", "text-label-small text-md-on-surface-variant bg-md-surface-container-high px-1.5 py-0.5 rounded", `v${plugin.version}`));
                    }
                    titleCol.appendChild(nameRow);

                    const metaRow = UI.el("div", "flex flex-wrap gap-2");
                    if (plugin.request_interceptors && plugin.request_interceptors.length > 0) {
                        const reqChip = UI.el("span", "inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full bg-md-primary-container text-md-on-primary-container text-label-small");
                        reqChip.appendChild(UI.icon("arrow_upward", "text-xs"));
                        reqChip.appendChild(document.createTextNode(`请求: ${plugin.request_interceptors.length}`));
                        metaRow.appendChild(reqChip);
                    }
                    if (plugin.response_interceptors && plugin.response_interceptors.length > 0) {
                        const resChip = UI.el("span", "inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-small");
                        resChip.appendChild(UI.icon("arrow_downward", "text-xs"));
                        resChip.appendChild(document.createTextNode(`响应: ${plugin.response_interceptors.length}`));
                        metaRow.appendChild(resChip);
                    }
                    if (plugin.enabled === false) {
                        const disabledChip = UI.el("span", "inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full bg-md-surface-container-high text-md-on-surface-variant text-label-small");
                        disabledChip.appendChild(UI.icon("block", "text-xs"));
                        disabledChip.appendChild(document.createTextNode("插件未启用"));
                        metaRow.appendChild(disabledChip);
                    }
                    if (metaRow.childNodes.length > 0) {
                        titleCol.appendChild(metaRow);
                    }

                    left.appendChild(titleCol);
                    headerRow.appendChild(left);

                    const right = UI.el("div", "flex items-center gap-1 flex-shrink-0");
                    const expandIcon = UI.icon(isExpanded ? "expand_less" : "expand_more", "text-md-on-surface-variant");
                    right.appendChild(expandIcon);
                    headerRow.appendChild(right);

                    // 点击头部区域时，折叠/展开当前插件详情（手风琴行为）
                    headerRow.onclick = (e) => {
                        if (e.target.closest("input") || e.target.closest("button") || e.target.closest("label")) {
                            return;
                        }
                        if (isExpanded) {
                            expanded.delete(plugin.plugin_name);
                        } else {
                            expanded.add(plugin.plugin_name);
                        }
                        renderList();
                    };

                    // 展开区域：详细描述 + 拦截器信息 + 参数输入
                    if (isExpanded) {
                        const details = UI.el("div", "px-3 pb-3 pt-1 border-t border-md-outline-variant bg-md-surface-container-low flex flex-col gap-2");

                        if (plugin.description) {
                            details.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant", plugin.description));
                        }

                        // 参数输入框（所有插件都显示，插件内部决定是否使用）
                        const paramsSection = UI.el("div", "flex flex-col gap-1");
                        const paramsLabel = UI.el("div", "flex items-center gap-1 text-label-small text-md-on-surface-variant");
                        paramsLabel.appendChild(UI.icon("tune", "text-xs"));
                        paramsLabel.appendChild(document.createTextNode("插件参数"));
                        paramsSection.appendChild(paramsLabel);

                        const paramsInput = document.createElement("input");
                        paramsInput.type = "text";
                        paramsInput.value = currentOptions;
                        paramsInput.placeholder = "例如: max 或 12000 或 foo,bar";
                        paramsInput.className = "w-full px-3 py-1.5 bg-md-surface border border-md-outline rounded-md-xs text-body-small font-mono text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2 transition-all";
                        
                        // 参数变化时更新 selected Map
                        paramsInput.oninput = (e) => {
                            const newOptions = e.target.value.trim();
                            if (isSelected) {
                                selected.set(plugin.plugin_name, newOptions);
                            }
                        };
                        
                        // 失焦时刷新显示（更新标题行的显示）
                        paramsInput.onblur = () => {
                            renderList();
                        };

                        paramsSection.appendChild(paramsInput);
                        
                        // 参数提示（如果插件有 params_hint）
                        const paramsHint = plugin.metadata?.params_hint;
                        if (paramsHint) {
                            paramsSection.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant/70 mt-0.5", paramsHint));
                        } else {
                            paramsSection.appendChild(UI.el("div", "text-body-small text-md-on-surface-variant/70 mt-0.5", "参数由插件内部解析，留空表示使用默认值"));
                        }
                        
                        details.appendChild(paramsSection);

                        const interceptorsDetail = UI.el("div", "flex flex-col gap-1 text-body-small text-md-on-surface-variant/90 mt-2");
                        if (plugin.request_interceptors && plugin.request_interceptors.length > 0) {
                            const reqHeader = UI.el("div", "flex items-center gap-1 text-label-small text-md-on-surface-variant");
                            reqHeader.appendChild(UI.icon("arrow_upward", "text-xs"));
                            reqHeader.appendChild(document.createTextNode("请求拦截器"));
                            interceptorsDetail.appendChild(reqHeader);
                        }
                        if (plugin.response_interceptors && plugin.response_interceptors.length > 0) {
                            const resHeader = UI.el("div", "flex items-center gap-1 text-label-small text-md-on-surface-variant");
                            resHeader.appendChild(UI.icon("arrow_downward", "text-xs"));
                            resHeader.appendChild(document.createTextNode("响应拦截器"));
                            interceptorsDetail.appendChild(resHeader);
                        }
                        if (interceptorsDetail.childNodes.length > 0) {
                            details.appendChild(interceptorsDetail);
                        }

                        panel.appendChild(details);
                    }

                    listContainer.appendChild(panel);
                });
            };

            renderList();
            updateStats();
            content.appendChild(listContainer);

            return content;
        };

        // 使用独立 sideSheet 作为插件抽屉，避免在主表单中堆叠太多内容
        UI.sideSheet(
            "配置插件拦截器",
            renderPanelContent,
            async () => {
                // 保存选中的插件，格式：plugin_name 或 plugin_name:options
                const result = [];
                selected.forEach((options, pluginName) => {
                    if (options) {
                        result.push(`${pluginName}:${options}`);
                    } else {
                        result.push(pluginName);
                    }
                });
                prefs.enabled_plugins = result;
                if (onUpdate) onUpdate();
                UI.snackbar(`已选择 ${selected.size} 个插件`, null, null, { variant: "success" });
                return true;
            },
            "确认",
            { width: "max-w-xl", cancelText: "取消" }
        );
    },

    async _saveProvider(providerData, apiConfig, providers, providerIndex) {
        if (!providerData.name) {
            UI.snackbar("渠道标识为必填项", null, null, { variant: "error" });
            return false;
        }

        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        const newProviders = providers.slice();
        let target = providerIndex >= 0 && providers[providerIndex] 
            ? JSON.parse(JSON.stringify(providers[providerIndex])) 
            : { provider: "", base_url: "", api: "", model: [], preferences: {} };

        target.provider = providerData.name;
        target.base_url = providerData.base_url;
        // 模型前缀：如果有值则保存，否则删除该字段
        if (providerData.model_prefix && providerData.model_prefix.trim()) {
            target.model_prefix = providerData.model_prefix.trim();
        } else {
            delete target.model_prefix;
        }
        
        // 处理 API keys：将对象格式转换为字符串格式，禁用的 key 加上 ! 前缀
        const serializeApiKeys = () => {
            const serialized = [];
            for (const keyObj of providerData.api_keys) {
                // 兼容旧格式（字符串）
                if (typeof keyObj === "string") {
                    if (keyObj.trim()) serialized.push(keyObj.trim());
                } else {
                    const keyValue = (keyObj.key || "").trim();
                    if (keyValue) {
                        // 禁用的 key 加上 ! 前缀
                        serialized.push(keyObj.disabled ? `!${keyValue}` : keyValue);
                    }
                }
            }
            return serialized;
        };
        const serializedKeys = serializeApiKeys();
        target.api = !serializedKeys.length ? "" : serializedKeys.length === 1 ? serializedKeys[0] : serializedKeys;
        
        const finalModels = [...providerData.models];
        providerData.modelMappings.forEach(m => {
            if (m.from && m.to) finalModels.push({ [m.to]: m.from });
        });
        target.model = finalModels;
        
        if (providerData.engine?.trim()) target.engine = providerData.engine.trim();
        else delete target.engine;
        target.image = providerData.image;

        // 分组
        if (Array.isArray(providerData.groups) && providerData.groups.length) {
            target.groups = providerData.groups.slice();
        } else {
            target.groups = ["default"];
        }
        
        const prefs = providerData.preferences || {};
        const newPrefs = {};

        // 数值类偏好：权重与冷却时间
        if (prefs.weight !== undefined && prefs.weight !== null && String(prefs.weight).trim() !== "") {
            const n = Number(prefs.weight);
            if (!isNaN(n)) newPrefs.weight = n;
        }
        if (prefs.cooldown_period !== undefined && prefs.cooldown_period !== null && String(prefs.cooldown_period).trim() !== "") {
            const n = Number(prefs.cooldown_period);
            if (!isNaN(n)) newPrefs.cooldown_period = n;
        }

        // 已有的调度/代理/Tools 开关
        if (prefs.api_key_schedule_algorithm) newPrefs.api_key_schedule_algorithm = prefs.api_key_schedule_algorithm;
        if (typeof prefs.proxy === "string" && prefs.proxy.trim()) newPrefs.proxy = prefs.proxy.trim();
        if (typeof prefs.tools === "boolean") newPrefs.tools = prefs.tools;
        // 系统提示词
        if (typeof prefs._systemPromptText === "string" && prefs._systemPromptText.trim()) {
            newPrefs.system_prompt = prefs._systemPromptText;
        }

        // 自定义请求头 (headers) - 从 JSON 文本解析
        if (prefs._headersText && prefs._headersText.trim()) {
            try {
                const headersObj = JSON.parse(prefs._headersText);
                if (headersObj && typeof headersObj === "object" && !Array.isArray(headersObj)) {
                    newPrefs.headers = headersObj;
                } else {
                    UI.snackbar("自定义请求头必须是 JSON 对象", null, null, { variant: "error" });
                    return false;
                }
            } catch (e) {
                UI.snackbar(`自定义请求头 JSON 解析失败: ${e.message}`, null, null, { variant: "error" });
                return false;
            }
        }

        // 请求体参数覆写 (post_body_parameter_overrides) - 从 JSON 文本解析
        if (prefs._postBodyOverridesText && prefs._postBodyOverridesText.trim()) {
            try {
                const overridesObj = JSON.parse(prefs._postBodyOverridesText);
                if (overridesObj && typeof overridesObj === "object") {
                    newPrefs.post_body_parameter_overrides = overridesObj;
                } else {
                    UI.snackbar("请求体参数覆写必须是 JSON 对象", null, null, { variant: "error" });
                    return false;
                }
            } catch (e) {
                UI.snackbar(`请求体参数覆写 JSON 解析失败: ${e.message}`, null, null, { variant: "error" });
                return false;
            }
        }

        // 插件拦截器配置
        if (Array.isArray(prefs.enabled_plugins)) {
            newPrefs.enabled_plugins = prefs.enabled_plugins.slice();
        }

        // 保留未在表单中直接编辑的其他偏好字段（如 api_key_rate_limit、model_timeout 等）
        Object.keys(prefs).forEach((key) => {
            if (key.startsWith("_")) return; // 内部临时字段
            if (["weight", "cooldown_period", "api_key_schedule_algorithm", "proxy", "tools", "headers", "post_body_parameter_overrides", "enabled_plugins", "system_prompt"].includes(key)) {
                // 上面已处理
                return;
            }
            if (key in newPrefs) return;
            newPrefs[key] = prefs[key];
        });

        target.preferences = newPrefs;

        if (providerIndex >= 0) newProviders[providerIndex] = target;
        else newProviders.push(target);

        const bodyConfig = { ...apiConfig, providers: newProviders };

        try {
            const res = await fetch("/v1/api_config/update", {
                method: "POST",
                headers,
                body: JSON.stringify(bodyConfig),
            });
            if (!res.ok) {
                UI.snackbar(`更新失败: ${res.status}`, null, null, { variant: "error" });
                return false;
            }
            ConfigView._apiConfig = bodyConfig;
            ConfigView.refresh();
            UI.snackbar("配置已保存", null, null, { variant: "success" });
            return true;
        } catch (e) {
            UI.snackbar(`更新配置失败: ${e.message}`, null, null, { variant: "error" });
            return false;
        }
    },
});