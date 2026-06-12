import tomllib
from pathlib import Path
from typing import Any, Dict


def load_routing_config(root_dir: str = ".") -> Dict[str, Any]:
    """Reads the [routing] table from .agentrc.toml.

    Supported keys, all optional:

        [routing]
        priority = ["anthropic", "google", "github"]  # CLI try-order for auto mode

        [routing.models]   # override the default model used per CLI/tier
        [routing.models.gemini]
        low = "gemini-flash-lite-latest"

        [routing.model_map]   # override the auto-mode model try-order per tier
        low = ["gemini/gemini-flash-lite-latest", "anthropic/haiku"]

    Returns {} if no config file or no [routing] table is present.
    """
    config_path = Path(root_dir).resolve() / ".agentrc.toml"
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except Exception:
        return {}

    routing = config.get("routing", {})
    return routing if isinstance(routing, dict) else {}
