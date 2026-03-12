"""
tools/claude_client.py

Backward-compatibility shim.
All new code should use tools/llm_client.py directly.

    from tools.llm_client import build_llm_client
    client = build_llm_client(cfg["llm"])

This file is kept so any external code that still does:
    from tools.claude_client import ClaudeClient
continues to work unchanged — ClaudeClient is now just
an alias for AnthropicClient.
"""
from tools.llm_client import AnthropicClient as ClaudeClient  # noqa: F401

__all__ = ["ClaudeClient"]
