"""Grok (xAI) 渠道注册与配额解析测试。"""

from pathlib import Path
import sys

ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_grok_channel_registers_oauth_channel_with_ui_slots():
    from core.channels import grok_channel
    from core.channels.registry import get_channel, unregister_channel

    unregister_channel("grok")
    grok_channel.register()
    definition = get_channel("grok")

    assert definition is not None
    assert definition.is_oauth is True
    assert definition.oauth_provider is not None
    assert definition.type_name == "openai-responses"
    assert definition.default_base_url == "https://cli-chat-proxy.grok.com/v1"
    assert definition.request_adapter is not None
    assert definition.stream_adapter is not None
    assert definition.response_adapter is not None
    assert definition.models_adapter is not None
    assert definition.ui_slots is not None
    assert "quota_display" in definition.ui_slots
    assert "import_placeholder" in definition.ui_slots
    display = definition.ui_slots["quota_display"]
    assert "ctx.context?.mode" in display
    assert "mode === 'rack'" in display
    assert "plan" in display
    assert definition.to_dict()["is_oauth"] is True
    assert definition.to_dict()["ui_slots"]["quota_display"] == display


def test_grok_parse_ratelimit_headers_full():
    from core.channels.grok_channel import _parse_ratelimit_headers

    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "75",
        "x-ratelimit-limit-tokens": "1000000",
        "x-ratelimit-remaining-tokens": "500000",
        "x-subscription-tier": "SuperGrok",
        "x-entitlement-status": "active",
        "retry-after": "30",
    }
    quota = _parse_ratelimit_headers(headers)
    assert quota is not None
    assert quota["quota_inner"] == 75.0
    assert quota["quota_outer"] == 50.0
    assert quota["raw"]["x-subscription-tier"] == "SuperGrok"
    assert quota["raw"]["retry-after"] == "30"


def test_grok_parse_ratelimit_headers_empty():
    from core.channels.grok_channel import _parse_ratelimit_headers

    assert _parse_ratelimit_headers(None) is None
    assert _parse_ratelimit_headers({}) is None
    assert _parse_ratelimit_headers({"content-type": "application/json"}) is None


def test_grok_build_billing_quota_weekly_and_monthly():
    from core.channels.grok_channel import _build_billing_quota

    weekly = {
        "config": {
            "currentPeriod": {"type": "weekly", "start": "2026-07-20", "end": "2026-07-27"},
            "creditUsagePercent": 25.0,
            "productUsage": [{"product": "chat", "usagePercent": 30.0}],
        }
    }
    monthly = {
        "config": {
            "monthlyLimit": 15000,
            "used": 3000,
            "billingPeriodStart": "2026-07-01",
            "billingPeriodEnd": "2026-08-01",
        }
    }
    quota = _build_billing_quota(weekly, monthly)
    assert quota is not None
    # weekly: 100 - 25 = 75% 剩余
    assert quota["quota_inner"] == 75.0
    # monthly: 3000/15000 = 20% 已用 → 80% 剩余
    assert quota["quota_outer"] == 80.0
    assert quota["raw"]["plan"] == "SuperGrok"
    assert quota["raw"]["monthly_limit_cents"] == 15000
    assert quota["raw"]["monthly_used_cents"] == 3000


def test_grok_build_billing_quota_heavy_plan():
    from core.channels.grok_channel import _build_billing_quota

    quota = _build_billing_quota(None, {"config": {"monthlyLimit": {"val": 150000}, "used": 0}})
    assert quota is not None
    assert quota["raw"]["plan"] == "SuperGrok Heavy"
    assert quota["quota_outer"] == 100.0


def test_grok_build_billing_quota_empty():
    from core.channels.grok_channel import _build_billing_quota

    assert _build_billing_quota(None, None) is None
    assert _build_billing_quota({}, {}) is None


