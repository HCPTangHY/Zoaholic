/**
 * Material Design 3 Application Controller - Enhanced Version
 * Handles app initialization, layout management, and responsive navigation
 */
const App = {
    currentView: null,
    isMobile: false,

    /**
     * Initialize the application
     */
    init() {
        // 首先检查认证状态
        if (!AppConfig.initAuth()) {
            // 未登录，显示登录页面
            App.showLoginPage();
            return;
        }

        // 已登录，初始化应用
        App._initializeApp();
    },

    /**
     * 初始化应用（登录后调用）
     */
    _initializeApp() {
        App.checkMobile();
        App.renderNavigationRail();
        App.renderBottomNavigation();
        App.setupMobileMenu();
        App.setupResponsiveListeners();
        App.setupUserMenu();
        
        // 初始化路由系统
        Views.initRouter();
        
        console.log("[App] Material Design 3 UI initialized (v2.0)");
    },

    /**
     * 显示登录页面
     */
    showLoginPage() {
        const appRoot = document.getElementById("app-root");
        const bottomNav = document.getElementById("bottom-nav");
        
        // 隐藏底部导航
        if (bottomNav) bottomNav.style.display = "none";
        
        // 清空并显示登录页面
        appRoot.innerHTML = "";
        appRoot.className = "flex items-center justify-center min-h-screen bg-md-surface";
        
        const loginContainer = document.createElement("div");
        loginContainer.className = "w-full max-w-md p-6 animate-fade-in";
        
        // Logo 和标题
        const header = document.createElement("div");
        header.className = "text-center mb-8";
        header.innerHTML = `
            <div class="w-20 h-20 mx-auto mb-4 rounded-md-xl overflow-hidden shadow-md-2">
                <img src="./icons/128.png" alt="Zoaholic Logo" class="w-full h-full object-cover"/>
            </div>
            <h1 class="text-headline-medium text-md-on-surface">Zoaholic Gateway</h1>
            <p class="text-body-medium text-md-on-surface-variant mt-2">请输入 API Key 登录管理后台</p>
        `;
        loginContainer.appendChild(header);
        
        // 登录卡片
        const card = document.createElement("div");
        card.className = "bg-md-surface-container rounded-md-xl p-6 shadow-md-2";
        
        // API Key 输入框
        const inputWrapper = document.createElement("div");
        inputWrapper.className = "mb-4";
        
        const inputLabel = document.createElement("label");
        inputLabel.className = "block text-label-large text-md-on-surface mb-2";
        inputLabel.textContent = "API Key";
        inputWrapper.appendChild(inputLabel);
        
        const inputContainer = document.createElement("div");
        inputContainer.className = "relative";
        
        const input = document.createElement("input");
        input.type = "password";
        input.id = "login-api-key";
        input.placeholder = "sk-xxxxxxxxxxxxxxxx";
        input.className = "w-full px-4 py-3 pr-12 rounded-md-md border border-md-outline bg-md-surface text-md-on-surface text-body-large focus:outline-none focus:border-md-primary focus:ring-2 focus:ring-md-primary/20 transition-all font-mono";
        input.autocomplete = "off";
        inputContainer.appendChild(input);
        
        // 显示/隐藏密码按钮
        const toggleBtn = document.createElement("button");
        toggleBtn.type = "button";
        toggleBtn.className = "absolute right-3 top-1/2 -translate-y-1/2 text-md-on-surface-variant hover:text-md-on-surface transition-colors";
        toggleBtn.innerHTML = '<span class="material-symbols-outlined">visibility</span>';
        toggleBtn.onclick = () => {
            if (input.type === "password") {
                input.type = "text";
                toggleBtn.innerHTML = '<span class="material-symbols-outlined">visibility_off</span>';
            } else {
                input.type = "password";
                toggleBtn.innerHTML = '<span class="material-symbols-outlined">visibility</span>';
            }
        };
        inputContainer.appendChild(toggleBtn);
        inputWrapper.appendChild(inputContainer);
        
        const helperText = document.createElement("p");
        helperText.className = "text-body-small text-md-on-surface-variant mt-2";
        helperText.textContent = "使用您在 api.yaml 中配置的 API Key 登录";
        inputWrapper.appendChild(helperText);
        
        card.appendChild(inputWrapper);
        
        // 错误提示
        const errorMsg = document.createElement("div");
        errorMsg.id = "login-error";
        errorMsg.className = "hidden mb-4 p-3 rounded-md-md bg-md-error-container text-md-on-error-container text-body-medium";
        card.appendChild(errorMsg);
        
        // 登录按钮
        const loginBtn = document.createElement("button");
        loginBtn.className = "w-full py-3 px-6 rounded-md-full bg-md-primary text-md-on-primary text-label-large font-medium hover:shadow-md-2 active:shadow-md-1 transition-all flex items-center justify-center gap-2";
        loginBtn.innerHTML = '<span class="material-symbols-outlined">login</span> 登录';
        
        const spinner = document.createElement("div");
        spinner.className = "hidden w-5 h-5 border-2 border-md-on-primary border-t-transparent rounded-full animate-spin";
        loginBtn.appendChild(spinner);
        
        loginBtn.onclick = async () => {
            const apiKey = input.value.trim();
            if (!apiKey) {
                App._showLoginError("请输入 API Key");
                return;
            }
            
            // 显示加载状态
            loginBtn.disabled = true;
            loginBtn.querySelector("span").classList.add("hidden");
            spinner.classList.remove("hidden");
            errorMsg.classList.add("hidden");
            
            // 验证 API Key
            const result = await AppConfig.validateApiKey(apiKey);
            
            if (result.valid) {
                // 登录成功
                AppConfig.login(apiKey, result.role);
                
                // 重新初始化应用
                appRoot.innerHTML = `
                    <div class="w-20 flex-shrink-0 bg-md-surface-container flex flex-col items-center py-3 gap-3 border-r border-md-outline-variant" id="nav-rail">
                        <div class="w-14 h-14 rounded-md-lg overflow-hidden flex items-center justify-center mb-2">
                            <img src="./icons/64.png" alt="Zoaholic Logo" class="w-full h-full object-cover"/>
                        </div>
                        <div class="flex-1 flex flex-col gap-2 w-full px-2" id="nav-items"></div>
                        <div class="w-14 h-14 rounded-full bg-md-secondary-container flex items-center justify-center cursor-pointer md-state-layer" id="user-avatar">
                            <span class="text-md-on-secondary-container font-bold text-sm">${result.role === "admin" ? "AD" : "US"}</span>
                        </div>
                    </div>
                    <div class="flex-1 flex flex-col min-w-0 bg-md-surface-container-low" id="main-content">
                        <header class="h-16 bg-md-surface flex items-center justify-between px-6 border-b border-md-outline-variant md-elevation-0" id="top-app-bar">
                            <div class="flex items-center gap-4">
                                <button class="w-10 h-10 rounded-full flex items-center justify-center md-state-layer text-md-on-surface-variant hover:bg-md-on-surface/8 md:hidden" id="mobile-menu-btn">
                                    <span class="material-symbols-outlined">menu</span>
                                </button>
                                <h1 class="text-title-large text-md-on-surface">控制台总览</h1>
                            </div>
                            <div class="flex items-center gap-3">
                                <div class="px-4 py-1.5 rounded-md-sm bg-md-secondary-container text-md-on-secondary-container text-label-large hidden sm:block">
                                    ${result.role === "admin" ? "管理员" : "用户"}
                                </div>
                                <button class="w-10 h-10 rounded-full flex items-center justify-center md-state-layer text-md-on-surface-variant hover:bg-md-on-surface/8" data-tooltip="通知">
                                    <span class="material-symbols-outlined">notifications</span>
                                </button>
                                <button class="w-10 h-10 rounded-full flex items-center justify-center md-state-layer text-md-on-surface-variant hover:bg-md-on-surface/8" data-tooltip="帮助">
                                    <span class="material-symbols-outlined">help</span>
                                </button>
                            </div>
                        </header>
                        <main class="flex-1 overflow-auto p-4 md:p-6" id="content-viewport"></main>
                    </div>
                `;
                appRoot.className = "flex h-full w-full";
                
                App._initializeApp();
            } else {
                // 登录失败
                App._showLoginError(result.message);
                loginBtn.disabled = false;
                loginBtn.querySelector("span").classList.remove("hidden");
                spinner.classList.add("hidden");
            }
        };
        
        // 回车键登录
        input.onkeydown = (e) => {
            if (e.key === "Enter") {
                loginBtn.click();
            }
        };
        
        card.appendChild(loginBtn);
        loginContainer.appendChild(card);
        
        // 底部提示
        const footer = document.createElement("div");
        footer.className = "text-center mt-6 text-body-small text-md-on-surface-variant";
        footer.innerHTML = `
            <p>首次使用？请先在服务器上配置 <code class="px-1 py-0.5 bg-md-surface-container rounded">api.yaml</code></p>
            <p class="mt-2">
                <a href="https://github.com/HCPTangHY/Zoaholic" target="_blank" class="text-md-primary hover:underline">
                    查看文档 →
                </a>
            </p>
        `;
        loginContainer.appendChild(footer);
        
        appRoot.appendChild(loginContainer);
        
        // 自动聚焦输入框
        setTimeout(() => input.focus(), 100);
    },

    /**
     * 显示登录错误
     */
    _showLoginError(message) {
        const errorMsg = document.getElementById("login-error");
        if (errorMsg) {
            errorMsg.textContent = message;
            errorMsg.classList.remove("hidden");
        }
    },

    /**
     * 设置用户菜单
     */
    setupUserMenu() {
        const userAvatar = document.getElementById("user-avatar");
        if (!userAvatar || typeof UI === "undefined") return;

        const menuItems = [
            {
                label: AppConfig.isAdmin() ? "管理员" : "普通用户",
                icon: "person",
                onClick: () => {}
            },
            {
                label: "退出登录",
                icon: "logout",
                onClick: () => {
                    if (confirm("确定要退出登录吗？")) {
                        AppConfig.logout();
                    }
                }
            }
        ];

        UI.menu(userAvatar, menuItems, { position: "top-start" });
    },

    /**
     * Check if current viewport is mobile
     */
    checkMobile() {
        App.isMobile = window.innerWidth < 768;
    },

    /**
     * Setup responsive listeners
     */
    setupResponsiveListeners() {
        let resizeTimeout;
        window.addEventListener("resize", () => {
            clearTimeout(resizeTimeout);
            resizeTimeout = setTimeout(() => {
                const wasMobile = App.isMobile;
                App.checkMobile();
                
                // Only update if breakpoint crossed
                if (wasMobile !== App.isMobile) {
                    App.updateNavigation();
                }
            }, 100);
        });
    },

    /**
     * Setup mobile menu button (top app bar)
     * 绑定移动端顶部左侧三杠按钮，弹出导航菜单
     */
    setupMobileMenu() {
        const menuBtn = document.getElementById("mobile-menu-btn");
        if (!menuBtn) return;

        if (!AppConfig || !Array.isArray(AppConfig.navItems)) return;

        const items = AppConfig.navItems.map((item) => ({
            label: item.label,
            icon: item.icon,
            onClick: () => {
                App.navigateTo(item.id, item.label);
            }
        }));

        // 使用 MD3 菜单组件；UI.menu 内部会在点击时自动打开/关闭
        UI.menu(menuBtn, items, { position: "bottom-start" });
    },

    /**
     * Update navigation based on viewport
     */
    updateNavigation() {
        const navRail = document.getElementById("nav-rail");
        const bottomNav = document.getElementById("bottom-nav");
        
        if (App.isMobile) {
            if (navRail) navRail.style.display = "none";
            if (bottomNav) bottomNav.style.display = "flex";
        } else {
            if (navRail) navRail.style.display = "flex";
            if (bottomNav) bottomNav.style.display = "none";
        }
    },

    /**
     * Render MD3 Navigation Rail (Desktop)
     */
    renderNavigationRail() {
        const navItems = document.getElementById("nav-items");
        if (!navItems) {
            console.error("[App] Navigation items container not found");
            return;
        }

        navItems.innerHTML = "";

        AppConfig.navItems.forEach((item) => {
            const navItem = document.createElement("button");
            navItem.className = "nav-rail-item w-full h-14 rounded-md-lg flex flex-col items-center justify-center gap-1 text-md-on-surface-variant hover:bg-md-on-surface/8 transition-all md-state-layer";
            navItem.dataset.id = item.id;
            navItem.setAttribute("data-tooltip", item.label);
            
            const icon = UI.icon(item.icon, "text-2xl");
            const label = document.createElement("span");
            label.className = "text-label-small";
            label.textContent = item.label.split(" ")[0];
            
            navItem.appendChild(icon);
            navItem.appendChild(label);
            
            navItem.onclick = () => {
                App.navigateTo(item.id, item.label);
            };
            
            navItems.appendChild(navItem);
        });

        App.setActiveNavItem("dashboard");
    },

    /**
     * Render MD3 Bottom Navigation (Mobile)
     */
    renderBottomNavigation() {
        const bottomNav = document.getElementById("bottom-nav");
        if (!bottomNav) {
            console.error("[App] Bottom navigation container not found");
            return;
        }

        bottomNav.innerHTML = "";

        const totalItems = AppConfig.navItems.length;
        
        // If 5 or fewer items, show all; otherwise show first 4 + "more" menu
        if (totalItems <= 5) {
            AppConfig.navItems.forEach((item) => {
                bottomNav.appendChild(App._createBottomNavItem(item));
            });
        } else {
            // Show first 4 items
            const visibleItems = AppConfig.navItems.slice(0, 4);
            const moreItems = AppConfig.navItems.slice(4);

            visibleItems.forEach((item) => {
                bottomNav.appendChild(App._createBottomNavItem(item));
            });

            // Add "More" button with menu
            const moreBtn = document.createElement("button");
            moreBtn.className = "bottom-nav-item flex-1 flex flex-col items-center justify-center gap-1 py-3 text-md-on-surface-variant hover:bg-md-on-surface/8 transition-all md-state-layer rounded-md-lg mx-1";
            moreBtn.dataset.id = "more";
            
            const iconContainer = document.createElement("div");
            iconContainer.className = "icon-container w-16 h-8 rounded-md-full flex items-center justify-center transition-all";
            iconContainer.appendChild(UI.icon("more_horiz", "text-2xl"));
            
            const label = document.createElement("span");
            label.className = "text-label-small mt-1";
            label.textContent = "更多";
            
            moreBtn.appendChild(iconContainer);
            moreBtn.appendChild(label);
            
            // Create menu items for the "more" button
            const menuItems = moreItems.map((item) => ({
                label: item.label,
                icon: item.icon,
                onClick: () => {
                    App.navigateTo(item.id, item.label);
                }
            }));
            
            // Setup menu
            UI.menu(moreBtn, menuItems, { position: "top-end" });
            
            bottomNav.appendChild(moreBtn);
        }

        App.setActiveBottomNavItem("dashboard");
    },

    /**
     * Create a bottom navigation item
     * @private
     */
    _createBottomNavItem(item) {
        const navItem = document.createElement("button");
        navItem.className = "bottom-nav-item flex-1 flex flex-col items-center justify-center gap-1 py-3 text-md-on-surface-variant hover:bg-md-on-surface/8 transition-all md-state-layer rounded-md-lg mx-1";
        navItem.dataset.id = item.id;
        
        // Icon container with pill background for active state
        const iconContainer = document.createElement("div");
        iconContainer.className = "icon-container w-16 h-8 rounded-md-full flex items-center justify-center transition-all";
        iconContainer.appendChild(UI.icon(item.icon, "text-2xl"));
        
        const label = document.createElement("span");
        label.className = "text-label-small mt-1";
        label.textContent = item.label.split(" ")[0];
        
        navItem.appendChild(iconContainer);
        navItem.appendChild(label);
        
        navItem.onclick = () => {
            App.navigateTo(item.id, item.label);
        };
        
        return navItem;
    },

    /**
     * Navigate to a view (使用 URL 路由)
     */
    navigateTo(viewId, label) {
        // 使用 Views 的路由系统进行导航
        Views.navigateTo(viewId);
    },

    /**
     * Set active navigation item (Desktop Rail)
     */
    setActiveNavItem(itemId) {
        document.querySelectorAll(".nav-rail-item").forEach((el) => {
            if (el.dataset.id === itemId) {
                el.classList.add("active", "bg-md-primary-container", "text-md-on-primary-container");
                el.classList.remove("text-md-on-surface-variant");
                const icon = el.querySelector(".material-symbols-outlined");
                if (icon) {
                    icon.style.fontVariationSettings = "'FILL' 1, 'wght' 400, 'GRAD' 0, 'opsz' 24";
                }
            } else {
                el.classList.remove("active", "bg-md-primary-container", "text-md-on-primary-container");
                el.classList.add("text-md-on-surface-variant");
                const icon = el.querySelector(".material-symbols-outlined");
                if (icon) {
                    icon.style.fontVariationSettings = "'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24";
                }
            }
        });
    },

    /**
     * Set active bottom navigation item (Mobile)
     */
    setActiveBottomNavItem(itemId) {
        // Check if the item is in the "more" menu (items after index 3 when total > 5)
        const totalItems = AppConfig.navItems.length;
        const moreItemIds = totalItems > 5 ? AppConfig.navItems.slice(4).map(item => item.id) : [];
        const isInMoreMenu = moreItemIds.includes(itemId);
        
        document.querySelectorAll(".bottom-nav-item").forEach((el) => {
            const iconContainer = el.querySelector(".icon-container");
            const elId = el.dataset.id;
            
            // Highlight "more" button if the active item is in the more menu
            const shouldHighlight = (elId === itemId) || (elId === "more" && isInMoreMenu);
            
            if (shouldHighlight) {
                el.classList.add("text-md-on-primary-container");
                el.classList.remove("text-md-on-surface-variant");
                if (iconContainer) {
                    iconContainer.classList.add("bg-md-primary-container");
                }
                const icon = el.querySelector(".material-symbols-outlined");
                if (icon) {
                    icon.style.fontVariationSettings = "'FILL' 1, 'wght' 400, 'GRAD' 0, 'opsz' 24";
                }
            } else {
                el.classList.remove("text-md-on-primary-container");
                el.classList.add("text-md-on-surface-variant");
                if (iconContainer) {
                    iconContainer.classList.remove("bg-md-primary-container");
                }
                const icon = el.querySelector(".material-symbols-outlined");
                if (icon) {
                    icon.style.fontVariationSettings = "'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24";
                }
            }
        });
    },

    /**
     * Update Top App Bar title and actions
     */
    updateTopAppBar(title) {
        const topAppBar = document.getElementById("top-app-bar");
        if (!topAppBar) return;

        const titleEl = topAppBar.querySelector("h1");
        if (titleEl) {
            titleEl.textContent = title;
        }

        const balanceChip = topAppBar.querySelector(".bg-md-secondary-container");
        if (balanceChip && AppConfig.currentUser) {
            balanceChip.textContent = AppConfig.isAdmin() ? "管理员" : "用户";
        }
    },

    /**
     * Show a success message
     */
    showSuccess(message) {
        UI.snackbar(message, "关闭", null, { variant: "success" });
    },

    /**
     * Show an error message
     */
    showError(message) {
        UI.snackbar(message, "重试", null, { variant: "error" });
    },

    /**
     * Show a loading state in content viewport
     */
    showLoading() {
        const viewport = document.getElementById("content-viewport");
        if (!viewport) return;
        
        viewport.innerHTML = "";
        viewport.appendChild(UI.spinner());
    },

    /**
     * Clear content viewport
     */
    clearContent() {
        const viewport = document.getElementById("content-viewport");
        if (viewport) {
            viewport.innerHTML = "";
        }
    }
};

/**
 * Initialize app when DOM is ready
 */
document.addEventListener("DOMContentLoaded", () => {
    App.init();
});