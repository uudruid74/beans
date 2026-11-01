# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE
# END PROVENANCE
"""Start llama.cpp's llama-server on demand, locally or on a remote host over ssh, and stop it when idle.

A server this tool did not start is reused but never stopped.
"""
from .client import ChatResult, ClientError, ContextTooLarge, LlamaClient
from .manager import LlamaManager, LlamaRefused, LlamaUnavailable

__all__ = ["ChatResult", "ClientError", "ContextTooLarge", "LlamaClient", "LlamaManager", "LlamaRefused",
           "LlamaUnavailable"]
__version__ = "0.1.0"
