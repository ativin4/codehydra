import socket
import subprocess
import shutil
import os
import re
import json
import sys
import threading
import time
from pathlib import Path
from typing import Generator, List, Dict, Optional
from codehydra.auth.scavenger import Scavenger
from codehydra.routing.classifier import Classifier
from codehydra.routing.config import load_routing_config
from codehydra.routing.claude_session import ClaudeSession
from codehydra.routing.oss_provider import OllamaProvider
from codehydra.mcp.config import load_mcp_servers
from codehydra.routing.constants import CLI, Mode, Role, Tier
from codehydra.tools.media import IMAGE_EXTS, PDF_EXTS, VIDEO_EXTS, image_to_base64, pdf_to_text


# Shared persistent MCP HTTP server (started once per process, reused by all
# Gateway instances including parallel/compare sub-gateways).
_SHARED_MCP: Dict = {}
_SHARED_MCP_LOCK = threading.Lock()

# Shared rate-limit state so parallel Gateway instances respect each other's
# rate limits and don't pile requests onto a CLI that's already throttled.
_SHARED_RATE_LIMITED: Dict[str, float] = {}
_SHARED_RATE_LOCK = threading.Lock()


class RateLimitError(Exception):
    """Raised when a backend reports quota/rate-limit exhaustion."""


