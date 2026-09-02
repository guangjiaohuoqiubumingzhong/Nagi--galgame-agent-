"""模型后端适配层。

runtime 只关心一件事：传入角色消息，拿回下一条 assistant 文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

from ..messages import normalize_messages, render_messages

OPENAI_COMPATIBLE_USER_AGENT = "nagi/0.1"
HTTP_ATTEMPTS = 4


def _decode_json_object(body_text, backend):
    try:
        data = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{backend} 返回了无法解析的非 JSON 内容") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{backend} 返回了无效响应（JSON 顶层不是对象）。"
            "这通常是模型服务暂时异常，请稍后重试；已完成的翻译批次不会丢失。"
        )
    return data


def _retryable_http(code):
    return code == 429 or 500 <= code < 600


def _retry_delay(exc, attempt):
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    try:
        if value is not None:
            return min(max(float(value), 0.1), 8.0)
    except (TypeError, ValueError):
        pass
    return min(2**attempt, 8.0)


def _http_failure(backend, exc, body, attempts):
    detail = str(body or "").strip().replace("\r", " ").replace("\n", " ")[:1000]
    if _retryable_http(exc.code):
        message = (
            f"{backend} request failed with HTTP {exc.code}: "
            f"模型服务暂时繁忙或不可用，已自动尝试 {attempts} 次。"
            "请稍后重试；已完成的翻译批次不会丢失。"
        )
    else:
        message = f"{backend} request failed with HTTP {exc.code}"
    return RuntimeError(message + (f" 服务返回：{detail}" if detail else ""))


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.message_requests = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        messages = normalize_messages(prompt)
        self.message_requests.append(messages)
        self.prompts.append(render_messages(prompt))
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": normalize_messages(prompt),
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        data = _decode_json_object(body_text, "Ollama")
        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        message = data.get("message")
        return message.get("content", "") if isinstance(message, dict) else ""


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", []) or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：角色消息数组、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Nagi.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": normalize_messages(prompt),
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = HTTP_ATTEMPTS
        data = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _retryable_http(exc.code) and attempt < attempts - 1:
                    time.sleep(_retry_delay(exc, attempt))
                    continue
                raise _http_failure("OpenAI-compatible", exc, body, attempts) from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(min(2**attempt, 8.0))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc
            is_stream = content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:")
            if not is_stream:
                try:
                    data = _decode_json_object(body_text, "OpenAI-compatible backend")
                except RuntimeError:
                    if attempt < attempts - 1:
                        time.sleep(min(2**attempt, 8.0))
                        continue
                    raise
            break

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if is_stream:
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
                self.last_completion_metadata = {
                    "prompt_cache_supported": self.supports_prompt_cache,
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    **_extract_usage_cache_details(response_data),
                }
            if text:
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        return _extract_openai_text(data)


class OpenAIChatCompatibleModelClient:
    """OpenAI-compatible chat completions client with optional JSON mode."""

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        response_format=None,
        thinking=None,
        extra_body=None,
        request_opener=None,
    ):
        self.model = model
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.response_format = dict(response_format) if response_format else None
        self.thinking = dict(thinking) if thinking else None
        self.extra_body = dict(extra_body or {})
        self.request_opener = request_opener
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
    ):
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": normalize_messages(prompt),
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.response_format is not None:
            payload["response_format"] = dict(self.response_format)
        if self.thinking is not None:
            payload["thinking"] = dict(self.thinking)
        payload.update(self.extra_body)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        attempts = HTTP_ATTEMPTS
        data = None
        for attempt in range(attempts):
            try:
                with (self.request_opener or urllib.request.urlopen)(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _retryable_http(exc.code) and attempt < attempts - 1:
                    time.sleep(_retry_delay(exc, attempt))
                    continue
                raise _http_failure("Chat-completions", exc, body, attempts) from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(min(2**attempt, 8.0))
                    continue
                raise RuntimeError(
                    "Could not reach the chat-completions backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc
            try:
                data = _decode_json_object(body_text, "Chat-completions backend")
            except RuntimeError:
                if attempt < attempts - 1:
                    time.sleep(min(2**attempt, 8.0))
                    continue
                raise
            break

        if data.get("error"):
            raise RuntimeError(f"Chat-completions error: {data['error']}")
        choices = data.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
        self.last_completion_metadata = {
            "stop_reason": finish_reason,
            **_extract_usage_cache_details(data),
        }
        text = _extract_openai_text(data)
        if text:
            return text
        raise RuntimeError(
            "Chat-completions response ended without message content "
            f"(finish_reason={finish_reason or 'unknown'})"
        )


def _extract_anthropic_text(data):
    texts = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts)


def _extract_anthropic_metadata(data):
    content_block_types = []
    for item in data.get("content", []):
        if isinstance(item, dict):
            block_type = str(item.get("type", "")).strip()
            if block_type:
                content_block_types.append(block_type)
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "stop_reason": str(data.get("stop_reason", "") or ""),
        "content_block_types": content_block_types,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout, thinking=None, *, request_opener=None):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.thinking = dict(thinking) if thinking else None
        self.request_opener = request_opener
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        messages = normalize_messages(prompt)
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        conversation = []
        for item in messages:
            if item["role"] == "system":
                continue
            block = {"type": "text", "text": item["content"]}
            if conversation and conversation[-1]["role"] == item["role"]:
                conversation[-1]["content"].append(block)
            else:
                conversation.append({"role": item["role"], "content": [block]})
        payload = {
            "model": self.model,
            "messages": conversation,
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.thinking is not None:
            payload["thinking"] = dict(self.thinking)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = HTTP_ATTEMPTS
        data = None
        for attempt in range(attempts):
            try:
                with (self.request_opener or urllib.request.urlopen)(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _retryable_http(exc.code) and attempt < attempts - 1:
                    time.sleep(_retry_delay(exc, attempt))
                    continue
                raise _http_failure("Anthropic-compatible", exc, body, attempts) from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(min(2**attempt, 8.0))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc
            try:
                data = _decode_json_object(body_text, "Anthropic-compatible backend")
            except RuntimeError:
                if attempt < attempts - 1:
                    time.sleep(min(2**attempt, 8.0))
                    continue
                raise
            break

        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        self.last_completion_metadata = _extract_anthropic_metadata(data)
        text = _extract_anthropic_text(data)
        if text:
            return text
        stop_reason = self.last_completion_metadata["stop_reason"] or "unknown"
        content_types = ",".join(self.last_completion_metadata["content_block_types"]) or "none"
        raise RuntimeError(
            "Anthropic-compatible response ended before a text block "
            f"(stop_reason={stop_reason}, content_types={content_types})"
        )
