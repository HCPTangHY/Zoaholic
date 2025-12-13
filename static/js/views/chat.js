/**
 * Chat View - Playground
 * 聊天视图 - 对话测试（真实 API 调用）
 */
const ChatView = {
    // 内部状态
    _models: [],
    _selectedModel: "",
    _temperature: 0.7,
    _stream: true,
    _messages: [],
    _apiKey: "",
    _isLoading: false,
    _msgList: null,  // 消息列表 DOM 引用
    _renderMessage: null,  // renderMessage 函数引用

    /**
     * 初始化：获取第一个 admin key 和模型列表
     */
    async _init() {
        // 尝试从 AppConfig 获取当前登录的 admin key
        if (AppConfig.currentUser && AppConfig.currentUser.key) {
            ChatView._apiKey = AppConfig.currentUser.key;
        } else {
            // 如果没有登录，尝试从后端获取第一个 admin key
            try {
                const adminKey = await ChatView._fetchFirstAdminKey();
                if (adminKey) {
                    ChatView._apiKey = adminKey;
                }
            } catch (e) {
                console.error("[ChatView] Failed to get admin key:", e);
            }
        }

        // 获取模型列表
        if (ChatView._apiKey) {
            try {
                await ChatView._fetchModels();
            } catch (e) {
                console.error("[ChatView] Failed to fetch models:", e);
            }
        }
    },

    /**
     * 获取第一个 admin key
     */
    async _fetchFirstAdminKey() {
        // 如果已经有 admin key，直接返回
        if (AppConfig.currentUser && AppConfig.currentUser.key && AppConfig.isAdmin()) {
            return AppConfig.currentUser.key;
        }

        // 尝试从 /v1/api_config 获取配置（需要有 admin key 才能访问）
        // 这里我们假设用户已经登录并且是 admin
        return null;
    },

    /**
     * 获取可用模型列表
     */
    async _fetchModels() {
        if (!ChatView._apiKey) {
            console.warn("[ChatView] No API key available for fetching models");
            return;
        }

        try {
            const res = await fetch("/v1/models", {
                headers: {
                    "Authorization": `Bearer ${ChatView._apiKey}`
                }
            });

            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }

            const data = await res.json();
            if (data && Array.isArray(data.data)) {
                ChatView._models = data.data.map(m => m.id || m.name || m).filter(Boolean);
                if (ChatView._models.length > 0 && !ChatView._selectedModel) {
                    ChatView._selectedModel = ChatView._models[0];
                }
            }
        } catch (e) {
            console.error("[ChatView] Failed to fetch models:", e);
            UI.snackbar(`获取模型列表失败: ${e.message}`, null, null, { variant: "error" });
        }
    },

    /**
     * 发送聊天消息
     */
    async _sendMessage(content, msgList, renderMessage) {
        if (ChatView._isLoading) return;
        if (!content.trim()) return;

        if (!ChatView._apiKey) {
            UI.snackbar("请先登录或配置 API Key", null, null, { variant: "error" });
            return;
        }

        if (!ChatView._selectedModel) {
            UI.snackbar("请先选择模型", null, null, { variant: "error" });
            return;
        }

        // 添加用户消息
        ChatView._messages.push({ role: "user", content: content });
        const userMsgIndex = ChatView._messages.length - 1;
        renderMessage("user", content, userMsgIndex);

        ChatView._isLoading = true;

        // 显示加载指示器
        const typingWrapper = UI.el("div", "flex w-full justify-start");
        const typingBubble = UI.el("div", "rounded-md-lg px-5 py-3 bg-md-surface-container border border-md-outline-variant");
        typingBubble.appendChild(UI.progressLinear(null, "w-20"));
        typingWrapper.appendChild(typingBubble);
        msgList.appendChild(typingWrapper);
        msgList.scrollTop = msgList.scrollHeight;

        try {
            const requestBody = {
                model: ChatView._selectedModel,
                messages: ChatView._messages,
                temperature: ChatView._temperature,
                stream: ChatView._stream
            };

            const response = await fetch("/v1/chat/completions", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${ChatView._apiKey}`
                },
                body: JSON.stringify(requestBody)
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.error?.message || errorData.detail || `HTTP ${response.status}`);
            }

            // 移除加载指示器
            typingWrapper.remove();

            if (ChatView._stream) {
                // 流式响应处理
                await ChatView._handleStreamResponse(response, msgList, renderMessage);
            } else {
                // 非流式响应处理
                const data = await response.json();
                const assistantContent = data.choices?.[0]?.message?.content || "";
                ChatView._messages.push({ role: "assistant", content: assistantContent });
                const assistantMsgIndex = ChatView._messages.length - 1;
                renderMessage("assistant", assistantContent, assistantMsgIndex);
            }
        } catch (e) {
            console.error("[ChatView] Chat error:", e);
            typingWrapper.remove();
            UI.snackbar(`发送失败: ${e.message}`, null, null, { variant: "error" });
        } finally {
            ChatView._isLoading = false;
        }
    },

    /**
     * 处理流式响应
     */
    async _handleStreamResponse(response, msgList, renderMessage) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let assistantContent = "";
        let reasoningContent = "";

        // 创建助手消息卡片（流式模式）
        const wrapper = UI.el("div", "message-wrapper");
        const roleLabel = UI.el("div", "role-label", "Model");
        wrapper.appendChild(roleLabel);
        
        const card = UI.el("div", "message-card");
        
        // 创建思维链容器（初始隐藏）
        const thinkingSection = UI.el("div", "thinking-section hidden");
        const thinkingHeader = UI.el("div", "thinking-header");
        const thinkingToggle = UI.el("button", "thinking-toggle");
        thinkingToggle.appendChild(UI.icon("psychology"));
        thinkingToggle.appendChild(UI.el("span", "", "思维过程"));
        const thinkingIcon = UI.icon("expand_more", "thinking-icon");
        thinkingToggle.appendChild(thinkingIcon);
        thinkingHeader.appendChild(thinkingToggle);
        
        const thinkingContent = UI.el("div", "thinking-content collapsed");
        thinkingContent.innerHTML = '<span class="typing-cursor"></span>';
        
        thinkingToggle.onclick = () => {
            const isCollapsed = thinkingContent.classList.contains("collapsed");
            if (isCollapsed) {
                thinkingContent.classList.remove("collapsed");
                thinkingIcon.textContent = "expand_less";
            } else {
                thinkingContent.classList.add("collapsed");
                thinkingIcon.textContent = "expand_more";
            }
        };
        
        thinkingSection.appendChild(thinkingHeader);
        thinkingSection.appendChild(thinkingContent);
        card.appendChild(thinkingSection);
        
        // 创建回复内容区域
        const content = UI.el("div", "message-content");
        content.innerHTML = '<span class="typing-cursor"></span>';
        card.appendChild(content);
        
        wrapper.appendChild(card);
        msgList.appendChild(wrapper);
        msgList.scrollTop = msgList.scrollHeight;

        try {
            let buffer = "";
            
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed || !trimmed.startsWith("data:")) continue;

                    const dataStr = trimmed.slice(5).trim();
                    if (dataStr === "[DONE]") continue;

                    try {
                        const data = JSON.parse(dataStr);
                        const delta = data.choices?.[0]?.delta;
                        
                        // 处理思维链内容
                        if (delta?.reasoning_content) {
                            reasoningContent += delta.reasoning_content;
                            thinkingSection.classList.remove("hidden");
                            thinkingContent.innerHTML = ChatView._formatContent(reasoningContent) + '<span class="typing-cursor"></span>';
                            msgList.scrollTop = msgList.scrollHeight;
                        }
                        
                        // 处理最终回复内容
                        if (delta?.content) {
                            assistantContent += delta.content;
                            content.innerHTML = ChatView._formatContent(assistantContent) + '<span class="typing-cursor"></span>';
                            msgList.scrollTop = msgList.scrollHeight;
                        }
                    } catch (e) {
                        // 忽略解析错误
                    }
                }
            }

            // 移除光标
            if (reasoningContent) {
                thinkingContent.innerHTML = ChatView._formatContent(reasoningContent);
            }
            content.innerHTML = ChatView._formatContent(assistantContent);
            
            // 添加操作栏
            const actionBar = UI.el("div", "action-bar");
            const messageIndex = ChatView._messages.length;
            card.dataset.messageIndex = messageIndex;
            
            const editBtn = UI.el("button", "icon-btn");
            editBtn.title = "编辑消息";
            editBtn.appendChild(UI.icon("edit"));
            editBtn.onclick = () => ChatView._editMessage(card, content, messageIndex, assistantContent);
            actionBar.appendChild(editBtn);
            
            const retryBtn = UI.el("button", "icon-btn");
            retryBtn.title = "重新生成";
            retryBtn.appendChild(UI.icon("refresh"));
            retryBtn.onclick = () => ChatView._retryMessage(messageIndex);
            actionBar.appendChild(retryBtn);
            
            const moreBtn = UI.el("button", "icon-btn");
            moreBtn.title = "更多选项";
            moreBtn.appendChild(UI.icon("more_vert"));
            moreBtn.onclick = (e) => ChatView._showMoreMenu(e, card, messageIndex);
            actionBar.appendChild(moreBtn);
            
            card.appendChild(actionBar);
            
            // 保存消息（包含思维链内容）
            const message = {
                role: "assistant",
                content: assistantContent
            };
            if (reasoningContent) {
                message.reasoning_content = reasoningContent;
            }
            ChatView._messages.push(message);
        } catch (e) {
            console.error("[ChatView] Stream error:", e);
            content.innerHTML = ChatView._formatContent(assistantContent) || `<span class="text-md-error">流式响应中断</span>`;
        }
    },

    /**
     * 复制消息内容
     */
    _copyMessage(text) {
        if (!text) {
            UI.snackbar("没有可复制的内容", null, null, { variant: "error" });
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                () => UI.snackbar("已复制到剪贴板", null, null, { variant: "success" }),
                () => UI.snackbar("复制失败", null, null, { variant: "error" })
            );
        } else {
            UI.snackbar("当前浏览器不支持剪贴板 API", null, null, { variant: "error" });
        }
    },

    /**
     * 进入编辑模式 - 内联编辑
     */
    _editMessage(card, content, messageIndex, originalText) {
        // 标记卡片为编辑状态
        card.classList.add("editing");
        
        // 保存原始内容DOM
        const originalContent = content.innerHTML;
        const isUser = ChatView._messages[messageIndex]?.role === "user";
        
        // 创建textarea
        const textarea = document.createElement("textarea");
        textarea.className = "message-edit-textarea";
        textarea.value = originalText;
        
        // 替换内容为textarea
        content.innerHTML = "";
        content.appendChild(textarea);
        
        // 自适应高度函数
        const autoResize = () => {
            textarea.style.height = "auto";
            textarea.style.height = `${Math.max(100, textarea.scrollHeight)}px`;
        };
        
        // 初始自适应高度
        autoResize();
        
        // 输入时自动调整高度
        textarea.addEventListener("input", autoResize);
        
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        
        // 修改操作栏按钮
        const actionBar = card.querySelector(".action-bar");
        const oldButtons = actionBar.innerHTML;
        actionBar.innerHTML = "";
        
        // 确认按钮（双对勾）
        const confirmBtn = UI.el("button", "icon-btn");
        confirmBtn.title = "确认编辑";
        confirmBtn.appendChild(UI.icon("done_all"));
        confirmBtn.onclick = async () => {
            const newText = textarea.value.trim();
            if (!newText) {
                UI.snackbar("消息不能为空", null, null, { variant: "error" });
                return;
            }
            
            if (newText === originalText) {
                // 没有修改，退出编辑模式
                ChatView._cancelEdit(card, content, actionBar, originalContent, oldButtons);
                return;
            }
            
            // 检查消息是否存在
            if (messageIndex == null || !ChatView._messages[messageIndex]) {
                UI.snackbar("消息不存在", null, null, { variant: "error" });
                return;
            }
            
            // 更新消息内容
            ChatView._messages[messageIndex].content = newText;
            
            // 删除该消息之后的所有消息
            ChatView._messages = ChatView._messages.slice(0, messageIndex + 1);
            
            // 删除该消息之后的 DOM
            if (ChatView._msgList) {
                const wrappers = ChatView._msgList.querySelectorAll(".message-wrapper");
                for (let i = wrappers.length - 1; i >= 0; i--) {
                    const wrapper = wrappers[i];
                    const wCard = wrapper.querySelector(".message-card");
                    const idx = parseInt(wCard?.dataset.messageIndex || "-1");
                    if (idx > messageIndex) {
                        wrapper.remove();
                    }
                }
            }
            
            // 退出编辑模式并更新显示
            card.classList.remove("editing");
            content.innerHTML = ChatView._formatContent(newText);
            actionBar.innerHTML = oldButtons;
            ChatView._rebindActionBar(actionBar, card, content);
            
            // 如果是用户消息，自动重新生成助手响应
            if (isUser) {
                UI.snackbar("消息已更新，正在重新生成...", null, null, { variant: "info" });
                await ChatView._resendRequest();
            } else {
                UI.snackbar("消息已更新", null, null, { variant: "success" });
            }
        };
        actionBar.appendChild(confirmBtn);
        
        // 取消按钮
        const cancelBtn = UI.el("button", "icon-btn");
        cancelBtn.title = "取消编辑";
        cancelBtn.appendChild(UI.icon("close"));
        cancelBtn.onclick = () => {
            ChatView._cancelEdit(card, content, actionBar, originalContent, oldButtons);
        };
        actionBar.appendChild(cancelBtn);
        
        // 更多按钮保留
        const moreBtn = UI.el("button", "icon-btn");
        moreBtn.title = "更多选项";
        moreBtn.appendChild(UI.icon("more_vert"));
        moreBtn.onclick = (e) => {
            ChatView._showMoreMenu(e, card, messageIndex);
        };
        actionBar.appendChild(moreBtn);
        
        // ESC键取消编辑
        textarea.onkeydown = (e) => {
            if (e.key === "Escape") {
                ChatView._cancelEdit(card, content, actionBar, originalContent, oldButtons);
            }
        };
    },

    /**
     * 取消编辑模式
     */
    _cancelEdit(card, content, actionBar, originalContent, oldButtons) {
        card.classList.remove("editing");
        content.innerHTML = originalContent;
        actionBar.innerHTML = oldButtons;
        
        // 重新绑定事件
        ChatView._rebindActionBar(actionBar, card, content);
    },

    /**
     * 重新绑定操作栏事件
     */
    _rebindActionBar(actionBar, card, content) {
        const buttons = actionBar.querySelectorAll(".icon-btn");
        buttons.forEach(btn => {
            const iconText = btn.querySelector(".material-symbols-outlined")?.textContent;
            if (iconText === "edit") {
                btn.onclick = () => {
                    const messageIndex = parseInt(card.dataset.messageIndex);
                    const originalText = ChatView._messages[messageIndex].content;
                    ChatView._editMessage(card, content, messageIndex, originalText);
                };
            } else if (iconText === "refresh") {
                btn.onclick = () => {
                    const messageIndex = parseInt(card.dataset.messageIndex);
                    ChatView._retryMessage(messageIndex);
                };
            } else if (iconText === "more_vert") {
                btn.onclick = (e) => {
                    const messageIndex = parseInt(card.dataset.messageIndex);
                    ChatView._showMoreMenu(e, card, messageIndex);
                };
            }
        });
    },

    /**
     * 直接重试消息（不刷新页面）
     */
    async _retryMessage(messageIndex) {
        if (ChatView._isLoading) {
            UI.snackbar("请等待当前请求完成", null, null, { variant: "warning" });
            return;
        }
        
        const msg = ChatView._messages[messageIndex];
        if (!msg) {
            UI.snackbar("消息不存在", null, null, { variant: "error" });
            return;
        }
        
        const isUser = msg.role === "user";
        const msgContent = msg.content;
        
        // 删除该消息及之后的所有消息
        ChatView._messages = ChatView._messages.slice(0, messageIndex);
        
        // 删除 DOM
        if (ChatView._msgList) {
            const wrappers = ChatView._msgList.querySelectorAll(".message-wrapper");
            for (let i = wrappers.length - 1; i >= 0; i--) {
                const wrapper = wrappers[i];
                const card = wrapper.querySelector(".message-card");
                const idx = parseInt(card?.dataset.messageIndex || "-1");
                if (idx >= messageIndex) {
                    wrapper.remove();
                }
            }
        }
        
        if (isUser) {
            // 用户消息重试：重新发送该用户消息
            UI.snackbar("正在重新发送...", null, null, { variant: "info" });
            await ChatView._resendUserMessage(msgContent);
        } else {
            // 助手消息重试：基于前面的对话重新请求
            if (ChatView._messages.length > 0) {
                const lastMsg = ChatView._messages[ChatView._messages.length - 1];
                if (lastMsg.role === "user") {
                    UI.snackbar("正在重新生成...", null, null, { variant: "info" });
                    await ChatView._resendRequest();
                } else {
                    UI.snackbar("消息已删除", null, null, { variant: "info" });
                }
            } else {
                UI.snackbar("消息已删除", null, null, { variant: "info" });
            }
        }
    },

    /**
     * 重新发送用户消息（包含添加用户消息到 DOM）
     */
    async _resendUserMessage(content) {
        if (!ChatView._msgList || !ChatView._renderMessage) {
            console.error("[ChatView] Missing msgList or renderMessage reference");
            return;
        }
        
        if (!ChatView._apiKey) {
            UI.snackbar("请先登录或配置 API Key", null, null, { variant: "error" });
            return;
        }

        if (!ChatView._selectedModel) {
            UI.snackbar("请先选择模型", null, null, { variant: "error" });
            return;
        }
        
        // 添加用户消息
        ChatView._messages.push({ role: "user", content: content });
        const userMsgIndex = ChatView._messages.length - 1;
        ChatView._renderMessage("user", content, userMsgIndex);
        
        ChatView._isLoading = true;
        
        // 显示加载指示器
        const typingWrapper = UI.el("div", "flex w-full justify-start");
        const typingBubble = UI.el("div", "rounded-md-lg px-5 py-3 bg-md-surface-container border border-md-outline-variant");
        typingBubble.appendChild(UI.progressLinear(null, "w-20"));
        typingWrapper.appendChild(typingBubble);
        ChatView._msgList.appendChild(typingWrapper);
        ChatView._msgList.scrollTop = ChatView._msgList.scrollHeight;
        
        try {
            const requestBody = {
                model: ChatView._selectedModel,
                messages: ChatView._messages,
                temperature: ChatView._temperature,
                stream: ChatView._stream
            };
            
            const response = await fetch("/v1/chat/completions", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${ChatView._apiKey}`
                },
                body: JSON.stringify(requestBody)
            });
            
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.error?.message || errorData.detail || `HTTP ${response.status}`);
            }
            
            typingWrapper.remove();
            
            if (ChatView._stream) {
                await ChatView._handleStreamResponse(response, ChatView._msgList, ChatView._renderMessage);
            } else {
                const data = await response.json();
                const assistantContent = data.choices?.[0]?.message?.content || "";
                ChatView._messages.push({ role: "assistant", content: assistantContent });
                const assistantMsgIndex = ChatView._messages.length - 1;
                ChatView._renderMessage("assistant", assistantContent, assistantMsgIndex);
            }
        } catch (e) {
            console.error("[ChatView] Resend user message error:", e);
            typingWrapper.remove();
            UI.snackbar(`重试失败: ${e.message}`, null, null, { variant: "error" });
        } finally {
            ChatView._isLoading = false;
        }
    },

    /**
     * 重新发送请求（不添加用户消息，仅请求助手响应）
     */
    async _resendRequest() {
        if (!ChatView._msgList || !ChatView._renderMessage) {
            console.error("[ChatView] Missing msgList or renderMessage reference");
            return;
        }
        
        if (!ChatView._apiKey) {
            UI.snackbar("请先登录或配置 API Key", null, null, { variant: "error" });
            return;
        }

        if (!ChatView._selectedModel) {
            UI.snackbar("请先选择模型", null, null, { variant: "error" });
            return;
        }
        
        ChatView._isLoading = true;
        
        // 显示加载指示器
        const typingWrapper = UI.el("div", "flex w-full justify-start");
        const typingBubble = UI.el("div", "rounded-md-lg px-5 py-3 bg-md-surface-container border border-md-outline-variant");
        typingBubble.appendChild(UI.progressLinear(null, "w-20"));
        typingWrapper.appendChild(typingBubble);
        ChatView._msgList.appendChild(typingWrapper);
        ChatView._msgList.scrollTop = ChatView._msgList.scrollHeight;
        
        try {
            const requestBody = {
                model: ChatView._selectedModel,
                messages: ChatView._messages,
                temperature: ChatView._temperature,
                stream: ChatView._stream
            };
            
            const response = await fetch("/v1/chat/completions", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${ChatView._apiKey}`
                },
                body: JSON.stringify(requestBody)
            });
            
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.error?.message || errorData.detail || `HTTP ${response.status}`);
            }
            
            typingWrapper.remove();
            
            if (ChatView._stream) {
                await ChatView._handleStreamResponse(response, ChatView._msgList, ChatView._renderMessage);
            } else {
                const data = await response.json();
                const assistantContent = data.choices?.[0]?.message?.content || "";
                ChatView._messages.push({ role: "assistant", content: assistantContent });
                const assistantMsgIndex = ChatView._messages.length - 1;
                ChatView._renderMessage("assistant", assistantContent, assistantMsgIndex);
            }
        } catch (e) {
            console.error("[ChatView] Resend error:", e);
            typingWrapper.remove();
            UI.snackbar(`重试失败: ${e.message}`, null, null, { variant: "error" });
        } finally {
            ChatView._isLoading = false;
        }
    },

    /**
     * 显示下拉菜单
     */
    _showMoreMenu(event, card, messageIndex) {
        event.stopPropagation();
        
        // 关闭其他已打开的菜单
        document.querySelectorAll(".dropdown-menu").forEach(menu => menu.remove());
        
        const msg = ChatView._messages[messageIndex];
        if (!msg) {
            console.error("[ChatView] Message not found at index:", messageIndex);
            return;
        }
        const isUser = msg.role === "user";
        
        // 创建下拉菜单
        const menu = UI.el("div", "dropdown-menu");
        menu.style.position = "fixed";
        
        // 复制内容
        const copyItem = UI.el("div", "dropdown-menu-item");
        copyItem.appendChild(UI.icon("content_copy"));
        copyItem.appendChild(UI.el("span", "", "复制内容"));
        copyItem.onclick = () => {
            ChatView._copyMessage(msg.content);
            menu.remove();
        };
        menu.appendChild(copyItem);
        
        // 复制为Markdown
        const copyMdItem = UI.el("div", "dropdown-menu-item");
        copyMdItem.appendChild(UI.icon("code"));
        copyMdItem.appendChild(UI.el("span", "", "复制为 Markdown"));
        copyMdItem.onclick = () => {
            const mdText = `**${isUser ? 'User' : 'Model'}:**\n\n${msg.content}`;
            ChatView._copyMessage(mdText);
            menu.remove();
        };
        menu.appendChild(copyMdItem);
        
        // 删除消息
        const deleteItem = UI.el("div", "dropdown-menu-item danger");
        deleteItem.appendChild(UI.icon("delete"));
        deleteItem.appendChild(UI.el("span", "", "删除消息"));
        deleteItem.onclick = () => {
            ChatView._messages = ChatView._messages.slice(0, messageIndex);
            UI.snackbar("消息已删除", null, null, { variant: "success" });
            menu.remove();
            Views.render("chat");
        };
        menu.appendChild(deleteItem);
        
        // 添加菜单到 body
        document.body.appendChild(menu);
        
        // 获取按钮位置并定位菜单
        const btnRect = event.target.closest(".icon-btn").getBoundingClientRect();
        const menuRect = menu.getBoundingClientRect();
        
        // 计算菜单位置（在按钮下方，右对齐）
        let top = btnRect.bottom + 4;
        let left = btnRect.right - menuRect.width;
        
        // 确保菜单不会超出视口
        if (left < 8) left = 8;
        if (top + menuRect.height > window.innerHeight - 8) {
            top = btnRect.top - menuRect.height - 4;
        }
        
        menu.style.top = `${top}px`;
        menu.style.left = `${left}px`;
        
        // 显示菜单
        setTimeout(() => menu.classList.add("show"), 10);
        
        // 点击外部关闭菜单
        const closeMenu = (e) => {
            const target = e.target;
            // 检查target是否是DOM元素
            if (!target || typeof target.contains !== 'function') return;
            
            if (!menu.contains(target) && target !== event.target) {
                menu.classList.remove("show");
                setTimeout(() => menu.remove(), 200);
                document.removeEventListener("click", closeMenu);
            }
        };
        setTimeout(() => document.addEventListener("click", closeMenu), 0);
    },

    /**
     * 格式化消息内容（支持简单的换行）
     */
    _formatContent(text) {
        if (!text) return "";
        // 转义 HTML 并保留换行
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/\n/g, "<br>");
    },

    /**
     * Render chat view
     * @param {HTMLElement} container - Container element
     */
    async render(container) {
        // 初始化
        await ChatView._init();

        // 检测是否为移动端
        const isMobile = window.innerWidth < 768;

        // 布局说明：
        // - 移动端：固定高度，消息区域可滚动，输入框固定底部
        // - 桌面端：左侧参数面板 + 右侧聊天区域并排
        container.classList.remove("gap-6", "min-h-full");
        
        if (isMobile) {
            // 移动端：启用固定高度布局（消息区滚动 + 输入区固定底部）
            container.classList.remove("pb-4", "md:pb-0");
            container.classList.add("chat-mobile-container");
            container.parentElement?.classList.add("chat-view-active");
        } else {
            container.classList.add("flex-col", "md:flex-row", "gap-4", "pb-0");
        }

        // Settings Panel - hidden on mobile
        const settingsPanel = UI.card("filled", "w-full md:w-80 flex-shrink-0 md:h-full overflow-y-auto hidden md:block");
        const settingsTitle = UI.el("h3", "text-title-large text-md-on-surface mb-4", "配置参数");
        settingsPanel.appendChild(settingsTitle);

        // API Key 状态显示
        const apiKeyStatus = UI.el("div", "mb-4 p-3 rounded-md-sm bg-md-surface-container-high");
        const updateApiKeyStatus = () => {
            apiKeyStatus.innerHTML = "";
            if (ChatView._apiKey) {
                const keyPreview = ChatView._apiKey.length > 15 
                    ? `${ChatView._apiKey.slice(0, 7)}...${ChatView._apiKey.slice(-4)}`
                    : ChatView._apiKey;
                apiKeyStatus.className = "mb-4 p-3 rounded-md-sm bg-md-success-container";
                apiKeyStatus.appendChild(UI.el("div", "flex items-center gap-2 text-md-on-success-container text-label-medium"));
                apiKeyStatus.firstChild.appendChild(UI.icon("check_circle", "text-sm"));
                apiKeyStatus.firstChild.appendChild(document.createTextNode(`API Key: ${keyPreview}`));
            } else {
                apiKeyStatus.className = "mb-4 p-3 rounded-md-sm bg-md-error-container";
                apiKeyStatus.appendChild(UI.el("div", "flex items-center gap-2 text-md-on-error-container text-label-medium"));
                apiKeyStatus.firstChild.appendChild(UI.icon("error", "text-sm"));
                apiKeyStatus.firstChild.appendChild(document.createTextNode("未配置 API Key，请先登录"));
            }
        };
        updateApiKeyStatus();
        settingsPanel.appendChild(apiKeyStatus);

        // Model Selection
        const modelOptions = ChatView._models.length > 0 
            ? ChatView._models.map(m => ({ value: m, label: m }))
            : [{ value: "", label: "请先获取模型列表" }];
        
        const modelSelect = UI.select("模型选择", modelOptions, ChatView._selectedModel || "");
        modelSelect.select.onchange = (e) => {
            ChatView._selectedModel = e.target.value;
        };
        settingsPanel.appendChild(modelSelect.wrapper);

        // 刷新模型按钮
        const refreshModelsBtn = UI.btn("刷新模型列表", async () => {
            refreshModelsBtn.setLoading(true);
            await ChatView._fetchModels();
            refreshModelsBtn.setLoading(false);
            
            // 更新下拉框选项
            modelSelect.select.innerHTML = "";
            const newOptions = ChatView._models.length > 0 
                ? ChatView._models.map(m => ({ value: m, label: m }))
                : [{ value: "", label: "请先获取模型列表" }];
            newOptions.forEach(opt => {
                const option = document.createElement("option");
                option.value = opt.value;
                option.textContent = opt.label;
                if (opt.value === ChatView._selectedModel) {
                    option.selected = true;
                }
                modelSelect.select.appendChild(option);
            });
            
            UI.snackbar(`已获取 ${ChatView._models.length} 个模型`, null, null, { variant: "success" });
        }, "text", "refresh");
        refreshModelsBtn.classList.add("mb-4", "w-full");
        settingsPanel.appendChild(refreshModelsBtn);

        // Temperature Slider
        const createSlider = (label, min, max, step, val, onChange) => {
            const wrap = UI.el("div", "mb-4");
            const header = UI.el("div", "flex justify-between items-center mb-2");
            header.appendChild(UI.el("label", "text-label-large text-md-on-surface-variant", label));
            const valDisplay = UI.el("span", "text-label-medium font-mono text-md-primary bg-md-primary-container px-2 py-0.5 rounded-md-xs", String(val));
            header.appendChild(valDisplay);

            const rng = document.createElement("input");
            rng.type = "range";
            rng.min = String(min);
            rng.max = String(max);
            rng.step = String(step);
            rng.value = String(val);
            rng.className = "md-range";
            rng.oninput = (e) => {
                const newVal = parseFloat(e.target.value);
                valDisplay.textContent = String(newVal);
                if (onChange) onChange(newVal);
            };

            wrap.appendChild(header);
            wrap.appendChild(rng);
            return wrap;
        };

        settingsPanel.appendChild(createSlider("Temperature", 0, 2, 0.1, ChatView._temperature, (v) => {
            ChatView._temperature = v;
        }));

        settingsPanel.appendChild(UI.divider("my-4"));
        
        // Stream switch
        const streamSwitch = UI.switch("Stream Response", ChatView._stream);
        streamSwitch.querySelector("input").onchange = (e) => {
            ChatView._stream = e.target.checked;
        };
        settingsPanel.appendChild(streamSwitch);

        settingsPanel.appendChild(UI.divider("my-4"));

        // 清空对话按钮
        const clearBtn = UI.btn("清空对话", () => {
            ChatView._messages = [];
            msgList.innerHTML = "";
            
            // 重新显示欢迎界面
            const welcomeWrapper = UI.el("div", "flex w-full justify-center py-8");
            const welcomeCard = UI.el("div", "text-center p-6 bg-md-surface-container rounded-md-lg max-w-md");
            welcomeCard.appendChild(UI.icon("chat", "text-4xl text-md-primary mb-3"));
            welcomeCard.appendChild(UI.el("h3", "text-title-medium text-md-on-surface mb-2", "欢迎使用 Zoaholic Playground"));
            welcomeCard.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant",
                ChatView._apiKey
                    ? "选择模型并开始对话吧！"
                    : "请先登录以获取 API Key，然后开始对话。"
            ));
            welcomeWrapper.appendChild(welcomeCard);
            msgList.appendChild(welcomeWrapper);
            
            UI.snackbar("对话已清空", null, null, { variant: "info" });
        }, "outlined", "delete");
        clearBtn.classList.add("w-full");
        settingsPanel.appendChild(clearBtn);

        container.appendChild(settingsPanel);

        // Chat Area
        // 移动端不使用 UI.card()（默认 p-6/圆角会干扰高度计算）
        let chatArea;
        if (isMobile) {
            chatArea = UI.el("div", "flex-1 flex flex-col min-h-0 overflow-hidden");
        } else {
            chatArea = UI.card("outlined", "flex-1 flex flex-col min-h-0 overflow-hidden");
        }

        // Message list with proper scrolling
        const msgListClasses = isMobile
            ? "flex-1 overflow-y-auto p-3 space-y-4 bg-md-surface-container-low"
            : "flex-1 overflow-y-auto p-4 md:p-6 space-y-4 bg-md-surface-container-low";
        const msgList = UI.el("div", msgListClasses);
        if (isMobile) {
            msgList.classList.add("chat-messages-area");
        }
        msgList.style.webkitOverflowScrolling = "touch"; // Smooth scrolling on iOS

        const renderMessage = (role, text, messageIndex = null) => {
            const isUser = role === "user";
            
            // Message wrapper
            const wrapper = UI.el("div", "message-wrapper");
            
            // Role label
            const roleLabel = UI.el("div", "role-label", isUser ? "User" : "Model");
            wrapper.appendChild(roleLabel);
            
            // Message card
            const card = UI.el("div", "message-card");
            card.dataset.messageIndex = messageIndex;
            
            // Message content
            const content = UI.el("div", "message-content");
            content.innerHTML = ChatView._formatContent(text);
            card.appendChild(content);
            
            // Action bar (编辑、重试、更多)
            const actionBar = UI.el("div", "action-bar");
            
            // 编辑按钮
            const editBtn = UI.el("button", "icon-btn");
            editBtn.title = "编辑消息";
            editBtn.appendChild(UI.icon("edit"));
            editBtn.onclick = () => ChatView._editMessage(card, content, messageIndex, text);
            actionBar.appendChild(editBtn);
            
            // 重试按钮
            const retryBtn = UI.el("button", "icon-btn");
            retryBtn.title = "重新生成";
            retryBtn.appendChild(UI.icon("refresh"));
            retryBtn.onclick = () => ChatView._retryMessage(messageIndex);
            actionBar.appendChild(retryBtn);
            
            // 更多菜单按钮
            const moreBtn = UI.el("button", "icon-btn");
            moreBtn.title = "更多选项";
            moreBtn.appendChild(UI.icon("more_vert"));
            moreBtn.onclick = (e) => ChatView._showMoreMenu(e, card, messageIndex);
            actionBar.appendChild(moreBtn);
            
            card.appendChild(actionBar);
            wrapper.appendChild(card);
            msgList.appendChild(wrapper);
            msgList.scrollTop = msgList.scrollHeight;
        };

        // 保存引用供重试功能使用
        ChatView._msgList = msgList;
        ChatView._renderMessage = renderMessage;

        // 渲染历史消息
        ChatView._messages.forEach((msg, index) => {
            if (msg.role !== "system") {
                renderMessage(msg.role, msg.content, index);
            }
        });

        // 如果没有消息，显示欢迎提示
        if (ChatView._messages.length === 0) {
            const welcomeWrapper = UI.el("div", "flex w-full justify-center py-8");
            const welcomeCard = UI.el("div", "text-center p-6 bg-md-surface-container rounded-md-lg max-w-md");
            welcomeCard.appendChild(UI.icon("chat", "text-4xl text-md-primary mb-3"));
            welcomeCard.appendChild(UI.el("h3", "text-title-medium text-md-on-surface mb-2", "欢迎使用 Zoaholic Playground"));
            welcomeCard.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant", 
                ChatView._apiKey 
                    ? "选择模型并开始对话吧！" 
                    : "请先登录以获取 API Key，然后开始对话。"
            ));
            welcomeWrapper.appendChild(welcomeCard);
            msgList.appendChild(welcomeWrapper);
        }

        chatArea.appendChild(msgList);

        // Input Area
        const inputAreaClasses = isMobile
            ? "p-3 border-t border-md-outline-variant bg-md-surface chat-input-area"
            : "p-4 border-t border-md-outline-variant bg-md-surface";
        const inputArea = UI.el("div", inputAreaClasses);
        const form = UI.el("form", "flex gap-2 md:gap-3");

        // 移动端：把“侧边模型选择/参数面板”入口放在输入框旁边（可见且易触达）
        if (isMobile) {
            const settingsBtn = UI.iconBtn(
                "tune",
                () => ChatView._showMobileSettings(),
                "standard",
                { tooltip: "配置参数" }
            );
            settingsBtn.type = "button";
            form.appendChild(settingsBtn);
        }

        const chatInput = document.createElement("input");
        chatInput.type = "text";
        chatInput.className = isMobile
            ? "flex-1 md-input md-input-compact md-input-pill md-input-surface-container text-body-medium"
            : "flex-1 md-input md-input-large md-input-pill md-input-surface-container text-body-large";
        chatInput.placeholder = ChatView._apiKey ? "输入消息..." : "请先登录...";
        chatInput.disabled = !ChatView._apiKey;

        // 移动端：发送按钮使用图标
        let sendBtn;
        if (isMobile) {
            sendBtn = UI.el("button", "w-10 h-10 rounded-full bg-md-primary flex items-center justify-center flex-shrink-0 disabled:opacity-38 disabled:cursor-not-allowed");
            sendBtn.appendChild(UI.icon("send", "text-md-on-primary text-lg"));
        } else {
            sendBtn = UI.btn("发送", null, "filled", "send");
        }
        sendBtn.type = "submit";
        sendBtn.disabled = !ChatView._apiKey;

        form.appendChild(chatInput);
        form.appendChild(sendBtn);

        form.onsubmit = async (e) => {
            e.preventDefault();
            const val = chatInput.value.trim();
            if (!val) return;
            
            // 清空欢迎消息
            const welcomeWrapper = msgList.querySelector(".justify-center");
            if (welcomeWrapper) {
                welcomeWrapper.remove();
            }

            chatInput.value = "";
            chatInput.disabled = true;
            sendBtn.disabled = true;

            await ChatView._sendMessage(val, msgList, renderMessage);

            chatInput.disabled = false;
            sendBtn.disabled = false;
            chatInput.focus();
        };

        inputArea.appendChild(form);
        chatArea.appendChild(inputArea);
        container.appendChild(chatArea);

        // 清理：离开聊天页面时移除特殊类
        ChatView._cleanup = () => {
            container.parentElement?.classList.remove("chat-view-active");
        };
    },

    /**
     * 移动端设置面板（Side Sheet）：提供“侧边模型选择/参数”入口
     */
    _showMobileSettings() {
        const isMobile = window.innerWidth < 768;
        const width = isMobile ? "max-w-full" : "max-w-lg";

        UI.sideSheet(
            "配置参数",
            () => {
                const root = UI.el("div", "flex flex-col gap-4");

                // API Key 状态
                const apiKeyStatus = UI.el("div", "p-3 rounded-md-sm");
                if (ChatView._apiKey) {
                    const keyPreview = ChatView._apiKey.length > 15
                        ? `${ChatView._apiKey.slice(0, 7)}...${ChatView._apiKey.slice(-4)}`
                        : ChatView._apiKey;
                    apiKeyStatus.className = "p-3 rounded-md-sm bg-md-success-container";
                    const row = UI.el("div", "flex items-center gap-2 text-md-on-success-container text-label-medium");
                    row.appendChild(UI.icon("check_circle", "text-sm"));
                    row.appendChild(document.createTextNode(`API Key: ${keyPreview}`));
                    apiKeyStatus.appendChild(row);
                } else {
                    apiKeyStatus.className = "p-3 rounded-md-sm bg-md-error-container";
                    const row = UI.el("div", "flex items-center gap-2 text-md-on-error-container text-label-medium");
                    row.appendChild(UI.icon("error", "text-sm"));
                    row.appendChild(document.createTextNode("未配置 API Key，请先登录"));
                    apiKeyStatus.appendChild(row);
                }
                root.appendChild(apiKeyStatus);

                const getModelOptions = () => (
                    ChatView._models.length > 0
                        ? ChatView._models.map(m => ({ value: m, label: m }))
                        : [{ value: "", label: "请先获取模型列表" }]
                );

                // 模型选择
                const modelSelect = UI.select("模型选择", getModelOptions(), ChatView._selectedModel || "");
                modelSelect.select.onchange = (e) => {
                    ChatView._selectedModel = e.target.value;
                };
                root.appendChild(modelSelect.wrapper);

                // 刷新模型列表
                const refreshBtn = UI.btn("刷新模型列表", async () => {
                    refreshBtn.setLoading(true);
                    await ChatView._fetchModels();
                    refreshBtn.setLoading(false);

                    if (!ChatView._selectedModel && ChatView._models.length > 0) {
                        ChatView._selectedModel = ChatView._models[0];
                    }

                    modelSelect.select.innerHTML = "";
                    getModelOptions().forEach(opt => {
                        const option = document.createElement("option");
                        option.value = opt.value;
                        option.textContent = opt.label;
                        if (opt.value === ChatView._selectedModel) {
                            option.selected = true;
                        }
                        modelSelect.select.appendChild(option);
                    });

                    UI.snackbar(`已获取 ${ChatView._models.length} 个模型`, null, null, { variant: "success" });
                }, "text", "refresh");
                refreshBtn.classList.add("w-full");
                root.appendChild(refreshBtn);

                // Temperature
                const tempWrap = UI.el("div", "");
                const tempHeader = UI.el("div", "flex justify-between items-center mb-2");
                tempHeader.appendChild(UI.el("label", "text-label-large text-md-on-surface-variant", "Temperature"));
                const tempDisplay = UI.el("span", "text-label-medium font-mono text-md-primary bg-md-primary-container px-2 py-0.5 rounded-md-xs", String(ChatView._temperature));
                tempHeader.appendChild(tempDisplay);

                const tempRange = document.createElement("input");
                tempRange.type = "range";
                tempRange.min = "0";
                tempRange.max = "2";
                tempRange.step = "0.1";
                tempRange.value = String(ChatView._temperature);
                tempRange.className = "md-range";
                tempRange.oninput = (e) => {
                    ChatView._temperature = parseFloat(e.target.value);
                    tempDisplay.textContent = String(ChatView._temperature);
                };

                tempWrap.appendChild(tempHeader);
                tempWrap.appendChild(tempRange);
                root.appendChild(tempWrap);

                // Stream 开关
                const streamSwitch = UI.switch("Stream Response", ChatView._stream);
                streamSwitch.querySelector("input").onchange = (e) => {
                    ChatView._stream = e.target.checked;
                };
                root.appendChild(streamSwitch);

                root.appendChild(UI.divider("my-2"));

                // 清空对话按钮
                const clearBtn = UI.btn("清空对话", () => {
                    ChatView._messages = [];
                    if (ChatView._msgList) {
                        ChatView._msgList.innerHTML = "";

                        const welcomeWrapper = UI.el("div", "flex w-full justify-center py-8");
                        const welcomeCard = UI.el("div", "text-center p-6 bg-md-surface-container rounded-md-lg max-w-md");
                        welcomeCard.appendChild(UI.icon("chat", "text-4xl text-md-primary mb-3"));
                        welcomeCard.appendChild(UI.el("h3", "text-title-medium text-md-on-surface mb-2", "欢迎使用 Zoaholic Playground"));
                        welcomeCard.appendChild(UI.el("p", "text-body-medium text-md-on-surface-variant",
                            ChatView._apiKey
                                ? "选择模型并开始对话吧！"
                                : "请先登录以获取 API Key，然后开始对话。"
                        ));
                        welcomeWrapper.appendChild(welcomeCard);
                        ChatView._msgList.appendChild(welcomeWrapper);
                    }
                    UI.snackbar("对话已清空", null, null, { variant: "info" });
                }, "outlined", "delete");
                clearBtn.classList.add("w-full");
                root.appendChild(clearBtn);

                return root;
            },
            () => true,
            "完成",
            { width, cancelText: "关闭" }
        );
    }
};