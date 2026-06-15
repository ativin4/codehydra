import subprocess
import shutil
import os
import re
import json
import sys
import time
from pathlib import Path
from typing import Generator, List, Dict, Optional
from src.auth.scavenger import Scavenger
from src.routing.classifier import Classifier
from src.routing.config import load_routing_config
from src.routing.claude_session import ClaudeSession
from src.routing.oss_provider import OllamaProvider
from src.mcp.config import load_mcp_servers
from src.routing.constants import CLI, Mode, Role, Tier
from src.tools.media import IMAGE_EXTS, PDF_EXTS, VIDEO_EXTS, image_to_base64, pdf_to_text


class Gateway:
    # Default auto-mode model try-order per tier. claude/codex use rolling
    # aliases ("sonnet"/"haiku"/"opus", "default") that auto-resolve to the
    # latest model. Gemini's CLI has no such aliases (its "-latest" names
    # 404) so its entries are pinned to specific model versions and need
    # updating as new generations ship — override via [routing.model_map]
    # in .agentrc.toml instead of editing this code.
    MODEL_MAP = {
        Tier.LOW:    [f"anthropic/haiku",  f"codex/default", f"gemini/gemini-2.5-flash", f"ollama/llama3.2"],
        Tier.MEDIUM: [f"anthropic/sonnet", f"codex/default", f"gemini/gemini-2.5-pro",   f"ollama/llama3.2"],
        Tier.HIGH:   [f"anthropic/opus",   f"codex/default", f"gemini/gemini-2.5-pro",   f"ollama/llama3.3"],
    }

    # Default model used for each CLI/tier when /cli pins a backend without
    # /model. Override via [routing.models.<cli>] in .agentrc.toml.
    CLI_DEFAULT_MODELS = {
        CLI.CLAUDE: {Tier.LOW: "haiku",            Tier.MEDIUM: "sonnet",          Tier.HIGH: "opus"},
        CLI.GEMINI: {Tier.LOW: "gemini-2.5-flash", Tier.MEDIUM: "gemini-2.5-pro",  Tier.HIGH: "gemini-2.5-pro"},
        CLI.CODEX:  {Tier.LOW: "default",          Tier.MEDIUM: "default",         Tier.HIGH: "default"},
        CLI.OLLAMA: {Tier.LOW: "llama3.2",         Tier.MEDIUM: "llama3.2",        Tier.HIGH: "llama3.3"},
    }

    # Maps a model's "<provider>/" prefix to the scavenger provider name used
    # for subscription detection and [routing] priority ordering.
    PROVIDER_MAP = {
        "anthropic": "anthropic",
        "gemini":    "google",
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
            # acceptEdits only auto-approves file edits - Bash and MCP tool
            # calls still prompt and hang headless. bypassPermissions is the
            # real full-auto mode.
            CLI.CLAUDE: ["--permission-mode", "bypassPermissions"],
            # --approval-mode auto_edit hangs headless on the workspace-trust
            # prompt; --yolo + --skip-trust runs non-interactively.
            CLI.GEMINI: ["--yolo", "--skip-trust"],
            # -a never: don't escalate to the user for approval (which would
            # hang headless) - MCP/exec tool calls run directly.
            CLI.CODEX:  ["-s", "workspace-write", "-a", "never"],
        },
        Mode.PLAN: {
            CLI.CLAUDE: ["--permission-mode", "plan"],
            CLI.GEMINI: ["--approval-mode", "plan", "--skip-trust"],
            CLI.CODEX:  ["-s", "read-only", "-a", "never"],
        },
    }

    # Max non-system messages passed to one-shot CLIs (gemini/codex/ollama).
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
        CLI.GEMINI: [CLI.GEMINI],  # first interactive launch walks through OAuth
    }

    def __init__(self):
        self.scavenger = Scavenger()
        self.scavenger.apply_to_env()
        self.headers = self.scavenger.get_all_headers()
        self.active_providers = self.scavenger.get_active_providers()
        # Per-CLI logged-in status (claude/gemini/codex), used to prioritize
        # routing - more reliable than active_providers, which only reflects
        # scavenged token files and can miss CLIs that manage their own auth
        # (e.g. claude's keychain entry) or conflate unrelated tokens (e.g.
        # a `gh` CLI login surfaces as "github" but doesn't mean codex is set up).
        self.cli_auth_status = self.scavenger.get_cli_auth_status()
        self.cli_auth_status[CLI.OLLAMA] = OllamaProvider.is_available()
        # Persistent `claude -p --input-format stream-json` process (see
        # claude_session.py). _claude_history_len is the count of non-system
        # messages sent so far; used to detect new/resumed conversations that
        # need a fresh process instead of a continuation.
        self._claude_session: Optional["ClaudeSession"] = None
        self._claude_history_len = 0
        self.classifier = Classifier()
        # Populated after each completed request/request_stream call with
        # {"cli": ..., "model": ..., "tokens": int|None, "tier": ...}.
        self.last_usage: Optional[Dict] = None
        # cli_name -> unix timestamp until which that CLI is rate-limited.
        # Populated when a request fails with a rate-limit error; checked in
        # _prioritize_models to skip the CLI until its window resets.
        self._rate_limited_until: Dict[str, float] = {}
        self.mcp_servers = {**self._builtin_mcp_servers(), **load_mcp_servers()}
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

    @staticmethod
    def _builtin_mcp_servers() -> Dict[str, Dict]:
        """The "codehydra-tools" MCP server (src/mcp/server.py) gives the
        active CLI extra CodeHydra-native tools: `dispatch_agents` to fan
        independent sub-tasks out to parallel CodeHydra-routed agents
        (mirroring Claude Code's Task tool), and `run_in_background` /
        `get_background_output` / `stop_background_task` for long-lived
        processes (mirroring Claude Code's background Bash + Monitor).
        Disabled for sub-agents themselves (CODEHYDRA_ENABLE_SUBAGENTS=0) to
        avoid unbounded recursive fan-out.
        """
        if os.environ.get("CODEHYDRA_ENABLE_SUBAGENTS") == "0":
            return {}
        return {
            "codehydra-tools": {
                "command": sys.executable,
                "args": ["-m", "src.mcp.server"],
            }
        }

    def _write_mcp_configs(self) -> Optional[Path]:
        """Materializes MCP server definitions into the config files/flags
        each backend CLI expects.

        - claude: written to .codehydra/mcp-config.json, passed via
          --mcp-config at invocation time.
        - gemini: merged into the project's .gemini/settings.json under
          "mcpServers" (the only way gemini CLI picks up MCP servers).
        - codex: no file needed; servers are passed per-invocation as
          `-c mcp_servers.<name>.*` overrides (see _build_cmd).
        """
        if not self.mcp_servers:
            return None

        claude_config_path = Path(".codehydra") / "mcp-config.json"
        self._write_json_if_changed(claude_config_path, {"mcpServers": self.mcp_servers})

        gemini_settings_path = Path(".gemini") / "settings.json"
        settings = {}
        if gemini_settings_path.exists():
            try:
                with open(gemini_settings_path, "r") as f:
                    settings = json.load(f)
            except Exception:
                settings = {}
        # Gemini loads MCP servers synchronously at startup — each server spawns
        # a subprocess before the first token is sent, adding ~3-5s per server.
        # We skip injecting codehydra-tools into gemini to keep its latency low;
        # claude and codex get MCP support via --mcp-config / -c flags instead.
        user_mcp = {k: v for k, v in settings.get("mcpServers", {}).items()
                    if k not in self.mcp_servers}
        if user_mcp:
            settings["mcpServers"] = user_mcp
        else:
            settings.pop("mcpServers", None)
        # Disable blocking startup checks. Key names from official settings schema:
        # general.enableAutoUpdate blocks on version check; ui.renderProcess
        # starts an Ink render subprocess we don't need in headless mode.
        settings.setdefault("general", {}).update({
            "enableAutoUpdate": False,
            "enableAutoUpdateNotification": False,
        })
        settings.setdefault("ui", {})["renderProcess"] = False
        self._write_json_if_changed(gemini_settings_path, settings)

        return claude_config_path

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
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

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
    def _parse_gemini_stream_line(line: str) -> tuple:
        """Returns (text_chunk, total_tokens), either of which may be None."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None, None
        if data.get("type") == "message" and data.get("role") == Role.ASSISTANT and data.get("delta"):
            return data.get("content"), None
        elif data.get("type") == "result":
            return None, (data.get("stats") or {}).get("total_tokens")
        return None, None

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

        # For non-gemini CLIs, inject media content as text before assembling
        # the prompt. Gemini handles @path refs natively so we leave those for
        # the prompt string below.
        if media_files and cli_name != CLI.GEMINI:
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

        # For Claude one-shot mode, extract the system message and pass it via
        # --system-prompt so it's treated as a real system prompt, not injected
        # as SYSTEM:\n...\n\n text (which Claude detects as prompt injection).
        claude_system_prompt = ""
        full_prompt = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if cli_name == CLI.CLAUDE and role == Role.SYSTEM:
                claude_system_prompt = content
                continue
            full_prompt += f"{role.upper()}:\n{content}\n\n"

        model_name = model.split("/")[-1]

        if cli_name == CLI.GEMINI:
            # Prepend @path refs so gemini resolves them natively (multimodal).
            if media_files:
                refs = " ".join(f"@{p.absolute()}" for p in media_files)
                full_prompt = refs + "\n\n" + full_prompt
            cmd = [cli_path, "--prompt", full_prompt, "--model", model_name]
            # Plain text mode buffers the whole response and prints it at
            # once at the end; stream-json emits incremental text deltas.
            if stream:
                cmd += ["--output-format", "stream-json"]
        elif cli_name == CLI.CODEX:
            # codex exec uses the account's default model; explicit model
            # aliases (e.g. "default") are not valid -m values.
            cmd = [cli_path, "exec", full_prompt]
        elif cli_name == CLI.CLAUDE:
            cmd = [cli_path, "-p", full_prompt, "--model", model_name]
            if claude_system_prompt:
                cmd += ["--system-prompt", claude_system_prompt]
        else:
            cmd = [cli_path, full_prompt]

        # Insert mode flags (yolo/plan). codex's (-s/-a) are global flags and
        # must precede the "exec" subcommand; the others are top-level flags.
        insert_at = 1
        mode_flags = self.MODE_FLAGS.get(mode, self.MODE_FLAGS[Mode.YOLO]).get(cli_name, [])
        cmd[insert_at:insert_at] = mode_flags

        # Wire up MCP servers declared in .agentrc.toml, if any.
        if self.mcp_servers:
            if cli_name == CLI.CLAUDE and self.claude_mcp_config_path:
                cmd += ["--mcp-config", str(self.claude_mcp_config_path)]
            elif cli_name == CLI.CODEX:
                mcp_flags = []
                for name, server in self.mcp_servers.items():
                    for key, value in server.items():
                        mcp_flags += ["-c", f"mcp_servers.{name}.{key}={json.dumps(value)}"]
                # -c overrides must precede the prompt positional argument
                # but after "exec" (at index 1 + len(mode_flags)).
                flag_pos = insert_at + len(mode_flags) + 1
                cmd[flag_pos:flag_pos] = mcp_flags
            # gemini reads mcpServers from .gemini/settings.json automatically.

        # Prepare a clean environment for the subprocess
        env = os.environ.copy()
        if "GOOGLE_CLOUD_PROJECT" in env:
            del env["GOOGLE_CLOUD_PROJECT"]
        if cli_name == CLI.GEMINI:
            # Bypass folder trust prompt in headless mode (replaces --skip-trust flag).
            env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

        return cmd, env

    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07|\x1b[@-Z\\-_]")

    # Patterns that indicate a subscription rate-limit (not a bug/auth failure).
    _RATE_LIMIT_RE = re.compile(
        r"rate.?limit|too many requests|quota exceeded|429|overloaded|"
        r"capacity|try again|please wait|usage limit",
        re.IGNORECASE,
    )
    # Default cooldown when no retry-after header is available.
    _RATE_LIMIT_COOLDOWN = 60  # seconds

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        return cls._ANSI_RE.sub("", text)

    def _mark_rate_limited(self, cli_name: str, seconds: int = _RATE_LIMIT_COOLDOWN) -> None:
        self._rate_limited_until[cli_name] = time.time() + seconds

    def is_rate_limited(self, cli_name: str) -> bool:
        return time.time() < self._rate_limited_until.get(cli_name, 0)

    def rate_limit_resets_in(self, cli_name: str) -> Optional[int]:
        """Seconds until cooldown expires, or None if not rate-limited."""
        remaining = self._rate_limited_until.get(cli_name, 0) - time.time()
        return int(remaining) + 1 if remaining > 0 else None

    @staticmethod
    def _is_noise_line(line: str) -> bool:
        """Diagnostic lines some CLIs (e.g. gemini) print to stdout."""
        return line.startswith("[ExtensionManager]") or "MCP issues detected" in line

    def _run_cli(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None, mode: str = "yolo", media_files: List[Path] = []) -> str:
        """Executes a local CLI (gemini, codex, claude) and returns its full output."""
        cmd, env = self._build_cmd(cli_name, messages, model, mode, media_files=media_files)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            output = result.stdout.strip()
            err_output = result.stderr.strip()

            if result.returncode != 0 and not output:
                raise Exception(f"{cli_name} CLI failed: {err_output}")

            output = self._strip_ansi(output)
            cleaned_lines = [line for line in output.split("\n") if not self._is_noise_line(line)]
            final_output = "\n".join(cleaned_lines).strip()
            if not final_output:
                raise Exception(f"{cli_name} CLI returned no usable output. STDOUT: {output} STDERR: {err_output}")
            self._record_usage(cli_name, model, err_output, tier)
            return final_output
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _run_claude_persistent_stream(self, messages: List[Dict[str, str]], model: str, tier: Optional[str], mode: str) -> Generator[str, None, None]:
        """Sends one turn through a persistent claude session (claude_session.py),
        starting/restarting it if the conversation doesn't match what it has
        already seen (new conversation, /resume, or /mode//model change)."""
        model_name = model.split("/")[-1]
        mode_flags = self.MODE_FLAGS.get(mode, self.MODE_FLAGS[Mode.YOLO])["claude"]
        user_turns = [m for m in messages if m["role"] != Role.SYSTEM]
        if not user_turns:
            raise Exception("claude CLI execution error: no user message to send")

        # A continuation means the session has already seen all prior messages
        # and just needs the new last user turn. We check the total non-system
        # message count: if it equals what we last recorded + 1 new user turn
        # and +1 assistant turn for each prior round (i.e. len == prev + 2 or
        # len == 1 for the very first turn), it's a continuation.
        # Simpler: _claude_history_len stores len(user_turns) after each send,
        # so a continuation is exactly len(user_turns) == _claude_history_len + 1.
        is_continuation = (
            self._claude_session is not None
            and self._claude_session.alive()
            and self._claude_session.matches(model_name, mode_flags)
            and len(user_turns) == self._claude_history_len + 1
        )

        if not is_continuation:
            if self._claude_session is not None:
                self._claude_session.close()
            system_prompt = next((m["content"] for m in messages if m["role"] == Role.SYSTEM), "")
            self._claude_session = ClaudeSession(
                model_name, mode_flags, system_prompt=system_prompt,
                mcp_config_path=self.claude_mcp_config_path,
            )
            self._claude_history_len = 0

        tokens = None
        yielded_any = False
        try:
            from src.routing.claude_session import THINKING_START, THINKING_END
            sentinels = {THINKING_START, THINKING_END}
            for chunk, line_tokens in self._claude_session.send(user_turns[-1]["content"]):
                if line_tokens is not None:
                    tokens = line_tokens
                if chunk:
                    if chunk not in sentinels:
                        yielded_any = True
                    yield chunk
        except Exception as e:
            self._claude_session.close()
            self._claude_session = None
            self._claude_history_len = 0
            raise Exception(f"claude CLI execution error: {e}")

        self._claude_history_len = len(user_turns)
        if not yielded_any:
            raise Exception("claude CLI returned no usable output.")
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

    def close(self) -> None:
        """Releases any persistent CLI sessions. Call on app exit."""
        if self._claude_session is not None:
            self._claude_session.close()
            self._claude_session = None

    def _run_cli_stream(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None, mode: str = "yolo", media_files: List[Path] = []) -> Generator[str, None, None]:
        """Executes a local CLI and yields its output incrementally.

        claude/gemini use stream-json so chunks are real token deltas as the
        model generates them; codex has no equivalent and yields its output
        (still effectively one chunk near the end) line by line.
        """
        if cli_name == CLI.CLAUDE:
            yield from self._run_claude_persistent_stream(messages, model, tier, mode)
            return

        stream_json = cli_name == CLI.GEMINI
        cmd, env = self._build_cmd(cli_name, messages, model, mode, stream=stream_json, media_files=media_files)
        line_parser = self._parse_gemini_stream_line if cli_name == CLI.GEMINI else None

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
                                    yielded_any = True
                                    yield chunk
                                    
                        except OSError:
                            break # Master fd closed or EOF
                    elif process.poll() is not None:
                        break # Process finished and no more data
                
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
                            yielded_any = True
                            yield chunk

            finally:
                sel.close()
                os.close(master_fd)
                process.wait()

            if process.returncode != 0 and not yielded_any:
                raise Exception(f"{cli_name} CLI failed")
            if not yielded_any:
                raise Exception(f"{cli_name} CLI returned no usable output.")

            self._record_usage(cli_name, model, "", tier, tokens=tokens)
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
        if model.startswith("gemini/"):
            return CLI.GEMINI
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
        yielded_any = False
        try:
            for chunk in OllamaProvider.generate(model_name, messages):
                yielded_any = True
                yield chunk
        except Exception as e:
            raise Exception(f"ollama execution error: {e}")
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
        """Routes request to appropriate CLI model with fallback logic.

        If cli_override is set (and not "auto"), that CLI is used directly
        with no fallback. model_override picks the exact model name for it,
        defaulting to CLI_DEFAULT_MODELS[cli_override][tier].
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": Role.USER, "content": prompt}]

        resolved = self._resolve_cli_and_model(cli_override, model_override, tier)
        if resolved:
            cli_name, model = resolved
            if cli_name == "ollama":
                return "".join(self._run_oss_stream(model, messages, tier=tier, media_files=media_files))
            return self._run_cli(cli_name, messages, model, tier=tier, mode=mode, media_files=media_files)

        models = self._prioritize_models(self.model_map.get(tier, self.model_map[Tier.MEDIUM]))

        last_exception = None
        for model in models:
            try:
                cli = self._cli_for_model(model)
                if cli == "ollama":
                    return "".join(self._run_oss_stream(model, messages, tier=tier, media_files=media_files))
                return self._run_cli(cli, messages, model, tier=tier, mode=mode, media_files=media_files)
            except Exception as e:
                if self._RATE_LIMIT_RE.search(str(e)):
                    self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                error_msg = str(e).split("\n")[0]
                print(f"Error on {model}: {error_msg}")
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

        Fallback to the next model only happens if the chosen CLI produces no
        output at all; once any chunk is yielded, that model is committed.
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": Role.USER, "content": prompt}]

        resolved = self._resolve_cli_and_model(cli_override, model_override, tier)
        if resolved:
            cli_name, model = resolved
            if cli_name == "ollama":
                yield from self._run_oss_stream(model, messages, tier=tier, media_files=media_files)
            else:
                yield from self._run_cli_stream(cli_name, messages, model, tier=tier, mode=mode, media_files=media_files)
            return

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
            except Exception as e:
                if self._RATE_LIMIT_RE.search(str(e)):
                    self._mark_rate_limited(self._cli_for_model(model))
                last_exception = e
                error_msg = str(e).split("\n")[0]
                print(f"Error on {model}: {error_msg}")
                continue

            yield first_chunk
            yield from gen
            return

        raise last_exception or Exception("All CLIs failed. No active subscriptions found.")


if __name__ == "__main__":
    gateway = Gateway()
    # Basic test
    try:
        res = gateway.request("Say hello", tier="low")
        print(f"Response: {res}")
    except Exception as e:
        print(f"Test failed: {e}")
