/**
 * Settings View - Global System Settings
 * 系统设置视图 - 全局配置管理
 */
const SettingsView = {
    _apiConfig: null,

    render(container) {
        const header = UI.el("div", "flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6");
        const titleSection = UI.el("div");
        titleSection.appendChild(UI.el("h2", "text-display-small text-md-on-surface", "系统设置"));
        titleSection.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant mt-2", "管理全局配置和系统首选项"));
        header.appendChild(titleSection);

        container.appendChild(header);

        const loading = UI.spinner();
        container.appendChild(loading);

        SettingsView._loadSettings(container, loading);
    },

    async _loadSettings(container, loading) {
        const adminKey = AppConfig?.currentUser?.key || null;
        const headers = adminKey ? { Authorization: `Bearer ${adminKey}` } : {};

        let apiConfig = null;

        try {
            const res = await fetch("/v1/api_config", { headers });
            if (res.ok) {
                const json = await res.json();
                apiConfig = json.api_config || json;
            }
        } catch (e) {
            console.error("Failed to load /v1/api_config:", e);
            UI.snackbar("加载配置失败", null, null, { variant: "error" });
        }

        SettingsView._apiConfig = apiConfig || { preferences: {} };
        loading.remove();

        SettingsView._renderForm(container);
    },

    _renderForm(container) {
        const card = UI.card("outlined", "p-6");
        const form = UI.el("div", "flex flex-col gap-6");

        const preferences = SettingsView._apiConfig.preferences || {};

        // 高可用性设置
        const availabilitySection = UI.el("div", "flex flex-col gap-4");
        const availabilityHeader = UI.el("div", "inline-flex items-center gap-2 text-md-primary text-label-large mb-2");
        availabilityHeader.appendChild(UI.icon("sync", "text-lg", true));
        availabilityHeader.appendChild(UI.el("span", "", "高可用性"));
        availabilitySection.appendChild(availabilityHeader);

        const availabilityCard = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");

        // 最大重试次数
        const maxRetryWrap = UI.textField(
            "最大重试次数",
            "默认 10",
            "number",
            preferences.max_retry_count != null ? String(preferences.max_retry_count) : "10",
            {
                helperText: "多渠道场景下的最大重试次数上限（1-100），默认 10"
            }
        );
        maxRetryWrap.input.min = "1";
        maxRetryWrap.input.max = "100";
        maxRetryWrap.input.oninput = (e) => {
            let val = parseInt(e.target.value) || 10;
            if (val < 1) val = 1;
            if (val > 100) val = 100;
            preferences.max_retry_count = val;
        };
        availabilityCard.appendChild(maxRetryWrap.wrapper);

        // 渠道冷却时间
        const cooldownWrap = UI.textField(
            "渠道冷却时间 (秒)",
            "默认 300",
            "number",
            preferences.cooldown_period != null ? String(preferences.cooldown_period) : "300",
            {
                helperText: "失败渠道的冷却时间，设为 0 禁用冷却机制"
            }
        );
        cooldownWrap.input.min = "0";
        cooldownWrap.input.oninput = (e) => {
            preferences.cooldown_period = parseInt(e.target.value) || 0;
        };
        availabilityCard.appendChild(cooldownWrap.wrapper);

        // 全局调度算法
        const scheduleSelect = UI.select(
            "全局调度算法",
            [
                { value: "fixed_priority", label: "固定优先级 (fixed_priority) - 始终使用第一个可用渠道" },
                { value: "round_robin", label: "轮询 (round_robin) - 按顺序依次请求" },
                { value: "weighted_round_robin", label: "加权轮询 (weighted_round_robin) - 按渠道权重分配" },
                { value: "lottery", label: "抽奖 (lottery) - 按权重随机选择" },
                { value: "random", label: "随机 (random) - 完全随机" },
                { value: "smart_round_robin", label: "智能轮询 (smart_round_robin) - 基于历史成功率" },
            ],
            preferences.SCHEDULING_ALGORITHM || "fixed_priority",
            (val) => { preferences.SCHEDULING_ALGORITHM = val; }
        );
        // 添加说明文字
        const scheduleHelperText = UI.el("div", "text-body-small text-md-on-surface-variant mt-1",
            "提示：当渠道配置了优先级(weight)时，建议使用「加权轮询」或「抽奖」算法");
        scheduleSelect.wrapper.appendChild(scheduleHelperText);
        availabilityCard.appendChild(scheduleSelect.wrapper);

        availabilitySection.appendChild(availabilityCard);
        form.appendChild(availabilitySection);

        // 速率限制设置
        const rateLimitSection = UI.el("div", "flex flex-col gap-4 mt-4");
        const rateLimitHeader = UI.el("div", "inline-flex items-center gap-2 text-md-secondary text-label-large mb-2");
        rateLimitHeader.appendChild(UI.icon("speed", "text-lg", true));
        rateLimitHeader.appendChild(UI.el("span", "", "速率限制"));
        rateLimitSection.appendChild(rateLimitHeader);

        const rateLimitCard = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");

        const rateLimitWrap = UI.textField(
            "全局速率限制",
            "例如: 60/min 或 100/hour,1000/day",
            "text",
            preferences.rate_limit || "999999/min",
            {
                helperText: "支持多种周期组合：15/min,100/hour,1000/day,10000/month"
            }
        );
        rateLimitWrap.input.oninput = (e) => {
            preferences.rate_limit = e.target.value;
        };
        rateLimitCard.appendChild(rateLimitWrap.wrapper);

        rateLimitSection.appendChild(rateLimitCard);
        form.appendChild(rateLimitSection);

        // 数据保留设置
        const dataSection = UI.el("div", "flex flex-col gap-4 mt-4");
        const dataHeader = UI.el("div", "inline-flex items-center gap-2 text-md-tertiary text-label-large mb-2");
        dataHeader.appendChild(UI.icon("storage", "text-lg", true));
        dataHeader.appendChild(UI.el("span", "", "数据管理"));
        dataSection.appendChild(dataHeader);

        const dataCard = UI.el("div", "bg-md-surface-container p-4 rounded-md-lg flex flex-col gap-3");

        const retentionWrap = UI.textField(
            "日志原始数据保留时间 (小时)",
            "默认 24",
            "number",
            preferences.log_raw_data_retention_hours != null ? String(preferences.log_raw_data_retention_hours) : "24",
            {
                helperText: "设为 0 表示不保存请求/响应原始数据，减少存储占用"
            }
        );
        retentionWrap.input.min = "0";
        retentionWrap.input.oninput = (e) => {
            preferences.log_raw_data_retention_hours = parseInt(e.target.value) || 0;
        };
        dataCard.appendChild(retentionWrap.wrapper);

        dataSection.appendChild(dataCard);
        form.appendChild(dataSection);

        // 保存按钮
        const actions = UI.el("div", "flex justify-end gap-2 mt-6 pt-4 border-t border-md-outline-variant");
        const saveBtn = UI.btn("保存配置", () => SettingsView._saveSettings(preferences), "filled", "save");
        actions.appendChild(saveBtn);
        form.appendChild(actions);

        card.appendChild(form);
        container.appendChild(card);
    },

    async _saveSettings(preferences) {
        const adminKey = AppConfig?.currentUser?.key || null;
        if (!adminKey) {
            UI.snackbar("需要管理员权限", null, null, { variant: "error" });
            return;
        }

        const headers = {
            "Content-Type": "application/json",
            Authorization: `Bearer ${adminKey}`
        };

        // 验证并清理配置
        const cleanPreferences = {};
        
        // 最大重试次数
        if (preferences.max_retry_count !== undefined) {
            let val = parseInt(preferences.max_retry_count);
            if (isNaN(val) || val < 1) val = 1;
            if (val > 100) val = 100;
            cleanPreferences.max_retry_count = val;
        }

        // 冷却时间
        if (preferences.cooldown_period !== undefined) {
            cleanPreferences.cooldown_period = Math.max(0, parseInt(preferences.cooldown_period) || 0);
        }

        // 速率限制
        if (preferences.rate_limit) {
            cleanPreferences.rate_limit = preferences.rate_limit.trim();
        }

        // 日志保留时间
        if (preferences.log_raw_data_retention_hours !== undefined) {
            cleanPreferences.log_raw_data_retention_hours = Math.max(0, parseInt(preferences.log_raw_data_retention_hours) || 0);
        }

        // 调度算法
        if (preferences.SCHEDULING_ALGORITHM) {
            cleanPreferences.SCHEDULING_ALGORITHM = preferences.SCHEDULING_ALGORITHM;
        }

        // 保留其他未在界面中编辑的字段
        Object.keys(preferences).forEach(key => {
            if (!["max_retry_count", "cooldown_period", "rate_limit", "log_raw_data_retention_hours", "SCHEDULING_ALGORITHM"].includes(key)) {
                cleanPreferences[key] = preferences[key];
            }
        });

        const bodyConfig = {
            ...SettingsView._apiConfig,
            preferences: cleanPreferences
        };

        try {
            const res = await fetch("/v1/api_config/update", {
                method: "POST",
                headers,
                body: JSON.stringify(bodyConfig),
            });

            if (!res.ok) {
                UI.snackbar(`保存失败: ${res.status}`, null, null, { variant: "error" });
                return;
            }

            SettingsView._apiConfig = bodyConfig;
            UI.snackbar("配置已保存", null, null, { variant: "success" });
        } catch (e) {
            UI.snackbar(`保存失败: ${e.message}`, null, null, { variant: "error" });
        }
    }
};