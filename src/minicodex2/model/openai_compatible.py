from __future__ import annotations

import base64
import json
import logging
import http.client
import os
import re
import ssl
import time
import requests
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex2.model.adapter import ModelAdapter
from minicodex2.model.messages import ChatMessage, ModelRequest, ModelResponse, TokenUsage, ToolCall


_logger = logging.getLogger(__name__)
_cache_handler = None
# Module-level payload prefix tracker for drift detection
_PREFIX_TRACKER: dict[str, object] = {"prev_hash": None, "call_count": 0}


def _ensure_cache_log_handler():
    global _cache_handler
    if _cache_handler is not None:
        return
    log_dir = _artifact_root()
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(str(log_dir), "cache_stats.log")
    _cache_handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    _cache_handler.setLevel(logging.DEBUG)
    _cache_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _logger.addHandler(_cache_handler)
    _logger.setLevel(logging.DEBUG)


def _artifact_root() -> Path:
    configured = os.environ.get("MINICODEX2_ARTIFACT_ROOT")
    if configured:
        return Path(configured)
    return Path.cwd() / ".minicodex2"


def _capture_mini_payload(payload: dict[str, Any], *, data_bytes: bytes | None = None) -> dict[str, Any]:
    """Save the exact request bytes and a compact journal row for cache debugging.

    DeepSeek prompt-cache debugging is byte-prefix debugging.  The human-readable
    context buffer can look stable while the final HTTP payload still shifts.  We
    therefore record the exact bytes sent to the provider, the common-prefix
    length vs the previous captured payload, and an append-only journal entry.
    """
    import hashlib as _hl

    root = _artifact_root()
    capture_dir = root / "captured_payloads"
    capture_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    nonce = time.time_ns()

    # Use the ACTUAL bytes sent to DeepSeek (not re-serialized)
    if data_bytes is not None:
        curr_bytes = data_bytes
    else:
        curr_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    full_sha = _hl.sha256(curr_bytes).hexdigest()
    prefix_sha = _hl.sha256(curr_bytes[:8192]).hexdigest()

    # Find previous payload
    prev_files = sorted(capture_dir.glob("payload_*.bin"), key=lambda x: x.stat().st_mtime)
    prev_bytes = None
    if prev_files:
        prev_bytes = prev_files[-1].read_bytes()

    # Save current for next comparison
    curr_file = capture_dir / f"payload_{ts}_{nonce}.bin"
    curr_file.write_bytes(curr_bytes)

    common_prefix_bytes = 0
    prev_total = 0
    first_diff_prev = ""
    first_diff_curr = ""
    if prev_bytes is not None:
        # Byte-by-byte comparison
        min_len = min(len(prev_bytes), len(curr_bytes))
        for i in range(min_len):
            if prev_bytes[i] != curr_bytes[i]:
                common_prefix_bytes = i
                break
        else:
            common_prefix_bytes = min_len

        prev_total = len(prev_bytes)
        curr_total = len(curr_bytes)
        match_pct = common_prefix_bytes / curr_total * 100 if curr_total else 0

        if common_prefix_bytes < min_len:
            diff_ctx_prev = prev_bytes[common_prefix_bytes:common_prefix_bytes + 80]
            diff_ctx_curr = curr_bytes[common_prefix_bytes:common_prefix_bytes + 80]
            first_diff_prev = diff_ctx_prev[:40].decode("utf-8", errors="replace")
            first_diff_curr = diff_ctx_curr[:40].decode("utf-8", errors="replace")
            _logger.info(
                "PREFIX DIFF: match=%d/%d bytes (%.1f%%) prev_hash=%s curr_hash=%s first_diff_prev=%s first_diff_curr=%s",
                common_prefix_bytes, curr_total, match_pct,
                _hl.sha256(prev_bytes[:8192]).hexdigest()[:12] if prev_bytes else "N/A",
                prefix_sha[:12],
                first_diff_prev,
                first_diff_curr,
            )
        else:
            added = curr_total - prev_total
            _logger.info(
                "PREFIX PURE APPEND: match=%d/%d bytes (%.1f%%) +%d new bytes",
                common_prefix_bytes, curr_total, match_pct, added,
            )
    else:
        _logger.info("PREFIX FIRST: %d bytes hash=%s", len(curr_bytes), prefix_sha[:12])

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    request_id = f"req_{ts}_{nonce}"
    messages_record = _capture_model_messages(
        messages,
        request_id=request_id,
        model=str(payload.get("model") or ""),
        timestamp=ts,
        nonce=nonce,
    )
    record = {
        "request_id": request_id,
        "created_at": ts,
        "model": payload.get("model"),
        "path": str(curr_file),
        "messages_path": messages_record.get("path"),
        "messages_common_prefix_bytes": messages_record.get("common_prefix_bytes"),
        "messages_common_prefix_ratio": messages_record.get("common_prefix_ratio"),
        "messages_same_leading_count": messages_record.get("same_leading_count"),
        "messages_first_diff_index": messages_record.get("first_diff_index"),
        "bytes": len(curr_bytes),
        "sha256": full_sha,
        "prefix8k_sha256": prefix_sha,
        "previous_bytes": prev_total,
        "common_prefix_bytes": common_prefix_bytes,
        "common_prefix_ratio": round(common_prefix_bytes / max(1, len(curr_bytes)), 6),
        "first_diff_prev": first_diff_prev,
        "first_diff_curr": first_diff_curr,
        "messages": len(messages),
        "tools": len(tools),
    }
    journal = capture_dir / "payload_journal.jsonl"
    with journal.open("ab") as fh:
        fh.write((json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    _logger.info(
        "payload_captured id=%s bytes=%d common_prefix=%d ratio=%.1f%% messages=%d tools=%d path=%s",
        record["request_id"],
        record["bytes"],
        record["common_prefix_bytes"],
        record["common_prefix_ratio"] * 100,
        record["messages"],
        record["tools"],
        curr_file,
    )
    return record


def _capture_model_messages(
    messages: list[Any],
    *,
    request_id: str,
    model: str,
    timestamp: str,
    nonce: int,
) -> dict[str, Any]:
    """Persist the final provider-visible messages sequence for cache analysis.

    This is intentionally different from ChatHistory or ContextBufferStore:
    those are MiniCodex2 internal structures and may contain `role=runtime`.
    This capture stores the exact message objects after `to_model_dict()`, so a
    runtime message should already appear as a provider-visible `system` role.
    Comparing these files answers the question we actually care about: whether
    the model input message sequence grows append-only or whether a system/tool
    message changed/appeared in the middle.
    """

    import hashlib as _hl

    root = _artifact_root()
    capture_dir = root / "captured_messages"
    capture_dir.mkdir(parents=True, exist_ok=True)
    curr_file = capture_dir / f"messages_{timestamp}_{nonce}.json"
    curr_bytes = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    prev_files = sorted(capture_dir.glob("messages_*.json"), key=lambda x: x.stat().st_mtime)
    prev_bytes = prev_files[-1].read_bytes() if prev_files else None
    prev_messages: list[Any] = []
    if prev_bytes:
        try:
            parsed = json.loads(prev_bytes.decode("utf-8"))
            if isinstance(parsed, list):
                prev_messages = parsed
        except (UnicodeDecodeError, json.JSONDecodeError):
            prev_messages = []

    curr_file.write_bytes(curr_bytes)

    common_prefix_bytes = 0
    first_diff_prev = ""
    first_diff_curr = ""
    if prev_bytes is not None:
        min_len = min(len(prev_bytes), len(curr_bytes))
        for index in range(min_len):
            if prev_bytes[index] != curr_bytes[index]:
                common_prefix_bytes = index
                break
        else:
            common_prefix_bytes = min_len
        if common_prefix_bytes < min_len:
            first_diff_prev = prev_bytes[common_prefix_bytes : common_prefix_bytes + 80].decode(
                "utf-8",
                errors="replace",
            )
            first_diff_curr = curr_bytes[common_prefix_bytes : common_prefix_bytes + 80].decode(
                "utf-8",
                errors="replace",
            )
    same_leading_count = 0
    for previous, current in zip(prev_messages, messages):
        if previous != current:
            break
        same_leading_count += 1
    first_diff_index: int | None = None
    first_diff_prev_role = ""
    first_diff_curr_role = ""
    if prev_messages or messages:
        min_count = min(len(prev_messages), len(messages))
        if same_leading_count < min_count:
            first_diff_index = same_leading_count
            previous = prev_messages[same_leading_count]
            current = messages[same_leading_count]
            if isinstance(previous, dict):
                first_diff_prev_role = str(previous.get("role") or "")
            if isinstance(current, dict):
                first_diff_curr_role = str(current.get("role") or "")
        elif len(prev_messages) != len(messages):
            first_diff_index = same_leading_count
            first_diff_curr_role = (
                str(messages[same_leading_count].get("role") or "")
                if same_leading_count < len(messages) and isinstance(messages[same_leading_count], dict)
                else ""
            )

    record = {
        "request_id": request_id,
        "created_at": timestamp,
        "model": model,
        "path": str(curr_file),
        "bytes": len(curr_bytes),
        "sha256": _hl.sha256(curr_bytes).hexdigest(),
        "previous_bytes": len(prev_bytes or b""),
        "common_prefix_bytes": common_prefix_bytes,
        "common_prefix_ratio": round(common_prefix_bytes / max(1, len(curr_bytes)), 6),
        "message_count": len(messages),
        "previous_message_count": len(prev_messages),
        "same_leading_count": same_leading_count,
        "first_diff_index": first_diff_index,
        "first_diff_prev_role": first_diff_prev_role,
        "first_diff_curr_role": first_diff_curr_role,
        "first_diff_prev": first_diff_prev,
        "first_diff_curr": first_diff_curr,
    }
    journal = capture_dir / "messages_journal.jsonl"
    with journal.open("ab") as fh:
        fh.write((json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    _logger.info(
        "messages_captured id=%s bytes=%d common_prefix=%d ratio=%.1f%% same_messages=%d/%d diff_index=%s diff_roles=%s->%s path=%s",
        request_id,
        record["bytes"],
        record["common_prefix_bytes"],
        record["common_prefix_ratio"] * 100,
        same_leading_count,
        len(messages),
        first_diff_index,
        first_diff_prev_role or "-",
        first_diff_curr_role or "-",
        curr_file,
    )
    return record


def _log_payload_usage(capture_record: dict[str, Any], usage_data: dict[str, Any]) -> None:
    usage = _parse_token_usage(usage_data or {})
    record = {
        "request_id": capture_record.get("request_id"),
        "path": capture_record.get("path"),
        "bytes": capture_record.get("bytes"),
        "common_prefix_bytes": capture_record.get("common_prefix_bytes"),
        "common_prefix_ratio": capture_record.get("common_prefix_ratio"),
        "prompt_tokens": usage.prompt_tokens,
        "cache_hit_prompt_tokens": usage.cache_hit_prompt_tokens,
        "cache_miss_prompt_tokens": usage.cache_miss_prompt_tokens,
        "cached_prompt_tokens": usage.cached_prompt_tokens,
        "cache_hit_ratio": usage.cache_hit_ratio,
    }
    journal = _artifact_root() / "captured_payloads" / "payload_usage_journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as fh:
        fh.write((json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    _logger.info(
        "payload_usage id=%s prompt=%d hit=%d miss=%d hit_ratio=%s common_prefix=%.1f%%",
        record["request_id"],
        record["prompt_tokens"],
        record["cache_hit_prompt_tokens"],
        record["cache_miss_prompt_tokens"],
        f"{record['cache_hit_ratio'] * 100:.1f}%" if record["cache_hit_ratio"] is not None else "n/a",
        float(record["common_prefix_ratio"] or 0) * 100,
    )
class OpenAICompatibleModelAdapter(ModelAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        wire_api: str = "chat",
        timeout_seconds: int = 120,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.wire_api = wire_api
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._supports_image_messages = True
        self._roundtrip_reasoning_content = _is_deepseek_like(base_url, model)
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })

    def clone_for_parallel_use(self):
        cloned = OpenAICompatibleModelAdapter(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            wire_api=self.wire_api,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )
        cloned._supports_image_messages = self._supports_image_messages
        cloned._roundtrip_reasoning_content = self._roundtrip_reasoning_content
        return cloned

    def complete(self, request: ModelRequest) -> ModelResponse:
        has_image_messages = any(isinstance(message.metadata.get("image"), dict) for message in request.messages)
        try:
            raw = self._complete_once(request, support_images=self._supports_image_messages)
        except RuntimeError as exc:
            if (
                self._supports_image_messages
                and has_image_messages
                and _is_unsupported_image_message_error(str(exc))
            ):
                self._supports_image_messages = False
                raw = self._complete_once(request, support_images=False)
            elif self._roundtrip_reasoning_content and _is_missing_reasoning_content_error(str(exc)):
                repaired = _repair_deepseek_reasoning_history(request)
                raw = self._complete_once(repaired, support_images=self._supports_image_messages)
            else:
                raise

        choice = raw["choices"][0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        metadata: dict[str, Any] = {}
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            metadata["reasoning_content"] = reasoning_content
            self._roundtrip_reasoning_content = True
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            arguments = function.get("arguments") or "{}"
            try:
                parsed_args = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_args = {"_raw": arguments}
            tool_calls.append(
                ToolCall(
                    id=call.get("id", "tool_call"),
                    name=function.get("name", ""),
                    arguments=parsed_args,
                )
            )
        usage_data = raw.get("usage") or {}
        _ensure_cache_log_handler()
        _logger.debug("raw_usage_data: prompt=%s cache_hit=%s cache_miss=%s details=%s",
            usage_data.get("prompt_tokens"),
            usage_data.get("prompt_cache_hit_tokens") or usage_data.get("cache_hit_prompt_tokens"),
            usage_data.get("prompt_cache_miss_tokens") or usage_data.get("cache_miss_prompt_tokens"),
            usage_data.get("prompt_tokens_details"))
        usage = _parse_token_usage(usage_data)
        if usage.cache_hit_prompt_tokens or usage.cached_prompt_tokens:
            _ensure_cache_log_handler()
            hit = usage.cache_hit_prompt_tokens or usage.cached_prompt_tokens
            miss = usage.cache_miss_prompt_tokens or (usage.prompt_tokens - hit)
            pct = round(hit / max(1, usage.prompt_tokens) * 100)
            _logger.info("cache: hit=%d miss=%d prompt=%d pct=%d%%%% model=%s", hit, miss, usage.prompt_tokens, pct, self.model)
        return ModelResponse(
            message=ChatMessage(role="assistant", content=content, metadata=metadata),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason") or "stop",
            raw=raw,
        )

    def _complete_once(self, request: ModelRequest, *, support_images: bool) -> dict[str, Any]:
        if self.wire_api == "responses":
            return self._complete_responses_once(request, support_images=support_images)
        if self.wire_api != "chat":
            raise RuntimeError(f"unsupported wire_api={self.wire_api!r}; expected 'chat' or 'responses'.")
        return self._complete_chat_once(request, support_images=support_images)

    def _complete_chat_once(self, request: ModelRequest, *, support_images: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model if request.model != "default" else self.model,
        }
        # Prompt-cache providers generally key on the serialized request prefix.
        # Keep stable request-wide fields before the append-only messages array so
        # growing chat history does not push the large tool schema past the first
        # changed byte on every loop.
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        payload["messages"] = [
            m.to_model_dict(
                support_images=support_images,
                include_reasoning_content=self._roundtrip_reasoning_content,
            )
            for m in request.messages
        ]
        # Diagnostic: log first 5 and middle message to detect drift
        _ensure_cache_log_handler()
        msgs = request.messages
        for idx in range(min(5, len(msgs))):
            c = msgs[idx].content
            _logger.debug("send_msg[%d] role=%s len=%d h=%s first40=%s", idx, msgs[idx].role, len(c), str(hash(c))[-6:], c[:40].replace("\n", " "))
        if len(msgs) > 10:
            mid = len(msgs) // 2
            c = msgs[mid].content
            _logger.debug("send_msg[%d] role=%s len=%d h=%s first40=%s", mid, msgs[mid].role, len(c), str(hash(c))[-6:], c[:40].replace("\n", " "))
        data = json.dumps(payload).encode("utf-8")
        # Log payload prefix hash and detect drift vs previous turn
        payload_hash = hash(data[:8192])
        prev = _PREFIX_TRACKER.get("prev_hash")
        _PREFIX_TRACKER["call_count"] = int(_PREFIX_TRACKER.get("call_count", 0)) + 1
        call_n = _PREFIX_TRACKER["call_count"]
        if prev is not None and prev != payload_hash:
            _logger.warning(
                "PREFIX DRIFT! turn=%d prev_hash=%s curr_hash=%s msgs=%d tools=%d",
                call_n, str(prev)[-6:], str(payload_hash)[-6:],
                len(payload.get("messages", [])), len(payload.get("tools", [])))
        elif prev == payload_hash:
            _logger.info(
                "PREFIX STABLE turn=%d hash=%s msgs=%d (first 8KB unchanged)",
                call_n, str(payload_hash)[-6:], len(payload.get("messages", [])))
        else:
            _logger.debug(
                "PREFIX FIRST turn=%d hash=%s model=%s msgs=%d tools=%d",
                call_n, str(payload_hash)[-6:], payload.get("model"),
                len(payload.get("messages", [])), len(payload.get("tools", [])))
        _PREFIX_TRACKER["prev_hash"] = payload_hash
        # Capture full payload for comparison with Codex CLI
        capture_record = _capture_mini_payload(payload, data_bytes=data)
        raw = self._send_with_retries(data)
        usage_data = raw.get("usage") or {}
        _log_payload_usage(capture_record, usage_data)
        return raw

    def _complete_responses_once(self, request: ModelRequest, *, support_images: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model if request.model != "default" else self.model,
            "input": _messages_to_responses_input(
                request.messages,
                support_images=support_images,
            ),
        }
        if request.tools:
            payload["tools"] = _chat_tools_to_responses_tools(request.tools)
            payload["tool_choice"] = "auto"

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        capture_record = _capture_mini_payload(payload, data_bytes=data)
        raw = self._send_responses_with_retries(data)
        usage_data = raw.get("usage") or {}
        _log_payload_usage(capture_record, usage_data)
        return _normalize_responses_raw(raw)

    def _send_with_retries(self, data: bytes) -> dict[str, Any]:
        last_error: RuntimeError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.post(
                    f"{self.base_url}/chat/completions",
                    data=data,
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 200:
                    return response.json()
                body = response.text[:1000]
                last_error = RuntimeError(f"model request failed: HTTP {response.status_code}: {body}")
                if not _is_retryable_http_status(response.status_code) or attempt >= self.max_retries:
                    raise last_error
                time.sleep(_retry_delay_from_response(response, attempt))
            except requests.RequestException as exc:
                last_error = RuntimeError(f"model request failed: network error: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                time.sleep(_transient_retry_delay(attempt))
        raise last_error or RuntimeError("model request failed")

    def _send_responses_with_retries(self, data: bytes) -> dict[str, Any]:
        last_error: RuntimeError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.post(
                    f"{self.base_url}/responses",
                    data=data,
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 200:
                    return response.json()
                body = response.text[:1000]
                last_error = RuntimeError(f"model request failed: HTTP {response.status_code}: {body}")
                if not _is_retryable_http_status(response.status_code) or attempt >= self.max_retries:
                    raise last_error
                time.sleep(_retry_delay_from_response(response, attempt))
            except requests.RequestException as exc:
                last_error = RuntimeError(f"model request failed: network error: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                time.sleep(_transient_retry_delay(attempt))
        raise last_error or RuntimeError("model request failed")


@dataclass(slots=True)
class _NormalizedUsageData:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    cache_hit_input_tokens: int = 0
    cache_miss_input_tokens: int = 0


def _parse_token_usage(usage_data: dict[str, Any]) -> TokenUsage:
    normalized = _normalize_usage_data(usage_data)

    return TokenUsage(
        prompt_tokens=normalized.input_tokens,
        completion_tokens=normalized.output_tokens,
        total_tokens=normalized.total_tokens,
        cached_prompt_tokens=normalized.cached_input_tokens,
        cache_hit_prompt_tokens=normalized.cache_hit_input_tokens,
        cache_miss_prompt_tokens=normalized.cache_miss_input_tokens,
        estimated=not bool(usage_data),
    )


def _normalize_usage_data(usage_data: dict[str, Any]) -> _NormalizedUsageData:
    """Normalize provider-specific token accounting into MiniCodex2 terms.

    OpenAI Chat uses `prompt_tokens` and `prompt_tokens_details.cached_tokens`.
    OpenAI Responses uses `input_tokens` and `input_tokens_details.cached_tokens`.
    DeepSeek exposes explicit `prompt_cache_hit_tokens` and
    `prompt_cache_miss_tokens`.  The rest of MiniCodex2 should not need to know
    which provider produced those fields.
    """

    input_tokens = _usage_int(usage_data, "prompt_tokens") or _usage_int(usage_data, "input_tokens")
    output_tokens = _usage_int(usage_data, "completion_tokens") or _usage_int(usage_data, "output_tokens")
    total_tokens = _usage_int(usage_data, "total_tokens")
    if not total_tokens:
        total_tokens = input_tokens + output_tokens

    details = _usage_details(usage_data)
    cached_input_tokens = (
        _usage_int(usage_data, "cached_prompt_tokens")
        or _usage_int(usage_data, "cached_input_tokens")
        or _usage_int(details, "cached_tokens")
        or _usage_int(details, "cache_read_input_tokens")
    )
    cache_hit_input_tokens = (
        _usage_int(usage_data, "prompt_cache_hit_tokens")
        or _usage_int(usage_data, "cache_hit_prompt_tokens")
        or _usage_int(usage_data, "input_cache_hit_tokens")
        or cached_input_tokens
    )
    cache_miss_input_tokens = (
        _usage_int(usage_data, "prompt_cache_miss_tokens")
        or _usage_int(usage_data, "cache_miss_prompt_tokens")
        or _usage_int(usage_data, "input_cache_miss_tokens")
        or _usage_int(details, "cache_creation_input_tokens")
    )
    if cache_hit_input_tokens and not cache_miss_input_tokens and input_tokens >= cache_hit_input_tokens:
        cache_miss_input_tokens = input_tokens - cache_hit_input_tokens
    if cache_hit_input_tokens and not cached_input_tokens:
        cached_input_tokens = cache_hit_input_tokens
    return _NormalizedUsageData(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_hit_input_tokens=cache_hit_input_tokens,
        cache_miss_input_tokens=cache_miss_input_tokens,
    )


def _usage_details(usage_data: dict[str, Any]) -> dict[str, Any]:
    for key in ("prompt_tokens_details", "input_tokens_details"):
        value = usage_data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _usage_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _chat_tools_to_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Chat Completions function tools to Responses API tool objects.

    MiniCodex2 keeps one internal tool schema shape: the Chat Completions shape
    (`{"type":"function","function":{...}}`).  Responses wants the function
    fields at the top level (`{"type":"function","name":...}`).  Keeping this
    conversion at the wire boundary lets DeepSeek/chat providers stay untouched.
    """

    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            converted.append(dict(tool))
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            converted.append(dict(tool))
            continue
        item: dict[str, Any] = {
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }
        if "strict" in function:
            item["strict"] = function["strict"]
        converted.append(item)
    return converted


def _messages_to_responses_input(
    messages: list[ChatMessage],
    *,
    support_images: bool,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = "system" if message.role == "runtime" else message.role
        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant" and isinstance(message.metadata.get("tool_calls"), list):
            if message.content.strip():
                items.append({"role": "assistant", "content": message.content})
            for call in message.metadata["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or call.get("call_id") or "tool_call"),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        item: dict[str, Any] = {"role": role, "content": message.content}
        image = message.metadata.get("image")
        if message.role == "user" and isinstance(image, dict):
            item["content"] = _responses_content_for_user_image(
                message,
                image=image,
                support_images=support_images,
            )
        items.append(item)
    return items


def _responses_content_for_user_image(
    message: ChatMessage,
    *,
    image: dict[str, Any],
    support_images: bool,
) -> str | list[dict[str, Any]]:
    path = Path(str(image.get("path") or ""))
    if not support_images or not path.exists() or not path.is_file():
        return f"{message.content}\n\n[image attached: {path}]"
    mime_type = str(image.get("mime_type") or "application/octet-stream")
    detail = str(image.get("detail") or "low")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return [
        {"type": "input_text", "text": message.content},
        {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
            "detail": detail,
        },
    ]


def _normalize_responses_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a Responses API response onto the existing chat-like parser shape."""

    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_reason = str(raw.get("status") or "stop")
    output = raw.get("output")
    if not isinstance(output, list):
        output = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            message_content = item.get("content")
            if isinstance(message_content, list):
                for part in message_content:
                    text = _responses_text_part(part)
                    if text:
                        content_parts.append(text)
            elif isinstance(message_content, str):
                content_parts.append(message_content)
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "tool_call")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )
    if tool_calls:
        finish_reason = "tool_calls"
    return {
        "choices": [
            {
                "message": {
                    "content": "\n".join(part for part in content_parts if part),
                    "tool_calls": tool_calls,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": raw.get("usage") or {},
        "responses_raw": raw,
    }


def _responses_text_part(part: object) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if isinstance(part.get("text"), str):
        return str(part["text"])
    if isinstance(part.get("content"), str):
        return str(part["content"])
    return ""


_TRANSIENT_NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    TimeoutError,
    ssl.SSLError,
)


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599



# Backward-compatible retry delay (supports both requests.Response and HTTPError-like objects)
def _retry_after_seconds(exc: Exception, body: str = "", attempt: int = 0) -> float:
    import requests
    if isinstance(exc, requests.Response):
        return _retry_delay_from_response(exc, attempt)
    header = getattr(exc, "headers", None) or {}
    retry = header.get("Retry-After")
    if retry:
        try:
            return max(1.0, float(retry))
        except ValueError:
            return 5.0
    import re
    match = re.search(r"try again in (\d+(?:\.\d+)?)\s*s", body, re.IGNORECASE)
    if match:
        return max(1.0, float(match.group(1)) * 1.05)
    return 2.0 ** attempt

def _retry_delay_from_response(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After") if response.headers else None
    if header:
        try:
            return min(30.0, max(0.5, float(header)))
        except ValueError:
            pass
    body = response.text or ""
    match = re.search(r"try again in ([0-9]+(?:\.[0-9]+)?)s", body, re.IGNORECASE)
    if match:
        return min(30.0, max(0.5, float(match.group(1)) + 0.25))
    return _transient_retry_delay(attempt)


def _transient_retry_delay(attempt: int) -> float:
    return min(30.0, 1.0 * (2**attempt))


def _is_unsupported_image_message_error(text: str) -> bool:
    lowered = text.lower()
    return "image_url" in lowered and "expected `text`" in lowered


def _is_deepseek_like(base_url: str, model: str) -> bool:
    value = f"{base_url} {model}".lower()
    return "deepseek" in value


def _is_missing_reasoning_content_error(text: str) -> bool:
    lowered = text.lower()
    return "reasoning_content" in lowered and "thinking mode" in lowered


def _repair_deepseek_reasoning_history(request: ModelRequest) -> ModelRequest:
    """Recover histories created before reasoning_content round-tripping existed.

    DeepSeek thinking mode requires assistant messages to be replayed with their
    provider-private reasoning_content. Older MiniCodex2 histories cannot invent
    that field, so the safest recovery is to keep their visible content as runtime
    summaries and remove the assistant/tool protocol roles that DeepSeek rejects.
    Future assistant messages will carry reasoning_content normally.
    """
    repaired: list[ChatMessage] = []
    skipping_tool_results = False
    omitted = 0
    for message in request.messages:
        if message.role == "assistant" and not isinstance(message.metadata.get("reasoning_content"), str):
            omitted += 1
            content = message.content.strip()
            if content:
                repaired.append(
                    ChatMessage(
                        role="runtime",
                        content=(
                            "[provider history repair]\n"
                            "An older assistant message was preserved as runtime context because "
                            "DeepSeek thinking mode requires unavailable reasoning_content:\n"
                            f"{content[:1200]}"
                        ),
                    )
                )
            skipping_tool_results = bool(message.metadata.get("tool_calls"))
            continue
        if message.role == "tool" and skipping_tool_results:
            omitted += 1
            repaired.append(
                ChatMessage(
                    role="runtime",
                    content=(
                        "[provider history repair]\n"
                        "An older tool result was preserved as runtime context because its assistant "
                        "tool_call message could not be replayed in DeepSeek thinking mode:\n"
                        f"{message.name or 'tool'}: {message.content[:1200]}"
                    ),
                )
            )
            continue
        if message.role != "tool":
            skipping_tool_results = False
        repaired.append(message)
    if omitted == 0:
        return request
    return ModelRequest(
        messages=repaired,
        tools=request.tools,
        model=request.model,
        runtime_context=dict(request.runtime_context),
    )
