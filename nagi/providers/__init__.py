"""Model provider adapters."""

from .clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OllamaModelClient,
    OpenAIChatCompatibleModelClient,
    OpenAICompatibleModelClient,
)

__all__ = [
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
    "OllamaModelClient",
    "OpenAIChatCompatibleModelClient",
    "OpenAICompatibleModelClient",
]