def test_grok_build_billing_quota_free_plan():
    """免费账号实测响应（2026-07-27）：monthlyLimit=0、无 creditUsagePercent，应显示 Free。"""
    from core.channels.grok_channel import _build_billing_quota

    weekly = {
        "config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-07-22", "end": "2026-07-29"},
            "onDemandCap": {"val": 0},
            "isUnifiedBillingUser": True,
        }
    }
    monthly = {
        "config": {
            "monthlyLimit": {"val": 0},
            "used": {"val": 0},
            "billingPeriodStart": "2026-07-01",
            "billingPeriodEnd": "2026-08-01",
        }
    }
    quota = _build_billing_quota(weekly, monthly)
    assert quota is not None
    assert quota["raw"]["plan"] == "Free"
    # 免费账号没有 creditUsagePercent，quota_inner 不存在
    assert "quota_inner" not in quota
    # monthlyLimit=0 时不计算 quota_outer
    assert "quota_outer" not in quota


def test_grok_provider_auth_url_pkce():
    from core.channels.grok_channel import GrokProvider, CLIENT_ID, DEFAULT_REDIRECT_URI

    provider = GrokProvider()
    url, verifier = provider.build_auth_url("state123")
    assert verifier
    assert url.startswith("https://auth.x.ai/oauth2/authorize?")
    assert f"client_id={CLIENT_ID}" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=state123" in url
    assert "grok-cli%3Aaccess" in url or "grok-cli:access" in url
    from urllib.parse import quote
    assert quote(DEFAULT_REDIRECT_URI, safe="") in url


def test_grok_provider_redirect_mode_manual():
    from core.channels.grok_channel import GrokProvider

    provider = GrokProvider()
    assert provider.redirect_mode == "manual"
    assert provider.localhost_redirect_uri == "http://127.0.0.1:56121/callback"


def test_grok_normalize_sso_token():
    from core.channels.grok_channel import normalize_sso_token

    assert normalize_sso_token("sso:abc123") == "abc123"
    assert normalize_sso_token("sso=abc123") == "abc123"
    assert normalize_sso_token("sso-rw=abc123") == "abc123"
    assert normalize_sso_token("sso=abc123; other=zzz") == "abc123"
    assert normalize_sso_token("cookie: sso-rw=abc123; foo=bar") == "abc123"
    assert normalize_sso_token("abc123") == "abc123"
    assert normalize_sso_token("") == ""
    assert normalize_sso_token(None) == ""


def test_grok_looks_like_sso_cookie():
    from core.channels.grok_channel import looks_like_sso_cookie

    assert looks_like_sso_cookie("sso:abc") is True
    assert looks_like_sso_cookie("sso=abc") is True
    assert looks_like_sso_cookie("sso-rw=abc; x=y") is True
    assert looks_like_sso_cookie("cookie: sso=abc") is True
    assert looks_like_sso_cookie("plain-refresh-token-xyz") is False
    assert looks_like_sso_cookie("") is False


def test_grok_parse_authorization_code():
    from core.channels.grok_channel import _parse_authorization_code

    assert _parse_authorization_code("rawcode123") == "rawcode123"
    assert _parse_authorization_code(
        "http://127.0.0.1:56121/callback?code=abc&state=xyz"
    ) == "abc"
    assert _parse_authorization_code("code=abc&state=xyz") == "abc"
    assert _parse_authorization_code("?code=abc") == "abc"


def test_grok_build_credential_extracts_identity():
    import base64 as b64
    import json as js
    from core.channels.grok_channel import GrokProvider

    claims = {"email": "user@example.com", "sub": "user-123", "team_id": "team-9"}
    payload = b64.urlsafe_b64encode(js.dumps(claims).encode()).rstrip(b"=").decode()
    id_token = f"header.{payload}.sig"

    provider = GrokProvider()
    cred = provider._build_credential(
        {},
        {
            "access_token": "at-xxx",
            "refresh_token": "rt-yyy",
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": 21600,
        },
    )
    assert cred["access_token"] == "at-xxx"
    assert cred["refresh_token"] == "rt-yyy"
    assert cred["email"] == "user@example.com"
    assert cred["account_id"] == "user-123"
    assert cred["team_id"] == "team-9"
    assert cred["expires_at"] > 0
    assert cred["base_url"] == "https://cli-chat-proxy.grok.com/v1"


