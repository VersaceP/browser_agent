"""Documented endpoints and protocol identity; never contains credentials.

The provider selects a service. The api selects an encoder/SDK. Legacy
openai/anthropic configurations retain their endpoint; exact known endpoints
also select the appropriate extension mapping during migration.
"""
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

OPENAI = "openai-chat-completions"
RESPONSES = "openai-responses"
ANTHROPIC = "anthropic-messages"
API_ALIASES = {"openai": OPENAI, "openai-completions": OPENAI,
               "anthropic": ANTHROPIC, OPENAI: OPENAI, ANTHROPIC: ANTHROPIC,
               RESPONSES: RESPONSES}
ENDPOINTS = {
    # Deployment host supplied by the operator; paths follow Allinone docs.
    "allinone": {OPENAI: "https://api.juhenextvip.com/v1", ANTHROPIC: "https://api.juhenextvip.com"},
    "juao": {OPENAI: "https://ai.juaotoken.com/v1", ANTHROPIC: "https://ai.juaotoken.com"},
    "qwen-token-plan": {
        OPENAI: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ANTHROPIC: "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
    },
    "volcengine-agent-plan": {
        OPENAI: "https://ark.cn-beijing.volces.com/api/plan/v3",
        ANTHROPIC: "https://ark.cn-beijing.volces.com/api/plan",
    },
    "volcengine-coding-plan": {
        OPENAI: "https://ark.cn-beijing.volces.com/api/coding/v3",
        ANTHROPIC: "https://ark.cn-beijing.volces.com/api/coding",
    },
    # DeepSeek official (api-docs.deepseek.com/zh-cn/quick_start/pricing,
    # 2026-09-18): Chat and Responses share the root; Anthropic has its own path.
    "deepseek": {
        OPENAI: "https://api.deepseek.com",
        RESPONSES: "https://api.deepseek.com",
        ANTHROPIC: "https://api.deepseek.com/anthropic",
    },
}

# Allinone documents a shared root for both protocols. Custom deployments can
# supply that root or its /v1 endpoint; the documentation site is never an API.
CONFIGURABLE_PROVIDERS = {"allinone": (OPENAI, RESPONSES, ANTHROPIC)}
PROVIDER_APIS = {**{name: tuple(urls) for name, urls in ENDPOINTS.items()},
                 **CONFIGURABLE_PROVIDERS}


def provider_endpoints(provider: str, base_url: str | None = None) -> dict[str, str]:
    """SDK base URLs for a documented deployment root or its /v1 endpoint."""
    if provider not in CONFIGURABLE_PROVIDERS:
        return dict(ENDPOINTS.get(provider, {}))
    base_url = base_url or ENDPOINTS.get(provider, {}).get(ANTHROPIC)
    if not base_url:
        raise ValueError(f"provider {provider!r} requires an explicit base_url")
    address = urlsplit(base_url)
    path = address.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    root = urlunsplit(address._replace(path=path))
    openai = urlunsplit(address._replace(path=path + "/v1"))
    return {OPENAI: openai, RESPONSES: openai, ANTHROPIC: root}

# Verified transport compatibility, independent of model names. Each listed
# relay counts an image nested in an Anthropic tool_result as base64 text;
# ordinary user image blocks work. Both official protocols accept the nested
# form, but OpenAI Chat tool messages are text-only, so a relay that maps
# Anthropic onto a GPT backend can flatten it. Evidence: juao e015e32e and
# allinone c6a56d5e. See docs/provider-model-configuration.md.
TOOL_RESULT_IMAGE_PLACEMENT = {
    ("juao", ANTHROPIC): "user",
    ("allinone", ANTHROPIC): "user",
}


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    api: str
    model: str
    base_url: str | None
    # Only protocol extension mapping, not model capability inference.
    profile: str


def resolve_target(config: Any) -> ModelTarget:
    provider = str(config.provider).strip().lower()
    requested_api = getattr(config, "api", None)
    if not requested_api and provider not in {"openai", "anthropic"}:
        raise ValueError(f"provider {provider!r} requires explicit api ({OPENAI}, {RESPONSES} or {ANTHROPIC})")
    api = API_ALIASES.get(str(requested_api or provider).strip().lower())
    if api is None:
        raise ValueError(f"unsupported api: {requested_api!r}; expected {OPENAI}, {RESPONSES} or {ANTHROPIC}")
    url = (
        provider_endpoints(provider, config.base_url).get(api)
        if provider in CONFIGURABLE_PROVIDERS
        else config.base_url or ENDPOINTS.get(provider, {}).get(api)
    )
    if url is None and provider not in {"openai", "anthropic"}:
        raise ValueError(f"provider {provider!r} requires an explicit base_url")
    profile = provider
    if provider in {"openai", "anthropic"} and url:
        address = urlsplit(url)
        for candidate, endpoints in ENDPOINTS.items():
            if api not in endpoints:
                continue
            expected = urlsplit(endpoints[api])
            if (address.scheme, address.netloc, address.path.rstrip("/")) == (
                expected.scheme, expected.netloc, expected.path.rstrip("/")
            ):
                profile = candidate
                break
    return ModelTarget(provider, api, config.model_id, url, profile)
