"""Deterministic canned-playback gateway for demo recordings and manual QA.

Activated when CODEHYDRA_DEMO_SCRIPT points at a JSON script file:

    {
      "turns": [
        {
          "response": "markdown answer streamed word by word",
          "cli": "claude",
          "model": "anthropic/claude-sonnet-4-5",
          "tier": "medium",
          "tokens": 1234,
          "thinking": "optional reasoning text streamed first",
          "write_file": {"path": "fib.py", "content": "..."},
          "fail_midway": {
            "after_chars": 120,
            "notice": "claude failed mid-response",
            "cli": "codex",
            "model": "codex/default"
          }
        }
      ]
    }

Each request_stream() call consumes the next turn. `write_file` creates the
file mid-turn so the TUI's git diff view fires without a real CLI editing
anything. `fail_midway` streams a partial answer, emits the fallback notice
and STREAM_RESET, then streams the full answer as the fallback CLI —
showcasing mid-stream fallback recovery deterministically.
"""

import json
import time
from pathlib import Path
from typing import Dict, Generator, List, Optional

from codehydra.routing.claude_session import THINKING_END, THINKING_START
from codehydra.routing.gateway import STREAM_RESET


class DemoGateway:
    CHUNK_DELAY = 0.03
    THINKING_DELAY = 0.05

    def __init__(self, script_path: Path):
        data = json.loads(Path(script_path).read_text())
        self.turns: List[Dict] = list(data.get("turns", []))
        self._turn_idx = 0
        self.cli_auth_status = {"claude": True, "agy": True, "codex": True, "ollama": False}
        self.last_usage: Optional[Dict] = None
        self.on_fallback = None
        self._cancel_event = None

    # ── interface parity with Gateway ────────────────────────────────
    def rate_limit_resets_in(self, cli_name: str) -> Optional[int]:
        return None

    def reset_session(self) -> None:
        pass

    def refresh_auth(self) -> None:
        pass

    def close(self, stop_mcp_server: bool = False) -> None:
        pass

    # ── playback ──────────────────────────────────────────────────────
    def _next_turn(self) -> Dict:
        if self._turn_idx < len(self.turns):
            turn = self.turns[self._turn_idx]
            self._turn_idx += 1
            return turn
        return {"response": "*(demo script exhausted)*", "cli": "claude",
                "model": "anthropic/claude-sonnet-4-5", "tier": "low"}

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _stream_text(self, text: str, delay: float) -> Generator[str, None, None]:
        for word in text.split(" "):
            if self._cancelled():
                return
            yield word + " "
            time.sleep(delay)

    def _set_usage(self, turn: Dict, cli: str, model: str) -> None:
        self.last_usage = {
            "cli": cli,
            "model": model,
            "tier": turn.get("tier", "medium"),
            "tokens": turn.get("tokens"),
        }

    def request_stream(self, prompt: str, **kwargs) -> Generator[str, None, None]:
        turn = self._next_turn()
        response = turn.get("response", "")
        cli = turn.get("cli", "claude")
        model = turn.get("model", "anthropic/claude-sonnet-4-5")

        thinking = turn.get("thinking")
        if thinking:
            yield THINKING_START
            yield from self._stream_text(thinking, self.THINKING_DELAY)
            yield THINKING_END

        wf = turn.get("write_file")
        if wf:
            path = Path.cwd() / wf["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wf["content"])

        fail = turn.get("fail_midway")
        if fail:
            partial = response[: fail.get("after_chars", 120)]
            yield from self._stream_text(partial, self.CHUNK_DELAY)
            if self.on_fallback:
                self.on_fallback(f"↩ {fail.get('notice', 'claude failed mid-response')} — trying next…")
            time.sleep(1.0)
            yield STREAM_RESET
            cli = fail.get("cli", "codex")
            model = fail.get("model", "codex/default")
            time.sleep(0.8)

        yield from self._stream_text(response, self.CHUNK_DELAY)
        self._set_usage(turn, cli, model)

    def request(self, prompt: str, **kwargs) -> str:
        turn = self._next_turn()
        cli = turn.get("cli", "claude")
        model = turn.get("model", "anthropic/claude-sonnet-4-5")
        self._set_usage(turn, cli, model)
        return turn.get("response", "")