def test_grok_build_credential_default_expiry_and_sso_cleanup():
    from core.channels.grok_channel import GrokProvider

    provider = GrokProvider()
    cred = provider._build_credential(
        {"sso_token": "should-be-dropped"},
        {"access_token": "at-xxx"},
    )
    assert "sso_token" not in cred
    # 缺省 expires_in 时按 6 小时处理
    import time
    assert cred["expires_at"] > time.time() + 5 * 3600


def test_grok_cli_headers():
    from core.channels.grok_channel import _cli_headers

    headers = _cli_headers("token123")
    assert headers["Authorization"] == "Bearer token123"
    assert headers["X-XAI-Token-Auth"] == "xai-grok-cli"
    assert headers["x-grok-client-version"] == "0.2.93"
    assert "grok-pager/0.2.93" in headers["User-Agent"]


def test_grok_models_fallback():
    import asyncio
    from core.channels.grok_channel import fetch_grok_models, DEFAULT_MODELS

    class _FailingClient:
        async def get(self, *a, **k):
            raise RuntimeError("network down")

    models = asyncio.run(fetch_grok_models(_FailingClient(), {"base_url": "https://cli-chat-proxy.grok.com/v1"}))
    assert models == DEFAULT_MODELS
    assert "grok-4.5" in models
    assert "grok-build-0.1" in models


def test_grok_models_appends_imagine_models():
    import asyncio
    from core.channels.grok_channel import fetch_grok_models

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "grok-4.5"}, {"id": "grok-4.3"}]}

    class _FakeClient:
        async def get(self, *a, **k):
            return _FakeResponse()

    models = asyncio.run(fetch_grok_models(_FakeClient(), {"base_url": "https://cli-chat-proxy.grok.com/v1"}))
    assert "grok-4.5" in models
    assert "grok-4.3" in models
    # CLI 网关 /models 不含 Imagine，静态补齐
    assert "grok-imagine" in models
    assert "grok-imagine-video" in models
    assert "grok-imagine-image" in models


def test_grok_media_model_classification():
    from core.channels.grok_channel import _is_media_model, _is_video_model

    assert _is_media_model("grok-imagine") is True
    assert _is_media_model("grok-imagine-image") is True
    assert _is_media_model("grok-imagine-video") is True
    assert _is_media_model("grok-imagine-video-1.5") is True
    assert _is_media_model("grok-4.5") is False
    assert _is_media_model("grok-build-0.1") is False
    assert _is_media_model(None) is False

    assert _is_video_model("grok-imagine-video") is True
    assert _is_video_model("grok-imagine-video-1.5") is True
    assert _is_video_model("grok-imagine-image") is False
    assert _is_video_model("grok-imagine") is False
    assert _is_video_model("grok-4.5") is False


def test_grok_normalize_media_base_url():
    from core.channels.grok_channel import _normalize_media_base_url, MEDIA_BASE_URL

    # cli-chat-proxy 或未配置 → 固定官方 API
    assert _normalize_media_base_url({}) == MEDIA_BASE_URL
    assert _normalize_media_base_url({"base_url": "https://cli-chat-proxy.grok.com/v1"}) == MEDIA_BASE_URL
    assert _normalize_media_base_url({"base_url": "https://cli-chat-proxy.grok.com/v1/"}) == MEDIA_BASE_URL
    # 显式第三方反代 → 尊重配置
    assert _normalize_media_base_url({"base_url": "https://proxy.example.com/v1"}) == "https://proxy.example.com/v1"


