"""Built-in MCP server giving the active backend CLI (claude/gemini/codex)
extra CodeHydra-native tools: fanning sub-tasks out to parallel agents
(mirroring Claude Code's Task tool) and running long-lived background
processes (mirroring Claude Code's background Bash + Monitor).

CodeHydra wires this server into every CLI invocation (see
Gateway._builtin_mcp_servers). To avoid unbounded recursive fan-out, the
sub-agents dispatch_agents spawns run with CODEHYDRA_ENABLE_SUBAGENTS=0, so
they don't get this server themselves.
"""
import json
import os
import signal
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from mcp.server.fastmcp import FastMCP
from src.routing.gateway import Gateway

mcp = FastMCP("codehydra-tools")

BG_DIR = Path(".codehydra") / "bg"


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


def _is_running(pid: int) -> bool:
    # Reap the process if it's our child and has exited, so it doesn't show
    # as "running" forever as a zombie for the rest of this server's life.
    try:
        if os.waitpid(pid, os.WNOHANG)[0] != 0:
            return False
    except ChildProcessError:
        pass

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@mcp.tool()
def run_in_background(command: str) -> dict:
    """Start a shell command running in the background and return a task id.

    Use this for long-running processes (dev servers, watchers, long builds)
    that should keep running after this turn ends. The process keeps running
    even after the current CLI invocation exits. Check on it with
    get_background_output and stop it with stop_background_task.

    Args:
        command: Shell command to run (executed via the system shell).

    Returns:
        {"task_id": ..., "pid": ...}
    """
    task_id = uuid.uuid4().hex[:8]
    task_dir = BG_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / "output.log"

    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    (task_dir / "meta.json").write_text(json.dumps({"command": command, "pid": proc.pid}))
    return {"task_id": task_id, "pid": proc.pid}


@mcp.tool()
def get_background_output(task_id: str, tail: int = 100) -> dict:
    """Get the status and recent output of a background task.

    Args:
        task_id: The id returned by run_in_background.
        tail: Number of trailing output lines to return.

    Returns:
        {"command": ..., "running": bool, "output": "..."}
    """
    task_dir = BG_DIR / task_id
    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        return {"error": f"Unknown task id: {task_id}"}

    meta = json.loads(meta_path.read_text())
    log_path = task_dir / "output.log"
    lines = log_path.read_text().splitlines()[-tail:] if log_path.exists() else []
    return {
        "command": meta["command"],
        "running": _is_running(meta["pid"]),
        "output": "\n".join(lines),
    }


@mcp.tool()
def list_background_tasks() -> List[dict]:
    """List all background tasks started in this workspace, with running status.

    Returns:
        List of {"task_id": ..., "command": ..., "running": bool}.
    """
    if not BG_DIR.exists():
        return []

    tasks = []
    for task_dir in sorted(BG_DIR.iterdir()):
        meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        tasks.append({
            "task_id": task_dir.name,
            "command": meta["command"],
            "running": _is_running(meta["pid"]),
        })
    return tasks


@mcp.tool()
def stop_background_task(task_id: str) -> dict:
    """Stop a running background task.

    Args:
        task_id: The id returned by run_in_background.

    Returns:
        {"status": "stopped"} or {"status": "already stopped"} or {"error": ...}
    """
    task_dir = BG_DIR / task_id
    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        return {"error": f"Unknown task id: {task_id}"}

    meta = json.loads(meta_path.read_text())
    pid = meta["pid"]
    if not _is_running(pid):
        return {"status": "already stopped"}

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError as e:
        return {"error": str(e)}
    return {"status": "stopped"}


if __name__ == "__main__":
    mcp.run()
