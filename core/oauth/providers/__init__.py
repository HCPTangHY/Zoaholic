"""OAuth provider 包。"""

# 修改原因：Phase 1 只实现 Codex，但保留 providers 包便于后续加入 Claude Code 和 Antigravity。
# 修改方式：当前文件不做自动注册，OAuthManager.init 负责显式创建 provider。
# 目的：避免导入 providers 包时触发额外依赖或网络逻辑。
