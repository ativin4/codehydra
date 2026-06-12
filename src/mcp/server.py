"""Built-in MCP server giving the active backend CLI (claude/gemini/codex) a
tool to fan out independent sub-tasks to parallel CodeHydra-routed agents,
mirroring Claude Code's Task tool.

CodeHydra wires this server into every CLI invocation (see
Gateway._builtin_mcp_servers). To avoid unbounded recursive fan-out, the
sub-agents this spawns run with CODEHYDRA_ENABLE_SUBAGENTS=0, so they don't
get this tool themselves.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from typing import List

from mcp.server.fastmcp import FastMCP
from src.routing.gateway import Gateway

mcp = FastMCP("codehydra-agents")


@mcp.tool()
def dispatch_agents(tasks: List[str], tier: str = "medium") -> List[str]:
    """Run multiple independent tasks in parallel, each via its own CodeHydra-routed
    coding agent (claude/gemini/codex, picked automatically).

    Use this for independent sub-tasks that don't depend on each other's output
    (e.g. researching several files, writing several modules, running several
    independent checks). Each task gets its own fresh agent with no shared
    context - give each one a fully self-contained prompt.

    Args:
        tasks: List of self-contained task prompts to run concurrently.
        tier: Effort tier for the sub-agents ("low", "medium", or "high").

    Returns:
        One result string per task, in the same order as `tasks`.
    """
    os.environ["CODEHYDRA_ENABLE_SUBAGENTS"] = "0"

    def run(task: str) -> str:
        try:
            return Gateway().request(task, tier=tier)
        except Exception as e:
            return f"Error: {e}"

    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as executor:
        return list(executor.map(run, tasks))


if __name__ == "__main__":
    mcp.run()
