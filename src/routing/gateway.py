import subprocess
import shutil
import os
import re
import json
from pathlib import Path
from typing import Generator, List, Dict, Optional
from src.auth.scavenger import Scavenger
from src.routing.classifier import Classifier
from src.routing.config import load_routing_config
from src.mcp.config import load_mcp_servers

class Gateway:
    # Default auto-mode model try-order per tier. claude/codex use rolling
    # aliases ("sonnet"/"haiku"/"opus", "default") that auto-resolve to the
    # latest model. Gemini's CLI has no such aliases (its "-latest" names
    # 404) so its entries are pinned to specific model versions and need
    # updating as new generations ship — override via [routing.model_map]
    # in .agentrc.toml instead of editing this code.
    MODEL_MAP = {
        "low": ["gemini/gemini-2.5-flash", "anthropic/haiku"],
        "medium": ["anthropic/sonnet", "gemini/gemini-2.5-pro", "codex/default"],
        "high": ["anthropic/opus", "gemini/gemini-3-pro-preview", "codex/default"],
    }

    # Default model used for each CLI/tier when /cli pins a backend without
    # /model. Override via [routing.models.<cli>] in .agentrc.toml.
    CLI_DEFAULT_MODELS = {
        "claude": {"low": "haiku", "medium": "sonnet", "high": "opus"},
        "gemini": {"low": "gemini-2.5-flash", "medium": "gemini-2.5-pro", "high": "gemini-3-pro-preview"},
        "codex": {"low": "default", "medium": "default", "high": "default"},
    }

    # Maps a model's "<provider>/" prefix to the scavenger provider name used
    # for subscription detection and [routing] priority ordering.
    PROVIDER_MAP = {
        "anthropic": "anthropic",
        "gemini": "google",
        "github": "github",
        "codex": "github",
    }

    # Flags that put each CLI in non-interactive, auto-approve-edits agentic mode.
    AGENTIC_FLAGS = {
        "claude": ["--permission-mode", "acceptEdits"],
        # --approval-mode auto_edit hangs headless on the workspace-trust
        # prompt; --yolo + --skip-trust runs non-interactively.
        "gemini": ["--yolo", "--skip-trust"],
        "codex": ["-s", "workspace-write"],
    }

    # Regexes for extracting token-usage info each CLI prints (best-effort,
    # only some CLIs report this).
    USAGE_PATTERNS = {
        "codex": re.compile(r"tokens used\s*\n\s*([\d,]+)", re.IGNORECASE),
    }

    def __init__(self):
        self.scavenger = Scavenger()
        self.scavenger.apply_to_env()
        self.headers = self.scavenger.get_all_headers()
        self.active_providers = self.scavenger.get_active_providers()
        self.classifier = Classifier()
        # Populated after each completed request/request_stream call with
        # {"cli": ..., "model": ..., "tokens": int|None, "tier": ...}.
        self.last_usage: Optional[Dict] = None
        self.mcp_servers = load_mcp_servers()
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
        # Merge per-server so we don't clobber servers the user added by hand.
        settings.setdefault("mcpServers", {}).update(self.mcp_servers)
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

    def _record_usage(self, cli_name: str, model: str, err_output: str, tier: Optional[str]) -> None:
        self.last_usage = {
            "cli": cli_name,
            "model": model,
            "tokens": self._parse_usage(cli_name, err_output),
            "tier": tier,
        }

    def _build_cmd(self, cli_name: str, messages: List[Dict[str, str]], model: str):
        """Builds the subprocess argv + env for invoking a CLI with a prompt."""
        cli_path = shutil.which(cli_name)
        if not cli_path:
            raise Exception(f"'{cli_name}' CLI not found on PATH. Install it and ensure it's accessible.")

        full_prompt = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            full_prompt += f"{role.upper()}:\n{content}\n\n"

        model_name = model.split("/")[-1]

        if cli_name == "gemini":
            cmd = [cli_path, "--prompt", full_prompt, "--model", model_name]
        elif cli_name == "codex":
            # codex exec uses the account's default model; explicit model
            # aliases (e.g. "default") are not valid -m values.
            cmd = [cli_path, "exec", full_prompt]
        elif cli_name == "claude":
            cmd = [cli_path, "-p", full_prompt, "--model", model_name]
        else:
            cmd = [cli_path, full_prompt]

        # Insert agentic auto-approve flags. codex's go after the "exec"
        # subcommand; the others are top-level flags.
        insert_at = 2 if cli_name == "codex" else 1
        agentic_flags = self.AGENTIC_FLAGS.get(cli_name, [])
        cmd[insert_at:insert_at] = agentic_flags

        # Wire up MCP servers declared in .agentrc.toml, if any.
        if self.mcp_servers:
            if cli_name == "claude" and self.claude_mcp_config_path:
                cmd += ["--mcp-config", str(self.claude_mcp_config_path)]
            elif cli_name == "codex":
                mcp_flags = []
                for name, server in self.mcp_servers.items():
                    for key, value in server.items():
                        mcp_flags += ["-c", f"mcp_servers.{name}.{key}={json.dumps(value)}"]
                # -c overrides must precede the prompt positional argument.
                flag_pos = insert_at + len(agentic_flags)
                cmd[flag_pos:flag_pos] = mcp_flags
            # gemini reads mcpServers from .gemini/settings.json automatically.

        # Prepare a clean environment for the subprocess
        env = os.environ.copy()
        if "GOOGLE_CLOUD_PROJECT" in env:
            del env["GOOGLE_CLOUD_PROJECT"]

        return cmd, env

    @staticmethod
    def _is_noise_line(line: str) -> bool:
        """Diagnostic lines some CLIs (e.g. gemini) print to stdout."""
        return line.startswith("[ExtensionManager]") or "MCP issues detected" in line

    def _run_cli(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None) -> str:
        """Executes a local CLI (gemini, codex, claude) and returns its full output."""
        cmd, env = self._build_cmd(cli_name, messages, model)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            output = result.stdout.strip()
            err_output = result.stderr.strip()

            if result.returncode != 0 and not output:
                raise Exception(f"{cli_name} CLI failed: {err_output}")

            cleaned_lines = [line for line in output.split("\n") if not self._is_noise_line(line)]
            final_output = "\n".join(cleaned_lines).strip()
            if not final_output:
                raise Exception(f"{cli_name} CLI returned no usable output. STDOUT: {output} STDERR: {err_output}")
            self._record_usage(cli_name, model, err_output, tier)
            return final_output
        except Exception as e:
            raise Exception(f"{cli_name} CLI execution error: {e}")

    def _run_cli_stream(self, cli_name: str, messages: List[Dict[str, str]], model: str, tier: Optional[str] = None) -> Generator[str, None, None]:
        """Executes a local CLI and yields its stdout incrementally, line by line."""
        cmd, env = self._build_cmd(cli_name, messages, model)
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1
            )
            yielded_any = False
            try:
                for line in process.stdout:
                    if self._is_noise_line(line):
                        continue
                    yielded_any = True
                    yield line
            finally:
                process.stdout.close()
                process.wait()

            err_output = process.stderr.read().strip()
            if process.returncode != 0 and not yielded_any:
                raise Exception(f"{cli_name} CLI failed: {err_output}")
            if not yielded_any:
                raise Exception(f"{cli_name} CLI returned no usable output. STDERR: {err_output}")

            self._record_usage(cli_name, model, err_output, tier)
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

        for model in models:
            scav_provider = self.PROVIDER_MAP.get(model.split("/")[0])
            if scav_provider in self.active_providers or (scav_provider == "google" and "gemini_cli" in self.active_providers):
                priority_models.append(model)
            else:
                other_models.append(model)

        return priority_models + other_models

    @staticmethod
    def _cli_for_model(model: str) -> str:
        """Maps a "<provider>/<model>" entry to its CLI binary name."""
        if model.startswith("gemini/"):
            return "gemini"
        elif model.startswith("github/") or model.startswith("codex/"):
            return "codex"
        elif model.startswith("anthropic/"):
            return "claude"
        raise Exception(f"Unsupported model provider in: {model}")

    def request(
        self,
        prompt: str,
        tier: Optional[str] = None,
        history: List[Dict[str, str]] = [],
        cli_override: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> str:
        """Routes request to appropriate CLI model with fallback logic.

        If cli_override is set (and not "auto"), that CLI is used directly
        with no fallback. model_override picks the exact model name for it,
        defaulting to CLI_DEFAULT_MODELS[cli_override][tier].
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": "user", "content": prompt}]

        if cli_override and cli_override != "auto":
            if cli_override not in self.cli_default_models:
                raise Exception(f"Unknown CLI override: {cli_override}")
            model_name = model_override or self.cli_default_models[cli_override][tier]
            return self._run_cli(cli_override, messages, f"{cli_override}/{model_name}", tier=tier)

        models = self.model_map.get(tier, self.model_map["medium"])
        models = self._prioritize_models(models)

        last_exception = None
        for model in models:
            try:
                return self._run_cli(self._cli_for_model(model), messages, model, tier=tier)
            except Exception as e:
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
    ) -> Generator[str, None, None]:
        """Like request(), but yields output incrementally as the CLI produces it.

        Fallback to the next model only happens if the chosen CLI produces no
        output at all; once any chunk is yielded, that model is committed.
        """
        if tier is None:
            tier = self.classifier.evaluate(prompt)

        messages = history + [{"role": "user", "content": prompt}]

        if cli_override and cli_override != "auto":
            if cli_override not in self.cli_default_models:
                raise Exception(f"Unknown CLI override: {cli_override}")
            model_name = model_override or self.cli_default_models[cli_override][tier]
            yield from self._run_cli_stream(cli_override, messages, f"{cli_override}/{model_name}", tier=tier)
            return

        models = self._prioritize_models(self.model_map.get(tier, self.model_map["medium"]))

        last_exception = None
        for model in models:
            try:
                gen = self._run_cli_stream(self._cli_for_model(model), messages, model, tier=tier)
                first_chunk = next(gen)
            except StopIteration:
                continue
            except Exception as e:
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
