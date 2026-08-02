import json
import queue
import shutil
import subprocess
import threading
from typing import Generator, List, Optional, Tuple

# Sentinels injected into the stream so the TUI can render thinking and answer
# separately without changing the Generator[str] API.
THINKING_START = ""
THINKING_END = ""


class ClaudeSession:
    """A persistent `claude -p --input-format stream-json --output-format
    stream-json` process.

    Spawning `claude` pays a fixed ~1-1.5s startup cost (hooks, MCP config,
    etc.) on top of model latency. Keeping one process alive across turns
    means only the first turn in a conversation pays that cost - later turns
    just write a new user-message line to stdin and claude replies using its
    own session memory, so only the new prompt is sent (not the full history).
    """

    def __init__(self, model: str, mode_flags: List[str], system_prompt: str = "",
                 mcp_config_path: Optional[str] = None, effort: Optional[str] = None):
        cli_path = shutil.which("claude")
        if not cli_path:
            raise Exception("'claude' CLI not found on PATH.")

        cmd = [cli_path, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose", "--model", model] + mode_flags
        if effort:
            cmd += ["--effort", effort]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]
        if mcp_config_path:
            cmd += ["--mcp-config", str(mcp_config_path)]

        self.model = model
        self.mode_flags = mode_flags
        self.system_prompt = system_prompt
        self.effort = effort
        self.last_result_text = ""
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _drain_stderr(self) -> None:
        for line in self._proc.stderr:
            self._stderr_lines.append(line)

    def _get_stderr(self) -> str:
        return "".join(self._stderr_lines[-50:]).strip()

    def matches(self, model: str, mode_flags: List[str], system_prompt: str = "",
                effort: Optional[str] = None) -> bool:
        return (self.model == model and self.mode_flags == mode_flags
                and self.system_prompt == system_prompt and self.effort == effort)

    def alive(self) -> bool:
        return self._proc.poll() is None

    def send(self, prompt: str, cancel_event=None) -> Generator[Tuple[Optional[str], Optional[int]], None, None]:
        """Sends one user turn and yields (text_chunk, total_tokens) until the
        turn's "result" event arrives. Raises if the process has exited.

        cancel_event: a threading.Event; when set, the generator raises
        CancelledError so the caller can kill the session and show a message.
        """
        if not self.alive():
            raise Exception(f"claude session is no longer running: {self._get_stderr()}")

        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        self.last_result_text = ""

        thinking_open = False
        while True:
            # Poll with a short timeout so cancel_event is checked frequently
            # even when Claude is silently executing a long MCP tool call.
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("cancelled")
                try:
                    line = self._lines.get(timeout=0.1)
                    break
                except queue.Empty:
                    if not self.alive():
                        raise Exception(f"claude session ended unexpectedly: {self._get_stderr()}")
                    continue
            if line is None:
                raise Exception(f"claude session ended unexpectedly: {self._get_stderr()}")
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "stream_event":
                delta = data.get("event", {}).get("delta", {})
                # Yield thinking deltas so the user sees real-time reasoning.
                # Extended-thinking models emit many thinking_delta events then
                # a single text_delta — without streaming thinking, the UI
                # appears frozen. THINKING_START/END sentinels mark the boundary.
                if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    if not thinking_open:
                        thinking_open = True
                        yield THINKING_START, None
                    yield delta["thinking"], None
                elif delta.get("type") == "text_delta" and delta.get("text"):
                    if thinking_open:
                        thinking_open = False
                        yield THINKING_END, None
                    yield delta["text"], None
            elif data.get("type") == "result":
                result_text = data.get("result")
                if isinstance(result_text, str):
                    self.last_result_text = result_text.strip()
                usage = data.get("usage") or {}
                total = sum(v for v in usage.values() if isinstance(v, int))
                yield None, total or None
                return

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        if self.alive():
            self._proc.terminate()
