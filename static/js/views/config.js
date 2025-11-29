/**
 * Config View - Provider Configuration
 * 配置视图 - 渠道配置管理
 */
const ConfigView = {
    _apiConfig: null,

    render(container) {
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
            <th class="px-4 py-3 text-center text-label-large text-md-on-surface">状态</th>
            <th class="px-4 py-3 text-center text-label-large text-md-on-surface">优先级</th>
            <th class="px-4 py-3 text-right text-label-large text-md-on-surface">操作</th>
        </tr>`;
        table.appendChild(thead);

        const tbody = UI.el("tbody", "divide-y divide-md-outline-variant");

        if (!sortedProviders || sortedProviders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-12 text-body-medium text-md-on-surface-variant">暂无渠道配置，点击"添加渠道"开始配置</td></tr>';
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
        const card = UI.card("outlined", "p-4");
        
        // 头部：名称 + 状态
        const header = UI.el("div", "flex items-center justify-between mb-3");
        const nameSection = UI.el("div", "flex items-center gap-2");
        nameSection.appendChild(UI.icon("dns", "text-md-primary text-xl"));
        nameSection.appendChild(UI.el("span", "text-title-medium text-md-on-surface font-medium", provider.provider || provider.name || ""));
        header.appendChild(nameSection);
        
        // 状态标签
        const isEnabled = provider.enabled !== false;
        const statusChip = UI.el("span", `inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full text-label-small ${
            isEnabled
                ? "bg-md-success-container text-md-on-success-container"
                : "bg-md-surface-container-high text-md-on-surface-variant"
        }`);
        statusChip.appendChild(UI.icon(isEnabled ? "check_circle" : "cancel", "text-sm"));
        statusChip.appendChild(document.createTextNode(isEnabled ? "启用" : "禁用"));
        header.appendChild(statusChip);
        card.appendChild(header);
        
        // 信息区域：分组、类型、优先级
        const infoSection = UI.el("div", "flex flex-wrap gap-2 mb-4");
        
        // 分组
        const group = provider.group || provider.preferences?.group || "默认";
        const groupChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-medium");
        groupChip.appendChild(UI.icon("folder", "text-sm"));
        groupChip.appendChild(document.createTextNode(group));
        infoSection.appendChild(groupChip);
        
        // 类型
        const engine = provider.engine || "openai";
        const typeChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-tertiary-container text-md-on-tertiary-container text-label-medium font-mono");
        typeChip.appendChild(UI.icon("memory", "text-sm"));
        typeChip.appendChild(document.createTextNode(engine));
        infoSection.appendChild(typeChip);
        
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
        const tr = UI.el("tr", "hover:bg-md-surface-container transition-colors group");

        // 渠道名称
        const name = provider.provider || provider.name || "";
        const nameTd = UI.el("td", "px-4 py-3");
        const nameContent = UI.el("div", "flex items-center gap-2");
        nameContent.appendChild(UI.icon("dns", "text-md-on-surface-variant group-hover:text-md-primary transition-colors text-lg"));
        nameContent.appendChild(UI.el("span", "text-body-large text-md-on-surface font-medium", name));
        nameTd.appendChild(nameContent);

        // 分组 - 使用带图标的 Chip 样式
        const group = provider.group || provider.preferences?.group || "默认";
        const groupTd = UI.el("td", "px-4 py-3");
        const groupChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-secondary-container text-md-on-secondary-container text-label-medium");
        groupChip.appendChild(UI.icon("folder", "text-sm"));
        groupChip.appendChild(document.createTextNode(group));
        groupTd.appendChild(groupChip);

        // 类型 (engine) - 使用带图标的 Chip 样式
        const engine = provider.engine || "openai";
        const typeTd = UI.el("td", "px-4 py-3");
        const typeChip = UI.el("span", "inline-flex items-center gap-1.5 px-3 py-1 rounded-md-full bg-md-tertiary-container text-md-on-tertiary-container text-label-medium font-mono");
        typeChip.appendChild(UI.icon("memory", "text-sm"));
        typeChip.appendChild(document.createTextNode(engine));
        typeTd.appendChild(typeChip);

        // 状态
        const isEnabled = provider.enabled !== false;
        const statusTd = UI.el("td", "px-4 py-3 text-center");
        const statusChip = UI.el("span", `inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full text-label-small ${
            isEnabled
                ? "bg-md-success-container text-md-on-success-container"
                : "bg-md-surface-container-high text-md-on-surface-variant"
        }`);
        statusChip.appendChild(UI.icon(isEnabled ? "check_circle" : "cancel", "text-sm"));
        statusChip.appendChild(document.createTextNode(isEnabled ? "启用" : "禁用"));
        statusTd.appendChild(statusChip);

        // 优先级/权重 - 可编辑输入框
        const weight = (provider.preferences?.weight) || provider.weight || 0;
        const priorityTd = UI.el("td", "px-4 py-3 text-center");
        const priorityInput = document.createElement("input");
        priorityInput.type = "number";
        priorityInput.min = "0";
        priorityInput.value = weight;
        priorityInput.className = "w-16 px-2 py-1 bg-md-surface border border-md-outline rounded-md-xs font-mono text-body-medium text-md-on-surface text-center focus:outline-none focus:border-md-primary focus:border-2 transition-all hover:border-md-primary/50";
        priorityInput.onclick = (e) => e.stopPropagation();
        priorityInput.onchange = (e) => {
            const newWeight = parseInt(e.target.value) || 0;
            ConfigView._updateProviderWeight(index, providers, newWeight);
        };
        priorityTd.appendChild(priorityInput);

        // 操作按钮组
        const actionsTd = UI.el("td", "px-4 py-3");
        const actionsGroup = UI.el("div", "flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity");

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
        const models = [];
        modelArray.forEach(m => {
            if (typeof m === "string") {
                models.push(m);
            } else if (typeof m === "object") {
                // 模型映射，取 key（上游模型名）
                Object.keys(m).forEach(k => models.push(k));
            }
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
     */
    _openTestDialog(provider, providerName, models) {
        // 测试状态管理
        const testState = {
            concurrency: 3,
            searchKeyword: "",
            results: new Map(), // model -> { status: 'pending'|'testing'|'success'|'error', latency: number, error: string }
            isRunning: false,
            abortController: null,
        };

        // 初始化所有模型状态
        models.forEach(m => {
            testState.results.set(m, { status: "pending", latency: null, error: null });
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
            concurrencyInput.className = "w-12 px-2 py-1.5 bg-md-surface border border-md-outline rounded-md-xs text-body-small text-md-on-surface text-center focus:outline-none focus:border-md-primary focus:border-2";
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
            searchInput.className = "w-full pl-8 pr-3 py-1.5 bg-md-surface border border-md-outline rounded-md-full text-body-small text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2 transition-all";
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
                
                models.forEach(model => {
                    if (keyword && !model.toLowerCase().includes(keyword)) {
                        return;
                    }

                    const result = testState.results.get(model);
                    
                    // MD3 List Item: 56dp 高度，标准结构
                    const listItem = UI.el("li", "flex items-center h-14 px-4 hover:bg-md-surface-container-low active:bg-md-surface-container transition-colors cursor-default");
                    // 行空白区域点击亦可复制（排除按钮/输入控件）
                    listItem.setAttribute("data-tooltip", "点击复制模型名");
                    listItem.onclick = (e) => {
                        if (!e.target.closest("button") && !e.target.closest("input")) {
                            copyModelName(model);
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
                        copyModelName(model);
                    };
                    
                    // Headline: 模型名称（点击复制）
                    const headline = UI.el("div", "font-mono text-body-medium text-md-on-surface truncate", model);
                    headline.title = model;
                    headline.setAttribute("data-tooltip", "点击复制模型名");
                    headline.onclick = (e) => {
                        e.stopPropagation();
                        copyModelName(model);
                    };
                    contentArea.appendChild(headline);
                    
                    // Supporting text: 耗时或错误信息
                    const supporting = UI.el("div", "text-body-small text-md-on-surface-variant truncate h-5");
                    supporting.appendChild(ConfigView._createSupportingText(result));
                    contentArea.appendChild(supporting);
                    
                    listItem.appendChild(contentArea);
                    
                    // Trailing: 测试按钮
                    const trailing = UI.el("div", "flex items-center gap-2 ml-4 flex-shrink-0");
                    const testBtn = UI.iconBtn("play_arrow", () => ConfigView._testSingleModel(provider, model, testState, updateRow), "standard", { tooltip: "测试此模型" });
                    testBtn.classList.add("text-md-primary");
                    if (result.status === "testing") {
                        testBtn.disabled = true;
                        testBtn.classList.add("opacity-50");
                    }
                    trailing.appendChild(testBtn);
                    listItem.appendChild(trailing);
                    
                    list.appendChild(listItem);
                    rowRefs.set(model, { listItem, leading, supporting, testBtn });
                });

                // 空状态
                if (list.children.length === 0) {
                    const emptyState = UI.el("li", "flex flex-col items-center justify-center py-12 text-md-on-surface-variant");
                    emptyState.appendChild(UI.icon("search_off", "text-4xl mb-2"));
                    emptyState.appendChild(UI.el("span", "text-body-medium", "没有匹配的模型"));
                    list.appendChild(emptyState);
                }
            };

            // 更新单行
            const updateRow = (model) => {
                const ref = rowRefs.get(model);
                if (!ref) return;

                const result = testState.results.get(model);
                
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

                // 重置所有状态
                models.forEach(m => {
                    testState.results.set(m, { status: "pending", latency: null, error: null });
                });
                renderRows();

                // 并发测试
                const queue = [...models];
                const runNext = async () => {
                    while (queue.length > 0 && testState.isRunning) {
                        const model = queue.shift();
                        if (!model) break;
                        await ConfigView._testSingleModel(provider, model, testState, updateRow);
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
     */
    async _testSingleModel(provider, model, testState, updateCallback) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = { "Content-Type": "application/json" };
        if (adminKey) headers["Authorization"] = `Bearer ${adminKey}`;

        // 设置为测试中
        testState.results.set(model, { status: "testing", latency: null, error: null });
        if (updateCallback) updateCallback(model);

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
                    model: model,
                    timeout: 30,
                }),
                signal: testState.abortController?.signal,
            });

            const data = await res.json().catch(() => ({}));

            if (res.ok && data.success) {
                testState.results.set(model, {
                    status: "success",
                    latency: data.latency_ms || null,
                    error: null
                });
            } else {
                const errorMsg = data.error || data.detail || data.message || `HTTP ${res.status}`;
                testState.results.set(model, { status: "error", latency: null, error: errorMsg });
            }
        } catch (e) {
            if (e.name === "AbortError") {
                testState.results.set(model, { status: "pending", latency: null, error: null });
            } else {
                testState.results.set(model, { status: "error", latency: null, error: e.message });
            }
        }

        if (updateCallback) updateCallback(model);
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
            Views.render("config");
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
            Views.render("config");
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
            Views.render("config");
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
            api_keys: [],
            models: [],
            modelMappings: [],
            engine: originalProvider?.engine || "",
            image: originalProvider?.image !== false,
            preferences: { ...rawPreferences },
        };

        if (originalProvider) {
            if (originalProvider.api !== undefined && originalProvider.api !== null) {
                if (Array.isArray(originalProvider.api)) {
                    providerData.api_keys = originalProvider.api.slice();
                } else if (typeof originalProvider.api === "string" && originalProvider.api.trim()) {
                    providerData.api_keys = [originalProvider.api.trim()];
                }
            } else if (Array.isArray(originalProvider.api_keys)) {
                providerData.api_keys = originalProvider.api_keys.slice();
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
        const basicHeader = UI.el("div", "inline-flex items-center gap-2 text-md-primary text-label-large mb-2");
        basicHeader.appendChild(UI.icon("settings", "text-lg", true));
        basicHeader.appendChild(UI.el("span", "", "基础配置"));
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
 
        // Base URL：占位文本会根据所选渠道的 default_base_url 动态更新，这里不再写死 OpenAI 示例
        const urlWrap = UI.textField("上游 Base URL", "", "text", providerData.base_url, { required: true });
        // 标记为配置用 Base URL 输入框，便于 _loadChannelTypes 动态更新 placeholder
        urlWrap.input.setAttribute("data-config-base-url-input", "1");
        urlWrap.input.oninput = (e) => { providerData.base_url = e.target.value; };
        basicSection.appendChild(urlWrap.wrapper);
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
                        "提示: 输入回车可快速添加新密钥"
                    )
                );
        
                const renderKeyRows = () => {
                    listContainer.innerHTML = "";
        
                    const keys = providerData.api_keys;
                    if (!keys.length) {
                        keys.push("");
                    }
        
                    let nonEmptyCount = 0;
        
                    keys.forEach((value, index) => {
                        if (value && value.trim()) nonEmptyCount += 1;
        
                        const row = UI.el("div", "flex items-center gap-2 px-3 py-2");
        
                        // 序号
                        const indexLabel = UI.el(
                            "span",
                            "w-6 text-right text-body-small text-md-on-surface-variant",
                            String(index + 1)
                        );
        
                        // 输入框
                        const input = document.createElement("input");
                        input.type = "text";
                        input.className =
                            "flex-1 px-2 py-1 bg-md-surface border border-transparent rounded-md-xs " +
                            "text-body-small font-mono text-md-on-surface " +
                            "focus:outline-none focus:border-md-primary focus:border-2";
                        input.value = value || "";
                        input.setAttribute("data-key-index", String(index));
        
                        input.oninput = (e) => {
                            providerData.api_keys[index] = e.target.value;
                        };
        
                        // 回车快速添加/跳转
                        input.onkeydown = (e) => {
                            if (e.key === "Enter") {
                                e.preventDefault();
                                const lastIndex = providerData.api_keys.length - 1;
                                if (index === lastIndex) {
                                    providerData.api_keys.push("");
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
                        row.appendChild(copyBtn);
                        row.appendChild(deleteBtn);
                        listContainer.appendChild(row);
                    });
        
                    keysSummary.textContent = `总计: ${nonEmptyCount} 个密钥`;
                };
        
                // 按钮行为
                addKeyBtn.onclick = () => {
                    providerData.api_keys.push("");
                    renderKeyRows();
                    setTimeout(() => {
                        const inputs = listContainer.querySelectorAll('input[data-key-index]');
                        const last = inputs[inputs.length - 1];
                        if (last) last.focus();
                    }, 0);
                };
        
                copyAllBtn.onclick = () => {
                    const nonEmptyKeys = providerData.api_keys
                        .map((k) => (k || "").trim())
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
        if (!providerData.base_url) {
            UI.snackbar("请先填写 Base URL", null, null, { variant: "error" });
            return;
        }
        if (!providerData.api_keys.length) {
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
            const res = await fetch("/v1/channels/fetch_models", {
                method: "POST",
                headers,
                body: JSON.stringify({
                    engine: providerData.engine,
                    base_url: providerData.base_url,
                    api_key: providerData.api_keys[0],
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
                const row = UI.el("div", "px-4 py-2 flex items-center hover:bg-md-surface-container transition-colors border-b border-md-outline-variant last:border-b-0");
                // 行空白区域点击复制（排除复选框/按钮/label）
                row.setAttribute("data-tooltip", "点击复制模型名");
                row.onclick = (e) => {
                    if (!e.target.closest("input") && !e.target.closest("button") && !e.target.closest("label")) {
                        copyModelName(display);
                    }
                };

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

                // 模型名（点击复制显示名）
                const label = UI.el("span", "flex-1 ml-3 font-mono text-body-medium text-md-on-surface truncate", display);
                label.title = `${display} (${upstream})`;
                label.setAttribute("data-tooltip", "点击复制模型名");
                label.onclick = (e) => {
                    e.stopPropagation();
                    copyModelName(display);
                };

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
                fromInput.className = "flex-1 px-3 py-2 bg-md-surface border border-md-outline rounded-md-xs text-body-medium text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2";
                fromInput.oninput = (e) => { providerData.modelMappings[i].from = e.target.value.trim(); if (rerenderCb) rerenderCb(); };
                
                const toInput = document.createElement("input");
                toInput.type = "text";
                toInput.value = mapping.to || "";
                toInput.placeholder = "上游模型名";
                toInput.className = "flex-1 px-3 py-2 bg-md-surface border border-md-outline rounded-md-xs text-body-medium text-md-on-surface focus:outline-none focus:border-md-primary focus:border-2";
                toInput.oninput = (e) => { providerData.modelMappings[i].to = e.target.value.trim(); if (rerenderCb) rerenderCb(); };
                
                const deleteBtn = UI.iconBtn("delete", () => {
                    providerData.modelMappings.splice(i, 1);
                    renderList();
                    if (rerenderCb) rerenderCb();
                }, "standard", { tooltip: "删除" });
                deleteBtn.classList.add("opacity-0", "group-hover:opacity-100", "transition-opacity");
                
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

    async _saveProvider(providerData, apiConfig, providers, providerIndex) {
        if (!providerData.name || !providerData.base_url) {
            UI.snackbar("渠道标识和 Base URL 为必填项", null, null, { variant: "error" });
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
        target.api = !providerData.api_keys.length ? "" : providerData.api_keys.length === 1 ? providerData.api_keys[0] : providerData.api_keys.slice();
        
        const finalModels = [...providerData.models];
        providerData.modelMappings.forEach(m => {
            if (m.from && m.to) finalModels.push({ [m.to]: m.from });
        });
        target.model = finalModels;
        
        if (providerData.engine?.trim()) target.engine = providerData.engine.trim();
        else delete target.engine;
        target.image = providerData.image;

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

        // 保留未在表单中直接编辑的其他偏好字段（如 api_key_rate_limit、model_timeout 等）
        Object.keys(prefs).forEach((key) => {
            if (key.startsWith("_")) return; // 内部临时字段
            if (["weight", "cooldown_period", "api_key_schedule_algorithm", "proxy", "tools", "headers", "post_body_parameter_overrides"].includes(key)) {
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
            Views.render("config");
            UI.snackbar("配置已保存", null, null, { variant: "success" });
            return true;
        } catch (e) {
            UI.snackbar(`更新配置失败: ${e.message}`, null, null, { variant: "error" });
            return false;
        }
    },
});