import tomllib
from pathlib import Path
from typing import Any, Dict


def load_routing_config(root_dir: str = ".") -> Dict[str, Any]:
    """Reads the [routing] table from .agentrc.toml.

    Supported keys, all optional:

        [routing]
        priority = ["claude", "agy", "codex"]  # CLI or provider try-order for auto mode

        [routing.models]   # override the default model used per CLI/tier
        [routing.models.agy]
        low = "gemini-3.6-flash-low"

        [routing.model_map]   # override the auto-mode model try-order per tier
        low = ["agy/gemini-3.6-flash-low", "anthropic/haiku"]

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
