"""透传入站头隐私清洗（内置 header_scrubber）的行为锁定测试。

修改原因：原 header_scrubber 插件仅在生产目录存在（未入 git），且 opt-in 启用导致
  默认部署把客户端真实 IP / 地理位置 / 反代域名 / 面板 cookie 透传给上游。
修改方式：清洗规则内置到 core.passthrough._filter_passthrough_headers，本文件按
  六层匹配顺序逐场景锁定行为。
目的：防止后续改动静默放宽清洗或误删 SDK 功能头。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.passthrough import _filter_passthrough_headers


def _lower_keys(headers: dict) -> set:
    return {str(k).lower() for k in headers}


# 模拟真实入站：nginx XFF/X-Real-IP + CF 地理头 + 浏览器隐私头 + SDK 功能头
REAL_WORLD_HEADERS = {
    "Authorization": "Bearer client-key",
    "X-Forwarded-For": "1.2.3.4, 10.0.0.1",
    "X-Real-Ip": "1.2.3.4",
    "Cf-Connecting-Ip": "1.2.3.4",
    "Cf-Ipcountry": "CN",
    "Cf-Ray": "ray-id",
    "cookie": "panel_session=secret",
    "referer": "https://zoaholic.example.top/channels",
    "origin": "https://zoaholic.example.top",
    "accept-language": "zh-CN,zh;q=0.9",
    "x-request-id": "trace-abc",
    "sec-ch-ua": '"Chromium"',
    "sec-fetch-site": "same-origin",
    "x-vercel-ip-city": "shanghai",
    # SDK / 功能头：必须保留
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "openai-organization": "org-x",
    "x-stainless-lang": "js",
    "x-goog-user-project": "proj",
    "x-amz-date": "20260903T000000Z",
    "cf-aig-foo": "1",
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.161 (external, cli)",
    "x-app": "cli",
    "x-claude-code-session-id": "uuid",
    "x-client-request-id": "rid",
    "x-title": "my-app",
    "http-referer": "https://my.site",
    "x-custom-unknown": "keep-me",
    # 传输完整性：硬删
    "host": "panel.example.com",
    "content-length": "123",
    "accept-encoding": "gzip",
    "x-goog-api-key": "legacy-key",
}

EXPECTED_STRIPPED = {
    "authorization", "x-forwarded-for", "x-real-ip", "cf-connecting-ip",
    "cf-ipcountry", "cf-ray", "cookie", "referer", "origin",
    "accept-language", "x-request-id", "sec-ch-ua", "sec-fetch-site",
    "x-vercel-ip-city", "host", "content-length", "accept-encoding",
    "x-goog-api-key",
}

EXPECTED_KEPT = {
    "anthropic-beta", "anthropic-version", "openai-organization",
    "x-stainless-lang", "x-goog-user-project", "x-amz-date", "cf-aig-foo",
    "accept", "content-type", "user-agent", "x-app",
    "x-claude-code-session-id", "x-client-request-id",
    "x-title", "http-referer", "x-custom-unknown",
}


def test_default_scrub_strips_leaks_and_keeps_sdk_headers():
    out = _filter_passthrough_headers(dict(REAL_WORLD_HEADERS), {"engine": "openai"})
    got = _lower_keys(out)
    leaked = EXPECTED_STRIPPED & got
    assert not leaked, f"privacy headers leaked upstream: {leaked}"
    missing = EXPECTED_KEPT - got
    assert not missing, f"SDK/functional headers wrongly stripped: {missing}"


def test_keep_preference_rescues_privacy_headers():
    provider = {"engine": "openai", "preferences": {"keep_passthrough_headers": ["cookie", "x-vercel-ip-city"]}}
    out = _filter_passthrough_headers(
        {"cookie": "s=1", "x-vercel-ip-city": "sh", "cf-connecting-ip": "1.2.3.4"},
        provider,
    )
    assert _lower_keys(out) == {"cookie", "x-vercel-ip-city"}


def test_keep_cannot_rescue_drop_always():
    provider = {"engine": "openai", "preferences": {"keep_passthrough_headers": "authorization;host"}}
    out = _filter_passthrough_headers({"authorization": "x", "host": "h"}, provider)
    assert not out


def test_strip_preference_overrides_protected_and_unknown():
    provider = {"engine": "openai", "preferences": {"strip_passthrough_headers": "anthropic-beta;x-mine;accept"}}
    out = _filter_passthrough_headers(
        {"anthropic-beta": "b", "x-mine": "1", "accept": "a", "unknown-x": "u"},
        provider,
    )
    assert _lower_keys(out) == {"unknown-x"}


def test_antigravity_engine_skips_privacy_scrub():
    out = _filter_passthrough_headers(
        {"cookie": "s=1", "cf-connecting-ip": "1.2.3.4", "authorization": "x"},
        {"engine": "antigravity"},
    )
    # 该渠道有自己的极小出站白名单，这里只保留硬删行为
    assert _lower_keys(out) == {"cookie", "cf-connecting-ip"}


def test_legacy_single_argument_signature_still_works():
    out = _filter_passthrough_headers({"cookie": "s=1", "accept": "a"})
    assert _lower_keys(out) == {"accept"}
