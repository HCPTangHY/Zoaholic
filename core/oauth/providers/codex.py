"""兼容桥接 — CodexProvider 已搬迁到 core/channels/codex_channel.py"""
from core.channels.codex_channel import CodexProvider, CLIENT_ID, SCOPES, DEFAULT_TOKEN_URL

__all__ = ["CodexProvider", "CLIENT_ID", "SCOPES", "DEFAULT_TOKEN_URL"]
