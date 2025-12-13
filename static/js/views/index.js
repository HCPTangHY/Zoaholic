/**
 * Material Design 3 Views - Main Entry
 * 视图主入口 - 整合所有视图并提供路由支持
 */
const Views = {
    currentView: null,
    
    // 视图注册表
    _views: {
        dashboard: DashboardView,
        chat: ChatView,
        tools: ToolsView,
        logs: LogsView,
        admin: AdminView,
        config: ConfigView,
        settings: SettingsView,
        plugins: PluginsView,
    },

    // 视图标题映射
    _titles: {
        dashboard: "控制台总览",
        chat: "对话 Playground",
        tools: "工具箱 (Tools)",
        logs: "请求日志",
        admin: "API 密钥管理",
        config: "渠道配置",
        settings: "系统设置",
        plugins: "插件管理",
    },

    /**
     * 初始化路由系统
     */
    initRouter() {
        // 监听 hash 变化
        window.addEventListener("hashchange", () => {
            Views._handleRouteChange();
        });

        // 页面加载时处理初始路由
        Views._handleRouteChange();
    },

    /**
     * 处理路由变化
     * @private
     */
    _handleRouteChange() {
        const hash = window.location.hash;
        let viewName = "dashboard"; // 默认视图

        if (hash && hash.startsWith("#/")) {
            const route = hash.slice(2); // 移除 "#/"
            if (Views._views[route]) {
                viewName = route;
            }
        } else if (hash && hash.startsWith("#")) {
            const route = hash.slice(1); // 移除 "#"
            if (Views._views[route]) {
                viewName = route;
            }
        }

        // 如果视图没有变化，不重新渲染
        if (Views.currentView === viewName) {
            return;
        }

        // 更新导航状态
        if (typeof App !== "undefined") {
            App.setActiveNavItem(viewName);
            App.setActiveBottomNavItem(viewName);
            App.updateTopAppBar(Views._titles[viewName] || viewName);
        }

        // 渲染视图
        Views.render(viewName, false); // false 表示不更新 URL
    },

    /**
     * 导航到指定视图
     * @param {string} viewName - 视图名称
     */
    navigateTo(viewName) {
        if (!Views._views[viewName]) {
            console.error(`[Views] Unknown view: ${viewName}`);
            return;
        }

        // 更新 URL hash
        window.location.hash = `#/${viewName}`;
    },

    /**
     * 渲染指定视图
     * @param {string} viewName - 视图名称
     * @param {boolean} updateUrl - 是否更新 URL（默认 true）
     */
    render(viewName, updateUrl = true) {
        const viewport = document.getElementById("content-viewport");
        if (!viewport) {
            console.error("[Views] Content viewport not found");
            return;
        }

        // 清理之前视图的特殊状态
        if (Views.currentView === "chat" && ChatView._cleanup) {
            ChatView._cleanup();
        }
        
        // 移除可能存在的 chat-view-active 类
        viewport.classList.remove("chat-view-active");

        // 清空视口
        viewport.innerHTML = "";
        
        // 创建容器 - 使用 min-h-full 而不是 h-full，允许内容超出时滚动
        // 移动端需要额外的底部内边距来避免被底部导航栏遮挡
        const container = UI.el("div", "animate-fade-in w-full min-h-full flex flex-col gap-6 pb-4 md:pb-0");
        viewport.appendChild(container);

        Views.currentView = viewName;

        // 更新 URL（如果需要）
        if (updateUrl && window.location.hash !== `#/${viewName}`) {
            // 使用 replaceState 避免产生额外的历史记录
            history.replaceState(null, "", `#/${viewName}`);
        }

        // 获取视图对象
        const viewObj = Views._views[viewName];
        
        if (viewObj && typeof viewObj.render === "function") {
            viewObj.render(container);
        } else if (typeof Views[viewName] === "function") {
            // 兼容旧的函数式视图
            Views[viewName](container);
        } else {
            container.appendChild(
                UI.el("div", "text-md-error text-center p-8", `视图未找到: ${viewName}`)
            );
        }
    },

    /**
     * 注册新视图
     * @param {string} name - 视图名称
     * @param {Object} viewObj - 视图对象（需要有 render 方法）
     * @param {string} title - 视图标题
     */
    register(name, viewObj, title) {
        Views._views[name] = viewObj;
        if (title) {
            Views._titles[name] = title;
        }
    },

    /**
     * 获取当前视图名称
     * @returns {string}
     */
    getCurrentView() {
        return Views.currentView;
    },

    /**
     * 获取视图标题
     * @param {string} viewName - 视图名称
     * @returns {string}
     */
    getTitle(viewName) {
        return Views._titles[viewName] || viewName;
    },

    /**
     * 检查视图是否存在
     * @param {string} viewName - 视图名称
     * @returns {boolean}
     */
    exists(viewName) {
        return !!Views._views[viewName];
    },

    // 为了向后兼容，保留 _apiConfig 引用
    get _apiConfig() {
        return ConfigView._apiConfig;
    },
    set _apiConfig(value) {
        ConfigView._apiConfig = value;
    },

    // 向后兼容：openConfigSideSheet

    openConfigSideSheet(existingData) {
        return ConfigView.openConfigSideSheet(existingData);
    },
};