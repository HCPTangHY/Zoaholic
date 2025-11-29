/**
 * Application Configuration
 * Material Design 3 Enhanced Version
 */
const AppConfig = {
    appName: "Zoaholic Gateway",
    version: "v2.0.0",
    
    // 当前用户信息 - 从 localStorage 加载或为空
    currentUser: null,
    
    // 导航顺序：总览 - 日志 - 密钥 - 渠道 - 对话 - 工具箱
    navItems: [
        { id: "dashboard", label: "控制台总览", icon: "dashboard" },
        { id: "logs", label: "请求日志", icon: "receipt_long" },
        { id: "admin", label: "API 密钥管理", icon: "vpn_key" },
        { id: "config", label: "渠道配置", icon: "settings_applications" },
        { id: "chat", label: "对话", icon: "chat" },
        { id: "tools", label: "工具箱", icon: "extension" }
    ],

    /**
     * 初始化用户认证状态
     */
    initAuth() {
        const savedKey = localStorage.getItem("zoaholic_api_key");
        if (savedKey) {
            AppConfig.currentUser = {
                key: savedKey,
                role: localStorage.getItem("zoaholic_user_role") || "user",
                balance: 0
            };
            return true;
        }
        return false;
    },

    /**
     * 验证 API Key
     * @param {string} apiKey - 要验证的 API Key
     * @returns {Promise<{valid: boolean, role: string, message: string}>}
     */
    async validateApiKey(apiKey) {
        if (!apiKey || !apiKey.trim()) {
            return { valid: false, role: "", message: "API Key 不能为空" };
        }

        try {
            // 尝试调用需要认证的 API 来验证 key
            const response = await fetch("/v1/models", {
                headers: {
                    "Authorization": `Bearer ${apiKey}`
                }
            });

            if (response.status === 403) {
                return { valid: false, role: "", message: "API Key 无效或已过期" };
            }

            if (response.status === 429) {
                return { valid: false, role: "", message: "请求过于频繁，请稍后再试" };
            }

            if (!response.ok) {
                return { valid: false, role: "", message: `验证失败: HTTP ${response.status}` };
            }

            // 验证成功，尝试获取角色信息
            let role = "user";
            try {
                const configRes = await fetch("/v1/api_config", {
                    headers: { "Authorization": `Bearer ${apiKey}` }
                });
                if (configRes.ok) {
                    role = "admin";
                }
            } catch (e) {
                // 忽略错误，默认为普通用户
            }

            return { valid: true, role, message: "验证成功" };
        } catch (e) {
            return { valid: false, role: "", message: `网络错误: ${e.message}` };
        }
    },

    /**
     * 登录
     * @param {string} apiKey - API Key
     * @param {string} role - 用户角色
     */
    login(apiKey, role) {
        localStorage.setItem("zoaholic_api_key", apiKey);
        localStorage.setItem("zoaholic_user_role", role);
        AppConfig.currentUser = {
            key: apiKey,
            role: role,
            balance: 0
        };
    },

    /**
     * 登出
     */
    logout() {
        localStorage.removeItem("zoaholic_api_key");
        localStorage.removeItem("zoaholic_user_role");
        AppConfig.currentUser = null;
        // 刷新页面以显示登录界面
        window.location.reload();
    },

    /**
     * 检查是否已登录
     */
    isLoggedIn() {
        return AppConfig.currentUser !== null && AppConfig.currentUser.key;
    },

    /**
     * 检查是否是管理员
     */
    isAdmin() {
        return AppConfig.currentUser && 
               (AppConfig.currentUser.role === "admin" || 
                (AppConfig.currentUser.role && AppConfig.currentUser.role.includes("admin")));
    }
};

// Mock Data to simulate backend responses
const MockData = {
    stats: {
        total_requests: 14502,
        total_tokens: 8504921,
        success_rate: 98.4,
        active_channels: 12
    },
    models: [
        "gpt-4-turbo",
        "gpt-3.5-turbo",
        "claude-3-opus",
        "gemini-pro",
        "dall-e-3"
    ],
    channels: [
        { name: "OpenAI Official", status: "healthy", latency: "240ms", success: "99.9%" },
        { name: "Azure OpenAI", status: "healthy", latency: "180ms", success: "99.5%" },
        { name: "Anthropic Direct", status: "warning", latency: "850ms", success: "92.1%" },
        { name: "Google Vertex", status: "healthy", latency: "300ms", success: "98.8%" }
    ],
    // Detailed Provider Configuration Data
    providers: [
        {
            id: 1,
            name: "openai",
            base_url: "https://api.openai.com/v1",
            api_keys: ["sk-proj-123...", "sk-proj-456..."],
            models: [
                { name: "gpt-4", price_ratio: 1.0, enabled: true },
                { name: "gpt-3.5-turbo", price_ratio: 0.5, enabled: true }
            ],
            timeout: 60,
            weight: 10
        },
        {
            id: 2,
            name: "anthropic",
            base_url: "https://api.anthropic.com/v1",
            api_keys: ["sk-ant-987..."],
            models: [
                { name: "claude-3-opus-20240229", price_ratio: 1.2, enabled: true },
                { name: "claude-3-sonnet-20240229", price_ratio: 0.8, enabled: true }
            ],
            timeout: 120,
            weight: 5
        }
    ],
    apiKeys: [
        { key: "sk-proj-...", owner: "Admin", credits: 1000, cost: 12.5, status: "Active" },
        { key: "sk-user-...", owner: "TestUser", credits: 50, cost: 45.2, status: "Active" },
        { key: "sk-temp-...", owner: "Guest", credits: 5, cost: 5.0, status: "Depleted" }
    ],
    chatHistory: [
        { role: "system", content: "You are a helpful assistant powered by Zoaholic Gateway." },
        { role: "user", content: "Hello, system is running?" },
        { role: "assistant", content: "Yes! The Zoaholic Gateway is fully operational. How can I help you today?" }
    ]
};