/**
 * Logs View - Request Logs
 * 日志视图 - RequestStat 请求日志
 */
const LogsView = {
    _state: {
        page: 1,
        pageSize: 20,
        total: 0,
        totalPages: 0,
        items: [],
        expandedRows: new Set(), // 跟踪展开的行
        // 筛选条件
        filters: {
            startTime: null,
            endTime: null,
            provider: "",
            apiKey: "",
            model: "",
            success: null, // null=全部, true=成功, false=失败
        },
    },

    render(container) {
        const header = UI.el("div", "flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6");
        const titleSection = UI.el("div");
        titleSection.appendChild(UI.el("h2", "text-display-small text-md-on-surface", "请求日志"));
        titleSection.appendChild(
            UI.el(
                "p",
                "text-body-medium text-md-on-surface-variant mt-2",
                "查看网关中所有请求的日志记录，点击行可展开详细信息。"
            )
        );
        header.appendChild(titleSection);

        const actions = UI.el("div", "flex items-center gap-2");
        const refreshBtn = UI.iconBtn("refresh", null, "standard", { tooltip: "刷新" });
        actions.appendChild(refreshBtn);
        header.appendChild(actions);

        container.appendChild(header);

        // 筛选面板
        const filterPanel = LogsView._createFilterPanel();
        container.appendChild(filterPanel);

        const content = UI.el("div", "flex flex-col gap-4");
        content.id = "logs-content";
        container.appendChild(content);

        const loading = UI.spinner(40);
        content.appendChild(loading);

        refreshBtn.onclick = () => {
            LogsView._state.expandedRows.clear();
            LogsView._loadPage(content, LogsView._state.page, true);
        };

        LogsView._loadPage(content, 1, false);
    },

    _createFilterPanel() {
        const panel = UI.card("outlined", "p-4 mb-4");
        
        // 第一行：时间筛选
        const timeRow = UI.el("div", "flex flex-wrap items-end gap-4 mb-4");
        
        // 开始时间
        const startTimeGroup = UI.el("div", "flex flex-col gap-1");
        startTimeGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "开始时间"));
        const startTimeInput = UI.el("input", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium");
        startTimeInput.type = "datetime-local";
        startTimeInput.id = "filter-start-time";
        if (LogsView._state.filters.startTime) {
            startTimeInput.value = LogsView._formatDatetimeLocal(LogsView._state.filters.startTime);
        }
        startTimeGroup.appendChild(startTimeInput);
        timeRow.appendChild(startTimeGroup);
        
        // 结束时间
        const endTimeGroup = UI.el("div", "flex flex-col gap-1");
        endTimeGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "结束时间"));
        const endTimeInput = UI.el("input", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium");
        endTimeInput.type = "datetime-local";
        endTimeInput.id = "filter-end-time";
        if (LogsView._state.filters.endTime) {
            endTimeInput.value = LogsView._formatDatetimeLocal(LogsView._state.filters.endTime);
        }
        endTimeGroup.appendChild(endTimeInput);
        timeRow.appendChild(endTimeGroup);
        
        // 快捷时间按钮
        const quickTimeGroup = UI.el("div", "flex items-center gap-2");
        const quickButtons = [
            { label: "1小时", hours: 1 },
            { label: "24小时", hours: 24 },
            { label: "7天", hours: 24 * 7 },
            { label: "30天", hours: 24 * 30 },
        ];
        quickButtons.forEach(({ label, hours }) => {
            const btn = UI.el("button", "px-3 py-1.5 text-label-medium rounded-md bg-md-surface-container-high text-md-on-surface hover:bg-md-surface-container-highest transition-colors");
            btn.textContent = label;
            btn.onclick = () => {
                const now = new Date();
                const start = new Date(now.getTime() - hours * 60 * 60 * 1000);
                startTimeInput.value = LogsView._formatDatetimeLocal(start);
                endTimeInput.value = LogsView._formatDatetimeLocal(now);
            };
            quickTimeGroup.appendChild(btn);
        });
        timeRow.appendChild(quickTimeGroup);
        
        panel.appendChild(timeRow);
        
        // 第二行：搜索筛选
        const searchRow = UI.el("div", "flex flex-wrap items-end gap-4 mb-4");
        
        // 渠道搜索
        const providerGroup = UI.el("div", "flex flex-col gap-1 flex-1 min-w-[150px]");
        providerGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "渠道"));
        const providerInput = UI.el("input", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium w-full");
        providerInput.type = "text";
        providerInput.placeholder = "模糊搜索渠道名...";
        providerInput.id = "filter-provider";
        providerInput.value = LogsView._state.filters.provider || "";
        providerGroup.appendChild(providerInput);
        searchRow.appendChild(providerGroup);
        
        // 令牌搜索
        const apiKeyGroup = UI.el("div", "flex flex-col gap-1 flex-1 min-w-[150px]");
        apiKeyGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "令牌/分组"));
        const apiKeyInput = UI.el("input", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium w-full");
        apiKeyInput.type = "text";
        apiKeyInput.placeholder = "模糊搜索令牌...";
        apiKeyInput.id = "filter-api-key";
        apiKeyInput.value = LogsView._state.filters.apiKey || "";
        apiKeyGroup.appendChild(apiKeyInput);
        searchRow.appendChild(apiKeyGroup);
        
        // 模型搜索
        const modelGroup = UI.el("div", "flex flex-col gap-1 flex-1 min-w-[150px]");
        modelGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "模型"));
        const modelInput = UI.el("input", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium w-full");
        modelInput.type = "text";
        modelInput.placeholder = "模糊搜索模型名...";
        modelInput.id = "filter-model";
        modelInput.value = LogsView._state.filters.model || "";
        modelGroup.appendChild(modelInput);
        searchRow.appendChild(modelGroup);
        
        // 状态筛选
        const statusGroup = UI.el("div", "flex flex-col gap-1");
        statusGroup.appendChild(UI.el("label", "text-label-small text-md-on-surface-variant", "状态"));
        const statusSelect = UI.el("select", "px-3 py-2 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium");
        statusSelect.id = "filter-success";
        statusSelect.innerHTML = `
            <option value="">全部</option>
            <option value="true">成功</option>
            <option value="false">失败</option>
        `;
        if (LogsView._state.filters.success === true) {
            statusSelect.value = "true";
        } else if (LogsView._state.filters.success === false) {
            statusSelect.value = "false";
        }
        statusGroup.appendChild(statusSelect);
        searchRow.appendChild(statusGroup);
        
        panel.appendChild(searchRow);
        
        // 第三行：操作按钮和每页条数
        const actionRow = UI.el("div", "flex flex-wrap items-center justify-between gap-4");
        
        const leftActions = UI.el("div", "flex items-center gap-3");
        
        // 每页条数
        const pageSizeGroup = UI.el("div", "flex items-center gap-2");
        pageSizeGroup.appendChild(UI.el("span", "text-label-medium text-md-on-surface-variant", "每页"));
        const pageSizeSelect = UI.el("select", "px-2 py-1 rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium");
        pageSizeSelect.id = "filter-page-size";
        [10, 20, 50, 100, 200].forEach(size => {
            const option = document.createElement("option");
            option.value = size;
            option.textContent = size;
            if (size === LogsView._state.pageSize) {
                option.selected = true;
            }
            pageSizeSelect.appendChild(option);
        });
        pageSizeGroup.appendChild(pageSizeSelect);
        pageSizeGroup.appendChild(UI.el("span", "text-label-medium text-md-on-surface-variant", "条"));
        leftActions.appendChild(pageSizeGroup);
        
        actionRow.appendChild(leftActions);
        
        const rightActions = UI.el("div", "flex items-center gap-2");
        
        // 重置按钮
        const resetBtn = UI.el("button", "px-4 py-2 text-label-medium rounded-md border border-md-outline text-md-on-surface hover:bg-md-surface-container transition-colors");
        resetBtn.textContent = "重置";
        resetBtn.onclick = () => {
            LogsView._state.filters = {
                startTime: null,
                endTime: null,
                provider: "",
                apiKey: "",
                model: "",
                success: null,
            };
            startTimeInput.value = "";
            endTimeInput.value = "";
            providerInput.value = "";
            apiKeyInput.value = "";
            modelInput.value = "";
            statusSelect.value = "";
            pageSizeSelect.value = "20";
            LogsView._state.pageSize = 20;
            LogsView._state.page = 1;
            LogsView._state.expandedRows.clear();
            const content = document.getElementById("logs-content");
            if (content) LogsView._loadPage(content, 1, false);
        };
        rightActions.appendChild(resetBtn);
        
        // 搜索按钮
        const searchBtn = UI.el("button", "px-4 py-2 text-label-medium rounded-md bg-md-primary text-md-on-primary hover:opacity-90 transition-opacity");
        searchBtn.textContent = "搜索";
        searchBtn.onclick = () => {
            // 收集筛选条件
            LogsView._state.filters.startTime = startTimeInput.value ? new Date(startTimeInput.value) : null;
            LogsView._state.filters.endTime = endTimeInput.value ? new Date(endTimeInput.value) : null;
            LogsView._state.filters.provider = providerInput.value.trim();
            LogsView._state.filters.apiKey = apiKeyInput.value.trim();
            LogsView._state.filters.model = modelInput.value.trim();
            const successVal = statusSelect.value;
            LogsView._state.filters.success = successVal === "" ? null : successVal === "true";
            LogsView._state.pageSize = parseInt(pageSizeSelect.value, 10);
            LogsView._state.page = 1;
            LogsView._state.expandedRows.clear();
            const content = document.getElementById("logs-content");
            if (content) LogsView._loadPage(content, 1, false);
        };
        rightActions.appendChild(searchBtn);
        
        actionRow.appendChild(rightActions);
        panel.appendChild(actionRow);
        
        return panel;
    },

    _formatDatetimeLocal(date) {
        if (!date) return "";
        const d = new Date(date);
        const pad = (n) => n.toString().padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },

    async _loadPage(contentEl, page, keepPage) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = adminKey ? { Authorization: `Bearer ${adminKey}` } : {};

        if (!keepPage) {
            LogsView._state.page = page;
        }

        // 显示 loading
        contentEl.innerHTML = "";
        const loading = UI.spinner(40);
        contentEl.appendChild(loading);

        try {
            // 构建带筛选参数的 URL
            const params = new URLSearchParams();
            params.append("page", LogsView._state.page);
            params.append("page_size", LogsView._state.pageSize);
            
            const { filters } = LogsView._state;
            if (filters.startTime) {
                params.append("start_time", filters.startTime.toISOString());
            }
            if (filters.endTime) {
                params.append("end_time", filters.endTime.toISOString());
            }
            if (filters.provider) {
                params.append("provider", filters.provider);
            }
            if (filters.apiKey) {
                params.append("api_key", filters.apiKey);
            }
            if (filters.model) {
                params.append("model", filters.model);
            }
            if (filters.success !== null) {
                // 后端期望 true/false 字符串
                params.append("success", filters.success ? "true" : "false");
            }
            
            const url = `/v1/logs?${params.toString()}`;
            const res = await fetch(url, { headers });
            const data = await res.json().catch(() => ({}));

            if (!res.ok) {
                const detail = data.detail || data.message || `HTTP ${res.status}`;
                throw new Error(detail);
            }

            LogsView._state.total = data.total || 0;
            LogsView._state.page = data.page || LogsView._state.page;
            LogsView._state.pageSize = data.page_size || LogsView._state.pageSize;
            LogsView._state.totalPages = data.total_pages || 0;
            LogsView._state.items = Array.isArray(data.items) ? data.items : [];

            contentEl.innerHTML = "";
            LogsView._renderContent(contentEl);
        } catch (e) {
            console.error("[LogsView] Failed to load logs:", e);
            contentEl.innerHTML = "";
            const card = UI.card("outlined", "p-6 flex flex-col gap-3");
            card.appendChild(
                UI.el(
                    "div",
                    "text-title-medium text-md-error",
                    "加载日志失败"
                )
            );
            card.appendChild(
                UI.el(
                    "p",
                    "text-body-medium text-md-on-surface-variant",
                    e.message || "未知错误"
                )
            );
            contentEl.appendChild(card);
            UI.snackbar(`加载日志失败: ${e.message}`, null, null, { variant: "error" });
        }
    },

    _renderContent(contentEl) {
        const { items, total, page, pageSize, totalPages } = LogsView._state;

        // 摘要卡片
        const summaryCard = UI.card("filled", "flex flex-wrap items-center justify-between gap-3");
        const left = UI.el("div", "flex flex-col");
        left.appendChild(
            UI.el(
                "span",
                "text-title-medium text-md-on-surface",
                "请求日志"
            )
        );
        left.appendChild(
            UI.el(
                "span",
                "text-body-small text-md-on-surface-variant",
                `共 ${total} 条记录，当前显示第 ${Math.max(page, 1)} 页`
            )
        );
        summaryCard.appendChild(left);

        const right = UI.el("div", "flex items-center gap-3");
        right.appendChild(
            UI.el(
                "span",
                "text-body-small text-md-on-surface-variant",
                `每页 ${pageSize} 条`
            )
        );
        summaryCard.appendChild(right);

        contentEl.appendChild(summaryCard);

        if (!items.length) {
            const emptyCard = UI.card("outlined", "p-8 flex flex-col items-center justify-center text-center gap-3");
            emptyCard.appendChild(UI.icon("receipt_long", "text-5xl text-md-on-surface-variant"));
            emptyCard.appendChild(
                UI.el(
                    "p",
                    "text-body-large text-md-on-surface-variant",
                    "暂无日志数据"
                )
            );
            contentEl.appendChild(emptyCard);
            LogsView._renderPagination(contentEl);
            return;
        }

        // Desktop table
        const desktopWrapper = UI.el("div", "hidden lg:block");
        const tableCard = UI.card("outlined", "overflow-hidden p-0");
        const tableWrapper = UI.el("div", "overflow-x-auto");
        const table = UI.el("table", "w-full text-left min-w-[1200px]");

        const thead = UI.el("thead", "bg-md-surface-container-highest");
        thead.innerHTML = `
            <tr>
                <th class="px-3 py-3 text-label-large text-md-on-surface w-8"></th>
                <th class="px-3 py-3 text-label-large text-md-on-surface">时间</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface">渠道</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface">Key(索引)</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface">令牌/分组</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface">模型</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface text-center">用时/首字</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface text-center">提示</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface text-center">补全</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface text-center">重试</th>
                <th class="px-3 py-3 text-label-large text-md-on-surface text-center">状态</th>
            </tr>
        `;
        table.appendChild(thead);

        const tbody = UI.el("tbody", "divide-y divide-md-outline-variant");

        items.forEach((log) => {
            const isExpanded = LogsView._state.expandedRows.has(log.id);
            
            // 主行
            const tr = UI.el("tr", `hover:bg-md-surface-container transition-colors cursor-pointer ${isExpanded ? 'bg-md-surface-container' : ''}`);
            tr.onclick = () => LogsView._toggleRow(log.id, tbody, tr, log);

            // 展开图标
            const expandTd = UI.el("td", "px-3 py-3 align-middle");
            const expandIcon = UI.icon(isExpanded ? "expand_less" : "expand_more", "text-md-on-surface-variant");
            expandTd.appendChild(expandIcon);

            // 时间
            const tsTd = UI.el("td", "px-3 py-3 align-top");
            tsTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface whitespace-nowrap", LogsView._formatTimestamp(log.timestamp))
            );
            tsTd.appendChild(
                UI.el("div", "text-body-small text-md-on-surface-variant", log.id != null ? `#${log.id}` : "")
            );

            // 渠道
            const channelTd = UI.el("td", "px-3 py-3 align-top");
            channelTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface", log.provider_id || log.provider || "-")
            );

            // Key(索引)
            const keyTd = UI.el("td", "px-3 py-3 align-top");
            const keyIndex = log.provider_key_index != null ? `[${log.provider_key_index}]` : "";
            keyTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface font-mono text-sm", keyIndex || "-")
            );

            // 令牌/分组
            const tokenGroupTd = UI.el("td", "px-3 py-3 align-top");
            tokenGroupTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface", log.api_key_name || log.api_key_prefix || "-")
            );
            if (log.api_key_group) {
                tokenGroupTd.appendChild(
                    UI.el("div", "text-body-small text-md-on-surface-variant", log.api_key_group)
                );
            }

            // 模型
            const modelTd = UI.el("td", "px-3 py-3 align-top");
            modelTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface break-all", log.model || "-")
            );
            // 如果是失败请求，在模型下方显示错误摘要
            if (!log.success) {
                const errorMsg = LogsView._extractErrorMessage(log);
                if (errorMsg) {
                    const errorEl = UI.el("div", "text-body-small text-md-error mt-1 break-all line-clamp-2");
                    errorEl.textContent = errorMsg;
                    errorEl.title = errorMsg; // 完整错误信息作为 tooltip
                    modelTd.appendChild(errorEl);
                }
            }

            // 用时/首字
            const timeTd = UI.el("td", "px-3 py-3 text-center align-top");
            const pt = log.process_time != null ? `${log.process_time.toFixed(2)}s` : "-";
            const frt = log.first_response_time != null && log.first_response_time >= 0
                ? `${log.first_response_time.toFixed(2)}s` : "-";
            timeTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface", pt)
            );
            timeTd.appendChild(
                UI.el("div", "text-body-small text-md-on-surface-variant", frt)
            );

            // 提示tokens
            const promptTd = UI.el("td", "px-3 py-3 text-center align-top");
            promptTd.appendChild(
                UI.el("span", "text-body-medium text-md-on-surface",
                    log.prompt_tokens != null ? String(log.prompt_tokens) : "-")
            );

            // 补全tokens
            const completionTd = UI.el("td", "px-3 py-3 text-center align-top");
            completionTd.appendChild(
                UI.el("div", "text-body-medium text-md-on-surface",
                    log.completion_tokens != null ? String(log.completion_tokens) : "-")
            );
            // 计算并显示速度 (token/s)
            const speedInfo = LogsView._calculateTokenSpeed(log);
            if (speedInfo) {
                completionTd.appendChild(
                    UI.el("div", `text-body-small ${speedInfo.colorClass}`, speedInfo.text)
                );
            }

            // 重试次数
            const retryTd = UI.el("td", "px-3 py-3 text-center align-top");
            const retryCount = log.retry_count || 0;
            if (retryCount > 0) {
                const retryChip = UI.el("span", "inline-flex items-center px-2 py-0.5 rounded-full text-label-small bg-md-error-container text-md-on-error-container");
                retryChip.textContent = String(retryCount);
                retryTd.appendChild(retryChip);
            } else {
                retryTd.appendChild(UI.el("span", "text-body-medium text-md-on-surface-variant", "-"));
            }

            // 状态
            const statusTd = UI.el("td", "px-3 py-3 text-center align-top");
            statusTd.appendChild(LogsView._createStatusChip(log.success, log.status_code, log.is_flagged));

            tr.appendChild(expandTd);
            tr.appendChild(tsTd);
            tr.appendChild(channelTd);
            tr.appendChild(keyTd);
            tr.appendChild(tokenGroupTd);
            tr.appendChild(modelTd);
            tr.appendChild(timeTd);
            tr.appendChild(promptTd);
            tr.appendChild(completionTd);
            tr.appendChild(retryTd);
            tr.appendChild(statusTd);

            tbody.appendChild(tr);

            // 如果已展开，添加详情行
            if (isExpanded) {
                const detailRow = LogsView._createDetailRow(log);
                tbody.appendChild(detailRow);
            }
        });

        table.appendChild(tbody);
        tableWrapper.appendChild(table);
        tableCard.appendChild(tableWrapper);
        desktopWrapper.appendChild(tableCard);
        contentEl.appendChild(desktopWrapper);

        // Mobile cards
        const mobileWrapper = UI.el("div", "lg:hidden flex flex-col gap-3");
        items.forEach((log) => {
            const isExpanded = LogsView._state.expandedRows.has(log.id);
            const card = UI.card("outlined", "p-4 flex flex-col gap-2 cursor-pointer");
            card.onclick = () => {
                if (LogsView._state.expandedRows.has(log.id)) {
                    LogsView._state.expandedRows.delete(log.id);
                } else {
                    LogsView._state.expandedRows.add(log.id);
                }
                // 重新渲染
                contentEl.innerHTML = "";
                LogsView._renderContent(contentEl);
            };

            const topRow = UI.el("div", "flex items-center justify-between gap-2");
            const leftTop = UI.el("div", "flex flex-col");
            leftTop.appendChild(
                UI.el("span", "text-body-medium text-md-on-surface", LogsView._formatTimestamp(log.timestamp))
            );
            leftTop.appendChild(
                UI.el("span", "text-body-small text-md-on-surface-variant", `#${log.id} | ${log.model || "-"}`)
            );
            // 如果是失败请求，显示错误摘要
            if (!log.success) {
                const errorMsg = LogsView._extractErrorMessage(log);
                if (errorMsg) {
                    const errorEl = UI.el("div", "text-body-small text-md-error mt-1 line-clamp-2");
                    errorEl.textContent = errorMsg;
                    leftTop.appendChild(errorEl);
                }
            }
            topRow.appendChild(leftTop);
            
            const rightTop = UI.el("div", "flex items-center gap-2");
            rightTop.appendChild(UI.icon(isExpanded ? "expand_less" : "expand_more", "text-md-on-surface-variant"));
            rightTop.appendChild(LogsView._createStatusChip(log.success, log.status_code, log.is_flagged));
            topRow.appendChild(rightTop);
            card.appendChild(topRow);

            // 基本信息行
            const infoRow = UI.el("div", "grid grid-cols-2 gap-2 text-body-small");
            infoRow.appendChild(LogsView._createInfoItem("渠道", log.provider_id || log.provider || "-"));
            infoRow.appendChild(LogsView._createInfoItem("Key索引", log.provider_key_index != null ? `[${log.provider_key_index}]` : "-"));
            infoRow.appendChild(LogsView._createInfoItem("令牌", log.api_key_name || log.api_key_prefix || "-"));
            infoRow.appendChild(LogsView._createInfoItem("分组", log.api_key_group || "-"));
            infoRow.appendChild(LogsView._createInfoItem("用时", log.process_time != null ? `${log.process_time.toFixed(2)}s` : "-"));
            infoRow.appendChild(LogsView._createInfoItem("首字", log.first_response_time != null && log.first_response_time >= 0 ? `${log.first_response_time.toFixed(2)}s` : "-"));
            infoRow.appendChild(LogsView._createInfoItem("提示", log.prompt_tokens != null ? String(log.prompt_tokens) : "-"));
            // 补全 + 速度
            const completionText = log.completion_tokens != null ? String(log.completion_tokens) : "-";
            const mobileSpeedInfo = LogsView._calculateTokenSpeed(log);
            const completionDisplay = mobileSpeedInfo ? `${completionText} (${mobileSpeedInfo.text})` : completionText;
            infoRow.appendChild(LogsView._createInfoItem("补全", completionDisplay));
            card.appendChild(infoRow);

            // 展开的详情
            if (isExpanded) {
                const detailSection = LogsView._createMobileDetailSection(log);
                card.appendChild(detailSection);
            }

            mobileWrapper.appendChild(card);
        });
        contentEl.appendChild(mobileWrapper);

        LogsView._renderPagination(contentEl);
    },

    _createInfoItem(label, value) {
        const item = UI.el("div", "flex flex-col");
        item.appendChild(UI.el("span", "text-md-on-surface-variant", label));
        item.appendChild(UI.el("span", "text-md-on-surface font-medium", value));
        return item;
    },

    _toggleRow(logId, tbody, tr, log) {
        if (LogsView._state.expandedRows.has(logId)) {
            LogsView._state.expandedRows.delete(logId);
            // 移除详情行
            const detailRow = tr.nextElementSibling;
            if (detailRow && detailRow.classList.contains("detail-row")) {
                detailRow.remove();
            }
            // 更新展开图标
            const icon = tr.querySelector("span.material-symbols-outlined");
            if (icon) icon.textContent = "expand_more";
            tr.classList.remove("bg-md-surface-container");
        } else {
            LogsView._state.expandedRows.add(logId);
            // 添加详情行
            const detailRow = LogsView._createDetailRow(log);
            tr.insertAdjacentElement("afterend", detailRow);
            // 更新展开图标
            const icon = tr.querySelector("span.material-symbols-outlined");
            if (icon) icon.textContent = "expand_less";
            tr.classList.add("bg-md-surface-container");
        }
    },

    _createDetailRow(log) {
        const tr = UI.el("tr", "detail-row bg-md-surface-container-low");
        const td = UI.el("td", "px-4 py-4 max-w-0", "");
        td.colSpan = 11;

        const detailContainer = UI.el("div", "flex flex-col gap-4 w-full overflow-hidden");

        // 基本信息卡片
        const basicInfo = UI.el("div", "grid grid-cols-2 md:grid-cols-4 gap-4");
        basicInfo.appendChild(LogsView._createDetailItem("客户端 IP", log.client_ip || "-"));
        basicInfo.appendChild(LogsView._createDetailItem("Endpoint", log.endpoint || "-"));
        basicInfo.appendChild(LogsView._createDetailItem("总Tokens", log.total_tokens != null ? String(log.total_tokens) : "-"));
        basicInfo.appendChild(LogsView._createDetailItem("请求ID", log.id != null ? `#${log.id}` : "-"));
        basicInfo.appendChild(LogsView._createDetailItem("状态码", log.status_code != null ? String(log.status_code) : "-"));
        basicInfo.appendChild(LogsView._createDetailItem("请求状态", log.success ? "成功" : "失败"));
        detailContainer.appendChild(basicInfo);

        // 重试路径
        if (log.retry_path) {
            const retrySection = LogsView._createCollapsibleSection("重试路径", () => {
                try {
                    const retryData = JSON.parse(log.retry_path);
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-48 overflow-y-auto");
                    pre.textContent = JSON.stringify(retryData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small text-md-on-surface-variant", log.retry_path);
                }
            });
            detailContainer.appendChild(retrySection);
        }

        // 用户请求头
        if (log.request_headers) {
            const headersSection = LogsView._createCollapsibleSection("用户请求头", () => {
                try {
                    const headersData = JSON.parse(log.request_headers);
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-48 overflow-y-auto");
                    pre.textContent = JSON.stringify(headersData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small text-md-on-surface-variant", log.request_headers);
                }
            });
            detailContainer.appendChild(headersSection);
        }

        // 用户请求体
        if (log.request_body) {
            const bodySection = LogsView._createCollapsibleSection("用户请求体", () => {
                try {
                    const bodyData = JSON.parse(log.request_body);
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto");
                    pre.textContent = JSON.stringify(bodyData, null, 2);
                    return pre;
                } catch {
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap");
                    pre.textContent = log.request_body;
                    return pre;
                }
            });
            detailContainer.appendChild(bodySection);
        }

        // 上游请求头
        if (log.upstream_request_headers) {
            const upstreamHeadersSection = LogsView._createCollapsibleSection("上游请求头", () => {
                try {
                    const headersData = JSON.parse(log.upstream_request_headers);
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-48 overflow-y-auto");
                    pre.textContent = JSON.stringify(headersData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small text-md-on-surface-variant", log.upstream_request_headers);
                }
            });
            detailContainer.appendChild(upstreamHeadersSection);
        }

        // 上游请求体
        if (log.upstream_request_body) {
            const upstreamReqSection = LogsView._createCollapsibleSection("上游请求体", () => {
                try {
                    const bodyData = JSON.parse(log.upstream_request_body);
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto");
                    pre.textContent = JSON.stringify(bodyData, null, 2);
                    return pre;
                } catch {
                    const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap");
                    pre.textContent = log.upstream_request_body;
                    return pre;
                }
            });
            detailContainer.appendChild(upstreamReqSection);
        }

        // 上游响应体
        if (log.upstream_response_body) {
            const upstreamRespSection = LogsView._createCollapsibleSection("上游响应体", () => {
                return LogsView._renderResponseBody(log.upstream_response_body);
            });
            detailContainer.appendChild(upstreamRespSection);
        }

        // 返回给用户的响应体
        if (log.response_body) {
            const responseSection = LogsView._createCollapsibleSection("用户响应体", () => {
                return LogsView._renderResponseBody(log.response_body);
            });
            detailContainer.appendChild(responseSection);
        }

        // 过期提示
        if (log.raw_data_expires_at) {
            const expiresAt = new Date(log.raw_data_expires_at);
            const now = new Date();
            if (expiresAt > now) {
                const expireInfo = UI.el("div", "text-body-small text-md-on-surface-variant mt-2");
                expireInfo.textContent = `原始数据将于 ${expiresAt.toLocaleString()} 过期`;
                detailContainer.appendChild(expireInfo);
            }
        } else if (!log.request_headers && !log.request_body && !log.response_body) {
            const noDataInfo = UI.el("div", "text-body-small text-md-on-surface-variant mt-2");
            noDataInfo.textContent = "未配置原始数据保留或数据已过期";
            detailContainer.appendChild(noDataInfo);
        }

        td.appendChild(detailContainer);
        tr.appendChild(td);
        return tr;
    },

    _createDetailItem(label, value) {
        const item = UI.el("div", "flex flex-col gap-1");
        item.appendChild(UI.el("span", "text-label-small text-md-on-surface-variant", label));
        item.appendChild(UI.el("span", "text-body-medium text-md-on-surface", value));
        return item;
    },

    _createCollapsibleSection(title, contentRenderer) {
        const section = UI.el("div", "border border-md-outline-variant rounded-md overflow-hidden");
        
        const header = UI.el("div", "flex items-center justify-between px-4 py-2 bg-md-surface-container cursor-pointer hover:bg-md-surface-container-high transition-colors");
        const titleEl = UI.el("span", "text-label-large text-md-on-surface", title);
        const icon = UI.icon("expand_more", "text-md-on-surface-variant transition-transform");
        header.appendChild(titleEl);
        header.appendChild(icon);
        
        const content = UI.el("div", "hidden px-4 py-3 border-t border-md-outline-variant overflow-x-auto w-full");
        content.appendChild(contentRenderer());
        
        header.onclick = (e) => {
            e.stopPropagation();
            const isHidden = content.classList.contains("hidden");
            content.classList.toggle("hidden");
            icon.style.transform = isHidden ? "rotate(180deg)" : "";
        };
        
        section.appendChild(header);
        section.appendChild(content);
        return section;
    },

    _createMobileDetailSection(log) {
        const section = UI.el("div", "mt-3 pt-3 border-t border-md-outline-variant flex flex-col gap-3");
        section.onclick = (e) => e.stopPropagation();

        // 基本信息
        const basicInfo = UI.el("div", "grid grid-cols-2 gap-2 text-body-small");
        basicInfo.appendChild(LogsView._createInfoItem("客户端IP", log.client_ip || "-"));
        basicInfo.appendChild(LogsView._createInfoItem("总Tokens", log.total_tokens != null ? String(log.total_tokens) : "-"));
        basicInfo.appendChild(LogsView._createInfoItem("状态码", log.status_code != null ? String(log.status_code) : "-"));
        basicInfo.appendChild(LogsView._createInfoItem("请求状态", log.success ? "成功" : "失败"));
        section.appendChild(basicInfo);

        // 重试路径
        if (log.retry_path) {
            section.appendChild(LogsView._createCollapsibleSection("重试路径", () => {
                try {
                    const retryData = JSON.parse(log.retry_path);
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-32 overflow-y-auto");
                    pre.textContent = JSON.stringify(retryData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small", log.retry_path);
                }
            }));
        }

        // 用户请求头
        if (log.request_headers) {
            section.appendChild(LogsView._createCollapsibleSection("用户请求头", () => {
                try {
                    const headersData = JSON.parse(log.request_headers);
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-32 overflow-y-auto");
                    pre.textContent = JSON.stringify(headersData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small", log.request_headers);
                }
            }));
        }

        // 用户请求体
        if (log.request_body) {
            section.appendChild(LogsView._createCollapsibleSection("用户请求体", () => {
                try {
                    const bodyData = JSON.parse(log.request_body);
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-40 overflow-y-auto");
                    pre.textContent = JSON.stringify(bodyData, null, 2);
                    return pre;
                } catch {
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap");
                    pre.textContent = log.request_body;
                    return pre;
                }
            }));
        }

        // 上游请求头
        if (log.upstream_request_headers) {
            section.appendChild(LogsView._createCollapsibleSection("上游请求头", () => {
                try {
                    const headersData = JSON.parse(log.upstream_request_headers);
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-32 overflow-y-auto");
                    pre.textContent = JSON.stringify(headersData, null, 2);
                    return pre;
                } catch {
                    return UI.el("span", "text-body-small", log.upstream_request_headers);
                }
            }));
        }

        // 上游请求体
        if (log.upstream_request_body) {
            section.appendChild(LogsView._createCollapsibleSection("上游请求体", () => {
                try {
                    const bodyData = JSON.parse(log.upstream_request_body);
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-40 overflow-y-auto");
                    pre.textContent = JSON.stringify(bodyData, null, 2);
                    return pre;
                } catch {
                    const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap");
                    pre.textContent = log.upstream_request_body;
                    return pre;
                }
            }));
        }

        // 上游响应体
        if (log.upstream_response_body) {
            section.appendChild(LogsView._createCollapsibleSection("上游响应体", () => {
                return LogsView._renderResponseBody(log.upstream_response_body);
            }));
        }

        // 用户响应体
        if (log.response_body) {
            section.appendChild(LogsView._createCollapsibleSection("用户响应体", () => {
                return LogsView._renderResponseBody(log.response_body);
            }));
        }

        return section;
    },

    _renderPagination(contentEl) {
        const { page, totalPages, total, pageSize } = LogsView._state;

        const footer = UI.card(
            "filled",
            "mt-2 flex flex-col sm:flex-row items-center justify-between gap-3"
        );

        const info = UI.el(
            "div",
            "text-body-medium text-md-on-surface",
            totalPages > 0
                ? `第 ${page} / ${totalPages} 页，共 ${total} 条`
                : `共 ${total} 条`
        );
        footer.appendChild(info);

        const actions = UI.el("div", "flex items-center gap-2");
        
        // 首页按钮
        const firstBtn = UI.iconBtn("first_page", null, "standard", { tooltip: "首页" });
        firstBtn.onclick = () => {
            if (LogsView._state.page > 1) {
                LogsView._state.page = 1;
                LogsView._loadPage(contentEl, 1, true);
            }
        };
        if (page <= 1) firstBtn.disabled = true;
        actions.appendChild(firstBtn);
        
        // 上一页按钮
        const prevBtn = UI.iconBtn("chevron_left", null, "standard", { tooltip: "上一页" });
        prevBtn.onclick = () => {
            if (LogsView._state.page > 1) {
                LogsView._state.page -= 1;
                LogsView._loadPage(contentEl, LogsView._state.page, true);
            }
        };
        if (page <= 1) prevBtn.disabled = true;
        actions.appendChild(prevBtn);
        
        // 页码输入
        const pageInputGroup = UI.el("div", "flex items-center gap-1");
        const pageInput = UI.el("input", "w-16 px-2 py-1 text-center rounded-md border border-md-outline bg-md-surface text-md-on-surface text-body-medium");
        pageInput.type = "number";
        pageInput.min = 1;
        pageInput.max = totalPages || 1;
        pageInput.value = page;
        pageInput.onkeydown = (e) => {
            if (e.key === "Enter") {
                let newPage = parseInt(pageInput.value, 10);
                if (isNaN(newPage) || newPage < 1) newPage = 1;
                if (totalPages && newPage > totalPages) newPage = totalPages;
                if (newPage !== LogsView._state.page) {
                    LogsView._state.page = newPage;
                    LogsView._loadPage(contentEl, newPage, true);
                }
            }
        };
        pageInput.onblur = () => {
            pageInput.value = LogsView._state.page;
        };
        pageInputGroup.appendChild(pageInput);
        pageInputGroup.appendChild(UI.el("span", "text-body-medium text-md-on-surface-variant", `/ ${totalPages || 1}`));
        actions.appendChild(pageInputGroup);
        
        // 跳转按钮
        const goBtn = UI.el("button", "px-2 py-1 text-label-small rounded-md bg-md-surface-container-high text-md-on-surface hover:bg-md-surface-container-highest transition-colors");
        goBtn.textContent = "跳转";
        goBtn.onclick = () => {
            let newPage = parseInt(pageInput.value, 10);
            if (isNaN(newPage) || newPage < 1) newPage = 1;
            if (totalPages && newPage > totalPages) newPage = totalPages;
            if (newPage !== LogsView._state.page) {
                LogsView._state.page = newPage;
                LogsView._loadPage(contentEl, newPage, true);
            }
        };
        actions.appendChild(goBtn);
        
        // 下一页按钮
        const nextBtn = UI.iconBtn("chevron_right", null, "standard", { tooltip: "下一页" });
        nextBtn.onclick = () => {
            if (LogsView._state.totalPages > 0 && LogsView._state.page < LogsView._state.totalPages) {
                LogsView._state.page += 1;
                LogsView._loadPage(contentEl, LogsView._state.page, true);
            }
        };
        if (!totalPages || page >= totalPages) nextBtn.disabled = true;
        actions.appendChild(nextBtn);
        
        // 末页按钮
        const lastBtn = UI.iconBtn("last_page", null, "standard", { tooltip: "末页" });
        lastBtn.onclick = () => {
            if (LogsView._state.totalPages > 0 && LogsView._state.page < LogsView._state.totalPages) {
                LogsView._state.page = LogsView._state.totalPages;
                LogsView._loadPage(contentEl, LogsView._state.totalPages, true);
            }
        };
        if (!totalPages || page >= totalPages) lastBtn.disabled = true;
        actions.appendChild(lastBtn);

        footer.appendChild(actions);

        contentEl.appendChild(footer);
    },

    _renderResponseBody(responseBody) {
        const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap break-all");
        
        try {
            // 首先尝试解析为单个 JSON 对象（非流式响应）
            const singleJson = JSON.parse(responseBody);
            pre.textContent = JSON.stringify(singleJson, null, 2);
            return pre;
        } catch {
            // 如果失败，尝试解析为 SSE 流式格式
            try {
                const lines = responseBody.split('\n').filter(l => l.trim());
                const parsed = [];
                
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const dataStr = line.slice(6);
                        if (dataStr === '[DONE]') {
                            parsed.push('[DONE]');
                        } else {
                            try {
                                parsed.push(JSON.parse(dataStr));
                            } catch {
                                // 解析失败的行保留原样
                                parsed.push(line);
                            }
                        }
                    } else if (line.startsWith(': ')) {
                        // SSE 注释行（如心跳）
                        parsed.push(line);
                    } else {
                        parsed.push(line);
                    }
                }
                
                // 如果成功解析了至少一个对象，显示为 JSON
                if (parsed.some(item => typeof item === 'object')) {
                    pre.textContent = JSON.stringify(parsed, null, 2);
                } else {
                    // 否则显示原始文本
                    pre.textContent = responseBody;
                }
                return pre;
            } catch {
                // 完全无法解析，显示原始文本
                pre.textContent = responseBody;
                return pre;
            }
        }
    },

    _extractErrorMessage(log) {
        // 优先从 retry_path 提取最后一个错误
        if (log.retry_path) {
            try {
                const retryData = JSON.parse(log.retry_path);
                if (Array.isArray(retryData) && retryData.length > 0) {
                    const lastRetry = retryData[retryData.length - 1];
                    if (lastRetry.error) {
                        // 截取前 200 字符作为摘要
                        const error = lastRetry.error;
                        return error.length > 200 ? error.substring(0, 200) + "..." : error;
                    }
                }
            } catch {
                // 解析失败，继续尝试其他来源
            }
        }

        // 从 upstream_response_body 提取错误
        if (log.upstream_response_body) {
            try {
                const body = JSON.parse(log.upstream_response_body);
                // OpenAI 格式的错误
                if (body.error) {
                    const msg = body.error.message || body.error.code || JSON.stringify(body.error);
                    return msg.length > 200 ? msg.substring(0, 200) + "..." : msg;
                }
                // Gemini 格式的错误
                if (body.error?.message) {
                    const msg = body.error.message;
                    return msg.length > 200 ? msg.substring(0, 200) + "..." : msg;
                }
                // 其他格式：直接返回 message 或 detail
                if (body.message) {
                    return body.message.length > 200 ? body.message.substring(0, 200) + "..." : body.message;
                }
                if (body.detail) {
                    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
                    return detail.length > 200 ? detail.substring(0, 200) + "..." : detail;
                }
            } catch {
                // 不是 JSON，可能是纯文本错误
                const text = log.upstream_response_body;
                if (text && text.length > 0) {
                    return text.length > 200 ? text.substring(0, 200) + "..." : text;
                }
            }
        }

        // 从 response_body 提取错误
        if (log.response_body) {
            try {
                const body = JSON.parse(log.response_body);
                if (body.error) {
                    const msg = body.error.message || body.error.code || JSON.stringify(body.error);
                    return msg.length > 200 ? msg.substring(0, 200) + "..." : msg;
                }
            } catch {
                // 忽略
            }
        }

        return null;
    },

    _createStatusChip(success, statusCode, isFlagged) {
        const chip = UI.el(
            "span",
            "inline-flex items-center gap-1 px-2 py-0.5 rounded-md-full text-label-small"
        );
        
        // 优先显示道德审查失败
        if (isFlagged) {
            chip.classList.add("md-chip-status-error");
            chip.appendChild(UI.icon("report", "text-sm"));
            chip.appendChild(document.createTextNode("Flagged"));
        } else if (success) {
            // 成功请求
            chip.classList.add("md-chip-status-healthy");
            chip.appendChild(UI.icon("check_circle", "text-sm"));
            const text = statusCode ? `${statusCode}` : "OK";
            chip.appendChild(document.createTextNode(text));
        } else {
            // 失败请求
            chip.classList.add("md-chip-status-error");
            chip.appendChild(UI.icon("error", "text-sm"));
            const text = statusCode ? `${statusCode}` : "Failed";
            chip.appendChild(document.createTextNode(text));
        }
        return chip;
    },

    _formatTimestamp(value) {
        if (!value) return "-";
        try {
            const d = new Date(value);
            if (Number.isNaN(d.getTime())) return String(value);
            return d.toLocaleString();
        } catch {
            return String(value);
        }
    },

    /**
     * 计算 token 生成速度
     * 对于推理模型，优先使用 content_start_time（正文开始时间）来计算正文生成速度
     * 这样可以排除思维链时间，更准确地反映正文生成速度
     *
     * @param {Object} log - 日志对象
     * @returns {Object|null} - { text: "xx.x t/s", colorClass: "text-..." } 或 null
     */
    _calculateTokenSpeed(log) {
        // 需要有 completion_tokens 和 process_time
        if (log.completion_tokens == null || log.completion_tokens <= 0) return null;
        if (log.process_time == null) return null;
        
        // 优先使用 content_start_time（正文开始时间）
        // 对于推理模型，这是正文开始输出的时间，可以准确计算正文生成速度
        let startTime = null;
        if (log.content_start_time != null && log.content_start_time >= 0) {
            startTime = log.content_start_time;
        } else if (log.first_response_time != null && log.first_response_time >= 0) {
            startTime = log.first_response_time;
        }
        
        if (startTime == null) return null;
        
        const generationTime = log.process_time - startTime;
        if (generationTime <= 0) return null;
        
        const speed = log.completion_tokens / generationTime;
        const text = `${speed.toFixed(1)} t/s`;
        
        // 根据速度设置颜色
        let colorClass = "text-md-on-surface-variant";
        if (speed >= 100) {
            colorClass = "text-md-tertiary"; // 非常快
        } else if (speed >= 50) {
            colorClass = "text-md-primary"; // 快
        } else if (speed < 20) {
            colorClass = "text-md-error"; // 慢
        }
        
        return { text, colorClass };
    },
};