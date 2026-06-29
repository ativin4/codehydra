import json
import os
import urllib.request
from typing import Dict, Generator, List


def _base_url() -> str:
    # Read at call time so OLLAMA_HOST changes (e.g. /login ollama) take effect.
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


class OllamaProvider:
    """Ollama REST client (local or cloud via OLLAMA_HOST)."""

    @classmethod
    def is_available(cls) -> bool:
        try:
            with urllib.request.urlopen(f"{_base_url()}/api/tags", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    @classmethod
    def list_models(cls) -> List[str]:
        try:
            with urllib.request.urlopen(f"{_base_url()}/api/tags", timeout=2) as r:
                return [m["name"] for m in json.loads(r.read()).get("models", [])]
        except Exception:
            return []

    @classmethod
    def generate(cls, model: str, messages: List[Dict]) -> Generator[str, None, None]:
        body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
        req = urllib.request.Request(
            f"{_base_url()}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                content = data.get("message", {}).get("content", "")
                if content:
                    yield content
                if data.get("done"):
                    return
