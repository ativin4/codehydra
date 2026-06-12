import tomllib
from pathlib import Path
from typing import Any, Dict


def load_mcp_servers(root_dir: str = ".") -> Dict[str, Dict[str, Any]]:
    """Reads [mcp.servers.<name>] tables from .agentrc.toml.

    Each server entry looks like:

        [mcp.servers.fetch]
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-fetch"]
        # env = { API_KEY = "..." }

    Returns {} if no config file or no servers are declared.
    """
    config_path = Path(root_dir).resolve() / ".agentrc.toml"
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception:
        return {}

    servers = config.get("mcp", {}).get("servers", {})
    return servers if isinstance(servers, dict) else {}
