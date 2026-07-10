"""FinMerge — Trade statement processor and tax report generator.

A CodeHydra plugin that reads PDF trade statements from multiple brokers,
extracts buy/sell transactions, performs FIFO matching, and generates
tax-ready capital gains reports for Indian ITR filing (Indian + Foreign Assets).

Optionally uses a local Ollama model for intelligent parsing of ambiguous PDFs.
"""

from .mcp_tools import register_tools

__all__ = ["register_tools"]