def test_grok_image_content_items_b64_and_url():
    from core.channels.grok_channel import _image_content_items

    items = _image_content_items(
        {"data": [{"b64_json": "aGVsbG8=", "revised_prompt": "a cat"}, {"url": "https://img.example.com/1.png"}]},
        {"output_format": "jpeg"},
    )
    assert items[0] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="}}
    assert items[1] == {"type": "text", "text": "*Revised prompt: a cat*"}
    assert items[2] == {"type": "image_url", "image_url": {"url": "https://img.example.com/1.png"}}


def test_grok_video_status_helpers():
    from core.channels.grok_channel import _video_status_done, _video_status_failed

    assert _video_status_done("done") is True
    assert _video_status_done("completed") is True
    assert _video_status_done("pending") is False
    assert _video_status_failed("failed") is True
    assert _video_status_failed("expired") is True
    assert _video_status_failed("pending") is False


def test_grok_build_media_payload_image_generation():
    from types import SimpleNamespace
    from core.channels.grok_channel import _build_media_payload

    request = SimpleNamespace(
        model="grok-imagine",
        messages=[SimpleNamespace(role="user", content="draw a whale")],
        model_dump=lambda exclude_unset=True: {"model": "grok-imagine", "n": 2},
    )
    provider = {"base_url": "https://cli-chat-proxy.grok.com/v1", "model": [{"grok-imagine-image": "grok-imagine"}]}
    url, headers, payload = _build_media_payload(request, provider, "at-xxx")

    # base_url 是 cli-chat-proxy 时媒体端点切到官方 API
    assert url == "https://api.x.ai/v1/images/generations"
    assert headers["Authorization"] == "Bearer at-xxx"
    assert payload["model"] == "grok-imagine-image"
    assert payload["prompt"] == "draw a whale"
    assert payload["n"] == 2


def test_grok_build_media_payload_image_edit():
    from types import SimpleNamespace
    from core.channels.grok_channel import _build_media_payload

    image_item = SimpleNamespace(type="image_url", image_url=SimpleNamespace(url="https://img.example.com/in.png"))
    text_item = SimpleNamespace(type="text", text="make it blue")
    request = SimpleNamespace(
        model="grok-imagine-edit",
        messages=[SimpleNamespace(role="user", content=[text_item, image_item])],
        model_dump=lambda exclude_unset=True: {"model": "grok-imagine-edit"},
    )
    provider = {"model": ["grok-imagine-edit"]}
    url, headers, payload = _build_media_payload(request, provider, "at-xxx")

    assert url == "https://api.x.ai/v1/images/edits"
    assert payload["prompt"] == "make it blue"
    assert payload["images"] == [{"image_url": "https://img.example.com/in.png"}]


def test_grok_build_media_payload_video():
    from types import SimpleNamespace
    from core.channels.grok_channel import _build_media_payload

    request = SimpleNamespace(
        model="grok-imagine-video",
        messages=[SimpleNamespace(role="user", content="a running cat")],
        model_dump=lambda exclude_unset=True: {"model": "grok-imagine-video", "duration": 8, "aspect_ratio": "16:9"},
    )
    provider = {"model": ["grok-imagine-video"]}
    url, headers, payload = _build_media_payload(request, provider, "at-xxx")

    assert url == "https://api.x.ai/v1/videos/generations"
    assert payload["model"] == "grok-imagine-video"
    assert payload["prompt"] == "a running cat"
    assert payload["duration"] == 8
    assert payload["aspect_ratio"] == "16:9"


def test_grok_provider_resolve_base_url_and_token_url():
    from core.channels.grok_channel import GrokProvider

    provider = GrokProvider()
    assert provider._resolve_base_url({}) == "https://cli-chat-proxy.grok.com/v1"
    assert provider._resolve_token_url({}) == "https://auth.x.ai/oauth2/token"

    config = {
        "providers": [
            {"engine": "grok", "base_url": "https://grok-proxy.example.com/v1/", "token_url": "https://auth-proxy.example.com"}
        ]
    }
    assert provider._resolve_base_url(config) == "https://grok-proxy.example.com/v1"
    assert provider._resolve_token_url(config) == "https://auth-proxy.example.com/oauth2/token"
