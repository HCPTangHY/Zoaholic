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

        const content = UI.el("div", "flex flex-col gap-4");
        container.appendChild(content);

        const loading = UI.spinner(40);
        content.appendChild(loading);

        refreshBtn.onclick = () => {
            LogsView._state.expandedRows.clear();
            LogsView._loadPage(content, LogsView._state.page, true);
        };

        LogsView._loadPage(content, 1, false);
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
            const url = `/v1/logs?page=${encodeURIComponent(LogsView._state.page)}&page_size=${encodeURIComponent(
                LogsView._state.pageSize
            )}`;
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
                UI.el("span", "text-body-medium text-md-on-surface",
                    log.completion_tokens != null ? String(log.completion_tokens) : "-")
            );

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
            infoRow.appendChild(LogsView._createInfoItem("补全", log.completion_tokens != null ? String(log.completion_tokens) : "-"));
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
        const td = UI.el("td", "px-4 py-4", "");
        td.colSpan = 11;

        const detailContainer = UI.el("div", "flex flex-col gap-4");

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

        // 请求头
        if (log.request_headers) {
            const headersSection = LogsView._createCollapsibleSection("请求头", () => {
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

        // 请求体
        if (log.request_body) {
            const bodySection = LogsView._createCollapsibleSection("请求体", () => {
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

        // 返回体
        if (log.response_body) {
            const responseSection = LogsView._createCollapsibleSection("返回体", () => {
                const pre = UI.el("pre", "text-body-small font-mono bg-md-surface-container p-3 rounded-md overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap");
                
                try {
                    // 首先尝试解析为单个 JSON 对象（非流式响应）
                    const singleJson = JSON.parse(log.response_body);
                    pre.textContent = JSON.stringify(singleJson, null, 2);
                    return pre;
                } catch {
                    // 如果失败，尝试解析为 SSE 流式格式
                    try {
                        const lines = log.response_body.split('\n').filter(l => l.trim());
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
                            pre.textContent = log.response_body;
                        }
                        return pre;
                    } catch {
                        // 完全无法解析，显示原始文本
                        pre.textContent = log.response_body;
                        return pre;
                    }
                }
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
        
        const content = UI.el("div", "hidden px-4 py-3 border-t border-md-outline-variant");
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

        // 请求头
        if (log.request_headers) {
            section.appendChild(LogsView._createCollapsibleSection("请求头", () => {
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

        // 请求体
        if (log.request_body) {
            section.appendChild(LogsView._createCollapsibleSection("请求体", () => {
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

        // 返回体
        if (log.response_body) {
            section.appendChild(LogsView._createCollapsibleSection("返回体", () => {
                const pre = UI.el("pre", "text-xs font-mono bg-md-surface-container p-2 rounded overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap");
                
                try {
                    // 首先尝试解析为单个 JSON 对象（非流式响应）
                    const singleJson = JSON.parse(log.response_body);
                    pre.textContent = JSON.stringify(singleJson, null, 2);
                    return pre;
                } catch {
                    // 如果失败，尝试解析为 SSE 流式格式
                    try {
                        const lines = log.response_body.split('\n').filter(l => l.trim());
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
                                        parsed.push(line);
                                    }
                                }
                            } else {
                                parsed.push(line);
                            }
                        }
                        
                        if (parsed.some(item => typeof item === 'object')) {
                            pre.textContent = JSON.stringify(parsed, null, 2);
                        } else {
                            pre.textContent = log.response_body;
                        }
                        return pre;
                    } catch {
                        pre.textContent = log.response_body;
                        return pre;
                    }
                }
            }));
        }

        return section;
    },

    _renderPagination(contentEl) {
        const { page, totalPages, total } = LogsView._state;

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
        const prevBtn = UI.btn("上一页", () => {
            if (LogsView._state.page > 1) {
                LogsView._state.page -= 1;
                LogsView._loadPage(contentEl, LogsView._state.page, true);
            }
        }, "text", "chevron_left");
        const nextBtn = UI.btn("下一页", () => {
            if (LogsView._state.totalPages > 0 && LogsView._state.page < LogsView._state.totalPages) {
                LogsView._state.page += 1;
                LogsView._loadPage(contentEl, LogsView._state.page, true);
            }
        }, "text", "chevron_right");

        if (page <= 1) {
            prevBtn.disabled = true;
        }
        if (!totalPages || page >= totalPages) {
            nextBtn.disabled = true;
        }

        actions.appendChild(prevBtn);
        actions.appendChild(nextBtn);
        footer.appendChild(actions);

        contentEl.appendChild(footer);
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
};