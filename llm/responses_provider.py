"""Responses transport; never falls back to Chat Completions."""
import asyncio

from llm.openai_provider import OpenAIProvider
from llm.adapters import encode_request, decode_response, field
from llm.base import LLMStreamDecodeError
from llm.profiles import resolve_target


class OpenAIResponsesProvider(OpenAIProvider):
    async def generate(self, request):
        params = encode_request(request, self.config)

        def validate(response):
            # Validate before retry machinery returns, including tool JSON.
            decode_response(response, self.config)
            if not field(response, "output") and field(response, "usage") is None:
                return {"provider": "openai-responses", "reason": "empty_response"}
            return None

        async def streamed():
            stream = await asyncio.wait_for(
                self.client.responses.create(**params, stream=True),
                timeout=self._llm_timeout_seconds(),
            )
            final = None
            async with stream:
                async for event in self._iterate_with_llm_idle_timeout(stream):
                    kind = field(event, "type")
                    if kind in {"response.completed", "response.incomplete"}:
                        final = field(event, "response")
                    elif kind == "response.failed":
                        decode_response(field(event, "response"), self.config)
                        raise LLMStreamDecodeError("Responses stream failed without error details")
                    elif kind == "error":
                        raise ValueError(f"Responses provider error ({field(event, 'code')}): {field(event, 'message')}")
            if final is None:
                raise LLMStreamDecodeError("Responses stream ended without a terminal response")
            validate(final)
            return final

        async def nonstream():
            response = await self.client.responses.create(**params, stream=False)
            validate(response)
            return response

        use_stream = (self.config.extra_params or {}).get("stream", True)
        response, attempts = await self._request_with_timeout_retries(
            streamed if use_stream else nonstream, provider=resolve_target(self.config).profile,
            operation="responses.stream" if use_stream else "responses.create", response_validator=validate,
            timeout_managed=bool(use_stream),
            reserved_nonstream_fallback_factory=nonstream if use_stream else None,
            fallback_response_validator=validate,
        )
        diagnostics = {"timeout_attempts": attempts,
            "nonstream_fallback_used": any(a.get("reason") == "nonstream_fallback" for a in attempts)}
        for name, reason in (("timeout", "timeout"), ("connection", "connection_error"),
                             ("stream_decode", "stream_decode_error"), ("degenerate", "degenerate_response")):
            diagnostics[name + "_retries"] = sum(a.get("reason") == reason for a in attempts)
        return decode_response(response, self.config, diagnostics)