class Gateway:
    # Default auto-mode model try-order per tier. claude/codex use rolling
    # aliases ("sonnet"/"haiku"/"opus", "default") that auto-resolve to the
    # latest model. Gemini's CLI has no such aliases (its "-latest" names
    # 404) so its entries are pinned to specific model versions and need
    # updating as new generations ship — override via [routing.model_map]
    # in .agentrc.toml instead of editing this code.
    MODEL_MAP = {
        Tier.LOW:    ["anthropic/haiku",  "codex/default", "agy/agy-2.5-flash", "ollama/llama3.2"],
        Tier.MEDIUM: ["anthropic/sonnet", "codex/default", "agy/agy-2.5-pro",   "ollama/llama3.2"],
        Tier.HIGH:   ["anthropic/opus",   "codex/default", "agy/agy-2.5-pro",   "ollama/llama3.3"],
    }

    # Default model used for each CLI/tier when /cli pins a backend without
    # /model. Override via [routing.models.<cli>] in .agentrc.toml.
    CLI_DEFAULT_MODELS = {
        CLI.CLAUDE: {Tier.LOW: "haiku",            Tier.MEDIUM: "sonnet",          Tier.HIGH: "opus"},
        CLI.AGY:    {Tier.LOW: "agy-2.5-flash",    Tier.MEDIUM: "agy-2.5-pro",     Tier.HIGH: "agy-2.5-pro"},
        CLI.CODEX:  {Tier.LOW: "default",          Tier.MEDIUM: "default",         Tier.HIGH: "default"},
        CLI.OLLAMA: {Tier.LOW: "llama3.2",         Tier.MEDIUM: "llama3.2",        Tier.HIGH: "llama3.3"},
    }

    # Maps a model's "<provider>/" prefix to the scavenger provider name used
    # for subscription detection and [routing] priority ordering.
    PROVIDER_MAP = {
        "anthropic": "anthropic",
        "agy":       "google",
        "github":    "github",
        "codex":     "github",
        "ollama":    "ollama",
    }

    # Flags that put each CLI in non-interactive mode, per session /mode.
    # Mode.YOLO: auto-approve edits and commands (default; required for headless
    # operation since CLIs hang on permission prompts otherwise).
    # Mode.PLAN: read-only - the CLI can look around but can't edit files or run
    # commands. Useful for "what would you do" without touching the workspace.
    MODE_FLAGS = {
        Mode.YOLO: {
            CLI.CLAUDE: ["--permission-mode", "bypassPermissions"],
            # --dangerously-skip-permissions: auto-approve all tool calls headless.
            CLI.AGY:    ["--dangerously-skip-permissions"],
            # --dangerously-bypass-approvals-and-sandbox: headless full-auto.
            # For exec-subcommand flags these must be inserted AFTER "exec"
            # (see _build_cmd insert_at logic for codex).
            CLI.CODEX:  ["--dangerously-bypass-approvals-and-sandbox"],
        },
        Mode.PLAN: {
            CLI.CLAUDE: ["--permission-mode", "plan"],
            # print mode is inherently non-destructive; no extra flags needed.
            CLI.AGY:    [],
            CLI.CODEX:  ["-s", "read-only"],
        },
    }

    # Max non-system messages passed to one-shot CLIs (agy/codex/ollama).
    # Claude manages its own context via persistent session.
    _CONTEXT_WINDOW = 30

    # Regexes for extracting token-usage info each CLI prints (best-effort,
    # only some CLIs report this).
    USAGE_PATTERNS = {
        CLI.CODEX: re.compile(r"tokens used\s*\n\s*([\d,]+)", re.IGNORECASE),
    }

    # Commands to launch each CLI's interactive auth flow.
    LOGIN_COMMANDS = {
        CLI.CLAUDE: [CLI.CLAUDE, "auth", "login"],
        CLI.CODEX:  [CLI.CODEX, "login"],
        CLI.AGY:    [CLI.AGY],  # first interactive launch walks through OAuth
    }

    def __init__(self):
        self.scavenger = Scavenger()
        self.scavenger.apply_to_env()
        self.headers = self.scavenger.get_all_headers()
        self.active_providers = self.scavenger.get_active_providers()
        self.cli_auth_status = self.scavenger.get_cli_auth_status()
        self.cli_auth_status[CLI.OLLAMA] = OllamaProvider.is_available()
        self._claude_session: Optional["ClaudeSession"] = None
        self._claude_history_len = 0
        self.classifier = Classifier()
        self.last_usage: Optional[Dict] = None

        # Start the shared HTTP MCP server (once per process) so all CLIs
        # connect to the same long-running server rather than spawning a new
        # subprocess per invocation — eliminates per-turn MCP startup cost and
        # trust prompts.
        self._mcp_port: Optional[int] = None
        if os.environ.get("CODEHYDRA_ENABLE_SUBAGENTS") != "0":
            self._mcp_port = self._ensure_http_mcp_server()

        self.mcp_servers = {**self._builtin_mcp_config(), **load_mcp_servers()}
        self.claude_mcp_config_path = self._write_mcp_configs()

        routing_cfg = load_routing_config()
        self.priority: Optional[List[str]] = routing_cfg.get("priority")

        self.model_map = dict(self.MODEL_MAP)
        for tier, models in routing_cfg.get("model_map", {}).items():
            if tier in self.model_map and isinstance(models, list):
                self.model_map[tier] = models

        self.cli_default_models = {cli: dict(tiers) for cli, tiers in self.CLI_DEFAULT_MODELS.items()}
        for cli, tiers in routing_cfg.get("models", {}).items():
            if cli in self.cli_default_models and isinstance(tiers, dict):
                self.cli_default_models[cli].update(tiers)

    @classmethod
    def _ensure_http_mcp_server(cls) -> Optional[int]:
        """Start the codehydra-tools HTTP MCP server if not already running.
        Returns the port number, or None if startup failed."""
        global _SHARED_MCP
        with _SHARED_MCP_LOCK:
            proc = _SHARED_MCP.get("proc")
            port = _SHARED_MCP.get("port")
            if proc and proc.poll() is None and port:
                return port  # already running

            # Bind to get a free port, then release so the server can claim it.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

            proc = subprocess.Popen(
                [sys.executable, "-m", "codehydra.mcp.server",
                 "--transport", "streamable-http", "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait up to 5 s for the server to accept connections.
            deadline = time.time() + 5.0
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break  # server crashed
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.1)

            if not ready:
                proc.kill()
                return None

            _SHARED_MCP["proc"] = proc
            _SHARED_MCP["port"] = port
            return port

    @classmethod
    def close_shared_mcp_server(cls) -> None:
        """Kill the shared HTTP MCP server. Called on app exit."""
        global _SHARED_MCP
        with _SHARED_MCP_LOCK:
            proc = _SHARED_MCP.get("proc")
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()  # reap to avoid zombie
            _SHARED_MCP.clear()

    def _builtin_mcp_config(self) -> Dict[str, Dict]:
        """Returns the codehydra-tools MCP server config for use in mcp_servers.
        Uses HTTP URL when the persistent server is running (preferred — no
        per-invocation startup cost or trust prompts), falls back to stdio."""
        if self._mcp_port:
            return {
                "codehydra-tools": {
                    "url": f"http://127.0.0.1:{self._mcp_port}/mcp"
                }
            }
        # Fallback: stdio (subagents or server startup failure)
        if os.environ.get("CODEHYDRA_ENABLE_SUBAGENTS") == "0":
            return {}
        return {
            "codehydra-tools": {
                "command": sys.executable,
                "args": ["-m", "codehydra.mcp.server"],
            }
        }

    def _write_mcp_configs(self) -> Optional[Path]:
        """Materializes MCP server definitions into the config files/flags
        each backend CLI expects.

        - claude: written to .codehydra/mcp-config.json, passed via
          --mcp-config at invocation time.
        - agy: merged into the project's .agy/settings.json under
          "mcpServers" (the only way agy CLI picks up MCP servers).
        - codex: no file needed; servers are passed per-invocation as
          `-c mcp_servers.<name>.*` overrides (see _build_cmd).
        """
        claude_config_path = Path(".codehydra") / "mcp-config.json"
        if self.mcp_servers:
            self._write_json_if_changed(claude_config_path, {"mcpServers": self.mcp_servers})

        gemini_settings_path = Path(".agy") / "settings.json"
        settings: Dict = {}
        if gemini_settings_path.exists():
            try:
                with open(gemini_settings_path, "r") as f:
                    loaded = json.load(f)
                    settings = loaded if isinstance(loaded, dict) else {}
            except Exception:
                settings = {}

        # Sanitise null values that could crash .update()/.items() calls below.
        if not isinstance(settings.get("mcpServers"), dict):
            settings.pop("mcpServers", None)
        if not isinstance(settings.get("general"), dict):
            settings.pop("general", None)
        if not isinstance(settings.get("ui"), dict):
            settings.pop("ui", None)

        # When codehydra-tools runs as a persistent HTTP server give agy the URL
        # directly — no subprocess spawn, no startup latency. Stdio entries are
        # excluded to avoid the 3-5s spawn cost on every invocation.
        http_mcp = {k: v for k, v in self.mcp_servers.items() if "url" in v}
        # Preserve any user-defined MCP servers that aren't codehydra-managed.
        existing = settings.get("mcpServers") or {}
        user_mcp = {k: v for k, v in existing.items() if k not in self.mcp_servers}
        combined_mcp = {**http_mcp, **user_mcp}
        if combined_mcp:
            settings["mcpServers"] = combined_mcp
        else:
            settings.pop("mcpServers", None)

        # Disable blocking startup checks.
        settings.setdefault("general", {}).update({
            "enableAutoUpdate": False,
            "enableAutoUpdateNotification": False,
        })
        settings.setdefault("ui", {})["renderProcess"] = False
        self._write_json_if_changed(gemini_settings_path, settings)

        return claude_config_path if self.mcp_servers else None

    @staticmethod
    def _write_json_if_changed(path: Path, data: Dict) -> None:
        if path.exists():
            try:
                with open(path, "r") as f:
                    if json.load(f) == data:
                        return
            except Exception:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def _parse_usage(cls, cli_name: str, err_output: str) -> Optional[int]:
        pattern = cls.USAGE_PATTERNS.get(cli_name)
        if not pattern:
            return None
        match = pattern.search(err_output)
        if not match:
            return None
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None

    def _record_usage(self, cli_name: str, model: str, err_output: str, tier: Optional[str], tokens: Optional[int] = None) -> None:
        self.last_usage = {
            "cli": cli_name,
            "model": model,
            "tokens": tokens if tokens is not None else self._parse_usage(cli_name, err_output),
            "tier": tier,
        }

    @staticmethod
    def _parse_agy_stream_line(line: str) -> tuple:
        """Returns (text_chunk, total_tokens) for an agy plain-text output line.
        agy 1.x outputs plain text (no --output-format flag); each line is a
        content chunk. Token counts are not reported by this CLI version."""
        return line, None

    def _build_cmd(self, cli_name: str, messages: List[Dict[str, str]], model: str, mode: str = "yolo", stream: bool = False, media_files: List[Path] = []):
        """Builds the subprocess argv + env for invoking a CLI with a prompt."""
        cli_path = shutil.which(cli_name)
        if not cli_path:
            raise Exception(f"'{cli_name}' CLI not found on PATH. Install it and ensure it's accessible.")

        # Claude has a persistent session that tracks its own context; one-shot
        # CLIs get the full history concatenated as a string, so cap it to avoid
        # unbounded prompt growth across long conversations.
        if cli_name != CLI.CLAUDE:
            system = [m for m in messages if m["role"] == Role.SYSTEM]
            turns = [m for m in messages if m["role"] != Role.SYSTEM]
            if len(turns) > self._CONTEXT_WINDOW:
                turns = turns[-self._CONTEXT_WINDOW:]
            messages = system + turns

        # For non-agy CLIs, inject media content as text before assembling
        # the prompt. Agy handles @path refs natively so we leave those for
        # the prompt string below.
        if media_files and cli_name != CLI.AGY:
            extra = []
            for p in media_files:
                ext = p.suffix.lower()
                if ext in PDF_EXTS:
                    text = pdf_to_text(p)
                    if text:
                        extra.append(f"[PDF: {p.name}]\n{text}")
                    else:
                        extra.append(f"[PDF: {p.name} — install pdftotext (poppler) to extract text]")
                elif ext in IMAGE_EXTS:
                    extra.append(f"[Image attached: {p.name} — image rendering not supported for {cli_name}]")
                elif ext in VIDEO_EXTS:
                    extra.append(f"[Video attached: {p.name} — video not supported for {cli_name}]")
            if extra:
                # Prepend to last user message content
                messages = list(messages)
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == Role.USER:
                        messages[i] = dict(messages[i])
                        messages[i]["content"] = "\n\n".join(extra) + "\n\n" + messages[i]["content"]
                        break

        # Build the prompt string from the message list.
        # - System messages: extracted separately for Claude (--system-prompt),
        #   or prepended as context for other CLIs.
        # - Conversation turns: formatted as Human/Assistant to make the history
        #   legible to the underlying LLM without ambiguity about message roles.
        # - The last user message is separated by "---" so the CLI can clearly
        #   distinguish history from the current request.
        claude_system_prompt = ""
        history_parts: list[str] = []
        current_user_content = ""

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            is_last = (i == len(messages) - 1)

            if role == Role.SYSTEM:
                if cli_name == CLI.CLAUDE:
                    claude_system_prompt = content
                else:
                    history_parts.append(f"<system>\n{content}\n</system>\n\n")
                continue

            if is_last and role == Role.USER:
                current_user_content = content
            else:
                label = "Human" if role == Role.USER else "Assistant"
                history_parts.append(f"{label}: {content}\n\n")

        if history_parts:
            full_prompt = "".join(history_parts) + "---\n\nHuman: " + current_user_content
        else:
            full_prompt = current_user_content

        model_name = model.split("/")[-1]

        if cli_name == CLI.AGY:
            # Prepend @path refs so agy resolves them natively (multimodal).
            if media_files:
                refs = " ".join(f"@{p.absolute()}" for p in media_files)
                full_prompt = refs + "\n\n" + full_prompt
            # agy 1.x: use --print (alias --prompt) for non-interactive mode.
            # No --output-format flag exists in agy 1.x; plain text is default.
            cmd = [cli_path, "--print", full_prompt, "--model", model_name]
        elif cli_name == CLI.CODEX:
            # codex exec: model defaults to account default; don't pass -m with
            # "default" since it's not a valid -m value.
            cmd = [cli_path, "exec", full_prompt]
        elif cli_name == CLI.CLAUDE:
            cmd = [cli_path, "-p", full_prompt, "--model", model_name]
            if claude_system_prompt:
                cmd += ["--system-prompt", claude_system_prompt]
        else:
            cmd = [cli_path, full_prompt]

        # Insert mode flags.
        # For codex, mode flags (-s / --dangerously-bypass-*) are exec-subcommand
        # flags and must come AFTER the "exec" argument (position 2).
        # All other CLIs take top-level flags (position 1).
        insert_at = 2 if cli_name == CLI.CODEX else 1
        mode_flags = self.MODE_FLAGS.get(mode, self.MODE_FLAGS[Mode.YOLO]).get(cli_name, [])
        cmd[insert_at:insert_at] = mode_flags

        # Wire up MCP servers.
        if self.mcp_servers:
            if cli_name == CLI.CLAUDE and self.claude_mcp_config_path:
                cmd += ["--mcp-config", str(self.claude_mcp_config_path)]
            elif cli_name == CLI.CODEX:
                mcp_flags = []
                for name, server in self.mcp_servers.items():
                    for key, value in server.items():
                        mcp_flags += ["-c", f"mcp_servers.{name}.{key}={json.dumps(value)}"]
                # Insert after mode flags, before the prompt positional arg.
                flag_pos = insert_at + len(mode_flags)
                cmd[flag_pos:flag_pos] = mcp_flags
            # agy reads mcpServers from .agy/settings.json automatically.

        # Prepare a clean environment for the subprocess.
        # Strip GOOGLE_CLOUD_PROJECT: agy picks it up and routes to the wrong
        # Vertex project instead of using the OAuth session credentials.
        env = os.environ.copy()
        env.pop("GOOGLE_CLOUD_PROJECT", None)
        if cli_name == CLI.AGY:
            # Bypass folder trust prompt in headless mode (replaces --skip-trust flag).
            env["AGY_CLI_TRUST_WORKSPACE"] = "true"
            env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

        return cmd, env

    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07|\x1b[@-Z\\-_]")

    # Patterns that indicate a subscription rate-limit (not a bug/auth failure).
    _RATE_LIMIT_RE = re.compile(
        r"rate.?limit|too many requests|quota exceeded|429|overloaded|"
        r"capacity|try again|please wait|usage limit|session limit|"
        r"hit your .*limit|limit resets?|resets \d",
        re.IGNORECASE,
    )
    # Default cooldown when no retry-after header is available.
    _RATE_LIMIT_COOLDOWN = 60  # seconds

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        return cls._ANSI_RE.sub("", text)

    @classmethod
    def _is_rate_limit_text(cls, text: str) -> bool:
        return bool(text and cls._RATE_LIMIT_RE.search(cls._strip_ansi(text)))

    @classmethod
    def _raise_if_rate_limited_output(cls, cli_name: str, text: str) -> None:
        if cls._is_rate_limit_text(text):
            raise RateLimitError(f"{cli_name} CLI rate limited: {text.strip()}")

    def _mark_rate_limited(self, cli_name: str, seconds: int = _RATE_LIMIT_COOLDOWN) -> None:
        with _SHARED_RATE_LOCK:
            _SHARED_RATE_LIMITED[cli_name] = time.time() + seconds

    def is_rate_limited(self, cli_name: str) -> bool:
        with _SHARED_RATE_LOCK:
            return time.time() < _SHARED_RATE_LIMITED.get(cli_name, 0)

    def rate_limit_resets_in(self, cli_name: str) -> Optional[int]:
        """Seconds until cooldown expires, or None if not rate-limited."""
        with _SHARED_RATE_LOCK:
            remaining = _SHARED_RATE_LIMITED.get(cli_name, 0) - time.time()
        return int(remaining) + 1 if remaining > 0 else None

    _NOISE_PREFIXES = (
        "[ExtensionManager]", "[MCP]", "[mcp]", "Connecting to MCP",
        "Connected to MCP", "MCP server", "MCP issues", "Starting MCP",
        "Proxy server listening", "Starting MCP proxy", "mcp-remote",
        "Loaded tokens", "Token stored", "Opening browser",
        "Authorization URL", "Waiting for OAuth", "OAuth callback",
        "Loading extension", "Extension loaded",
    )
    _NOISE_SUBSTRINGS = (
        "MCP issues detected", "mcp-remote", "growwmcp", "chrome-devtools",
        "mcpServer", "mcp_server",
    )

    @classmethod
    def _is_noise_line(cls, line: str) -> bool:
        """Filter diagnostic / MCP connection chatter CLIs print to stdout."""
        for pfx in cls._NOISE_PREFIXES:
            if line.startswith(pfx):
                return True
        for sub in cls._NOISE_SUBSTRINGS:
            if sub in line:
                return True
        return False

    # How long to wait for a CLI before treating it as hung (e.g. waiting for
    # an OAuth prompt it will never get in headless mode). 5 minutes is generous
    # for long tasks; a CLI that needs auth will typically hang immediately.
    _CLI_TIMEOUT = 300  # seconds

    def _run_cli(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None, mode: str = "yolo", media_files: List[Path] = []) -> str:
        """Executes a local CLI (agy, codex, claude) and returns its full output."""
        cmd, env = self._build_cmd(cli_name, messages, model, mode, media_files=media_files)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                    stdin=subprocess.DEVNULL, timeout=self._CLI_TIMEOUT)

            output = result.stdout.strip()
            err_output = result.stderr.strip()

            if result.returncode != 0 and not output:
                raise Exception(f"{cli_name} CLI failed: {err_output}")

            output = self._strip_ansi(output)
            cleaned_lines = [line for line in output.split("\n") if not self._is_noise_line(line)]
            final_output = "\n".join(cleaned_lines).strip()
            if not final_output:
                raise Exception(f"{cli_name} CLI returned no usable output. STDOUT: {output} STDERR: {err_output}")
            self._raise_if_rate_limited_output(cli_name, final_output)
            self._record_usage(cli_name, model, err_output, tier)
            return final_output
        except subprocess.TimeoutExpired:
            raise Exception(f"{cli_name} CLI timed out ({self._CLI_TIMEOUT}s) — likely waiting for auth. Run /login {cli_name}.")
        except RateLimitError:
            raise
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _run_claude_persistent_stream(self, messages: List[Dict[str, str]], model: str, tier: Optional[str], mode: str) -> Generator[str, None, None]:
        """Sends one turn through a persistent claude session (claude_session.py),
        starting/restarting it if the conversation doesn't match what it has
        already seen (new conversation, /resume, or /mode//model change)."""
        model_name = model.split("/")[-1]
        mode_flags = self.MODE_FLAGS.get(mode, self.MODE_FLAGS[Mode.YOLO])["claude"]
        system_prompt = next((m["content"] for m in messages if m["role"] == Role.SYSTEM), "")
        non_system = [m for m in messages if m["role"] != Role.SYSTEM]
        if not non_system:
            raise Exception("claude CLI execution error: no user message to send")

        # Track completed turn-pairs (user+assistant) so we know whether to
        # continue the existing session or start a new one.
        # messages[-1] is always the new user turn; everything before it is
        # prior history: an even number of (user, assistant) pairs.
        complete_pairs = (len(non_system) - 1) // 2

        is_continuation = (
            self._claude_session is not None
            and self._claude_session.alive()
            and self._claude_session.matches(model_name, mode_flags, system_prompt)
            and complete_pairs == self._claude_history_len
        )

        current_prompt = non_system[-1]["content"]

        if not is_continuation:
            if self._claude_session is not None:
                self._claude_session.close()
            self._claude_session = ClaudeSession(
                model_name, mode_flags, system_prompt=system_prompt,
                mcp_config_path=self.claude_mcp_config_path,
            )
            self._claude_history_len = 0
            # Replay prior turns as context so Claude isn't starting blind after
            # a compact, /resume, or system-prompt change.
            if complete_pairs > 0:
                prior = non_system[:-1]
                history_text = "\n\n".join(
                    f"{'Human' if m['role'] == Role.USER else 'Assistant'}: {m['content']}"
                    for m in prior
                )
                current_prompt = (
                    f"[Conversation history for context — do not repeat or summarize it, "
                    f"just use it to answer the request below]\n\n"
                    f"{history_text}\n\n"
                    f"---\n\n{current_prompt}"
                )

        tokens = None
        yielded_any = False
        try:
            from codehydra.routing.claude_session import THINKING_START, THINKING_END
            sentinels = {THINKING_START, THINKING_END}
            for chunk, line_tokens in self._claude_session.send(current_prompt):
                if line_tokens is not None:
                    tokens = line_tokens
                if chunk:
                    if chunk not in sentinels:
                        yielded_any = True
                    yield chunk
        except GeneratorExit:
            # User cancelled mid-stream. Kill and nullify the session so the
            # next turn starts fresh — avoids reading leftover chunks from the
            # abandoned response off the queue.
            self._claude_session.close()
            self._claude_session = None
            self._claude_history_len = 0
            raise
        except Exception as e:
            self._claude_session.close()
            self._claude_session = None
            self._claude_history_len = 0
            raise Exception(f"claude CLI execution error: {e}")

        self._claude_history_len = complete_pairs + 1
        if not yielded_any:
            result_text = getattr(self._claude_session, "last_result_text", "")
            self._raise_if_rate_limited_output(CLI.CLAUDE, result_text)
            detail = f": {result_text}" if result_text else "."
            raise Exception(f"claude CLI returned no usable output{detail}")
        self._record_usage(CLI.CLAUDE, model, "", tier, tokens=tokens)

    def refresh_auth(self) -> None:
        """Re-reads all credentials after a login flow completes."""
        self.active_providers = self.scavenger.get_active_providers()
        self.cli_auth_status = self.scavenger.get_cli_auth_status()
        self.cli_auth_status[CLI.OLLAMA] = OllamaProvider.is_available()
        self.headers = self.scavenger.get_all_headers()

    def _resolve_cli_and_model(
        self, cli_override: Optional[str], model_override: Optional[str], tier: str
    ) -> Optional[tuple]:
        """Returns (cli_name, full_model_str) when cli_override is set, else None."""
        if not cli_override or cli_override == "auto":
            return None
        if cli_override not in self.cli_default_models:
            raise Exception(f"Unknown CLI override: {cli_override}")
        model_name = model_override or self.cli_default_models[cli_override][tier]
        return cli_override, f"{cli_override}/{model_name}"

    def _fallback_models_after_override(self, failed_cli: str, tier: str) -> List[str]:
        """Auto-mode candidates excluding the backend that just hit quota."""
        models = self._prioritize_models(self.model_map.get(tier, self.model_map[Tier.MEDIUM]))
        return [model for model in models if self._cli_for_model(model) != failed_cli]

    def close(self, stop_mcp_server: bool = False) -> None:
        """Releases persistent CLI sessions. Call on app exit.
        Pass stop_mcp_server=True from the main app instance only."""
        if self._claude_session is not None:
            self._claude_session.close()
            self._claude_session = None
        if stop_mcp_server:
            self.close_shared_mcp_server()

    def _run_cli_stream(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None, mode: str = "yolo", media_files: List[Path] = []) -> Generator[str, None, None]:
        """Executes a local CLI and yields its output incrementally.

        claude/agy use stream-json so chunks are real token deltas as the
        model generates them; codex has no equivalent and yields its output
        (still effectively one chunk near the end) line by line.
        """
        if cli_name == CLI.CLAUDE:
            yield from self._run_claude_persistent_stream(messages, model, tier, mode)
            return

        # agy 1.x outputs plain text; the stream=True flag no longer switches
        # to stream-json (that flag was removed in agy 1.x).
        cmd, env = self._build_cmd(cli_name, messages, model, mode, stream=False, media_files=media_files)
        # Both agy and codex output plain text line-by-line via PTY.
        line_parser = self._parse_agy_stream_line if cli_name == CLI.AGY else None

        try:
            import pty
            import selectors
            
            master_fd, slave_fd = pty.openpty()
            
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True
            )
            os.close(slave_fd)
            
            sel = selectors.DefaultSelector()
            sel.register(master_fd, selectors.EVENT_READ)

            yielded_any = False
            tokens = None
            buffer = b""
            last_output_time = time.time()

            try:
                # Keep reading as long as the process is alive OR there is data to read
                while True:
                    # select with a timeout so we can check process.poll()
                    ready = sel.select(0.1)
                    if ready:
                        try:
                            data = os.read(master_fd, 1024)
                            if not data:
                                break  # EOF reached
                            buffer += data
                            last_output_time = time.time()

                            while b'\n' in buffer:
                                line_bytes, buffer = buffer.split(b'\n', 1)
                                line = self._strip_ansi(line_bytes.decode('utf-8', errors='replace').strip())

                                if not line or self._is_noise_line(line):
                                    continue

                                if line_parser:
                                    chunk, line_tokens = line_parser(line)
                                    if line_tokens is not None:
                                        tokens = line_tokens
                                else:
                                    chunk = line

                                if chunk:
                                    self._raise_if_rate_limited_output(cli_name, chunk)
                                    yielded_any = True
                                    last_output_time = time.time()
                                    yield chunk

                        except OSError:
                            break  # Master fd closed or EOF
                    elif process.poll() is not None:
                        break  # Process finished and no more data
                    elif time.time() - last_output_time > self._CLI_TIMEOUT:
                        process.kill()
                        raise Exception(
                            f"{cli_name} CLI timed out ({self._CLI_TIMEOUT}s with no output) "
                            f"— likely waiting for auth. Run /login {cli_name}."
                        )

                # Flush remaining buffer if it doesn't end in newline
                if buffer:
                    line = buffer.decode('utf-8', errors='replace').strip()
                    if line and not self._is_noise_line(line):
                        if line_parser:
                            chunk, line_tokens = line_parser(line)
                            if line_tokens is not None:
                                tokens = line_tokens
                        else:
                            chunk = line
                        if chunk:
                            self._raise_if_rate_limited_output(cli_name, chunk)
                            yielded_any = True
                            yield chunk

            finally:
                sel.close()
                os.close(master_fd)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

            if process.returncode != 0 and not yielded_any:
                raise Exception(f"{cli_name} CLI failed")
            if not yielded_any:
                raise Exception(f"{cli_name} CLI returned no usable output.")

            self._record_usage(cli_name, model, "", tier, tokens=tokens)
        except RateLimitError:
            raise
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _prioritize_models(self, models: List[str]) -> List[str]:
        """Orders models by user-defined [routing] priority if set, otherwise
        by which CLIs we have active subscriptions for."""
        if self.priority:
            def rank(model: str) -> int:
                scav_provider = self.PROVIDER_MAP.get(model.split("/")[0])
                try:
                    return self.priority.index(scav_provider)
                except ValueError:
                    return len(self.priority)
            return sorted(models, key=rank)

        priority_models = []
        other_models = []
        rate_limited = []

        for model in models:
            cli = self._cli_for_model(model)
            if self.is_rate_limited(cli):
                rate_limited.append(model)
            elif self.cli_auth_status.get(cli):
                priority_models.append(model)
            else:
                other_models.append(model)

        # Rate-limited CLIs go last — they may have recovered by the time
        # all higher-priority options are exhausted.
        return priority_models + other_models + rate_limited

    @staticmethod
    def _cli_for_model(model: str) -> CLI:
        """Maps a "<provider>/<model>" entry to its CLI binary name."""
        if model.startswith("agy/"):
            return CLI.AGY
        elif model.startswith("github/") or model.startswith("codex/"):
            return CLI.CODEX
        elif model.startswith("anthropic/"):
            return CLI.CLAUDE
        elif model.startswith("ollama/"):
            return CLI.OLLAMA
        raise Exception(f"Unsupported model provider in: {model}")

    def _run_oss_stream(self, model: str, messages: List[Dict], tier: Optional[str] = None, media_files: List[Path] = []) -> Generator[str, None, None]:
        """Streams from an Ollama server (local or remote via OLLAMA_HOST)."""
        model_name = model.split("/")[-1]
        # Inject base64 images into last user message for vision-capable models.
        if media_files:
            images = [image_to_base64(p) for p in media_files if p.suffix.lower() in IMAGE_EXTS]
            if images:
                messages = list(messages)
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == Role.USER:
                        messages[i] = dict(messages[i])
                        messages[i]["images"] = images
                        break
        import queue as _queue
        chunk_q: "_queue.Queue[object]" = _queue.Queue()
        _DONE = object()
        _ERR = object()

        def _generate():
            try:
                for chunk in OllamaProvider.generate(model_name, messages):
                    chunk_q.put(chunk)
                chunk_q.put(_DONE)
            except Exception as e:
                chunk_q.put((_ERR, e))

        t = threading.Thread(target=_generate, daemon=True)
        t.start()

        yielded_any = False
        while True:
            try:
                item = chunk_q.get(timeout=self._CLI_TIMEOUT)
            except _queue.Empty:
                raise Exception(
                    f"ollama timed out ({self._CLI_TIMEOUT}s) — server stalled or unreachable."
                )
            if item is _DONE:
                break
            if isinstance(item, tuple) and item[0] is _ERR:
                raise Exception(f"ollama execution error: {item[1]}")
            yielded_any = True
            yield item  # type: ignore[misc]

        if not yielded_any:
            raise Exception("ollama returned no output.")
        self._record_usage(CLI.OLLAMA, model, "", tier)

    def request(
        self,
        prompt: str,
        tier: Optional[str] = None,
        history: List[Dict[str, str]] = [],
        cli_override: Optional[str] = None,
        model_override: Optional[str] = None,
        mode: str = "yolo",
        media_files: List[Path] = [],
    ) -> str:
        """Routes request to an appropriate CLI model with fallback logic.

        If cli_override is set (and not "auto"), that CLI is tried first.
        Non-quota failures stay pinned and surface directly; quota/rate-limit
        failures fall through to the normal auto-mode candidates.
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": Role.USER, "content": prompt}]

        resolved = self._resolve_cli_and_model(cli_override, model_override, tier)
        if resolved:
            cli_name, model = resolved
            try:
                if cli_name == "ollama":
                    return "".join(self._run_oss_stream(model, messages, tier=tier, media_files=media_files))
                return self._run_cli(cli_name, messages, model, tier=tier, mode=mode, media_files=media_files)
            except RateLimitError as e:
                self._mark_rate_limited(cli_name)
                last_exception = e
                models = self._fallback_models_after_override(cli_name, tier)
            except Exception as e:
                if self._is_rate_limit_text(str(e)):
                    self._mark_rate_limited(cli_name)
                    last_exception = e
                    models = self._fallback_models_after_override(cli_name, tier)
                else:
                    raise
        else:
            models = self._prioritize_models(self.model_map.get(tier, self.model_map[Tier.MEDIUM]))
            last_exception = None

        for model in models:
            try:
                cli = self._cli_for_model(model)
                if cli == "ollama":
                    return "".join(self._run_oss_stream(model, messages, tier=tier, media_files=media_files))
                return self._run_cli(cli, messages, model, tier=tier, mode=mode, media_files=media_files)
            except RateLimitError as e:
                self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                continue
            except Exception as e:
                if self._RATE_LIMIT_RE.search(str(e)):
                    self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                continue

        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")

    def request_stream(
        self,
        prompt: str,
        tier: Optional[str] = None,
        history: List[Dict[str, str]] = [],
        cli_override: Optional[str] = None,
        model_override: Optional[str] = None,
        mode: str = "yolo",
        media_files: List[Path] = [],
    ) -> Generator[str, None, None]:
        """Like request(), but yields output incrementally as the CLI produces it.

        Fallback to the next model only happens if the chosen CLI fails before
        yielding a real chunk; once any non-quota chunk is yielded, that model
        is committed.
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": Role.USER, "content": prompt}]

        resolved = self._resolve_cli_and_model(cli_override, model_override, tier)
        if resolved:
            cli_name, model = resolved
            try:
                gen = (
                    self._run_oss_stream(model, messages, tier=tier, media_files=media_files)
                    if cli_name == "ollama"
                    else self._run_cli_stream(cli_name, messages, model, tier=tier, mode=mode, media_files=media_files)
                )
                first_chunk = next(gen)
            except StopIteration:
                models = self._fallback_models_after_override(cli_name, tier)
                last_exception = None
            except RateLimitError as e:
                self._mark_rate_limited(cli_name)
                models = self._fallback_models_after_override(cli_name, tier)
                last_exception = e
            except Exception as e:
                if self._is_rate_limit_text(str(e)):
                    self._mark_rate_limited(cli_name)
                    models = self._fallback_models_after_override(cli_name, tier)
                    last_exception = e
                else:
                    raise
            else:
                yield first_chunk
                yield from gen
                return
        else:
            models = self._prioritize_models(self.model_map.get(tier, self.model_map[Tier.MEDIUM]))
            last_exception = None

        for model in models:
            try:
                cli = self._cli_for_model(model)
                gen = (
                    self._run_oss_stream(model, messages, tier=tier, media_files=media_files)
                    if cli == "ollama"
                    else self._run_cli_stream(cli, messages, model, tier=tier, mode=mode, media_files=media_files)
                )
                first_chunk = next(gen)
            except StopIteration:
                continue
            except RateLimitError as e:
                self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                continue
            except Exception as e:
                if self._RATE_LIMIT_RE.search(str(e)):
                    self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                continue

            yield first_chunk
            yield from gen
            return

        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")
