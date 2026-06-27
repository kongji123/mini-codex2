from __future__ import annotations

import os

import pytest

from minicodex2.model.messages import ChatMessage, ModelRequest
from minicodex2.model.openai_compatible import OpenAICompatibleModelAdapter


def _real_model_config() -> tuple[str, str, str]:
    api_key = os.environ.get("MINICODEX2_TEST_API_KEY")
    base_url = os.environ.get("MINICODEX2_TEST_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("MINICODEX2_TEST_MODEL")
    if not api_key or not model:
        pytest.skip("real model smoke requires MINICODEX2_TEST_API_KEY and MINICODEX2_TEST_MODEL")
    return base_url, api_key, model


def test_real_model_chat_smoke() -> None:
    base_url, api_key, model = _real_model_config()
    adapter = OpenAICompatibleModelAdapter(base_url=base_url, api_key=api_key, model=model)
    response = adapter.complete(
        ModelRequest(
            messages=[ChatMessage(role="user", content="Reply with exactly: ok")],
            tools=[],
            model=model,
        )
    )
    assert response.message.content
